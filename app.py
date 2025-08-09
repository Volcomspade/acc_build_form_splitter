import io
import re
import time
import zipfile
from typing import List, Tuple

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st


# ───────────────────────────── Streamlit config ───────────────────────────── #

st.set_page_config(page_title="ACC Build TOC Splitter", layout="wide")
st.title("ACC Build TOC Splitter")


# ─────────────────────── Helpers: filenames & patterns ─────────────────────── #

def slugify(name: str) -> str:
    """Sanitize a string for use as a filename."""
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def build_patterns(raw: str) -> List[str]:
    """
    Turn comma-separated tokens into regex patterns, with * treated as wildcard.
    Example: "03.*_, L2_"  ->  ["03\\..*_", "L2_"]
    """
    pats: List[str] = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok).replace(r"\*", ".*")
        pats.append(esc)
    return pats


def apply_patterns_to_filename(title: str, patterns: List[str], remove_id: bool) -> str:
    """
    Apply user remove-patterns BOTH before and after slugify so inputs like
    '03.*_' or 'L2_' work whether the text has spaces or underscores.
    """
    base = re.sub(r"^#\s*\d+:\s*", "", title) if remove_id else title

    # Pass 1: operate on the raw title (with spaces)
    for rx in patterns:
        base = re.sub(rx, "", base, flags=re.IGNORECASE)

    # Slugify (turn spaces to underscores, clean punctuation)
    fname = slugify(base)

    # Pass 2: operate again after slugify (with underscores)
    for rx in patterns:
        fname = re.sub(rx, "", fname, flags=re.IGNORECASE)

    # Tidy up
    fname = re.sub(r"_+", "_", fname).strip("_")
    return fname


# ───────────────────── PDF parsing: TOC & metadata (fitz) ─────────────────── #

def page_text(doc: fitz.Document, i: int) -> str:
    # "text" is fast and robust for these reports
    return doc.load_page(i).get_text("text") or ""


def detect_toc_pages(doc: fitz.Document) -> List[int]:
    """Return 1-based page numbers that contain TOC entries like '# 6849:'."""
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages: List[int] = []
    for i in range(doc.page_count):
        if entry_rx.search(page_text(doc, i)):
            pages.append(i + 1)
    return pages


def parse_toc(doc: fitz.Document, toc_pages: List[int]) -> List[Tuple[str, int]]:
    """
    Parse lines like:
        #6849: ACC/DCC-D4.1 ... ................. 4
    Returns [(title, start_page), ...]
    """
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries: List[Tuple[str, int]] = []
    for pg in toc_pages:
        txt = page_text(doc, pg - 1)
        for m in toc_rx.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    # Keep natural order
    entries.sort(key=lambda x: x[1])
    return entries


def split_ranges(entries: List[Tuple[str, int]], total_pages: int) -> List[Tuple[str, int, int]]:
    """
    Given [(title, start), ...], compute [(title, start, end), ...].
    """
    out: List[Tuple[str, int, int]] = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out


def extract_meta(doc: fitz.Document) -> Tuple[str, str, str]:
    """
    Extract (template, category, location).
      - Template: from page 0 "Forms" table (line containing 'Template')
      - Category/Location: from 'References and Attachments' section (pages 1+)
    Robust to either "Label\nValue" or "Label: Value" formats.
    """
    def pick_value(lines: List[str], label: str) -> str:
        lab = label.lower()
        for idx, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            # 'Label: Value'
            if s.lower().startswith(lab + ":"):
                return s.split(":", 1)[1].strip()
            # row label then next line contains value
            if s.lower() == lab and idx + 1 < len(lines):
                nxt = lines[idx + 1].strip()
                if nxt and nxt.lower() != lab:
                    return nxt
        return "Unknown"

    # --- Page 0: Template ---
    t0 = page_text(doc, 0)
    lines0 = t0.splitlines()
    template = pick_value(lines0, "Template")

    # Sometimes 'Template' is on a line, and the next line is the value:
    if template == "Unknown":
        for i, s in enumerate(lines0):
            if s.strip().lower() == "template" and i + 1 < len(lines0):
                val = lines0[i + 1].strip()
                if val:
                    template = val
                    break

    # --- Pages 1+: References & Attachments area ---
    category = "Unknown"
    location = "Unknown"
    found_ref = False

    for i in range(1, doc.page_count):
        txt = page_text(doc, i)
        lines = txt.splitlines()

        if not found_ref:
            for s in lines:
                if "References and Attachments" in s:
                    found_ref = True
                    break
            if not found_ref:
                continue  # keep scanning pages

        # Once in R&A region, try to grab Category/Location
        if category == "Unknown":
            category = pick_value(lines, "Category")
        if location == "Unknown":
            location = pick_value(lines, "Location")

        if category != "Unknown" and location != "Unknown":
            break

    # Normalize
    template = template if template else "Unknown"
    category = category if category else "Unknown"
    location = location if location else "Unknown"
    return template, category, location


# ───────────────────────────── Zipping / Splitting ─────────────────────────── #

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

    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total_pages)

    tpl, cat, loc = extract_meta(doc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # folder path inside zip
            if group_by == "Location/Category":
                # Folder separator must be '/', keep the '>' tokens in names
                folder = f"{loc}/{cat}/" if loc != "Unknown" or cat != "Unknown" else ""
            elif group_by == "Template":
                folder = f"{tpl}/" if tpl != "Unknown" else ""
            else:
                folder = ""

            # Build filename with robust pattern stripping
            fname = apply_patterns_to_filename(title, patterns, remove_id)
            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # Assemble the pages
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            part_bytes = new_doc.write()
            new_doc.close()

            zf.writestr(out_name, part_bytes)

    buf.seek(0)
    return buf


# ──────────────────────────────── UI Controls ──────────────────────────────── #

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only",
    value=True,
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Location/Category", "Template"],
)

with st.expander("📎 Pattern tips", expanded=False):
    st.markdown(
        """
- **Exact text**: just type it, e.g. `Checklist`  
- **Wildcard `*`**: matches any run of characters, e.g. `03.*_` removes `03.04_`, `03.03_`, etc.  
- **Combine** with commas: `03.*_, L2_`  
- Patterns apply only to the saved filenames (the **Form Name** column always shows the original TOC title).
        """
    )

if uploads:
    patterns = build_patterns(remove_input)

    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    total_pages = sum(d.page_count for d in docs)
    total_forms = 0
    for d in docs:
        total_forms += len(parse_toc(d, detect_toc_pages(d)))
    elapsed = time.perf_counter() - t0
    mins, secs = divmod(int(elapsed), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # Build master ZIP (kept the same behavior)
    def get_master_zip() -> io.BytesIO:
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as zmaster:
            for b in all_bytes:
                subzip = create_subzip(
                    b, patterns, prefix, suffix, remove_id_prefix, group_by
                )
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        zmaster.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    st.download_button(
        "Download all splits",
        get_master_zip(),
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # Live preview (uses the same filename logic)
    preview_rows = []
    for idx, (b, d) in enumerate(zip(all_bytes, docs)):
        tpl, cat, loc = extract_meta(d)
        splits = split_ranges(parse_toc(d, detect_toc_pages(d)), d.page_count)

        for title, start, end in splits:
            if group_by == "Location/Category":
                # Human-friendly display uses ' > ' between the two sections
                folder_disp = (
                    f"{loc} > {cat}" if (loc != "Unknown" or cat != "Unknown") else ""
                )
            elif group_by == "Template":
                folder_disp = tpl if tpl != "Unknown" else ""
            else:
                folder_disp = ""

            fn_core = apply_patterns_to_filename(title, patterns, remove_id_prefix)
            fn = f"{prefix}{fn_core}{suffix}.pdf"

            preview_rows.append(
                {
                    "Source PDF": uploads[idx].name,
                    "Folder": folder_disp,
                    "Form Name": title,  # keep original TOC title for review
                    "Pages": f"{start}-{end}",
                    "Filename": fn,
                }
            )

    df = pd.DataFrame(preview_rows)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)
