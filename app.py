import streamlit as st
import pdfplumber
import pandas as pd
import re
import json
import io
import requests

# ==================================================
# PAGE CONFIG
# ==================================================
st.set_page_config(
    page_title="Bank Statement → Excel",
    page_icon="🏦",
    layout="centered"
)

st.markdown("""
<style>
    .stDownloadButton > button {
        background-color: #1e7e34;
        color: white;
        font-weight: 600;
        border-radius: 8px;
        padding: 0.5em 1.5em;
        width: 100%;
    }
    .stDownloadButton > button:hover { background-color: #155724; }
</style>
""", unsafe_allow_html=True)

# ==================================================
# GROQ API KEY — loaded from Streamlit Secrets only
# On Streamlit Cloud: Settings → Secrets → paste:
#   GROQ_API_KEY = "your_key_here"
# Locally: create .streamlit/secrets.toml and paste:
#   GROQ_API_KEY = "your_key_here"
# ==================================================
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")

# ==================================================
# DEFAULT RULES (fallback)
# ==================================================
DEFAULT_RULES = {
    "row_start_regex": r"\d{2}-\d{2}-\d{4}",
    "amount_regex": r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?",
    "dr_keywords": [" DR"],
    "cr_keywords": [" CR"]
}

# ==================================================
# GROQ — AUTO-GENERATE RULES
# ==================================================
def generate_rules_via_groq(sample_text: str) -> dict:
    prompt = f"""You are a senior data engineer. Analyze this bank statement text and generate regex parsing rules.

BANK STATEMENT SAMPLE:
{sample_text[:3000]}

Return ONLY a valid JSON object with these exact keys (no explanation, no markdown):
{{
  "row_start_regex": "<regex that matches the START of a transaction row, usually a date pattern>",
  "amount_regex": "<regex to extract monetary amounts, handle commas as thousand separators>",
  "dr_keywords": ["<keyword(s) that indicate a debit/withdrawal>"],
  "cr_keywords": ["<keyword(s) that indicate a credit/deposit>"]
}}

Rules:
- row_start_regex must match the date format used in this specific statement
- amount_regex must capture numbers like 1,234.56 or 1234.56 or 12,34,567.89
- dr_keywords and cr_keywords must match the exact strings used in this statement
- Return ONLY the JSON, nothing else"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 500
    }

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=30
    )
    resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"].strip()

    start = content.find("{")
    end = content.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON found in LLM response")

    rules = json.loads(content[start:end])

    for k in ["row_start_regex", "amount_regex", "dr_keywords", "cr_keywords"]:
        if k not in rules:
            raise ValueError(f"Missing key in LLM response: {k}")

    return rules


def extract_sample_text(pdf_bytes: bytes, pages: int = 3) -> str:
    texts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i in range(min(pages, len(pdf.pages))):
            t = pdf.pages[i].extract_text()
            if t:
                texts.append(t)
    return "\n".join(texts)


# ==================================================
# PARSE PDF
# ==================================================
def parse_pdf(pdf_bytes: bytes, rules: dict) -> list:
    rows = []
    current = None

    row_start = re.compile(rules["row_start_regex"])
    amount_re = re.compile(rules["amount_regex"])

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total_pages = len(pdf.pages)
        progress = st.progress(0, text="Reading PDF pages...")

        for idx, page in enumerate(pdf.pages):
            text = page.extract_text()
            progress.progress((idx + 1) / total_pages, text=f"Reading page {idx + 1} of {total_pages}...")
            if not text:
                continue

            for line in [l.strip() for l in text.split("\n") if l.strip()]:
                if row_start.match(line):
                    if current:
                        rows.append(current)
                    parts = line.split(maxsplit=1)
                    current = {
                        "Date": parts[0],
                        "Particulars": parts[1] if len(parts) > 1 else "",
                        "Cheque_No": "",
                        "Withdrawals": None,
                        "Deposits": None,
                        "Balance": None,
                        "_raw": line
                    }
                elif current:
                    current["Particulars"] += " " + line
                    current["_raw"] += " " + line

        progress.empty()

    if current:
        rows.append(current)

    for r in rows:
        text = r["_raw"]
        amounts = [a.replace(",", "") for a in amount_re.findall(text)]
        if amounts:
            r["Balance"] = amounts[-1]
        if any(k in text for k in rules["dr_keywords"]):
            r["Withdrawals"] = r["Balance"]
        elif any(k in text for k in rules["cr_keywords"]):
            r["Deposits"] = r["Balance"]
        r["Particulars"] = r["Particulars"].strip()

    return rows


def rows_to_excel(rows: list):
    df = pd.DataFrame(rows)
    df.drop(columns=["_raw"], inplace=True, errors="ignore")

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
    return buf, df


# ==================================================
# SIDEBAR
# ==================================================
with st.sidebar:
    st.header("⚙️ Configuration")

    if GROQ_API_KEY:
        st.success("✅ AI Auto-Detection is ON")
        st.caption("Rules will be generated automatically for any bank PDF.")
    else:
        st.warning("⚠️ No Groq key found. Add it in Streamlit Secrets.")

    st.divider()

    st.subheader("📂 Manual Rules Override")
    st.markdown("Upload a `parsing_rules.json` to skip AI detection entirely.")
    rules_file = st.file_uploader("Upload parsing_rules.json", type=["json"])

    manual_rules = None
    if rules_file:
        try:
            manual_rules = json.load(rules_file)
            st.success("✅ Manual rules loaded!")
            st.json(manual_rules)
        except Exception as e:
            st.error(f"Invalid JSON: {e}")

    st.divider()
    st.download_button(
        label="⬇️ Download Default Rules JSON",
        data=json.dumps(DEFAULT_RULES, indent=2),
        file_name="parsing_rules.json",
        mime="application/json"
    )


# ==================================================
# MAIN
# ==================================================
st.title("🏦 Bank Statement PDF → Excel")
st.markdown("Works with **any bank's PDF** — AI auto-detects the format for you.")
st.divider()

pdf_file = st.file_uploader(
    "📄 Browse & Upload Bank Statement PDF",
    type=["pdf"],
    help="Upload the PDF exported from your bank's internet banking portal"
)

if pdf_file:
    st.success(f"✅ **{pdf_file.name}** uploaded ({pdf_file.size / 1024:.1f} KB)")
    pdf_bytes = pdf_file.read()

    if st.button("🚀 Convert to Excel", use_container_width=True, type="primary"):
        try:
            # ── STEP 1: Determine rules ──────────────────────
            if manual_rules:
                rules = manual_rules
                st.info("📂 Using manually uploaded rules.")

            elif GROQ_API_KEY:
                with st.status("🤖 AI is analyzing your PDF format...", expanded=True) as status:
                    st.write("Extracting sample text from PDF...")
                    sample = extract_sample_text(pdf_bytes, pages=3)
                    st.write("Sending to Groq (Llama 3.1 8B)...")
                    rules = generate_rules_via_groq(sample)
                    status.update(label="✅ Rules auto-generated!", state="complete")

                st.success("🤖 AI-generated rules:")
                st.json(rules)

                st.download_button(
                    label="⬇️ Save these rules as JSON (reuse without AI next time)",
                    data=json.dumps(rules, indent=2),
                    file_name="parsing_rules.json",
                    mime="application/json"
                )

            else:
                rules = DEFAULT_RULES
                st.warning("⚠️ Using default rules — may not work for all PDFs.")

            # ── STEP 2: Parse ─────────────────────────────────
            st.divider()
            rows = parse_pdf(pdf_bytes, rules)

            if not rows:
                st.error("❌ No transactions found. Try uploading a manual rules JSON from the sidebar.")
            else:
                excel_buf, df = rows_to_excel(rows)

                st.subheader("📊 Summary")
                total_txns = len(df)
                total_dr = df["Withdrawals"].dropna().sum() if "Withdrawals" in df.columns else 0
                total_cr = df["Deposits"].dropna().sum() if "Deposits" in df.columns else 0

                c1, c2, c3 = st.columns(3)
                c1.metric("Transactions", total_txns)
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
            st.error(f"❌ Groq API error: {e.response.status_code} — check your API key in Streamlit Secrets.")
        except Exception as e:
            st.error(f"❌ Error: {e}")

else:
    st.info("👆 Upload a PDF file above to get started.")

st.divider()
st.caption("💡 Tip: Once AI generates rules for your bank, save the JSON and re-upload it next time — no API call needed!")
