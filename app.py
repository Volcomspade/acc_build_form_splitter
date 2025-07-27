import re
import io
import zipfile
from pathlib import Path

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i for i, p in enumerate(reader.pages, start=1)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    # Extract form names and start pages, handling dot leaders
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ''
        for m in pattern.finditer(text):
            name = m.group(1).strip()
            start = int(m.group(2))
            entries.append((name, start))
    return entries

def slugify(name):
    s = name.strip()
    s = re.sub(r'[\\/:*?"<>|]', '', s)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s)

def build_patterns(raw_input: str):
    pats = []
    for p in [x.strip() for x in raw_input.split(',') if x.strip()]:
        if '*' in p:
            esc = re.escape(p)
            pats.append(esc.replace(r'\*','.*'))
        else:
            pats.append(p)
    return pats

def split_forms(reader, entries):
    # Determine page ranges for each form
    splits = []
    total = len(reader.pages)
    for i, (name, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total
        splits.append((name, start, end))
    return splits

def create_zip(pdf_bytes, patterns, prefix, suffix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)
    splits = split_forms(reader, toc_entries)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, start, end in splits:
            clean = name
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part.read())
    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title='ACC Build TOC Splitter')
st.title('ACC Build TOC Splitter')

uploaded = st.file_uploader('Upload ACC Build PDF(s)', type='pdf', accept_multiple_files=True)
remove_input = st.text_input('Remove patterns (* wildcards or regex)', '')
prefix = st.text_input('Filename prefix', '')
suffix = st.text_input('Filename suffix', '')
patterns = build_patterns(remove_input)

if uploaded:
    # Download button at top
    zip_out = io.BytesIO()
    with zipfile.ZipFile(zip_out, 'w') as zf:
        for f in uploaded:
            buf = create_zip(f.read(), patterns, prefix, suffix)
            for info in zipfile.ZipFile(buf).infolist():
                zf.writestr(info.filename, zipfile.ZipFile(buf).read(info.filename))
    zip_out.seek(0)
    st.download_button('Download all splits', zip_out, file_name='acc_build_forms.zip')

    # Live preview
    st.subheader('Filename Preview')
    table = []
    for f in uploaded:
        reader = PdfReader(io.BytesIO(f.read()))
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_forms(reader, entries)
        for name, start, end in splits:
            clean = name
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            table.append({
                'Form Name': name,
                'Pages': f"{start}-{end}",
                'Filename': f"{prefix}{fname}{suffix}.pdf"
            })
    df = pd.DataFrame(table)
    st.dataframe(df, width=900)
