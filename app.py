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
st.set_page_config(page_title="PDF → Excel", layout="centered")

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

# ==============================
# PASSWORD HANDLING
# ==============================
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
        raise ValueError("Incorrect password or unreadable PDF")

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
# LLM EXTRACTION (FIXED)
# ==============================
def extract_structured_via_llm(text):
    prompt = f"""
You are a financial data extraction system.

Extract ALL transactions into JSON.

Columns:
- Date (DD-MM-YYYY)
- Particulars
- Cheque_No
- Withdrawals
- Deposits
- Balance

STRICT RULES:
- Return ONLY JSON array
- No explanation
- No markdown
- Use null for missing values

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

    # =========================
    # ROBUST JSON PARSING
    # =========================

    # 1️⃣ Direct JSON
    try:
        return pd.DataFrame(json.loads(content))
    except:
        pass

    # 2️⃣ Extract JSON array
    try:
        start = content.find("[")
        end = content.rfind("]") + 1
        json_str = content[start:end]
        return pd.DataFrame(json.loads(json_str))
    except:
        pass

    # 3️⃣ Remove markdown
    try:
        cleaned = re.sub(r"```json|```", "", content).strip()
        return pd.DataFrame(json.loads(cleaned))
    except:
        pass

    # ❌ Fail → show debug
    st.error("❌ JSON parsing failed")
    st.code(content)
    raise ValueError("Invalid LLM JSON")

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
# VALIDATION
# ==============================
def validate_first_page(df):
    issues = []

    if df.empty:
        return ["No data"]

    bad_dates = df[~df["Date"].astype(str).str.match(r"\d{2}-\d{2}-\d{4}", na=False)]
    if not bad_dates.empty:
        issues.append(f"{len(bad_dates)} invalid dates")

    both = df[df["Withdrawals"].notna() & df["Deposits"].notna()]
    if not both.empty:
        issues.append(f"{len(both)} rows with both debit & credit")

    if df["Balance"].isna().mean() > 0.5:
        issues.append("Too many missing balances")

    return issues

# ==============================
# TO EXCEL
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
st.title("🏦 Bank PDF → Excel")

pdf_file = st.file_uploader("Upload PDF", type=["pdf"])

if pdf_file:

    pdf_bytes = pdf_file.read()

    password = st.text_input("🔐 Enter Password (if protected)", type="password")

    if st.button("🚀 Convert"):

        df = pd.DataFrame()

        # STEP 1: TABLE EXTRACTION
        try:
            with st.spinner("🔍 Reading tables..."):
                df = extract_via_table(pdf_bytes, password=password or None)

            if not df.empty:
                st.success(f"✅ Table extraction worked ({len(df)} rows)")
            else:
                st.warning("⚠️ No table data found")

        except Exception:
            st.warning("⚠️ PDF locked or table extraction failed")

        # STEP 2: VALIDATION
        if not df.empty:
            issues = validate_first_page(df.head(20))

            if issues:
                st.warning("⚠️ Validation issues:")
                for i in issues:
                    st.write(f"- {i}")
                df = pd.DataFrame()
            else:
                st.success("✅ First page validated!")

        # STEP 3: LLM FALLBACK
        if df.empty:

            if not GROQ_API_KEY:
                st.error("❌ Missing Groq API key")
                st.stop()

            with st.spinner("🤖 AI extracting..."):
                text = extract_sample_text(pdf_bytes, password=password or None)

                if not text:
                    st.error("❌ Could not read PDF (check password)")
                    st.stop()

                df = extract_structured_via_llm(text)

        # FINAL OUTPUT
        if df.empty:
            st.error("❌ No data extracted")
        else:
            st.success("✅ Done!")

            st.dataframe(df.head(10), use_container_width=True)

            total_dr = pd.to_numeric(df["Withdrawals"], errors="coerce").sum()
            total_cr = pd.to_numeric(df["Deposits"], errors="coerce").sum()

            st.write(f"💸 Withdrawals: ₹{total_dr:,.2f}")
            st.write(f"💰 Deposits: ₹{total_cr:,.2f}")

            excel = df_to_excel(df)

            st.download_button(
                "⬇️ Download Excel",
                data=excel,
                file_name="transactions.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

else:
    st.info("Upload a PDF to begin.")