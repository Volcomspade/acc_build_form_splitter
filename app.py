import re
import io
import zipfile
import time

import streamlit as st
import pandas as pd
import fitz  # PyMuPDF

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(doc):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    toc_pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if entry_rx.search(text):
            # pages are 1-based in TOC
            toc_pages.append(i + 1)
    return toc_pages

def parse_toc(doc, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg-1).get_text()
        for m in toc_rx.finditer(text):
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

def build_patterns(raw):
    pats = []
    for tok in [t.strip() for t in raw.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*?')  # non-greedy wildcard
        pats.append(esc)
    return pats

def extract_template(title):
    parts = title.split(':', 2)
    return parts[2].strip() if len(parts) == 3 else title

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id, group_by):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            raw_title = title
            fn_base = re.sub(r'^#\s*\d+:\s*', '', raw_title) if remove_id else raw_title
            for rx in patterns:
                fn_base = re.sub(rx, '', fn_base, flags=re.IGNORECASE)
            fname = slugify(fn_base)
            arcname = f"{prefix}{fname}{suffix}.pdf"

            if group_by == "Template":
                folder = slugify(extract_template(raw_title))
                arcname = f"{folder}/{arcname}"

            # extract pages via PyMuPDF
            new_doc = fitz.open()
            for p in range(start-1, end):
                new_doc.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = new_doc.write()
            zf.writestr(arcname, part_bytes)

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads          = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_input     = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix           = st.text_input("Filename prefix", "")
suffix           = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox("Remove numeric ID prefix (e.g. ‘#6849: ’) from **filenames** only", value=True)
group_by         = st.selectbox("Group files in ZIP by:", ["None", "Template"])

patterns = build_patterns(remove_input)

if uploads:
    t0 = time.time()
    all_bytes = [f.read() for f in uploads]
    read_time = time.time() - t0

    # build ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b in all_bytes:
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix, group_by)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button("Download all splits", master, file_name="acc_build_forms.zip", mime="application/zip")

    # stats
    st.markdown("---")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    total_pages = sum(fitz.open(stream=b, filetype="pdf").page_count for b in all_bytes)
    c2.metric("Total pages", total_pages)
    total_forms = sum(len(split_ranges(parse_toc(fitz.open(stream=b, filetype="pdf"), detect_toc_pages(fitz.open(stream=b, filetype="pdf"))), fitz.open(stream=b, filetype="pdf").page_count)) for b in all_bytes)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{int(read_time//60):02}:{int(read_time%60):02}")

    # preview
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        doc = fitz.open(stream=b, filetype="pdf")
        total = doc.page_count
        entries = parse_toc(doc, detect_toc_pages(doc))
        splits = split_ranges(entries, total)
        for title, start, end in splits:
            raw_title = title
            fn_base = re.sub(r'^#\s*\d+:\s*', '', raw_title) if remove_id_prefix else raw_title
            for rx in patterns:
                fn_base = re.sub(rx, '', fn_base, flags=re.IGNORECASE)
            fname = slugify(fn_base)
            arcname = f"{prefix}{fname}{suffix}.pdf"
            folder = slugify(extract_template(raw_title)) if group_by=="Template" else ""
            rows.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  raw_title,
                "Pages":      f"{start}-{end}",
                "Filename":   arcname
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
