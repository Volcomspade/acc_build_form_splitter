import re
import io
import zipfile

import streamlit as st
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter

# --- PDF splitting logic ---

def detect_toc_pages(reader):
    """Find pages containing TOC entries (lines like '# 123: ...')."""
    entry_rx = re.compile(r'^#\s*\d+:', re.MULTILINE)
    return [
        i for i, p in enumerate(reader.pages, start=1)
        if entry_rx.search(p.extract_text() or "")
    ]

def parse_toc(reader, toc_pages):
    """
    Parse the TOC pages for (name, start_page) entries.
    Uses dot‐leader lines of format: '# 123: Form Name ... 45'
    """
    pattern = re.compile(r'#\s*\d+:\s*(.*?)\s*\.{3,}\s*(\d+)', re.MULTILINE)
    entries = []
    for pg in toc_pages:
        text = reader.pages[pg-1].extract_text() or ''
        for m in pattern.finditer(text):
            raw_name = m.group(1).strip()
            start = int(m.group(2))
            entries.append((raw_name, start))
    return entries

def get_form_title(reader, start_page, remove_id=True):
    """
    Extract the form title from the first page of a form.
    Grabs the first non-empty line of text and optionally removes any leading numeric ID.
    """
    text = reader.pages[start_page-1].extract_text() or ''
    for line in text.splitlines():
        line = line.strip()
        if line:
            if remove_id:
                # Remove leading "#123: " if present to get the pure form name
                return re.sub(r'^#\s*\d+:\s*', '', line)
            else:
                # Keep numeric ID: remove only the leading '#' symbol (colon will be stripped later by slugify)
                return re.sub(r'^#', '', line)
    return f"Page_{start_page}"  # Fallback title if page has no text

def slugify(name):
    """Make a filesystem-safe slug from the form title by removing problematic characters and spaces."""
    # Remove characters not allowed in file names (\/:*?"<>| and also #)
    s = re.sub(r'[\\/:*?"<>|#]', '', name.strip())
    # Replace whitespace with underscores
    s = re.sub(r'\s+', '_', s)
    # Collapse multiple underscores if any
    return re.sub(r'_+', '_', s)

def build_patterns(raw_input: str):
    """
    Convert comma-separated removal patterns into regex patterns.
    Supports '*' as a wildcard (converted to '.*'), or full regex syntax.
    """
    pats = []
    for token in (x.strip() for x in raw_input.split(',') if x.strip()):
        if '*' in token:
            # Treat '*' as wildcard: escape the token then replace \* with .*
            esc = re.escape(token)
            pats.append(esc.replace(r'\*', '.*'))
        else:
            # No wildcard, use the token as is (can be a regex or literal)
            pats.append(token)
    return pats

def split_forms(reader, toc_entries):
    """
    Given TOC entries [(name, start_page), ...], return a list of page ranges [(start, end), ...] for each form.
    The end page is one before the next form’s start, or the last page of the document for the final form.
    """
    # Ensure the entries are sorted by start page (for safety in case TOC was out of order)
    toc_entries_sorted = sorted(toc_entries, key=lambda x: x[1])
    splits = []
    total_pages = len(reader.pages)
    for idx, (_, start) in enumerate(toc_entries_sorted):
        if idx + 1 < len(toc_entries_sorted):
            next_start = toc_entries_sorted[idx + 1][1]
            if next_start <= start:
                # Skip invalid or duplicate entry where next start is not greater than current start
                continue
            end = next_start - 1
        else:
            end = total_pages
        # Clamp the end within the document bounds
        if end > total_pages:
            end = total_pages
        if end < start:
            end = start
        splits.append((start, end))
    return splits

def create_zip(pdf_bytes, patterns, prefix, suffix):
    """
    Process a single PDF (as bytes): detect its TOC, split into individual form PDFs,
    and return an in-memory ZIP buffer containing all split PDFs for that one file.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    toc_pages = detect_toc_pages(reader)
    toc_entries = parse_toc(reader, toc_pages)
    page_ranges = split_forms(reader, toc_entries)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for (raw_name, start_page), (pg_start, pg_end) in zip(toc_entries, page_ranges):
            # Get the form’s actual title from its first page (blue heading)
            title = get_form_title(reader, pg_start, remove_id=remove_id_default)
            # Apply all removal patterns to the title
            clean_title = title
            for rx in patterns:
                clean_title = re.sub(rx, '', clean_title, flags=re.IGNORECASE)
            # Generate a safe filename
            fname = slugify(clean_title)
            # Extract the pages for this form into a new PDF
            writer = PdfWriter()
            for p in range(pg_start - 1, pg_end):
                writer.add_page(reader.pages[p])
            part_pdf = io.BytesIO()
            writer.write(part_pdf)
            part_pdf.seek(0)
            # Write this form’s PDF into the zip with the chosen prefix/suffix
            zf.writestr(f"{prefix}{fname}{suffix}.pdf", part_pdf.read())
    buf.seek(0)
    return buf

# --- Streamlit UI ---

st.set_page_config(page_title='ACC Build TOC Splitter')
st.title('ACC Build TOC Splitter')

# File uploader (supports multiple PDFs)
uploaded_files = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True
)

# User input fields for removal patterns, prefix, suffix, and numeric ID toggle
remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_default = st.checkbox("Remove numeric ID prefix from form titles", value=True)

# Build regex patterns list from user input
patterns = build_patterns(remove_input)

if uploaded_files:
    # Read all uploaded files into memory to avoid repeated file reads or empties on rerun
    uploads = []
    for f in uploaded_files:
        # Safely get file bytes
        try:
            pdf_bytes = f.getvalue()
        except Exception:
            pdf_bytes = f.read()
        if not pdf_bytes:
            # If reading was empty (possibly already read), reset and read again
            try:
                f.seek(0)
                pdf_bytes = f.read()
            except Exception:
                pdf_bytes = b""
        uploads.append((f.name, pdf_bytes))

    # Create a combined ZIP for all splits
    zip_out = io.BytesIO()
    with zipfile.ZipFile(zip_out, 'w') as zf:
        existing_filenames = set()
        for orig_name, pdf_bytes in uploads:
            with st.spinner(f"Processing {orig_name} ..."):
                # Split the current PDF into forms and get its ZIP in memory
                part_buf = create_zip(pdf_bytes, patterns, prefix, suffix)
                with zipfile.ZipFile(part_buf) as part_zip:
                    for info in part_zip.infolist():
                        content = part_zip.read(info.filename)
                        out_name = info.filename
                        # Ensure unique filenames if duplicates occur
                        if out_name in existing_filenames:
                            base_name, ext = out_name.rsplit('.', 1)
                            i = 1
                            new_name = f"{base_name} ({i}).{ext}"
                            while new_name in existing_filenames:
                                i += 1
                                new_name = f"{base_name} ({i}).{ext}"
                            out_name = new_name
                        zf.writestr(out_name, content)
                        existing_filenames.add(out_name)
    zip_out.seek(0)
    # Download button for the combined ZIP of all form PDFs
    st.download_button(
        "Download all splits",
        zip_out,
        file_name="acc_build_forms.zip"
    )
    st.success("Splitting complete! You can download the ZIP above.")

    # Preview table of filenames and page ranges
    st.subheader("Filename Preview")
    preview_rows = []
    no_forms_files = []
    for orig_name, pdf_bytes in uploads:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        toc_entries = parse_toc(reader, detect_toc_pages(reader))
        page_ranges = split_forms(reader, toc_entries)
        if not toc_entries:
            # No forms found in this PDF
            no_forms_files.append(orig_name)
            continue
        for (raw_name, start_page), (pg_start, pg_end) in zip(toc_entries, page_ranges):
            # Get title from page and apply patterns (use the same remove_id setting)
            title = get_form_title(reader, pg_start, remove_id=remove_id_default)
            clean_title = title
            for rx in patterns:
                clean_title = re.sub(rx, '', clean_title, flags=re.IGNORECASE)
            fname = slugify(clean_title)
            preview_rows.append({
                "Form Title": title if remove_id_default else title.strip(),
                "Pages": f"{pg_start}-{pg_end}",
                "Filename": f"{prefix}{fname}{suffix}.pdf"
            })
    # Display the preview table or a message if no forms found
    if preview_rows:
        df = pd.DataFrame(preview_rows)
        st.dataframe(df, width=900)
    if no_forms_files:
        if len(no_forms_files) == len(uploads):
            # All files had no forms
            if len(no_forms_files) == 1:
                st.warning("No form entries were detected in the uploaded PDF.")
            else:
                st.warning("No form entries were detected in any of the uploaded PDF(s).")
        else:
            # Some files had no forms – list them
            st.warning(
                f"No form entries were detected in the following uploaded file(s): {', '.join(no_forms_files)}"
            )
