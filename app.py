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

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw: str):
    """Turn comma-separated tokens into regex, treating '*' as wildcard."""
    pats = []
    for tok in [t.strip() for t in raw.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats

def detect_toc_pages(doc):
    """Return 0-based page indices that look like a TOC page."""
    rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    out = []
    for i in range(doc.page_count):
        txt = doc.load_page(i).get_text("text")
        if rx.search(txt):
            out.append(i)
    return out

def parse_toc(doc, toc_pages):
    """
    From each TOC page extract lines like
    '#1234: Title ....... 56' -> (Title, 56)
    """
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = doc.load_page(pg).get_text("text")
        for m in toc_rx.finditer(txt):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def split_ranges(entries, total_pages):
    """Turn [(title,start),...] into [(title,start,end),...]"""
    out = []
    for i, (t, st) in enumerate(entries):
        en = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        out.append((t, st, en))
    return out

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    """
    Split one PDF by its TOC, write into an in-memory ZIP, return BytesIO.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # Use full title for display; strip only for filename if requested
            raw_name = title
            fn_name = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title

            for rx in patterns:
                fn_name = re.sub(rx, '', fn_name, flags=re.IGNORECASE)

            fname = slugify(fn_name)
            out_pdf = fitz.open()
            for p in range(start - 1, end):
                out_pdf.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = out_pdf.write()
            out_pdf.close()

            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part_bytes)
    buf.seek(0)
    doc.close()
    return buf

# ─── UI ───────────────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id = st.checkbox(
    "Remove numeric ID prefix (e.g. '#6849: ') from filenames",
    value=True
)

patterns = build_patterns(remove_input)

if uploads:
    # INITIAL READ & TIMING
    start_time = time.time()
    pdf_bytes_list = [f.read() for f in uploads]
    read_time = time.time() - start_time

    # BUILD PREVIEW DATAFRAME
    rows = []
    total_forms = 0
    total_pages = 0
    for i, b in enumerate(pdf_bytes_list):
        doc = fitz.open(stream=b, filetype="pdf")
        total_pages += doc.page_count
        toc = parse_toc(doc, detect_toc_pages(doc))
        splits = split_ranges(toc, doc.page_count)
        for title, stp, enp in splits:
            total_forms += 1
            # displayed form name always full title
            disp = title
            # filename applies removals and strip-ID
            fn = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title
            for rx in patterns:
                fn = re.sub(rx, '', fn, flags=re.IGNORECASE)
            fn = slugify(fn)
            rows.append({
                "Source PDF": uploads[i].name,
                "Form Name": disp,
                "Pages": f"{stp}-{enp}",
                "Filename": f"{prefix}{fn}{suffix}.pdf"
            })
        doc.close()

    df = pd.DataFrame(rows)

    # SUMMARY & PREVIEW
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    mins, secs = divmod(int(read_time), 60)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)

    # DOWNLOAD BUTTON is disabled until preview is built
    zip_buffer = None
    if st.button("Generate & Download ZIP"):
        with st.spinner("Building ZIP…"):
            master = io.BytesIO()
            with zipfile.ZipFile(master, 'w') as mz:
                for b in pdf_bytes_list:
                    subzip = create_zip(b, patterns, prefix, suffix, remove_id)
                    with zipfile.ZipFile(subzip) as sz:
                        for info in sz.infolist():
                            mz.writestr(info.filename, sz.read(info.filename))
            master.seek(0)
            zip_buffer = master

    if zip_buffer:
        st.success("Done assembling ZIP!")
        st.download_button(
            "Download all splits",
            zip_buffer,
            file_name="acc_build_forms.zip",
            mime="application/zip"
        )
