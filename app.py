import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i + 1 for i, p in enumerate(reader.pages)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            start = int(m.group(2))
            entries.append((raw, start))
    return entries

def split_forms(entries, total_pages):
    splits = []
    for i, (raw, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        splits.append((raw, start, end))
    return splits

def slugify(name):
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input):
    pats = []
    for token in [t.strip() for t in raw_input.split(',') if t.strip()]:
        if '*' in token:
            esc = re.escape(token)
            token_re = esc.replace(r'\*', '.*')
        else:
            token_re = token
        pats.append(token_re)
    return pats

def get_toc_title(raw_entry, remove_id):
    # raw_entry comes from TOC: e.g. "#6849: ACC/DCC-D4.1 ... "
    if remove_id:
        return re.sub(r'^#\s*\d+:?\s*', '', raw_entry).strip()
    else:
        return raw_entry.strip()

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits  = split_forms(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            title = get_toc_title(raw, remove_id)
            clean = title
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

st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide"              # <— restore the full‑width mode
)
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix       = st.text_input("Filename prefix", "")
suffix       = st.text_input("Filename suffix", "")
remove_id    = st.checkbox("Remove numeric ID prefix (e.g. '#6849:')", value=True)
patterns     = build_patterns(remove_input)

# — Regex & wildcard tips expander —
with st.expander("🛈 Regex & wildcard tips", expanded=False):
    st.markdown("""
    - Use `*` to match any number of characters.  
      e.g. `03.*_` will strip `03.04_`, `03.02_`, etc.  
    - Separate multiple patterns with commas.  
      e.g. `_0*_0*`, `Exhibit`, `\d{2}\.\d{2}_`  
    - For full regex, avoid needing to escape `/` or `:`.
    """)

if uploads:
    # read all bytes just once
    file_bytes_list = [f.getvalue() for f in uploads]

    # build combined zip
    zip_out = io.BytesIO()
    with zipfile.ZipFile(zip_out, 'w') as mz:
        for pdf_bytes in file_bytes_list:
            part = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(part) as subz:
                for info in subz.infolist():
                    mz.writestr(info.filename, subz.read(info.filename))
    zip_out.seek(0)

    st.download_button(
        "Download all splits",
        zip_out,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # **Wide** preview table
    st.subheader("Filename & Page-Range Preview")
    preview_rows = []
    for pdf_bytes in file_bytes_list:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits  = split_forms(entries, total)
        for raw, start, end in splits:
            title = get_toc_title(raw, remove_id)
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            preview_rows.append({
                "Source PDF":    "(multiple)" if len(uploads)>1 else uploads[0].name,
                "Form Name":     title,
                "Pages":         f"{start}-{end}",
                "Filename":      f"{prefix}{fname}{suffix}.pdf"
            })

    df = pd.DataFrame(preview_rows)
    st.dataframe(df, use_container_width=True)
