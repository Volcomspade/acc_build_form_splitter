import io
import re
import time
import zipfile
from typing import List, Tuple

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st


# ───────────────────────── Streamlit config ─────────────────────────
st.set_page_config(page_title="ACC Build TOC Splitter", layout="wide")
st.title("ACC Build TOC Splitter")


# ───────────────────────── Helpers: text + parsing ─────────────────────────
def page_text(doc: fitz.Document, idx: int) -> str:
    """Return plain text for a page (defensive)."""
    try:
        return doc.load_page(idx).get_text("text") or ""
    except Exception:
        return ""


def detect_toc_pages(doc: fitz.Document) -> List[int]:
    """
    TOC pages contain lines like:
      #6849: ACC/DCC-D4.1 ... .............. 4
    Return 1-based indices.
    """
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        if entry_rx.search(page_text(doc, i)):
            pages.append(i + 1)
    return pages


def parse_toc(doc: fitz.Document, toc_pages: List[int]) -> List[Tuple[str, int]]:
    """
    Parse TOC entries on the given pages.
    Returns [(title, start_page)] using 1-based pages.
    """
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    out = []
    for pg in toc_pages:
        txt = page_text(doc, pg - 1)
        for m in toc_rx.finditer(txt):
            title = m.group(1).strip()
            start = int(m.group(2))
            out.append((title, start))
    return out


def split_ranges(entries: List[Tuple[str, int]], total_pages: int) -> List[Tuple[str, int, int]]:
    """Turn [(title, start)] into [(title, start, end)]. Pages are 1-based."""
    out = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total_pages
        out.append((title, start, end))
    return out


def slugify_filename(name: str) -> str:
    """Windows-safe filename from text."""
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def slugify_folder(name: str) -> str:
    """
    Safer folder component (keep spaces, kill bad chars).
    Used for ZIP folder path. We don't underscore spaces in folder names.
    """
    s = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return s


def token_to_regex(token: str) -> str:
    """
    Convert a simple token to a regex:
    - Escape all characters, then make '*' -> '.*?' (non-greedy).
    This reduces over-deleting when a pattern like '0*.0*_' is used.
    """
    esc = re.escape(token)
    return esc.replace(r"\*", ".*?")


def build_patterns(raw: str) -> List[re.Pattern]:
    """
    Build a list of case-insensitive, compiled regex patterns from comma-separated tokens.
    Each token can use '*' as a non-greedy wildcard.
    """
    pats = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        rx = token_to_regex(tok)
        pats.append(re.compile(rx, re.IGNORECASE))
    return pats


# ───────────────────────── Metadata extraction ─────────────────────────
def extract_meta(doc: fitz.Document) -> Tuple[str, str, str]:
    """
    Extract:
      - template (from first page, 'Forms' table row: Template ...)
      - category & location (from 'References and Attachments' pages)
    Returns (template, category, location). If not found, 'Unknown'.
    """
    template = "Unknown"
    category = "Unknown"
    location = "Unknown"

    # First page: find "Template    <value>"
    p0 = page_text(doc, 0)
    # Most PDFs render 'Template' followed by the value on the same line
    m = re.search(r"^Template\s+(.*)$", p0, flags=re.MULTILINE)
    if m:
        template = m.group(1).strip()
    else:
        # Fallback: find a line 'Template' and use next non-empty line
        lines = [ln.strip() for ln in p0.splitlines()]
        for i, ln in enumerate(lines):
            if ln.strip().lower() == "template":
                for j in range(i + 1, len(lines)):
                    if lines[j]:
                        template = lines[j]
                        break
                break

    # Pages 2..N: find 'Category' and 'Location' under 'References and Attachments'
    cat_re = re.compile(r"^Category\s+(.*)$", re.MULTILINE)
    loc_re = re.compile(r"^Location\s+(.*)$", re.MULTILINE)

    for i in range(1, doc.page_count):
        txt = page_text(doc, i)
        # Minor guard: prefer pages that actually look like the references section
        if "References and Attachments" not in txt and "Assets" not in txt:
            continue

        cm = cat_re.search(txt)
        lm = loc_re.search(txt)
        if cm:
            val = cm.group(1).strip()
            # avoid the long ACC note page
            if not val.lower().startswith("form detail report"):
                category = val
        if lm:
            val = lm.group(1).strip()
            if not val.lower().startswith("form detail report"):
                location = val

        if category != "Unknown" and location != "Unknown":
            break

    return template, category, location


# ───────────────────────── Split & ZIP ─────────────────────────
def create_subzip(
    pdf_bytes: bytes,
    patterns: List[re.Pattern],
    prefix: str,
    suffix: str,
    remove_id_prefix: bool,
    group_by: str,
) -> Tuple[io.BytesIO, str, str, str, List[Tuple[str, int, int]]]:
    """
    Split one PDF into forms, name the parts, and write them to a sub-zip.
    Returns (zip_buf, template, category, location, splits).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count

    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total)

    tpl, cat, loc = extract_meta(doc)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # Folder path
            if group_by == "Location/Category":
                folder_parts = [slugify_folder(p) for p in [loc, cat] if p and p != "Unknown"]
                folder_path = "/".join(folder_parts) + ("/" if folder_parts else "")
            elif group_by == "Template":
                folder_part = slugify_folder(tpl) if tpl != "Unknown" else ""
                folder_path = (folder_part + "/") if folder_part else ""
            else:
                folder_path = ""

            # Build filename (strip ID only here)
            base = title
            if remove_id_prefix:
                base = re.sub(r"^#\s*\d+:\s*", "", base)

            # Apply removal patterns once, in order
            for rx in patterns:
                base = rx.sub("", base, count=1)

            fname = slugify_filename(base)
            out_name = f"{folder_path}{prefix}{fname}{suffix}.pdf"

            # Assemble pages
            out = fitz.open()
            out.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            zf.writestr(out_name, out.tobytes())
            out.close()

    buf.seek(0)
    return buf, tpl, cat, loc, splits


# ───────────────────────── UI controls ─────────────────────────
uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)

remove_input = st.text_input("Remove patterns (* wildcards or regex-ish, comma-separated)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. “#6849: ”) from filenames only",
    value=True,
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Template", "Location/Category"],
)

with st.expander("🧩 Pattern tips", expanded=False):
    st.markdown(
        """
- **Exact**: just type it, e.g. `Checklist`
- **Wildcard `*`** is **non-greedy** here: `0*.0*_` removes `03.04_` without eating the rest
- **Multiple**: separate with commas, e.g. `03.04_, L2_`
- Patterns only change the **saved filenames**, not the “Form Name” column.
        """
    )


# ───────────────────────── Main flow ─────────────────────────
if uploads:
    # Compile patterns once (case-insensitive, non-greedy wildcards)
    patterns = build_patterns(remove_input)

    # Start timer for preview build
    t0 = time.perf_counter()

    # Load PDF bytes + open docs
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]

    total_pages = sum(d.page_count for d in docs)
    total_forms = 0

    # Build preview table (this is what we time)
    preview_rows = []
    with st.spinner("Building preview…"):
        for idx, d in enumerate(docs):
            tpl, cat, loc = extract_meta(d)
            entries = parse_toc(d, detect_toc_pages(d))
            splits = split_ranges(entries, d.page_count)
            total_forms += len(splits)

            for title, start, end in splits:
                # Folder (display with >)
                if group_by == "Location/Category":
                    folder = " > ".join([p for p in [loc, cat] if p and p != "Unknown"])
                elif group_by == "Template":
                    folder = tpl
                else:
                    folder = ""

                # Filename (strip ID + apply patterns)
                base = title
                if remove_id_prefix:
                    base = re.sub(r"^#\s*\d+:\s*", "", base)
                for rx in patterns:
                    base = rx.sub("", base, count=1)
                fname = slugify_filename(base)

                preview_rows.append(
                    {
                        "Source PDF": uploads[idx].name,
                        "Folder": folder if folder else "",
                        "Form Name": title,
                        "Pages": f"{start}-{end}",
                        "Filename": f"{prefix}{fname}{suffix}.pdf",
                    }
                )

    # Stop timer when preview is done
    elapsed = time.perf_counter() - t0
    mins, secs = divmod(int(round(elapsed)), 60)

    # Metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # Preview
    df = pd.DataFrame(preview_rows)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)

    # Build the ZIP after preview timing
    @st.cache_data(show_spinner=False)
    def build_master_zip(
        all_bytes: List[bytes],
        remove_input: str,
        prefix: str,
        suffix: str,
        remove_id_prefix: bool,
        group_by: str,
    ) -> io.BytesIO:
        pats = build_patterns(remove_input)  # compile inside the cache for hashability
        master = io.BytesIO()
        with zipfile.ZipFile(master, "w") as mz:
            for b in all_bytes:
                sub, *_ = create_subzip(b, pats, prefix, suffix, remove_id_prefix, group_by)
                with zipfile.ZipFile(sub) as sz:
                    for info in sz.infolist():
                        mz.writestr(info.filename, sz.read(info.filename))
        master.seek(0)
        return master

    with st.spinner("Preparing ZIP…"):
        zip_buf = build_master_zip(all_bytes, remove_input, prefix, suffix, remove_id_prefix, group_by)

    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )
