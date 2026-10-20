# -*- coding: utf-8 -*-
"""lib.py — 共享路径与 IO 工具（所有脚本都从这里取路径，避免散落硬编码）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "knowledge"
CLIENTS = KB / "clients"
VIRAL = KB / "viral-logic" / "library.json"
BENCH_GLOBAL = KB / "benchmarks"
TEMPLATES = ROOT / "templates"
OUTPUT = ROOT / "output"
TMP = ROOT / ".tmp"


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def client_dir(client_id):
    return CLIENTS / client_id


def client_profile(client_id):
    return load_json(client_dir(client_id) / "profile.json")


def client_materials(client_id):
    return load_json(client_dir(client_id) / "materials.json", {})


def client_benchmarks(client_id):
    """客户自己的对标拆解 + 全局对标库。"""
    out = []
    d = client_dir(client_id) / "benchmarks"
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            item = load_json(f)
            if item:
                item.setdefault("_source", str(f.relative_to(ROOT)))
                out.append(item)
    if BENCH_GLOBAL.is_dir():
        for f in sorted(BENCH_GLOBAL.glob("*.json")):
            item = load_json(f)
            if item:
                item.setdefault("_source", str(f.relative_to(ROOT)))
                out.append(item)
    return out


def viral_library():
    return load_json(VIRAL, {"version": 0, "cards": []})


def list_clients():
    if not CLIENTS.is_dir():
        return []
    out = []
    for d in sorted(CLIENTS.iterdir()):
        if d.is_dir() and (d / "profile.json").exists():
            p = load_json(d / "profile.json")
            out.append({"client_id": d.name, "name": p.get("name", d.name),
                        "industry": p.get("industry", ""), "city": p.get("city", ""),
                        "updated": p.get("updated", "")})
    return out


# ------------------------------------------------------------------ 文本工具
_PUNCT = "，。！？、；：“”‘’（）《》…—·,.!?;:\"'()<>[]{}- \t\r\n"

NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def norm(text: str) -> str:
    """归一化：去空白与标点，便于比较钩子与首句。"""
    return "".join(ch for ch in str(text or "") if ch not in _PUNCT)


def digits_in(text: str):
    return set(NUM_RE.findall(str(text or "")))


def tokenize(text: str):
    """中英混排的粗切词：中文按 2-gram，英文数字按词。"""
    text = str(text or "")
    tokens = set()
    for seg in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", text):
        if re.match(r"[A-Za-z0-9]", seg):
            tokens.add(seg.lower())
        else:
            for i in range(len(seg) - 1):
                tokens.add(seg[i:i + 2])
    return tokens
