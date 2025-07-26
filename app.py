import re
import io
import zipfile
from pathlib import Path

import streamlit as st
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """
    Return list of 1-based pages containing lines like:
      #1234: Title … 56
    """
    entry_rx = re.compile(r'^\s*#\s*\d+:\s*.+\s+\d+\s*$', re.MULTILINE)
    return [i for i, p in enumerate(reader.pages, start=1)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, pages):
    """
    Given TOC page numbers, extract entries (#…: Title  N).
    Returns list of (title, page_number).
    """
    line_rx = re.compile(r'^\s*#\s*\d+:\s*(.+?)\s+(\d+)\s*$', re.MULTILINE)
    entries = []
    for pg in pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in line_rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def slugify(name):
    s = name.strip()
    s = re.sub(r'[\/:\*\?"<>|]', '', s)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s)

def split_and_package(pdf_bytes, remove_patterns, prefix, suffix):
    """
    Split one PDF (bytes) into sections, apply regex removals,
    and return an in-memory ZIP of the resulting PDFs.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    toc = parse_toc(reader, toc_pages)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        for idx, (title, start_pg) in enumerate(toc):
            start = start_pg - 1
            end   = (toc[idx+1][1] - 2) if idx+1 < len(toc) else total - 1

            clean = title
            for rx in remove_patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            writer = PdfWriter()
            for p in range(start, end+1):
                writer.add_page(reader.pages[p])

            out_bytes = io.BytesIO()
            writer.write(out_bytes)
            out_bytes.seek(0)

            file_name = f"{prefix}{fname}{suffix}.pdf"
            zf.writestr(file_name, out_bytes.read())

    zip_buf.seek(0)
    return zip_buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC PDF Splitter")

uploaded = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True
)

remove_input = st.text_input(
    "Regex patterns to remove (comma‑separated)",
    value="RREG,\d{6}"
)
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")

if st.button("Split & Download ZIP") and uploaded:
    patterns = [r.strip() for r in remove_input.split(",") if r.strip()]
    combined = io.BytesIO()
    with zipfile.ZipFile(combined, "w") as all_z:
        for pdf in uploaded:
            zip_buf = split_and_package(pdf.read(), patterns, prefix, suffix)
            base = pdf.name.replace('.pdf','')
            for zi in zipfile.ZipFile(zip_buf).infolist():
                all_z.writestr(f"{base}/{zi.filename}", zipfile.ZipFile(zip_buf).read(zi.filename))

    combined.seek(0)
    st.download_button(
        "Download all splits as acc_build_splits.zip",
        combined,
        file_name="acc_build_splits.zip"
    )
