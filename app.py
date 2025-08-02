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

# ─── HELPERS / CACHING ─────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def detect_toc_pages(reader_texts):
    """
    reader_texts: list of page text strings already extracted to avoid
    re-extracting inside this cached function.
    """
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i + 1 for i, txt in enumerate(reader_texts) if entry_rx.search(txt or "")]


@st.cache_data(show_spinner=False)
def parse_toc_from_texts(reader_texts, toc_pages):
    toc_rx = re.compile(r'#\s*\d+:\s*(.+?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        txt = reader_texts[pg - 1] or ""
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
    Turn comma-separated tokens into regex patterns supporting * and ? wildcards.
    '*' => '.*', '?' => '.'
    """
    pats = []
    for tok in [t.strip() for t in raw_input.split(',') if t.strip()]:
        # escape everything first
        esc = re.escape(tok)
        # replace wildcard tokens
        esc = esc.replace(r'\*', '.*').replace(r'\?', '.')
        pats.append(esc)
    return pats


def compile_patterns(raw_input):
    raw_pats = build_patterns(raw_input)
    return [re.compile(p, re.IGNORECASE) for p in raw_pats]


@st.cache_data(show_spinner=False)
def extract_all_page_texts(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    texts = []
    for p in reader.pages:
        try:
            texts.append(p.extract_text() or "")
        except Exception:
            texts.append("")
    return texts, len(reader.pages)


def build_preview_rows(all_bytes, uploads, remove_id_prefix, prefix, suffix, compiled_patterns):
    rows = []
    for idx, b in enumerate(all_bytes):
        source_name = uploads[idx].name
        reader_texts, total_pages = extract_all_page_texts(b)
        toc_pages = detect_toc_pages(reader_texts)
        entries = parse_toc_from_texts(reader_texts, toc_pages)
        splits = split_ranges(entries, total_pages)

        for title, start, end in splits:
            # Title shown keeps the original (with maybe the ID)
            display_title = title
            # Filename base: strip ID only if requested
            base_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id_prefix else title
            fname = slugify(base_for_filename)
            # apply removal patterns on slugified name
            for pat in compiled_patterns:
                fname = pat.sub('', fname)
            final_name = f"{prefix}{fname}{suffix}.pdf"

            rows.append({
                "Source PDF": source_name,
                "Form Name": display_title,
                "Pages": f"{start}-{end}",
                "Filename": final_name
            })
    return rows


def create_zip(pdf_bytes, compiled_patterns, prefix, suffix, remove_id):
    reader_texts, total_pages = extract_all_page_texts(pdf_bytes)
    toc_pages = detect_toc_pages(reader_texts)
    entries = parse_toc_from_texts(reader_texts, toc_pages)
    splits = split_ranges(entries, total_pages)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for title, start, end in splits:
            # Filename base (strip ID if requested)
            base_for_filename = re.sub(r'^#\s*\d+:\s*', '', title) if remove_id else title
            fname = slugify(base_for_filename)
            for pat in compiled_patterns:
                fname = pat.sub('', fname)
            final_name = f"{prefix}{fname}{suffix}.pdf"

            writer = PdfWriter()
            # be defensive about bounds
            for p in range(max(0, start - 1), min(end, len(reader.pages))):
                try:
                    writer.add_page(reader.pages[p])
                except Exception:
                    continue
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)
            zf.writestr(final_name, part.read())
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
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames",
    value=True
)

# compile regexes once
compiled_patterns = compile_patterns(remove_input)

# regex helper expander
with st.expander("🛈 Regex & wildcard tips", expanded=False):
    st.markdown(
        """
- `*` matches any sequence of characters. Example: `03.*` removes `03.04`, `03.02`, etc.
- `?` matches any single character. Example: `0?.0?` matches `03.04`, `02.03`, etc.
- Separate multiple patterns with commas: `03.*,_L2_` will remove both parts.
- Patterns are applied after slugification, so spaces become `_`. For example, to remove `03.04` from `03.04_Exhibit`, you might use `03\.04_` or `03.*_` depending on desired fuzziness.
"""
    )

if uploads:
    # read bytes once
    all_bytes = [f.read() for f in uploads]

    # Preview generation with progress
    with st.spinner("Parsing uploaded PDFs and building preview..."):
        progress = st.progress(0)
        rows = []
        total = len(all_bytes)
        for i in range(total):
            # update progress
            progress.progress((i + 1) / total)
        # build all rows (this uses cached helpers internally)
        rows = build_preview_rows(all_bytes, uploads, remove_id_prefix, prefix, suffix, compiled_patterns)
        progress.empty()

    df = pd.DataFrame(rows)

    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)

    # Build combined ZIP
    with st.spinner("Building ZIP of splits..."):
        master = io.BytesIO()
        with zipfile.ZipFile(master, 'w') as mz:
            for b in all_bytes:
                subzip = create_zip(b, compiled_patterns, prefix, suffix, remove_id_prefix)
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        mz.writestr(info.filename, sz.read(info.filename))
        master.seek(0)

    st.download_button(
        "Download all splits",
        master,
        file_name="acc_build_forms.zip",
        mime="application/zip"
    )
