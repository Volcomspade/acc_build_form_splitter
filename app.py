import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """Find pages containing TOC entries (lines starting with '# 1234:')."""
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [i + 1 for i, p in enumerate(reader.pages)
            if entry_rx.search(p.extract_text() or "")]


def parse_toc(reader, toc_pages):
    """
    Parse TOC pages for entries of the form:
      # 7893: Form Name ........ 15
    Returns [(raw_title_with_id, start_page), ...].
    """
    pattern = re.compile(r'#\s*(\d+):\s*(.*?)\.*\s+(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ""
        for m in pattern.finditer(text):
            form_id   = m.group(1).strip()
            form_body = m.group(2).strip()
            start     = int(m.group(3))
            raw_title = f"#{form_id}: {form_body}"
            entries.append((raw_title, start))
    # sort by page just in case
    return sorted(entries, key=lambda x: x[1])


def split_forms(entries, total_pages):
    """
    Given [(raw, start), ...], compute page ranges
    [(raw, start, end), ...] where end is one before next start.
    """
    splits = []
    for i, (raw, start) in enumerate(entries):
        end = entries[i+1][1] - 1 if i+1 < len(entries) else total_pages
        splits.append((raw, start, end))
    return splits


def slugify(name):
    """Make a safe filename."""
    s = re.sub(r'[\\/:*?"<>|]', '', name)
    s = re.sub(r'\s+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')


def build_patterns(raw_input):
    """
    Turn comma-separated wildcards/regex into list of regex strings.
    '*' in a token becomes '.*'; otherwise it's taken as-is.
    """
    pats = []
    for token in [t.strip() for t in raw_input.split(',') if t.strip()]:
        if '*' in token:
            esc = re.escape(token)
            token_re = esc.replace(r'\*', '.*')
        else:
            token_re = token
        pats.append(token_re)
    return pats


def create_zip(pdf_bytes, patterns, prefix, suffix, remove_id):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    entries = parse_toc(reader, detect_toc_pages(reader))
    splits  = split_forms(entries, total)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for raw, start, end in splits:
            # apply remove_id checkbox
            if remove_id:
                title = re.sub(r'^#\s*\d+:\s*', '', raw)
            else:
                title = raw
            # apply user patterns
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            writer = PdfWriter()
            for p in range(start-1, end):
                writer.add_page(reader.pages[p])
            part = io.BytesIO()
            writer.write(part)
            part.seek(0)
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part.read())

    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title="ACC Build TOC Splitter")
st.title("ACC Build TOC Splitter")

uploads    = st.file_uploader("Upload ACC Build PDF(s)", type="pdf", accept_multiple_files=True)
remove_in  = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix     = st.text_input("Filename prefix", "")
suffix     = st.text_input("Filename suffix", "")
remove_id  = st.checkbox("Remove numeric ID prefix (e.g. '#6849:')", value=True)
patterns   = build_patterns(remove_in)

# Regex helper expander
with st.expander("🛈 Regex & wildcard tips", expanded=False):
    st.markdown(r"""
- Use `*` to match any number of characters, e.g. `03.*_` will strip both `03.04_` and `03.02_`.  
- Separate multiple patterns with commas, e.g. `_0*_0*`, `Exhibit`, `\d{2}\.\d{2}_`.  
- For full‑regex patterns, either double your backslashes (`\\d`) or use raw strings as shown here.  
""")

if uploads:
    # prepare all PDF byte streams
    file_bytes = [f.getvalue() for f in uploads]

    # build master ZIP
    master = io.BytesIO()
    with zipfile.ZipFile(master, 'w') as mz:
        for pdf in file_bytes:
            subzip = create_zip(pdf, patterns, prefix, suffix, remove_id)
            with zipfile.ZipFile(subzip) as sz:
                for info in sz.infolist():
                    mz.writestr(info.filename, sz.read(info.filename))
    master.seek(0)

    st.download_button("Download all splits",
                       master,
                       file_name="acc_build_forms.zip",
                       mime="application/zip")

    # Live preview
    st.subheader("Filename & Page‑Range Preview")
    preview = []
    for i, pdf in enumerate(file_bytes):
        reader  = PdfReader(io.BytesIO(pdf))
        total   = len(reader.pages)
        entries = parse_toc(reader, detect_toc_pages(reader))
        splits  = split_forms(entries, total)
        for raw, start, end in splits:
            # same remove‑id logic
            if remove_id:
                title = re.sub(r'^#\s*\d+:\s*', '', raw)
            else:
                title = raw
            # apply patterns
            clean = title
            for rx in patterns:
                clean = re.sub(rx, '', clean, flags=re.IGNORECASE)
            fname = slugify(clean)

            preview.append({
                "Source PDF": uploads[i].name,
                "Form Name":  title,
                "Pages":      f"{start}-{end}",
                "Filename":   f"{prefix}{fname}{suffix}.pdf"
            })

    df = pd.DataFrame(preview)
    st.dataframe(df, use_container_width=True)
