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
    Turn comma-separated tokens into literal-escaped regex patterns,
    then replace any \* back into .* for wildcards.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*')  # wildcard support
        pats.append(esc)
    return pats

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id_filename):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # Derive base name for filename: optionally strip "#1234: "
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_filename else title

            # apply removal patterns to filename base
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)

            fname = slugify(name_for_filename)
            writer = PdfWriter()
            # guard against malformed ranges
            if start - 1 < 0:
                continue
            for p in range(start - 1, end):
                if p < len(reader.pages):
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

# Regex/helper expander
with st.expander("ℹ️ Regex & wildcard tips", expanded=False):
    st.markdown(
        """
- Comma-separate multiple patterns to apply.  
- Use `*` as a wildcard: e.g., `03.*` matches `03.04`, `03.02`, etc.  
- To remove specific substrings like `L2_`, just include them literally.  
- Examples:  
  * `0\d\.0\d` to match versions like `03.04` or `02.03` (note: `?` in regex means "optional previous", so for flexible digits prefer `\d` or character classes).  
  * `L2_` will strip the literal `L2_` from the filename.  
"""
    )

patterns = build_patterns(remove_input)

if uploads:
    start_time = time.time()

    # ── STEP 1: read source PDFs with progress ─────────────────────────────────
    st.subheader("Reading source PDFs")
    read_bar = st.progress(0)
    all_bytes = []
    per_file_stats = []
    for i, f in enumerate(uploads):
        file_start = time.time()
        with st.spinner(f"Reading {f.name} ({i+1}/{len(uploads)})"):
            b = f.read()
            all_bytes.append(b)
        file_elapsed = time.time() - file_start
        per_file_stats.append({
            "Source PDF": f.name,
            "Pages": len(PdfReader(io.BytesIO(b)).pages) if b else 0,
            "Read Time": f"{int(file_elapsed//60)}:{int(file_elapsed%60):02d}",
        })
        pct = int(((i + 1) / len(uploads)) * 100)
        read_bar.progress(pct)
    read_bar.text("Done reading files.")

    # ── STEP 2: build master ZIP with progress ─────────────────────────────────
    st.subheader("Assembling ZIP of splits")
    zip_bar = st.progress(0)
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for i, (b, file_obj) in enumerate(zip(all_bytes, uploads)):
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
            pct = int(((i + 1) / len(all_bytes)) * 100)
            zip_bar.progress(pct)
    master.seek(0)
    zip_bar.text("ZIP ready.")

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # ── STEP 3: preview with progress ─────────────────────────────────────────
    st.subheader("Filename & Page-Range Preview")
    preview_bar = st.progress(0)
    rows = []
    for idx, b in enumerate(all_bytes):
        reader = PdfReader(io.BytesIO(b))
        total_pages = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages)

        for title, start, end in splits:
            # Form Name keeps original title (with ID) always
            form_name_display = title

            # Derive filename base separately, applying remove-id flag only for filenames
            base_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            for rx in patterns:
                base_for_filename = re.sub(rx, '', base_for_filename, flags=re.IGNORECASE)
            fname = slugify(base_for_filename)

            rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name": form_name_display,
                "Pages": f"{start}-{end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf",
            })
        pct = int(((idx + 1) / len(all_bytes)) * 100)
        preview_bar.progress(pct)
    preview_bar.text("Preview built.")

    df = pd.DataFrame(rows)

    # Summary section (counts + time)
    total_forms = len(rows)
    total_pages = 0
    for r in rows:
        if "-" in r["Pages"]:
            a, b = r["Pages"].split("-")
            try:
                total_pages += int(b) - int(a) + 1
            except ValueError:
                pass
    elapsed = time.time() - start_time
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    st.markdown(
        f"**Summary:** {len(uploads)} source PDF(s), ~{total_pages} pages covered, {total_forms} forms extracted. Initial read+preview time: {mins}:{secs:02d}."
    )

    # Per-file read stats
    st.subheader("Per-file read stats")
    stats_df = pd.DataFrame(per_file_stats)
    st.table(stats_df)

    # Show preview dataframe
    st.dataframe(df, use_container_width=True)
