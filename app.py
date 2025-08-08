import io
import re
import time
import zipfile

import pandas as pd
import streamlit as st
import fitz  # PyMuPDF

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── METADATA EXTRACTION ────────────────────────────────────────────────────────
def extract_meta(doc: fitz.Document):
    """
    - template: from the blue header on page 0, 3rd colon-delimited field
    - category & location: from the References & Attachments table pages 1–4,
      matching lines like 'Category  Electrical > BESS Assembly'
    """
    # 1) TEMPLATE from the first header line on page 0
    page0_lines = doc.load_page(0).get_text().splitlines()
    template = "Unknown"
    for line in page0_lines:
        if line.startswith("#") and ":" in line:
            parts = line.split(":", 2)  # "#id: prefix: template"
            if len(parts) == 3:
                template = parts[2].strip()
            break

    # 2) CATEGORY & LOCATION from pages 1–4
    category = location = "Unknown"
    cat_rx = re.compile(r"Category\s*[:\s]\s*(.+)")
    loc_rx = re.compile(r"Location\s*[:\s]\s*(.+)")
    for pnum in range(1, min(doc.page_count, 5)):
        for line in doc.load_page(pnum).get_text().splitlines():
            m1 = cat_rx.match(line)
            if m1:
                category = m1.group(1).strip()
            m2 = loc_rx.match(line)
            if m2:
                location = m2.group(1).strip()
        if category != "Unknown" and location != "Unknown":
            break

    return template, category, location

# ─── TOC PARSING ───────────────────────────────────────────────────────────────
def detect_toc_pages(doc: fitz.Document):
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if entry_rx.search(text):
            pages.append(i + 1)
    return pages

def parse_toc(doc: fitz.Document, toc_pages: list[int]):
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text() or ""
        for m in toc_rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def split_ranges(entries: list[tuple[str,int]], total: int):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total
        out.append((title, start, end))
    return out

# ─── FILENAME SANITIZATION ────────────────────────────────────────────────────
def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")

def build_patterns(raw: str):
    pats = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*")
        pats.append(esc)
    return pats

# ─── SINGLE-PDF SPLIT & ZIPPING ────────────────────────────────────────────────
def create_subzip(
    pdf_bytes: bytes,
    patterns: list[str],
    prefix: str,
    suffix: str,
    remove_id: bool,
    group_by: str,
):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count

    toc_pages = detect_toc_pages(doc)
    entries   = parse_toc(doc, toc_pages)
    splits    = split_ranges(entries, total_pages)

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

            # form name (display only)
            form_name = title

            # build raw filename
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # assemble page range
            new_doc = fitz.open()
            for p in range(start-1, end):
                new_doc.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = new_doc.write()
            new_doc.close()

            zf.writestr(out_name, part_bytes)

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ─────────────────────────────────────────────────────────────
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)

remove_input     = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix           = st.text_input("Filename prefix", "")
suffix           = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from **filenames only**",
    value=True,
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Location/Category", "Template"],
)

if uploads:
    # build regex list
    patterns = build_patterns(remove_input)

    # initial read timing
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs      = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]

    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(
        len(parse_toc(d, detect_toc_pages(d)))
        for d in docs
    )
    elapsed = time.perf_counter() - t0
    mins, secs = divmod(int(elapsed), 60)

    # summary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # assemble master ZIP on‐demand
    def get_master_zip():
        mz_buf = io.BytesIO()
        with zipfile.ZipFile(mz_buf, "w") as mz:
            for b in all_bytes:
                subzip = create_subzip(
                    b, patterns, prefix, suffix, remove_id_prefix, group_by
                )
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        mz.writestr(info.filename, sz.read(info.filename))
        mz_buf.seek(0)
        return mz_buf

    zip_buf = get_master_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # live preview table
    preview_rows = []
    for idx, (b, d) in enumerate(zip(all_bytes, docs)):
        tpl, cat, loc = extract_meta(d)
        splits = split_ranges(
            parse_toc(d, detect_toc_pages(d)),
            d.page_count
        )

        for title, start, end in splits:
            if group_by == "Location/Category":
                folder = f"{loc} / {cat}"
            elif group_by == "Template":
                folder = tpl
            else:
                folder = ""

            base = title
            if remove_id_prefix:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            fn = f"{prefix}{fname}{suffix}.pdf"

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   fn,
            })

    df = pd.DataFrame(preview_rows)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)
