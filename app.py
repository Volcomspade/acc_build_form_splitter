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
    return [
        i + 1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]


def parse_toc(reader, toc_pages):
    """
    Parse TOC pages for entries of the form:
      # 7893: Form Name ........ 15
    Returns a list of (raw_title, start_page).
    """
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg - 1].extract_text() or ""
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            start = int(m.group(2))
            entries.append((raw, start))
    return entries


def get_form_title(reader, page_no, remove_id=True):
    """
    From the form’s first page, extract the blue heading, i.e. the first line
    beginning with ‘#’.  Skip any site‑header lines like ‘400060 – Wizard BESS’.
    Optionally strip off the ‘# 1234:’ prefix.
    """
    text = reader.pages[page_no - 1].extract_text() or ""
    # collect all non‑empty lines
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # 1) First look for the blue heading line (starts with '#')
    for ln in lines:
        if ln.startswith("#"):
            if remove_id:
                return re.sub(r'^#\s*\d+:?\s*', '', ln)
            return ln

    # 2) Fallback: if nothing starts with '#', skip any pure‑numeric headers
    header_rx = re.compile(r'^\d+\s*[-–]\s*.+$')
    for ln in lines:
        if header_rx.match(ln):
            continue
        # take the first “real” line
        return ln

    # 3) Ultimate fallback
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
    tokens = [t.strip() for t in raw_input.split(',') if t.strip()]
    for token in tokens:
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
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        splits.append((raw, start, end))
    return splits


def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    """
    Split a single PDF according to its TOC and pack into an in-memory ZIP.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_forms(entries, total_pages)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            title = get_form_title(reader, start, remove_id)
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            writer = PdfWriter()
            for p in range(start - 1, end):
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

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True
)
remove_input = st.text_input(
    "Remove patterns (* wildcards or regex)",
    ""
)
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id = st.checkbox("Remove numeric ID prefix", value=True)
patterns = build_patterns(remove_input)

if uploads:
    # Cache bytes for double-processing
    file_bytes_list = [f.getvalue() for f in uploads]

    # Build master ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for pdf_bytes in file_bytes_list:
            subzip = create_zip(
                pdf_bytes, patterns, prefix, suffix, remove_id
            )
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

    # Live preview
    st.subheader("Filename Preview")
    rows = []
    for pdf_bytes in file_bytes_list:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_forms(entries, total_pages)
        for _, start, end in splits:
            title = get_form_title(reader, start, remove_id)
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            rows.append({
                "Form Title": title,
                "Pages": f"{start}-{end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf"
            })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
