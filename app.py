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

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i + 1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]


def parse_toc(reader, toc_pages):
    """
    Parse the TOC entries like:
    #6849: ACC/DCC-D4.1 (P23459AD0003): 03.04 Exhibit H-3 - Exhibit H-4 ACC Build ..................................... 4
    """
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


def slugify(name):
    # sanitize for filename
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')


def build_patterns(raw_input):
    """
    Turn comma-separated tokens into regex patterns, with '*' -> '.*' and '?' -> '.'
    If user wants literal regex they can supply it directly, but * and ? are translated.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        placeholder = tok.replace('*', '__STAR__').replace('?', '__QMARK__')
        esc = re.escape(placeholder)
        esc = esc.replace('__STAR__', '.*').replace('__QMARK__', '.')
        pats.append(esc)
    return pats


def make_folder_path(folder_str):
    """
    Turn something like "BESS Yard > Feeder 12A > Electrical > BESS Assembly"
    into nested zip path: "BESS_Yard/Feeder_12A/Electrical/BESS_Assembly"
    """
    if not folder_str:
        return "Unknown"
    parts = []
    for part in folder_str.split(">"):
        p = part.strip()
        if not p:
            continue
        clean = re.sub(r'[\\/:*?"<>|]', '', p)
        clean = re.sub(r'\s+', '_', clean)
        clean = re.sub(r'_+', '_', clean).strip('_')
        parts.append(clean)
    return "/".join(parts) if parts else "Unknown"


def extract_folder_metadata(reader, start, end):
    """
    Look inside the form's pages (from start to end) for the "References and Attachments"
    block and extract Location and Category to compose "Location > Category".
    Fallback to whichever is found; if none, return "Unknown".
    """
    search_text = ""
    # scan first few pages of the split to find the block quickly
    for pg in range(start - 1, min(start - 1 + 5, end)):
        search_text += (reader.pages[pg].extract_text() or "") + "\n"
        if "References and Attachments" in search_text:
            break
    else:
        for pg in range(start - 1, end):
            search_text += (reader.pages[pg].extract_text() or "") + "\n"
            if "References and Attachments" in search_text:
                break

    if "References and Attachments" in search_text:
        idx = search_text.index("References and Attachments")
        snippet = search_text[idx : idx + 1000]
    else:
        snippet = search_text[:1000]

    loc = None
    cat = None
    m_cat = re.search(r'Category\s*[:]?[\s]*([^\n\r]+)', snippet, re.IGNORECASE)
    m_loc = re.search(r'Location\s*[:]?[\s]*([^\n\r]+)', snippet, re.IGNORECASE)

    if m_loc:
        loc = m_loc.group(1).strip()
    if m_cat:
        cat = m_cat.group(1).strip()

    if loc and cat:
        return f"{loc} > {cat}"
    if loc:
        return loc
    if cat:
        return cat
    return "Unknown"


# ─── CORE ZIP CREATION ────────────────────────────────────────────────────────

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id_prefix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            filename_title = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            name_for_filename = filename_title
            for rx in patterns:
                name_for_filename = re.sub(rx, "", name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)
            zip_fname = f"{prefix}{fname}{suffix}.pdf"

            folder_hierarchy = extract_folder_metadata(reader, start, end)
            folder_path = make_folder_path(folder_hierarchy)

            writer = PdfWriter()
            for p in range(start - 1, end):
                if 0 <= p < len(reader.pages):
                    writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            entry_name = f"{folder_path}/{zip_fname}"
            zf.writestr(entry_name, part.read())

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
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames", value=True
)

with st.expander("ℹ️ Regex & wildcard tips", expanded=False):
    st.markdown(
        """
**How to use the removal patterns:**
- Multiple patterns can be comma-separated.
- Use `*` to mean any sequence of characters. Example: `03.0*` will match `03.04`, `03.02`, etc.
- Use `?` to match a single character. Example: `0?.0?` matches `03.04`, `02.03`, etc.
- You can mix literal regex if you want more control (e.g., `0[23]\.0[23]_` to remove both `03.04_` and `02.03_`).
- Patterns are applied case-insensitively.
- Examples:
  - To drop version-like prefixes such as `03.04_`, `02.03_`: `0?.0?_`
  - To remove `L2_`: `L2_`
  - Combine: `0?.0?_, L2_`
"""
    )

if uploads:
    all_bytes = [f.read() for f in uploads]

    # Build preview with progress
    st.subheader("Summary")
    total_forms = 0
    total_split_pages = 0
    original_pdf_pages = 0
    preview_rows = []
    progress = st.progress(0)
    for idx, b in enumerate(all_bytes):
        reader = PdfReader(io.BytesIO(b))
        total_pages = len(reader.pages)
        original_pdf_pages += total_pages

        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages)

        for title, start, end in splits:
            total_forms += 1
            total_split_pages += (end - start + 1)

            form_name = title
            filename_title = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            patterns = build_patterns(remove_input)
            name_for_filename = filename_title
            for rx in patterns:
                name_for_filename = re.sub(rx, "", name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)
            zip_fname = f"{prefix}{fname}{suffix}.pdf"
            folder_hierarchy = extract_folder_metadata(reader, start, end)

            preview_rows.append(
                {
                    "Source PDF": uploads[idx].name,
                    "Form Name": form_name,
                    "Pages": f"{start}-{end}",
                    "Folder": folder_hierarchy,
                    "Filename": zip_fname,
                }
            )

        progress.progress((idx + 1) / len(all_bytes))

    # Summary metrics
    c1, c2, c3 = st.columns([1, 1, 1])
    c1.metric("Total forms", total_forms)
    c2.metric("Total split pages", total_split_pages)
    c3.metric("Original PDF pages", original_pdf_pages)

    st.subheader("Filename & Page-Range Preview")
    df = pd.DataFrame(preview_rows)
    st.dataframe(df, use_container_width=True)

    # build combined ZIP
    master = io.BytesIO()
    with st.spinner("Generating ZIP of splits…"):
        patterns = build_patterns(remove_input)
        with zipfile.ZipFile(master, "w") as mz:
            for b in all_bytes:
                sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
                with zipfile.ZipFile(sub) as sz:
                    for info in sz.infolist():
                        mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )
