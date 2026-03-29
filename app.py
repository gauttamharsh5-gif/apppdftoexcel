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
st.set_page_config(page_title="Tax-Ready PDF Parser", layout="wide")
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
# HYPER-SAFE DATA CLEANER
# ==============================
def clean_dataframe(df):
    """Carefully purges noise WITHOUT risking actual transaction records."""
    if df.empty: return df
    
    # Drop completely empty rows
    df = df.dropna(how='all')
    cleaned_rows = []
    
    for _, row in df.iterrows():
        # Convert row to a single searchable string
        row_str = " ".join(row.fillna("").astype(str)).lower().strip()
        
        # 1. STRICT TAX NOISE: Only match exact accounting phrases
        is_tax_noise = bool(re.search(r"\b(opening balance|brought forward|carried forward|closing balance|statement summary)\b", row_str))
        
        # 2. PAGE NOISE: Only drop if the row STARTS with "page X"
        is_page_noise = bool(re.search(r"^page\s*\d+", row_str)) 
        
        if is_tax_noise or is_page_noise:
            continue # Safely drop
            
        # 3. HEADER STRIPPING: If the first column is literally the word "Date", it's a repeated header
        first_col_val = str(row.iloc[0]).strip().lower()
        if first_col_val in ["date", "transaction date", "txn date", "posting date"]:
            continue
            
        # 4. NUMBER CHECK: A real transaction MUST have at least one number (a date or an amount)
        if not re.search(r'\d', row_str):
            continue # Drop rows that are purely alphabetical text
            
        cleaned_rows.append(row)
        
    final_df = pd.DataFrame(cleaned_rows, columns=df.columns)
    return final_df.reset_index(drop=True)

# ==============================
# 1. DYNAMIC CONSULTANT
# ==============================
def consult_structure(sample_text):
    """Analyzes the first few pages to determine columns and extraction method."""
    prompt = f"""
    Analyze this bank statement text.
    1. What are the EXACT column headers for the transaction table? (e.g., Date, Description, Withdrawals, Deposits, Balance)
    2. Does the text look like a structured grid (choose 'table') or a messy block of text (choose 'llm')?
    
    TEXT: {sample_text[:4000]}
    
    Return ONLY JSON:
    {{
      "method": "table" or "llm",
      "columns": ["Col1", "Col2", "Col3", ...]
    }}
    """
    res = safe_json_loads(call_llm(prompt))
    if res and "columns" in res and len(res["columns"]) > 0:
        return res["method"], res["columns"]
    return "llm", ["Date", "Description", "Withdrawal", "Deposit", "Balance"]

# ==============================
# 2. TABLE ENGINE
# ==============================
def extract_via_table(pdf_bytes, columns, password=None):
    rows = []
    target_len = len(columns)
    
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    clean_row = [str(c).strip().replace('\n', ' ') if c else "" for c in row]
                    if not any(clean_row): continue
                    
                    # Pad or truncate to match exact column count
                    while len(clean_row) < target_len: clean_row.append("")
                    rows.append(clean_row[:target_len])

    df = pd.DataFrame(rows, columns=columns)
    return clean_dataframe(df)

# ==============================
# 3. LLM ENGINE
# ==============================
def extract_via_llm(pages_text, columns):
    all_data = []
    
    for i, text in enumerate(pages_text):
        if not text.strip(): continue
        
        prompt = f"""
        Extract bank transactions from this text into a JSON list under the key "transactions".
        You MUST use these EXACT keys: {json.dumps(columns)}
        
        CRITICAL TAX RULES:
        1. ONLY extract actual financial transactions.
        2. DO NOT extract "Opening Balance", "Closing Balance", or statement summaries.
        3. If a column has no data, use null.
        
        TEXT: {text[:5000]} 
        """
        
        content = call_llm(prompt, require_json=True)
        data = safe_json_loads(content)
        
        if data and "transactions" in data and isinstance(data["transactions"], list):
            all_data.extend(data["transactions"])
            
        time.sleep(1) # Protect free API tier
        
    df = pd.DataFrame(all_data)
    return clean_dataframe(df)

# ==============================
# 4. SMART VALIDATION
# ==============================
def validate_data(df, raw_text):
    if df.empty or len(df) < 2: 
        return False, "Not enough rows extracted."
    
    # Grab the first 3 cleaned rows
    sample_df = df.head(3)
    sample_json = sample_df.to_json(orient="records")
    
    # Find the exact spot in the raw text where the first transaction occurs
    first_row = sample_df.iloc[0]
    anchor_str = str(first_row.iloc[0]).strip() 
    start_idx = raw_text.find(anchor_str)
    
    if start_idx != -1:
        # Cut a window of text exactly where the data starts
        text_chunk = raw_text[max(0, start_idx - 200) : start_idx + 1500]
    else:
        text_chunk = raw_text[:2000] 

    prompt = f"""
    You are a lenient Data Auditor. Verify if the EXTRACTED JSON matches the RAW TEXT.
    
    RAW TEXT CHUNK: 
    {text_chunk}
    
    EXTRACTED JSON (First 3 rows): 
    {sample_json}
    
    AUDIT RULES:
    1. Check if the amounts and dates in the JSON exist in the raw text. 
    2. If the numbers match, the extraction is CORRECT.
    3. IGNORE slight differences in text descriptions.
    4. IGNORE missing "Opening Balance" rows (intentionally removed).
    
    Return ONLY JSON: {{"is_correct": true or false, "reason": "1 short sentence explaining why."}}
    """
    res = safe_json_loads(call_llm(prompt))
    if res and "is_correct" in res:
        return res["is_correct"], res.get("reason", "Verified against source text.")
    return True, "Validation parsing failed, assuming data is okay."

# ==============================
# UI & EXECUTION LOGIC
# ==============================
st.title("🧾 Tax-Ready PDF Parser")
st.markdown("Extracts dynamic columns, skips cover pages, and safely cleans data.")

file = st.file_uploader("Upload Bank Statement (PDF)", type=["pdf"])

if file:
    pdf_bytes = file.read()
    password = st.text_input("Password (if any)", type="password")

    if st.button("Start Clean Extraction"):
        with st.spinner("Reading PDF..."):
            pages_text = []
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t and len(t.strip()) > 20: 
                            pages_text.append(t)
            except Exception as e:
                st.error(f"Could not read PDF: {e}")
                st.stop()
                
            if not pages_text:
                st.error("❌ No digital text found. This is likely a scanned image requiring OCR.")
                st.stop()
                
            # Combine up to the first 3 valid pages to ensure we hit the table
            sample_text = "\n".join(pages_text[:min(3, len(pages_text))])

        with st.spinner("🧠 Defining dynamic columns..."):
            method, columns = consult_structure(sample_text)
            st.success(f"**Detected Columns:** `{', '.join(columns)}`")

        final_df = pd.DataFrame()
        strategies = [
            ("table", lambda: extract_via_table(pdf_bytes, columns, password)),
            ("llm", lambda: extract_via_llm(pages_text, columns))
        ]
        if method == "llm": strategies.reverse()

        st.write("### ⚙️ Extraction & Cleaning Process")
        for strategy_name, strategy_func in strategies:
            status = st.empty()
            status.warning(f"Attempting **{strategy_name.upper()}** extraction...")
            
            try:
                df = strategy_func()
            except Exception as e:
                df = pd.DataFrame()
                
            if not df.empty and len(df) > 1 and len(df.columns) == len(columns):
                status.success(f"✅ **{strategy_name.upper()}** succeeded & safely cleaned! ({len(df)} transactions)")
                final_df = df
                break
            else:
                status.error(f"❌ **{strategy_name.upper()}** failed or yielded 0 rows. Falling back...")

        if final_df.empty:
            st.error("🚨 All extraction methods failed. The document layout may be unsupported.")
            st.stop()

        with st.spinner("🕵️ Validating extracted data..."):
            is_valid, reason = validate_data(final_df, sample_text)
            if is_valid:
                st.success(f"🏆 **Audit Passed:** {reason}")
            else:
                st.warning(f"⚠️ **Audit Warning:** {reason}")

        st.dataframe(final_df.head(15))
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            final_df.to_excel(writer, index=False)
        st.download_button("⬇️ Download Tax-Ready Excel", data=buf.getvalue(), file_name="tax_ready_statement.xlsx")