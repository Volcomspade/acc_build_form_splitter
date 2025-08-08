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
    """Sanitize a string for use as a filename."""
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
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text("text")
        if rx.search(text):
            pages.append(i)
    return pages

def parse_toc(doc, toc_pages):
    """
    From each TOC page extract lines of the form:
      #1234: Title ....... 56
    returning [(Title, 56), ...].
    """
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    out = []
    for pg in toc_pages:
        text = doc.load_page(pg).get_text("text")
        for m in toc_rx.finditer(text):
            out.append((m.group(1).strip(), int(m.group(2))))
    return out

def split_ranges(entries, total_pages):
    """Given [(title,start),...], return [(title,start,end),...]."""
    out = []
    for idx, (t, st) in enumerate(entries):
        en = entries[idx+1][1] - 1 if idx+1 < len(entries) else total_pages
        out.append((t, st, en))
    return out

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix      = st.text_input("Filename prefix", "")
suffix      = st.text_input("Filename suffix", "")
remove_id   = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames",
    value=True
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Template"]
)

patterns = build_patterns(remove_input)

if uploads:
    # ─ INITIAL READ & TIMING ────────────────────────────────────────────────
    t0 = time.time()
    pdf_bytes_list = [f.read() for f in uploads]
    read_secs = time.time() - t0

    # ─ COLLECT ALL FORM SLICES ──────────────────────────────────────────────
    slices = []
    total_pages = 0
    for idx, b in enumerate(pdf_bytes_list):
        doc = fitz.open(stream=b, filetype="pdf")
        total_pages += doc.page_count
        toc_pages = detect_toc_pages(doc)
        entries   = parse_toc(doc, toc_pages)
        splits    = split_ranges(entries, doc.page_count)

        for title, stp, enp in splits:
            # derive 'template' (everything after the last colon)
            template = title.split(":")[-1].strip()
            # store one slice record
            slices.append({
                "pdf_index": idx,
                "source":    uploads[idx].name,
                "title":     title,
                "template":  template,
                "start":     stp,
                "end":       enp
            })
        doc.close()

    total_forms = len(slices)

    # ─ SHOW SUMMARY ─────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    m, s = divmod(int(read_secs), 60)
    c4.metric("Initial read", f"{m:02d}:{s:02d}")

    # ─ BUILD PREVIEW DATAFRAME ──────────────────────────────────────────────
    rows = []
    for sl in slices:
        # display name always unmodified
        disp = sl["title"]

        # build filename: strip ID?, slugify, apply removals
        raw_fn = re.sub(r'^#\s*\d+:\s*', '', sl["title"]) if remove_id else sl["title"]
        fname  = slugify(raw_fn)
        for rx in patterns:
            fname = re.sub(rx, '', fname, flags=re.IGNORECASE)
        out_fn = f"{prefix}{fname}{suffix}.pdf"

        # decide folder
        folder = ""
        if group_by == "Template":
            folder = slugify(sl["template"])

        rows.append({
            "Source PDF": sl["source"],
            "Folder":     folder,
            "Form Name":  disp,
            "Pages":      f"{sl['start']}-{sl['end']}",
            "Filename":   out_fn
        })

    df = pd.DataFrame(rows)

    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)

    # ─ GENERATE & DOWNLOAD ZIP ─────────────────────────────────────────────
    if st.button("Generate & Download ZIP"):
        with st.spinner("Assembling your ZIP…"):
            master = io.BytesIO()
            with zipfile.ZipFile(master, "w") as mz:
                for sl in slices:
                    idx, stp, enp = sl["pdf_index"], sl["start"], sl["end"]
                    doc = fitz.open(stream=pdf_bytes_list[idx], filetype="pdf")
                    # slice pages
                    out = fitz.open()
                    for p in range(stp - 1, enp):
                        out.insert_pdf(doc, from_page=p, to_page=p)
                    data = out.write()
                    out.close(); doc.close()

                    # compute filename again (same logic as preview)
                    raw_fn = re.sub(r'^#\s*\d+:\s*', '', sl["title"]) if remove_id else sl["title"]
                    fname  = slugify(raw_fn)
                    for rx in patterns:
                        fname = re.sub(rx, '', fname, flags=re.IGNORECASE)
                    out_fn = f"{prefix}{fname}{suffix}.pdf"

                    # build path inside zip
                    path = out_fn
                    if group_by == "Template":
                        path = f"{slugify(sl['template'])}/{out_fn}"

                    mz.writestr(path, data)

            master.seek(0)
            st.success("✅ ZIP is ready!")
            st.download_button(
                "Download all splits",
                master,
                file_name="acc_build_forms.zip",
                mime="application/zip"
            )
