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

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def format_duration(sec):
    m = int(sec) // 60
    s = int(sec) % 60
    return f"{m}:{s:02d}"

def slugify(name):
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input):
    """
    Turn comma-separated tokens into regex patterns, treating '*' as wildcard
    (multi-character) and '?' as single-character wildcard.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        # escape everything first
        esc = re.escape(tok)
        # wildcard conversions: \* -> .* , \? -> .
        esc = esc.replace(r'\*', '.*').replace(r'\?', '.')
        pats.append(esc)
    return pats

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i + 1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    # Matches: #6849: ACC/... .......................... 4
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

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id_prefix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # file name: strip off "#1234: " if requested
            name_for_file = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title

            # apply removal patterns
            for rx in patterns:
                name_for_file = re.sub(rx, '', name_for_file, flags=re.IGNORECASE)

            fname = slugify(name_for_file)

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

# Regex helper
with st.expander("Regex / wildcard helper", expanded=True):
    st.markdown(
        """
**Usage examples for the "Remove patterns" field**  
- `*Build` → removes any substring ending in "Build" (because `*` becomes `.*`).  
- `0?.0?` → matches `03.04`, `02.03`, etc. (`?` is single-character wildcard).  
- `L2_` → removes literal `L2_`.  
- Combine: `03.04,L2_` → removes those pieces from filenames.  

**Syntax details:**  
- `*` maps to `.*` (any sequence of characters).  
- `?` maps to `.` (exactly one character).  
- Comma-separate multiple patterns.  
- Matching is case-insensitive.
"""
    )

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

if uploads:
    # --- initial read/parse for summary & preview ---
    all_bytes = []
    per_file_stats = []
    total_pages = 0
    total_forms = 0

    start_all = time.perf_counter()
    for upload in uploads:
        t0 = time.perf_counter()
        data = upload.read()
        all_bytes.append(data)

        reader = PdfReader(io.BytesIO(data))
        pages_count = len(reader.pages)
        toc_entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(toc_entries, pages_count)

        t1 = time.perf_counter()
        file_dur = t1 - t0

        per_file_stats.append({
            "Source PDF": upload.name,
            "Pages": pages_count,
            "Forms": len(splits),
            "Read Time": format_duration(file_dur)
        })
        total_pages += pages_count
        total_forms += len(splits)
    end_all = time.perf_counter()
    total_read_time = end_all - start_all

    # --- summary display ---
    st.markdown("### Summary")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
    c1.metric("Total source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read time", format_duration(total_read_time), f"{total_read_time:.2f}s")

    st.markdown("#### Per-file read stats")
    df_stats = pd.DataFrame(per_file_stats)
    st.dataframe(df_stats, use_container_width=True)

    # --- build master ZIP ---
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b, upload in zip(all_bytes, uploads):
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    # If duplicate filename occurs, it will naturally warn; keep original source context
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # --- live preview of individual form splits ---
    st.subheader("Filename & Page-Range Preview")
    preview_rows = []
    for idx, b in enumerate(all_bytes):
        reader = PdfReader(io.BytesIO(b))
        total_pages_in_pdf = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages_in_pdf)

        for title, start, end in splits:
            # Form name always shows the original title (with ID if present)
            form_name = title

            # Filename transformation: optionally strip ID
            name_for_file = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title

            for rx in patterns:
                name_for_file = re.sub(rx, '', name_for_file, flags=re.IGNORECASE)
            fname = slugify(name_for_file)

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name": form_name,
                "Pages": f"{start}-{end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf"
            })

    df_preview = pd.DataFrame(preview_rows)
    st.dataframe(df_preview, use_container_width=True)
