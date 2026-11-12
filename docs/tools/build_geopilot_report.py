#!/usr/bin/env python3
"""Build the formal GeoPilot requirements/design Word report from Markdown."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt, RGBColor


DOCS_ROOT = Path(__file__).resolve().parents[1]
SOURCE = DOCS_ROOT / "source" / "GeoPilot_产品需求规格与系统详细设计报告_V2.0.md"
OUTPUT = DOCS_ROOT / "GeoPilot_产品需求规格与系统详细设计报告_V2.0.docx"
ARCH_IMAGE = DOCS_ROOT / "assets" / "figures" / "fig-07-local-architecture.png"

NAVY = "17324D"
BLUE = "2E6F9E"
LIGHT_BLUE = "E9F3FB"
PALE_BLUE = "F4F8FB"
GRAY = "526A7F"
LIGHT_GRAY = "E8EDF1"
ORANGE = "D17C2F"
WHITE = "FFFFFF"
IMAGE_BORDER = "D8E3EB"
CONTENT_WIDTH_CM = 16.8


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top=90, start=100, bottom=90, end=100) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for margin, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{margin}"))
        if node is None:
            node = OxmlElement(f"w:{margin}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def prevent_row_split(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.append(cant_split)


def set_table_borders(table, color="A8B6C3", size="4") -> None:
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = f"w:{edge}"
        node = borders.find(qn(tag))
        if node is None:
            node = OxmlElement(tag)
            borders.append(node)
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), size)
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), color)


def set_table_layout_fixed(table) -> None:
    tbl_pr = table._tbl.tblPr
    layout = tbl_pr.first_child_found_in("w:tblLayout")
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    width = tbl_pr.first_child_found_in("w:tblW")
    if width is None:
        width = OxmlElement("w:tblW")
        tbl_pr.append(width)
    width.set(qn("w:type"), "pct")
    width.set(qn("w:w"), "5000")


def set_cell_width(cell, width_cm: float) -> None:
    width = Cm(width_cm)
    cell.width = width
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:type"), "dxa")
    tc_w.set(qn("w:w"), str(width.twips))


def table_column_ratios(headers: list[str]) -> list[float]:
    key = tuple(headers)
    presets = {
        ("版本", "日期", "修订说明", "状态"): [0.10, 0.17, 0.50, 0.23],
        ("编号", "需求说明", "判定方式", "优先级"): [0.16, 0.42, 0.32, 0.10],
        ("编号", "场景与输入", "自动断言", "预期结果"): [0.09, 0.35, 0.30, 0.26],
        ("项目与来源编号", "所查提交／日期", "当前材料形态", "本章重点"): [0.27, 0.24, 0.29, 0.20],
        ("项目", "任务组织", "参数与执行", "结果判断的证据范围"): [0.23, 0.23, 0.23, 0.31],
        ("项目", "优先采用的方式", "主要改造工作", "开始接入前的检查"): [0.23, 0.23, 0.29, 0.25],
        ("借鉴内容", "来源与采用边界", "对应模块", "现有用例或检查"): [0.25, 0.29, 0.19, 0.27],
        ("组件", "适合承担的职责", "GeoPilot 需要补充的控制", "本轮建议"): [0.17, 0.30, 0.32, 0.21],
        ("地图库", "适合的使用方式", "栅格与投影需要注意什么", "对个人原型的判断"): [0.19, 0.27, 0.32, 0.22],
        ("工作包", "主要内容", "前置依赖", "完成判定"): [0.25, 0.29, 0.14, 0.32],
        ("步骤", "主要交付物", "前置条件", "进入下一步的标准"): [0.21, 0.27, 0.15, 0.37],
        ("固定用例", "首次完整验收", "核对重点", "失败时优先查看"): [0.20, 0.17, 0.34, 0.29],
        ("工作包", "负责内容", "对应步骤", "主要需求"): [0.24, 0.28, 0.20, 0.28],
        ("官方资料", "本轮核对内容", "对应步骤"): [0.40, 0.40, 0.20],
        ("阅读目的", "建议阅读内容", "读后应能回答的问题"): [0.22, 0.32, 0.46],
        ("检查内容", "操作方式", "完成判定", "关联需求"): [0.16, 0.30, 0.34, 0.20],
        ("内容", "公共约定", "由方法或工具确定的内容"): [0.18, 0.42, 0.40],
        ("能力方向", "可以组织的处理", "在原型中的落实方式"): [0.18, 0.42, 0.40],
        ("对象", "主要字段", "关系"): [0.24, 0.51, 0.25],
        ("对象", "主要字段", "关系与保存方式"): [0.23, 0.43, 0.34],
        ("步骤", "接收内容", "交给后续的内容"): [0.21, 0.35, 0.44],
        ("方法与路径", "请求中的关键信息", "返回与下一步"): [0.46, 0.25, 0.29],
        ("事件", "执行状态", "检查状态", "界面处理"): [0.25, 0.19, 0.23, 0.33],
        ("方法与路径", "用途", "主要返回"): [0.49, 0.24, 0.27],
        ("错误类型", "示例说明", "用户下一步"): [0.34, 0.36, 0.30],
        ("编号", "官方仓库", "所查 main 提交（链接含完整提交号）"): [0.10, 0.49, 0.41],
        ("来源", "本次定位的关键文件", "支持的比较判断"): [0.10, 0.49, 0.41],
        ("版本", "日期", "修订说明", "编制/修订", "状态"): [0.11, 0.14, 0.39, 0.22, 0.14],
        ("角色", "姓名", "审批意见", "签字/日期"): [0.19, 0.19, 0.39, 0.23],
        ("用户类型", "典型角色", "核心诉求", "系统侧重点"): [0.18, 0.23, 0.27, 0.32],
        ("控制点", "系统行为", "用户行为", "禁止行为"): [0.17, 0.29, 0.25, 0.29],
        ("编号", "业务需求", "验收说明", "优先级"): [0.16, 0.41, 0.33, 0.10],
        ("编号", "需求说明", "验收要点", "优先级"): [0.17, 0.37, 0.36, 0.10],
        ("编号", "需求/目标", "建议验收口径", "优先级"): [0.17, 0.36, 0.37, 0.10],
        ("编号", "需求/目标", "验收口径", "优先级"): [0.17, 0.36, 0.37, 0.10],
        ("编号", "待决事项", "决策所需输入", "建议决策阶段"): [0.14, 0.28, 0.41, 0.17],
        ("业务需求", "关键功能需求", "典型用例", "主要测试/验收"): [0.24, 0.27, 0.19, 0.30],
        ("决策项", "设计结论", "目的"): [0.23, 0.34, 0.43],
        ("领域", "典型任务", "主要成果"): [0.20, 0.40, 0.40],
        ("传统工作方式", "GeoPilot 目标方式", "预期价值"): [0.29, 0.36, 0.35],
        ("层级", "模块", "主要职责"): [0.17, 0.40, 0.43],
        ("模块", "负责", "不负责"): [0.20, 0.40, 0.40],
        ("等级", "典型情况", "默认处理"): [0.15, 0.44, 0.41],
        ("对象/表", "主要职责", "关键关系"): [0.28, 0.31, 0.41],
        ("错误码", "含义", "建议处理"): [0.27, 0.31, 0.42],
        ("技术", "用途", "约束"): [0.22, 0.34, 0.44],
        ("层级", "测试目标", "主要方法"): [0.23, 0.34, 0.43],
        ("指标", "初期目标", "验收条件"): [0.29, 0.18, 0.53],
        ("风险", "影响", "缓解措施"): [0.27, 0.24, 0.49],
        ("术语", "说明"): [0.29, 0.71],
        ("角色", "典型权限"): [0.27, 0.73],
        ("页面", "核心功能"): [0.25, 0.75],
        ("类别", "数量目标"): [0.73, 0.27],
        ("指标", "定义要点"): [0.31, 0.69],
        ("决策项", "最终选择/当前基线"): [0.39, 0.61],
    }
    if key in presets:
        return presets[key]
    count = len(headers)
    return [1.0 / count] * count


def add_image_border(inline_shape, color: str = IMAGE_BORDER) -> None:
    doc_pr = inline_shape._inline.docPr
    pic = inline_shape._inline.graphic.graphicData.pic
    sp_pr = pic.spPr
    line = OxmlElement("a:ln")
    line.set("w", "6350")
    fill = OxmlElement("a:solidFill")
    rgb = OxmlElement("a:srgbClr")
    rgb.set("val", color)
    fill.append(rgb)
    line.append(fill)
    sp_pr.append(line)
    doc_pr.set("title", "GeoPilot 报告插图")


def set_run_font(run, name="Noto Serif CJK SC", size=10.5, bold=None, color=None) -> None:
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def add_field(paragraph, instruction: str, result_text: str = "") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = result_text
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend((begin, instr, separate, text, end))


def add_page_number(paragraph) -> None:
    add_field(paragraph, "PAGE", "1")


def add_toc(paragraph) -> None:
    add_field(paragraph, 'TOC \\o "1-2" \\h \\z', "右键更新目录")


def enable_update_fields(document: Document) -> None:
    settings = document.settings._element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields")
        settings.append(update)
    update.set(qn("w:val"), "true")


def configure_styles(document: Document) -> None:
    styles = document.styles

    normal = styles["Normal"]
    normal.font.name = "Noto Serif CJK SC"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Serif CJK SC")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.line_spacing = 1.2
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    normal.paragraph_format.widow_control = True

    for level, size, color, before, after in (
        (1, 18, NAVY, 12, 8),
        (2, 14, BLUE, 12, 6),
        (3, 12, NAVY, 8, 4),
        (4, 11, GRAY, 8, 4),
    ):
        style = styles[f"Heading {level}"]
        style.font.name = "Noto Sans CJK SC"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.keep_together = True
    styles["Heading 1"].paragraph_format.page_break_before = True

    # Front matter keeps its visual hierarchy without occupying the body TOC.
    for level in (1, 2):
        style = styles.add_style(f"Front Matter {level}", WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = styles[f"Heading {level}"]
        outline = OxmlElement("w:outlineLvl")
        outline.set(qn("w:val"), "9")
        style._element.get_or_add_pPr().append(outline)

    for level in (1, 2):
        name = f"TOC {level}"
        toc = styles[name] if name in styles else styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        toc.font.name = "Noto Sans CJK SC"
        toc._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
        toc.font.size = Pt(10.5 if level == 1 else 10)
        toc.font.bold = level == 1
        toc.font.color.rgb = RGBColor.from_string(NAVY if level == 1 else GRAY)
        toc.paragraph_format.left_indent = Cm(0 if level == 1 else 0.6)
        toc.paragraph_format.first_line_indent = Cm(0)
        toc.paragraph_format.tab_stops.add_tab_stop(
            Cm(CONTENT_WIDTH_CM), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
        )
        toc.paragraph_format.space_before = Pt(4 if level == 1 else 0)
        toc.paragraph_format.space_after = Pt(2)
        toc.paragraph_format.line_spacing = 1.15
        toc.paragraph_format.keep_with_next = level == 1

    for style_name in ("List Bullet", "List Number"):
        style = styles[style_name]
        style.font.name = "Noto Serif CJK SC"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Serif CJK SC")
        style.font.size = Pt(10.5)
        style.paragraph_format.left_indent = Cm(0.65)
        style.paragraph_format.first_line_indent = Cm(-0.25)
        style.paragraph_format.space_after = Pt(2)

    if "Code Block" not in [style.name for style in styles]:
        code = styles.add_style("Code Block", WD_STYLE_TYPE.PARAGRAPH)
    else:
        code = styles["Code Block"]
    code.font.name = "DejaVu Sans Mono"
    code._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans Mono CJK SC")
    code.font.size = Pt(8.5)
    code.paragraph_format.left_indent = Cm(0.45)
    code.paragraph_format.right_indent = Cm(0.35)
    code.paragraph_format.space_before = Pt(4)
    code.paragraph_format.space_after = Pt(4)
    code.paragraph_format.line_spacing = 1.1

    if "Quote Box" not in [style.name for style in styles]:
        quote = styles.add_style("Quote Box", WD_STYLE_TYPE.PARAGRAPH)
    else:
        quote = styles["Quote Box"]
    quote.font.name = "Noto Sans CJK SC"
    quote._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
    quote.font.size = Pt(10)
    quote.font.color.rgb = RGBColor.from_string(NAVY)
    quote.paragraph_format.left_indent = Cm(0.5)
    quote.paragraph_format.right_indent = Cm(0.3)
    quote.paragraph_format.space_before = Pt(5)
    quote.paragraph_format.space_after = Pt(7)


def set_paragraph_shading(paragraph, fill: str, border_color: str | None = None) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    p_pr.append(shd)
    if border_color:
        p_bdr = OxmlElement("w:pBdr")
        left = OxmlElement("w:left")
        left.set(qn("w:val"), "single")
        left.set(qn("w:sz"), "20")
        left.set(qn("w:space"), "6")
        left.set(qn("w:color"), border_color)
        p_bdr.append(left)
        p_pr.append(p_bdr)


def setup_document() -> Document:
    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.1)
    section.bottom_margin = Cm(1.9)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.0)
    section.header_distance = Cm(0.85)
    section.footer_distance = Cm(0.85)
    section.different_first_page_header_footer = True

    configure_styles(document)
    enable_update_fields(document)

    header = section.header
    hp = header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = hp.add_run("GeoPilot 产品需求规格与系统详细设计报告  ·  V2.0")
    set_run_font(run, "Noto Sans CJK SC", 8.5, color=GRAY)
    p_pr = hp._p.get_or_add_pPr()
    border = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "8")
    bottom.set(qn("w:space"), "5")
    bottom.set(qn("w:color"), BLUE)
    border.append(bottom)
    p_pr.append(border)

    footer = section.footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run("个人原型设计稿    |    第 ")
    set_run_font(run, "Noto Sans CJK SC", 8.5, color=GRAY)
    add_page_number(fp)
    run = fp.add_run(" 页")
    set_run_font(run, "Noto Sans CJK SC", 8.5, color=GRAY)

    return document


def add_cover(document: Document) -> None:
    for _ in range(3):
        document.add_paragraph()

    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("GEOPILOT")
    set_run_font(run, "Noto Sans CJK SC", 24, bold=True, color=BLUE)

    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(18)
    p.paragraph_format.space_after = Pt(12)
    run = p.add_run("GeoPilot 产品需求规格与\n系统详细设计报告")
    set_run_font(run, "Noto Sans CJK SC", 28, bold=True, color=NAVY)

    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("个人原型需求 · 本机系统设计")
    set_run_font(run, "Noto Sans CJK SC", 10.5, bold=True, color=GRAY)

    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(20)
    run = p.add_run("面向个人实现的 GIS Agent 原型")
    set_run_font(run, "Noto Sans CJK SC", 11, bold=True, color=ORANGE)

    table = document.add_table(rows=5, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.columns[0].width = Cm(4.0)
    table.columns[1].width = Cm(9.2)
    values = [
        ("项目名称", "GeoPilot"),
        ("文档版本", "V2.0（个人原型设计稿）"),
        ("产品类型", "单用户、本机 Web GIS 原型"),
        ("编制日期", "2026-11-11"),
        ("文档状态", "通用能力与实现计划整理稿"),
    ]
    for idx, (label, value) in enumerate(values):
        left, right = table.rows[idx].cells
        left.width = Cm(4.0)
        right.width = Cm(9.2)
        set_cell_shading(left, LIGHT_BLUE)
        set_cell_shading(right, WHITE)
        for cell in (left, right):
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_margins(cell, top=110, bottom=110, start=140, end=140)
        lp = left.paragraphs[0]
        lp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        lr = lp.add_run(label)
        set_run_font(lr, "Noto Sans CJK SC", 10.5, bold=True, color=NAVY)
        rp = right.paragraphs[0]
        rr = rp.add_run(value)
        set_run_font(rr, "Noto Serif CJK SC", 10.5, color=NAVY)
    set_table_borders(table, "B8CDD9", "5")

    p = document.add_paragraph()
    p.paragraph_format.space_before = Pt(18)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("以通用能力组织需求、模块、接口和实现步骤。\n配套方法样例与检查标准，供个人开发和测试使用。")
    set_run_font(run, "Noto Sans CJK SC", 9.5, color=GRAY)

    document.add_page_break()


INLINE_PATTERN = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?://[^\s)]+\))")


def add_inline(paragraph, text: str, base_size=10.5, base_color=None, bold=False) -> None:
    text = text.replace("  ", " ").replace("<br>", "\n").strip()
    parts = INLINE_PATTERN.split(text)
    for part in parts:
        if not part:
            continue
        link = re.fullmatch(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", part)
        if link:
            hyperlink = OxmlElement("w:hyperlink")
            hyperlink.set(qn("r:id"), paragraph.part.relate_to(link.group(2), RT.HYPERLINK, is_external=True))
            run = paragraph.add_run(link.group(1))
            set_run_font(run, "Noto Serif CJK SC", base_size, bold=bold, color=base_color or BLUE)
            run.font.underline = True
            hyperlink.append(run._r)
            paragraph._p.append(hyperlink)
        elif part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            set_run_font(run, "Noto Serif CJK SC", base_size, bold=True, color=base_color)
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            set_run_font(run, "DejaVu Sans Mono", max(base_size - 1, 8), bold=False, color=NAVY)
            r_pr = run._r.get_or_add_rPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:fill"), LIGHT_GRAY)
            r_pr.append(shd)
        else:
            run = paragraph.add_run(part)
            set_run_font(run, "Noto Serif CJK SC", base_size, bold=bold, color=base_color)


def parse_table_line(line: str) -> list[str]:
    content = line.strip().strip("|")
    return [cell.strip() for cell in content.split("|")]


def is_table_separator(line: str) -> bool:
    cells = parse_table_line(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def add_table(document: Document, rows: list[list[str]]) -> None:
    if not rows:
        return
    col_count = max(len(row) for row in rows)
    table = document.add_table(rows=len(rows), cols=col_count)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_layout_fixed(table)
    set_table_borders(table)
    ratios = table_column_ratios(rows[0])
    widths = [CONTENT_WIDTH_CM * ratio for ratio in ratios]
    grid_columns = table._tbl.tblGrid.gridCol_lst
    for c_idx, width_cm in enumerate(widths):
        width = Cm(width_cm)
        table.columns[c_idx].width = width
        if c_idx < len(grid_columns):
            grid_columns[c_idx].set(qn("w:w"), str(width.twips))
    for r_idx, row_data in enumerate(rows):
        row = table.rows[r_idx]
        prevent_row_split(row)
        if r_idx == 0:
            set_repeat_table_header(row)
        for c_idx in range(col_count):
            cell = row.cells[c_idx]
            set_cell_width(cell, widths[c_idx])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_margins(cell, top=80, start=105, bottom=80, end=105)
            if r_idx == 0:
                set_cell_shading(cell, NAVY)
            elif r_idx % 2 == 0:
                set_cell_shading(cell, PALE_BLUE)
            text = row_data[c_idx] if c_idx < len(row_data) else ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.12
            # Keep short requirement tables together. In longer tables keep
            # the header with data, and the final two rows together.
            p.paragraph_format.keep_with_next = (
                r_idx < len(rows) - 1 if len(rows) <= 5
                else r_idx == 0 or r_idx == len(rows) - 2
            )
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            add_inline(p, text, base_size=8.4 if col_count >= 5 else (9.2 if col_count == 4 else 9.5),
                       base_color=WHITE if r_idx == 0 else NAVY,
                       bold=r_idx == 0)
    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)
    spacer.paragraph_format.line_spacing = Pt(2)


def add_code_block(document: Document, lines: list[str]) -> None:
    p = document.add_paragraph(style="Code Block")
    set_paragraph_shading(p, "F3F5F7", "A8B6C3")
    run = p.add_run("\n".join(lines))
    set_run_font(run, "DejaVu Sans Mono", 8.3, color=NAVY)
    p.paragraph_format.keep_together = True


def add_quote(document: Document, text: str) -> None:
    p = document.add_paragraph(style="Quote Box")
    set_paragraph_shading(p, LIGHT_BLUE, BLUE)
    add_inline(p, text, base_size=10, base_color=NAVY)


def add_heading(document: Document, text: str, level: int, front_matter=False) -> None:
    p = document.add_heading(level=level)
    if front_matter and level in (1, 2):
        p.style = document.styles[f"Front Matter {level}"]
    module_label = level == 3 and not re.match(r"\d+\.", text)
    if module_label:
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(3)
    add_inline(p, text, base_size=10.5 if module_label else {1: 18, 2: 14, 3: 12, 4: 11}.get(level, 10.5),
               base_color={1: NAVY, 2: BLUE, 3: NAVY, 4: GRAY}.get(level, NAVY),
               bold=True)
    if level == 1:
        p_pr = p._p.get_or_add_pPr()
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "12")
        bottom.set(qn("w:space"), "5")
        bottom.set(qn("w:color"), BLUE)
        border.append(bottom)
        p_pr.append(border)


def add_body_paragraph(document: Document, text: str, keep_with_next=False) -> None:
    p = document.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0.74)
    p.paragraph_format.keep_with_next = keep_with_next
    add_inline(p, text)


def add_image(document: Document, alt: str, relative_path: str, figure_label: str) -> None:
    image_path = (SOURCE.parent / relative_path).resolve()
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(3)
    # Inline pictures need automatic line height; an inherited exact height
    # clips the drawing and lets subsequent content overlap it in Writer.
    p.paragraph_format.line_spacing = 1.0
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.keep_with_next = True
    p.paragraph_format.keep_together = True
    run = p.add_run()
    section = document.sections[-1]
    available_width = section.page_width - section.left_margin - section.right_margin
    target_width = available_width
    shape = run.add_picture(str(image_path), width=target_width)
    add_image_border(shape)
    shape._inline.docPr.set("descr", alt)
    caption = document.add_paragraph()
    caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
    caption.paragraph_format.keep_with_next = True
    caption.paragraph_format.keep_together = True
    caption.paragraph_format.line_spacing = Pt(14)
    caption.paragraph_format.space_before = Pt(2)
    caption.paragraph_format.space_after = Pt(6)
    cr = caption.add_run(f"图 {figure_label}　{alt}")
    set_run_font(cr, "Noto Sans CJK SC", 9, color=GRAY)


def parse_markdown(document: Document, text: str) -> None:
    lines = text.splitlines()
    start = 0
    for idx, line in enumerate(lines):
        if line.strip() == "<!-- COVER_END -->":
            start = idx + 1
            break

    i = start
    current_chapter = "0"
    front_matter = True
    figure_counts: dict[str, int] = {}
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped == "<!-- PAGE_BREAK -->":
            document.add_page_break()
            i += 1
            continue

        if stripped == "<!-- TOC -->":
            front_matter = False
            document.add_page_break()
            p = document.add_paragraph()
            p.style = document.styles["Title"]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run("目录")
            set_run_font(run, "Noto Sans CJK SC", 18, bold=True, color=NAVY)
            toc = document.add_paragraph()
            toc.paragraph_format.space_before = Pt(10)
            add_toc(toc)
            i += 1
            continue

        if stripped.startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            add_code_block(document, code_lines)
            i += 1
            continue

        image_match = re.fullmatch(r"!\[(.*?)\]\((.*?)\)", stripped)
        if image_match:
            figure_counts[current_chapter] = figure_counts.get(current_chapter, 0) + 1
            figure_label = f"{current_chapter}-{figure_counts[current_chapter]}"
            add_image(document, image_match.group(1), image_match.group(2), figure_label)
            i += 1
            continue

        heading_match = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading_match:
            heading_level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            if heading_level == 1:
                chapter_match = re.match(r"^(\d+)(?:\s|$)", heading_text)
                if chapter_match:
                    current_chapter = chapter_match.group(1)
                elif heading_text == "执行摘要":
                    current_chapter = "1"
            add_heading(document, heading_text, heading_level, front_matter=front_matter)
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and is_table_separator(lines[i + 1].strip()):
            rows = [parse_table_line(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(parse_table_line(lines[i]))
                i += 1
            add_table(document, rows)
            continue

        if stripped.startswith(">"):
            add_quote(document, stripped.lstrip("> "))
            i += 1
            continue

        bullet_match = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet_match:
            p = document.add_paragraph(style="List Bullet")
            add_inline(p, bullet_match.group(1))
            i += 1
            continue

        number_match = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
        if number_match:
            p = document.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.left_indent = Cm(0.7)
            p.paragraph_format.first_line_indent = Cm(-0.48)
            p.paragraph_format.space_after = Pt(2)
            marker = p.add_run(f"{number_match.group(1)}. ")
            set_run_font(marker, "Noto Sans CJK SC", 10.5, bold=False, color=NAVY)
            add_inline(p, number_match.group(2))
            i += 1
            continue

        following = next((item.strip() for item in lines[i + 1:] if item.strip()), "")
        list_follows = bool(re.match(r"^(?:[-*]|\d+[.)])\s+", following))
        add_body_paragraph(document, stripped, keep_with_next=(
            following.startswith("![") or
            ((following.startswith("|") or list_follows) and len(stripped) < 180)
        ))
        i += 1


def add_core_properties(document: Document) -> None:
    props = document.core_properties
    props.title = "GeoPilot 产品需求规格与系统详细设计报告"
    props.subject = "GeoPilot 单用户本机 Web GIS 个人原型需求与系统设计"
    props.author = "GeoPilot"
    props.keywords = "GeoPilot, GIS Agent, 个人原型, 本机 Web GIS, 规则检查"
    props.comments = "V2.0 通用能力与实现计划整理稿；调研截至 2026-11-11，含 27 张图、通用需求、方法接入检查、开源来源及 16 项初始回归设计。"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT, help="DOCX output path")
    args = parser.parse_args()
    output_path = args.output.expanduser().resolve()

    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    if not ARCH_IMAGE.exists():
        raise FileNotFoundError(ARCH_IMAGE)

    document = setup_document()
    add_core_properties(document)
    add_cover(document)
    parse_markdown(document, SOURCE.read_text(encoding="utf-8"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    print(output_path)


if __name__ == "__main__":
    main()
