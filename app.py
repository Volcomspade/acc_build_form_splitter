# … your existing imports and UI …

remove_input = st.text_input(
    "Remove patterns (* wildcards or regex)", 
    "", 
    help="Use either pure‑regex or *‑wildcards.  * → .*  (e.g. \\d{2}\\.\\d{2}_ strips '03.04_')."
)

# --- add this block right below it ---
with st.expander("🛈 Regex helper / wildcard tips"):
    st.markdown("""
- **Pure‑regex** (recommended):  
  - `\d{2}\.\d{2}_`  
    matches exactly two digits, a dot, two digits, then an underscore (e.g. `03.04_`, `12.34_`).

- **Wildcard** syntax:  
  - In your input, `*` becomes `.*` under the hood.  
  - To match `03.04_`, you could write `03.04_*`, which turns into `03\.04_.*`.
  - If you want “any two‑digit dot two‑digit underscore”, try: `*[0-9][0-9]\.[0-9][0-9]_*`

- **Escaping special chars**:  
  - `.` → `\.`  
  - `\` → `\\`

- **Examples**:  
  - Remove `XX.XX_` patterns: `\d{2}\.\d{2}_`  
  - Strip *any* “02.03”, “03.04”, etc: `\d{2}\.\d{2}`  
  - Wildcard remove everything after underscore: `*_`  
    (be careful—this will eat the rest of the filename unless you anchor it)

Feel free to tweak or add more examples here!
""")
# — end expander —

# … the rest of your Streamlit UI …
