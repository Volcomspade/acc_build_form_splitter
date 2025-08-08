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

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(doc):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        txt = doc.load_page(i).get_text()
        if entry_rx.search(txt):
            pages.append(i + 1)
    return pages

def parse_toc(doc, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = doc.load_page(pg-1).get_text()
        for m in toc_rx.finditer(txt):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries

def split_ranges(entries, total_pages):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        out.append((title, start, end))
    return out

def slugify(name):
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')

def build_patterns(raw):
    """
    Turn comma-separated wildcards/regex into patterns:
      * → .*    (zero-or-more)
      ? → .     (single char)
    and escape everything else.
    """
    pats = []
    for tok in [t.strip() for t in raw.split(',') if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r'\*', '.*').replace(r'\?', '.')
        pats.append(esc)
    return pats

def extract_meta(doc):
    """
    Scan every page for lines starting 'Category' or 'Location',
    split on the FIRST colon only, ignore lines without one.
    """
    cat, loc = "Unknown", "Unknown"
    for i in range(doc.page_count):
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
    return loc, cat

def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id, group_by):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    toc   = detect_toc_pages(doc)
    entries = parse_toc(doc, toc)
    splits  = split_ranges(entries, total)
    loc, cat = extract_meta(doc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            raw_title = title
            # only strip '#1234:' from filename
            fn = re.sub(r'^#\s*\d+:\s*', '', raw_title) if remove_id else raw_title
            for rx in patterns:
                fn = re.sub(rx, '', fn, flags=re.IGNORECASE)
            base = slugify(fn)
            filename = f"{prefix}{base}{suffix}.pdf"

            # decide folder path
            if group_by == "Template":
                parts = raw_title.split(":", 2)
                tpl = parts[2].strip() if len(parts) == 3 else raw_title
                folder = slugify(tpl)
            elif group_by == "Location/Category":
                folder = f"{slugify(loc)}/{slugify(cat)}"
            else:
                folder = ""

            arcname = filename if not folder else f"{folder}/{filename}"

            # extract pages
            part = fitz.open()
            part.insert_pdf(doc, from_page=start-1, to_page=end-1)
            zf.writestr(arcname, part.write())

    buf.seek(0)
    return buf

# ─── STREAMLIT UI ────────────────────────────────────────────────────────────

st.title("ACC Build TOC Splitter")

uploads          = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_input     = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix           = st.text_input("Filename prefix", "")
suffix           = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox("Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only", value=True)
group_by         = st.selectbox("Group files in ZIP by:", ["Location/Category","Template","None"], index=0)

patterns = build_patterns(remove_input)

if uploads:
    # initial read timing
    t0 = time.time()
    all_bytes = [f.read() for f in uploads]
    read_sec = time.time() - t0

    # assemble ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b in all_bytes:
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix, group_by)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button("Download all splits", master, "acc_build_forms.zip", "application/zip")

    # summary metrics
    st.markdown("---")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    total_pages = sum(fitz.open(stream=b, filetype="pdf").page_count for b in all_bytes)
    c2.metric("Total pages", total_pages)
    total_forms = sum(len(split_ranges(parse_toc(fitz.open(stream=b, filetype="pdf"),
                                                  detect_toc_pages(fitz.open(stream=b, filetype="pdf"))),
                                         fitz.open(stream=b, filetype="pdf").page_count))
                      for b in all_bytes)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{int(read_sec//60):02}:{int(read_sec%60):02}")

    # live preview
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        doc = fitz.open(stream=b, filetype="pdf")
        total = doc.page_count
        toc   = detect_toc_pages(doc)
        ent   = parse_toc(doc, toc)
        spl   = split_ranges(ent, total)
        loc, cat = extract_meta(doc)

        for title, start, end in spl:
            raw_title = title
            fn = re.sub(r'^#\s*\d+:\s*', '', raw_title) if remove_id_prefix else raw_title
            for rx in patterns:
                fn = re.sub(rx, '', fn, flags=re.IGNORECASE)
            base = slugify(fn)
            fname = f"{prefix}{base}{suffix}.pdf"

            if group_by=="Template":
                parts = raw_title.split(":",2)
                tpl = parts[2].strip() if len(parts)==3 else raw_title
                folder = slugify(tpl)
            elif group_by=="Location/Category":
                folder = f"{slugify(loc)}/{slugify(cat)}"
            else:
                folder = ""

            rows.append({
                "Source PDF": uploads[idx].name,
                "Folder":     folder,
                "Form Name":  raw_title,
                "Pages":      f"{start}-{end}",
                "Filename":   fname
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
