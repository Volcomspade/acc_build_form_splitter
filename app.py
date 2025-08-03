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

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i+1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = reader.pages[pg-1].extract_text() or ""
        for m in toc_rx.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries

def split_ranges(entries, total_pages):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
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
        # escape everything first
        esc = re.escape(tok)
        # if user included *, treat those as wildcards
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader     = PdfReader(io.BytesIO(pdf_bytes))
    total      = len(reader.pages)
    toc_pages  = detect_toc_pages(reader)
    entries    = parse_toc(reader, toc_pages)
    splits     = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # strip off "#1234: " if requested
            name = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title

            # apply removal patterns
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)

            fname = slugify(name)
            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part.read())

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads          = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)
remove_input     = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix           = st.text_input("Filename prefix", "")
suffix           = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’)",
    value=True
)
patterns = build_patterns(remove_input)

if uploads:
    # read all once
    all_bytes = [f.read() for f in uploads]

    # build master ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b, file in zip(all_bytes, uploads):
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # live preview
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        reader      = PdfReader(io.BytesIO(b))
        total_pages = len(reader.pages)
        entries     = parse_toc(reader, detect_toc_pages(reader))
        splits      = split_ranges(entries, total_pages)

        for title, start, end in splits:
            name = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)
            fname = slugify(name)

            rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf"
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
