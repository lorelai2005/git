# 抖音开放平台接口参考（本 Skill 实际用到的部分）

> ⚠️ **使用前提**：本文件记录的是**本 Skill 的实现约定**，不是官方文档的镜像。
> 抖音接口路径、字段名、错误码会随版本变化，所以：
> - 所有接口路径与 HTTP 方法都在 `config/config.yaml` 的 `douyin.endpoints` / `douyin.methods` 里，**改版只改配置**；
> - 所有字段名通过 `src/dyreview/metrics.py` 的别名表兼容，**新增字段名只加一行别名**；
> - 错误码分类在 `douyin.error_codes` 里，**用 `doctor --network` 和真实报错校准**。
> 首次接入时请同时对照官方文档：[open.douyin.com](https://open.douyin.com/platform/resource/docs/openapi/account-permission/refresh-access-token/) ·
> [开放能力列表](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/list)

---

## 1. 授权与 token 生命周期

```
客户扫码授权
   │  code（一次性，几分钟内有效）
   ▼
/oauth/access_token/           授权码换 token
   │  {access_token, expires_in, refresh_token, refresh_expires_in, open_id, scope}
   ▼
落库 dy.oauth_token（加密信封）  version=0
   │
   ├── 采集时先查缓存：剩余寿命 > 预刷新窗口 → 直接用（0 次接口调用）
   │
   └── 剩余寿命 ≤ 预刷新窗口 → /oauth/refresh_token/
         返回**新的** access_token + **新的** refresh_token（旧的立刻作废）
         → 乐观锁回写（version+1），refresh_count+1
   │
   └── 每日巡检：refresh_token 剩余 < renew_before_days → /oauth/renew_refresh_token/
         只延长 refresh_token 寿命，access_token 不变
```

### 关键事实

| 事实 | 影响 |
| --- | --- |
| **refresh_token 一次性轮换** | 并发刷新 = 后到者拿着已作废的 token 去刷 → 直接锁死。必须单飞（本项目用 PG advisory lock + 二次读库 + 乐观锁） |
| access_token / refresh_token 有效期由服务端下发 | 代码只读 `expires_in` / `refresh_expires_in`，不硬编码天数；没下发时用保守兜底 7 天 |
| `open_id` 是**应用维度**唯一 | 同一客户换应用会换 open_id，`dy.account` 用 `(client_key, open_id)` 唯一约束 |
| 数据类接口需要单独的数据权限 | 粉丝画像、封面点击率经常缺失，缺失时 facts 会写进 `data_gaps`，报告照实说明 |

### 预刷新窗口公式（`token_manager.refresh_window`）

```python
window = max(ttl × refresh_before_ratio,
             min(refresh_before_seconds, ttl × 0.5))
```

- 15 天有效期 → `max(4.5天, min(1天, 7.5天)) = 4.5 天`（提前 4.5 天刷）
- 1 小时有效期 → `max(18分钟, min(1天, 30分钟)) = 30 分钟`（不会“刚拿到就判过期”）

调整建议：token 刷新频繁报限流就把 `refresh_before_ratio` 降到 0.15。

---

## 2. 接口清单

| 配置键 | 默认路径 | 方法 | 用途 | 必需权限 |
| --- | --- | --- | --- | --- |
| `oauth_access_token` | `/oauth/access_token/` | GET | 授权码换 token（首次接入） | — |
| `oauth_refresh_token` | `/oauth/refresh_token/` | GET | 刷新 access_token（**会轮换 refresh_token**） | — |
| `oauth_renew_refresh_token` | `/oauth/renew_refresh_token/` | GET | 续期 refresh_token | — |
| `oauth_client_token` | `/oauth/client_token/` | GET | 应用级 token（连通性探测用） | — |
| `userinfo` | `/oauth/userinfo/` | POST | 用户信息（昵称/头像） | `user_info` |
| `user_data` | `/api/douyin/v1/user/user_data/` | POST | 账号指标：粉丝数、净增、作品数 | 数据开放服务 |
| `fan_data` | `/api/douyin/v1/user/fan_data/` | POST | 粉丝画像 | 数据开放服务 + 画像权限 |
| `video_list` | `/api/douyin/v1/video/video_list/` | POST | 作品列表（游标分页） | `video.list` |
| `video_data` | `/api/douyin/v1/video/video_data/` | POST | 作品指标（批量 item_ids） | `video.data` |

### 统一调用约定（`douyin_client._call`）

1. `access_token` 与 `open_id` 走 **query 参数**；`video_data` 的 `item_ids` 走 **JSON body**；
2. 返回 envelope 有两种，两种都处理：
   ```json
   {"data": {...}, "extra": {"error_code": 0, "description": "", "logid": "..."}}   // 数据类接口
   {"data": {"error_code": 0, "access_token": "...", ...}}                          // oauth 类接口
   ```
   错误码优先取 `extra.error_code`，其次 `data.error_code`；为 `0`/缺省视为成功；
3. `logid` 一律落到 `dy.oauth_event.logid` 或异常里，排障时给官方提工单要用；
4. 限速：客户端令牌桶（`douyin.qps`，默认 4/秒）+ 失败退避重试（`retries`/`retry_backoff_seconds`）。

---

## 3. 错误码分类与处置

代码不认具体数字，认**分类**（`douyin.error_codes`）：

| 分类 | 默认包含 | 判定后行为 |
| --- | --- | --- |
| `token_invalid` | `2190008`, `2190012`, `10008`（HTTP 401） | 抛 `TokenInvalid` → 上层刷新一次并**重试一次** |
| `refresh_invalid` | `2190002`, `2190003` | 抛带 `refresh_invalid` 标记的异常 → 标记账号 `reauth_required`，告警，不重试 |
| `rate_limited` | `2190005`, `1000005`, `429` | 退避重试，超限后抛 `RateLimited` |
| `scope_missing` | `2190006` | 直接失败，提示补权限（重试无意义） |
| 其它 | — | `DouyinApiError`，`status>=500` 视为可重试 |

> 这几组数字是**工程默认值**。首次接入务必用真实错误校准：
> `dyreview doctor --network` → 人为用过期 token 打一次 → 看 `dy.oauth_event.error_code` 实际值 → 回填配置。

---

## 4. 字段别名表（`metrics.py`）

抖音同一指标在不同接口/版本里有多种命名，别名表统一成一个列名：

| 归一化列 | 接受的原始字段 |
| --- | --- |
| `play_count` | `play_count`, `play_volume`, `video_play_count`, `play_cnt`, `play`, `vv` |
| `like_count` | `like_count`, `digg_count`, `like_cnt`, `digg`, `like` |
| `comment_count` | `comment_count`, `comment_cnt`, `comments` |
| `share_count` | `share_count`, `share_cnt`, `share`, `share_volume` |
| `collect_count` | `collect_count`, `collect_cnt`, `favorite_count`, `collect` |
| `finish_rate` | `finish_rate`, `finish_rate_avg`, `completion_rate`, `play_finish_rate` |
| `full_play_rate` | `full_play_rate`, `full_play_rate_avg` |
| `bounce_rate` | `bounce_rate`, `bounce_rate_2s`, `play_2s_bounce_rate`, `jump_rate` |
| `avg_play_duration_ms` | `avg_play_duration_ms`, `avg_play_duration`, `item_avg_play_duration` |
| `cover_click_rate` | `cover_click_rate`, `cover_ctr`, `click_rate` |
| `follower_count` | `follower_count`, `fans_count`, `follower_cnt`, `fans`, `total_fans` |
| `new_follower_count` | `new_follower_count`, `new_fans`, `follower_inc`, `new_follower` |
| `unfollower_count` | `unfollower_count`, `cancel_fans`, `lost_follower`, `unfollow_count` |
| `total_favorited` | `total_favorited`, `like_total`, `total_like`, `favorited_count` |
| `video_count` | `video_count`, `aweme_count`, `item_count`, `works_count` |

数值解析规则：

- `"1.2万"` → `12000`，`"3.5亿"` → `350000000`，`"8.5w"` → `85000`；
- 比率 `0.2537` / `25.37` / `"25.37%"` 统一成 `0.2537`（>1 视为百分数）；
- **解析失败返回 `None`，绝不返回 0**——“没拿到”和“0 播放”在复盘里是完全不同的结论。

新增字段名：直接在 `metrics.py` 对应元组的末尾加一个字符串，不要改 SQL。

---

## 5. 分页与批量上限

| 参数 | 默认 | 为什么有上限 |
| --- | --- | --- |
| `batch.video_list_count` | 20 | 单页条数 |
| `batch.max_video_pages` | 25 | 防大号作品上万条拖死定时任务（500 条后截断） |
| `batch.video_data_batch` | 20 | 单次 `item_ids` 条数（按官方上限保守取 20） |
| `batch.video_data_rounds` | 3 | 单次采集最多 3 轮批量 = 60 条作品指标 |

超过上限时**日志会告警但不报错**，用已有数据处理；要补历史就单独跑 `--kind full`
（`full` 模式优先按发布时间倒序取最新 N 条）。

翻页游标不推进（`next_cursor <= cursor`）会立即停止并告警，避免死循环。
