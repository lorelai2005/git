---
name: shortvideo-script
description: 短视频脚本生成流水线（客户库 + 爆款逻辑库 + 账号逻辑去重 + 对标拆解 → 拍摄执行版 PDF）。当用户要为某个客户/账号生成抖音、小红书、视频号等短视频脚本，说「写脚本」「出脚本」「生成拍摄执行版」「拍几条视频」「开业/上新/活动短视频」「按这个格式出脚本」，或需要复刻《…短视频脚本.pdf》那种「内容结构 / 开头钩子 / 完整口播 / 拍摄执行表 / 发布标签」版式并交付 PDF 时使用。首次合作先做客户资料入库与策划案目标确认；每次具体生成都按「加载脚本目标 → 检索爆款逻辑并检查账号复用 → 检索客户素材 → 生成脚本」执行。
version: 1.1.0
---

# 短视频脚本生成 Skill

把「客户资料 + 爆款逻辑 + 对标拆解」变成一份可以直接开拍的 PDF。
交付物固定为 **拍摄执行版 PDF**：每条脚本含 `内容结构 / 开头钩子 / 完整口播 / 拍摄执行表 / 发布标签`，
文末统一列出「发布前核对」清单。版式对齐 `assets/reference-sample.pdf`。

## 每次脚本生成的固定顺序

无论是首次合作完成入库后的写稿，还是老客户再次生成，每一个具体脚本任务都必须按下面的顺序执行。前一步的结果是后一步的输入；脚本目标没有加载完成时，不进入逻辑检索、素材检索或写稿。

1. **加载脚本目标**：确定客户、账号、`account_id`、`goal_id`、任务目标、脚本条数、单条时长、发布平台、必须提及和必须规避的内容。优先读取 `knowledge/clients/<id>/plans/<goal_id>.json`，并从数据层确认 `account_id`；没有账号或目标文件时，先补齐并向用户确认。
2. **检索爆款逻辑并检查账号复用**：根据已加载目标检索匹配逻辑卡，再用 `check_account_logic_duplicates` 查询 `account_logic_usage`。同一账号已有 `used` 或 `candidate` 记录的逻辑签名不得直接复用。
3. **检索客户素材**：根据目标和未被账号复用的逻辑检索客户事实、场景、动作、人物、道具、可拆卖点和可用原话，只使用已确认且可拍的素材；素材不足时标记补拍或改写，不凭空补画面。
4. **生成脚本**：把目标、未重复的逻辑卡和客户素材写入 payload，生成候选签名并再次检查账号历史重复；通过事实、结构、时长、合规和去重校验后生成 DOCX/PDF，并查看逐页 PNG。

每次交付都要能回溯到本次使用的 `goal_id`、逻辑卡 `logic_id`、客户素材来源和待确认信息。首次合作的客户入库、资料核对和对标拆解属于一次性准备工作，准备完成后仍然必须按以上四步执行。

## 两条主线

| 场景 | 用户提供 | 你的工作 |
| --- | --- | --- |
| **首次合作** | 客户资料、账号、策划案/目标、对标视频（可选） | 一次性完成客户入库、账号绑定、目标确认和对标拆解入库；之后按固定四步执行，并检查账号逻辑复用 |
| **后续生成** | 脚本目标和对应账号（对标视频不强制） | 加载目标与 `account_id` → 检索逻辑并过滤账号历史复用 → 检索客户素材 → 生成脚本并复查签名 |

后续生成时若没给对标视频，**不要问用户要**：直接从 `knowledge/viral-logic/library.json`
按目标检索结构卡，并在交付时说明用了哪几张卡。

## 先看一眼现状

```bash
python scripts/new_client.py --list
```

- 客户不在列表里 → 走「首次入库」。
- 客户在列表里、且 `profile.json` 的 facts 基本都是 `confirmed` → 直接走「后续生成」。

---

## 首次入库（只做一次）

### 1. 建目录

```bash
python scripts/new_client.py --id <client_id> --name "<客户名>" --industry "<行业>" --city "<城市>"
```

`client_id` 用拼音小写（例：`shuqingzheyi`），后面所有命令都用它。

### 2. 补客户资料 `knowledge/clients/<id>/profile.json`

必须填满的四块，缺一块就不要往下走：

1. **定位**：`one_liner`、`positioning`、`style_tags`、`differentiators`（差异点要能被镜头证明）
2. **客群**：`audience.core`（年龄+地域+身份）、`jobs`（顾客要完成什么）
3. **事实库 `facts`**：每条一个事实，`status` 只有 `confirmed` 才能写进脚本。
   数字类（数量、价格、折扣、日期、时间、电话）必须逐条确认，这是防虚构的唯一闸门。
4. **待确认 `pending`**：还没问清的信息写在这里，写稿时避开或做字幕占位。

问客户的话术见 `references/client-onboarding.md`，照那份清单问，一轮问完。

### 3. 补素材库 `knowledge/clients/<id>/materials.json`

只登记**已经能拍到**的东西：`scenes / actions / people / props / segments / voice_lines`。
拿不准能不能拍的，`status` 写 `pending`，不要写 `available` —— 写稿时只从 `available` 取素材。

### 4. 确认脚本目标（来自策划案）

```bash
python scripts/new_client.py --id <client_id> --add-plan <goal_id> --plan-title "<任务名>"
```

然后编辑 `knowledge/clients/<id>/plans/<goal_id>.json`：把 `goal.summary`、`count`、`duration`、
`must_mention`、`must_avoid` 填实。**目标没和用户确认过就不要开始写**，
确认话术：把 `summary + count + duration + must_mention + must_avoid` 复述一遍，让用户点头。

### 5. 对标视频拆解入库（有就给，没有跳过）

```bash
python scripts/new_client.py --id <client_id> --add-benchmark
```

按 `references/benchmark-teardown.md` 填 `knowledge/clients/<id>/benchmarks/<id>.json`，
重点是 `hook`、`beats`、`logic_id`、`reusable`、`not_reusable`。
**只搬结构，不搬文案和人设。**

---

## 四步执行细则

### 第 1 步：加载脚本目标

先确认本次任务对应的客户、账号和计划，不要直接根据一句模糊主题开始检索。

```bash
python scripts/new_client.py --list
```

读取或补齐 `knowledge/clients/<client_id>/plans/<goal_id>.json`，至少确认：

- `client_id`：客户 ID
- `account_id`：数据层 `accounts.id`，必须是当前生成账号的真实 ID，不能用客户 ID 或账号名称代替
- `goal.summary`：这批视频要让观众知道、记住或完成什么
- `count`：生成几条
- `duration`：单条时长范围
- `platform`：发布平台
- `must_mention`：每条必须出现的信息
- `must_avoid`：不能出现的表达或内容

如果账号、目标、条数、时长或平台没有和用户确认，先确认并回填计划；**没有明确目标或 `account_id` 时，不进入第 2 步、第 3 步和第 4 步**。

`account_id` 只能来自数据层。账号还没有记录时，先用 `ensure_account` 建号再取 id：

```text
ensure_account(client=<客户编码或名称>, name=<平台昵称>, platform=douyin, account_type=...)
→ 返回 account_id；created=false 表示复用已有账号，不要因此改名字重开一个
```

### 第 2 步：检索爆款逻辑并检查账号复用

```bash
python scripts/retrieve.py logic --query "<目标关键词>" --count 5
```

`--query` 用已加载目标提炼出的关键词，不要用整句话，例如：`开业 同城到店 福利`、`上新 试穿 静奢`。

读返回的 `cards[]`：每张卡的 `beats` 是一条脚本的时间骨架，`hook_formula` 是首句公式，
`shoot_rules` 是拍摄约束，`risks` 是要避开的坑。**选 1 张卡 = 一条脚本**。

逻辑库记录本身不存签名，签名要用平台统一算法现算——**不要自己拼字符串做哈希**，各端口径一旦不一致，历史记录永远匹配不上：

```text
compute_script_signatures(logic_pattern_id=<逻辑卡 id>)
→ signatures.logic_signature
```

对每个候选逻辑调用 `check_account_logic_duplicates`，至少传入：

```text
client=<client_id 或客户编码>
account_id=<第 1 步确认的 accounts.id>
logic_signature=<上面算出的签名>
```

五个签名全都不传会被直接拒绝（`SIGNATURE_REQUIRED`）：签名缺失时数据库只能返回空结果，若当成「没有重复」就是无条件放行。返回里的 `checked_signatures` 说明本次实际比对了哪几个签名。

只把 `duplicate=false` 的候选交给后续写稿。查询命中 `usage_status` 为 `used` 或 `candidate` 的记录时，视为该账号已经使用或预占该逻辑，换下一张卡；`rejected` 和 `archived` 不阻断复用。若所有候选都命中，停止生成并报告「该账号没有未使用的候选逻辑」，不要为了凑数量重复套用。

如果当前运行环境没有数据库/MCP 去重能力，不能把本地 JSON 检索结果当成已完成去重；必须明确标记「账号逻辑复用未核验」，按任务要求停止出片或等待去重能力恢复。

条数不够就在库里加卡（规则见 `references/viral-logic.md`），不要硬套同一张卡写 5 条。

### 第 3 步：检索客户素材

```bash
python scripts/retrieve.py material --client <client_id> --query "<目标关键词>"
```

只看 `"usable": true` 的条目。返回里的 `usable` 已经把「能不能拍」筛过一遍；
`buckets.*.items[].item.tags` 是写「具体视频画面」时的素材来源。

写进脚本的画面必须能在客户素材库中找到对应的场景、动作、人物、道具或卖点。库里没有的内容，要么让客户补拍，要么改写；不能用想象中的门店、人物、产品或活动填空。

### 第 4 步：生成脚本

#### 4.1 写脚本 payload

照 `templates/payload.example.json`（完整示例见 `examples/shuqingzheyi-kaiye/payload.json`）写 JSON。顶层的 `client_id`、`account_id` 和 `goal_id` 必须对应第 1 步加载的客户、账号与计划；每条脚本的 `logic_id` 必须对应第 2 步通过账号去重的逻辑卡。

生成 payload 后，在出片前再对每条脚本检查候选签名。优先沿用数据层已有的签名生成方式，提交 `logic_signature`，并在可用时一并提交 `hook_signature`、`structure_signature`、`speech_signature`、`selling_point_signature` 调用 `check_account_logic_duplicates`。任一签名命中同一账号的 `used` 或 `candidate` 记录，都必须改写或换逻辑后重新检查，不能靠改标题或换几个词绕过。

写的时候守住五条硬规则：

1. **开头钩子 = 完整口播首段的压缩版**（口播首段必须能原样念出来，钩子做字幕版；两者语义必须一致）
2. **时间轴从 0 秒起、首尾相接**：`0—3秒 / 3—15秒 / 15—35秒 …`，最后一段结束时间就是总时长
3. **文本里的每个数字都要能在 `facts` 里找到**，找不到就改写或先去确认
4. **每条 `shoot` 四列都要写实**：`执行要点` 必须写出「什么必须做、什么禁止做」，不写「注意细节」这种空话
5. **发布标签**以 `#` 开头、不少于 3 个、必须含品牌词

口播按 **5 字/秒** 估算时长；60 秒的片子大约 300 字，超太多要删。

#### 4.2 校验并出 PDF

```bash
python scripts/build.py \
  --payload examples/shuqingzheyi-kaiye/payload.json \
  --client <client_id> \
  --out "output/<客户名>-<任务名>.pdf"
```

流程是：**事实/结构校验 → 账号逻辑去重复查 → DOCX → Word 导出 PDF → 逐页 PNG**。
去重检查不通过或数据库/MCP 不可用时，不得继续出片。校验不通过会直接拦住出片，按 `[错误]` 修 payload 再跑。

成功出片后必须写回去重表，**这是闭环的最后一环**，不写等于下次查不到任何重复：

```text
# 1) 先算签名（logic_signature 可省略——给了 logic_pattern_id 由服务端按库中结构摘要现算，口径最稳）
compute_script_signatures(
  logic_pattern_id=<本次用的逻辑卡 id>,
  hook=<钩子>, structure=<结构串>, voiceover=[...], selling_points=[...])

# 2) 逐条写回
record_account_logic_usage(
  client=<客户编码>, account_id=<accounts.id>, logic_pattern_id=<逻辑卡 id>,
  hook_signature=..., structure_signature=..., speech_signature=..., selling_point_signature=...,
  content_title=<这批稿子的标识>, usage_status=used,
  source_locator={goal_id:..., payload:..., pdf:...})
```

- 幂等键是 `(client_id, account_id, logic_signature)`：重复写只更新并保留首次 id，可以放心重跑。
- `usage_status` 只升不降：已标 `used` 的记录不会被 `candidate` 覆盖回去。
- 只预占不交付时写 `usage_status=candidate`。
- 调用失败或无写入权限时，必须在交付里写明「本次使用记录未写回，后续账号去重会失效」，不能宣称闭环已完成。

`[提示]` 不阻断，但要判断：能改就改，确实是刻意为之就在交付说明里写一句。

出片后**一定要看逐页 PNG**（打印在输出的 `逐页图：` 一行里）：
标题是否单行、表格有没有被拦腰截断、口播有没有把某个段落挤到下一页孤零零一行。
有问题就调 payload 的文字长度重出，不要直接交付没看过的 PDF。

## 硬约束清单（出片前逐条对）

- [ ] 已加载本次任务对应的 `client_id`、`account_id`、`goal_id` 和计划内容
- [ ] 目标、账号、条数、时长和客户确认过
- [ ] 已按「加载脚本目标 → 检索爆款逻辑并检查账号复用 → 检索客户素材 → 生成脚本」顺序执行
- [ ] 每个逻辑候选都检查过 `account_logic_usage`
- [ ] 每条脚本都通过最终签名复查，未命中同账号 `used` / `candidate` 记录
- [ ] 出片后已调用 `record_account_logic_usage` 写回（`used`），或明确声明「未写回、去重失效」
- [ ] 每条脚本都对应一张爆款逻辑卡，5 条不重复用同一张
- [ ] 文案里的数字全部来自 `facts.status == confirmed`
- [ ] `pending` 里的信息没有被写成确定语气
- [ ] 口播里没有「最 / 第一 / 唯一 / 保证」等绝对化用语（校验脚本会自动拦）
- [ ] 顾客、员工出镜的肖像许可在 `materials.people[].consent` 里是 `confirmed`
- [ ] 逐页 PNG 看过一遍
- [ ] 交付时说明了用了哪几张逻辑卡、哪些信息还待客户确认

## 目录

```
SKILL.md                      本文件
references/
  script-format.md            版式规范：字号/颜色/列宽/间距（复刻参考稿的依据）
  payload-schema.md           payload JSON 字段说明
  client-onboarding.md        首次入库：问客户的问题清单 + 事实核对
  benchmark-teardown.md       对标视频拆解字段与判读要点
  viral-logic.md              爆款逻辑库字段、新增规则、检索机制
  quality-gates.md            校验规则与人工复核清单
scripts/
  new_client.py               客户/策划案/对标文件脚手架
  retrieve.py                 爆款逻辑 + 客户素材检索（可解释打分；账号去重走 MCP）
  validate.py                 生成前质量闸门
  build.py                    校验 → DOCX → PDF → PNG 一条命令
  build_docx.py               版式引擎（python-docx，无外部依赖）
  export_pdf.ps1              DOCX → PDF（Word COM）
  render_preview.ps1          PDF → 逐页 PNG（供自查）
  lib.py                      路径与文本工具
knowledge/
  viral-logic/library.json    爆款逻辑库（跨客户复用）
  clients/<id>/profile.json   客户事实库（唯一事实源）
  clients/<id>/materials.json 客户素材库（能拍什么）
  clients/<id>/plans/*.json   每次任务的脚本目标（来自策划案，含 account_id）
  clients/<id>/benchmarks/*.json 该客户的对标拆解
  benchmarks/*.json           全局对标库（跨客户复用）
templates/                    入库与 payload 空白模板
examples/shuqingzheyi-kaiye/  用参考稿内容做成的完整示例，可直接跑通
output/                       交付物
```

## 依赖

- Python 3 + `python-docx`（已在 DSH 自带运行时里）
- Microsoft Word（COM 导出 PDF，高保真，字体和表格与参考稿一致）
- 版式引擎只用 python-docx，不引 PDF 库；PDF 由 Word 生成，中文断行和表格分页最稳

## 明确不做

- 不编造数据：没有 `confirmed` 事实就不写数字
- 不搬对标视频的文案、人设、口头禅
- 不在脚本里替客户承诺效果（「穿上显瘦十斤」「保证成交」）
- 不在校验未通过时硬出片（`--force` 只用于排错，不用于交付）
