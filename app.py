# app.py
import io
import re
import time
import zipfile

import pandas as pd
import streamlit as st
import fitz  # PyMuPDF

# ────────────────────────── STREAMLIT CONFIG ──────────────────────────
st.set_page_config(page_title="ACC Build TOC Splitter", layout="wide")
st.title("ACC Build TOC Splitter")

# ──────────────────────── UTILITIES / REGEX HELPERS ───────────────────
TOC_ENTRY_RX = re.compile(r"^#\s*\d+:", re.MULTILINE)
TOC_PARSE_RX = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)  # non-greedy title
ID_PREFIX_RX = re.compile(r"^#\s*\d+:\s*")

def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")

def build_patterns(raw: str):
    """
    Turn comma-separated tokens into literal-escaped regex patterns,
    then replace \* with .* to support wildcards.
    """
    pats = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*")
        pats.append(esc)
    return pats

# ────────────────────────── METADATA EXTRACTION ───────────────────────
def extract_meta(doc: fitz.Document):
    """
    Returns (template, category, location).

    - Template: from page 0 (the 'Forms' table).
    - Category + Location: from the page containing 'References and Attachments'.
    """
    template = "Unknown"
    category = "Unknown"
    location = "Unknown"

    # --- Template from first page (Forms section) ---
    try:
        lines = (doc.load_page(0).get_text() or "").splitlines()
        for i, ln in enumerate(lines):
            ln = ln.strip()
            if ln.startswith("Template"):
                # 'Template: value' OR 'Template' \n 'value'
                if ":" in ln:
                    candidate = ln.split(":", 1)[1].strip()
                    if candidate:
                        template = candidate
                        break
                # fallback: look to next non-header line
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt and not nxt.endswith(":"):
                    template = nxt
                break
    except Exception:
        pass

    # --- Category / Location from "References and Attachments" page ---
    try:
        ref_pg = None
        for p in range(1, doc.page_count):
            if "References and Attachments" in (doc.load_page(p).get_text() or ""):
                ref_pg = p
                break
        if ref_pg is not None:
            rlines = [x.strip() for x in (doc.load_page(ref_pg).get_text() or "").splitlines()]
            for i, ln in enumerate(rlines):
                if ln == "Category" and i + 1 < len(rlines):
                    val = rlines[i + 1].strip()
                    if val and not val.endswith(":"):
                        category = val
                if ln == "Location" and i + 1 < len(rlines):
                    val = rlines[i + 1].strip()
                    if val and not val.endswith(":"):
                        location = val
    except Exception:
        pass

    return template, category, location

# ───────────────────────────── TOC PROCESSING ─────────────────────────
def detect_toc_pages(doc: fitz.Document):
    pages = []
    for i in range(doc.page_count):
        if TOC_ENTRY_RX.search(doc.load_page(i).get_text() or ""):
            pages.append(i + 1)  # 1-based
    return pages

def parse_toc(doc: fitz.Document, toc_pages):
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text() or ""
        for m in TOC_PARSE_RX.finditer(text):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries

def split_ranges(entries, total_pages):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out

# ───────────────────────── SPLITTING / ZIPPING ────────────────────────
def create_subzip(pdf_bytes, patterns, prefix, suffix, remove_id, group_by):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count
    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total_pages)

    tpl, cat, loc = extract_meta(doc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # decide folder path
            if group_by == "Location/Category":
                folder = f"{loc}/{cat}/"
            elif group_by == "Template":
                folder = f"{tpl}/"
            else:
                folder = ""

            # filename base: optionally strip '#1234: ' then apply removals
            base = ID_PREFIX_RX.sub("", title) if remove_id else title
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)

            fname = slugify(base)
            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # assemble page range (inclusive indices for PyMuPDF)
            part = fitz.open()
            part.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            zf.writestr(out_name, part.tobytes())
            part.close()

    doc.close()
    buf.seek(0)
    return buf

# ──────────────────────────────── UI ──────────────────────────────────
uploads = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox("Remove numeric ID prefix (e.g. ‘#6849: ’) from **filenames only**", value=True)
group_by = st.selectbox("Group files in ZIP by:", ["None", "Location/Category", "Template"])

patterns = build_patterns(remove_input)

with st.expander("ℹ️ Pattern tips"):
    st.markdown(
        """
- **Wildcard**: `*` → e.g. `03.*_` removes `03.04_`, `03.03_`, etc.
- **Multiple**: comma separate → e.g. `03.*_, L2\\s*`
- For `L2_`: the underscore appears **after** slugify; match the raw title with a space or regex:  
  use `L2 ` (with a space) **or** `L2\\s*` (regex).
"""
    )

if uploads:
    # Initial read – do it once for perf
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]

    total_pages = sum(d.page_count for d in docs)
    total_forms = 0
    for d in docs:
        total_forms += len(parse_toc(d, detect_toc_pages(d)))

    for d in docs:
        d.close()

    elapsed = time.perf_counter() - t0
    mm, ss = divmod(int(elapsed), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mm:02d}:{ss:02d}")

    # Build master zip on demand
    def get_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as mzf:
            for b in all_bytes:
                sub, = (create_subzip(b, patterns, prefix, suffix, remove_id_prefix, group_by),)
                with zipfile.ZipFile(sub) as sz:
                    for info in sz.infolist():
                        mzf.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = get_zip()
    st.download_button("Download all splits", zip_buf, file_name="acc_build_forms.zip", mime="application/zip")

    # Live preview table
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        d = fitz.open(stream=b, filetype="pdf")
        splits = split_ranges(parse_toc(d, detect_toc_pages(d)), d.page_count)
        tpl, cat, loc = extract_meta(d)

        for title, start, end in splits:
            if group_by == "Location/Category":
                folder = f"{loc} / {cat}"
            elif group_by == "Template":
                folder = tpl
            else:
                folder = ""

            base = ID_PREFIX_RX.sub("", title) if remove_id_prefix else title
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            fn = f"{prefix}{fname}{suffix}.pdf"

            rows.append(
                {
                    "Source PDF": uploads[idx].name,
                    "Folder": folder or "(none)",
                    "Form Name": title,
                    "Pages": f"{start}-{end}",
                    "Filename": fn,
                }
            )
        d.close()

    st.dataframe(pd.DataFrame(rows), use_container_width=True)
