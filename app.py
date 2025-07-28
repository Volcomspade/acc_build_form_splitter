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
        i+1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    """
    Parse TOC pages for lines like:
      # 7893: Form Name ........ 15
    Returns a list of (raw_title, start_page).
    """
    rx = re.compile(r'#\s*\d+:\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in rx.finditer(text):
            raw = m.group(1).strip()      # exactly the blue form name
            start = int(m.group(2))       # page number
            entries.append((raw, start))
    return entries

def slugify(name):
    """
    Sanitize a string for use as a filename.
    """
    # remove illegal chars
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    # collapse whitespace to underscores
    s = re.sub(r'\s+', '_', s)
    # collapse runs of underscores
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input):
    """
    Turn comma‑separated wildcards/regex into a list of regex patterns.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        if '*' in tok:
            esc = re.escape(tok)
            # wildcard * → .*
            pats.append(esc.replace(r'\*', '.*'))
        else:
            pats.append(tok)
    return pats

def split_forms(entries, total_pages):
    """
    Turn [(raw, start), …] into [(raw, start, end), …] ranges.
    """
    out = []
    for i, (raw, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        out.append((raw, start, end))
    return out

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    """
    Split a single PDF into pieces named by its TOC entries,
    apply patterns/prefix/suffix, and pack into an in‑memory ZIP.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries  = parse_toc(reader, toc_pages)
    splits   = split_forms(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            # raw is exactly the TOC form name
            title = raw
            if remove_id:
                # if raw ever still has a leading ID you want stripped:
                title = re.sub(r'^\d+:\s*', '', title)
            # apply any remove‑patterns
            clean = title
            for pat in patterns:
                clean = re.sub(pat, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            # build the PDF slice
            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            # write into the ZIP
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part.read())

    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads    = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_in  = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix     = st.text_input("Filename prefix", "")
suffix     = st.text_input("Filename suffix", "")
remove_id  = st.checkbox("Remove numeric ID prefix", value=True)
patterns   = build_patterns(remove_in)

if uploads:
    # read all files into memory so we can reuse them
    all_bytes = [f.read() for f in uploads]

    # --- master ZIP ---
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for pdf_bytes in all_bytes:
            subzip = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(subzip) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # --- live preview ---
    st.subheader("Filename Preview")
    preview = []
    for pdf_bytes in all_bytes:
        reader  = PdfReader(io.BytesIO(pdf_bytes))
        total   = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits  = split_forms(entries, total)

        for raw, start, end in splits:
            title = raw
            if remove_id:
                title = re.sub(r'^\d+:\s*', '', title)
            clean = title
            for pat in patterns:
                clean = re.sub(pat, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)
            preview.append({
                "Form Name": title,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf"
            })

    df = pd.DataFrame(preview)
    st.dataframe(df, use_container_width=True)
