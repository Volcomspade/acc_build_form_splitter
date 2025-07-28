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
        text = reader.pages[pg - 1].extract_text() or ""
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            start = int(m.group(2))
            entries.append((raw, start))
    return entries


def get_form_title(reader, page_no, remove_id=True, debug=False):
    """
    From the form's first page, extract the first non-empty line (the blue heading).
    Optionally strip leading '# 1234:' IDs.
    If debug=True, dump the raw page text to help troubleshoot.
    """
    text = reader.pages[page_no - 1].extract_text() or ""

    if debug:
        st.subheader(f"🛠 Debug: page {page_no} raw text")
        st.text_area("Raw page text", text, height=200)

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if remove_id:
            # Remove '# 1234:' or '#1234' prefix
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
            # Treat '*' as wildcard
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
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id = st.checkbox("Remove numeric ID prefix", value=True)
patterns = build_patterns(remove_input)

if uploads:
    # Read all uploaded files once into memory
    file_bytes_list = [f.read() for f in uploads]

    # --- ZIP generation ---
    zip_out = io.BytesIO()
    with zipfile.ZipFile(zip_out, 'w') as mz:
        for pdf_bytes in file_bytes_list:
            part_bytes = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(part_bytes) as part_zip:
                for info in part_zip.infolist():
                    mz.writestr(info.filename, part_zip.read(info.filename))
    zip_out.seek(0)

    st.download_button(
        "Download all splits",
        zip_out,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # --- Live preview with debug on first entry ---
    st.subheader("Filename & Page-Range Preview")
    preview = []

    for pdf_idx, pdf_bytes in enumerate(file_bytes_list):
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_forms(entries, total_pages)

        for i, (raw, start, end) in enumerate(splits):
            # turn on debug for the very first split of the first PDF
            debug_flag = (pdf_idx == 0 and i == 0)
            title = get_form_title(reader, start, remove_id, debug=debug_flag)

            if debug_flag:
                st.markdown(f"**TOC entry raw**: `{raw}`")
                cleaned_raw = re.sub(r'\.{2,}\s*\d+$', '', raw)
                st.markdown(f"**After cleanup**: `{cleaned_raw}`")

            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            preview.append({
                "Source PDF": uploads[pdf_idx].name,
                "Form Name": title,
                "Pages": f"{start}-{end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf",
            })

    df = pd.DataFrame(preview)
    st.dataframe(df, use_container_width=True)
