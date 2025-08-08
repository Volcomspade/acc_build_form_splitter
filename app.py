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
    Returns (template, category, location).
    - template: from page 0's "Template: ..." line
    - category, location: from the References & Attachments table on pages 1+
    """
    # ─ Template on page 0 ─────────────────
    template = "Unknown"
    for line in doc.load_page(0).get_text().splitlines():
        l = line.strip()
        if l.lower().startswith("template"):
            parts = l.split(":", 1)
            if len(parts) == 2:
                template = parts[1].strip()
            break

    # ─ Category & Location on pages 1+ ────
    category = location = "Unknown"
    for i in range(1, doc.page_count):
        for line in doc.load_page(i).get_text().splitlines():
            l = line.strip()
            # match "Category" even if there's no colon
            if l.lower().startswith("category"):
                val = l[len("Category"):].strip(" :")
                if val:
                    category = val
            elif l.lower().startswith("location"):
                val = l[len("Location"):].strip(" :")
                if val:
                    location = val
        # stop early once both found
        if category != "Unknown" and location != "Unknown":
            break

    return template, category, location

# ─── TOC PARSING ───────────────────────────────────────────────────────────────
def detect_toc_pages(doc: fitz.Document):
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        if entry_rx.search(doc.load_page(i).get_text()):
            pages.append(i + 1)
    return pages

def parse_toc(doc: fitz.Document, toc_pages: list[int]):
    # capture the full "#1234: Title ..." as group(1)
    toc_rx = re.compile(r"(#\s*\d+:\s*.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text() or ""
        for m in toc_rx.finditer(text):
            raw = m.group(1).strip()
            start = int(m.group(2))
            entries.append((raw, start))
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
        # non-greedy wildcard
        esc = esc.replace(r"\*", ".*?")
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
            # ─ choose folder path ───────
            if group_by == "Location/Category":
                folder = f"{loc}/{cat}/"
            elif group_by == "Template":
                folder = f"{tpl}/"
            else:
                folder = ""

            # ─ build base filename ──────
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)

            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # ─ extract that page range ───
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
    patterns = build_patterns(remove_input)

    # ─ initial read & timing ─────────
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs      = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(
        len(parse_toc(d, detect_toc_pages(d)))
        for d in docs
    )
    elapsed = time.perf_counter() - t0
    m, s = divmod(int(elapsed), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{m:02d}:{s:02d}")

    # ─ generate master ZIP ─────────────
    def get_master_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as out_z:
            for b in all_bytes:
                subzip, *_ = create_subzip(
                    b, patterns, prefix, suffix,
                    remove_id_prefix, group_by
                )
                with zipfile.ZipFile(subzip) as inz:
                    for info in inz.infolist():
                        out_z.writestr(info.filename, inz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = get_master_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # ─ live preview ─────────────────────
    preview = []
    for idx, doc in enumerate(docs):
        tpl, cat, loc = extract_meta(doc)
        splits = split_ranges(
            parse_toc(doc, detect_toc_pages(doc)),
            doc.page_count
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
            fn = slugify(base)

            preview.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  title,           # still contains "#1234: …"
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{fn}{suffix}.pdf",
            })

    st.subheader("Filename & Page-Range Preview")
    st.dataframe(pd.DataFrame(preview), use_container_width=True)
