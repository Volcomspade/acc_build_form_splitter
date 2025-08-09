import io
import re
import time
import zipfile
from typing import List, Tuple

import pandas as pd
import streamlit as st
import fitz  # PyMuPDF


# ──────────────────────────────────────────────────────────────────────────────
# Streamlit config
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="ACC Build TOC Splitter", layout="wide")
st.title("ACC Build TOC Splitter")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """Make a string safe for filenames."""
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def build_patterns(raw: str) -> List[str]:
    """
    Turn comma-separated tokens into regex patterns.
    - Escape literally (so dots, plus, etc. don't explode)
    - Then replace '*' with '.*' to create wildcards
    """
    pats: List[str] = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*")
        pats.append(esc)
    return pats


def _first_nonempty_after(lines: List[str], i: int, max_ahead: int = 6) -> str:
    for j in range(i + 1, min(len(lines), i + 1 + max_ahead)):
        v = lines[j].strip()
        if v:
            return v
    return ""


def _grab_key_value(text: str, key: str) -> str:
    """
    Return the value for 'key' in two cases:
      1) 'Key: value'
      2) Table layout:
           Key
           value
    """
    # Case 1: 'Key: value'
    m = re.search(rf"(?im)^\s*{re.escape(key)}\s*[:\-]\s*(.+?)\s*$", text)
    if m:
        return m.group(1).strip()

    # Case 2: key on line N, value on the next non-empty line
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.lower() == key.lower():
            v = _first_nonempty_after(lines, i)
            if v and v.lower() != key.lower():
                return v.strip()
    return ""


def extract_meta(doc: fitz.Document) -> Tuple[str, str, str]:
    """
    Robustly extract:
      - Template (from first page 'Forms' table)
      - Category and Location (from 'References and Attachments' / Assets)
    """
    template = "Unknown"
    category = "Unknown"
    location = "Unknown"

    # --- Template from page 0
    try:
        t0 = doc.load_page(0).get_text() or ""
        val = _grab_key_value(t0, "Template")
        if val:
            template = val
        else:
            # Fallback: a reasonable exhibit line
            for ln in t0.splitlines():
                low = ln.lower()
                if "exhibit" in low and ("checklist" in low or "inspection" in low):
                    template = ln.strip()
                    break
    except Exception:
        pass

    # --- Category & Location near "References and Attachments"
    try:
        scored = []
        for i in range(min(doc.page_count, 30)):
            tx = (doc.load_page(i).get_text() or "")
            low = tx.lower()
            score = 0
            if "references and attachments" in low:
                score += 3
            if "assets" in low:
                score += 2
            if "category" in low or "location" in low:
                score += 1
            if score:
                scored.append((score, i))
        scored.sort(reverse=True)
        search_pages = [i for _, i in scored] or list(range(min(10, doc.page_count)))

        for i in search_pages:
            tx = doc.load_page(i).get_text() or ""
            vcat = _grab_key_value(tx, "Category")
            vloc = _grab_key_value(tx, "Location")

            if vcat:
                category = vcat
            if vloc and "form detail report" not in vloc.lower():
                location = vloc

            if category != "Unknown" and location != "Unknown":
                break
    except Exception:
        pass

    return template, category, location


def detect_toc_pages(doc: fitz.Document) -> List[int]:
    """Return 1-based page numbers that contain TOC entries ('# 1234:')."""
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if entry_rx.search(text or ""):
            pages.append(i + 1)
    return pages


def parse_toc(doc: fitz.Document, toc_pages: List[int]) -> List[Tuple[str, int]]:
    """
    Return a list of (title, start_page) from TOC pages.
    TOC lines look like: '# 7893: Form Name ........ 15'
    """
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries: List[Tuple[str, int]] = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text() or ""
        for m in toc_rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries


def split_ranges(entries: List[Tuple[str, int]], total: int) -> List[Tuple[str, int, int]]:
    """Turn (title, start) into (title, start, end)."""
    out: List[Tuple[str, int, int]] = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total
        out.append((title, start, end))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Single-PDF → sub-zip
# ──────────────────────────────────────────────────────────────────────────────

def create_subzip(
    pdf_bytes: bytes,
    patterns: List[str],
    prefix: str,
    suffix: str,
    remove_id: bool,
    group_by: str,
) -> Tuple[io.BytesIO, str, str, str, List[Tuple[str, int, int]]]:
    """
    Split a single PDF (by TOC) and return:
      subzip_buf, template, category, location, splits
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count

    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total_pages)

    tpl, cat, loc = extract_meta(doc)

    # Prepare folder names for zip paths
    def folderify(s: str) -> str:
        return slugify(s.replace(">", "/").replace("\\", "/"))

    folder_tpl = folderify(tpl) if tpl != "Unknown" else "Unknown"
    folder_loc = folderify(loc) if loc != "Unknown" else "Unknown"
    folder_cat = folderify(cat) if cat != "Unknown" else "Unknown"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # folder path for this split
            if group_by == "Location/Category":
                folder = f"{folder_loc}/{folder_cat}/" if folder_loc or folder_cat else ""
            elif group_by == "Template":
                folder = f"{folder_tpl}/" if folder_tpl else ""
            else:
                folder = ""

            # filename base (ID stripped only for the filename)
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # write the page range into a new PDF in the zip
            part_doc = fitz.open()
            for p in range(start - 1, end):
                part_doc.insert_pdf(doc, from_page=p, to_page=p)
            part_bytes = part_doc.tobytes()
            part_doc.close()

            zf.writestr(out_name, part_bytes)

    buf.seek(0)
    return buf, tpl, cat, loc, splits


# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────

uploads = st.file_uploader(
    "Upload ACC Build PDF(s)",
    type="pdf",
    accept_multiple_files=True,
)

remove_input = st.text_input("Remove patterns (* wildcards or regex)", "")
prefix = st.text_input("Filename prefix", "")
suffix = st.text_input("Filename suffix", "")
remove_id_prefix = st.checkbox(
    "Remove numeric ID prefix (e.g. ‘#6849: ’) from filenames only",
    value=True,
)
group_by = st.selectbox(
    "Group files in ZIP by:",
    ["None", "Template", "Location/Category"],
)

with st.expander("📘 Pattern tips"):
    st.markdown(
        """
- **Exact text**: just type it, e.g. `Checklist`  
- **Wildcard `*`**: matches any run of characters, e.g. `03.*_` removes `03.04_`, `03.03_`, etc.  
- **Combine with commas**: e.g. `03.*_, L2_`  
- Patterns apply **only to the saved filenames**, not to the ‘Form Name’ column.
        """
    )

if uploads:
    patterns = build_patterns(remove_input)

    # Initial read (load into memory & quick stats)
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(
        len(parse_toc(d, detect_toc_pages(d)))
        for d in docs
    )
    elapsed = int(time.perf_counter() - t0)
    mins, secs = divmod(elapsed, 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # Build master ZIP
    def get_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as master:
            for b in all_bytes:
                subzip, *_ = create_subzip(
                    b, patterns, prefix, suffix, remove_id_prefix, group_by
                )
                with zipfile.ZipFile(subzip) as sz:
                    for info in sz.infolist():
                        master.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = get_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # Live preview
    preview_rows = []
    for idx, (b, d) in enumerate(zip(all_bytes, docs)):
        tpl, cat, loc = extract_meta(d)
        splits = split_ranges(
            parse_toc(d, detect_toc_pages(d)), d.page_count
        )

        # Pretty folder names for the preview table
        folder_preview = ""
        if group_by == "Location/Category":
            folder_preview = (
                f"{loc} / {cat}" if (loc != 'Unknown' or cat != 'Unknown') else ""
            )
        elif group_by == "Template":
            folder_preview = tpl if tpl != "Unknown" else ""

        for title, start, end in splits:
            base = title
            if remove_id_prefix:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            fn = f"{prefix}{fname}{suffix}.pdf"

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Folder": folder_preview if folder_preview else "Unknown",
                "Form Name": title,
                "Pages": f"{start}-{end}",
                "Filename": fn,
            })

    df = pd.DataFrame(preview_rows)
    st.subheader("Filename & Page-Range Preview")
    st.dataframe(df, use_container_width=True)
