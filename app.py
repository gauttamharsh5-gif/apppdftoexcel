import streamlit as st
import pdfplumber
import pandas as pd
import re
import json
import io
import requests

# ==============================
# CONFIG
# ==============================
st.set_page_config(page_title="Smart PDF → Excel", layout="centered")

GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")

# ==============================
# HELPERS
# ==============================
def is_date(val):
    return bool(val and re.match(r"\d{2}-\d{2}-\d{4}", str(val).strip()))

def clean_amount(val):
    if not val:
        return None
    val = str(val).replace(",", "").strip()
    val = re.sub(r"(DR|CR)$", "", val).strip()
    try:
        return float(val)
    except:
        return None

def open_pdf_with_password(pdf_bytes, password=None):
    try:
        return pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except:
        return None

# ==============================
# TABLE EXTRACTION
# ==============================
def extract_via_table(pdf_bytes, password=None):
    rows = []
    pdf = open_pdf_with_password(pdf_bytes, password)

    if pdf is None:
        return pd.DataFrame()

    with pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if row and is_date(row[0]):
                        row = [str(c).strip() if c else "" for c in row]
                        while len(row) < 6:
                            row.append("")

                        rows.append({
                            "Date": row[0],
                            "Particulars": row[1],
                            "Cheque_No": row[2],
                            "Withdrawals": clean_amount(row[3]),
                            "Deposits": clean_amount(row[4]),
                            "Balance": clean_amount(row[5])
                        })

    return pd.DataFrame(rows)

# ==============================
# SAMPLE TEXT
# ==============================
def extract_sample_text(pdf_bytes, password=None, pages=2):
    texts = []
    pdf = open_pdf_with_password(pdf_bytes, password)

    if pdf is None:
        return ""

    with pdf:
        for i in range(min(pages, len(pdf.pages))):
            t = pdf.pages[i].extract_text()
            if t:
                texts.append(t)

    return "\n".join(texts)

# ==============================
# LLM: STRUCTURE DETECTION
# ==============================
def detect_structure_with_llm(sample_text, sample_table):
    prompt = f"""
You are a data engineer.

Analyze this document.

TEXT:
{sample_text[:2000]}

TABLE SAMPLE:
{str(sample_table[:5])}

Return ONLY JSON:

{{
  "is_standard": true/false,
  "recommended_method": "table" or "llm",
  "reason": "short reason"
}}
"""

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0
        }
    )

    content = response.json()["choices"][0]["message"]["content"]

    try:
        return json.loads(content)
    except:
        start = content.find("{")
        end = content.rfind("}") + 1
        return json.loads(content[start:end])

# ==============================
# LLM: DATA EXTRACTION
# ==============================
def extract_structured_via_llm(text):
    prompt = f"""
Extract data into JSON.

Columns:
Date, Particulars, Cheque_No, Withdrawals, Deposits, Balance

Return ONLY JSON array.

TEXT:
{text[:4000]}
"""

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0
        }
    )

    content = response.json()["choices"][0]["message"]["content"]

    # Robust JSON parsing
    try:
        return pd.DataFrame(json.loads(content))
    except:
        pass

    try:
        start = content.find("[")
        end = content.rfind("]") + 1
        return pd.DataFrame(json.loads(content[start:end]))
    except:
        pass

    try:
        cleaned = re.sub(r"```json|```", "", content).strip()
        return pd.DataFrame(json.loads(cleaned))
    except:
        st.error("❌ JSON parsing failed")
        st.code(content)
        return pd.DataFrame()

# ==============================
# VALIDATION
# ==============================
def validate_first_page(df):
    issues = []

    if df.empty:
        return ["No data"]

    bad_dates = df[~df["Date"].astype(str).str.match(r"\d{2}-\d{2}-\d{4}", na=False)]
    if not bad_dates.empty:
        issues.append("Invalid dates found")

    both = df[df["Withdrawals"].notna() & df["Deposits"].notna()]
    if not both.empty:
        issues.append("Debit & Credit both filled")

    if "Balance" in df.columns:
        if df["Balance"].isna().mean() > 0.5:
            issues.append("Too many missing balances")

    return issues

# ==============================
# EXCEL
# ==============================
def df_to_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return buf

# ==============================
# UI
# ==============================
st.title("🧠 Smart PDF → Excel")

pdf_file = st.file_uploader("Upload PDF", type=["pdf"])

if pdf_file:

    pdf_bytes = pdf_file.read()
    password = st.text_input("🔐 Password (if needed)", type="password")

    if st.button("🚀 Convert"):

        # STEP 1: Extract both
        table_df = extract_via_table(pdf_bytes, password=password or None)
        sample_text = extract_sample_text(pdf_bytes, password=password or None)

        # STEP 2: LLM decision
        with st.spinner("🧠 Understanding structure..."):
            decision = detect_structure_with_llm(
                sample_text,
                table_df.to_dict("records")
            )

        st.json(decision)

        # STEP 3: Route
        if decision["recommended_method"] == "table" and not table_df.empty:

            st.success("✅ Using table extraction")
            df = table_df

            issues = validate_first_page(df.head(50))

            if issues:
                st.warning("⚠️ Validation failed → switching to LLM")
                df = extract_structured_via_llm(sample_text)

        else:
            st.warning("🤖 Using LLM extraction")
            df = extract_structured_via_llm(sample_text)

        # STEP 4: Output
        if df.empty:
            st.error("❌ No data extracted")
        else:
            st.success("✅ Done")

            st.dataframe(df.head(10), use_container_width=True)

            total_dr = pd.to_numeric(df.get("Withdrawals"), errors="coerce").sum()
            total_cr = pd.to_numeric(df.get("Deposits"), errors="coerce").sum()

            st.write(f"💸 Withdrawals: ₹{total_dr:,.2f}")
            st.write(f"💰 Deposits: ₹{total_cr:,.2f}")

            excel = df_to_excel(df)

            st.download_button(
                "⬇️ Download Excel",
                data=excel,
                file_name="output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

else:
    st.info("Upload a PDF to start.")