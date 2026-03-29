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
st.set_page_config(page_title="Robust PDF → Excel", layout="centered")
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")

# ==============================
# HELPERS
# ==============================
def safe_json_loads(text):
    try:
        return json.loads(text)
    except:
        pass

    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except:
        pass

    try:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except:
        pass

    return None


def is_date(val):
    return bool(val and re.match(r"\d{2}-\d{2}-\d{4}", str(val)))


def clean_amount(val):
    if not val:
        return None
    val = str(val).replace(",", "").strip()
    val = re.sub(r"(DR|CR)$", "", val)
    try:
        return float(val)
    except:
        return None


def open_pdf(pdf_bytes, password=None):
    try:
        return pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except:
        return None


# ==============================
# TABLE EXTRACTION
# ==============================
def extract_via_table(pdf_bytes, password=None):
    rows = []
    pdf = open_pdf(pdf_bytes, password)

    if not pdf:
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
# TEXT EXTRACTION
# ==============================
def extract_text(pdf_bytes, password=None, pages=2):
    pdf = open_pdf(pdf_bytes, password)
    if not pdf:
        return ""

    texts = []
    with pdf:
        for i in range(min(pages, len(pdf.pages))):
            t = pdf.pages[i].extract_text()
            if t:
                texts.append(t)

    return "\n".join(texts)


# ==============================
# LLM CALL (SAFE)
# ==============================
def call_llm(prompt):
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0
            },
            timeout=20
        )

        return res.json()["choices"][0]["message"]["content"]

    except Exception as e:
        st.warning(f"LLM call failed: {e}")
        return ""


# ==============================
# STRUCTURE DETECTION
# ==============================
def detect_structure(sample_text, table_sample):
    prompt = f"""
Decide parsing method.

Return JSON:
{{
 "method": "table" or "llm"
}}

TEXT:
{sample_text[:1500]}

TABLE:
{str(table_sample[:3])}
"""

    content = call_llm(prompt)

    data = safe_json_loads(content)

    if not data:
        return {"method": "llm"}

    return data


# ==============================
# LLM EXTRACTION
# ==============================
def llm_extract(text):
    prompt = f"""
Extract rows into JSON list.

Columns:
Date, Particulars, Cheque_No, Withdrawals, Deposits, Balance

Return ONLY JSON list.

TEXT:
{text[:4000]}
"""

    content = call_llm(prompt)

    data = safe_json_loads(content)

    if isinstance(data, list):
        return pd.DataFrame(data)

    return pd.DataFrame()


# ==============================
# VALIDATION
# ==============================
def validate(df):
    if df.empty:
        return False

    if "Date" in df.columns:
        if df["Date"].isna().mean() > 0.5:
            return False

    return True


# ==============================
# EXCEL
# ==============================
def to_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return buf


# ==============================
# UI
# ==============================
st.title("🧠 Robust PDF → Excel")

file = st.file_uploader("Upload PDF", type=["pdf"])

if file:
    pdf_bytes = file.read()
    password = st.text_input("Password (if any)", type="password")

    if st.button("Convert"):

        # STEP 1: Extract
        table_df = extract_via_table(pdf_bytes, password)
        text = extract_text(pdf_bytes, password)

        # STEP 2: Decide
        decision = detect_structure(text, table_df.to_dict("records"))

        st.write("Decision:", decision)

        # STEP 3: Route
        if decision.get("method") == "table" and not table_df.empty:
            df = table_df

            if not validate(df):
                st.warning("Table failed validation → LLM fallback")
                df = llm_extract(text)

        else:
            df = llm_extract(text)

        # STEP 4: Output
        if df.empty:
            st.error("❌ Could not extract data")
        else:
            st.success("✅ Done")
            st.dataframe(df.head(10))

            st.download_button(
                "Download Excel",
                data=to_excel(df),
                file_name="output.xlsx"
            )