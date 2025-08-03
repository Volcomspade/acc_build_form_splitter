import re
import io
import zipfile
import time

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
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')


def build_patterns(raw_input):
    """
    Turn comma-separated tokens into regex patterns, treating '*' as wildcard.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        # allow wildcard '*' -> '.*'
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats


def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # strip off "#1234: " if requested for filename (but leave in displayed title)
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title

            # apply removal patterns
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)

            fname = slugify(name_for_filename)
            writer = PdfWriter()
            # guard against out-of-range
            for p in range(max(0, start - 1), min(end, len(reader.pages))):
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

# Regex helper
with st.expander("🛈 Regex & wildcard tips", expanded=False):
    st.markdown(
        """
- You can supply comma-separated patterns.
- `*` acts as a wildcard (e.g., `03.*` will match `03.04`, `03.02`, etc.).
- Patterns are internally escaped except `*` which becomes `.*` in regex.
- If you need more precise removal, you can use raw regex like:
  - `\d+\.\d+` to strip version-like numbers such as `03.04` or `02.03`
  - `_L2_` to remove `L2_` fragments
  - Example: `03.*,_L2_` will remove `03.04` and `L2_` occurrences.
"""
    )

patterns = build_patterns(remove_input)

if uploads:
    # Read all uploaded file bytes once
    all_bytes = [f.read() for f in uploads]

    # Summarize & preview build with timing/progress
    st.subheader("Summary")
    total_forms = 0
    total_split_pages = 0
    original_pdf_pages = 0
    preview_rows = []
    per_file_stats = []

    progress = st.progress(0)
    start_all = time.perf_counter()
    for idx, b in enumerate(all_bytes):
        file_start = time.perf_counter()

        reader = PdfReader(io.BytesIO(b))
        total_pages = len(reader.pages)
        original_pdf_pages += total_pages

        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages)

        forms_this_file = 0
        pages_this_file = 0
        for title, start, end in splits:
            forms_this_file += 1
            span = max(0, end - start + 1)
            pages_this_file += span
            total_forms += 1
            total_split_pages += span

            # Filename personalization (remove ID if requested)
            filename_title = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            name_for_filename = filename_title
            for rx in patterns:
                name_for_filename = re.sub(rx, "", name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)
            zip_fname = f"{prefix}{fname}{suffix}.pdf"

            preview_rows.append(
                {
                    "Source PDF": uploads[idx].name,
                    "Form Name": title,
                    "Pages": f"{start}-{end}",
                    "Filename": zip_fname,
                }
            )

        file_dur = time.perf_counter() - file_start
        per_file_stats.append(
            {
                "Source PDF": uploads[idx].name,
                "Forms": forms_this_file,
                "Pages": pages_this_file,
                "Read Time (s)": round(file_dur, 2),
            }
        )
        progress.progress((idx + 1) / len(all_bytes))
    total_read_time = time.perf_counter() - start_all

    # Display summary metrics
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
    c1.metric("Total forms", total_forms)
    c2.metric("Total split pages", total_split_pages)
    c3.metric("Original PDF pages", original_pdf_pages)
    c4.metric("Initial read time", f"{total_read_time:.2f}s")

    # Per-file breakdown
    st.markdown("**Per-file read breakdown**")
    file_stats_df = pd.DataFrame(per_file_stats)
    st.dataframe(file_stats_df, use_container_width=True)

    # Live preview table
    st.subheader("Filename & Page-Range Preview")
    df = pd.DataFrame(preview_rows)
    st.dataframe(df, use_container_width=True)

    # Build master ZIP for download
    master = io.BytesIO()
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
