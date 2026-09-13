from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from pypdf import PdfReader

CAS = re.compile(r"\d{2,8}-\d{2}-\d")
SAMPLE_DIR = Path(__file__).parent / "samples"


def cas_check_digit(raw: str) -> bool:
    """验证 CAS 号校验位"""
    digits = raw.replace("-", "")
    return sum(int(d) * n for n, d in enumerate(reversed(digits[:-1]), 1)) % 10 == int(digits[-1])


def find_all_cas(text: str, dedup: bool = True) -> list[dict]:
    """找出文本中所有 CAS 候选，返回位置、原始值、规范值、是否通过校验。
    dedup=True 时，移除位置重叠的子串匹配（如 822-06-0 中的 22-06-0）。"""
    results = []
    for index, char in enumerate(text):
        if not char.isdigit():
            continue
        match = CAS.match(text, index)
        if not match:
            continue
        raw = match.group()
        valid = cas_check_digit(raw)
        if valid:
            parts = raw.split("-")
            canonical = f"{int(parts[0])}-{parts[1].zfill(2)}-{parts[2]}"
        else:
            canonical = raw
        results.append({
            "start": index,
            "raw": raw,
            "canonical": canonical,
            "valid": valid,
        })
    if dedup and results:
        # 移除被更长匹配覆盖的子串匹配
        filtered = []
        for r in results:
            r_end = r["start"] + len(r["raw"])
            overlapped = False
            for other in results:
                if other is r:
                    continue
                o_end = other["start"] + len(other["raw"])
                if other["start"] <= r["start"] and o_end >= r_end and len(other["raw"]) > len(r["raw"]):
                    overlapped = True
                    break
            if not overlapped:
                filtered.append(r)
        results = filtered
    return results


def load_knowledge(path: Path) -> dict[str, dict[str, str]]:
    table = pd.read_excel(path, dtype=str).fillna("")
    index = {}
    for _, row in table.iterrows():
        data = {str(key): str(value) for key, value in row.items()}
        digits = data.get("CAS号", "").replace("-", "")
        if digits:
            # normalize
            cas_candidates = find_all_cas(data.get("CAS号", ""))
            for c in cas_candidates:
                if c["valid"]:
                    index[c["canonical"]] = {
                        "catalog": data.get("目录名称", ""),
                        "high_toxic": data.get("是否属于《高毒物品目录》", "") or "知识表未注明",
                    }
    return index


def composition_pages(pdf: Path) -> list[tuple[int, str]]:
    result = []
    for number, page in enumerate(PdfReader(pdf).pages, 1):
        text = page.extract_text() or ""
        heading = text[:1200]
        heading_lower = heading.lower()
        is_section_three = (
            "第3部分" in heading
            or "第 3 部分" in heading
            or "section 3" in heading_lower
            or "composition" in heading_lower
            or "ingredients" in heading_lower
            or "成分" in heading
            or "组成" in heading
            or bool(re.search(r"(?:^|\n)3\.", heading))
        )
        if is_section_three and find_all_cas(text):
            result.append((number, text))
    return result


def content_near(text: str, start: int, raw: str) -> str:
    before = text[max(0, start - 100):start]
    match = re.search(r"(\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?)\s*$", before)
    if match:
        return match.group(1).replace(" ", "")
    after = text[start + len(raw):start + len(raw) + 80]
    match = re.search(r"(\d+(?:\.\d+)?\s*-\s*<?\s*\d+(?:\.\d+)?|<\s*\d+)", after)
    return match.group(1).replace(" ", "") if match else "未可靠提取"


def extract_rows(text: str, knowledge: dict[str, dict[str, str]], page: int) -> list[dict]:
    """从文本中提取 CAS 行，去重聚类"""
    all_cas = find_all_cas(text)
    valid_cas = [c for c in all_cas if c["valid"]]

    # 聚类：距离 < 12 的视为同一组
    clusters: list[list[dict]] = []
    for c in valid_cas:
        if not clusters or c["start"] - clusters[-1][-1]["start"] > 12:
            clusters.append([c])
        else:
            clusters[-1].append(c)

    rows = []
    seen = set()
    for cluster in clusters:
        best = max(cluster, key=lambda c: (c["canonical"] in knowledge, len(content_near(text, c["start"], c["raw"])), len(c["raw"])))
        cas = best["canonical"]
        if cas in seen:
            continue
        seen.add(cas)
        raw = best["raw"]
        content = content_near(text, best["start"], raw)
        matched = knowledge.get(cas)
        if matched:
            high_toxic = matched["high_toxic"]
            action = "必须复核" if high_toxic != "知识表未注明" else "自动通过"
            rows.append({
                "页码": page, "原始 CAS": raw, "规范 CAS": cas, "含量": content,
                "知识表状态": "命中", "目录名称": matched["catalog"],
                "高毒目录": high_toxic, "处理动作": action,
            })
        else:
            rows.append({
                "页码": page, "原始 CAS": raw, "规范 CAS": cas, "含量": content,
                "知识表状态": "当前知识表未命中", "目录名称": "",
                "高毒目录": "知识表未注明", "处理动作": "人工确认",
            })
    return rows


# ============ UI ============

st.set_page_config(page_title="MSDS Agent Demo", page_icon="🧪", layout="wide")
st.title("🧪 可追溯 MSDS 职业危害识别 Agent")
st.caption("公开演示版｜CAS 优先匹配 · 页码证据 · 高风险人工复核")

# ---- 顶部：文件选择 ----
st.markdown("### 第一步：选择文件")

col_btn, col_upload1, col_upload2 = st.columns([1, 2, 2])
with col_btn:
    use_sample = st.button("📋 使用示例文件", type="primary", use_container_width=True,
                           help="加载内置的脱敏 MSDS 样本和知识表，自动运行完整识别流程")
if use_sample:
    st.session_state["use_sample"] = True
    st.session_state["auto_run"] = True

with col_upload1:
    if st.session_state.get("use_sample"):
        st.success("✅ demo_msds.pdf")
        pdf_file = None
    else:
        pdf_file = st.file_uploader("上传 MSDS PDF", type="pdf")
with col_upload2:
    if st.session_state.get("use_sample"):
        st.success("✅ demo_knowledge.xlsx")
        xlsx_file = None
    else:
        xlsx_file = st.file_uploader("上传化学物质名称对应表", type="xlsx")

# 手动上传时也可以点分析
manual_analyze = False
if not st.session_state.get("use_sample") and pdf_file and xlsx_file:
    manual_analyze = st.button("开始分析", type="primary")

should_run = st.session_state.get("auto_run", False) or manual_analyze

if should_run:
    st.session_state["auto_run"] = False

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        if st.session_state.get("use_sample"):
            pdf_path = SAMPLE_DIR / "demo_msds.pdf"
            xlsx_path = SAMPLE_DIR / "demo_knowledge.xlsx"
        else:
            pdf_path = root / "input.pdf"
            xlsx_path = root / "knowledge.xlsx"
            pdf_path.write_bytes(pdf_file.getvalue())
            xlsx_path.write_bytes(xlsx_file.getvalue())

        # ========== 第二步：源文件预览 ==========
        st.markdown("---")
        st.markdown("### 第二步：源文件")

        tab_pdf, tab_knowledge = st.tabs(["📄 MSDS 源文件（PDF）", "📊 化学物质知识表（Excel）"])

        with tab_pdf:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            st.download_button("⬇️ 下载样本 PDF", pdf_bytes, "demo_msds.pdf", "application/pdf")
            try:
                st.pdf(pdf_bytes, width=700)
            except Exception:
                st.info("PDF 预览不可用，请下载查看。")

        with tab_knowledge:
            knowledge_df = pd.read_excel(xlsx_path, dtype=str).fillna("")
            st.dataframe(knowledge_df, use_container_width=True, hide_index=True)

        # ========== 第三步：识别过程 ==========
        st.markdown("---")
        st.markdown("### 第三步：识别过程")

        # Step 1: PDF 文本提取
        with st.expander("Step 1：PDF 文本提取", expanded=True):
            reader = PdfReader(pdf_path)
            st.write(f"PDF 共 **{len(reader.pages)}** 页")
            all_page_texts = []
            for i, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                all_page_texts.append((i, text))
                has_text = "✅ 有文本层" if text.strip() else "❌ 无文本层（需 OCR）"
                st.write(f"第 {i} 页：{has_text}（{len(text)} 字符）")

        # Step 2: 定位成分章节
        with st.expander("Step 2：定位成分/组成章节", expanded=True):
            pages = composition_pages(pdf_path)
            if pages:
                for page_num, text in pages:
                    st.write(f"✅ 第 {page_num} 页命中成分章节（检测到章节标题 + CAS 号）")
            else:
                st.warning("未检测到成分章节，扫描件可能需要 OCR")

        # Step 3: CAS 号识别与校验
        with st.expander("Step 3：CAS 号识别与校验位验证", expanded=True):
            st.markdown("""
            **CAS 号校验规则**：CAS 号最后一位是校验位，计算公式：
            `校验位 = (各位数字 × 权重之和) mod 10`，权重从右向左递增。
            只有通过校验的 CAS 号才会进入后续匹配。
            """)
            all_candidates = []
            for page_num, text in pages:
                for c in find_all_cas(text):
                    all_candidates.append({
                        "页码": page_num,
                        "原始 CAS": c["raw"],
                        "规范 CAS": c["canonical"] if c["valid"] else "—",
                        "校验位": "✅ 通过" if c["valid"] else "❌ 不通过",
                    })
            if all_candidates:
                st.dataframe(pd.DataFrame(all_candidates), use_container_width=True, hide_index=True)
            else:
                st.warning("未找到 CAS 号")

        # Step 4: 知识表匹配
        with st.expander("Step 4：知识表匹配与处理动作判定", expanded=True):
            knowledge = load_knowledge(xlsx_path)
            st.write(f"知识表共 **{len(knowledge)}** 条有效记录")
            match_details = []
            for page_num, text in pages:
                for c in find_all_cas(text):
                    if not c["valid"]:
                        continue
                    cas = c["canonical"]
                    matched = knowledge.get(cas)
                    if matched:
                        match_details.append({
                            "规范 CAS": cas,
                            "知识表": "✅ 命中",
                            "目录名称": matched["catalog"],
                            "高毒目录": matched["high_toxic"],
                            "处理动作": "必须复核" if matched["high_toxic"] != "知识表未注明" else "自动通过",
                        })
                    else:
                        match_details.append({
                            "规范 CAS": cas,
                            "知识表": "⚠️ 未命中",
                            "目录名称": "—",
                            "高毒目录": "—",
                            "处理动作": "人工确认（不等于无危害）",
                        })
            if match_details:
                # deduplicate
                seen = set()
                unique = []
                for m in match_details:
                    if m["规范 CAS"] not in seen:
                        seen.add(m["规范 CAS"])
                        unique.append(m)
                st.dataframe(pd.DataFrame(unique), use_container_width=True, hide_index=True)

        # ========== 第四步：最终输出 ==========
        st.markdown("---")
        st.markdown("### 第四步：最终输出")

        all_rows = []
        for page_num, text in pages:
            all_rows.extend(extract_rows(text, knowledge, page_num))

        if not all_rows:
            st.warning("未形成可靠的文本记录。请使用本地 OCR 版或人工复核；系统不会将其误认为「无危害」。")
        else:
            result = pd.DataFrame(all_rows)

            # 统计摘要
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("识别 CAS 数", len(result))
            col2.metric("命中知识表", len(result[result["知识表状态"] == "命中"]))
            col3.metric("必须复核", len(result[result["处理动作"] == "必须复核"]))
            col4.metric("人工确认", len(result[result["处理动作"] == "人工确认"]))

            st.subheader("识别结果表")
            st.dataframe(result, use_container_width=True, hide_index=True)
            st.download_button("⬇️ 下载 CSV 结果", result.to_csv(index=False).encode("utf-8-sig"),
                             "msds_result.csv", "text/csv")

            st.markdown("**动作说明：** `自动通过` = CAS 命中且未触发高毒规则；`必须复核` = 高毒目录命中；`人工确认` = 当前知识表未命中。")

            # 证据追溯
            st.subheader("📄 成分章节原文（证据追溯）")
            st.caption("以下是从 PDF 中提取的成分章节原文，CAS 号已加粗。每个结果都可追溯到这里。")
            for page_num, text in pages:
                with st.expander(f"第 {page_num} 页原文", expanded=True):
                    # bold CAS numbers
                    highlighted = text
                    for c in find_all_cas(text):
                        if c["valid"]:
                            highlighted = highlighted.replace(c["raw"], f"**{c['raw']}**")
                    st.markdown(highlighted)

            st.subheader("🔍 CAS 号上下文证据")
            for page_num, text in pages:
                for c in find_all_cas(text):
                    if not c["valid"]:
                        continue
                    cas = c["canonical"]
                    ctx_before = text[max(0, c["start"] - 80):c["start"]].replace("\n", " ")
                    ctx_after = text[c["start"] + len(c["raw"]):c["start"] + len(c["raw"]) + 80].replace("\n", " ")
                    matched = knowledge.get(cas)
                    label = f"CAS {cas} — 第 {page_num} 页 — {'✅ 命中' if matched else '⚠️ 未命中'}"
                    with st.expander(label):
                        st.code(f"...{ctx_before}【{c['raw']}】{ctx_after}...", language=None)
                        if matched:
                            st.markdown(f"知识表：{matched['catalog']}｜高毒目录：{matched['high_toxic']}")
                        else:
                            st.markdown("知识表未命中 → 人工确认，不等于无危害")

st.info("本页面不保存上传文件。演示优先覆盖带可读文本层的 MSDS；扫描件和乱码文件会提示进入 OCR / 人工确认流程。")
