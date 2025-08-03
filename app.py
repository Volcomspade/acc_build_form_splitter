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


def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id_prefix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # For filename, optionally strip the numeric ID prefix; form name keeps it
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title

            # apply removal patterns to the filename component
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)

            fname = slugify(name_for_filename)
            writer = PdfWriter()
            for p in range(start - 1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part.read())

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

# Regex helper
with st.expander("🔎 Regex & wildcard tips", expanded=False):
    st.markdown(
        """
**Basic usage:**
- Comma-separated terms are treated as independent regex removals.
- Use `*` as a wildcard (e.g., `03.*` will match `03.04`, `03.02`, etc.).
- Example to remove "03.04" or "02.03": `0[2-3]\.[0-4]` or use wildcard `0?.0?` (but note `?` in regex is quantifier—use `.` to match any single character, or `.*` for arbitrary stretch).
- To strip underscores or trailing artifacts, you can chain patterns like `_+` or `\s+`.
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

    # Read and parse each upload with progress
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

    # Summary
    st.markdown("## Summary")
    cols = st.columns([1, 1, 1, 1.5])
    cols[0].metric("Total source PDFs", len(uploads))
    cols[1].metric("Total pages", total_pages)
    cols[2].metric("Total forms", total_forms)
    cols[3].metric("Initial read time", formatted_time)

    st.subheader("Per-file read stats")
    df_stats = pd.DataFrame(per_file_stats)
    st.dataframe(df_stats, use_container_width=True)

    # Build master ZIP
    st.subheader("Assembling ZIP of splits")
    zip_status = st.empty()
    zip_status.markdown("Building ZIP...")
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b, file in zip(all_bytes, uploads):
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    # if duplicate, append numeric suffix automatically
                    try:
                        mz.writestr(info.filename, sz.read(info.filename))
                    except zipfile.BadZipFile:
                        pass  # ignore malformed inside
    master.seek(0)
    zip_status.markdown("ZIP ready.")
    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # Live preview
    st.subheader("Filename & Page-Range Preview")
    preview_rows = []
    for idx, b in enumerate(all_bytes):
        reader = PdfReader(io.BytesIO(b))
        total_pages_each = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages_each)

        for title, start, end in splits:
            # Form Name always includes the ID prefix
            form_name = title

            # Derive filename base (apply removal of ID if requested)
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name": form_name,
                "Pages": f"{start}-{end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf"
            })

    df_preview = pd.DataFrame(preview_rows)
    st.dataframe(df_preview, use_container_width=True)
