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

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i+1 for i, p in enumerate(reader.pages)
            if entry_rx.search(p.extract_text() or "")]

def parse_toc(reader, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = reader.pages[pg-1].extract_text() or ""
        for m in toc_rx.finditer(txt):
            entries.append((m.group(1).strip(), int(m.group(2))))
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
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats

def extract_metadata(reader, page_no):
    """
    From the form’s first page (page_no),
    grab the Template, Location, and Category fields under “Forms”.
    """
    text = reader.pages[page_no-1].extract_text() or ""
    meta = {"Template": "Unknown", "Location": "Unknown", "Category": "Unknown"}
    # look for lines like "Template  03.04 Exhibit H-3 - Exhibit H-4 ACC Build"
    for line in text.splitlines():
        for key in meta:
            if line.startswith(key):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    meta[key] = parts[1].strip()
    return meta

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id, group_by):
    reader     = PdfReader(io.BytesIO(pdf_bytes))
    total      = len(reader.pages)
    toc_pages  = detect_toc_pages(reader)
    entries    = parse_toc(reader, toc_pages)
    splits     = split_ranges(entries, total)
    buf        = io.BytesIO()

    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # raw title always includes the leading "#1234: …"
            raw_title = title

            # file‐name title: optionally strip the "#1234: " prefix
            file_title = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title

            # apply remove‐patterns
            for rx in patterns:
                file_title = re.sub(rx, '', file_title, flags=re.IGNORECASE)

            fname = slugify(file_title)
            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            # determine folder based on group_by
            if group_by == "Location > Category":
                # metadata is on page start
                meta = extract_metadata(reader, start)
                folder = f"{slugify(meta['Location'])}/{slugify(meta['Category'])}"
            else:  # Template
                meta = extract_metadata(reader, start)
                folder = slugify(meta['Template'])

            arcname = f"{folder}/{prefix}{fname}{suffix}.pdf"
            zf.writestr(arcname, part.read())

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads      = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix       = st.text_input("Filename prefix", "")
suffix       = st.text_input("Filename suffix", "")
remove_id    = st.checkbox("Remove numeric ID prefix (e.g. ‘#6849: ’)", value=True)
group_by     = st.selectbox("Group files in ZIP by:", ["Location > Category", "Template"])

patterns = build_patterns(remove_input)

if uploads:
    # read & time
    start_time = time.time()
    all_bytes  = [f.read() for f in uploads]
    read_time  = time.time() - start_time

    # build & time
    zip_start  = time.time()
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b in all_bytes:
            sub = create_zip(b, patterns, prefix, suffix, remove_id, group_by)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)
    zip_time = time.time() - zip_start

    # summary
    toc_counts = []
    for b in all_bytes:
        r = PdfReader(io.BytesIO(b))
        toc_pages = detect_toc_pages(r)
        toc_counts += parse_toc(r, toc_pages)
    total_forms = len(toc_counts)
    total_pages = sum(len(PdfReader(io.BytesIO(b)).pages) for b in all_bytes)

    # display
    st.download_button("Download all splits", master, file_name="acc_build_forms.zip", mime="application/zip")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    mins, secs = divmod(int(read_time), 60)
    c4.metric("Initial read", f"{mins}:{secs:02d}")

    # live preview
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        reader      = PdfReader(io.BytesIO(b))
        total_p     = len(reader.pages)
        entries     = parse_toc(reader, detect_toc_pages(reader))
        splits      = split_ranges(entries, total_p)

        for title, start, end in splits:
            file_title = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title
            for rx in patterns:
                file_title = re.sub(rx, '', file_title, flags=re.IGNORECASE)
            fname  = slugify(file_title)
            meta   = extract_metadata(reader, start)
            if group_by == "Location > Category":
                folder = f"{meta['Location']} > {meta['Category']}"
            else:
                folder = meta['Template']
            rows.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf"
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
