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
    # remove illegal filename chars, collapse spaces → underscores
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw: str):
    """
    Turn comma-separated tokens into regex patterns,
    treating '*' as wildcard.
    """
    pats = []
    for tok in [t.strip() for t in raw.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats

def detect_toc_pages(doc):
    """Return list of page indices that look like TOC pages."""
    rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    out = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text("text")
        if rx.search(text):
            out.append(i)
    return out

def parse_toc(doc, toc_pages):
    """
    From each TOC page extract lines of the form:
      #1234: Title ....... 56
    returning [(Title, 56), ...].
    """
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg).get_text("text")
        for m in toc_rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def split_ranges(entries, total_pages):
    """Given [(title,start),...], return [(title,start,end),...]."""
    out = []
    for idx, (title, st) in enumerate(entries):
        en = entries[idx+1][1] - 1 if idx+1 < len(entries) else total_pages
        out.append((title, st, en))
    return out

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    """
    Split one PDF by TOC, pack into an in-memory ZIP, return BytesIO.
    Patterns are applied to the slugified filename only.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, doc.page_count)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # strip "#1234: " from filename only if requested
            raw_fn = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title
            # slugify before removal
            fname = slugify(raw_fn)
            # apply each removal pattern against slugified fname
            for rx in patterns:
                fname = re.sub(rx, '', fname, flags=re.IGNORECASE)
            # assemble the slice
            out_pdf = fitz.open()
            for p in range(start - 1, end):
                out_pdf.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = out_pdf.write()
            out_pdf.close()
            # write into ZIP
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part_bytes)

    buf.seek(0)
    doc.close()
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

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
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames",
    value=True
)

patterns = build_patterns(remove_input)

if uploads:
    # ─ INITIAL READ & TIMING ────────────────────────────────────────────────
    t0 = time.time()
    pdf_bytes_list = [f.read() for f in uploads]
    read_secs = time.time() - t0

    # ─ BUILD PREVIEW ROWS ───────────────────────────────────────────────────
    rows = []
    total_forms = 0
    total_pages = 0

    for idx, b in enumerate(pdf_bytes_list):
        doc = fitz.open(stream=b, filetype="pdf")
        total_pages += doc.page_count
        toc = parse_toc(doc, detect_toc_pages(doc))
        splits = split_ranges(toc, doc.page_count)
        for title, stp, enp in splits:
            total_forms += 1
            # Form Name always full title
            disp = title
            # Filename: strip ID if asked, then slugify & apply removals
            raw_fn = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title
            fname = slugify(raw_fn)
            for rx in patterns:
                fname = re.sub(rx, '', fname, flags=re.IGNORECASE)

            rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name":  disp,
                "Pages":      f"{stp}-{enp}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf"
            })
        doc.close()

    df = pd.DataFrame(rows)

    # ─ SHOW SUMMARY ─────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    m, s = divmod(int(read_secs), 60)
    c4.metric("Initial read", f"{m:02d}:{s:02d}")

    # ─ SHOW PREVIEW ─────────────────────────────────────────────────────────
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)

    # ─ ZIP BUTTON ────────────────────────────────────────────────────────────
    zip_buf = None
    if st.button("Generate & Download ZIP"):
        with st.spinner("Building your ZIP…"):
            master = io.BytesIO()
            with zipfile.ZipFile(master, 'w') as mz:
                for b in pdf_bytes_list:
                    sub = create_zip(b, patterns, prefix, suffix, remove_id)
                    with zipfile.ZipFile(sub) as sz:
                        for info in sz.infolist():
                            mz.writestr(info.filename, sz.read(info.filename))
            master.seek(0)
            zip_buf = master

    if zip_buf:
        st.success("✅ ZIP is ready")
        st.download_button(
            "Download all splits",
            zip_buf,
            file_name="acc_build_forms.zip",
            mime="application/zip"
        )
