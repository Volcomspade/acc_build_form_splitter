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
    Parse the TOC pages for (name, start_page),
    using dot-leader syntax: '# 123: Form Name ...  45'
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

def get_form_title(reader, start_page):
    """
    On the actual form page, grab the first non-empty line,
    then strip off any leading '#123:' ID.
    """
    text = reader.pages[start_page-1].extract_text() or ''
    for line in text.splitlines():
        line = line.strip()
        if line:
            # remove leading "#123:" if present
            return re.sub(r'^#\s*\d+:\s*', '', line)
    return f"Page_{start_page}"

def slugify(name):
    """Make a filesystem‑safe slug from the form title."""
    s = re.sub(r'[\\/:*?"<>|]', '', name.strip())
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s)

def build_patterns(raw_input: str):
    """
    Turn comma‑separated patterns into regexes.
    '*' becomes '.*', others treated literally or as full regex.
    """
    pats = []
    for token in (x.strip() for x in raw_input.split(',') if x.strip()):
        if '*' in token:
            esc = re.escape(token)
            pats.append(esc.replace(r'\*', '.*'))
        else:
            pats.append(token)
    return pats

def split_forms(reader, toc_entries):
    """
    Given [(name, start_page), ...], produce [(start_page, end_page), ...].
    End is the one‑before the next start, or last page of the PDF.
    """
    splits = []
    total_pages = len(reader.pages)
    for idx, (_, start) in enumerate(toc_entries):
        end = toc_entries[idx+1][1] - 1 if idx+1 < len(toc_entries) else total_pages
        splits.append((start, end))
    return splits

def create_zip(pdf_bytes, patterns, prefix, suffix):
    """
    Read a PDF, detect and parse its TOC, split into forms,
    and package each form‐PDF into a single in‑memory ZIP.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)
    page_ranges = split_forms(reader, toc_entries)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for (orig_name, start), (pg_start, pg_end) in zip(toc_entries, page_ranges):
            # Grab the real title from its first page:
            title = get_form_title(reader, pg_start)
            # Apply user removal patterns:
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            # Extract pages:
            writer = PdfWriter()
            for p in range(pg_start-1, pg_end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            # Write to the ZIP:
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part.read())

    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title='ACC Build TOC Splitter')
st.title('ACC Build TOC Splitter')

uploaded = st.file_uploader(
    'Upload ACC Build PDF(s)',
    type='pdf',
    accept_multiple_files=True
)
remove_input = st.text_input('Remove patterns (* wildcards or regex)', '')
prefix = st.text_input('Filename prefix', '')
suffix = st.text_input('Filename suffix', '')
patterns = build_patterns(remove_input)

# After st.file_uploader and inputs…
if uploaded:
    # Read all files into memory once (avoid EmptyFileError on second read)
    uploads = [(f.name, f.read()) for f in uploaded]

    # Download button at top
    zip_out = io.BytesIO()
    with zipfile.ZipFile(zip_out, 'w') as zf:
        for orig_name, pdf_bytes in uploads:
            buf = create_zip(pdf_bytes, patterns, prefix, suffix)
            with zipfile.ZipFile(buf) as part_zip:
                for info in part_zip.infolist():
                    zf.writestr(info.filename, part_zip.read(info.filename))
    zip_out.seek(0)
    st.download_button('Download all splits', zip_out, file_name='acc_build_forms.zip')

    # Live preview table
    st.subheader('Filename Preview')
    table = []
    for orig_name, pdf_bytes in uploads:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        entries = parse_toc(reader, detect_toc_pages(reader))
        for name, start, end in split_forms(reader, entries):
            clean = name
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            table.append({
                'Form Name': name,
                'Pages': f"{start}-{end}",
                'Filename': f"{prefix}{fname}{suffix}.pdf"
            })
    df = pd.DataFrame(table)
    st.dataframe(df, width=900)


    # **Live preview** table:
    st.subheader('Filename Preview')
    rows = []
    for f in uploaded:
        reader = PdfReader(io.BytesIO(f.read()))
        toc = parse_toc(reader, detect_toc_pages(reader))
        ranges = split_forms(reader, toc)
        for (orig_name, start), (pg_start, pg_end) in zip(toc, ranges):
            title = get_form_title(reader, pg_start)
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            rows.append({
                'Form Title': title,
                'Pages': f"{pg_start}-{pg_end}",
                'Filename': f"{prefix}{fname}{suffix}.pdf"
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, width=900)
