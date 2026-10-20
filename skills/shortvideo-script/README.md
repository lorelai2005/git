# 短视频脚本生成 Skill

输入客户资料、爆款逻辑和对标视频，输出一份可以直接开拍的 **拍摄执行版 PDF**。
每条脚本包含：内容结构 → 开头钩子 → 完整口播 → 拍摄执行表（4 列）→ 发布标签。

版式对齐 `assets/reference-sample.pdf`（《淑情哲依开业庆典 5 条短视频脚本》）。

## 30 秒跑通

```bash
cd E:\脚本生成skill

# 看现有客户
python scripts/new_client.py --list

# 直接跑示例（已含完整客户库 + 5 条脚本 payload）
python scripts/build.py ^
  --payload examples/shuqingzheyi-kaiye/payload.json ^
  --client shuqingzheyi ^
  --out "output/淑情哲依-开业庆典-5条短视频脚本.pdf"
```

输出：`output/*.pdf`、同名 `.docx`、以及逐页 PNG（在 `.tmp/preview-*/`，用来检查版式）。

## 日常用法

### 老客户再生成（只要一句目标）

每次都按固定顺序执行：**加载脚本目标 → 检索爆款逻辑并检查账号复用 → 检索客户素材 → 生成脚本**。

脚本目标必须带对应账号的 `account_id`。逻辑检索后调用 `check_account_logic_duplicates` 查询 `account_logic_usage`；命中同账号 `used` 或 `candidate` 记录的逻辑和签名不得直接出片。

```bash
# 1) 加载或确认本次脚本目标：client_id、goal_id、条数、时长、平台、必须提及/规避内容
python scripts/new_client.py --list
# 然后读取 knowledge/clients/shuqingzheyi/plans/<goal_id>.json

# 2) 检索爆款逻辑
python scripts/retrieve.py logic --query "上新 试穿 静奢" --count 5

# 3) 检索客户素材（只看 usable: true 的条目）
python scripts/retrieve.py material --client shuqingzheyi --query "上新 试穿 静奢"

# 4) 写 payload，校验 + 出片
python scripts/build.py --payload my.json --client shuqingzheyi --out "output/xxx.pdf"
```

### 新客户首次入库

```bash
python scripts/new_client.py --id acme --name "ACME" --industry "女装实体店" --city "武汉"
# 补 profile.json（事实库）与 materials.json（能拍什么）
python scripts/new_client.py --id acme --add-plan kaiye --plan-title "开业 5 条"
python scripts/new_client.py --id acme --add-benchmark        # 有对标视频时
```

详细问法与字段含义见 `references/client-onboarding.md`。

## 三道防线（这个 skill 真正值钱的地方）

1. **事实库闸门**：文案里每个数字都必须在 `profile.json` 的 `facts` 里是 `confirmed`，
   否则校验直接拦下 —— 编不出假数字，就编不出假福利。
2. **素材库闸门**：写进「具体视频画面」的东西必须来自 `materials.json` 里 `status=available` 的条目 ——
   拍不出来、拍不了的画面不会进脚本。
3. **结构闸门**：钩子必须与口播首段语义一致、时间轴必须首尾相接、发布标签必须含品牌词。

校验规则全表见 `references/quality-gates.md`。

## 全流程

```
加载脚本目标 plans（这次要什么、对应哪个账号）
        ↓
检索爆款逻辑 viral-logic → 检查 account_logic_usage（这个账号是否用过）
        ↓
检索客户素材 materials / facts（能拍什么、能说什么）
        ↓
生成 payload → 最终签名复查 → 校验 → DOCX → PDF → 逐页复核 → 写回使用记录
```

首次合作在这条流程前增加一次性准备：客户资料入库、事实核对、素材登记和可选的对标拆解。

## 依赖

- Python 3（DSH 自带运行时，含 `python-docx`）
- Microsoft Word（用 COM 把 DOCX 导出 PDF，中文断行与表格分页最稳）

## 目录

完整目录说明见 `SKILL.md`。常改的文件只有四个：

| 文件 | 什么时候改 |
| --- | --- |
| `knowledge/clients/<id>/profile.json` | 客户信息、价格、日期变了 |
| `knowledge/clients/<id>/materials.json` | 新场地、新品、新出镜人 |
| `knowledge/viral-logic/library.json` | 发现新结构，或某张卡失效 |
| `scripts/build_docx.py` | 改版式（改前先读 `references/script-format.md`） |

## 已知边界

- 只做竖向短视频脚本（抖音/视频号/小红书），不做长视频、不做分镜手绘稿。
- 不替你判断内容好坏：结构对、事实对、能拍，是它的底线；爆不爆靠选题和投放。
- Word 不可用时无法导出 PDF（可用 `build_docx.py` 单独出 DOCX 应急）。
