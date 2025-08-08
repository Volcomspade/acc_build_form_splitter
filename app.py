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
    Extracts:
      - template (from page 0's "Template" line, with or without colon)
      - category  (from References & Attachments page)
      - location  (same page)
    """
    # ── Template ──
    template = "Unknown"
    for line in doc.load_page(0).get_text().splitlines():
        if line.startswith("Template"):
            if ":" in line:
                template = line.split(":", 1)[1].strip()
            else:
                # after the word "Template"
                template = line[len("Template"):].strip()
            break

    # ── Category & Location ──
    category = location = "Unknown"
    for p in range(1, doc.page_count):
        for line in doc.load_page(p).get_text().splitlines():
            if line.startswith("Category"):
                if ":" in line:
                    category = line.split(":", 1)[1].strip()
                else:
                    category = line[len("Category"):].strip()
            elif line.startswith("Location"):
                if ":" in line:
                    location = line.split(":", 1)[1].strip()
                else:
                    location = line[len("Location"):].strip()
        if category != "Unknown" and location != "Unknown":
            break

    return template, category, location

# ─── TOC PARSING ───────────────────────────────────────────────────────────────
def detect_toc_pages(doc: fitz.Document):
    rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if rx.search(text):
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
    # remove forbidden chars, collapse spaces, collapse multiple _
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")

def build_patterns(raw: str):
    """
    Turn comma-separated tokens into regex, treating '*' as '.*'
    """
    pats = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*")
        pats.append(esc)
    return pats

# ─── SPLIT & ZIP ───────────────────────────────────────────────────────────────
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
            # determine folder path
            if group_by == "Location/Category":
                folder = f"{loc} / {cat}/"
            elif group_by == "Template":
                folder = f"{tpl}/"
            else:
                folder = ""

            # strip the numeric ID only for the filename
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)

            # slugify first
            slug = slugify(base)

            # then remove any user-specified patterns from the slug
            for rx in patterns:
                slug = re.sub(rx, "", slug, flags=re.IGNORECASE)
            # clean up any accidental double-underscores
            slug = slugify(slug)

            out_name = f"{folder}{prefix}{slug}{suffix}.pdf"

            # build the sub-PDF
            part = fitz.open()
            for p in range(start-1, end):
                part.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = part.write()
            part.close()

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
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only",
    value=True,
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Location/Category", "Template"],
)

if uploads:
    patterns  = build_patterns(remove_input)
    t0         = time.perf_counter()
    all_bytes  = [f.read() for f in uploads]
    docs       = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    # metrics
    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(len(parse_toc(d, detect_toc_pages(d))) for d in docs)
    mins, secs  = divmod(int(time.perf_counter() - t0), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # assemble ZIP
    def get_master_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as mzf:
            for b in all_bytes:
                subzip, *_ = create_subzip(
                    b, patterns, prefix, suffix, remove_id_prefix, group_by
                )
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        mzf.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = get_master_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # live preview
    preview = []
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
            slug = slugify(base)
            for rx in patterns:
                slug = re.sub(rx, "", slug, flags=re.IGNORECASE)
            slug = slugify(slug)

            preview.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{slug}{suffix}.pdf",
            })

    df = pd.DataFrame(preview)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)
