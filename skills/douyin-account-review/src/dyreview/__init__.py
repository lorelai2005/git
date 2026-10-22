"""仁维 Crabot Skill：客户抖音账号后台数据采集 + 自动复盘。

模块职责速查
------------
config         配置加载（YAML/JSON + ${ENV} 展开 + 分场景必填校验）
secrets_box    access_token / refresh_token 落库加密信封
db             PostgreSQL 连接、事务、advisory lock、建表
repo           所有 SQL 访问（唯一允许写 SQL 的地方）
token_manager  access_token 缓存 -> 过期检测 -> refresh 自动刷新 -> 轮换回写（单飞）
douyin_client  抖音开放平台 API 客户端（限流、重试、错误码分类）
metrics        原始字段 -> 归一化指标（别名映射 + 派生率）
collect        定时采集编排：账号 -> token -> 作品/指标 -> 落库
account_resolver  把飞书里的“客户账号id”解析成 dy.account 记录
facts          复盘事实包 + 规则引擎诊断（所有数字的唯一来源）
report         大模型交接契约、报告渲染、数字反幻觉校验
feishu         飞书 tenant_access_token、卡片分片推送
cli            命令行入口（init-db/collect/review/render/push/run/doctor/demo）
"""

__version__ = "1.0.0"
SKILL_NAME = "douyin-account-review"
