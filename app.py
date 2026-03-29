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
                "temperature": 0,
                "response_format": {"type": "json_object"}
            },
            timeout=20
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return "{}"

# ==============================
# STEP 1: CONSULT THE LLM (NOW WITH FEEDBACK)
# ==============================
def consult_llm_for_strategy(first_page_text, feedback=""):
    prompt = f"""
    Analyze this bank statement text and determine the Python Regular Expression needed to extract the data line-by-line.
    
    RAW TEXT:
    {first_page_text[:3000]}
    """
    
    # If a previous attempt failed, inject the feedback here to force correction
    if feedback:
        prompt += f"""
        
    ⚠️ PREVIOUS ATTEMPT FAILED:
    {feedback}
    You MUST look closely at the RAW TEXT above and provide DIFFERENT, more accurate regex patterns. 
    Ensure you account for the exact spacing and date formats present in the text.
    """

    prompt += """
    Return ONLY a JSON object with these exact keys:
    1. "date_regex": A Python regex string to match the date at the start of a transaction line. (Use double backslashes, e.g., "^(\\\\d{2}[-/\\\\s]\\\\d{2}[-/\\\\s]\\\\d{2,4})").
    2. "amount_regex": A Python regex string to extract currency amounts.
    3. "confidence": A number from 1 to 10.
    """
    
    response = call_llm(prompt)
    strategy = safe_json_loads(response)
    
    if strategy and "date_regex" in strategy and "amount_regex" in strategy:
        return strategy
    return None

# ==============================
# STEP 2: EXECUTE
# ==============================
def execute_extraction(pages_text, strategy):
    transactions = []
    
    try:
        date_pattern = re.compile(strategy["date_regex"])
        amount_pattern = re.compile(strategy["amount_regex"], re.IGNORECASE)
    except Exception:
        # If the LLM wrote invalid regex syntax, we fail gracefully
        return pd.DataFrame()

    for text in pages_text:
        for line in text.split("\n"):
            line = line.strip()
            date_match = date_pattern.search(line)
            
            if date_match:
                if date_match.start() > 15: 
                    continue 

                date_str = date_match.group(1) if date_match.groups() else date_match.group(0)
                rest_of_line = line[date_match.end():].strip()
                amounts = amount_pattern.findall(rest_of_line)
                description = amount_pattern.sub("", rest_of_line).strip()
                
                withdrawal, deposit, balance = None, None, None
                if len(amounts) >= 3:
                    withdrawal = clean_amount(amounts[-3])
                    deposit = clean_amount(amounts[-2])
                    balance = clean_amount(amounts[-1])
                elif len(amounts) == 2:
                    withdrawal = clean_amount(amounts[-2])
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
# STEP 3: VALIDATE
# ==============================
def validate_with_llm(first_page_text, df_sample):
    sample_json = df_sample.to_json(orient="records")
    prompt = f"""
    You are a QA Agent. Does the extracted data accurately reflect the raw text from the first page?
    RAW TEXT: {first_page_text[:3000]}
    EXTRACTED DATA: {sample_json}
    Return ONLY a JSON object: {{"is_correct": true/false, "reason": "why"}}
    """
    response = call_llm(prompt)
    data = safe_json_loads(response)
    if data and "is_correct" in data:
        return data["is_correct"], data.get("reason", "No reason provided.")
    return False, "Failed to parse LLM validation."

def to_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return buf

# ==============================
# UI
# ==============================
st.title("🤖 Agentic PDF Parser (With Self-Correction)")

file = st.file_uploader("Upload Bank Statement (PDF)", type=["pdf"])

if file:
    pdf_bytes = file.read()
    password = st.text_input("Password (if any)", type="password")

    if st.button("Start Agentic Extraction"):
        pages_text = extract_pages_text(pdf_bytes, password)
        if not pages_text:
            st.error("❌ No text found. Might be a scanned image.")
            st.stop()
            
        first_page_raw = pages_text[0]
        
        # --- THE SELF-CORRECTION LOOP ---
        max_retries = 5
        feedback = ""
        df = pd.DataFrame()
        
        st.write("### 🔄 Extraction Loop")
        status_box = st.empty()
        
        for attempt in range(max_retries):
            status_box.info(f"Attempt {attempt + 1}/{max_retries}: Consulting LLM...")
            
            # Consult with feedback (feedback is empty on attempt 1)
            strategy = consult_llm_for_strategy(first_page_raw, feedback)
            
            if not strategy:
                feedback = "You failed to return a valid JSON object. Try again and return ONLY JSON."
                continue
                
            status_box.info(f"Attempt {attempt + 1}: Testing Regex -> Date: `{strategy.get('date_regex')}`")
            
            # Execute
            df = execute_extraction(pages_text, strategy)
            
            if not df.empty:
                status_box.success(f"✅ Success on Attempt {attempt + 1}! Extracted {len(df)} rows.")
                break # Break out of the loop! We got data!
            else:
                # Tell the LLM why it failed for the next loop
                feedback = f"The regex patterns you provided: date_regex '{strategy.get('date_regex')}' and amount_regex '{strategy.get('amount_regex')}' resulted in 0 rows extracted. The regex did not match the actual text. Please write a completely different regex pattern."
                st.warning(f"Attempt {attempt + 1} yielded 0 rows. Requesting new strategy...")

        # --------------------------------
        
        if df.empty:
            st.error("❌ LLM failed to write a working regex after 5 attempts. The layout might be too complex for simple Regex extraction.")
            st.stop()

        # STEP 3: Validate
        with st.spinner("Validating results with LLM..."):
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