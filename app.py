import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """
    Identify pages containing TOC entries (lines starting with '# 1234:').
    """
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i + 1 for i, p in enumerate(reader.pages)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    """
    Parse TOC pages for entries of the form:
      # 7893: Form Name ........ 15
    Returns a list of (raw_title, start_page).
    """
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            start = int(m.group(2))
            entries.append((raw, start))
    return entries

def get_form_title(reader, page_no, remove_id=True):
    """
    From the form's first page, extract the first non-empty line.
    Optionally strip leading '# 1234:' IDs.
    """
    text = reader.pages[page_no-1].extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if remove_id:
            return re.sub(r'^#\s*\d+:?\s*', '', line)
        return line
    return f"Page_{page_no}"

def slugify(name):
    """
    Sanitize a string for use as a filename.
    """
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input):
    """
    Turn comma-separated wildcards/regex into a list of regex patterns.
    """
    pats = []
    for token in [t.strip() for t in raw_input.split(',') if t.strip()]:
        if '*' in token:
            esc = re.escape(token)
            token_re = esc.replace(r'\*', '.*')
        else:
            token_re = token
        pats.append(token_re)
    return pats

def split_forms(entries, total_pages):
    """
    Given [(raw, start), ...], compute [(raw, start, end), ...] page ranges.
    """
    splits = []
    for i, (raw, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        splits.append((raw, start, end))
    return splits

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    """
    Split a single PDF according to its TOC and pack into an in-memory ZIP.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    toc_pages   = detect_toc_pages(reader)
    entries     = parse_toc(reader, toc_pages)
    splits      = split_forms(entries, total_pages)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            # Determine title from TOC raw text:
            title = raw
            if remove_id:
                title = re.sub(r'^#\s*\d+:?\s*', '', title)

            # apply user‑supplied remove‑patterns
            for rx in patterns:
                title = re.sub(rx, '', title, flags=re.IGNORECASE)

            fname = slugify(title)
            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part_pdf = io.BytesIO()
            writer.write(part_pdf)
            part_pdf.seek(0)
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part_pdf.read())
    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads    = st.file_uploader(
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix      = st.text_input("Filename prefix", "")
suffix      = st.text_input("Filename suffix", "")
remove_id   = st.checkbox("Remove numeric ID prefix (TOC names have no '#')", value=True)
patterns    = build_patterns(remove_input)

if uploads:
    # Read all uploaded files into memory
    file_bytes_list = [f.read() for f in uploads]

    # --- Generate combined ZIP ---
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for pdf_bytes in file_bytes_list:
            part_bytes = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(part_bytes) as part_zip:
                for info in part_zip.infolist():
                    mz.writestr(info.filename, part_zip.read(info.filename))
    master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # --- Preview Generation with Progress Bar ---
    st.subheader("Filename & Page‑Range Preview")
    progress = st.progress(0)

    preview_rows = []
    for idx, pdf_bytes in enumerate(file_bytes_list):
        reader      = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        toc_pages   = detect_toc_pages(reader)
        toc_entries = parse_toc(reader, toc_pages)
        splits      = split_forms(toc_entries, total_pages)

        for raw, start, end in splits:
            # raw TOC title:
            form_name = raw
            display_name = raw
            if remove_id:
                display_name = re.sub(r'^#\s*\d+:?\s*', '', raw)

            # apply remove‑patterns
            for rx in patterns:
                display_name = re.sub(rx, '', display_name, flags=re.IGNORECASE)

            filename = f"{prefix}{slugify(display_name)}{suffix}.pdf"

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name":  display_name,
                "Pages":      f"{start}–{end}",
                "Filename":   filename,
            })

        progress.progress((idx + 1) / len(file_bytes_list))

    df = pd.DataFrame(preview_rows)

    # highlight any duplicate filenames in red
    dup_mask = df["Filename"].duplicated(keep=False)
    styled = (
        df.style
          .set_properties(subset=["Form Name","Filename"], **{"white-space":"pre-wrap"})
          .apply(lambda col: ["color: red;" if dup_mask[i] else "" 
                              for i in range(len(df))],
                 subset=["Filename"], axis=0)
    )

    st.dataframe(styled, use_container_width=True)
