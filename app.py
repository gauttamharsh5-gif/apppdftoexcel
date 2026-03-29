import streamlit as st
import pdfplumber
import pandas as pd
import re
import json
import io
import requests

st.set_page_config(page_title="Bank Statement → Excel", page_icon="🏦", layout="centered")

st.markdown("""
<style>
    .stDownloadButton > button {
        background-color: #1e7e34; color: white; font-weight: 600;
        border-radius: 8px; padding: 0.5em 1.5em; width: 100%;
    }
    .stDownloadButton > button:hover { background-color: #155724; }
</style>
""", unsafe_allow_html=True)

GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")
DATE_PATTERN = re.compile(r"^\d{2}-\d{2}-\d{4}$")


# ==================================================
# HELPERS
# ==================================================
def is_date(val):
    return bool(val and DATE_PATTERN.match(str(val).strip()))


def clean_amount(val):
    if not val:
        return None
    val = str(val).strip()
    if not val or val.lower() in ("none", "-", ""):
        return None
    val = re.sub(r"\s*(DR|CR)\s*$", "", val, flags=re.IGNORECASE).strip()
    val = val.replace(",", "")
    try:
        return float(val)
    except ValueError:
        return None


# ==================================================
# METHOD 1 — TABLE EXTRACTION
# Reads actual PDF columns — fixes the mixing problem
# ==================================================
def extract_via_table(pdf_bytes: bytes):
    all_rows = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total = len(pdf.pages)
        progress = st.progress(0, text="Extracting tables from PDF...")

        for idx, page in enumerate(pdf.pages):
            progress.progress((idx + 1) / total, text=f"Page {idx+1} of {total}...")
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row:
                        continue
                    row = [str(c).strip().replace("\n", " ") if c else "" for c in row]
                    if is_date(row[0]):
                        all_rows.append(row)

        progress.empty()

    return all_rows


def table_rows_to_df(raw_rows):
    records = []
    for row in raw_rows:
        while len(row) < 6:
            row.append("")

        balance_raw = row[5].strip()
        is_dr = "DR" in balance_raw.upper()
        is_cr = "CR" in balance_raw.upper()

        records.append({
            "Date":        row[0].strip(),
            "Particulars": row[1].strip(),
            "Cheque_No":   row[2].strip(),
            "Withdrawals": clean_amount(row[3]),
            "Deposits":    clean_amount(row[4]),
            "Balance":     clean_amount(balance_raw),
            "Dr_Cr":       "DR" if is_dr else ("CR" if is_cr else "")
        })

    return pd.DataFrame(records)


# ==================================================
# METHOD 2 — GROQ AI + LINE PARSING (fallback)
# ==================================================
def generate_rules_via_groq(sample_text: str) -> dict:
    prompt = f"""You are a senior data engineer. Analyze this bank statement text and generate regex parsing rules.

BANK STATEMENT SAMPLE:
{sample_text[:3000]}

Return ONLY a valid JSON object with these exact keys (no explanation, no markdown):
{{
  "row_start_regex": "<regex matching the start of a transaction row>",
  "amount_regex": "<regex to extract monetary amounts with comma thousand separators>",
  "dr_keywords": ["<debit/withdrawal keyword>"],
  "cr_keywords": ["<credit/deposit keyword>"]
}}
Return ONLY the JSON, nothing else."""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.1, "max_tokens": 500},
        timeout=30
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    start, end = content.find("{"), content.rfind("}") + 1
    rules = json.loads(content[start:end])
    for k in ["row_start_regex", "amount_regex", "dr_keywords", "cr_keywords"]:
        if k not in rules:
            raise ValueError(f"Missing key: {k}")
    return rules


def extract_sample_text(pdf_bytes, pages=3):
    texts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i in range(min(pages, len(pdf.pages))):
            t = pdf.pages[i].extract_text()
            if t:
                texts.append(t)
    return "\n".join(texts)


def parse_via_text(pdf_bytes, rules):
    rows, current = [], None
    row_start = re.compile(rules["row_start_regex"])
    amount_re = re.compile(rules["amount_regex"])

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total = len(pdf.pages)
        progress = st.progress(0, text="Reading PDF text...")
        for idx, page in enumerate(pdf.pages):
            text = page.extract_text()
            progress.progress((idx + 1) / total, text=f"Page {idx+1} of {total}...")
            if not text:
                continue
            for line in [l.strip() for l in text.split("\n") if l.strip()]:
                if row_start.match(line):
                    if current:
                        rows.append(current)
                    parts = line.split(maxsplit=1)
                    current = {"Date": parts[0], "Particulars": parts[1] if len(parts) > 1 else "",
                               "Cheque_No": "", "Withdrawals": None, "Deposits": None,
                               "Balance": None, "_raw": line}
                elif current:
                    current["Particulars"] += " " + line
                    current["_raw"] += " " + line
        progress.empty()

    if current:
        rows.append(current)

    for r in rows:
        amounts = [a.replace(",", "") for a in amount_re.findall(r["_raw"])]
        if amounts:
            r["Balance"] = amounts[-1]
        if any(k in r["_raw"] for k in rules["dr_keywords"]):
            r["Withdrawals"] = r["Balance"]
        elif any(k in r["_raw"] for k in rules["cr_keywords"]):
            r["Deposits"] = r["Balance"]
        r["Particulars"] = r["Particulars"].strip()

    df = pd.DataFrame(rows)
    df.drop(columns=["_raw"], inplace=True, errors="ignore")
    return df


# ==================================================
# TO EXCEL
# ==================================================
def df_to_excel(df: pd.DataFrame) -> io.BytesIO:
    for col in ["Withdrawals", "Deposits", "Balance"]:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "", regex=False).replace("None", None),
                errors="coerce"
            )
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transactions")
    buf.seek(0)
    return buf


# ==================================================
# SIDEBAR
# ==================================================
with st.sidebar:
    st.header("⚙️ Configuration")

    method = st.radio(
        "🔍 Parsing Method",
        ["🤖 Auto — Table Extraction (Recommended)", "📝 AI Text Rules (Groq)"],
        help="Table Extraction reads PDF columns directly — much more accurate."
    )

    if GROQ_API_KEY:
        st.success("✅ Groq AI available")
    else:
        if "AI" in method:
            st.warning("⚠️ No Groq key found in secrets.")

    st.divider()
    st.subheader("📂 Manual Rules (AI method only)")
    rules_file = st.file_uploader("Upload parsing_rules.json", type=["json"])
    manual_rules = None
    if rules_file:
        try:
            manual_rules = json.load(rules_file)
            st.success("✅ Manual rules loaded!")
            st.json(manual_rules)
        except Exception as e:
            st.error(f"Invalid JSON: {e}")


# ==================================================
# MAIN
# ==================================================
st.title("🏦 Bank Statement PDF → Excel")
st.markdown("Accurately extracts **Date, Particulars, Cheque No, Withdrawals, Deposits, Balance** from any bank PDF.")
st.divider()

pdf_file = st.file_uploader("📄 Browse & Upload Bank Statement PDF", type=["pdf"])

if pdf_file:
    st.success(f"✅ **{pdf_file.name}** uploaded ({pdf_file.size / 1024:.1f} KB)")
    pdf_bytes = pdf_file.read()

    if st.button("🚀 Convert to Excel", use_container_width=True, type="primary"):
        try:
            df = None

            # ── TABLE EXTRACTION ──────────────────────────────
            if "Auto" in method:
                st.info("🔍 Reading PDF columns directly...")
                raw_rows = extract_via_table(pdf_bytes)

                if raw_rows:
                    df = table_rows_to_df(raw_rows)
                    st.success(f"✅ Found {len(df)} transactions via table extraction!")
                else:
                    st.warning("⚠️ No tables detected — switching to AI text rules...")

            # ── AI TEXT RULES ─────────────────────────────────
            if df is None or df.empty:
                if manual_rules:
                    rules = manual_rules
                    st.info("📂 Using manually uploaded rules.")
                elif GROQ_API_KEY:
                    with st.status("🤖 AI analyzing PDF...", expanded=True) as status:
                        sample = extract_sample_text(pdf_bytes)
                        rules = generate_rules_via_groq(sample)
                        status.update(label="✅ Rules generated!", state="complete")
                    st.json(rules)
                    st.download_button("⬇️ Save rules JSON", json.dumps(rules, indent=2),
                                       "parsing_rules.json", "application/json")
                else:
                    st.error("❌ No tables found and no Groq key. Try the AI method with a key.")
                    st.stop()

                df = parse_via_text(pdf_bytes, rules)

            # ── RESULTS ───────────────────────────────────────
            if df is None or df.empty:
                st.error("❌ No transactions found. Try switching the parsing method in the sidebar.")
            else:
                excel_buf = df_to_excel(df)

                st.divider()
                st.subheader("📊 Summary")
                c1, c2, c3 = st.columns(3)
                c1.metric("Transactions", len(df))
                total_dr = pd.to_numeric(df.get("Withdrawals", pd.Series()), errors="coerce").dropna().sum()
                total_cr = pd.to_numeric(df.get("Deposits", pd.Series()), errors="coerce").dropna().sum()
                c2.metric("Total Withdrawals", f"₹{total_dr:,.2f}" if total_dr else "—")
                c3.metric("Total Deposits", f"₹{total_cr:,.2f}" if total_cr else "—")

                st.subheader("👀 Preview (first 10 rows)")
                st.dataframe(df.head(10), use_container_width=True)

                st.divider()
                st.download_button(
                    label="⬇️ Download Excel File",
                    data=excel_buf,
                    file_name=f"{pdf_file.name.replace('.pdf', '')}_transactions.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

        except requests.exceptions.HTTPError as e:
            st.error(f"❌ Groq API error: {e.response.status_code}")
        except Exception as e:
            st.error(f"❌ Error: {e}")
            st.exception(e)

else:
    st.info("👆 Upload a PDF file above to get started.")

st.divider()
st.caption("💡 Table Extraction reads actual PDF columns — no regex needed. Use AI method only as fallback.")
