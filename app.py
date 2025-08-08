import io
import re
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

# ─── METADATA EXTRACTION ────────────────────────────────────────────────────────
def extract_meta(doc: fitz.Document):
    """
    Returns (template, category, location).
    - Template: from first page under "Forms" table (look for line starting "Template")
    - Category & Location: from pages 1+ under "References and Attachments"
    """
    # 1) Template on page 0
    template = "Unknown"
    for line in doc.load_page(0).get_text().splitlines():
        if line.strip().startswith("Template"):
            parts = line.split(":", 1)
            template = parts[1].strip() if len(parts) == 2 else line.strip()[len("Template"):].strip()
            break

    # 2) Category & Location on subsequent pages
    category = location = "Unknown"
    for p in range(1, doc.page_count):
        for line in doc.load_page(p).get_text().splitlines():
            line = line.strip()
            if line.startswith("Category"):
                parts = line.split(":", 1)
                category = parts[1].strip() if len(parts) == 2 else line[len("Category"):].strip()
            elif line.startswith("Location"):
                parts = line.split(":", 1)
                location = parts[1].strip() if len(parts) == 2 else line[len("Location"):].strip()
        if category != "Unknown" and location != "Unknown":
            break

    # clean parentheses if present
    category = re.sub(r'^\(|\)$', '', category).strip()
    location = re.sub(r'^\(|\)$', '', location).strip()

    return template, category, location

# ─── TOC PARSING ───────────────────────────────────────────────────────────────
def detect_toc_pages(doc: fitz.Document):
    rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    return [i for i in range(doc.page_count) if rx.search(doc.load_page(i).get_text())]

def parse_toc(doc: fitz.Document, toc_pages: list[int]):
    rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries = []
    for i in toc_pages:
        text = doc.load_page(i).get_text()
        for m in rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def split_ranges(entries, total):
    out = []
    for idx, (title, start) in enumerate(entries):
        end = entries[idx+1][1] - 1 if idx+1 < len(entries) else total
        out.append((title, start, end))
    return out

# ─── FILENAME CLEANUP ──────────────────────────────────────────────────────────
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

# ─── SPLIT + ZIP A SINGLE PDF ──────────────────────────────────────────────────
def create_subzip(
    pdf_bytes: bytes,
    patterns: list[str],
    prefix: str,
    suffix: str,
    remove_id: bool,
    group_by: str,
):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total)

    tpl, cat, loc = extract_meta(doc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # choose folder
            if group_by == "Location/Category":
                folder = f"{location}/{category}/"
            elif group_by == "Template":
                folder = f"{tpl}/"
            else:
                folder = ""

            # raw title for filename
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # extract pages
            new_doc = fitz.open()
            for p in range(start-1, end):
                new_doc.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = new_doc.write()
            new_doc.close()

            zf.writestr(out_name, part_bytes)

    buf.seek(0)
    return buf, tpl, cat, loc, splits

# ─── STREAMLIT UI ─────────────────────────────────────────────────────────────
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only",
    value=True,
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Location/Category", "Template"],
)

if uploads:
    patterns = build_patterns(remove_input)

    # initial read + metrics
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(len(parse_toc(d, detect_toc_pages(d))) for d in docs)
    elapsed = time.perf_counter() - t0
    mins, secs = divmod(int(elapsed), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # build master ZIP
    def get_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as zfm:
            for b in all_bytes:
                subzip, *_ = create_subzip(b, patterns, prefix, suffix, remove_id_prefix, group_by)
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        zfm.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = get_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # live preview
    rows = []
    for idx, d in enumerate(docs):
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
            filename = f"{prefix}{fname}{suffix}.pdf"
            rows.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   filename,
            })

    df = pd.DataFrame(rows)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)
