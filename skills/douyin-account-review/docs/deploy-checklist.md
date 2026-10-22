# 部署清单（本次**未执行**，按需逐项走）

> 目标：把本 Skill 装到 193 服务器，并接到仁维 Crabot 后端 + 飞书入口。
> 每条后面是**验收标准**——不满足就不要进下一条。

---

## 0. 前置：本次交付的范围

| 已完成（本地） | 未做（需你确认后再做） |
| --- | --- |
| Skill 全部代码、配置样例、建表 SQL、文档、111 个测试 | 193 服务器上建库建表 |
| 离线 demo 全链路跑通（事实包→诊断→渲染→数字校验） | 抖音开放平台应用申请与授权 |
| SQL 用真 PG 解析器校验通过 | 飞书应用创建与机器人进群 |
| — | 定时任务上线 |
| — | Crabot 后端注册与联调 |

---

## 1. 服务器准备

```bash
# 1.1 目录
sudo mkdir -p /opt/renwei/skills && cd /opt/renwei/skills
# 把整个 douyin-account-review 目录上传到这里（不要带 .venv / var）
sudo chown -R renwei:renwei /opt/renwei/skills/douyin-account-review

# 1.2 Python 3.10+ 与虚拟环境
cd /opt/renwei/skills/douyin-account-review
python3 --version            # 需要 >= 3.10（zoneinfo / 类型语法）
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

**验收**：`.venv/bin/python -m dyreview --version` 输出版本号。

---

## 2. PostgreSQL（193）

```sql
-- 2.1 建库与账号（用超级用户执行一次）
CREATE DATABASE renwei ENCODING 'UTF8';
CREATE USER dyreview WITH PASSWORD '<强密码>';
GRANT CONNECT ON DATABASE renwei TO dyreview;
\c renwei
CREATE SCHEMA IF NOT EXISTS dy AUTHORIZATION dyreview;
GRANT USAGE, CREATE ON SCHEMA dy TO dyreview;
-- 未来若表由别人建，需要补：
ALTER DEFAULT PRIVILEGES IN SCHEMA dy GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dyreview;
```

```bash
# 2.2 建表（幂等）
.venv/bin/python -m dyreview init-db --pretty
# 期望：{"ok": true, "status": "initialized", "tables_present": 12, "version": 1}
```

```bash
# 2.3 真库冒烟：造一个 smoke_ 临时账号，跑 collect→facts→render→数字校验
#     全用假抖音响应，不碰客户数据、不消耗抖音配额，跑完自动清理
.venv/bin/python tools/smoke_db.py --pretty
# 期望：{"ok": true, "steps_run": 20+, "failures": []}
```

**验收**：`psql -c "\dt dy.*"` 能看到 12 张表；`\dv dy.*` 看到 4 个视图；
`dyreview init-db --check` 返回 `ok: true`；`tools/smoke_db.py` 返回 `ok: true` 且 `kept=false` 时库表行数已归零。

> 注意：建的是 **PostgreSQL** 表（`bigserial` / `jsonb` / `timestamptz` / `text[]`）。
> 任务文档里写的 MySQL 表适用于其它两个 Skill，本 Skill 不建 MySQL 表。

---

## 3. 环境变量与配置

```bash
cp .env.example .env && chmod 600 .env
cp config/config.example.yaml config/config.yaml
vi .env && vi config/config.yaml
```

必填：

| 变量 | 来源 |
| --- | --- |
| `DYREVIEW_DB_DSN` | `postgresql://dyreview:<pw>@<193-ip>:5432/renwei` |
| `DYREVIEW_TOKEN_KEY` | `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` |
| `DOUYIN_CLIENT_KEY` / `DOUYIN_CLIENT_SECRET` | 抖音开放平台 → 应用详情 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书开放平台 → 自建应用 |
| `FEISHU_DEFAULT_CHAT_ID` | 群设置里的 `chat_id`（`oc_` 开头） |

**验收**：`.venv/bin/python -m dyreview doctor --pretty` 全部 `ok`（`feishu_api`/`douyin_api` 需要 `--network`）。

---

## 4. 抖音开放平台

1. 创建/确认应用类型（代运营场景一般是「第三方应用」或「自建应用」）；
2. 申请权限：`user_info`、`video.list`、`video.data`，以及数据开放服务的用户数据/粉丝画像；
3. 配置授权回调地址（指向 Crabot 后端接 code 的接口）；
4. 白名单：服务器出口 IP 加入应用白名单（`open.douyin.com` 需服务器可访问）；
5. **首次接入**：客户扫码 → 回调拿到 `code` → 立刻执行
   ```bash
   .venv/bin/python -m dyreview account add --name "客户账号名" --code "<code>" --pretty
   ```
6. **校准错误码**：用一个过期 token 打一次接口，看 `dy.oauth_event.error_code` 实际值，
   与 `douyin.error_codes` 默认分类对比，不一致就改配置。

**验收**：`token status` 里该账号 `state=ready`；`collect --account X --kind full` 成功，
`dy.collect_run.status=success`，`video_metric` 有数据。

---

## 5. 飞书应用

1. 创建自建应用，开启 `im:message`（发消息）与 `im:message:send_as_bot`；
2. 把机器人添加进目标群，拿 `chat_id`；
3. 若需在群里 @ 机器人触发，配置事件订阅（指向 Crabot 后端，而不是本 Skill——本 Skill 只负责产出与推送）。

**验收**：`.venv/bin/python -m dyreview push --review-id <最近一次> --chat-id oc_xxx`
手机飞书能收到卡片，且长报告自动分成多张。

---

## 6. 定时任务

```bash
sudo cp deploy/crontab.example /etc/cron.d/douyin-account-review
sudo vi /etc/cron.d/douyin-account-review    # 改成实际路径与用户
```

**验收**：等一个周期后 `dyreview doctor` 里 `accounts` 正常、`dy.collect_run` 有 latest success；
日志文件在 `var/logs/` 且没有 ERROR。

---

## 7. Crabot 后端注册

1. 把 `manifest.yaml` 里的 `triggers` / `tools` / `schedules` 落到 Crabot 的 Skill 注册表；
2. 配置工具执行器：以 `renwei` 用户、工作目录 `/opt/renwei/skills/douyin-account-review`、
   `env_file=.env` 执行 `dyreview ...`；
3. 大模型侧：`llm.mode=handoff`（默认），Crabot 拿到 `prompt` 后调模型，再回传 `render`；
4. 飞书侧：用户消息 → 解析账号与天数 → 调 `review_start` → 模型 → `review_render` → `review_push`；
5. 配 `max_tool_steps_per_review=8` 的熔断（Skill 内部已兜底，Crabot 侧也建议加）。

**验收**：在飞书群里发「帮我复盘 <客户账号> 账号数据」，3 分钟内收到卡片报告；
`dy.review.status=pushed`，`dy.llm_usage` 有 token 记录。

---

## 8. 联调测试用例（必须全过）

| # | 场景 | 期望 |
| --- | --- | --- |
| 0 | `python tools/smoke_db.py` | 真库全链路 `ok: true`，跑完数据自动清理 |
| 1 | `dyreview demo` | `ok: true`，报告生成 |
| 2 | 正常复盘 | 卡片到达，`validation.ok=true` |
| 3 | 账号名不存在 | 回话术“没有找到…”，不报错栈 |
| 4 | 账号名模糊（部分匹配多个） | 回候选列表让用户选 |
| 5 | 10 分钟内重复请求 | 返回 `deduped`，不重复消耗 token |
| 6 | 该周期无数据 | 自动补采 / 回 `DATA_UNAVAILABLE` 话术 |
| 7 | 手工吊销客户授权 | 回 `REAUTH_REQUIRED` + 重授权指引 |
| 8 | 模型给出编造数字 | `render` 拒绝，`NUMBERS_INVALID`，不推送 |
| 9 | 超长报告 | 自动分片，最多 4 张卡片 |
| 10 | 飞书接口 500 | Skill 不崩，记录 error，可重试 |

---

## 9. 回滚

```bash
# 停定时任务
sudo rm /etc/cron.d/douyin-account-review
# 在 Crabot 注册表里把 enabled 置 false
# 数据保留（dy schema 不动，便于复盘）；如需彻底清：
# psql -c "DROP SCHEMA dy CASCADE;"
```

---

## 10. 上线后前两周观察项

- `dy.oauth_event` 里 `refresh_failed` / `reauth_required` 的数量（客户授权是最大风险）；
- `dy.collect_run` 的 `status=partial` 比例（partial 多说明字段或权限有缺口）；
- `dy.llm_usage` 的 token 消耗（评估成本，必要时换更小模型）；
- `dy.review.validation.violation_count` 的分布（模型编数字的频率，决定 prompt 是否要调）。
