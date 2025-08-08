import re
import io
import time
import zipfile

import streamlit as st
import pandas as pd
import fitz  # PyMuPDF

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── PDF METADATA EXTRACTION ───────────────────────────────────────────────────
def extract_meta(doc: fitz.Document):
    """
    From page 0, find 'Template' line.
    From pages 1+, find 'Category' and 'Location' in the References & Attachments section.
    """
    # Template
    template = "Unknown"
    text0 = doc.load_page(0).get_text().splitlines()
    for line in text0:
        if line.startswith("Template"):
            # handle "Template: value" or "Template    value"
            if ":" in line:
                template = line.split(":", 1)[1].strip()
            else:
                template = line[len("Template"):].strip()
            break

    # Category & Location
    category = location = "Unknown"
    for i in range(1, doc.page_count):
        for line in doc.load_page(i).get_text().splitlines():
            if line.startswith("Category"):
                val = line.split(":", 1)[1].strip() if ":" in line else line[len("Category"):].strip()
                category = val
            elif line.startswith("Location"):
                val = line.split(":", 1)[1].strip() if ":" in line else line[len("Location"):].strip()
                location = val
        if category != "Unknown" and location != "Unknown":
            break

    return template, category, location

# ─── TOC PARSING ───────────────────────────────────────────────────────────────
def detect_toc_pages(doc: fitz.Document) -> list[int]:
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        if entry_rx.search(doc.load_page(i).get_text()):
            pages.append(i)
    return pages

def parse_toc(doc: fitz.Document, pages: list[int]) -> list[tuple[str,int]]:
    """
    Finds lines like:
      "#1234: Title…    15"
    Returns a list of (title, start_page_number).
    """
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries = []
    for pg in pages:
        txt = doc.load_page(pg).get_text() or ""
        for m in toc_rx.finditer(txt):
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

def build_patterns(raw: str) -> list[str]:
    """
    Comma-separated tokens → escaped regex,
    with '*' → '.*' wildcard support.
    """
    pats = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok).replace(r"\*", ".*")
        pats.append(esc)
    return pats

# ─── SINGLE-PDF SPLIT & ZIPPING ────────────────────────────────────────────────
def create_subzip(
    pdf_bytes: bytes,
    patterns: list[str],
    prefix: str,
    suffix: str,
    remove_id: bool,
    group_by: str
):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count

    toc_pages = detect_toc_pages(doc)
    entries   = parse_toc(doc, toc_pages)
    splits    = split_ranges(entries, total)

    tpl, cat, loc = extract_meta(doc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # determine folder path
            if group_by == "Location/Category":
                folder = f"{loc}/{cat}/"
            elif group_by == "Template":
                folder = f"{tpl}/"
            else:
                folder = ""

            # build filename (strip ID only if requested)
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            out_path = f"{folder}{prefix}{fname}{suffix}.pdf"

            # assemble the split PDF
            split_doc = fitz.open()
            for p in range(start-1, end):
                split_doc.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = split_doc.write()
            split_doc.close()

            zf.writestr(out_path, part_bytes)

    buf.seek(0)
    return buf, tpl, cat, loc, splits

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
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only",
    value=True
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Location/Category", "Template"]
)

if uploads:
    patterns = build_patterns(remove_input)

    # --- initial read & stats ---
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs      = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]

    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(len(parse_toc(d, detect_toc_pages(d))) for d in docs)
    elapsed = time.perf_counter() - t0
    m, s = divmod(int(elapsed), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{m:02d}:{s:02d}")

    # --- build master ZIP ---
    def get_master_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as mzip:
            for b in all_bytes:
                subzip, *_ = create_subzip(
                    b, patterns, prefix, suffix, remove_id_prefix, group_by
                )
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        mzip.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = get_master_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # --- preview table ---
    preview = []
    for idx, (b, d) in enumerate(zip(all_bytes, docs)):
        tpl, cat, loc = extract_meta(d)
        splits = split_ranges(parse_toc(d, detect_toc_pages(d)), d.page_count)
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

            preview.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   fn,
            })

    df = pd.DataFrame(preview)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)
