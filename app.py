import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """
    Return the contiguous block of pages that contain TOC entries
    (lines starting “# 1234:”).
    """
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if entry_rx.search(text):
            pages.append(i + 1)
        elif pages:
            # once we've started collecting, stop on the first non‑TOC page
            break
    return pages

def parse_toc(reader, toc_pages):
    """
    From each TOC page, extract lines like:
      # 7893: Form Name ………………… 15
    Returns [(raw_title, start_page), …].
    """
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    results = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            start = int(m.group(2))
            results.append((raw, start))
    return results

def split_forms(entries, total_pages):
    """
    Given [(raw, start), …], return [(raw, start, end), …]
    where end = next_start −1, or total_pages for the last.
    """
    splits = []
    for i, (raw, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        splits.append((raw, start, end))
    return splits

def slugify(s):
    """Make a filesystem‑safe filename."""
    safe = re.sub(r'[\\/:*?"<>|]', '', s)
    safe = re.sub(r'\s+', '_', safe)
    return re.sub(r'_+', '_', safe).strip('_')

def build_patterns(txt):
    """
    “a*,b” → [r’a.*’, r'b'] so you can re.sub(rx, '', …).
    """
    out = []
    for tok in [t.strip() for t in txt.split(',') if t.strip()]:
        if '*' in tok:
            esc = re.escape(tok)
            out.append(esc.replace(r'\*', '.*'))
        else:
            out.append(tok)
    return out

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)

    toc_pages = detect_toc_pages(reader)
    entries   = parse_toc(reader, toc_pages)
    splits    = split_forms(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            # optionally strip off the leading "#1234: "
            name = re.sub(r'^#\s*\d+:?\s*', '', raw) if remove_id else raw

            # apply any remove‑patterns
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)

            fname = slugify(name)
            full_name = f"{prefix}{fname}{suffix}.pdf"

            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])

            part = io.BytesIO()
            writer.write(part)
            part.seek(0)
            zf.writestr(full_name, part.read())

    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads    = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_txt = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix     = st.text_input("Filename prefix", "")
suffix     = st.text_input("Filename suffix", "")
remove_id  = st.checkbox("Remove numeric ID prefix (#1234:) from filenames", value=True)

patterns = build_patterns(remove_txt)

if uploads:
    # load all bytes once
    pdfs = [f.getvalue() for f in uploads]

    # --- Download All ZIP ---
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for pdf_bytes in pdfs:
            sz = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(sz) as part:
                for info in part.infolist():
                    mz.writestr(info.filename, part.read(info.filename))
    master.seek(0)
    st.download_button("Download all splits", master, "acc_build_forms.zip", "application/zip")

    # --- Live Preview with Progress ---
    st.subheader("Filename & Page‑Range Preview")

    # count total splits for progress bar
    total_splits = 0
    for pdf_bytes in pdfs:
        r = PdfReader(io.BytesIO(pdf_bytes))
        total_splits += len(parse_toc(r, detect_toc_pages(r)))

    prog = st.progress(0)
    done = 0
    rows = []

    for pdf_bytes in pdfs:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total  = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits  = split_forms(entries, total)

        for raw, start, end in splits:
            # raw TOC title
            display = re.sub(r'^#\s*\d+:?\s*', '', raw) if remove_id else raw
            name = display
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)
            fname = slugify(name)

            rows.append({
                "Form Name": raw,
                "Pages":     f"{start}-{end}",
                "Filename":  f"{prefix}{fname}{suffix}.pdf"
            })

            done += 1
            prog.progress(int(done/total_splits * 100))

    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    prog.empty()
