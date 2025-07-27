import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """Find pages where the TOC lives, by looking for lines like '# 1234:'."""
    toc_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i for i, p in enumerate(reader.pages, start=1)
            if toc_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    """
    Pull out (raw_name, start_page) from the TOC pages.
    Looks for lines like:
      # 7893: United Rentals 071925 ……… 15
    """
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\.*\s+(\d+)$', re.MULTILINE)
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
    From a form’s first page, grab the very first non‑empty line.
    By default strip off leading '# 1234:' IDs.
    """
    text = reader.pages[page_no-1].extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if remove_id:
            # drop leading '# 1234:' or '#1234'
            return re.sub(r'^#\s*\d+:?\s*', '', line)
        else:
            return line
    return f"Page_{page_no}"

def slugify(name):
    # Remove filesystem-unfriendly chars, collapse whitespace to underscores
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw: str):
    """
    Split on commas, treat '*' as wildcard, else literal regex.
    Returns list of regex strings.
    """
    pats = []
    for token in [t.strip() for t in raw.split(',') if t.strip()]:
        if '*' in token:
            esc = re.escape(token)
            token_re = esc.replace(r'\*', '.*')
        else:
            token_re = token
        pats.append(token_re)
    return pats

def split_ranges(reader, toc_entries):
    """
    Given [(raw_name, start), …], compute [(start_page, end_page), …].
    """
    toc_entries = sorted(toc_entries, key=lambda x: x[1])
    total = len(reader.pages)
    out = []
    for idx, (_, start) in enumerate(toc_entries):
        if idx+1 < len(toc_entries):
            end = toc_entries[idx+1][1] - 1
        else:
            end = total
        out.append((start, min(end, total)))
    return out

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)
    ranges = split_ranges(reader, toc_entries)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for (raw, start), (pg_start, pg_end) in zip(toc_entries, ranges):
            title = get_form_title(reader, pg_start, remove_id)
            name = title
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)
            fname = slugify(name)
            writer = PdfWriter()
            for p in range(pg_start-1, pg_end):
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
remove_id   = st.checkbox("Remove numeric ID prefix from titles", value=True)

patterns = build_patterns(remove_input)

if uploads:
    # build a single ZIP of all splits
    master = io.BytesIO()
    with zipfile.ZipFile(master, "w") as mz:
        for f in uploads:
            buf = create_zip(
                f.read(),
                patterns,
                prefix,
                suffix,
                remove_id
            )
            # merge each sub‑ZIP into master
            with zipfile.ZipFile(buf) as subz:
                for info in subz.infolist():
                    mz.writestr(info.filename, subz.read(info.filename))
    master.seek(0)
    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # live preview
    st.subheader("Filename Preview")
    preview = []
    for f in uploads:
        reader = PdfReader(io.BytesIO(f.read()))
        toc_entries = parse_toc(reader, detect_toc_pages(reader))
        ranges      = split_ranges(reader, toc_entries)
        for (raw, start), (pg_start, pg_end) in zip(toc_entries, ranges):
            title = get_form_title(reader, pg_start, remove_id)
            name = title
            for rx in patterns:
                name = re.sub(rx, "", name, flags=re.IGNORECASE)
            fname = slugify(name)
            preview.append({
                "Form Name": title,
                "Pages":     f"{pg_start}-{pg_end}",
                "Filename":  f"{prefix}{fname}{suffix}.pdf"
            })

    df = pd.DataFrame(preview)
    st.dataframe(df, width=900, use_container_width=True)
