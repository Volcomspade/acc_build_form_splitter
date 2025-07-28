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
    From the form's first page, extract the first non-empty line (the blue heading).
    Optionally strip leading '# 1234:' IDs.
    """
    text = reader.pages[page_no - 1].extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if remove_id:
            # Remove leading "#1234:" or "# 1234:"
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

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    """
    Split a single PDF according to its TOC and pack into an in-memory ZIP.
    """
    reader      = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)

    # Build entries from TOC
    raw_entries = parse_toc(reader, detect_toc_pages(reader))

    # Compute clamps and skip out-of-range
    splits = []
    for i, (raw, start) in enumerate(raw_entries):
        next_start = raw_entries[i+1][1] if i+1 < len(raw_entries) else total_pages + 1
        end_page   = min(next_start - 1, total_pages)

        # Skip entries that start beyond the PDF length
        if start > total_pages:
            continue

        splits.append((raw, start, end_page))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            # Title & filename logic
            title = get_form_title(reader, start, remove_id)
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            # Extract pages
            writer = PdfWriter()
            for p in range(start - 1, end):
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

uploads    = st.file_uploader(
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix        = st.text_input("Filename prefix", "")
suffix        = st.text_input("Filename suffix", "")
remove_id     = st.checkbox("Remove numeric ID prefix", value=True)
patterns      = build_patterns(remove_input)

if uploads:
    # Read all uploads into memory
    file_bytes_list = [f.read() for f in uploads]

    # --- Preview Table ---
    rows = []
    for pdf_bytes in file_bytes_list:
        reader      = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        raw_entries = parse_toc(reader, detect_toc_pages(reader))

        # clamp and skip out-of-range
        preview_splits = []
        for i, (raw, start) in enumerate(raw_entries):
            next_start = raw_entries[i+1][1] if i+1 < len(raw_entries) else total_pages + 1
            end_page   = min(next_start - 1, total_pages)
            if start > total_pages:
                continue
            preview_splits.append((raw, start, end_page))

        for raw, pg_start, pg_end in preview_splits:
            title = get_form_title(reader, pg_start, remove_id)
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            rows.append({
                "Source PDF": uploads[0].name if len(uploads)==1 else "…",
                "Form Name":  title,
                "Pages":      f"{pg_start}-{pg_end}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf",
            })

    df = pd.DataFrame(rows)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)

    # --- Download ZIP ---
    zip_out = io.BytesIO()
    with zipfile.ZipFile(zip_out, "w") as zf:
        for pdf_bytes in file_bytes_list:
            part_buf = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(part_buf) as part_zip:
                for info in part_zip.infolist():
                    zf.writestr(info.filename, part_zip.read(info.filename))
    zip_out.seek(0)

    st.download_button(
        "Download all splits",
        zip_out,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )
