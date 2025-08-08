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
    Returns (template, category, location):
      - template: first tries the "Template: ..." line under the Forms block on page 0;
                  if not found, falls back to the 3rd ':'-delimited piece of the TOC title.
      - category, location: regex-extracted anywhere on pages 1–end for "Category:" & "Location:".
    """
    # ── 1) Template from the Forms section on page 0
    template = "Unknown"
    page0 = doc.load_page(0).get_text().splitlines()
    for line in page0:
        m = re.match(r"\s*Template\s*:\s*(.+)", line)
        if m:
            template = m.group(1).strip()
            break

    # ── 2) Fallback: if still Unknown, grab after the 2nd ':' of a "#1234: ... : <template>" line
    if template == "Unknown":
        for line in page0:
            if line.startswith("#") and line.count(":") >= 2:
                template = line.split(":", 2)[2].strip()
                break

    # ── 3) Category & Location via regex on pages 1–end
    category = location = "Unknown"
    for i in range(1, doc.page_count):
        text = doc.load_page(i).get_text()
        m_cat = re.search(r"Category\s*:\s*(.+)", text)
        m_loc = re.search(r"Location\s*:\s*(.+)", text)
        if m_cat:
            category = m_cat.group(1).strip()
        if m_loc:
            location = m_loc.group(1).strip()
        if category != "Unknown" and location != "Unknown":
            break

    return template, category, location

    # ── 3) Pages 1–4: find Category & Location
    category = location = "Unknown"
    for p in range(1, min(doc.page_count, 5)):
        for line in doc.load_page(p).get_text().splitlines():
            if line.startswith("Category"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    category = parts[1].strip()
            elif line.startswith("Location"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    location = parts[1].strip()
        if category != "Unknown" and location != "Unknown":
            break

    return template, category, location


# ─── TOC PARSING ───────────────────────────────────────────────────────────────
def detect_toc_pages(doc: fitz.Document):
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    return [i + 1 for i in range(doc.page_count)
                 if entry_rx.search(doc.load_page(i).get_text() or "")]

def parse_toc(doc: fitz.Document, toc_pages: list[int]):
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg-1).get_text() or ""
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
    s = re.sub(r'[\\/:*?"<>|]', "", name)      # drop illegal chars
    s = re.sub(r"\s+", "_", s)                 # spaces → underscores
    return re.sub(r"_+", "_", s).strip("_")    # collapse multiples, trim

def build_patterns(raw: str):
    """
    From a comma-separated list of wildcards/regex tokens,
    build a list of regex patterns, and detect "_"-only removal.
    """
    pats = []
    remove_underscore = False
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        if tok == "_":
            remove_underscore = True
            continue
        esc = re.escape(tok).replace(r"\*", ".*")
        pats.append(esc)
    return pats, remove_underscore

# ─── SINGLE-PDF SPLIT & ZIPPING ────────────────────────────────────────────────
def create_subzip(
    pdf_bytes: bytes,
    patterns: list[str],
    remove_underscore: bool,
    prefix: str,
    suffix: str,
    remove_id: bool,
    group_by: str,
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
            # pick folder path
            if group_by == "Location/Category":
                folder = f"{loc}/{cat}/"
            elif group_by == "Template":
                folder = f"{tpl}/"
            else:
                folder = ""

            # build display name & raw base
            form_name = title
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)

            # slugify & optional underscore‐strip
            fname = slugify(base)
            if remove_underscore:
                fname = fname.replace("_", "")

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
    patterns, remove_underscore = build_patterns(remove_input)

    # initial‐read timing
    t0       = time.perf_counter()
    all_bytes= [f.read() for f in uploads]
    docs     = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    total_pg = sum(d.page_count for d in docs)
    total_fm = sum(len(parse_toc(d, detect_toc_pages(d))) for d in docs)
    elapsed  = time.perf_counter() - t0
    mins, secs = divmod(int(elapsed), 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pg)
    c3.metric("Total forms", total_fm)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # build master ZIP
    def get_master_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as master:
            for b in all_bytes:
                subzip, *_ = create_subzip(
                    b, patterns, remove_underscore,
                    prefix, suffix, remove_id_prefix, group_by
                )
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        master.writestr(info.filename, sz.read(info.filename))
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
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            if remove_underscore:
                fname = fname.replace("_", "")

            preview.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf",
            })

    st.subheader("Filename & Page-Range Preview")
    df = pd.DataFrame(preview)
    st.dataframe(df, use_container_width=True)
