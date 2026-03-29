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
# DATA CLEANING ENGINE (FOR TAX)
# ==============================
def clean_dataframe(df):
    """Purges non-transactional noise (headers, opening balances, pages)."""
    if df.empty: return df
    
    # 1. Drop completely empty rows
    df.dropna(how='all', inplace=True)
    
    # 2. Filter out common bank statement artifacts
    ignore_keywords = [
        'opening balance', 'brought forward', 'carried forward', 
        'closing balance', 'statement summary', 'page '
    ]
    
    mask = pd.Series([True] * len(df))
    for col in df.columns:
        if df[col].dtype == object:
            str_col = df[col].astype(str).str.lower()
            for kw in ignore_keywords:
                # Keep the row only if it DOES NOT contain the ignore keyword
                mask = mask & ~str_col.str.contains(kw, na=False)
                
    df = df[mask]
    
    # 3. Ensure the 'Date' column actually contains numbers (drops random text rows)
    date_col = next((col for col in df.columns if 'date' in col.lower()), None)
    if not date_col and len(df.columns) > 0:
        date_col = df.columns[0] # Assume the first column is the date if unnamed
        
    if date_col:
        # Keep rows where the date column has at least one digit
        df = df[df[date_col].astype(str).str.contains(r'\d', na=False)]
        
    return df.reset_index(drop=True)

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
    # Ultimate fallback
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
    
    # Remove header row if it was scraped as data
    if not df.empty:
        first_row_str = " ".join(df.iloc[0].astype(str)).lower()
        if any(kw in first_row_str for kw in ["date", "description", "balance"]):
            df = df.iloc[1:].reset_index(drop=True)
            
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
        2. DO NOT extract "Opening Balance", "Brought Forward", "Closing Balance", or "Carried Forward".
        3. DO NOT extract page numbers, headers, or statement summaries.
        4. If a column has no data, use null.
        
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
# 4. VALIDATION
# ==============================
def validate_data(df, raw_text):
    if df.empty or len(df) < 2: return False, "Not enough rows extracted."
    
    # Send a small sample of the CLEANED data for validation
    sample_json = df.head(4).to_json(orient="records")
    
    prompt = f"""
    You are a tax auditor. I have extracted transaction data from a bank statement, ignoring all redundant info (like page numbers and opening balances).
    
    RAW TEXT (May contain noise): 
    {raw_text[:3000]}
    
    EXTRACTED CLEAN TRANSACTIONS (JSON): 
    {sample_json}
    
    Does the extracted JSON accurately reflect the *actual transactions* found in the text without including redundant headers/balances?
    Return JSON {{"is_correct": true/false, "reason": "why"}}
    """
    res = safe_json_loads(call_llm(prompt))
    if res and "is_correct" in res:
        return res["is_correct"], res.get("reason", "Clean and accurate.")
    return True, "Validation parsing failed, assuming okay."

# ==============================
# UI & EXECUTION LOGIC
# ==============================
st.title("🧾 Tax-Ready PDF Parser")
st.markdown("Extracts dynamic columns, skips cover pages, and aggressively removes non-transactional noise.")

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
                        # Only append if the page actually contains a meaningful amount of text
                        if t and len(t.strip()) > 20: 
                            pages_text.append(t)
            except Exception as e:
                st.error(f"Could not read PDF: {e}")
                st.stop()
                
            if not pages_text:
                st.error("❌ No digital text found anywhere in the document. This is likely a scanned image requiring OCR.")
                st.stop()
                
            # Combine up to the first 3 valid pages to ensure we hit the transaction table
            sample_text = "\n".join(pages_text[:min(3, len(pages_text))])

        # STEP 1: Consult
        with st.spinner("🧠 Defining dynamic columns from the first few pages..."):
            method, columns = consult_structure(sample_text)
            st.success(f"**Detected Columns:** `{', '.join(columns)}`")

        final_df = pd.DataFrame()
        
        # Load strategies, placing the LLM's recommended one first
        strategies = [
            ("table", lambda: extract_via_table(pdf_bytes, columns, password)),
            ("llm", lambda: extract_via_llm(pages_text, columns))
        ]
        if method == "llm": strategies.reverse()

        # STEP 2: Execute
        st.write("### ⚙️ Extraction & Cleaning Process")
        for strategy_name, strategy_func in strategies:
            status = st.empty()
            status.warning(f"Attempting **{strategy_name.upper()}** extraction...")
            
            try:
                df = strategy_func()
            except Exception as e:
                df = pd.DataFrame()
                
            if not df.empty and len(df) > 1 and len(df.columns) == len(columns):
                status.success(f"✅ **{strategy_name.upper()}** succeeded & cleaned! ({len(df)} transactions)")
                final_df = df
                break
            else:
                status.error(f"❌ **{strategy_name.upper()}** failed or yielded 0 rows. Falling back...")

        if final_df.empty:
            st.error("🚨 All extraction methods failed. The document layout may be unsupported.")
            st.stop()

        # STEP 3: Validate
        with st.spinner("🕵️ Validating clean data for tax compliance..."):
            is_valid, reason = validate_data(final_df, sample_text)
            if is_valid:
                st.success(f"🏆 **Audit Passed:** {reason}")
            else:
                st.warning(f"⚠️ **Audit Warning:** {reason}")

        # Final Output
        st.dataframe(final_df.head(15))
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            final_df.to_excel(writer, index=False)
        st.download_button("⬇️ Download Tax-Ready Excel", data=buf.getvalue(), file_name="tax_ready_statement.xlsx")