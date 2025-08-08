import re
import io
import zipfile
import time

import fitz  # PyMuPDF
import streamlit as st
import pandas as pd

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter v2.0-beta",
    layout="wide",
)

st.title("ACC Build TOC Splitter 2.0-Beta 🚀")

# ─── REGEX HELPER ──────────────────────────────────────────────────────────────
with st.expander("❓ Regex & Wildcard Help", expanded=False):
    st.markdown("""
- Separate multiple patterns with commas.
- Use `*` as a wildcard (matches any sequence of characters).  
  - E.g. to remove all `03.02_`, `02.03_`, etc: enter `0*.0*_`
- You can also enter raw regex, e.g. `03\.0[2-4]_`  
- Patterns are applied _after_ optional ID stripping from filenames.
""")

# ─── PDF PARSING LOGIC ─────────────────────────────────────────────────────────
def detect_toc_pages(doc):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if entry_rx.search(text):
            pages.append(i)
    return pages

def parse_toc(doc, toc_page_indices):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pi in toc_page_indices:
        text = doc.load_page(pi).get_text()
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

def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input: str):
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats

@st.cache_data(show_spinner=False)
def parse_pdf(pdf_bytes: bytes):
    """Parse TOC and compute splits; cache results."""
    t0 = time.time()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total)
    duration = time.time() - t0
    return {
        "total_pages": total,
        "splits": splits,
        "parse_time": duration
    }

def create_zip(pdf_bytes: bytes, splits, prefix, suffix, patterns, remove_id):
    """Split via PyMuPDF and package into ZIP buffer."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            name = title
            if remove_id:
                name = re.sub(r'^#\s*\d+:\s*', '', name)
            for rx in patterns:
                name = re.sub(rx, "", name, flags=re.IGNORECASE)
            fname = slugify(name)
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=start-1, to_page=end-1)
            part_bytes = new_doc.write()
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part_bytes)
    buf.seek(0)
    return buf

# ─── UI INPUTS ─────────────────────────────────────────────────────────────────
uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix       = st.text_input("Filename prefix", "")
suffix       = st.text_input("Filename suffix", "")
remove_id    = st.checkbox("Remove numeric ID prefix from filenames only", value=True)
patterns     = build_patterns(remove_input)

if uploads:
    st.markdown("### 🔍 Parsing PDFs")
    parse_progress = st.progress(0)
    parse_results = []
    total_forms = 0
    total_parse_time = 0.0

    for idx, f in enumerate(uploads):
        bytes_ = f.read()
        res = parse_pdf(bytes_)
        res["name"] = f.name
        res["bytes"] = bytes_
        parse_results.append(res)
        total_forms += len(res["splits"])
        total_parse_time += res["parse_time"]
        parse_progress.progress((idx + 1) / len(uploads))

    mins = int(total_parse_time // 60)
    secs = int(total_parse_time % 60)
    st.success(f"Parsed **{len(uploads)}** PDF(s), **{total_forms}** forms in {mins:02d}:{secs:02d}")

    # ─── DOWNLOAD ZIP ─────────────────────────────────────────────────────────────
    st.markdown("### 💾 Build & Download ZIP")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as mz:
        for info in parse_results:
            sub = create_zip(
                info["bytes"],
                info["splits"],
                prefix,
                suffix,
                patterns,
                remove_id
            )
            with zipfile.ZipFile(sub) as sz:
                for zi in sz.infolist():
                    mz.writestr(zi.filename, sz.read(zi.filename))
    zip_buf.seek(0)
    st.download_button(
        "Download all splits as ZIP",
        zip_buf,
        file_name="acc_build_forms_v2.0-beta.zip",
        mime="application/zip",
    )

    # ─── LIVE PREVIEW ─────────────────────────────────────────────────────────────
    st.markdown("### 📄 Filename & Page Range Preview")
    rows = []
    for info in parse_results:
        for title, start, end in info["splits"]:
            name = title
            if remove_id:
                name = re.sub(r'^#\s*\d+:\s*', '', name)
            for rx in patterns:
                name = re.sub(rx, "", name, flags=re.IGNORECASE)
            fname = slugify(name)
            rows.append({
                "Source PDF": info["name"],
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf",
            })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
