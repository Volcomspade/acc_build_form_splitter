import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """Find pages containing TOC entries (lines like '# 123: ...')."""
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i for i, p in enumerate(reader.pages, start=1)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    """
    Parse TOC pages into a list of (raw_name, start_page).
    Looks for lines like '# 123: Form Name ... 45'.
    """
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ''
        for m in pattern.finditer(text):
            raw_name = m.group(1).strip()
            start = int(m.group(2))
            entries.append((raw_name, start))
    return entries

def get_form_title(reader, start_page, remove_id=True):
    """
    On the first page of a form, grab the first non‑empty line.
    By default strips off '#123:'; uncheck the box to keep the ID.
    """
    text = reader.pages[start_page-1].extract_text() or ''
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if remove_id:
            return re.sub(r'^#\s*\d+:?\s*', '', line)
        else:
            return re.sub(r'^#', '', line)
    return f"Page_{start_page}"

def slugify(name):
    """Turn a string into a safe filename (no slashes, wildcards, etc.)."""
    # remove illegal chars
    s = re.sub(r'[\\/:*?"<>|#]', '', name.strip())
    # spaces → underscores
    s = re.sub(r'\s+', '_', s)
    # collapse repeated underscores
    return re.sub(r'_+', '_', s)

def build_patterns(raw_input: str):
    """
    From a comma‑separated list of removal patterns (regex or *‑wildcards)
    produce a list of compiled regexes.
    """
    pats = []
    for token in (t.strip() for t in raw_input.split(',') if t.strip()):
        if '*' in token:
            esc = re.escape(token)
            token_re = esc.replace(r'\*', '.*')
        else:
            token_re = token
        pats.append(token_re)
    return pats

def split_forms(reader, toc_entries):
    """
    Given [(name, start), ...], return [(name, start, end), ...],
    where end is the page before the next start (or last page of PDF).
    """
    toc_entries = sorted(toc_entries, key=lambda x: x[1])
    total_pages = len(reader.pages)
    splits = []
    for idx, (raw_name, start) in enumerate(toc_entries):
        if idx + 1 < len(toc_entries):
            next_start = toc_entries[idx+1][1]
            end = next_start - 1
        else:
            end = total_pages
        end = min(end, total_pages)
        splits.append((raw_name, start, end))
    return splits

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)
    splits = split_forms(reader, toc_entries)

    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zf:
        for raw_name, start, end in splits:
            # extract the heading from page `start`
            title = get_form_title(reader, start, remove_id)
            # apply cleanup patterns
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

    out.seek(0)
    return out

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True
)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix       = st.text_input("Filename prefix", "")
suffix       = st.text_input("Filename suffix", "")
remove_id    = st.checkbox("Remove numeric ID prefix from titles", value=True)

patterns = build_patterns(remove_input)

if uploads:
    # Load each file into memory
    files = [(f.name, f.getvalue()) for f in uploads]

    # Build combined ZIP
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w') as zf:
        for name, data in files:
            with st.spinner(f"Splitting {name}…"):
                part = create_zip(data, patterns, prefix, suffix, remove_id)
                with zipfile.ZipFile(part) as pf:
                    for info in pf.infolist():
                        zf.writestr(info.filename, pf.read(info.filename))
    zip_buf.seek(0)

    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    st.subheader("Filename Preview")
    preview = []
    for _, data in files:
        reader = PdfReader(io.BytesIO(data))
        toc_entries = parse_toc(reader, detect_toc_pages(reader))
        for raw_name, start, end in split_forms(reader, toc_entries):
            title = get_form_title(reader, start, remove_id)
            clean = title
            for rx in patterns:
                clean = re.sub(rx, "", clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            preview.append({
                "Form Name": title,
                "Pages": f"{start}-{end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf"
            })

    st.dataframe(pd.DataFrame(preview), width=900)
