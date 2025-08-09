import io
import re
import time
import zipfile
from typing import List, Tuple, Dict

import pandas as pd
import streamlit as st
import fitz  # PyMuPDF


# ──────────────────────────────────────────────────────────────────────────────
# STREAMLIT CONFIG
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="ACC Build TOC Splitter", layout="wide")
st.title("ACC Build TOC Splitter")


# ──────────────────────────────────────────────────────────────────────────────
# TEXT / REGEX HELPERS
# ──────────────────────────────────────────────────────────────────────────────
NBSP = "\xa0"

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace(NBSP, " ")
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"[ \t]+", " ", s)
    # collapse stray spaces around newlines
    s = re.sub(r"\s+\n", "\n", s)
    s = re.sub(r"\n\s+", "\n", s)
    return s


def slugify(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    s = s.strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def slugify_path_segment(seg: str) -> str:
    # keep it readable but safe
    seg = seg.strip()
    seg = re.sub(r'[\\/:*?"<>|]', "", seg)
    seg = re.sub(r"\s+", " ", seg).strip()
    return seg


def build_patterns(raw: str) -> List[str]:
    """
    Turn comma-separated user tokens into regex patterns.
    - Escape everything first
    - Convert '*' to NON-GREEDY '.*?' so it doesn't eat the whole name
    """
    pats: List[str] = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*?")
        pats.append(esc)
    return pats


def apply_patterns(s: str, patterns: List[str]) -> str:
    for rx in patterns:
        s = re.sub(rx, "", s, flags=re.IGNORECASE)
    return s


def split_breadcrumbs(s: str) -> List[str]:
    # split on '>' or '/' while keeping clean pieces
    parts = [p.strip() for p in re.split(r"[>/]", s) if p.strip()]
    return parts


# ──────────────────────────────────────────────────────────────────────────────
# TOC PARSING
# ──────────────────────────────────────────────────────────────────────────────
TOC_PAGE_RX = re.compile(r"^#\s*\d+:", re.MULTILINE)
TOC_ENTRY_RX = re.compile(r"#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)", re.MULTILINE)

def detect_toc_pages(doc: fitz.Document) -> List[int]:
    pages = []
    for i in range(doc.page_count):
        txt = normalize_text(doc.load_page(i).get_text())
        if TOC_PAGE_RX.search(txt):
            pages.append(i + 1)  # 1-based
    return pages


def parse_toc(doc: fitz.Document, toc_pages: List[int]) -> List[Tuple[str, int]]:
    entries: List[Tuple[str, int]] = []
    for pg in toc_pages:
        txt = normalize_text(doc.load_page(pg - 1).get_text())
        for m in TOC_ENTRY_RX.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries


def split_ranges(entries: List[Tuple[str, int]], total_pages: int) -> List[Tuple[str, int, int]]:
    out: List[Tuple[str, int, int]] = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# PER-SPLIT METADATA (Template / Location / Category)
# ──────────────────────────────────────────────────────────────────────────────
def extract_template_for_split(doc: fitz.Document, start_page: int) -> str:
    """
    Look on the first page of the form split only.
    Accept:
      - 'Template: ...'
      - A standalone 'Template' header followed by the next non-empty line.
    """
    p = max(0, start_page - 1)
    txt = normalize_text(doc.load_page(p).get_text())
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]

    for i, ln in enumerate(lines):
        if re.match(r"^Template\b", ln, flags=re.IGNORECASE):
            if ":" in ln:
                val = ln.split(":", 1)[1].strip()
                if val:
                    return val
            # header style → next non-empty line
            for j in range(i + 1, len(lines)):
                nxt = lines[j].strip()
                if nxt:
                    return nxt
            break

    # fallback: first occurrence on the page
    m = re.search(r"\bTemplate\s*[:\-]?\s*(.+)", txt, flags=re.IGNORECASE)
    if m:
        return m.group(1).splitlines()[0].strip()

    return "Unknown Template"


def extract_loc_cat_for_split(doc: fitz.Document, start_page: int, end_page: int) -> Tuple[str, str]:
    """
    Search within the split for a 'References and Attachments' or 'Assets' page,
    then read anchored lines that begin with 'Location' and 'Category'.
    If not found, fall back to any page in the split with those anchors.
    """
    location = None
    category = None

    # pass 1: prefer pages that look like “References and Attachments”
    for p in range(start_page - 1, end_page):
        txt = normalize_text(doc.load_page(p).get_text())
        if "References and Attachments" in txt or "Assets (" in txt or "References" in txt and "Attachments" in txt:
            for ln in txt.splitlines():
                s = ln.strip()
                m_loc = re.match(r"^Location\s+(.+)$", s, flags=re.IGNORECASE)
                if m_loc:
                    location = m_loc.group(1).strip()
                m_cat = re.match(r"^Category\s+(.+)$", s, flags=re.IGNORECASE)
                if m_cat:
                    category = m_cat.group(1).strip()
            if location or category:
                break

    # pass 2: plain anchored scan within split
    if not location or not category:
        for p in range(start_page - 1, end_page):
            if location and category:
                break
            txt = normalize_text(doc.load_page(p).get_text())
            for ln in txt.splitlines():
                s = ln.strip()
                if not location:
                    m_loc = re.match(r"^Location\s+(.+)$", s, flags=re.IGNORECASE)
                    if m_loc:
                        location = m_loc.group(1).strip()
                if not category:
                    m_cat = re.match(r"^Category\s+(.+)$", s, flags=re.IGNORECASE)
                    if m_cat:
                        category = m_cat.group(1).strip()
            if location and category:
                break

    if not location:
        location = "Unknown Location"
    if not category:
        category = "Unknown Category"

    return location, category


# ──────────────────────────────────────────────────────────────────────────────
# SPLIT + ZIP (using precomputed splits/meta)
# ──────────────────────────────────────────────────────────────────────────────
def write_zip_for_docs(
    docs_info: List[Dict],
    patterns: List[str],
    prefix: str,
    suffix: str,
    remove_id_for_filenames: bool,
    group_by: str,
) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for info in docs_info:
            pdf_bytes = info["bytes"]
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            try:
                for sp in info["splits"]:
                    title = sp["title"]
                    start = sp["start"]
                    end = sp["end"]
                    template = sp["template"]
                    location = sp["location"]
                    category = sp["category"]

                    # Folder path
                    if group_by == "Location/Category":
                        loc_parts = split_breadcrumbs(location)
                        cat_parts = split_breadcrumbs(category)
                        segments = [slugify_path_segment(p) for p in (loc_parts + cat_parts)]
                        folder = "/".join(segments) + "/" if segments else ""
                    elif group_by == "Template":
                        folder = slugify_path_segment(template) + "/"
                    else:
                        folder = ""

                    # Filename logic (ID removal only affects filename)
                    base = title
                    if remove_id_for_filenames:
                        base = re.sub(r"^#\s*\d+:\s*", "", base)

                    # 1) remove patterns on raw
                    base = apply_patterns(base, patterns)
                    # 2) slugify
                    fname = slugify(base)
                    # 3) remove patterns again on slug (lets users target underscores etc.)
                    fname = apply_patterns(fname, patterns)
                    # 4) cleanup
                    fname = re.sub(r"_+", "_", fname).strip("_")

                    out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

                    # Assemble split pages
                    part_doc = fitz.open()
                    for p in range(start - 1, end):
                        part_doc.insert_pdf(doc, from_page=p, to_page=p)
                    part_bytes = part_doc.write()
                    part_doc.close()

                    zf.writestr(out_name, part_bytes)
            finally:
                doc.close()

    buf.seek(0)
    return buf


# ──────────────────────────────────────────────────────────────────────────────
# UI CONTROLS
# ──────────────────────────────────────────────────────────────────────────────
uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)

remove_input = st.text_input("Remove patterns (* = wildcard, non-greedy)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only",
    value=True,
)
group_by = st.selectbox(
    "Group files in ZIP by",
    ["None", "Location/Category", "Template"],
    index=1  # default to Location/Category like your earlier runs
)

with st.expander("ℹ️ Regex & wildcard tips"):
    st.markdown(
        """
- **Exact text**: type it, e.g. `Checklist`
- **Wildcard `*`** = any run of characters, **non-greedy** here:
  - `03.*_` removes `03.04_`, `03.03_`, etc.
  - `0*.0*_` removes things like `03.04_`, `02.03_`
- **Underscore note**: we remove patterns **before and after** slugify, so:
  - `L2` removes both `"L2 "` and `"L2_"`
  - `L2_` specifically removes the underscore version in the final filename
- **Combine** with commas: `03.*_, L2_`
"""
    )


# ──────────────────────────────────────────────────────────────────────────────
# PIPELINE (read → split → meta → preview → zip)
# ──────────────────────────────────────────────────────────────────────────────
if uploads:
    patterns = build_patterns(remove_input)

    t0 = time.perf_counter()
    progress = st.progress(0, text="Reading PDFs…")
    step = 0

    docs_info: List[Dict] = []
    total_pages = 0
    total_forms = 0

    for f in uploads:
        b = f.read()
        doc = fitz.open(stream=b, filetype="pdf")
        try:
            total_pages += doc.page_count

            toc_pages = detect_toc_pages(doc)
            entries = parse_toc(doc, toc_pages)
            splits = split_ranges(entries, doc.page_count)
            total_forms += len(splits)

            # per-split metadata
            split_rows = []
            for (title, start, end) in splits:
                template = extract_template_for_split(doc, start)
                location, category = extract_loc_cat_for_split(doc, start, end)
                split_rows.append(
                    {
                        "title": title,
                        "start": start,
                        "end": end,
                        "template": template,
                        "location": location,
                        "category": category,
                    }
                )

            docs_info.append(
                {"name": f.name, "bytes": b, "splits": split_rows}
            )
        finally:
            doc.close()

        step += 1
        progress.progress(step / len(uploads), text=f"Processed {step}/{len(uploads)}")

    # PREVIEW READY TIMER
    t1 = time.perf_counter()
    elapsed = t1 - t0
    mins, secs = divmod(int(elapsed), 60)

    # METRICS
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Preview ready", f"{mins:02d}:{secs:02d}")

    # LIVE PREVIEW TABLE
    st.subheader("Filename & Page-Range Preview")
    preview_rows = []
    for info in docs_info:
        for sp in info["splits"]:
            title = sp["title"]                 # display keeps ID
            start, end = sp["start"], sp["end"]
            template = sp["template"]
            location = sp["location"]
            category = sp["category"]

            # Where it will go (human string)
            if group_by == "Location/Category":
                folder_display = " > ".join(split_breadcrumbs(location) + split_breadcrumbs(category))
            elif group_by == "Template":
                folder_display = template
            else:
                folder_display = ""

            # Filename build (ID removal affects filename only)
            base = sp["title"]
            if remove_id_prefix:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            base = apply_patterns(base, patterns)
            fname = slugify(base)
            fname = apply_patterns(fname, patterns)
            fname = re.sub(r"_+", "_", fname).strip("_")
            final_name = f"{prefix}{fname}{suffix}.pdf"

            preview_rows.append(
                {
                    "Source PDF": info["name"],
                    "Folder": folder_display,
                    "Form Name": title,
                    "Pages": f"{start}-{end}",
                    "Filename": final_name,
                }
            )

    df = pd.DataFrame(preview_rows)
    st.dataframe(df, use_container_width=True)

    # DOWNLOAD
    st.divider()
    st.write("When you click download, the ZIP is assembled with the same previewed folder structure.")
    zip_buf = write_zip_for_docs(
        docs_info=docs_info,
        patterns=patterns,
        prefix=prefix,
        suffix=suffix,
        remove_id_for_filenames=remove_id_prefix,
        group_by=group_by,
    )
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )
