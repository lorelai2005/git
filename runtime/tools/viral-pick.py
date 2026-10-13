#!/usr/bin/env python3
"""Pick top viral posts from an account-posts evidence dir.

Reads the resumable state written by account-posts.py, ranks collected
posts with a weighted engagement score, marks the viral line relative to
the account median, and tracks pick-history convergence so the caller can
start analysing the top picks before full-history collection finishes.

Read-only: never mutates account-posts state; keeps its own sidecar
viral-pick-history.json next to state.json.
"""
import argparse, datetime as dt, json, math, os, re, statistics, tempfile, time
from pathlib import Path
ROOT = Path(os.environ.get('ACCOUNT_POSTS_ROOT', '/data/benchmarks/_work'))
VERSION = 'viral-pick-1.0'
MAX_STATE = 16*1024*1024
HISTORY_KEEP = 20

WEIGHTS = {'digg_count': 1, 'comment_count': 1, 'share_count': 3, 'collect_count': 2}

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def inside(root, p):
    p = p.resolve(strict=True); root = root.resolve(strict=True)
    if p == root or root not in p.parents: raise ValueError('artifact_path_escape')
    return p
def read_json(p, max_bytes=MAX_STATE):
    p = p.resolve()
    if not p.is_file() or p.stat().st_size > max_bytes: raise ValueError('invalid_or_oversize_artifact')
    return json.loads(p.read_text(encoding='utf-8'))
def atomic(p, data):
    fd, tmp = tempfile.mkstemp(prefix='.viral-', dir=p.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def setup(value, root=ROOT):
    directory = inside(root, Path(value))
    out = directory / 'account-posts-v1'
    if not out.is_dir(): raise ValueError('account_posts_not_collected')
    state = read_json(out / 'state.json')
    if not isinstance(state.get('items'), dict) or not isinstance(state.get('profile'), dict):
        raise ValueError('account_state_mismatch')
    return directory, out, state

def score_of(stats):
    stats = stats or {}
    total, missing = 0, 0
    for k, w in WEIGHTS.items():
        v = stats.get(k)
        if type(v) is int and v >= 0: total += v * w
        else: missing += 1
    return total, missing

def pick(state, min_pick=5, max_pick=8, ratio=3.0, history_path=None):
    if type(min_pick) is not int or not 1 <= min_pick <= max_pick <= 30: raise ValueError('invalid_pick_range')
    if not (isinstance(ratio, (int, float)) and math.isfinite(ratio) and 1.0 <= ratio <= 100): raise ValueError('invalid_ratio')
    items = sorted(state['items'].values(),
                   key=lambda x: (x.get('created_at') is None, x.get('created_at') or '', x.get('content_id') or ''))
    if not items: raise ValueError('no_posts_collected')
    scored = []
    for it in items:
        s, missing = score_of(it.get('stats'))
        scored.append({'content_id': it['content_id'], 'web_url': it.get('web_url'),
                       'title': (it.get('title') or it.get('description') or '')[:120],
                       'created_at': it.get('created_at'), 'published_at_china': it.get('published_at_china'),
                       'score': s, 'missing_metric_kinds': missing, 'source_page': it.get('source_page'),
                       'stats': it.get('stats') or {}})
    scores = [x['score'] for x in scored]
    median = statistics.median(scores)
    threshold = median * ratio
    ranked = sorted(scored, key=lambda x: (-x['score'], x['created_at'] or ''))
    above = [x for x in ranked if median > 0 and x['score'] >= threshold]
    picks = above[:max_pick]
    if len(picks) < min_pick:
        picks = ranked[:min_pick]
    for x in picks: x['below_viral_line'] = bool(median > 0 and x['score'] < threshold)
    for x in picks: x['score_vs_median'] = round(x['score'] / median, 2) if median > 0 else None

    # Convergence: the top set stops changing while collection keeps deepening.
    exhausted = bool(state.get('pagination_exhausted'))
    history = []
    if history_path is not None and history_path.exists():
        try: history = read_json(history_path, 1024*1024).get('entries', [])
        except ValueError: history = []
    top_ids = [x['content_id'] for x in picks]
    pages = int(state.get('pages_fetched') or 0)
    entry = {'at': now(), 'pages_fetched': pages, 'unique_posts': len(items), 'top_ids': top_ids, 'exhausted': exhausted}
    prev = [h for h in history if h.get('pages_fetched') != pages][-2:]
    stable = len(prev) >= 2 and all(h.get('top_ids') == top_ids for h in prev) and all(
        (h.get('pages_fetched') or 0) < pages for h in prev)
    converged = exhausted or stable
    if history_path is not None:
        history.append(entry)
        atomic(history_path, {'version': VERSION, 'entries': history[-HISTORY_KEEP:]})

    count = (state.get('profile', {}).get('stats') or {}).get('content_count')
    return {
        'ok': True, 'resource_type': 'viral_pick', 'version': VERSION, 'retrieved_at': now(),
        'profile': {k: state['profile'].get(k) for k in ['nickname', 'unique_id', 'sec_uid']},
        'unique_posts': len(items), 'pages_fetched': pages,
        'pagination_exhausted': exhausted, 'converged': converged,
        'convergence_basis': 'provider_end' if exhausted else ('top_set_stable_across_rounds' if stable else 'still_changing'),
        'viral_line': {'score_weights': WEIGHTS, 'median_score': median, 'ratio': ratio, 'threshold': threshold},
        'coverage': {'collected': len(items),
                     'profile_reported_count': count if type(count) is int else None,
                     'coverage_ratio': round(len(items) / count, 4) if type(count) is int and count > 0 else None},
        'pick_count': len(picks), 'min_pick': min_pick, 'max_pick': max_pick, 'picks': picks,
        'limitations': [
            '爆款判定基于已采集范围内的互动数快照，排序依据为 digg + comment + 3*share + 2*collect；未采集到的历史作品不在候选内。',
            'pagination_exhausted=false 时，结论为“当前覆盖范围内的爆款”；覆盖率见 coverage 字段，不得表述为账号历史全量爆款。',
            '互动数中的 null 按 0 计入得分并计入 missing_metric_kinds，可能影响低互动作品的相对排序。',
            'converged=true 仅表示 top 集合在连续采集轮次间稳定，不代表账号已无更多历史作品。'],
        'instruction': 'converged=true 且 picks 数量满足需求时，即可对每个 pick 的 web_url 依次调用 video_prepare_assets 进入时间轴分析；同时可让采集继续补全历史。分页状态仍以 account-posts state 为准，本工具不修改它。'}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('evidence_dir')
    p.add_argument('--min-pick', type=int, default=5)
    p.add_argument('--max-pick', type=int, default=8)
    p.add_argument('--ratio', type=float, default=3.0)
    a = p.parse_args()
    try:
        directory, out, state = setup(a.evidence_dir)
        result = pick(state, a.min_pick, a.max_pick, a.ratio, out / 'viral-pick-history.json')
        result['evidence_dir'] = str(directory.resolve())
        result['files'] = {'state': str(out / 'state.json'), 'history': str(out / 'viral-pick-history.json')}
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, KeyError, OSError) as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False)); raise SystemExit(1)
if __name__ == '__main__': main()
