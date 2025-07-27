import io
import zipfile
import streamlit as st
from PyPDF2 import PdfReader, PdfWriter
import re

def detect_toc_pages(reader):
    toc_rx = re.compile(r'^\s*#\s*\d+:', re.MULTILINE)
    return [i for i, p in enumerate(reader.pages, start=1)
            if toc_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    entry_rx = re.compile(r'#\s*\d+:\s*(.+?)\s+(\d+)', re.MULTILINE)
    out = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in entry_rx.finditer(text):
            out.append((m.group(1).strip(), int(m.group(2))))
    return out

st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
)

if uploads:
    # 1) Read each upload exactly once
    st.subheader("🗂️ Loading files...")
    load_prog = st.progress(0)
    pdf_cache = []  # list of (filename, bytes)
    for idx, f in enumerate(uploads):
        data = f.read()
        pdf_cache.append((f.name, data))
        load_prog.progress(int((idx+1)/len(uploads)*100))
    load_prog.empty()

    # 2) Now split each PDF in the cache
    st.subheader("✂️ Splitting PDFs...")
    split_prog = st.progress(0)
    final_zip = io.BytesIO()
    with zipfile.ZipFile(final_zip, "w") as bundle:
        for idx, (fname, data) in enumerate(pdf_cache):
            reader = PdfReader(io.BytesIO(data))
            toc_pages = detect_toc_pages(reader)
            toc_entries = parse_toc(reader, toc_pages)
            total_pages = len(reader.pages)

            for i, (form_name, start_pg) in enumerate(toc_entries):
                start_idx = start_pg - 1
                end_idx = (toc_entries[i+1][1] - 2) if i+1 < len(toc_entries) else total_pages-1

                writer = PdfWriter()
                for p in range(start_idx, end_idx+1):
                    writer.add_page(reader.pages[p])

                buf = io.BytesIO()
                writer.write(buf)
                bundle.writestr(f"{form_name}.pdf", buf.getvalue())

            split_prog.progress(int((idx+1)/len(pdf_cache)*100))
    final_zip.seek(0)

    # 3) Provide one download button
    st.download_button(
        "📥 Download All Form PDFs",
        final_zip,
        file_name="forms_by_TOC.zip"
    )
