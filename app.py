import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """
    Read pages in order; collect pages where a TOC entry (# 1234:) appears,
    then stop as soon as we hit a page without any.
    """
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    toc = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if entry_rx.search(text):
            toc.append(i + 1)
        elif toc:
            break
    return toc

def parse_toc(reader, toc_pages):
    """
    From those TOC pages, pull out (# 7893: Form Name .... 15) entries.
    Returns list of (raw_title, start_page).
    """
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    out = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            start = int(m.group(2))
            out.append((raw, start))
    return out

def split_forms(entries, total_pages):
    """
    Turn [(raw, start), …] into [(raw, start, end), …].
    """
    splits = []
    for i, (raw, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        splits.append((raw, start, end))
    return splits

def slugify(name):
    """
    Clean up a string to be a safe filename.
    """
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input):
    """
    Comma‑separated wildcards/regex → [regex…]
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        if '*' in tok:
            esc = re.escape(tok)
            pats.append(esc.replace(r'\*', '.*'))
        else:
            pats.append(tok)
    return pats

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)

    # 1) find TOC pages
    toc_pages = detect_toc_pages(reader)

    # 2) parse raw titles & start pages
    entries = parse_toc(reader, toc_pages)

    # 3) compute (raw, start, end)
    splits = split_forms(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            # raw is already the TOC form name
            title = raw
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

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix      = st.text_input("Filename prefix", "")
suffix      = st.text_input("Filename suffix", "")
remove_id   = st.checkbox("Remove numeric ID prefix (TOC‑derived names have no '#' anyway)", value=True)
patterns    = build_patterns(remove_input)

if uploads:
    # read bytes once
    file_bytes_list = [f.getvalue() for f in uploads]

    # --- combined ZIP download ---
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for pdf_bytes in file_bytes_list:
            subzip = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(subzip) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)
    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # --- Live preview with progress bar ---
    st.subheader("Filename & Page‑Range Preview")
    rows = []
    total_forms = 0
    # first count total splits so we can show meaningful progress
    for pdf_bytes in file_bytes_list:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        toc = parse_toc(reader, detect_toc_pages(reader))
        total_forms += len(toc)

    progress = st.progress(0)
    done = 0

    for pdf_bytes in file_bytes_list:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits  = split_forms(entries, total)

        for raw, start, end in splits:
            clean = raw
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            rows.append({
                "Form Name": raw,
                "Pages":     f"{start}-{end}",
                "Filename":  f"{prefix}{fname}{suffix}.pdf"
            })

            done += 1
            progress.progress(int(done/total_forms * 100))

    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    progress.empty()
