# 故障排查清单

排障顺序永远是：**`dyreview doctor --network` → 看 `code` → 对症处理**。
所有命令失败都返回 JSON（`{"ok": false, "code": "...", "message": "..."}`），不要靠猜。

---

## 1. 配置类

### `CONFIG_INVALID`：缺少必填配置项
```
缺少必填配置项 database.dsn（对应环境变量是否已注入？未解析：['DYREVIEW_DB_DSN']）
```
- 检查 `.env` 是否被加载（cron 环境不会自动读 `.env`！要用 `EnvironmentFile=` 或 `set -a; source .env`）；
- 检查 `config/config.yaml` 是否存在（不存在会回退到 `config.example.yaml` 并打警告）；
- `unresolved_env` 就是缺的变量名清单。

### `crypto.key 格式错误`
Fernet 密钥必须是 32 字节 base64：
```bash
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```
密钥轮换：新 key 放 `crypto.key`，旧 key 放 `crypto.key_previous`，解出来的旧 token 会在下次刷新时自动用新 key 重新封装。

### 提示“未配置 crypto.key，token 明文入库”
生产必须修。测试环境可容忍（库里是 `raw:` 前缀信封）。

---

## 2. 数据库类

### `SCHEMA_NOT_READY`
```
数据库 schema `dy` 未就绪：缺少表 [...]
```
`dyreview init-db` 是幂等的，直接重跑。若报权限错误：需要给 `dyreview` 账号 `CREATE` on database + `USAGE/CREATE` on schema `dy`。

### 连接超时
- 193 防火墙是否放行 5432（只放行 Crabot 服务器 IP 更安全）；
- `database.statement_timeout_ms` 是否太小（大号作品多时 `video_window_growth` 会慢）；
- 连接数不够：每个 CLI 进程一条连接，cron 并发高时调小并发或加 PgBouncer。

### 慢查询
`video_window_growth` 里对每条作品做 5 次相关子查询。作品数 > 2000 时：
- 确认 `video_metric(account_id, stat_date DESC)` 索引存在（建表脚本已带）；
- 或者把复盘窗口缩短到 7 天。

---

## 3. Token 类（最高频）

### `REAUTH_REQUIRED`
refresh_token 也失效了，代码**不会**硬刷（硬刷只会更糟）。处置：
1. 让客户重新扫码授权；
2. 把回调里的 `code` 立刻交给 `dyreview account add --name X --code <code>`（code 几分钟就过期）；
3. 检查 `dy.oauth_event` 里最近几条 `refresh_failed` 的 `error_code` 与 `logid`。

常见诱因：
- 客户在抖音 App 里手动取消了授权；
- refresh_token 超过有效期（长时间没巡检 → 把 `token keepalive` 加进 cron）；
- 开放平台应用被重置/换绑，导致 open_id 变了。

### `ACCESS_TOKEN_INVALID` 反复出现
说明刷新出来的 token 立刻又失效，通常是：
- 应用被风控（联系官方，带 logid）；
- 同一账号被多个环境同时刷（检查是否还有别的服务在跑旧版本、或在两套环境共用 client_key）；
- 本机时钟偏差 > 5 分钟（容器里常见，装 ntp）。

### 刷新被限流
把 `douyin.token.refresh_before_ratio` 从 0.3 降到 0.15（窗口变小 = 刷新次数变少），
同时确认 `collect` 的 cron 不是每几分钟跑一次。

### 并发刷新互锁（历史上的坑）
现象：两个 cron 同时刷新，一个成功后另一个报 `refresh_invalid`。
现状：已用三层保护防住（PG advisory lock + 二次读库 + 乐观锁回写）。
如果还在发生，检查是否**跨环境**共用同一份 token（比如测试环境连了生产库）。

---

## 4. 采集类

### `DATA_UNAVAILABLE`：该周期无快照
先补采：`dyreview collect --account X --kind full`。
若补采也拿不到指标：看 `dy.collect_run.error_code`；常见是应用没有 `video.data` 权限。

### 视频列表只拿到几条
- `douyin.batch.max_video_pages` 到顶了（默认 25 页 × 20 条 = 500 条）→ 日志会有“达到翻页上限”警告；
- 游标不推进 → 日志“游标未推进”，通常是接口返回结构变了，先看 `references/douyin-openapi.md` 的 `list_items` 兼容键名。

### 指标全空但列表正常
`video_data` 的 `item_ids` 上限被超了（降到 `video_data_batch=10` 试），
或作品刚发布数据还没生成（这时 `video_metric` 不写入，属于预期）。

### 粉丝画像为空
需要额外的数据权限，属预期；facts 会把它列进 `data_gaps`，报告照实说明，不硬编。

### 采集后账号 `reauth_required` 但 token 看着没过期
看 `dy.oauth_event`：如果 `refresh_invalid`，就是客户侧取消授权了。

---

## 5. 复盘 / 大模型类

### `NUMBERS_INVALID`
模型的报告里出现了事实包之外的数字。**这是保护机制在生效，不要绕过。**
正确处置：
1. 把 `validation.violations`（含 `context` 上下文）和事实包一起再喂给模型；
2. 要求“只使用事实包中的数字，不得换算、不得估算”；
3. 最多重试 1 次；仍不行就让模型只写结论与动作，数字全部用 `facts.display` 里的字符串。

`--allow-unsourced-numbers` 只用于人工确认过的场景（比如客户要求写一个目标值），默认不要用。

### 报告内容空泛
- 检查 `llm.mode`：`handoff` 模式下 Skill 不生成正文，必须由 Crabot 喂模型；
- 看 `var/reviews/<id>/prompt.md` 是不是被截断；
- 事实包 `data_gaps` 太多时模型会趋于保守，先补数据。

### `step 超限`
```
本次复盘已消耗 8 个工具步，超过上限 8
```
说明 Agent 在循环。直接用已有结论收尾；同时检查是不是 `review` 一直返回 `deduped` 又被反复重试。

### `REVIEW_BUSY`
同账号有复盘任务在跑（`task_lock` 900 秒）。等 1 分钟再试；如果卡死：
```sql
DELETE FROM dy.task_lock WHERE lock_key = 'review:1';
```

---

## 6. 飞书类

### 推不出去，日志 `code=230001` / `230002`
- 机器人不在群里 → 把应用机器人拉进群；
- `chat_id` 写错（应该是 `oc_` 开头）；
- 应用没开 `im:message:send_as_bot` 权限。

### `99991400`
飞书限流，代码已自动退避重试 2 次；持续触发说明卡片分片太多（`chunk_max_cards` 调大或 `card_max_chars` 调大）。

### 报告只推了一半
`feishu.chunk_max_cards`（默认 4）到顶了，剩下的内容没发。
处理：调大 `card_max_cards`，或把报告精简（`review.report.max_chars` 调小）。

### 卡片里表格乱码
飞书 `lark_md` 对表格支持有限；报告表格都控制在 6 列以内。超了就在正文里改成列表。

### 不想接飞书应用权限
配 `feishu.webhook_url`（群自定义机器人），`push_mode: webhook` 即可，功能受限（不能回复用户）。

---

## 7. 定时任务类

### cron 跑不起来但手动能跑
90% 是环境差异：cron 里没有 `PATH`、没 `cd` 到工作目录、没加载 `.env`。
用 `deploy/crontab.example` 里的写法（绝对路径 + `set -a; source .env; set +a`）。

### 每天都刷 token，但 access_token 有效期是 15 天
说明 `refresh_before_ratio` 配置得太大（比如 0.9），或者 `expires_in` 没被正确解析（看 `dy.oauth_token.access_ttl_seconds`）。

### 数据出现重复或跳变
- 重复：`video_metric` 主键是 `(account_id,item_id,stat_date)`，不会重复；检查是否两套环境写同一个库；
- 跳变：抖音偶发把累计值下调（清理/审核），代码用 `max(end-before, 0)` 兜底，不会出现负增量，但可能低估。
