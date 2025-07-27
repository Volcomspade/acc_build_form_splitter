import re
import io
import zipfile
import streamlit as st
from PyPDF2 import PdfReader, PdfWriter

def detect_toc_pages(reader):
    """Find pages containing TOC entries (#123: Title ... 5)."""
    toc_rx = re.compile(r'^\s*#\s*\d+:', re.MULTILINE)
    return [
        i for i, p in enumerate(reader.pages, start=1)
        if toc_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    """Extract (form_name, start_page) from each TOC page."""
    entry_rx = re.compile(r'#\s*\d+:\s*(.+?)\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in entry_rx.finditer(text):
            name = m.group(1).strip()
            start = int(m.group(2))
            entries.append((name, start))
    return entries

def split_and_zip(pdf_bytes):
    """Split PDF by TOC entries and return an in-memory ZIP of form PDFs."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for i, (name, start) in enumerate(toc_entries):
            start_idx = start - 1
            # end just before the next form’s start, or EOF
            end_idx = (toc_entries[i+1][1] - 2) if i+1 < len(toc_entries) else total - 1

            writer = PdfWriter()
            for p in range(start_idx, end_idx+1):
                writer.add_page(reader.pages[p])

            out = io.BytesIO()
            writer.write(out)
            zf.writestr(f"{name}.pdf", out.getvalue())

    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.title("ACC Build TOC Splitter")

uploaded = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True
)

if uploaded:
    # Build a single ZIP containing all form PDFs
    all_zip = io.BytesIO()
    with zipfile.ZipFile(all_zip, 'w') as bundle:
        for f in uploaded:
            subzip = split_and_zip(f.read())
            for info in zipfile.ZipFile(subzip).infolist():
                bundle.writestr(info.filename,
                                zipfile.ZipFile(subzip).read(info.filename))

    all_zip.seek(0)
    st.download_button(
        "Download All Form PDFs",
        all_zip,
        file_name="forms_by_TOC.zip"
    )
