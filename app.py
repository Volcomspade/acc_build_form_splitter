import re
import io
import zipfile

import streamlit as st
import pandas as pd
import fitz  # PyMuPDF

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(doc: fitz.Document) -> list[int]:
    """Return a list of 1-based page numbers that contain TOC entries (#1234:)."""
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if entry_rx.search(text):
            pages.append(i + 1)
    return pages

def parse_toc(doc: fitz.Document, toc_pages: list[int]) -> list[tuple[str,int]]:
    """
    From each TOC page, extract lines like:
      # 7893: Form Name ........ 15
    Returns [(title, start_page), ...].
    """
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text()
        for m in toc_rx.finditer(text):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries

def split_ranges(entries: list[tuple[str,int]], total_pages: int) -> list[tuple[str,int,int]]:
    """
    Given [(title, start), ...], compute [(title, start, end), ...]
    where end is one page before the next start, or total_pages.
    """
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        out.append((title, start, end))
    return out

def slugify(name: str) -> str:
    """Sanitize a string for use as a filename."""
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw_input: str) -> list[str]:
    """
    Turn comma-separated tokens into regex patterns.
    '*' in the token becomes '.*', everything else is literal-escaped.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats

def create_zip(
    pdf_bytes: bytes,
    patterns: list[str],
    prefix: str,
    suffix: str,
    remove_id: bool
) -> io.BytesIO:
    """
    Split one PDF (given as bytes) into many parts per its TOC, apply
    pattern removals, prefix/suffix, and pack into an in-memory ZIP.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    toc_pages = detect_toc_pages(doc)
    entries   = parse_toc(doc, toc_pages)
    splits    = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # compute base name (strip "#1234: " only in filename if requested)
            name_for_file = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title
            # apply user patterns
            for rx in patterns:
                name_for_file = re.sub(rx, '', name_for_file, flags=re.IGNORECASE)
            fname = slugify(name_for_file)

            # build a new PDF with just pages [start-1 .. end-1]
            new_doc = fitz.open()
            for p in range(start-1, end):
                new_doc.insert_pdf(doc, from_page=p, to_page=p)
            pdf_out = new_doc.write()  # bytes

            zf.writestr(f"{prefix}{fname}{suffix}.pdf", pdf_out)

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix       = st.text_input("Filename prefix", "")
suffix       = st.text_input("Filename suffix", "")
remove_id    = st.checkbox("Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames", value=True)

patterns = build_patterns(remove_input)

if uploads:
    # Read all uploads into memory once
    all_bytes = [f.read() for f in uploads]

    # --- Build master ZIP ---
    master_buf = io.BytesIO()
    with zipfile.ZipFile(master_buf, 'w') as mz:
        for pdf_bytes, up in zip(all_bytes, uploads):
            subzip = create_zip(pdf_bytes, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(subzip) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master_buf.seek(0)

    st.download_button(
        "Download all splits",
        master_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # --- Preview table ---
    st.subheader("Filename & Page-Range Preview")

    preview_rows = []
    for pdf_bytes, up in zip(all_bytes, uploads):
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total = doc.page_count
        entries = parse_toc(doc, detect_toc_pages(doc))
        splits  = split_ranges(entries, total)

        for title, start, end in splits:
            # show full form name (including #ID)
            form_name = title
            # filename
            nm = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title
            for rx in patterns:
                nm = re.sub(rx, '', nm, flags=re.IGNORECASE)
            slug = slugify(nm)

            preview_rows.append({
                "Source PDF": up.name,
                "Form Name":  form_name,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{slug}{suffix}.pdf",
            })

    df = pd.DataFrame(preview_rows)
    st.dataframe(df, use_container_width=True)
