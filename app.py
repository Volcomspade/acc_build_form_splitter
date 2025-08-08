import re
import io
import zipfile
import time

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i+1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = reader.pages[pg-1].extract_text() or ""
        for m in toc_rx.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries

def split_ranges(entries, total_pages):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        out.append((title, start, end))
    return out

def slugify(name):
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input):
    """
    Turn comma-separated tokens into literal-escaped regex patterns,
    then replace any \* back into non-greedy .*? for wildcards.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        # non-greedy wildcard
        esc = esc.replace(r'\*', '.*?')
        pats.append(esc)
    return pats

def extract_template(title):
    """
    From a TOC title like:
      "#6859: ACC/DCC-D6.2 (P...): 03.04 Exhibit H-3 - Exhibit H-4 ACC Build"
    grab the part after the second colon.
    """
    parts = title.split(':', 2)
    return parts[2].strip() if len(parts) == 3 else title

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id, group_by):
    reader     = PdfReader(io.BytesIO(pdf_bytes))
    total      = len(reader.pages)
    toc_pages  = detect_toc_pages(reader)
    entries    = parse_toc(reader, toc_pages)
    splits     = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # always keep full title for display; strip only for filename
            raw_name = title

            # filename base: optionally drop "#1234:"
            name_for_file = re.sub(r'^#\s*\d+:\s*', '', raw_name) if remove_id else raw_name
            # apply wildcards/regex
            for rx in patterns:
                name_for_file = re.sub(rx, '', name_for_file, flags=re.IGNORECASE)
            fname = slugify(name_for_file)

            # choose folder
            if group_by == "Template":
                folder = slugify(extract_template(raw_name))
            else:
                folder = None

            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            arcname = f"{prefix}{fname}{suffix}.pdf"
            if folder:
                arcname = f"{folder}/{arcname}"
            zf.writestr(arcname, part.read())

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads          = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)
remove_input     = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix           = st.text_input("Filename prefix", "")
suffix           = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from **filenames** only",
    value=True
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Template"]
)

patterns = build_patterns(remove_input)

if uploads:
    # ─ read & time it ─
    t0 = time.time()
    all_bytes = [f.read() for f in uploads]
    read_time = time.time() - t0

    # ─ build master ZIP ─
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b in all_bytes:
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix, group_by)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    # ─ stats & download ─
    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    st.markdown("---")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Source PDFs", len(uploads))
    col2.metric("Total pages", sum(len(PdfReader(io.BytesIO(b)).pages) for b in all_bytes))
    # total forms = sum of split counts
    total_forms = sum(len(split_ranges(parse_toc(PdfReader(io.BytesIO(b)), detect_toc_pages(PdfReader(io.BytesIO(b)))), len(PdfReader(io.BytesIO(b)).pages))) for b in all_bytes)
    col3.metric("Total forms", total_forms)
    col4.metric("Initial read", f"{int(read_time//60):02}:{int(read_time%60):02}")

    # ─ live preview ─
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        reader      = PdfReader(io.BytesIO(b))
        total_pages = len(reader.pages)
        entries     = parse_toc(reader, detect_toc_pages(reader))
        splits      = split_ranges(entries, total_pages)

        for title, start, end in splits:
            raw_name     = title
            # only strip ID for filename
            fn_base      = re.sub(r'^#\s*\d+:\s*', '', raw_name) if remove_id_prefix else raw_name
            for rx in patterns:
                fn_base  = re.sub(rx, '', fn_base, flags=re.IGNORECASE)
            fname        = slugify(fn_base)
            arcname      = f"{prefix}{fname}{suffix}.pdf"
            if group_by == "Template":
                folder = slugify(extract_template(raw_name))
            else:
                folder = ""
            rows.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  raw_name,
                "Pages":      f"{start}-{end}",
                "Filename":   arcname
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
