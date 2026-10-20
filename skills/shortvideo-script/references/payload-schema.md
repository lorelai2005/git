# payload 结构说明

`payload.json` 是唯一的内容源：**先写 JSON，再渲染 PDF**。
这样内容和版式解耦，改字号不用重写文案，改文案不会破坏版式。

完整示例：`examples/shuqingzheyi-kaiye/payload.json`
空白骨架：`templates/payload.example.json`

## 顶层

```json
{
  "client_id": "shuqingzheyi",
  "account_id": "<accounts.id>",
  "goal_id": "kaiye-5tiao",
  "meta": { "…": "…" },
  "scripts": [ "…" ],
  "final_check": [ "…" ],
  "final_check_title": "五条脚本统一发布前核对",
  "footer_note": "可选，文末一句备注"
}
```

`client_id` 决定用哪份客户事实库做校验，必须与 `knowledge/clients/<id>/` 同名。
`account_id` 是数据层 `accounts.id`，用于查询 `account_logic_usage`，不能用客户 ID 或账号名称代替。
`goal_id` 对应 `plans/<goal_id>.json`，便于回溯这份稿子是按哪个目标写的。

## meta（页首）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `title` | 是 | 主标题，`客户名 | 任务名 N 条短视频拍摄执行版` |
| `platform` | 是 | 适用平台，如「抖音短视频、门店宣传」 |
| `audience` | 是 | 目标受众，写到「地域 + 年龄 + 身份」 |
| `common_goal` | 是 | 本次共同目标，一句话说清让人知道什么、记住什么、做什么 |
| `unified_facts` | 是 | 全篇共用的事实口径、福利限制和免责说明 |

`unified_facts` 是全篇统一口径的位置，例如「以门店最终确认信息为准」。

## scripts[]（每条脚本）

```json
{
  "no": "01",
  "title": "同城开店通知：淑情哲依开店了",
  "logic_id": "LOCAL-LANDMARK-OPENING",
  "structure": "点名同城人群 → 明确状态 → 报具体位置 → 展示卖点 → 到店引导",
  "hook": "开头钩子，必须是首段口播的压缩版",
  "voiceover": ["首段", "中段", "末段"],
  "shoot": [
    {"time": "0—3秒", "picture": "拍什么 + 怎么拍", "audio": "实际说出口的话", "note": "必须做什么；禁止做什么"}
  ],
  "tags": "#品牌 #品类 #地标"
}
```

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `no` | 是 | `01`–`99`，两位数补零 |
| `title` | 是 | `场景类型：一句人话` |
| `logic_id` | 是 | 对应已通过账号复用检查的逻辑卡 ID |
| `structure` | 是 | 用 `→` 串起 4—6 步，每步不超过 8 字 |
| `hook` | 是 | `voiceover[0]` 的压缩版，语义必须一致 |
| `voiceover` | 是 | 字符串数组，每段一个意思，一段念完能换气 |
| `shoot` | 是 | 4—6 行，每行四列齐全 |
| `tags` | 是 | `#` 开头，至少 3 个，必须含品牌词 |

### voiceover 段落

3—5 段最稳：

1. 首段 = 钩子落地，点名人群和结果
2. 中段 = 主体信息，一段只讲一件事
3. 末段 = 到店引导和免责

按 **5 字/秒** 估算时长。超过 80 字的单段通常需要拆分。

### shoot 行

| 列 | 写法 |
| --- | --- |
| `time` | `0—3秒` 这种区间，第一行从 0 开始，前一行结束等于下一行开始 |
| `picture` | 拍什么、机位、景别和动作写清 |
| `audio` | 该段实际说出口的话，可以是半句 |
| `note` | 写清必须做什么和禁止做什么 |

## 账号逻辑去重

生成前后都要使用 `check_account_logic_duplicates` 检查同一 `client_id + account_id` 的历史记录：

- `logic_signature`：逻辑卡签名，优先使用逻辑库已有签名
- `hook_signature`：开头钩子签名
- `structure_signature`：结构签名
- `speech_signature`：口播签名
- `selling_point_signature`：卖点组合签名

命中 `usage_status` 为 `used` 或 `candidate` 的记录时，不能出片；`rejected` 和 `archived` 不阻断复用。成功出片后，要把本次使用记录写回 `account_logic_usage`。

## final_check

发布前人工核对清单，写这一单特有的核对项，不要只写通用的错别字检查。例如：

- 补齐并核对正式开业日期、营业时间和联系方式。
- 不得把有限数量的福利表达成人人可领取。
- 顾客、员工和路人出镜前取得肖像使用许可。

## 写 payload 的常见错误

| 症状 | 原因 | 改法 |
| --- | --- | --- |
| `client_id`、`account_id` 或 `goal_id` 不一致 | payload 与本次任务上下文脱节 | 以第 1 步加载的目标和账号为准 |
| 逻辑卡已经被同账号使用 | 没查 `account_logic_usage` | 换未使用逻辑，或停止并报告候选不足 |
| 钩子和首段口播不是一回事 | 钩子另写了一句宣传语 | 从首段口播压缩钩子 |
| 时间轴断档或重叠 | 只写画面没有连续时间 | 第一段从 0 开始，上一段结束等于下一段开始 |
| 文案出现事实库没有的数字 | 用经验补了价格、日期或数量 | 改写，或先确认事实 |
| 画面素材库中不存在 | 凭想象写门店、人物或活动 | 改成可拍素材，或列入补拍 |
