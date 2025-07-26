# ACC Build TOC PDF Splitter

A Streamlit app to automatically split ACC Build "Form detail" PDFs by their Table of Contents entries.

## Features

- **Auto‑detects** TOC pages by scanning for lines like `#1234: Title … 56`.
- **Parses** all `#...:` entries and splits the PDF into separate files.
- **Customizable** removal of text via regex patterns (e.g. dates, codes, suffixes).
- **Prefix/Suffix** options to template output filenames.
- **Batch processing**: upload multiple PDFs at once and download all splits in a ZIP.

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/<your-username>/acc_build_streamlit_splitter.git
cd acc_build_streamlit_splitter

# 2. Create a virtual environment (optional but recommended)
python3 -m venv env
source env/bin/activate  # on macOS/Linux
# env\Scripts\activate  # on Windows

# 3. Install dependencies
pip install -r requirements.txt
```

## Running

```bash
streamlit run app.py
```

Then open the URL shown in your terminal (usually http://localhost:8501).

## Usage

1. **Upload** one or more ACC Build PDFs.  
2. Enter **comma‑separated regexes** to remove unwanted text (e.g. `RREG,\d{6}`).  
3. Set **filename prefix** and/or **suffix**.  
4. Click **Split & Download ZIP**.

---

## License

MIT License
