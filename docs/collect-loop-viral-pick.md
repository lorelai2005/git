# 爆款优先的断点续采（collect-loop + viral-pick）

针对抖音上游风控只允许每次采集一部分作品的问题，本方案把"一次性抓全"改为**可断点的渐进式采集 + 滚动更新的爆款榜**：

- `collect-loop.sh`：驱动 `account-posts.py` 对同一 evidence_dir 反复续采，撞风控后按 provider `retry_after`（不低于 30 分钟退避下限）加随机抖动冷却，跨小时自动恢复，直到分页耗尽或预算耗尽。
- `viral-pick.py`：每轮采集落盘后重算爆款榜（加权分 = digg + comment + 3×share + 2×collect，爆款线 = 账号中位数 × ratio，默认 3 倍），输出 top 5-8 与覆盖率；当 top 集合在连续轮次间稳定（或已采到末页）时报告 `converged=true`，此时即可对入选视频启动时间轴分析，采集继续在后台补全历史。

## 文件

- `runtime/tools/collect-loop.sh` — 采集驱动（bash，flock 并发锁、预算/轮次上限、冷却抖动）
- `runtime/tools/viral-pick.py` — 爆款筛选（只读 account-posts state，自身历史存 `viral-pick-history.json`）

## 典型用法

```bash
# 1. 先由常规流程拿到 account_profile（video_prepare_assets 传账号主页 URL）
# 2. 启动续采循环（evidence_dir 即 profile 所在目录）
ACCOUNT_POSTS_ROOT=/data/benchmarks/_work \
bash /data/tools/collect-loop.sh /data/benchmarks/_work/<evidence_dir> \
  --max-rounds 8 --budget-seconds 7200 \
  --min-pick 5 --max-pick 8 --ratio 3.0 \
  --python /data/agent-venv/bin/python \
  --collector /data/tools/account-posts.py \
  --picker /data/tools/viral-pick.py \
  --on-converged "bash /data/tools/start-viral-analysis.sh"

# 3. 随时查看当前爆款榜（采集进行中或结束后均可）
python3 /data/tools/viral-pick.py /data/benchmarks/_work/<evidence_dir> --min-pick 5 --max-pick 8
```

`--on-converged` 钩子在榜单首次收敛时触发一次，接收 evidence_dir 和 picks JSON 文件路径，用于自动对入选视频逐个调用 `video_prepare_assets`。

## 自动续跑（生产部署）

需要跨小时无人值守续采时，用 systemd 托管（比 nohup 可靠，崩溃自动拉起）：

```ini
# /etc/systemd/system/renwei-collect@.service
[Unit]
Description=Renwei douyin account collect-loop (%i)
After=network.target

[Service]
Type=simple
Environment=ACCOUNT_POSTS_ROOT=/data/benchmarks/_work
Environment=RETREAT_FLOOR_SECONDS=1800
ExecStart=/data/tools/collect-loop.sh /data/benchmarks/_work/%i \
  --max-rounds 48 --budget-seconds 86400 \
  --min-pick 5 --max-pick 8 \
  --python /data/agent-venv/bin/python \
  --collector /data/tools/account-posts.py \
  --picker /data/tools/viral-pick.py
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now renwei-collect@<evidence_dir 名>
journalctl -u renwei-collect@<evidence_dir 名> -f   # 观察进度
```

注意：provider 的环境变量（`DOUYIN_API_V5_BASE`、`DOUYIN_API_V5_KEY_FILE`）需在 service 的 Environment/EnvironmentFile 中按服务器现有配置补齐，与 `account-posts.py` 的要求一致。循环内的 flock 锁保证重复启动不会并发采集同一账号。

## 行为要点与边界

- 冷却策略：本轮 `stop_reason=page_budget` 仅短暂暂停（5-15 秒抖动）；风控/限流按 `retry_after_seconds` 等待，且不低于 `RETREAT_FLOOR_SECONDS`（默认 1800 秒，即截图中的 30 分钟退避规则）。
- 每轮结束都会运行 viral-pick 并写日志（picks 数、converged、覆盖率），冷却期正好用于分析已入选视频。
- 并发锁：同一 evidence_dir 同时只允许一个 collect-loop，第二个实例退出码 75。
- 证据口径：`pagination_exhausted=false` 时只能说"当前覆盖范围内的爆款"，覆盖率见 viral-pick 输出的 `coverage` 字段；账号未公开/已删除作品不可据此还原（与 account-posts 的 limitations 一致）。
- viral-pick 不修改 account-posts 的 state，可随时安全重跑。

## 冒烟测试记录（2026-10-04）

在 156 服务器用假采集器验证：3 轮后以 `provider_end` 正常结束；on-converged 钩子恰好触发一次；末轮 picks 中 3 条爆款 `below_viral_line=false`、2 条保底日常作品为 true；并发第二个实例退出码 75；history 正确记录各轮 top_ids。
