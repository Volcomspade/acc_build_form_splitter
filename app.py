    # --- Preview Generation with Progress Bar ---
    st.subheader("Filename & Page‑Range Preview")
    progress = st.progress(0)

    preview_rows = []
    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        reader      = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        toc_pages   = detect_toc_pages(reader)
        toc_entries = parse_toc(reader, toc_pages)
        splits      = split_ranges(toc_entries, total_pages)

        for raw, start, end in splits:
            name = raw
            if remove_id:
                name = re.sub(r'^#\s*\d+:?\s*', '', name)
            for rx in patterns:
                name = re.sub(rx, '', name, flags=re.IGNORECASE)
            clean = re.sub(r'\s+', '_', name.strip())
            clean = re.sub(r'[\\/:*?"<>|]', '', clean)
            fname = f"{prefix}{clean}{suffix}.pdf"

            preview_rows.append({
                "Source PDF": uploads[idx].name,
                "Form Name":  raw,
                "Pages":      f"{start}–{end}",
                "Filename":   fname,
            })

        progress.progress((idx + 1) / len(pdf_bytes_list))

    df = pd.DataFrame(preview_rows)

    # mark duplicates in Filename
    dup = df["Filename"].duplicated(keep=False)
    styled = (
        df.style
          .set_properties(subset=["Form Name","Filename"], **{"white-space":"pre-wrap"})
          .apply(lambda col: ["color: red;" if is_dup else "" for is_dup in dup],
                 subset=["Filename"], axis=0)
    )

    # show full‐width dataframe
    st.dataframe(styled, use_container_width=True)
