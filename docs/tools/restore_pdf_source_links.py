#!/usr/bin/env python3
"""Restore DOCX source hyperlinks omitted by some LibreOffice PDF exporters.

Only PDF link annotations are added; text, images, layout and pagination are
unchanged. Match hyperlink labels in document order and fail before saving if
any label cannot be located. Requires PyMuPDF.
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import fitz


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def field_target(instruction: str) -> str | None:
    match = re.search(r'\bHYPERLINK\s+"([^"]+)"', instruction, re.IGNORECASE)
    if not match or not match.group(1).startswith(("https://", "http://")):
        return None
    target = match.group(1)
    anchor = re.search(r'\\l\s+"([^"]+)"', instruction, re.IGNORECASE)
    if anchor:
        target = target.split("#", 1)[0] + "#" + anchor.group(1)
    return target


def source_links(docx: Path) -> list[tuple[str, str]]:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with zipfile.ZipFile(docx) as archive:
        relations = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relations
                   if item.attrib.get("TargetMode") == "External"
                   and item.attrib.get("Type", "").endswith("/hyperlink")}
        document = ET.fromstring(archive.read("word/document.xml"))
    links = []
    fields = []
    w = f'{{{ns["w"]}}}'
    for item in document.iter():
        if item.tag == w + "hyperlink":
            target = targets.get(item.attrib.get(f'{{{ns["r"]}}}id', ""))
            if target and target.startswith(("https://", "http://")):
                anchor = item.get(w + "anchor")
                if anchor:
                    target = target.split("#", 1)[0] + "#" + anchor
                label = "".join(item.itertext())
                links.append((compact(label), target))
        elif item.tag == w + "fldSimple":
            target = field_target(item.get(w + "instr", ""))
            if target:
                label = "".join(node.text or "" for node in item.iter(w + "t"))
                links.append((compact(label), target))
        elif item.tag == w + "fldChar":
            kind = item.get(w + "fldCharType")
            if kind == "begin":
                fields.append({"instruction": [], "label": [], "result": False})
            elif fields and kind == "separate":
                fields[-1]["result"] = True
            elif fields and kind == "end":
                field = fields.pop()
                target = field_target("".join(field["instruction"]))
                if target:
                    links.append((compact("".join(field["label"])), target))
        elif fields and item.tag == w + "instrText" and not fields[-1]["result"]:
            fields[-1]["instruction"].append(item.text or "")
        elif fields and item.tag == w + "t" and fields[-1]["result"]:
            fields[-1]["label"].append(item.text or "")
    return links


def page_characters(page):
    characters = []
    for b, block in enumerate(page.get_text("rawdict")["blocks"]):
        if block["type"] != 0:
            continue
        for line_index, line in enumerate(block["lines"]):
            for span in line["spans"]:
                for char in span["chars"]:
                    if not char["c"].isspace():
                        characters.append((char["c"], char["bbox"], (b, line_index)))
    return "".join(char[0] for char in characters), characters


def restore(docx: Path, pdf: Path) -> tuple[int, int]:
    expected = source_links(docx)
    document = fitz.open(pdf)
    pages = [page_characters(page) for page in document]
    page_index = offset = added = 0
    try:
        for label, target in expected:
            while page_index < len(pages):
                text, characters = pages[page_index]
                start = text.find(label, offset)
                if start >= 0:
                    break
                page_index += 1
                offset = 0
            else:
                raise ValueError(f"Cannot locate source link in PDF: {label!r}")

            groups = {}
            for _, bounds, line in characters[start:start + len(label)]:
                rect = fitz.Rect(bounds)
                groups[line] = groups[line] | rect if line in groups else rect
            page = document[page_index]
            existing = page.get_links()
            for rect in groups.values():
                if any(link.get("uri") == target and
                       (fitz.Rect(link["from"]) & rect).get_area() > rect.get_area() * 0.9
                       for link in existing):
                    continue
                page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": target})
                added += 1
            offset = start + len(label)

        output = pdf.with_name(pdf.stem + ".links.tmp.pdf")
        document.save(output, garbage=4, deflate=True)
        document.close()
        output.replace(pdf)
    finally:
        if not document.is_closed:
            document.close()
    return len(expected), added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("pdf", type=Path)
    args = parser.parse_args()
    matched, added = restore(args.docx, args.pdf)
    print(f"Matched {matched} DOCX source links; added {added} PDF link rectangles.")


if __name__ == "__main__":
    main()
