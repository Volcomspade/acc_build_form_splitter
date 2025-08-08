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

# ─── PDF METADATA EXTRACTION ────────────────────────────────────────────────────
def extract_meta(doc):
    """
    Returns (template, category, location) for a given PyMuPDF Document.
    - Template from page 0 (“Template: ...”)
    - Category & Location from the “References & Attachments” section on pages 1+
    """
    # 1) TEMPLATE from page 0
    tpl = "Unknown"
    for line in doc.load_page(0).get_text().splitlines():
        if line.startswith("Template"):
            parts = line.split(":", 1)
            if len(parts) > 1:
                tpl = parts[1].strip()
            break

    # 2) CATEGORY & LOCATION from pages 1+
    cat = loc = "Unknown"
    for i in range(1, doc.page_count):
        for line in doc.load_page(i).get_text().splitlines():
            if line.startswith("Category"):
                parts = line.split(":", 1)
                if len(parts) > 1:
                    cat = parts[1].strip()
            elif line.startswith("Location"):
                parts = line.split(":", 1)
                if len(parts) > 1:
                    loc = parts[1].strip()
        if cat != "Unknown" and loc != "Unknown":
            break

    return tpl, cat, loc

# ─── TOC PARSING ────────────────────────────────────────────────────────────────
def detect_toc_pages(doc):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if entry_rx.search(text):
            pages.append(i + 1)
    return pages

def parse_toc(doc, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text() or ""
        for m in toc_rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def split_ranges(entries, total_pages):
    splits = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        splits.append((title, start, end))
    return splits

# ─── FILENAME SANITIZATION ────────────────────────────────────────────────────
def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")

def build_patterns(raw_input: str):
    pats = []
    for tok in [t.strip() for t in raw_input.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*")
        pats.append(esc)
    return pats

# ─── CREATE SUB-ZIP ────────────────────────────────────────────────────────────
def create_zip(
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

    template, category, location = extract_meta(doc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # Determine folder path
            if group_by == "Location/Category":
                folder = f"{location}/{category}/"
            elif group_by == "Template":
                folder = f"{template}/"
            else:
                folder = ""

            # Form name stays intact
            form_name = title

            # Build filename base
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)

            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # Split pages via PyMuPDF
            subdoc = fitz.open()
            for p in range(start - 1, end):
                subdoc.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = subdoc.write()
            subdoc.close()

            zf.writestr(out_name, part_bytes)

    buf.seek(0)
    return buf, template, category, location, splits

# ─── STREAMLIT UI ─────────────────────────────────────────────────────────────
st.title("ACC Build TOC Splitter")

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True
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

# Only proceed once at least one PDF is uploaded
if uploads:
    patterns = build_patterns(remove_input)

    # --- INITIAL READ & STATS ---
    start_time = time.perf_counter()
    all_bytes  = [f.read() for f in uploads]
    docs       = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]

    total_pages = sum(d.page_count for d in docs)
    toc_lists   = [parse_toc(d, detect_toc_pages(d)) for d in docs]
    total_forms = sum(len(t) for t in toc_lists)

    read_secs = time.perf_counter() - start_time
    mins = int(read_secs // 60)
    secs = int(read_secs % 60)

    # --- SUMMARY ---
    with st.container():
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Source PDFs", len(uploads))
        c2.metric("Total pages", total_pages)
        c3.metric("Total forms", total_forms)
        c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # --- DOWNLOAD BUTTON (ASSEMBLE ZIP ON DEMAND) ---
    def assemble_master():
        master = io.BytesIO()
        with zipfile.ZipFile(master, "w") as mz:
            for b in all_bytes:
                subzip, *_ = create_zip(
                    b,
                    patterns,
                    prefix,
                    suffix,
                    remove_id_prefix,
                    group_by,
                )
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        mz.writestr(info.filename, sz.read(info.filename))
        master.seek(0)
        return master

    zip_bytes = assemble_master()
    st.download_button(
        "Download all splits",
        zip_bytes,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # --- LIVE PREVIEW ---
    rows = []
    for idx, (b, d) in enumerate(zip(all_bytes, docs)):
        tpl, cat, loc = extract_meta(d)
        splits = split_ranges(parse_toc(d, detect_toc_pages(d)), d.page_count)

        for title, start, end in splits:
            # folder
            if group_by == "Location/Category":
                folder = f"{loc} / {cat}"
            elif group_by == "Template":
                folder = tpl
            else:
                folder = ""

            # build filename
            base = title
            if remove_id_prefix:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            full_name = f"{prefix}{fname}{suffix}.pdf"

            rows.append({
                "Source PDF": uploads[idx].name,
                "Folder":      folder,
                "Form Name":   title,
                "Pages":       f"{start}-{end}",
                "Filename":    full_name,
            })

    df = pd.DataFrame(rows)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)
