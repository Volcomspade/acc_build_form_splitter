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
    """Get the first non-empty line of a form page and strip trailing dots."""
    text = page.extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return re.sub(r'\.{3,}$', '', line).strip()
    return ""

def slugify(name):
    s = name.strip()
    s = re.sub(r'[\\/:\*\?"<>|]', '', s)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s)

def build_patterns(raw_input: str):
    """Convert comma-separated input into regex patterns; '*'→'.*' wildcards."""
    pats = []
    for p in [x.strip() for x in raw_input.split(',') if x.strip()]:
        if '*' in p:
            esc = re.escape(p)
            regex = '^' + esc.replace(r'\*', '.*') + '$'
        else:
            regex = p
        pats.append(regex)
    return pats

def split_and_package(pdf_bytes, patterns, prefix, suffix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc = parse_toc(reader, detect_toc_pages(reader))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for idx, (toc_title, pg) in enumerate(toc):
            start = pg - 1
            end = (toc[idx+1][1]-2) if idx+1 < len(toc) else len(reader.pages)-1
            header = extract_form_name(reader.pages[start])
            raw = header or toc_title
            cleaned = raw
            for rx in patterns:
                cleaned = re.sub(rx, '', cleaned, flags=re.IGNORECASE)
            fname = slugify(cleaned)
            writer = PdfWriter()
            for p in range(start, end+1):
                writer.add_page(reader.pages[p])
            fragment = io.BytesIO()
            writer.write(fragment)
            fragment.seek(0)
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", fragment.read())
    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC PDF Splitter")

uploaded = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_input = st.text_input("Remove patterns (comma-separated; '*' wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")

patterns = build_patterns(remove_input)

# Live preview with progress
if uploaded:
    st.subheader("Filename Preview")
    total = sum(len(parse_toc(PdfReader(io.BytesIO(f.read())), detect_toc_pages(PdfReader(io.BytesIO(f.read()))))) for f in uploaded)
    progress = st.progress(0)
    count = 0
    rows = []
    for f in uploaded:
        reader = PdfReader(io.BytesIO(f.read()))
        toc = parse_toc(reader, detect_toc_pages(reader))
        for title, pg in toc:
            header = extract_form_name(reader.pages[pg-1])
            raw = header or title
            cleaned = raw
            for rx in patterns:
                cleaned = re.sub(rx, '', cleaned, flags=re.IGNORECASE)
            fname = slugify(cleaned)
            rows.append({"TOC Title": title, "Page Header": raw, "Filename": f"{prefix}{fname}{suffix}.pdf"})
            count += 1
            progress.progress(int(count/total * 100))
    st.table(rows)

# Split & Download
if st.button("Split & Download ZIP") and uploaded:
    out_zip = io.BytesIO()
    with zipfile.ZipFile(out_zip, 'w') as zf:
        for f in uploaded:
            buf = split_and_package(f.read(), patterns, prefix, suffix)
            for info in zipfile.ZipFile(buf).infolist():
                zf.writestr(info.filename, zipfile.ZipFile(buf).read(info.filename))
    out_zip.seek(0)
    st.download_button("Download all splits", out_zip, file_name="acc_build_splits.zip")
