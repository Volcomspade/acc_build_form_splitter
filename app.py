# app.py
# ACC Build TOC Splitter — PyMuPDF version
# Streamlit + PyMuPDF (fitz)
# - Splits ACC Build "Form detail report" PDFs using the TOC
# - Optional grouping by Template or Location/Category
# - Friendly filename pattern removal (wildcards)

import io
import re
import time
import zipfile
from typing import List, Tuple

import streamlit as st
import pandas as pd
import fitz  # PyMuPDF


# ───────────────────────────────────────────────────────────────────────────────
# Streamlit config
# ───────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="ACC Build TOC Splitter", layout="wide")
st.title("ACC Build TOC Splitter")


# ───────────────────────────────────────────────────────────────────────────────
# Helpers: text/regex/filenames
# ───────────────────────────────────────────────────────────────────────────────
INVALID_FS = r'[\\/:*?"<>|]'


def slugify(name: str) -> str:
    s = re.sub(INVALID_FS, "", name)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def safe_segment(name: str) -> str:
    """
    Create a folder-safe path segment (keeps spaces, strips illegal chars).
    """
    s = re.sub(INVALID_FS, "", name or "")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or "Unknown"


def build_patterns(raw_input: str):
    """
    User-friendly wildcard patterns → compiled regexes.

    - ',' or newlines separate patterns.
    - '*'  -> non-greedy match ('.*?')
    - '_'  -> matches underscore OR spaces ('[_\\s]+')
    - Case-insensitive.
    """
    pats = []
    if not raw_input:
        return pats

    for tok in re.split(r"[,\n]+", raw_input):
        tok = tok.strip()
        if not tok:
            continue

        esc = re.escape(tok)              # escape first
        esc = esc.replace(r"\*", ".*?")   # wildcard non-greedy
        esc = esc.replace("_", r"[_\s]+") # underscore == underscore OR spaces
        pats.append(re.compile(esc, re.IGNORECASE))
    return pats


def clean_filename(base: str, patterns, remove_id: bool = False, collapse: bool = True) -> str:
    """
    Apply patterns before and after slugify so tokens are removed reliably.
    """
    if remove_id:
        base = re.sub(r"^#\s*\d+:\s*", "", base)

    # pass 1: pre-slugify
    for rx in patterns:
        base = rx.sub("", base)

    fname = slugify(base)

    # pass 2: post-slugify
    for rx in patterns:
        fname = rx.sub("", fname)

    if collapse:
        fname = re.sub(r"[_ ]{2,}", "_", fname).strip("_ ")
    return fname


# ───────────────────────────────────────────────────────────────────────────────
# PDF parsing (PyMuPDF)
# ───────────────────────────────────────────────────────────────────────────────
def detect_toc_pages(doc: fitz.Document) -> List[int]:
    """
    Find pages that contain lines like '# 6849: Title ..... 12'
    Return 1-based page numbers to align with the 'start page' numbers.
    """
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        txt = doc.load_page(i).get_text()
        if entry_rx.search(txt):
            pages.append(i + 1)
    return pages


def parse_toc(doc: fitz.Document, toc_pages: List[int]) -> List[Tuple[str, int]]:
    """
    Parse TOC lines into (title, start_page).
    Matches: '# 6849: Some title .... 123'
    """
    rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = doc.load_page(pg - 1).get_text() or ""
        for m in rx.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries


def split_ranges(entries: List[Tuple[str, int]], total_pages: int) -> List[Tuple[str, int, int]]:
    """
    Convert (title, start) into (title, start, end).
    """
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out


def find_references_page(doc: fitz.Document) -> int:
    """
    Try to locate the 'References and Attachments' page, return index (0-based),
    or -1 if not found.
    """
    key = "References and Attachments"
    for i in range(min(doc.page_count, 20)):  # shouldn't be far in
        if key.lower() in (doc.load_page(i).get_text() or "").lower():
            return i
    return -1


def extract_meta(doc: fitz.Document) -> Tuple[str, str, str]:
    """
    Extract Template (from first page 'Forms' table),
    and Location / Category (from References & Attachments → Assets block).
    Fallbacks to 'Unknown' if not found.
    """
    template = "Unknown"
    location = "Unknown"
    category = "Unknown"

    # --- Template: page 0 (Forms table)
    try:
        txt0 = doc.load_page(0).get_text() or ""
        # Strong match ("Template: <value>")
        m = re.search(r"(?im)^\s*Template\s*[:\-]\s*(.+?)\s*$", txt0)
        if m:
            template = m.group(1).strip()
        else:
            # Looser fallback: a line with 'Exhibit' and 'Checklist' often follows
            # 'Template:' row – grab the first such line
            for line in txt0.splitlines():
                if "exhibit" in line.lower() and "checklist" in line.lower():
                    template = line.strip()
                    break
    except Exception:
        pass

    # --- Location & Category: look near References & Attachments
    try:
        ref_pg = find_references_page(doc)
        search_pages = range(ref_pg, min(ref_pg + 3, doc.page_count)) if ref_pg >= 0 else range(0, min(6, doc.page_count))

        for i in search_pages:
            t = doc.load_page(i).get_text() or ""

            # Grab the first good hit of each; continue scanning until we have both
            if location == "Unknown":
                m_loc = re.search(r"(?im)^\s*Location\s*[:\-]\s*(.+?)\s*$", t)
                if m_loc:
                    candidate = m_loc.group(1).strip()
                    if "Form detail report report is split" not in candidate:
                        location = candidate

            if category == "Unknown":
                m_cat = re.search(r"(?im)^\s*Category\s*[:\-]\s*(.+?)\s*$", t)
                if m_cat:
                    category = m_cat.group(1).strip()

            if location != "Unknown" and category != "Unknown":
                break
    except Exception:
        pass

    return template, category, location


# ───────────────────────────────────────────────────────────────────────────────
# Splitting/zipping
# ───────────────────────────────────────────────────────────────────────────────
def create_subzip(
    pdf_bytes: bytes,
    patterns,
    prefix: str,
    suffix: str,
    remove_id_prefix: bool,
    group_by: str,
) -> io.BytesIO:
    """
    Split a single ACC PDF into form PDFs and return a ZIP buffer.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count

    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total_pages)

    tpl, cat, loc = extract_meta(doc)

    # choose folder per file basis
    def folder_path() -> str:
        if group_by == "Location/Category":
            return f"{safe_segment(loc)}/{safe_segment(cat)}/"
        elif group_by == "Template":
            return f"{safe_segment(tpl)}/"
        return ""

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        fld = folder_path()  # computed once per source PDF
        for title, start, end in splits:
            fname = clean_filename(title, patterns, remove_id=remove_id_prefix)
            out_name = f"{fld}{prefix}{fname}{suffix}.pdf"

            part = fitz.open()
            part.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            zf.writestr(out_name, part.write())

    zbuf.seek(0)
    return zbuf


# ───────────────────────────────────────────────────────────────────────────────
# UI
# ───────────────────────────────────────────────────────────────────────────────
uploads = st.file_uploader(
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only", value=True
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Template", "Location/Category"],
)

with st.expander("🧩 Pattern tips"):
    st.markdown(
        """
**How this box works**

- Separate multiple patterns with commas **or** new lines.
- `*` matches any characters (non-greedy).  
  Example: `0*.0*_` removes `03.04_`, `12.99_`, etc.
- `_` matches underscores **or spaces**.  
  Example: `L2_` removes both `L2_` and `L2 ` (space).
- We apply the removals both **before** and **after** slugify,
  so they work reliably.

**Examples**


or on one line:

`0*.0*_, L2_`
        """
    )

if uploads:
    patterns = build_patterns(remove_input)

    # --- read once for metrics
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]

    total_pages = sum(d.page_count for d in docs)
    total_forms = 0
    for d in docs:
        total_forms += len(parse_toc(d, detect_toc_pages(d)))

    elapsed = int(time.perf_counter() - t0)
    mins, secs = divmod(elapsed, 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # --- build master ZIP (on click)
    def get_zip() -> io.BytesIO:
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as master:
            for b in all_bytes:
                sub = create_subzip(b, patterns, prefix, suffix, remove_id_prefix, group_by)
                with zipfile.ZipFile(sub) as sz:
                    for info in sz.infolist():
                        master.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = get_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # --- live preview table
    rows = []
    for idx, (b, d) in enumerate(zip(all_bytes, docs)):
        tpl, cat, loc = extract_meta(d)
        splits = split_ranges(parse_toc(d, detect_toc_pages(d)), d.page_count)

        if group_by == "Template":
            folder = safe_segment(tpl)
        elif group_by == "Location/Category":
            folder = f"{safe_segment(loc)} / {safe_segment(cat)}"
        else:
            folder = ""

        for title, start, end in splits:
            fname = clean_filename(title, patterns, remove_id=remove_id_prefix)
            rows.append(
                {
                    "Source PDF": uploads[idx].name,
                    "Folder": folder,
                    "Form Name": title,
                    "Pages": f"{start}-{end}",
                    "Filename": f"{prefix}{fname}{suffix}.pdf",
                }
            )

    st.subheader("Filename & Page-Range Preview")
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
