# 仁维脚本生成数据层

本目录新增了 Crabot 脚本生成工作流需要的三类业务数据：

- 客户素材库：`clients`、`source_documents`、`client_facts`
- 爆款逻辑库：`logic_patterns`
- 账号逻辑去重库：`account_logic_usage`
- Agent 检索审计：`agent_retrieval_logs`

## 初始化 MySQL

在目标 MySQL 上先创建数据库，再执行迁移：

```sql
CREATE DATABASE renwei_script CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
```

```powershell
mysql --default-character-set=utf8mb4 -h 127.0.0.1 -u <user> -p renwei_script < db/migrations/001_renwei_script_assets.sql
```

迁移脚本只创建表，不导入客户资料，也不会自动写入未确认事实。

## Agent MCP 工具

`runtime/tools/renwei-script-assets-mcp.js` 暴露三个工具：

- `retrieve_client_materials`：按客户和关键词读取客户素材，返回确认状态、权限和来源定位；
- `retrieve_logic_patterns`：按关键词读取爆款逻辑，返回账号来源和证据状态；
- `check_account_logic_duplicates`：按账号和逻辑签名检查同账号历史重复。

工具通过 `runtime/lib/renwei-store.js` 访问 MySQL。连接配置使用环境变量：

```text
RENWEI_DB_HOST
RENWEI_DB_PORT
RENWEI_DB_USER
RENWEI_DB_PASSWORD
RENWEI_DB_NAME
RENWEI_DB_POOL_SIZE
```

默认检索不会写业务表，只写 `agent_retrieval_logs` 审计记录。素材入库和逻辑入库应由已有的人工确认/导入流程调用，避免模型把推测写成事实。

## Agent 调用边界

脚本生成应按以下顺序调用：

```text
客户识别 → retrieve_client_materials → retrieve_logic_patterns
→ 生成候选逻辑签名 → check_account_logic_duplicates → 生成脚本
```

`pending`、`restricted` 或 `inference` 记录必须在 Agent 上下文中保留状态，不能直接写成已确认事实。
