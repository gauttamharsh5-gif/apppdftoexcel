import streamlit as st
import pdfplumber
import pandas as pd
import re
import json
import io
import requests
import time

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
        if match: return json.loads(match.group())
    except:
        pass
    try:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match: return json.loads(match.group())
    except:
        pass
    return None

def is_date(val):
    """Broadened regex to catch multiple date formats (DD/MM/YYYY, DD-MMM-YY, YYYY-MM-DD)"""
    if not val: return False
    date_pattern = r"(\d{1,4}[-/.\s][a-zA-Z0-9]{2,3}[-/.\s]\d{2,4})"
    return bool(re.search(date_pattern, str(val).strip()))

def clean_amount(val):
    if not val: return None
    val = str(val).replace(",", "").strip()
    val = re.sub(r"(DR|CR|Dr|Cr|dr|cr)$", "", val, flags=re.IGNORECASE).strip()
    try:
        return float(val)
    except:
        return None

def open_pdf(pdf_bytes, password=None):
    try:
        return pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except Exception as e:
        st.error(f"Failed to open PDF: {e}")
        return None

# ==============================
# TABLE EXTRACTION
# ==============================
def extract_via_table(pdf_bytes, password=None):
    rows = []
    pdf = open_pdf(pdf_bytes, password)
    if not pdf: return pd.DataFrame()

    with pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    # Look for a date ANYWHERE in the first 3 columns to identify a valid transaction row
                    if row and any(is_date(c) for c in row[:3]):
                        clean_row = [str(c).strip().replace('\n', ' ') if c else "" for c in row]
                        
                        # Pad row to avoid index errors
                        while len(clean_row) < 6: clean_row.append("")
                        
                        # Note: This is still a heuristic. For truly random column orders, LLM is better.
                        rows.append({
                            "Date": clean_row[0] if is_date(clean_row[0]) else clean_row[1],
                            "Particulars": clean_row[1] if not is_date(clean_row[1]) else clean_row[2],
                            "Cheque_No": clean_row[2],
                            "Withdrawals": clean_amount(clean_row[3]),
                            "Deposits": clean_amount(clean_row[4]),
                            "Balance": clean_amount(clean_row[5])
                        })
    return pd.DataFrame(rows)

# ==============================
# TEXT EXTRACTION & CHUNKING
# ==============================
def extract_pages_text(pdf_bytes, password=None):
    """Returns a list of text strings, one for each page."""
    pdf = open_pdf(pdf_bytes, password)
    if not pdf: return []
    
    pages_text = []
    with pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t: pages_text.append(t)
    return pages_text

# ==============================
# LLM CALL
# ==============================
def call_llm(prompt, require_json=False):
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0
    }
    
    # Force Groq to return valid JSON
    if require_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        st.warning(f"LLM call failed: {e}")
        return "{}" if require_json else ""

# ==============================
# LLM EXTRACTION (PAGE BY PAGE)
# ==============================
def llm_extract_all_pages(pages_text):
    """Processes text page by page to avoid token truncation."""
    all_data = []
    progress_bar = st.progress(0)
    
    for i, page_text in enumerate(pages_text):
        if not page_text.strip(): continue
        
        prompt = f"""
        Extract bank statement transaction rows from the text below into a JSON list under the key "transactions".
        Columns required: Date, Particulars, Cheque_No, Withdrawals, Deposits, Balance.
        If a column is empty or missing, use null.
        Ensure you only return a JSON object with the "transactions" key.
        
        TEXT:
        {page_text[:6000]} 
        """
        
        content = call_llm(prompt, require_json=True)
        data = safe_json_loads(content)
        
        if data and "transactions" in data and isinstance(data["transactions"], list):
            all_data.extend(data["transactions"])
            
        # Update progress and respect rate limits
        progress_bar.progress((i + 1) / len(pages_text))
        time.sleep(1) # Prevent spamming the free API tier
        
    return pd.DataFrame(all_data) if all_data else pd.DataFrame()

# ==============================
# VALIDATION & EXCEL
# ==============================
def validate(df):
    if df is None or df.empty: return False
    if "Date" in df.columns and df["Date"].isna().mean() > 0.5:
        return False
    return True

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

file = st.file_uploader("Upload Bank Statement (PDF)", type=["pdf"])

if file:
    pdf_bytes = file.read()
    password = st.text_input("Password (if any)", type="password")

    if st.button("Convert to Excel"):
        with st.spinner("Extracting..."):
            
            # STEP 1: Attempt Table Extraction
            table_df = extract_via_table(pdf_bytes, password)
            
            # STEP 2: Validate Table Extraction
            if validate(table_df):
                st.success("✅ Standard Table Extracted Successfully")
                final_df = table_df
            else:
                st.warning("⚠️ Complex layout detected. Engaging LLM extraction...")
                # Fallback to LLM, processing page by page
                pages_text = extract_pages_text(pdf_bytes, password)
                
                if not pages_text:
                    st.error("❌ No text found. This might be a scanned image (requires OCR).")
                    st.stop()
                    
                final_df = llm_extract_all_pages(pages_text)

            # STEP 3: Output
            if not validate(final_df):
                st.error("❌ Could not reliably extract transaction data.")
            else:
                st.success("✅ Conversion Complete")
                st.dataframe(final_df.head(15))

                st.download_button(
                    label="Download Excel",
                    data=to_excel(final_df),
                    file_name="bank_statement.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )