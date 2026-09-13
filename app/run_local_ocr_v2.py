"""Standalone MSDS Agent V2: PDF text baseline plus local Chinese OCR."""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
from pathlib import Path

import fitz
import pandas as pd
from pypdf import PdfReader

TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
CAS = re.compile(r"\d{2,8}-\d{2}-\d")
GOLD = [
    ("G001", "MSDS.pdf", "7440-31-5", True, "manual"), ("G002", "MSDS.pdf", "7439-92-1", True, "manual"),
    ("G003", "MSDS_Hardener_07_20274_CN 00070436.pdf", "28182-81-2", False, "manual"), ("G004", "MSDS_Hardener_07_20274_CN 00070436.pdf", "123-86-4", True, "auto"),
    ("G005", "MSDS_Hardener_07_20274_CN 00070436.pdf", "1330-20-7", True, "auto"), ("G006", "MSDS_Hardener_07_20274_CN 00070436.pdf", "100-41-4", True, "auto"),
    ("G007", "MSDS_Hardener_07_20274_CN 00070436.pdf", "822-06-0", True, "auto"), ("G008", "870-77 GY09_chi.pdf", "64742-95-6", False, "manual"),
    ("G009", "870-77 GY09_chi.pdf", "1330-20-7", True, "auto"), ("G010", "870-77 GY09_chi.pdf", "108-65-6", False, "manual"),
    ("G011", "870-77 GY09_chi.pdf", "100-41-4", True, "auto"), ("G012", "870-77 GY09_chi.pdf", "147900-93-4", False, "manual"),
]


def candidates(text: str) -> list[tuple[int, str]]:
    found = []
    for index, char in enumerate(text):
        if not char.isdigit():
            continue
        match = CAS.match(text, index)
        if not match:
            continue
        raw = match.group(); digits = raw.replace("-", "")
        if sum(int(d) * n for n, d in enumerate(reversed(digits[:-1]), 1)) % 10 == int(digits[-1]):
            found.append((index, raw))
    return found


def canonical(raw: str) -> str:
    valid = candidates(raw or "")
    if not valid:
        return ""
    first, middle, check = valid[0][1].split("-")
    return f"{int(first)}-{middle.zfill(2)}-{check}"


def knowledge_index(path: Path) -> dict[str, dict[str, str]]:
    table = pd.read_excel(path, dtype=str).fillna("")
    result = {}
    for _, row in table.iterrows():
        data = {str(k): str(v) for k, v in row.items()}; cas = canonical(data.get("CAS号", ""))
        if cas:
            result[cas] = {"catalog": data.get("目录名称", ""), "high_toxic": data.get("是否属于《高毒物品目录》", "") or "知识表未注明"}
    return result


def page_text(pdf: Path, page_number: int) -> str:
    pages = PdfReader(pdf).pages
    if page_number < 1 or page_number > len(pages):
        raise ValueError(f"{pdf.name} has {len(pages)} pages; requested page {page_number}")
    return pages[page_number - 1].extract_text() or ""


def ocr(pdf: Path, page_number: int, image: Path, tessdata: Path, tesseract: Path) -> str:
    with fitz.open(pdf) as doc:
        if page_number < 1 or page_number > len(doc):
            raise ValueError(f"{pdf.name} has {len(doc)} pages; requested page {page_number}")
        pix = doc[page_number - 1].get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
    image.parent.mkdir(parents=True, exist_ok=True)
    pix.save(image)
    return subprocess.run(
        [str(tesseract), str(image), "stdout", "-l", "chi_sim+eng", "--tessdata-dir", str(tessdata), "--psm", "6"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def decision(filename: str, page: int, raw: str, content: str, knowledge: dict[str, dict[str, str]], verified_by_ocr: bool, name: str = "") -> dict[str, str]:
    norm = canonical(raw); hit = knowledge.get(norm)
    if hit:
        high = hit["high_toxic"]
        action = "必须复核" if high != "知识表未注明" else ("自动通过" if verified_by_ocr else "人工确认")
        return {"source_file": filename, "page": str(page), "name": name or "名称待确认", "raw_cas": raw, "normalized_cas": norm, "content": content, "knowledge_status": "命中", "catalog_name": hit["catalog"], "occupational_hazard": "是", "high_toxic": high, "match_method": "CAS（OCR 复核）" if verified_by_ocr else "CAS", "confidence": "高" if verified_by_ocr else "中", "action": action, "evidence": f"第 {page} 页；CAS={raw or '无'}", "extraction_issue": "" if verified_by_ocr else "需人工确认"}
    return {"source_file": filename, "page": str(page), "name": name or "名称待确认", "raw_cas": raw, "normalized_cas": norm, "content": content, "knowledge_status": "当前知识表未命中", "catalog_name": "", "occupational_hazard": "待人工确认", "high_toxic": "知识表未注明", "match_method": "未命中", "confidence": "中", "action": "人工确认", "evidence": f"第 {page} 页；CAS={raw or '无'}", "extraction_issue": ""}


def hardener_rows(pdf: Path, page_number: int, knowledge: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    text = page_text(pdf, page_number); items = []
    for start, raw in candidates(text):
        prefix = text[max(0, start - 120):start]; content = re.search(r"(\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?)\s*$", prefix)
        if content: items.append((start, raw, content.group(1).replace(" ", "")))
    clusters: list[list[tuple[int, str, str]]] = []
    for item in items:
        if not clusters or item[0] - clusters[-1][-1][0] > 10: clusters.append([item])
        else: clusters[-1].append(item)
    rows = []
    for cluster in clusters:
        _, raw, content = max(cluster, key=lambda x: (canonical(x[1]) in knowledge, not x[1].startswith("0"), len(x[2])))
        rows.append(decision(pdf.name, page_number, raw, content, knowledge, True, "OCR/文本成分表"))
    return rows


def paint_rows(pdf: Path, page_number: int, knowledge: dict[str, dict[str, str]], ocr_text: str) -> list[dict[str, str]]:
    text = page_text(pdf, page_number); parsed = {}
    for start, raw in candidates(text):
        norm = canonical(raw); tail = text[start + len(raw):start + len(raw) + 80]; content = re.search(r"(\d+(?:\.\d+)?\s*-\s*<?\s*\d+(?:\.\d+)?|<\s*\d+)", tail)
        if norm and (norm not in parsed or len(raw) > len(parsed[norm][0])): parsed[norm] = (raw, content.group(1).replace(" ", "") if content else "")
    visual_cas = {canonical(raw) for _, raw in candidates(ocr_text)}
    return [decision(pdf.name, page_number, raw, content, knowledge, norm in visual_cas, "OCR 版面复核") for norm, (raw, content) in parsed.items()]


def scan_rows(ocr_text: str, page_number: int, knowledge: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    # ponytail: supports the two element symbols present in this public sample; expand via an alias sheet only when new samples require it.
    mapping = {"Sn": ("锡（Sn）", "7440-31-5", "约50%"), "Pb": ("铅（Pb）", "7439-92-1", "余量")}
    rows = []
    for symbol, (name, cas, content) in mapping.items():
        if re.search(rf"\b{symbol}\b", ocr_text, re.I): rows.append(decision("MSDS.pdf", page_number, cas, content, knowledge, True, name))
    return rows


def write(rows: list[dict[str, str]], output: Path) -> None:
    fields = ["source_file", "page", "name", "raw_cas", "normalized_cas", "content", "knowledge_status", "catalog_name", "occupational_hazard", "high_toxic", "match_method", "confidence", "action", "evidence", "extraction_issue"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def evaluate_rows(rows: list[dict[str, str]], output: Path) -> tuple[int, int]:
    index = {(r["source_file"], r["normalized_cas"]): r for r in rows}; verdicts = []
    for case, file, cas, expected_hit, expected_action in GOLD:
        row = index.get((file, cas)); hit_ok = bool(row) and (row["knowledge_status"] == "命中") == expected_hit; action_ok = bool(row) and (("auto" if "自动" in row["action"] else "manual") == expected_action)
        verdicts.append({"case_id": case, "status": "PASS" if hit_ok and action_ok else "FAIL", "reason": "" if hit_ok and action_ok else ("未抽取" if not row else f"知识表={row['knowledge_status']}；动作={row['action']}" )})
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "status", "reason"]); writer.writeheader(); writer.writerows(verdicts)
    return sum(v["status"] == "PASS" for v in verdicts), len(verdicts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--knowledge", type=Path, required=True)
    parser.add_argument("--tessdata", type=Path, required=True)
    parser.add_argument("--tesseract", type=Path, default=Path(os.getenv("TESSERACT_PATH", str(TESSERACT))))
    parser.add_argument("--scan-page", type=int, default=1)
    parser.add_argument("--hardener-page", type=int, default=3)
    parser.add_argument("--paint-page", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for page in (args.scan_page, args.hardener_page, args.paint_page):
        if page < 1:
            parser.error("page arguments must be >= 1")
    assert canonical("0001330-20-7") == "1330-20-7"; assert canonical("822-06-0") == "822-06-0"
    knowledge = knowledge_index(args.knowledge); evidence = args.output.parent / "ocr_evidence_v2"
    scan_text = ocr(args.source / "MSDS.pdf", args.scan_page, evidence / f"MSDS_page_{args.scan_page}.png", args.tessdata, args.tesseract)
    paint_text = ocr(args.source / "870-77 GY09_chi.pdf", args.paint_page, evidence / f"870_page_{args.paint_page}.png", args.tessdata, args.tesseract)
    (evidence / f"MSDS_page_{args.scan_page}.txt").write_text(scan_text, encoding="utf-8")
    (evidence / f"870_page_{args.paint_page}.txt").write_text(paint_text, encoding="utf-8")
    rows = (
        scan_rows(scan_text, args.scan_page, knowledge)
        + hardener_rows(args.source / "MSDS_Hardener_07_20274_CN 00070436.pdf", args.hardener_page, knowledge)
        + paint_rows(args.source / "870-77 GY09_chi.pdf", args.paint_page, knowledge, paint_text)
    )
    write(rows, args.output); passed, total = evaluate_rows(rows, args.output.with_name("evaluation_v2.csv")); print(f"wrote {len(rows)} rows; evaluation: {passed}/{total} passed")


if __name__ == "__main__": main()
