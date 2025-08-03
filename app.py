import re
import io
import time
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i + 1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]


def parse_toc(reader, toc_pages):
    toc_rx = re.compile(r'#\s*(\d+):\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = reader.pages[pg - 1].extract_text() or ""
        for m in toc_rx.finditer(txt):
            full_title = f"#{m.group(1)}: {m.group(2).strip()}"
            start = int(m.group(3))
            entries.append((full_title, start))
    return entries


def split_ranges(entries, total_pages):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out


def slugify(name):
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')


def build_patterns(raw_input):
    """
    Turn comma-separated tokens into regex patterns; '*' becomes wildcard.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*')  # wildcard support
        pats.append(esc)
    return pats


def extract_location_category(reader, start, end):
    """
    Scan the early pages of a form's range for the "References and Attachments"
    section and pull out Location and Category.
    """
    location = "Unknown"
    category = "Unknown"
    for i in range(start - 1, min(end, start - 1 + 12)):
        text = reader.pages[i].extract_text() or ""
        if "References and Attachments" in text:
            # attempt to grab Category and Location fields
            cat_match = re.search(r'Category\s*(?:\n|\s)+([A-Za-z0-9 >]+)', text)
            loc_match = re.search(r'Location\s*(?:\n|\s)+([A-Za-z0-9 >]+)', text)
            if cat_match:
                category = cat_match.group(1).strip()
            if loc_match:
                location = loc_match.group(1).strip()
            break
    return location, category


def split_hierarchy(s):
    parts = [p.strip() for p in s.split('>') if p.strip()]
    return parts if parts else ["Unknown"]


def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id_prefix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # Form name always keeps ID (for display elsewhere)
            # Filename base: optionally strip the "#1234: " prefix
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title

            # apply removal patterns to the filename base
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)

            # determine folder path from metadata (hierarchical)
            location, category = extract_location_category(reader, start, end)
            loc_parts = split_hierarchy(location)
            cat_parts = split_hierarchy(category)
            folder_components = [slugify(p) for p in loc_parts + cat_parts]
            folder_path = "/".join(folder_components)

            # create the split PDF
            writer = PdfWriter()
            for p in range(start - 1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            full_name = f"{prefix}{fname}{suffix}.pdf"
            entry_name = f"{folder_path}/{full_name}"
            zf.writestr(entry_name, part.read())

    buf.seek(0)
    return buf


# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames", value=True
)
patterns = build_patterns(remove_input)

with st.expander("🔎 Regex & wildcard tips", expanded=False):
    st.markdown(
        """
**Basic usage:**
- Comma-separated terms are treated as independent regex removals.
- Use `*` as a wildcard (e.g., `03.*` will match `03.04`, `03.02`, etc.).
- To remove variations like `03.02`, `02.03`, you can do things like `0[2-3]\.[0-4]` or use `.*` for broader matches.  
- Examples:
  - `03\.\d+` removes things like `03.04`, `03.02`.
  - `_+` collapses repeated underscores.
- Patterns are applied case-insensitively.
"""
    )

if uploads:
    start_time = time.time()
    all_bytes = []
    per_file_stats = []
    total_pages = 0
    total_forms = 0

    st.subheader("Reading source PDFs")
    status_placeholder = st.empty()
    progress_bar = st.progress(0)

    for idx, f in enumerate(uploads):
        b = f.read()
        all_bytes.append(b)
        reader = PdfReader(io.BytesIO(b))
        num_pages = len(reader.pages)
        total_pages += num_pages

        toc_pages = detect_toc_pages(reader)
        entries = parse_toc(reader, toc_pages)
        splits = split_ranges(entries, num_pages)
        num_forms = len(splits)
        total_forms += num_forms
        per_file_stats.append({
            "Source PDF": f.name,
            "Pages": num_pages,
            "Forms": num_forms,
        })

        status_placeholder.markdown(f"Reading `{f.name}` ({idx + 1}/{len(uploads)}) – found {num_forms} forms.")
        progress_bar.progress((idx + 1) / len(uploads))

    status_placeholder.markdown("Done reading files.")
    read_duration = time.time() - start_time
    minutes = int(read_duration // 60)
    seconds = int(read_duration % 60)
    formatted_time = f"{minutes}:{seconds:02d}"

    st.markdown("## Summary")
    cols = st.columns([1, 1, 1, 1.5])
    cols[0].metric("Total source PDFs", len(uploads))
    cols[1].metric("Total pages", total_pages)
    cols[2].metric("Total forms", total_forms)
    cols[3].metric("Initial read time", formatted_time)

    st.subheader("Per-file read stats")
    df_stats = pd.DataFrame(per_file_stats)
    st.dataframe(df_stats, use_container_width=True)

    st.subheader("Assembling ZIP of splits")
    zip_status = st.empty()
    zip_status.markdown("Building ZIP...")
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b, file in zip(all_bytes, uploads):
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    try:
                        mz.writestr(info.filename, sz.read(info.filename))
                    except Exception:
                        pass
    master.seek(0)
    zip_status.markdown("ZIP ready.")
    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    st.subheader("Filename & Page-Range Preview")
    preview_rows = []
    for idx, b in enumerate(all_bytes):
        reader = PdfReader(io.BytesIO(b))
        total_pages_each = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages_each)

        for title, start, end in splits:
            form_name = title  # keeps the ID prefix visible

            location, category = extract_location_category(reader, start, end)
            folder_display = f"{location} > {category}"

            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)
            full_fname = f"{prefix}{fname}{suffix}.pdf"

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Folder": folder_display,
                "Form Name": form_name,
                "Pages": f"{start}-{end}",
                "Filename": full_fname
            })

    df_preview = pd.DataFrame(preview_rows)
    st.dataframe(df_preview, use_container_width=True)
