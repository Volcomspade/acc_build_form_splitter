import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic based on TOC only ---

def detect_toc_pages(reader):
    """Return list of page indices (1‑based) where a TOC lives."""
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i + 1 for i, p in enumerate(reader.pages)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    """
    From each TOC page, pull entries of the form:
        # 7893: Form name ……………………….. 15
    Returns list of (raw_title, start_page).
    """
    pattern = re.compile(r'#\s*(\d+):\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in pattern.finditer(text):
            full_id = m.group(1)
            title   = m.group(2).strip()
            start   = int(m.group(3))
            raw     = f"#{full_id}: {title}"
            entries.append((raw, start))
    # sort by page just in case
    return sorted(entries, key=lambda x: x[1])

def split_ranges(entries, total_pages):
    """
    Turn [(raw, start), …] into [(raw, start, end), …],
    where end = next_start−1 or last page.
    """
    out = []
    for i, (raw, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        out.append((raw, start, end))
    return out

def make_zip(pdf_bytes, splits, patterns, prefix, suffix, remove_id):
    """
    Given pre‑computed splits list, build an in‑memory ZIP of each form PDF.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            name = raw
            if remove_id:
                name = re.sub(r'^#\s*\d+:?\s*', '', name)
            # apply user patterns
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)
            # sanitize
            fname = re.sub(r'[\\/:*?"<>|]', '', name).strip()
            fname = re.sub(r'\s+', '_', fname)
            full_fname = f"{prefix}{fname}{suffix}.pdf"

            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            zf.writestr(full_fname, part.getvalue())
    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads     = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_txt  = st.text_input("Remove patterns (* wildcard or regex)", "")
prefix      = st.text_input("Filename prefix", "")
suffix      = st.text_input("Filename suffix", "")
remove_id   = st.checkbox("Remove numeric ID prefix (e.g. “#6849:”) ", value=True)
patterns    = []
for tok in [t.strip() for t in remove_txt.split(',') if t.strip()]:
    if '*' in tok:
        esc = re.escape(tok)
        patterns.append(esc.replace(r'\*', '.*'))
    else:
        patterns.append(tok)

if uploads:
    # read each PDF once
    pdf_bytes_list = [f.read() for f in uploads]

    # --- Preview Generation with Progress Bar ---
    st.subheader("Filename & Page‑Range Preview")
    progress = st.progress(0)
    preview_rows = []

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        reader      = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        toc_pages   = detect_toc_pages(reader)
        toc_entries = parse_toc(reader, toc_pages)
        splits      = split_ranges(toc_entries, total_pages)

        for raw, start, end in splits:
            name = raw
            if remove_id:
                name = re.sub(r'^#\s*\d+:?\s*', '', name)
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)
            clean = re.sub(r'\s+', '_', name.strip())
            clean = re.sub(r'[\\/:*?"<>|]', '', clean)
            fname = f"{prefix}{clean}{suffix}.pdf"

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name":  raw,
                "Pages":      f"{start}–{end}",
                "Filename":   fname,
            })

        progress.progress((idx + 1) / len(pdf_bytes_list))

    df = pd.DataFrame(preview_rows)
    # highlight duplicates in Filename
    dup_mask = df["Filename"].duplicated(keep=False)
    def highlight_dups(val, is_dup):
        return "color: red;" if is_dup else ""
    styled = (
        df.style
          .apply(lambda col: [highlight_dups(v, is_dup) 
                              for v, is_dup in zip(col, dup_mask)],
                 axis=0, subset=["Filename"])
    )
    st.write(styled, unsafe_allow_html=True)

    # --- ZIP Download ---
    st.subheader("Generate & Download Splits")
    if st.button("Build ZIP & Download"):
        # build master zip
        master = io.BytesIO()
        with zipfile.ZipFile(master, 'w') as mz:
            for pdf_bytes in pdf_bytes_list:
                # reuse split logic
                reader      = PdfReader(io.BytesIO(pdf_bytes))
                total_pages = len(reader.pages)
                splits      = split_ranges(parse_toc(reader, detect_toc_pages(reader)), total_pages)
                part_zip    = make_zip(pdf_bytes, splits, patterns, prefix, suffix, remove_id)
                with zipfile.ZipFile(part_zip) as z:
                    for info in z.infolist():
                        mz.writestr(info.filename, z.read(info.filename))
        master.seek(0)
        st.download_button(
            "Download All Splits (ZIP)",
            data=master,
            file_name="acc_build_forms.zip",
            mime="application/zip"
        )
