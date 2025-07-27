```python
import re
import io
import zipfile
from pathlib import Path

import streamlit as st
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i for i, p in enumerate(reader.pages, start=1)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    rx = re.compile(r'#\s*\d+:\s*(.+?)\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
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
    splits = []
    total_pages = len(reader.pages)
    for i, (name, start) in enumerate(entries):
        end = entries[i+1][1] - 2 if i+1 < len(entries) else total_pages - 1
        splits.append((name, start-1, end))
    return splits

def create_zip(pdf_bytes, patterns, prefix, suffix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)
    splits = split_forms(reader, toc_entries)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, start, end in splits:
            clean_name = name
            for rx in patterns:
                clean_name = re.sub(rx, '', clean_name, flags=re.IGNORECASE)
            fname = slugify(clean_name)

            writer = PdfWriter()
            for p in range(start, end+1):
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

uploaded = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")

patterns = build_patterns(remove_input)

if uploaded:
    st.subheader("Filename Preview")
    rows = []
    for f in uploaded:
        data = f.read()
        reader = PdfReader(io.BytesIO(data))
        toc_pages = detect_toc_pages(reader)
        toc_entries = parse_toc(reader, toc_pages)
        splits = split_forms(reader, toc_entries)
        for name, start, end in splits:
            clean_name = name
            for rx in patterns:
                clean_name = re.sub(rx, '', clean_name, flags=re.IGNORECASE)
            fname = slugify(clean_name)
            rows.append({"Form Name": name, "Pages": f"{start+1}-{end+1}", "Filename": f"{prefix}{fname}{suffix}.pdf"})
    st.table(rows)

    if st.button("Split & Download ZIP"):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as zf:
            for f in uploaded:
                buf = create_zip(f.read(), patterns, prefix, suffix)
                for info in zipfile.ZipFile(buf).infolist():
                    zf.writestr(info.filename, zipfile.ZipFile(buf).read(info.filename))
        out.seek(0)
        st.download_button("Download ZIP", out, file_name="acc_build_forms.zip")
```
