# 浏览器采集与 PostgreSQL 接入说明

本文件描述本会话已配置的单账号链路，定位信息核对日期为2026-10-22。运行前仍应查看当前任务状态、工具注册和查询结果。示例账号不能自动替代其他客户账号。

## 已部署实例

| 项目 | 当前配置 |
| --- | --- |
| 客户与抖音号 | 淑情哲依，SQZY-001，82713045072 |
| douyin-mcp | `E:\chatgpt\douyin-mcp` |
| 采集和导出目录 | `E:\chatgpt\douyin-account-82713045072` |
| 专用 Chrome profile | `E:\chatgpt\douyin-account-82713045072\browser-profile` |
| Windows 任务 | `Renwei-Douyin-82713045072-Daily`；名称保留，但触发器 `DaysInterval=3` |
| 任务时间 | 每三天13:00，Asia/Shanghai；错过后补跑，插电时允许唤醒 |
| 数据库 | 193服务器 PostgreSQL，数据库 `renwei_script`，schema `review` |
| 数据库账号 ID | `cffd43ec-6a49-4dc7-91ed-db4900031504`；不是抖音号 |
| 数据库客户 ID | `694d7d32-73dc-4b4a-b3a1-a8d5b2cb28dd` |
| 服务器宿主机工具目录 | `/opt/crabot-renwei-deploy/data/tools/douyin-account-82713045072` |
| Crabot 容器内工具目录 | `/data/tools/douyin-account-82713045072` |
| MCP 注册名 | `douyin-account-82713045072` |
| 实际 MCP 注册文件 | 容器内 `/data/admin/mcp-servers.json` |

Windows 需保持用户已登录、插电和联网，可锁屏；关机或注销不能执行交互式浏览器任务。登录态可能失效，不能承诺永久有效。不复制普通 Chrome 用户目录，也不把专用 profile、Cookie 或密码送入模型、日志、服务器或数据库。

## 本机执行入口

以下 PowerShell 命令仅用于当前 Windows 电脑，不在仁维 Linux 容器里执行。

重新扫码，仅在登录失效或首次接入时使用：

~~~powershell
& 'E:\chatgpt\douyin-account-82713045072\重新登录.cmd'
~~~

立即执行一次“采集、Excel生成、上传、PostgreSQL入库”：

~~~powershell
& 'E:\chatgpt\douyin-account-82713045072\run-daily.ps1'
~~~

该入口实际调用：

~~~powershell
& 'E:\chatgpt\douyin-mcp\.venv\Scripts\python.exe' 'E:\chatgpt\douyin-account-82713045072\sync_account.py' --publish
~~~

检查任务和最近一次结果：

~~~powershell
Get-ScheduledTask -TaskName 'Renwei-Douyin-82713045072-Daily'
Get-ScheduledTaskInfo -TaskName 'Renwei-Douyin-82713045072-Daily'
Get-Content -LiteralPath 'E:\chatgpt\douyin-account-82713045072\reports\sync-status.json'
Get-Content -LiteralPath 'E:\chatgpt\douyin-account-82713045072\reports\database-import.json'
~~~

`database-import.json` 可能是上一次回执，须结合 `sync-status.json` 的本次状态、数据库最新采集日志和数据时间核对；不能仅凭文件存在宣称成功。

## 自动入库链路

`sync_account.py --publish` 通过既有 SSH 配置传送报表，再在 `crabot-renwei` 容器内调用 `ingest-postgres.cjs`。写入凭证只保存在服务器，Crabot 查询使用只读数据库连接。本 Skill 不要求重新填写抖音 client_key、client_secret、access_token 或 refresh_token。

| 表 | 内容与口径 |
| --- | --- |
| `public.clients`、`public.accounts`、`review.account_profile` | 沿用客户、账号和抖音号映射；不要仅凭昵称写入另一个客户 |
| `review.metric_snapshot` | 逐日账号指标；日期、指标编码、维度组合唯一；缺失写NULL并标记is_missing |
| `review.content`、`review.content_snapshot` | 作品身份与采集日累计快照；同一作品同一日期更新，历史日期保留 |
| `review.collect_raw` | 原始报表、申请范围、实际导出、缺失日期及来源 |
| `review.collect_run_log` | 导入范围、状态、行数、错误、trace_id；相同报表重放不新增记录 |
| `review.collect_task` | 外部Windows任务登记；enabled=false防止服务器再次采集，真实周期见retry_json和Windows触发器 |
| `review.metric_dict` | 指标定义与口径；不自行创造平台字段 |

写入在单个事务内完成。不要用“每月第1、4、7日”式 cron 冒充固定每三天；当前由 Windows 的 `DaysInterval=3` 执行，不另建 OAuth keepalive、服务器采集 cron 或自动清理任务。

未取得平台作品ID的记录使用带 `local:` 前缀的本地身份，并在原始数据和快照额外字段记录来源；不能把本地指纹称为平台作品ID。作品5秒完播率、曝光等补充字段和缺失原因保存在快照的 `extra_json`、`missing_fields_json` 中。

## MCP 实际契约

工具由 Crabot 已注册的 MCP 提供；以下是函数名和参数，不是 `dyreview` CLI 子命令。返回在 MCP `content[].text` 内为 JSON。

| 工具 | 参数 | 关键返回 |
| --- | --- | --- |
| `douyin_account_status` | `{}` | data_source、database、sync、generated_at、stale、requested_range、history_coverage |
| `douyin_account_table` | account、offset、limit（最大50） | 首批account_center、作品rows、next_offset、coverage、Excel和CSV路径 |
| `douyin_account_daily` | account、offset、limit（最大31） | total_days、days、next_offset、source_table=review.metric_snapshot |

`account` 当前只接受字符串 `"82713045072"`。它不是数据库账号UUID。新账号需完成独立接入，不能通过改这个参数实现。

`data_source=postgresql` 表示结果来自数据库，不是直接控制浏览器。当前 `stale` 在无数据或报表超过78小时时为true，适配72小时采集间隔；该阈值不是平台的登录有效期。

按日字段包括：post_cnt、play_cnt、like_cnt、comment_cnt、share_cnt、five_second_completion_rate、two_second_bounce_rate、cover_click_rate、avg_play_sec、fans_total、fans_net、fans_new、fans_lost、fans_returning。NULL是未知，比例按0到1存储。

## 历史覆盖与交付

滚动三个自然月不是固定90天；月份日数不足时取该月最后一天，账号日数据截至前一天。用 `history_coverage` 的申请天数、实际返回天数和缺失天数描述覆盖，不凭日期选择器推断完整性。

2026-10-22的已验证案例：申请2026-07-22至2026-10-21共92天，后台实际返回2026-09-09至2026-10-21共43天，前49天无记录。该案例只说明验收方式，不应硬编码为未来数据。

本机输出位于采集目录的 `reports` 及桌面 `C:\Users\lr286\Desktop\数据复盘\82713045072`。Crabot返回的 `workbook_path` 通常是服务器容器内文件路径，该Excel是数据来源或可选附表，不能替代复盘与提升方案文档。成品文档默认通过实际文件交付工具返回请求前端；服务器路径不能当作公网URL。

现有采集链路可自动生成和存储数据表；新口令默认要求额外生成一份含复盘与提升方案的文档，交付要求见 [复盘文档交付要求](review-document-delivery.md)。本地Skill文件更新不等于已注册到Crabot、已接通文档生成或前端交付。

## 旧实现隔离

本目录 `src/dyreview`、`config`、`deploy`、`.env.example`、原有测试和旧版参考文档保留为OAuth实现资料。它们仍使用旧的API与库表结构，不能套用到新链路。旧版demo通过也不能证明浏览器采集、自动入库或飞书交付成功。

当前路线仅使用本文件、`SKILL.md` 和实际部署组件。需要迁移旧CLI的报告校验、去重或推送能力时，作为独立代码改造验证，不通过修改本说明宣称迁移完成。
