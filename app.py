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
    """Grab the first non-empty line on the page and trim trailing dots."""
    text = page.extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # remove sequences of three or more dots at end
        return re.sub(r'\.{3,}$', '', line).strip()
    return ""

def slugify(name):
    s = name.strip()
    s = re.sub(r'[\/:*?"<>|]', '', s)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s)

def split_and_package(pdf_bytes, remove_patterns, prefix, suffix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    toc = parse_toc(reader, toc_pages)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        for idx, (toc_title, start_pg) in enumerate(toc):
            start_idx = start_pg - 1
            end_idx   = (toc[idx+1][1] - 2) if idx+1 < len(toc) else total - 1

            # extract the real title from the page header
            header = extract_form_name(reader.pages[start_idx])
            raw = header or toc_title

            # apply removals
            clean = raw
            for rx in remove_patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            # build the split PDF
            writer = PdfWriter()
            for p in range(start_idx, end_idx + 1):
                writer.add_page(reader.pages[p])

            out = io.BytesIO()
            writer.write(out)
            out.seek(0)
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", out.read())

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
    "Regex patterns to remove (comma‑separated, leave blank for none)",
    value=""
)
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")

# Live preview
if uploaded:
    st.subheader("Filename Preview")
    patterns = [r.strip() for r in remove_input.split(",") if r.strip()]
    preview_rows = []
    for pdf in uploaded:
        reader = PdfReader(io.BytesIO(pdf.read()))
        toc = parse_toc(reader, detect_toc_pages(reader))
        for toc_title, start_pg in toc:
            header = extract_form_name(reader.pages[start_pg-1])
            raw = header or toc_title
            clean = raw
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            preview_rows.append({
                "TOC Title": toc_title,
                "Page Header": raw,
                "Filename": f"{prefix}{fname}{suffix}.pdf"
            })
    st.table(preview_rows)

# Split and download
if st.button("Split & Download ZIP") and uploaded:
    patterns = [r.strip() for r in remove_input.split(",") if r.strip()]
    result_zip = io.BytesIO()
    with zipfile.ZipFile(result_zip, "w") as all_z:
        for pdf in uploaded:
            zip_buf = split_and_package(pdf.read(), patterns, prefix, suffix)
            base = Path(pdf.name).stem
            for info in zipfile.ZipFile(zip_buf).infolist():
                all_z.writestr(f"{base}/{info.filename}",
                               zipfile.ZipFile(zip_buf).read(info.filename))
    result_zip.seek(0)
    st.download_button(
        "Download all splits as acc_build_splits.zip",
        result_zip,
        file_name="acc_build_splits.zip"
    )
