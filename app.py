import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def sanitize_folder_component(name: str) -> str:
    # allow spaces but strip dangerous characters
    return re.sub(r'[\\/:*?"<>|]', '', name).strip()

def build_patterns(raw_input: str):
    """
    Turn comma-separated tokens into escaped regex patterns, treating '*' as wildcard.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        # restore wildcard semantics for '*' in the user's input
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats

# ─── PDF PARSING ───────────────────────────────────────────────────────────────

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i + 1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = reader.pages[pg - 1].extract_text() or ""
        for m in toc_rx.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries

def split_ranges(entries, total_pages):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out

def extract_location_and_category(reader, start, end):
    """
    Within the pages for a form (start..end), find the "References and Attachments"
    section and extract Location and Category. Returns (location, category) or (None,None).
    """
    accumulating = ""
    for p in range(start - 1, end):
        page_text = reader.pages[p].extract_text() or ""
        accumulating += "\n" + page_text

        if "References and Attachments" in accumulating:
            # look in a window after the header, because Location/Category appear below
            idx = accumulating.find("References and Attachments")
            window = accumulating[idx: idx + 2500]  # generous slice
            loc_match = re.search(r"Location\s+([^\n\r]+)", window)
            cat_match = re.search(r"Category\s+([^\n\r]+)", window)
            if loc_match and cat_match:
                location = loc_match.group(1).strip()
                category = cat_match.group(1).strip()
                return location, category
    return None, None

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id_prefix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # Form name kept as-is (with ID) for display; filename may strip it
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title

            # apply removal patterns
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)

            fname = slugify(name_for_filename)

            # Determine folder from References & Attachments block
            loc, cat = extract_location_and_category(reader, start, end)
            if loc and cat:
                folder_path = f"{sanitize_folder_component(loc)}/{sanitize_folder_component(cat)}"
            else:
                folder_path = "Unknown"

            writer = PdfWriter()
            for p in range(start - 1, end):
                # defensive: ensure page index in range
                if 0 <= p < len(reader.pages):
                    writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            # Use folder hierarchy inside zip
            zip_name = f"{folder_path}/{prefix}{fname}{suffix}.pdf"
            zf.writestr(zip_name, part.read())

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames",
    value=True,
)
patterns = build_patterns(remove_input)

# Regex helper collapsible
with st.expander("🛈 Regex & wildcard tips", expanded=False):
    st.markdown(
        """
- Use commas to separate multiple patterns.
- `*` is a wildcard (e.g. `03.*` removes `03.04`, `03.02`, etc.).
- To match literal dots or underscores, just include them (`03\\.04_`).
- Patterns are applied case-insensitively.
- Examples:
  - `03.*` removes any `03.`-prefixed segment.
  - `_L2_` removes literal `_L2_`.
  - `0?\\.0?` is **not** a wildcard pattern; use `0.` or `03\\.` etc., or simply `03\\.04`.
"""
    )

if uploads:
    # read all PDFs once
    all_bytes = [f.read() for f in uploads]

    # build master ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b, file in zip(all_bytes, uploads):
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    # preserve folder hierarchy from inner zip
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # live preview
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        reader = PdfReader(io.BytesIO(b))
        total_pages = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages)

        for title, start, end in splits:
            # name in filename (with/without ID)
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)

            # folder for display
            loc, cat = extract_location_and_category(reader, start, end)
            if loc and cat:
                folder_display = f"{loc} > {cat}"
            else:
                folder_display = "Unknown"

            rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name": title,
                "Pages": f"{start}-{end}",
                "Folder": folder_display,
                "Filename": f"{prefix}{fname}{suffix}.pdf",
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
