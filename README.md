# ACC Build TOC PDF Splitter

A Streamlit app to automatically split ACC Build “Form detail” PDFs by their Table of Contents entries.

## Features

- **Auto‑detects** TOC pages and parses form names and page numbers.
- **Splits** each form into its own PDF named after the form.
- **Supports** regex and simple `*` wildcards for removal patterns.
- **Live preview** table of filenames and page ranges.
- **Download button** at the top for instant ZIP download.

## Setup

```bash
git clone https://github.com/<your-username>/acc_build_form_splitter.git
cd acc_build_form_splitter

python3 -m venv env
source env/bin/activate  # or `env\Scripts\activate` on Windows

pip install -r requirements.txt
