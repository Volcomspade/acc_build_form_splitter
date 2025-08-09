import io
import re
import time
import zipfile
from typing import List, Tuple

import pandas as pd
import streamlit as st
import fitz  # PyMuPDF


# ───────────────────────── Streamlit config ─────────────────────────
st.set_page_config(page_title="ACC Build TOC Splitter", layout="wide")


# ───────────────────────── Helpers ─────────────────────────
def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def build_patterns(raw: str) -> List[str]:
    pats = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*")
        pats.append(esc)
    return pats


def detect_toc_pages(doc: fitz.Document) -> List[int]:
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        txt = doc.load_page(i).get_text() or ""
        if entry_rx.search(txt):
            pages.append(i + 1)
    return pages


def parse_toc(doc: fitz.Document, toc_pages: List[int]) -> List[Tuple[str, int]]:
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text() or ""
        for m in toc_rx.finditer(text):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries


def split_ranges(entries: List[Tuple[str, int]], total_pages: int) -> List[Tuple[str, int, int]]:
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out


def extract_meta(doc: fitz.Document) -> Tuple[str, str, str]:
    """
    Extract (template, category, location) from a SINGLE form doc.
    - Template: page 0 'Forms' section row 'Template'
    - Category & Location: the page with 'References and Attachments'
    """
    template = "Unknown"
    category = "Unknown"
    location = "Unknown"

    # --- Template from page 0 ---
    try:
        lines = (doc.load_page(0).get_text() or "").splitlines()
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s.startswith("Template"):
                if ":" in s:
                    v = s.split(":", 1)[1].strip()
                    if v:
                        template = v
                        break
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt and not nxt.endswith(":"):
                    template = nxt
                break
    except Exception:
        pass

    # --- Category & Location from 'References and Attachments' page ---
    try:
        ref_pg = None
        for p in range(doc.page_count):
            txt = doc.load_page(p).get_text() or ""
            if "References and Attachments" in txt:
                ref_pg = p
                break

        if ref_pg is not None:
            rlines = [x.strip() for x in (doc.load_page(ref_pg).get_text() or "").splitlines()]

            def get_val(i: int) -> str:
                ln = rlines[i]
                if ":" in ln:
                    return ln.split(":", 1)[1].strip()
                nxt = rlines[i + 1].strip() if i + 1 < len(rlines) else ""
                if nxt and not nxt.endswith(":"):
                    return nxt
                return ""

            found_cat = False
            found_loc = False
            for i, s in enumerate(rlines):
                if not found_cat and (s == "Category" or s.startswith("Category:")):
                    val = get_val(i)
                    if val:
                        category = val
                        found_cat = True
                if not found_loc and (s == "Location" or s.startswith("Location:")):
                    val = get_val(i)
                    if val:
                        location = val
                        found_loc = True
                if found_cat and found_loc:
                    break
    except Exception:
        pass

    return template, category, location


def build_folder_path_from_loc_cat(location: str, category: str) -> str:
    def segs(s: str):
        parts = [p.strip() for p in s.split(">")] if ">" in s else [s.strip()]
        return [slugify(p) for p in parts if p]

    pieces = segs(location) + segs(category)
    return ("/".join(pieces) + "/") if pieces else ""


# ───────────────────────── Splitting & zipping ─────────────────────────
def create_subzip(
    pdf_bytes: bytes,
    patterns: List[str],
    prefix: str,
    suffix: str,
    remove_id: bool,
    group_by: str,
) -> io.BytesIO:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count
    entries = parse_toc(doc, detect_toc_pages(doc))
    splits = split_ranges(entries, total_pages)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # Build the split document first (we’ll extract meta from this)
            part_doc = fitz.open()
            part_doc.insert_pdf(doc, from_page=start - 1, to_page=end - 1)

            tpl, cat, loc = extract_meta(part_doc)

            # choose zip folder per *this form*
            if group_by == "Location/Category":
                folder = build_folder_path_from_loc_cat(loc, cat)
            elif group_by == "Template":
                folder = (slugify(tpl) + "/") if tpl != "Unknown" else ""
            else:
                folder = ""

            # filename
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)

            zf.writestr(f"{folder}{prefix}{fname}{suffix}.pdf", part_doc.tobytes())
            part_doc.close()

    buf.seek(0)
    return buf


# ───────────────────────── UI ─────────────────────────
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from **filenames only**",
    value=True,
)

group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Location/Category", "Template"],
)

with st.expander("📘 Pattern tips"):
    st.markdown(
        """
- **Exact text**: just type it, e.g. `Checklist`
- **Wildcard `*`**: matches any run of characters, e.g. `03.*_`
- **Combine** with commas: `03.*_, L2_`
"""
    )

if uploads:
    patterns = build_patterns(remove_input)

    # metrics
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(len(parse_toc(d, detect_toc_pages(d))) for d in docs)
    elapsed = time.perf_counter() - t0
    mins, secs = divmod(int(elapsed), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # build master zip
    def build_master_zip() -> io.BytesIO:
        mz_buf = io.BytesIO()
        with zipfile.ZipFile(mz_buf, "w") as mz:
            for b in all_bytes:
                sub = create_subzip(b, patterns, prefix, suffix, remove_id_prefix, group_by)
                with zipfile.ZipFile(sub) as sz:
                    for info in sz.infolist():
                        mz.writestr(info.filename, sz.read(info.filename))
        mz_buf.seek(0)
        return mz_buf

    zip_buf = build_master_zip()
    st.download_button("Download all splits", zip_buf, file_name="acc_build_forms.zip", mime="application/zip")

    # live preview using per-form metadata
    rows = []
    for idx, b in enumerate(all_bytes):
        d = fitz.open(stream=b, filetype="pdf")
        entries = parse_toc(d, detect_toc_pages(d))
        splits = split_ranges(entries, d.page_count)

        for title, start, end in splits:
            part_doc = fitz.open()
            part_doc.insert_pdf(d, from_page=start - 1, to_page=end - 1)
            tpl, cat, loc = extract_meta(part_doc)

            if group_by == "Location/Category":
                folder_disp = f"{loc} > {cat}"
            elif group_by == "Template":
                folder_disp = tpl
            else:
                folder_disp = ""

            file_title = re.sub(r"^#\s*\d+:\s*", "", title) if remove_id_prefix else title
            for rx in patterns:
                file_title = re.sub(rx, "", file_title, flags=re.IGNORECASE)
            fname = slugify(file_title)

            rows.append(
                {
                    "Source PDF": uploads[idx].name,
                    "Folder": folder_disp,
                    "Form Name": title,
                    "Pages": f"{start}-{end}",
                    "Filename": f"{prefix}{fname}{suffix}.pdf",
                }
            )
            part_doc.close()

    st.subheader("Filename & Page-Range Preview")
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
