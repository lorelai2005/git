# -*- coding: utf-8 -*-
"""build.py — 一条命令产出交付物：校验 → DOCX → PDF →（可选）逐页 PNG 自查。

用法:
  python build.py --payload examples/shuqingzheyi-kaiye/payload.json \
                  --out output/淑情哲依-开业庆典-5条短视频脚本.pdf

参数:
  --client      客户 id（默认取 payload.client_id），用于事实/合规校验
  --docx        DOCX 输出路径（默认与 PDF 同名 .docx）
  --force       校验不通过也继续出片（仅供排错，交付前不要用）
  --no-preview  不渲染 PNG
  --json        机器可读的汇总输出
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import build_docx  # noqa: E402
import validate as validate_mod  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent


def powershell_exe():
    for cand in ("powershell.exe", "pwsh.exe", "pwsh"):
        p = shutil.which(cand)
        if p:
            return p
    win = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if win.exists():
        return str(win)
    raise RuntimeError("找不到 PowerShell，无法调用 Word 导出 PDF")


def run_ps(script: Path, **kwargs):
    exe = powershell_exe()
    cmd = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    for k, v in kwargs.items():
        cmd += [f"-{k}", str(v)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc


def export_pdf(docx_path: Path, pdf_path: Path):
    proc = run_ps(SCRIPTS / "export_pdf.ps1", InFile=docx_path, OutFile=pdf_path)
    if proc.returncode != 0 or not pdf_path.exists():
        raise RuntimeError("Word 导出 PDF 失败：\n"
                           + (proc.stdout or "") + (proc.stderr or ""))
    return pdf_path


def make_preview(pdf_path: Path, out_dir: Path, width=1500):
    proc = run_ps(SCRIPTS / "render_preview.ps1", Pdf=pdf_path,
                  OutDir=out_dir, Width=width)
    pages = sorted(out_dir.glob("page-*.png")) if out_dir.is_dir() else []
    return pages, proc


def main():
    ap = argparse.ArgumentParser(description="短视频脚本 → DOCX/PDF 交付流水线")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--out", required=True, help="PDF 输出路径")
    ap.add_argument("--client", default=None)
    ap.add_argument("--docx", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    payload_path = Path(args.payload).resolve()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    cid = args.client or payload.get("client_id")
    profile = lib.client_profile(cid) if cid else None
    if cid and not profile:
        print(json.dumps({"error": f"客户不存在: {cid}", "clients": lib.list_clients()},
                         ensure_ascii=False, indent=2))
        return 3

    res = validate_mod.check(payload, profile)
    if not args.json:
        print(validate_mod.report_text(res))
    if not res["ok"] and not args.force:
        if args.json:
            print(json.dumps({"ok": False, "validation": res}, ensure_ascii=False, indent=2))
        print("\n校验未通过，已停止出片。按上面的 [错误] 修 payload 后重跑；"
              "确需先看版式可加 --force。", file=sys.stderr)
        return 1

    pdf_path = Path(args.out).resolve()
    docx_path = Path(args.docx).resolve() if args.docx else pdf_path.with_suffix(".docx")
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    build_docx.render(payload, docx_path)
    export_pdf(docx_path, pdf_path)

    pages = []
    if not args.no_preview:
        preview_dir = lib.TMP / ("preview-" + pdf_path.stem)
        if preview_dir.is_dir():
            shutil.rmtree(preview_dir, ignore_errors=True)
        pages, _ = make_preview(pdf_path, preview_dir)

    summary = {
        "ok": True,
        "pdf": str(pdf_path),
        "pdf_bytes": pdf_path.stat().st_size,
        "docx": str(docx_path),
        "pages": len(pages),
        "preview_dir": str(pages[0].parent) if pages else None,
        "validation": {"errors": len(res["errors"]), "warnings": len(res["warnings"]),
                       "notes": len(res["notes"])},
        "stats": res["stats"],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"\n已生成 PDF：{pdf_path}")
        print(f"  页数 {summary['pages']} / {summary['pdf_bytes'] // 1024} KB")
        print(f"  DOCX：{docx_path}")
        if summary["preview_dir"]:
            print(f"  逐页图：{summary['preview_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
