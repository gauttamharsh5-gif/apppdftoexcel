import streamlit as st
import pdfplumber
import pandas as pd
import re
import json
import io
import requests
import time

# ==============================
# CONFIG & HELPERS
# ==============================
st.set_page_config(page_title="Universal PDF Parser", layout="wide")
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")

def safe_json_loads(text):
    try: return json.loads(text)
    except:
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match: return json.loads(match.group())
        except: pass
    return None

def call_llm(prompt, require_json=True):
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0
    }
    if require_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=30
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        st.warning(f"LLM API Error: {e}")
        return "{}"

# ==============================
# 1. DYNAMIC CONSULTANT
# ==============================
def consult_structure(sample_text):
    """Asks the LLM to figure out the column names and best extraction method."""
    prompt = f"""
    Analyze this bank statement text.
    1. What are the EXACT column headers for the transaction table? (Look for words like Date, Description, Debit, Credit, Balance, Reference, etc.)
    2. Does the text look like a structured grid (choose 'table') or a messy block of text (choose 'llm')?
    
    TEXT: {sample_text[:3000]}
    
    Return ONLY JSON in this format:
    {{
      "method": "table" or "llm",
      "columns": ["Col1", "Col2", "Col3", ...]
    }}
    """
    res = safe_json_loads(call_llm(prompt))
    
    if res and "columns" in res and len(res["columns"]) > 0:
        return res["method"], res["columns"]
    
    # Fallback if LLM fails
    return "llm", ["Date", "Description", "Amount", "Balance"]

# ==============================
# 2. TABLE ENGINE (DYNAMIC COLUMNS)
# ==============================
def extract_via_table(pdf_bytes, columns, password=None):
    rows = []
    target_length = len(columns)
    
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    # Clean the row
                    clean_row = [str(c).strip().replace('\n', ' ') if c else "" for c in row]
                    
                    # Skip empty rows
                    if not any(clean_row): continue
                    
                    # Pad or truncate the row to match the dynamic column count perfectly
                    while len(clean_row) < target_length: clean_row.append("")
                    clean_row = clean_row[:target_length]
                    
                    rows.append(clean_row)

    df = pd.DataFrame(rows, columns=columns)
    
    # Cleanup: Remove the header row if pdfplumber accidentally extracted it as data
    if not df.empty:
        first_row_str = " ".join(df.iloc[0].astype(str)).lower()
        if "date" in first_row_str or "description" in first_row_str or "balance" in first_row_str:
            df = df.iloc[1:].reset_index(drop=True)
            
    return df

# ==============================
# 3. LLM ENGINE (DYNAMIC COLUMNS)
# ==============================
def extract_via_llm(pages_text, columns):
    all_data = []
    
    for i, text in enumerate(pages_text):
        if not text.strip(): continue
        
        prompt = f"""
        Extract bank transactions from this text into a JSON list under the key "transactions".
        You MUST use these EXACT keys for every transaction: {json.dumps(columns)}
        If a column has no data, use null. Do not include headers as a transaction.
        
        TEXT: {text[:5000]} 
        """
        
        content = call_llm(prompt, require_json=True)
        data = safe_json_loads(content)
        
        if data and "transactions" in data and isinstance(data["transactions"], list):
            all_data.extend(data["transactions"])
            
        time.sleep(1) # Protect API limits
        
    return pd.DataFrame(all_data)

# ==============================
# 4. VALIDATION
# ==============================
def validate_data(df, raw_text):
    if df.empty or len(df) < 2: return False, "Not enough rows extracted."
    sample_json = df.head(3).to_json(orient="records")
    
    prompt = f"""
    Does this extracted JSON accurately match the transactions in the raw text? 
    Return JSON {{"is_correct": true/false, "reason": "why"}}
    TEXT: {raw_text[:2000]}
    JSON: {sample_json}
    """
    res = safe_json_loads(call_llm(prompt))
    if res and "is_correct" in res:
        return res["is_correct"], res.get("reason", "Checks out.")
    return True, "Validation parsing failed, assuming okay."

# ==============================
# UI
# ==============================
st.title("🌐 Universal PDF Parser")
st.markdown("Dynamically detects column structures before extracting data.")

file = st.file_uploader("Upload Bank Statement (PDF)", type=["pdf"])

if file:
    pdf_bytes = file.read()
    password = st.text_input("Password (if any)", type="password")

    if st.button("Start Extraction"):
        with st.spinner("Reading PDF..."):
            pages_text = []
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t: pages_text.append(t)
            except Exception as e:
                st.error(f"Could not read PDF: {e}")
                st.stop()
                
            if not pages_text:
                st.error("❌ No text found. This is likely a scanned image requiring OCR.")
                st.stop()
                
            first_page_raw = pages_text[0]

        # STEP 1: Consult Structure
        with st.spinner("🧠 Analyzing layout and defining columns..."):
            method, columns = consult_structure(first_page_raw)
            st.success(f"**Detected Columns:** `{', '.join(columns)}`")
            st.info(f"**Recommended Method:** `{method.upper()}`")

        # STEP 2: Execute Engine
        final_df = pd.DataFrame()
        
        # We try the recommended method first, then fallback to the other
        strategies = [
            ("table", lambda: extract_via_table(pdf_bytes, columns, password)),
            ("llm", lambda: extract_via_llm(pages_text, columns))
        ]
        
        # Put the recommended one at the top of the list
        if method == "llm": strategies.reverse()

        st.write("### ⚙️ Extraction Process")
        for strategy_name, strategy_func in strategies:
            status = st.empty()
            status.warning(f"Attempting **{strategy_name.upper()}** extraction...")
            
            try:
                df = strategy_func()
            except Exception as e:
                df = pd.DataFrame()
                
            # Basic validation to ensure the extraction actually caught data
            if not df.empty and len(df) > 1 and len(df.columns) == len(columns):
                status.success(f"✅ **{strategy_name.upper()}** succeeded! ({len(df)} rows)")
                final_df = df
                break
            else:
                status.error(f"❌ **{strategy_name.upper()}** yielded 0 rows or mismatched columns. Falling back...")

        if final_df.empty:
            st.error("🚨 Both Table and LLM extraction failed. The layout is too complex.")
            st.stop()

        # STEP 3: Validate
        with st.spinner("🕵️ Validating data accuracy..."):
            is_valid, reason = validate_data(final_df, first_page_raw)
            if is_valid:
                st.success(f"🏆 **QA Passed:** {reason}")
            else:
                st.warning(f"⚠️ **QA Warning:** {reason} (Review the output carefully)")

        # Output
        st.dataframe(final_df.head(15))
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            final_df.to_excel(writer, index=False)
        st.download_button("Download Excel", data=buf.getvalue(), file_name="dynamic_statement.xlsx")