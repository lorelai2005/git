# -*- coding: utf-8 -*-
"""build_docx.py — 把脚本 payload(JSON) 渲染成与参考样例一致的 A4 拍摄执行版 DOCX。

版式参照《淑情哲依开业庆典5条短视频脚本.pdf》：
  · A4，左右页边距 2.8cm，正文宽约 151mm
  · 主标题 24pt 加粗 #0B5394，下方 1.5pt 同色通栏横线
  · 正文 10.5pt / #333333 / 固定行距 17pt
  · 栏目小标题 13.5pt 加粗 #C65911，左侧 3pt 橙色竖条
  · 拍摄执行表：表头 #C65911 底白字，表体 0.5pt 灰线，首列窄

只用 python-docx，不依赖外部库。中文用「微软雅黑」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Emu, Length, Pt, RGBColor

# ---------------------------------------------------------------- 设计常量
NAVY = RGBColor(0x0B, 0x53, 0x94)      # 主标题蓝
ORANGE = RGBColor(0xC6, 0x59, 0x11)    # 栏目橙
BODY = RGBColor(0x33, 0x33, 0x33)      # 正文灰黑
NAVY_HEX = "0B5394"
ORANGE_HEX = "C65911"
LINE_HEX = "BFBFBF"

FONT_LATIN = "Microsoft YaHei"
FONT_CJK = "微软雅黑"

BODY_SIZE = Pt(10)
LINE_EXACT = Pt(17)
H2_SIZE = Pt(12.5)
TITLE_SIZE = Pt(21)
SCRIPT_TITLE_SIZE = Pt(10.5)

PAGE_W, PAGE_H = Cm(21), Cm(29.7)
MARGIN_LR = Cm(3.17)
MARGIN_TOP = Cm(2.6)
MARGIN_BOTTOM = Cm(2.2)
CONTENT_W = Emu(int(PAGE_W) - 2 * int(MARGIN_LR))   # 14.66cm，与样例一致

# 表格列宽比例（实测样例：时间 / 具体视频画面 / 口播·同期声 / 执行要点）
COL_RATIO = (0.0835, 0.2934, 0.3124, 0.3106)
PARA_AFTER = Pt(4.5)


# ---------------------------------------------------------------- 底层工具
def set_run_font(run, size=None, bold=None, color=None):
    """同时设置西文与中文字体，避免中文回退成宋体。"""
    if size is not None:
        run.font.size = size
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    run.font.name = FONT_LATIN
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), FONT_LATIN)
    rfonts.set(qn("w:hAnsi"), FONT_LATIN)
    rfonts.set(qn("w:eastAsia"), FONT_CJK)
    rfonts.set(qn("w:cs"), FONT_LATIN)
    return run


def _len(v):
    """把数字当成 pt 解析；已经是 Length 的原样返回（避免二次包裹）。"""
    if v is None:
        return None
    if isinstance(v, Length):
        return v
    return Pt(v)


def add_para(container, text="", size=BODY_SIZE, bold=False, color=BODY,
             align=None, space_before=0, space_after=PARA_AFTER,
             line=LINE_EXACT, first_line_indent=None, left_indent=None,
             keep_with_next=False):
    p = container.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = _len(space_before)
    pf.space_after = _len(space_after)
    if line is not None:
        pf.line_spacing = _len(line)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    if align is not None:
        p.alignment = align
    if first_line_indent is not None:
        pf.first_line_indent = first_line_indent
    if left_indent is not None:
        pf.left_indent = left_indent
    pf.keep_with_next = keep_with_next
    if text:
        set_run_font(p.add_run(text), size=size, bold=bold, color=color)
    return p


def _ensure_pbdr(p):
    pPr = p._p.get_or_add_pPr()
    pbdr = pPr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        pPr.append(pbdr)
    return pbdr


def add_para_border(p, edge="bottom", sz=12, color=NAVY_HEX, space=4, val="single"):
    """给段落画边框。sz 单位 1/8 pt；space 为边框与文字的间距(pt)。"""
    pbdr = _ensure_pbdr(p)
    el = OxmlElement(f"w:{edge}")
    el.set(qn("w:val"), val)
    el.set(qn("w:sz"), str(sz))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color)
    pbdr.append(el)


def shade_cell(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def set_table_borders(table, color=LINE_HEX, sz=4):
    """整表统一细边框。sz 单位 1/8 pt（4 = 0.5pt）。"""
    tbl = table._tbl
    tblPr = tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    existing = tblPr.find(qn("w:tblBorders"))
    if existing is not None:
        tblPr.remove(existing)
    tblPr.append(borders)


def set_cell_borders(cell, color, sz=4, edges=("top", "left", "bottom", "right")):
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in edges:
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    existing = tcPr.find(qn("w:tcBorders"))
    if existing is not None:
        tcPr.remove(existing)
    tcPr.append(borders)


def set_cell_margins(table, top=60, bottom=60, left=90, right=90):
    """单元格内边距，单位 twips（60 = 3pt）。"""
    tblPr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for edge, val in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    existing = tblPr.find(qn("w:tblCellMar"))
    if existing is not None:
        tblPr.remove(existing)
    tblPr.append(mar)


def set_col_widths(table, widths):
    table.autofit = False
    tblPr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        for gc, w in zip(grid.findall(qn("w:gridCol")), widths):
            gc.set(qn("w:w"), str(int(w.twips)))
    for row in table.rows:
        for cell, w in zip(row.cells, widths):
            cell.width = w


def row_cant_split(row):
    trPr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:cantSplit")
    trPr.append(el)


def cell_text(cell, text, size=Pt(10), bold=False, color=BODY, align=None,
              line=Pt(15), valign=WD_ALIGN_VERTICAL.TOP):
    cell.vertical_alignment = valign
    p = cell.paragraphs[0]
    p.text = ""
    pf = p.paragraph_format
    pf.space_before = Pt(1)
    pf.space_after = Pt(1)
    pf.line_spacing = line
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    if align is not None:
        p.alignment = align
    lines = str(text).split("\n")
    for i, ln in enumerate(lines):
        if i:
            p = cell.add_paragraph()
            pf = p.paragraph_format
            pf.space_before = Pt(1)
            pf.space_after = Pt(1)
            pf.line_spacing = line
            pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            if align is not None:
                p.alignment = align
        set_run_font(p.add_run(ln), size=size, bold=bold, color=color)


# ---------------------------------------------------------------- 文档骨架
def new_document():
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = FONT_LATIN
    st.font.size = BODY_SIZE
    st.font.color.rgb = BODY
    rpr = st.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), FONT_LATIN)
    rfonts.set(qn("w:hAnsi"), FONT_LATIN)
    rfonts.set(qn("w:eastAsia"), FONT_CJK)
    rfonts.set(qn("w:cs"), FONT_LATIN)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = PAGE_W, PAGE_H
    sec.left_margin = sec.right_margin = MARGIN_LR
    sec.top_margin = MARGIN_TOP
    sec.bottom_margin = MARGIN_BOTTOM
    sec.header_distance = Cm(1.2)
    sec.footer_distance = Cm(1.2)
    return doc


def add_title(doc, text):
    p = add_para(doc, text, size=TITLE_SIZE, bold=True, color=NAVY,
                 align=WD_ALIGN_PARAGRAPH.CENTER,
                 space_before=0, space_after=Pt(8.5), line=Pt(30))
    add_para_border(p, "bottom", sz=10, color=NAVY_HEX, space=12)
    return p


def add_kv(doc, label, value):
    """「适用平台：…」这类标签行，标签与值同字重（与样例一致）。"""
    p = add_para(doc, "", space_before=0, space_after=PARA_AFTER)
    set_run_font(p.add_run(f"{label}："), size=BODY_SIZE, color=BODY)
    set_run_font(p.add_run(value), size=BODY_SIZE, color=BODY)
    return p


def add_h2(doc, text, space_before=4):
    """栏目小标题：橙色加粗 + 左侧竖条（竖条按样例探入左侧页边距）。"""
    p = add_para(doc, text, size=H2_SIZE, bold=True, color=ORANGE,
                 space_before=Pt(space_before), space_after=Pt(2), line=Pt(32),
                 keep_with_next=True)
    add_para_border(p, "left", sz=24, color=ORANGE_HEX, space=10)
    return p


def add_body(doc, text, space_after=Pt(4), space_before=0, mono_indent=None):
    return add_para(doc, text, size=BODY_SIZE, color=BODY,
                    space_before=space_before, space_after=space_after,
                    left_indent=mono_indent)


def add_numbered(doc, items):
    """统一下发前核对清单：手工编号 + 悬挂缩进，跨页也稳定。"""
    for i, item in enumerate(items, 1):
        p = add_para(doc, "", space_before=0, space_after=Pt(5),
                     left_indent=Pt(20), first_line_indent=Pt(-20))
        set_run_font(p.add_run(f"{i}. "), size=BODY_SIZE, color=BODY)
        set_run_font(p.add_run(item), size=BODY_SIZE, color=BODY)


# ---------------------------------------------------------------- 拍摄执行表
def set_row_height(row, height, rule="atLeast"):
    trPr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:trHeight")
    el.set(qn("w:val"), str(int(height.twips)))
    el.set(qn("w:hRule"), rule)
    trPr.append(el)


def add_shoot_table(doc, rows, header=("时间", "具体视频画面", "口播/同期声", "执行要点")):
    n_cols = len(header)
    table = doc.add_table(rows=1, cols=n_cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = doc.styles["Table Grid"]
    widths = [Emu(int(CONTENT_W.emu * r)) for r in COL_RATIO[:n_cols]]
    set_col_widths(table, widths)
    set_table_borders(table, color=LINE_HEX, sz=4)
    set_cell_margins(table, top=70, bottom=70, left=110, right=110)

    hdr = table.rows[0]
    row_cant_split(hdr)
    set_row_height(hdr, Cm(1.27))
    for idx, cell in enumerate(hdr.cells):
        shade_cell(cell, ORANGE_HEX)
        cell_text(cell, header[idx], size=Pt(10.5), bold=True,
                  color=RGBColor(0xFF, 0xFF, 0xFF), align=WD_ALIGN_PARAGRAPH.CENTER,
                  line=Pt(16), valign=WD_ALIGN_VERTICAL.CENTER)

    for row_data in rows:
        r = table.add_row()
        row_cant_split(r)
        for idx, cell in enumerate(r.cells):
            cell_text(cell, row_data[idx], size=Pt(10), color=BODY,
                      align=None, line=Pt(15))
    return table


# ---------------------------------------------------------------- 主渲染
def render(payload: dict, out_path: Path) -> Path:
    doc = new_document()
    meta = payload.get("meta", {})

    add_title(doc, meta.get("title", "短视频拍摄执行脚本"))

    for label, key in (("适用平台", "platform"), ("目标受众", "audience"),
                       ("共同目标", "common_goal"), ("统一事实", "unified_facts")):
        if meta.get(key):
            add_kv(doc, label, meta[key])

    for s in payload.get("scripts", []):
        # 脚本序号 + 标题（样例此处仅比正文大半号，不加粗）
        p = add_para(doc, "", space_before=Pt(5), space_after=Pt(4),
                     line=Pt(17))
        set_run_font(p.add_run(f"{s.get('no', '')} | "), size=SCRIPT_TITLE_SIZE, color=BODY)
        set_run_font(p.add_run(s.get("title", "")), size=SCRIPT_TITLE_SIZE, color=BODY)

        if s.get("structure"):
            add_h2(doc, "内容结构")
            add_body(doc, s["structure"])

        if s.get("hook"):
            add_h2(doc, "开头钩子")
            add_body(doc, s["hook"])

        if s.get("voiceover"):
            add_h2(doc, "完整口播")
            for para in s["voiceover"]:
                add_body(doc, para)

        shoot = s.get("shoot") or []
        if shoot:
            add_h2(doc, "拍摄执行")
            rows = [[str(x.get("time", "")), str(x.get("picture", "")),
                     str(x.get("audio", "")), str(x.get("note", ""))] for x in shoot]
            add_shoot_table(doc, rows, header=tuple(s.get("shoot_header") or
                                                   ("时间", "具体视频画面", "口播/同期声", "执行要点")))

        if s.get("tags"):
            add_h2(doc, "发布标签")
            add_body(doc, s["tags"], space_after=Pt(6))

    checklist = payload.get("final_check") or []
    if checklist:
        add_h2(doc, payload.get("final_check_title", "统一发布前核对"), space_before=15)
        add_numbered(doc, checklist)

    note = payload.get("footer_note")
    if note:
        add_body(doc, note, space_before=8, space_after=Pt(0))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def main():
    if len(sys.argv) < 3:
        print("用法: python build_docx.py <payload.json> <out.docx>", file=sys.stderr)
        return 2
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = render(payload, Path(sys.argv[2]))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
