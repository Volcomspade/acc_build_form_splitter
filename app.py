import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# ─── STREAMLIT CONFIG ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ACC Build TOC Splitter",
    layout="wide",
)

# ─── PDF SPLITTING LOGIC ──────────────────────────────────────────────────────

def detect_toc_pages(reader):
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i + 1
        for i, p in enumerate(reader.pages)
        if entry_rx.search(p.extract_text() or "")
    ]


def parse_toc(reader, toc_pages):
    # Match: #1234: Title ....... 56
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = reader.pages[pg - 1].extract_text() or ""
        for m in toc_rx.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            entries.append((title, start))
    return entries


def split_ranges(entries, total_pages):
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out


def slugify(name):
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')


def build_patterns(raw_input):
    """
    Turn comma-separated tokens into regex patterns, allowing '*' as wildcard.
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        esc = re.escape(tok)
        # treat '*' as wildcard
        esc = esc.replace(r'\*', '.*')
        pats.append(esc)
    return pats


def extract_location_category(reader, start_page, max_pages=6):
    """
    Starting at the form's first page, scan forward up to max_pages
    to locate 'Location' and 'Category' fields in the detail section.
    Returns (location, category), empty string if not found.
    """
    loc = ""
    cat = ""
    for i in range(start_page - 1, min(len(reader.pages), start_page - 1 + max_pages)):
        text = reader.pages[i].extract_text() or ""
        if not cat:
            mcat = re.search(r'Category\s+([^\n\r]+)', text)
            if mcat:
                cat = mcat.group(1).strip()
        if not loc:
            mloc = re.search(r'Location\s+([^\n\r]+)', text)
            if mloc:
                loc = mloc.group(1).strip()
        if loc and cat:
            break
    return loc, cat


def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id_prefix):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    toc_pages = detect_toc_pages(reader)
    entries = parse_toc(reader, toc_pages)
    splits = split_ranges(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for title, start, end in splits:
            # form name shown retains ID; filename optionally removes it
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title

            # apply removal patterns
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)

            fname = slugify(name_for_filename)

            # extract location/category to build folder path
            loc, cat = extract_location_category(reader, start)
            folder_parts = []
            if loc:
                folder_parts.append(slugify(loc))
            if cat:
                folder_parts.append(slugify(cat))
            folder_path = "/".join(folder_parts)  # e.g., BESS_Yard/Feeder_12B/Electrical/BESS_Assembly

            writer = PdfWriter()
            for p in range(start - 1, end):
                # guard against out-of-range just in case
                if p < len(reader.pages):
                    writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)

            zip_name = f"{prefix}{fname}{suffix}.pdf"
            if folder_path:
                archive_name = f"{folder_path}/{zip_name}"
            else:
                archive_name = zip_name

            zf.writestr(archive_name, part.read())

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
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames", value=True
)
patterns = build_patterns(remove_input)

# Regex helper
with st.expander("🛈 Regex & wildcard tips", expanded=False):
    st.markdown(
        """
- Comma-separate multiple patterns.  
- Use `*` as a wildcard (e.g., `03.*` matches `03.04`, `03.02`, etc.).  
- Patterns are treated case-insensitively.  
- Examples:  
  * `03.*` removes the section numbers like `03.04`.  
  * `L2_` removes literal `L2_`.  
  * `0?.0?` is **not** a wildcard in regex; use `0.` to match `03.` or `02.` or use `0\d\.\d` for precise digit matching.  
- If you want to match any two-digit dot-two-digit like `03.04` or `02.03`, use `\d{2}\.\d{2}`.  
"""
    )

if uploads:
    # read all uploaded files
    all_bytes = [f.read() for f in uploads]

    # build master ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for b, file in zip(all_bytes, uploads):
            sub = create_zip(b, patterns, prefix, suffix, remove_id_prefix)
            with zipfile.ZipFile(sub) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )

    # live preview
    st.subheader("Filename & Page-Range Preview")
    rows = []
    for idx, b in enumerate(all_bytes):
        reader = PdfReader(io.BytesIO(b))
        total_pages = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits = split_ranges(entries, total_pages)

        for title, start, end in splits:
            # maintain form name (with ID for display)
            form_name = title
            name_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            for rx in patterns:
                name_for_filename = re.sub(rx, '', name_for_filename, flags=re.IGNORECASE)
            fname = slugify(name_for_filename)

            # extract location/category
            loc, cat = extract_location_category(reader, start)
            folder_parts = []
            if loc:
                folder_parts.append(slugify(loc))
            if cat:
                folder_parts.append(slugify(cat))
            folder_path = "/".join(folder_parts)

            display_path = f"{folder_path}/{prefix}{fname}{suffix}.pdf" if folder_path else f"{prefix}{fname}{suffix}.pdf"

            rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name": form_name,
                "Location": loc,
                "Category": cat,
                "Pages": f"{start}-{end}",
                "Foldered Filename": display_path,
            })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
