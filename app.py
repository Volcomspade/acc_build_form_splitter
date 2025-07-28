import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i + 1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    """
    Extracts (title, start_page) from lines like:
      #6849: ACC/DCC-D4.1 (P23459AD0003): 03.04 Exhibit H-3 - Exhibit H-4 ACC Build  ......... 4
    """
    # group1 = the full title after the colon up to the dot leaders
    toc_pattern = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in toc_pattern.finditer(text):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries

def split_ranges(entries, total_pages):
    """
    Turns [(title, start), ...] into [(title, start, end), ...].
    """
    out = []
    for idx, (title, start) in enumerate(entries):
        end = entries[idx+1][1] - 1 if idx+1 < len(entries) else total_pages
        out.append((title, start, end))
    return out

def slugify(name):
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw):
    pats = []
    for tok in [t.strip() for t in raw.split(',') if t.strip()]:
        if '*' in tok:
            esc = re.escape(tok)
            pats.append(esc.replace(r'\*','.*'))
        else:
            pats.append(tok)
    return pats

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_hash):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)
    splits = split_ranges(toc_entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # optionally strip leading "#" if they want
            name = title.lstrip('# ') if remove_hash else title
            # apply user patterns
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

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_input = st.text_input("Remove patterns (* or regex)", "")
prefix        = st.text_input("Filename prefix", "")
suffix        = st.text_input("Filename suffix", "")
remove_hash   = st.checkbox("Remove leading ‘#’ from TOC titles", value=True)
patterns      = build_patterns(remove_input)

if uploads:
    # Read in all PDFs
    pdfs = [f.read() for f in uploads]

    # Build master ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for pdf_bytes in pdfs:
            sub = create_zip(pdf_bytes, patterns, prefix, suffix, remove_hash)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button("Download all splits", master, file_name="acc_build_forms.zip", mime="application/zip")

    # Preview table
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, pdf_bytes in enumerate(pdfs):
        reader      = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        entries     = parse_toc(reader, detect_toc_pages(reader))
        splits      = split_ranges(entries, total_pages)

        for title, start, end in splits:
            name = title.lstrip('# ') if remove_hash else title
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)
            fname = slugify(name)

            rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name": title,
                "Pages": f"{start}-{end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf"
            })

    st.dataframe(pd.DataFrame(rows), use_container_width=True)
