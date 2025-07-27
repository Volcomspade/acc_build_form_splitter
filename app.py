import re
import io
import zipfile
from pathlib import Path

import streamlit as st
from PyPDF2 import PdfReader, PdfWriter

# --- Helper functions ---

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^\s*#\s*\d+:', re.MULTILINE)
    return [i for i, p in enumerate(reader.pages, start=1)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    # Simple pattern: #123: Form Name ... 5
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\s*\.+\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in pattern.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def split_pdf_by_toc(pdf_bytes, prefix, suffix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w') as zf:
        for idx, (title, start) in enumerate(toc_entries):
            start_idx = start - 1
            end_idx = (toc_entries[idx+1][1] - 2) if idx+1 < len(toc_entries) else len(reader.pages) - 1

            writer = PdfWriter()
            for p in range(start_idx, end_idx + 1):
                writer.add_page(reader.pages[p])

            clean_title = re.sub(r'[\\/:*?"<>|]', '', title).strip()
            filename = f"{prefix}{clean_title}{suffix}.pdf"
            out = io.BytesIO()
            writer.write(out)
            zf.writestr(filename, out.getvalue())

    zip_buf.seek(0)
    return zip_buf

# --- Streamlit UI ---

st.title("ACC Build TOC Splitter")

uploaded = st.file_uploader("Upload ACC Build PDFs", type="pdf", accept_multiple_files=True)
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")

if uploaded:
    all_zip = io.BytesIO()
    with zipfile.ZipFile(all_zip, 'w') as all_z:
        for pdf_file in uploaded:
            buf = split_pdf_by_toc(pdf_file.read(), prefix, suffix)
            for info in zipfile.ZipFile(buf).infolist():
                all_z.writestr(info.filename, zipfile.ZipFile(buf).read(info.filename))
    all_zip.seek(0)
    st.download_button("Download split PDFs", all_zip, file_name="splits.zip")
