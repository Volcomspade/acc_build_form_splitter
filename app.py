import re
import io
import zipfile
from pathlib import Path

import streamlit as st
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^\s*#\s*\d+:\s*.+\s+\d+\s*$', re.MULTILINE)
    return [i for i, p in enumerate(reader.pages, start=1)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, pages):
    line_rx = re.compile(r'^\s*#\s*\d+:\s*(.+?)\s+(\d+)\s*$', re.MULTILINE)
    entries = []
    for pg in pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in line_rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def extract_form_name(page):
    """First non-empty line of the page, minus trailing dots."""
    text = page.extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return re.sub(r'\.{2,}$', '', line).strip()
    return ""

def slugify(name):
    s = name.strip()
    s = re.sub(r'[\\/:\*\?"<>|]', '', s)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s)

def build_patterns(raw_input: str):
    """Strip and convert '*' wildcards to '.*'."""
    pats = []
    for p in [x.strip() for x in raw_input.split(',') if x.strip()]:
        if '*' in p:
            esc = re.escape(p)
            pats.append('^' + esc.replace(r'\*', '.*') + '$')
        else:
            pats.append(p)
    return pats

def split_and_package(pdf_bytes, patterns, prefix, suffix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc = parse_toc(reader, detect_toc_pages(reader))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, pg in toc:
            start = pg - 1
            # compute end page
            idx = toc.index((title, pg))
            end = (toc[idx+1][1] - 2) if idx+1 < len(toc) else len(reader.pages) - 1

            header = extract_form_name(reader.pages[start])
            raw = header or title
            cleaned = raw
            for rx in patterns:
                cleaned = re.sub(rx, '', cleaned, flags=re.IGNORECASE)
            fname = slugify(cleaned)

            writer = PdfWriter()
            for p in range(start, end+1):
                writer.add_page(reader.pages[p])
            part_buf = io.BytesIO()
            writer.write(part_buf)
            part_buf.seek(0)
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part_buf.read())

    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC PDF Splitter")

uploaded = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_input = st.text_input("Remove patterns (* or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")

patterns = build_patterns(remove_input)

# Preload all PDFs into memory
pdf_data = [(f.name, f.read()) for f in uploaded] if uploaded else []

# Live preview with progress bar
if pdf_data:
    st.subheader("Filename Preview")
    total = sum(len(parse_toc(PdfReader(io.BytesIO(b)), detect_toc_pages(PdfReader(io.BytesIO(b))))) for _, b in pdf_data)
    prog = st.progress(0)
    count = 0
    table = []
    for fname, b in pdf_data:
        reader = PdfReader(io.BytesIO(b))
        toc = parse_toc(reader, detect_toc_pages(reader))
        for title, pg in toc:
            header = extract_form_name(reader.pages[pg-1])
            raw = header or title
            cleaned = raw
            for rx in patterns:
                cleaned = re.sub(rx, '', cleaned, flags=re.IGNORECASE)
            out_name = slugify(cleaned)
            table.append({
                "TOC Title": title,
                "Page Header": raw,
                "Filename": f"{prefix}{out_name}{suffix}.pdf"
            })
            count += 1
            prog.progress(int(count/total * 100))
    st.table(table)

# Split & Download ZIP
if st.button("Split & Download ZIP") and pdf_data:
    zip_out = io.BytesIO()
    with zipfile.ZipFile(zip_out, 'w') as zf:
        for _, b in pdf_data:
            buf = split_and_package(b, patterns, prefix, suffix)
            for info in zipfile.ZipFile(buf).infolist():
                zf.writestr(info.filename, zipfile.ZipFile(buf).read(info.filename))
    zip_out.seek(0)
    st.download_button("Download all splits", zip_out, file_name="acc_build_splits.zip")
