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
st.set_page_config(page_title="Ultimate PDF Parser", layout="wide")
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")

def safe_json_loads(text):
    try: return json.loads(text)
    except:
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match: return json.loads(match.group())
        except: pass
    return None

def is_date(val):
    if not val: return False
    return bool(re.search(r"(\d{1,4}[-/.\s][a-zA-Z0-9]{2,3}[-/.\s]\d{2,4}|\d{2}[-/.\s]\d{2}[-/.\s]\d{2,4})", str(val).strip()))

def clean_amount(val):
    if not val: return None
    val = str(val).replace(",", "").strip()
    val = re.sub(r"(DR|CR|Dr|Cr|dr|cr)$", "", val, flags=re.IGNORECASE).strip()
    try: return float(val)
    except: return None

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
        return "{}"

# ==============================
# EXTRACTION ENGINE 1: TABLE
# ==============================
def extract_via_table(pdf_bytes, password=None):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if row and any(is_date(c) for c in row[:3]):
                        clean_row = [str(c).strip().replace('\n', ' ') if c else "" for c in row]
                        while len(clean_row) < 6: clean_row.append("")
                        rows.append({
                            "Date": clean_row[0] if is_date(clean_row[0]) else clean_row[1],
                            "Particulars": clean_row[1] if not is_date(clean_row[1]) else clean_row[2],
                            "Withdrawals": clean_amount(clean_row[3]),
                            "Deposits": clean_amount(clean_row[4]),
                            "Balance": clean_amount(clean_row[5])
                        })
    return pd.DataFrame(rows)

# ==============================
# EXTRACTION ENGINE 2: REGEX
# ==============================
def extract_via_regex(pages_text):
    transactions = []
    date_pattern = re.compile(r"^(\d{1,4}[-/.\s][a-zA-Z0-9]{2,3}[-/.\s]\d{2,4}|\d{2}[-/.\s]\d{2}[-/.\s]\d{2,4})")
    amount_pattern = re.compile(r"([\d,]+\.\d{2})\s*(?:Cr|Dr|CR|DR)?\s*", re.IGNORECASE)

    for text in pages_text:
        for line in text.split("\n"):
            line = line.strip()
            date_match = date_pattern.search(line)
            if date_match and date_match.start() < 10: 
                date_str = date_match.group(1)
                rest_of_line = line[date_match.end():].strip()
                amounts = amount_pattern.findall(rest_of_line)
                description = amount_pattern.sub("", rest_of_line).strip()
                
                withdrawal, deposit, balance = None, None, None
                if len(amounts) >= 3:
                    withdrawal, deposit, balance = clean_amount(amounts[-3]), clean_amount(amounts[-2]), clean_amount(amounts[-1])
                elif len(amounts) == 2:
                    withdrawal, balance = clean_amount(amounts[-2]), clean_amount(amounts[-1])
                elif len(amounts) == 1:
                    balance = clean_amount(amounts[-1])
                
                transactions.append({"Date": date_str, "Particulars": description, "Withdrawals": withdrawal, "Deposits": deposit, "Balance": balance})
    return pd.DataFrame(transactions)

# ==============================
# EXTRACTION ENGINE 3: LLM CHUNKS
# ==============================
def extract_via_llm_chunks(pages_text):
    all_data = []
    for i, text in enumerate(pages_text):
        if not text.strip(): continue
        prompt = f"""
        Extract bank transactions from this text into a JSON list under the key "transactions".
        Columns: Date, Particulars, Withdrawals, Deposits, Balance. Use null if empty.
        TEXT: {text[:5000]} 
        """
        content = call_llm(prompt, require_json=True)
        data = safe_json_loads(content)
        if data and "transactions" in data and isinstance(data["transactions"], list):
            all_data.extend(data["transactions"])
        time.sleep(1) # Protect API limits
    return pd.DataFrame(all_data)

# ==============================
# ROUTER & VALIDATOR
# ==============================
def consult_router(sample_text):
    prompt = f"""
    Look at this bank statement text. Does it look like a highly structured grid (choose 'table'), a borderless list of text (choose 'regex'), or completely chaotic (choose 'llm')?
    TEXT: {sample_text[:2000]}
    Return ONLY JSON: {{"method": "table", "regex", or "llm"}}
    """
    res = safe_json_loads(call_llm(prompt))
    return res.get("method", "table") if res else "table"

def validate_data(df, raw_text):
    if df.empty or len(df) < 2: return False, "Not enough rows extracted."
    sample_json = df.head(4).to_json(orient="records")
    prompt = f"""
    Does this extracted JSON match the raw text? Return JSON {{"is_correct": true/false, "reason": "why"}}
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
st.title("🛡️ Ultimate Multi-Strategy Extractor")

file = st.file_uploader("Upload Bank Statement (PDF)", type=["pdf"])

if file:
    pdf_bytes = file.read()
    password = st.text_input("Password (if any)", type="password")

    if st.button("Start Intelligent Extraction"):
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

        # STEP 1: Consult
        with st.spinner("🧠 Consulting LLM for the best strategy..."):
            recommended_method = consult_router(first_page_raw)
            st.info(f"**LLM Router suggests starting with:** `{recommended_method.upper()}` strategy.")

        # Define our fallback chain based on the recommendation
        strategies = {
            "table": lambda: extract_via_table(pdf_bytes, password),
            "regex": lambda: extract_via_regex(pages_text),
            "llm": lambda: extract_via_llm_chunks(pages_text)
        }
        
        # Order the execution list: Put recommended first, then remaining, ending with expensive LLM
        execution_order = [recommended_method]
        for m in ["table", "regex", "llm"]:
            if m not in execution_order: execution_order.append(m)

        # STEP 2: Execute with Fallbacks
        final_df = pd.DataFrame()
        successful_method = ""
        
        st.write("### ⚙️ Extraction Engine")
        for method in execution_order:
            status = st.empty()
            status.warning(f"Attempting **{method.upper()}** extraction...")
            
            try:
                df = strategies[method]()
            except Exception as e:
                df = pd.DataFrame()
                
            if not df.empty and len(df) > 1:
                status.success(f"✅ **{method.upper()}** extraction succeeded! ({len(df)} rows)")
                final_df = df
                successful_method = method
                break # We got data! Break out of the fallback loop.
            else:
                status.error(f"❌ **{method.upper()}** extraction yielded 0 rows. Falling back to next method...")

        if final_df.empty:
            st.error("🚨 All extraction methods failed. The layout is too complex.")
            st.stop()

        # STEP 3: Validate
        with st.spinner("🕵️ Validating extracted data with LLM QA..."):
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
        st.download_button("Download Excel", data=buf.getvalue(), file_name="extracted_statement.xlsx")