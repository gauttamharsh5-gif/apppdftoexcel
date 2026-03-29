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
st.set_page_config(page_title="Agentic PDF Parser", layout="centered")
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")

# ==============================
# HELPERS
# ==============================
def safe_json_loads(text):
    try:
        return json.loads(text)
    except:
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match: return json.loads(match.group())
        except:
            pass
    return None

def clean_amount(val):
    if not val: return None
    val = str(val).replace(",", "").strip()
    val = re.sub(r"(DR|CR|Dr|Cr|dr|cr)$", "", val, flags=re.IGNORECASE).strip()
    try:
        return float(val)
    except:
        return None

def extract_pages_text(pdf_bytes, password=None):
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except Exception as e:
        st.error(f"Failed to open PDF: {e}")
        return []
    
    pages_text = []
    with pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t: pages_text.append(t)
    return pages_text

# ==============================
# LLM CALLER
# ==============================
def call_llm(prompt):
    """Generic function to call Groq API requiring JSON output."""
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
                "temperature": 0,
                "response_format": {"type": "json_object"}
            },
            timeout=20
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        st.warning(f"LLM API Error: {e}")
        return "{}"

# ==============================
# STEP 1: CONSULT THE LLM
# ==============================
def consult_llm_for_strategy(first_page_text):
    """Ask the LLM to figure out the Regex patterns based on the text."""
    prompt = f"""
    Analyze this bank statement text and determine the Python Regular Expression needed to extract the data line-by-line.
    
    RAW TEXT:
    {first_page_text[:3000]}
    
    Return ONLY a JSON object with these exact keys:
    1. "date_regex": A Python regex string to match the date at the start of a transaction line. (Use double backslashes, e.g., "^(\\\\d{{2}}[-/\\\\s]\\\\d{{2}}[-/\\\\s]\\\\d{{2,4}})").
    2. "amount_regex": A Python regex string to extract currency amounts like 1,234.56 or 500.00 Cr. (e.g., "([\\\\d,]+\\\\.\\\\d{{2}}\\\\s*(?:Cr|Dr)?)").
    3. "confidence": A number from 1 to 10 on how confident you are in these rules.
    """
    
    response = call_llm(prompt)
    strategy = safe_json_loads(response)
    
    if strategy and "date_regex" in strategy and "amount_regex" in strategy:
        return strategy
    return None

# ==============================
# STEP 2: EXECUTE (PYTHON)
# ==============================
def execute_extraction(pages_text, strategy):
    """Use the LLM's strategy to parse the whole document. Includes a safety fallback."""
    transactions = []
    
    # Try compiling LLM's regex, fallback to defaults if it hallucinates bad regex
    try:
        date_pattern = re.compile(strategy["date_regex"])
        amount_pattern = re.compile(strategy["amount_regex"], re.IGNORECASE)
        st.info("🧠 Using LLM's custom extraction rules.")
    except Exception as e:
        st.warning("⚠️ LLM generated invalid rules. Falling back to default robust patterns.")
        date_pattern = re.compile(r"^(\d{1,4}[-/.\s][a-zA-Z0-9]{2,3}[-/.\s]\d{2,4})")
        amount_pattern = re.compile(r"([\d,]+\.\d{2})\s*(?:Cr|Dr|CR|DR)?\s*", re.IGNORECASE)

    for text in pages_text:
        for line in text.split("\n"):
            line = line.strip()
            date_match = date_pattern.search(line)
            
            # If the line contains a date, assume it's a transaction
            if date_match:
                # Some regex patterns match mid-string, we want the start of the line or close to it
                if date_match.start() > 10: 
                    continue 

                date_str = date_match.group(1) if date_match.groups() else date_match.group(0)
                
                # Remove the date from the line to parse the rest
                rest_of_line = line[date_match.end():].strip()
                
                # Extract all amounts
                amounts = amount_pattern.findall(rest_of_line)
                
                # The description is whatever is left after removing amounts
                description = amount_pattern.sub("", rest_of_line).strip()
                
                # Assign columns logically based on how many amounts were found
                withdrawal, deposit, balance = None, None, None
                if len(amounts) >= 3:
                    withdrawal = clean_amount(amounts[-3])
                    deposit = clean_amount(amounts[-2])
                    balance = clean_amount(amounts[-1])
                elif len(amounts) == 2:
                    withdrawal = clean_amount(amounts[-2]) # Guessing it's a withdrawal
                    balance = clean_amount(amounts[-1])
                elif len(amounts) == 1:
                    balance = clean_amount(amounts[-1])
                
                transactions.append({
                    "Date": date_str.strip(),
                    "Particulars": description,
                    "Withdrawals": withdrawal,
                    "Deposits": deposit,
                    "Balance": balance
                })

    return pd.DataFrame(transactions)

# ==============================
# STEP 3: VALIDATE WITH LLM
# ==============================
def validate_with_llm(first_page_text, df_sample):
    """Ask LLM to check if the executed data matches the raw text."""
    sample_json = df_sample.to_json(orient="records")
    
    prompt = f"""
    You are a QA Agent. I extracted this data using regex. 
    Does the extracted data accurately reflect the raw text from the first page?
    
    RAW TEXT:
    {first_page_text[:3000]}
    
    EXTRACTED DATA:
    {sample_json}
    
    Return ONLY a JSON object:
    {{
      "is_correct": true or false,
      "reason": "1 sentence explaining why."
    }}
    """
    
    response = call_llm(prompt)
    data = safe_json_loads(response)
    
    if data and "is_correct" in data:
        return data["is_correct"], data.get("reason", "No reason provided.")
    return False, "Failed to parse LLM validation."

# ==============================
# EXPORT
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
st.title("🤖 Agentic PDF Parser")
st.markdown("**Workflow:** Consult LLM ➡️ Execute Python ➡️ Validate with LLM")

file = st.file_uploader("Upload Bank Statement (PDF)", type=["pdf"])

if file:
    pdf_bytes = file.read()
    password = st.text_input("Password (if any)", type="password")

    if st.button("Start Agentic Extraction"):
        
        # Get text
        pages_text = extract_pages_text(pdf_bytes, password)
        if not pages_text:
            st.error("❌ No text found. Might be a scanned image.")
            st.stop()
            
        first_page_raw = pages_text[0]

        # STEP 1: Consult
        with st.spinner("Step 1: Consulting LLM for extraction rules..."):
            strategy = consult_llm_for_strategy(first_page_raw)
            
            if not strategy:
                st.warning("⚠️ LLM failed to provide a strategy. Using default rules.")
                strategy = {
                    "date_regex": r"^(\d{1,4}[-/.\s][a-zA-Z0-9]{2,3}[-/.\s]\d{2,4})",
                    "amount_regex": r"([\d,]+\.\d{2})\s*(?:Cr|Dr|CR|DR)?\s*"
                }
            else:
                st.success(f"✅ LLM Strategy acquired! (Confidence: {strategy.get('confidence', 'N/A')}/10)")

        # STEP 2: Execute
        with st.spinner("Step 2: Executing bulk data extraction..."):
            df = execute_extraction(pages_text, strategy)
            
            if df.empty:
                st.error("❌ Extraction yielded 0 rows. The layout may be too complex.")
                st.stop()
                
            st.success(f"✅ Extracted {len(df)} transactions.")

        # STEP 3: Validate
        with st.spinner("Step 3: Validating results with LLM..."):
            is_valid, reason = validate_with_llm(first_page_raw, df.head(5))
            
            if is_valid:
                st.success(f"🏆 **QA Passed:** {reason}")
            else:
                st.warning(f"🚨 **QA Failed:** {reason}")

        # Final Output
        st.dataframe(df.head(15))
        st.download_button(
            label="Download Excel",
            data=to_excel(df),
            file_name="agentic_statement.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )