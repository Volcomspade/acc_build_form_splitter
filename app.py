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
    """Make a string safe for filenames & folders."""
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def build_patterns(raw: str) -> List[str]:
    """
    Comma-separated tokens → regex patterns with '*' as wildcard.
    """
    pats: List[str] = []
    for tok in [t.strip() for t in raw.split(",") if t.strip()]:
        esc = re.escape(tok)
        esc = esc.replace(r"\*", ".*")  # allow wildcard
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
      2) Two-line table:
           Key
           value
    """
    # Case 1
    m = re.search(rf"(?im)^\s*{re.escape(key)}\s*[:\-]\s*(.+?)\s*$", text)
    if m:
        return m.group(1).strip()

    # Case 2
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.lower() == key.lower():
            v = _first_nonempty_after(lines, i)
            if v and v.lower() != key.lower():
                return v.strip()
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Per-split metadata extraction
# ──────────────────────────────────────────────────────────────────────────────

def extract_meta_for_split(doc: fitz.Document, start0: int, end0: int) -> Tuple[str, str, str]:
    """
    Extract (template, category, location) for a single split.
    - template: from the split's first page "Forms" table (key 'Template')
    - category/location: from 'References and Attachments' nearby pages
    """
    template = "Unknown"
    category = "Unknown"
    location = "Unknown"

    # --- Template from the split's first page
    try:
        p0_text = doc.load_page(start0).get_text() or ""
        t = _grab_key_value(p0_text, "Template")
        if t:
            template = t
        else:
            # fallback: exhibit-ish line on that page
            for ln in p0_text.splitlines():
                low = ln.lower()
                if "exhibit" in low and ("checklist" in low or "inspection" in low or "acc build" in low):
                    template = ln.strip()
                    break
    except Exception:
        pass

    # --- Category & Location from the next few pages in this split
    # Keep it local to the split to avoid bleeding across forms
    try:
        scan_to = min(end0, start0 + 12)  # look at first ~12 pages of the split
        for p in range(start0, scan_to + 1):
            tx = doc.load_page(p).get_text() or ""
            vcat = _grab_key_value(tx, "Category")
            vloc = _grab_key_value(tx, "Location")

            if vcat:
                category = vcat
            if vloc and "form detail report report is split" not in vloc.lower():
                location = vloc

            if category != "Unknown" and location != "Unknown":
                break
    except Exception:
        pass

    return template, category, location


# ──────────────────────────────────────────────────────────────────────────────
# TOC parsing
# ──────────────────────────────────────────────────────────────────────────────

def detect_toc_pages(doc: fitz.Document) -> List[int]:
    entry_rx = re.compile(r"^#\s*\d+:", re.MULTILINE)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        if entry_rx.search(text or ""):
            pages.append(i + 1)  # 1-based for human-like
    return pages


def parse_toc(doc: fitz.Document, toc_pages: List[int]) -> List[Tuple[str, int]]:
    toc_rx = re.compile(r"#\s*\d+:\s*(.+?)\.{3,}\s*(\d+)", re.MULTILINE)
    entries: List[Tuple[str, int]] = []
    for pg in toc_pages:
        text = doc.load_page(pg - 1).get_text() or ""
        for m in toc_rx.finditer(text):
            entries.append((m.group(1).strip(), int(m.group(2))))
    return entries


def split_ranges(entries: List[Tuple[str, int]], total: int) -> List[Tuple[str, int, int]]:
    out: List[Tuple[str, int, int]] = []
    for i, (title, start) in enumerate(entries):
        end = entries[i + 1][1] - 1 if i + 1 < len(entries) else total
        out.append((title, start, end))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Zip creation (per PDF, per split foldering)
# ──────────────────────────────────────────────────────────────────────────────

def create_subzip(
    pdf_bytes: bytes,
    patterns: List[str],
    prefix: str,
    suffix: str,
    remove_id: bool,
    group_by: str,
) -> io.BytesIO:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = doc.page_count

    toc_pages = detect_toc_pages(doc)
    entries = parse_toc(doc, toc_pages)
    splits = split_ranges(entries, total_pages)

    def folderify_for_path(s: str) -> str:
        # turn 'A > B > C' into subfolders A/B/C and slugify each segment
        parts = [slugify(p.strip()) for p in s.split(">")]
        parts = [p for p in parts if p]
        return "/".join(parts)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for title, start, end in splits:
            # metadata for *this split*
            tpl, cat, loc = extract_meta_for_split(doc, start - 1, end - 1)

            # decide folder
            if group_by == "Location/Category":
                combined = ""
                if loc != "Unknown":
                    combined = loc
                if cat != "Unknown":
                    combined = f"{combined} > {cat}" if combined else cat
                folder = folderify_for_path(combined)
                folder = f"{folder}/" if folder else ""
            elif group_by == "Template":
                folder = folderify_for_path(tpl)
                folder = f"{folder}/" if folder else ""
            else:
                folder = ""

            # filename sanitization (strip ID only in filename)
            base = title
            if remove_id:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fname = slugify(base)
            out_name = f"{folder}{prefix}{fname}{suffix}.pdf"

            # write pages
            part_doc = fitz.open()
            for p in range(start - 1, end):
                part_doc.insert_pdf(doc, from_page=p, to_page=p)
            zf.writestr(out_name, part_doc.tobytes())
            part_doc.close()

    buf.seek(0)
    return buf


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
- **Combine with commas**: `03.*_, L2_`
- Patterns apply **only to the saved filenames**, not to the “Form Name” column.
        """
    )

if uploads:
    patterns = build_patterns(remove_input)

    # Read in memory & quick stats
    t0 = time.perf_counter()
    all_bytes = [f.read() for f in uploads]
    docs = [fitz.open(stream=b, filetype="pdf") for b in all_bytes]
    total_pages = sum(d.page_count for d in docs)
    total_forms = sum(len(parse_toc(d, detect_toc_pages(d))) for d in docs)
    elapsed = int(time.perf_counter() - t0)
    mins, secs = divmod(elapsed, 60)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source PDFs", len(uploads))
    c2.metric("Total pages", total_pages)
    c3.metric("Total forms", total_forms)
    c4.metric("Initial read", f"{mins:02d}:{secs:02d}")

    # Build ZIP
    def make_master_zip():
        mz = io.BytesIO()
        with zipfile.ZipFile(mz, "w") as master:
            for b in all_bytes:
                sub = create_subzip(b, patterns, prefix, suffix, remove_id_prefix, group_by)
                with zipfile.ZipFile(sub) as sz:
                    for info in sz.infolist():
                        master.writestr(info.filename, sz.read(info.filename))
        mz.seek(0)
        return mz

    zip_buf = make_master_zip()
    st.download_button(
        "Download all splits",
        zip_buf,
        file_name="acc_build_forms.zip",
        mime="application/zip",
    )

    # Live preview (metadata per split)
    preview_rows = []
    for idx, (b, d) in enumerate(zip(all_bytes, docs)):
        entries = parse_toc(d, detect_toc_pages(d))
        splits = split_ranges(entries, d.page_count)

        for title, start, end in splits:
            tpl, cat, loc = extract_meta_for_split(d, start - 1, end - 1)

            # display folder preview with > separators for readability
            if group_by == "Location/Category":
                pieces = []
                if loc != "Unknown":
                    pieces.append(loc)
                if cat != "Unknown":
                    pieces.append(cat)
                folder_preview = " > ".join(pieces) if pieces else ""
            elif group_by == "Template":
                folder_preview = tpl if tpl != "Unknown" else ""
            else:
                folder_preview = ""

            base = title
            if remove_id_prefix:
                base = re.sub(r"^#\s*\d+:\s*", "", base)
            for rx in patterns:
                base = re.sub(rx, "", base, flags=re.IGNORECASE)
            fn = f"{prefix}{slugify(base)}{suffix}.pdf"

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Folder": folder_preview if folder_preview else "Unknown",
                "Form Name": title,
                "Pages": f"{start}-{end}",
                "Filename": fn,
            })

    st.subheader("Filename & Page-Range Preview")
    st.dataframe(pd.DataFrame(preview_rows), use_container_width=True)
