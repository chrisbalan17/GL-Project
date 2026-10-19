import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import sqlparse
import streamlit as st
from langchain_openai import ChatOpenAI

st.set_page_config(
    page_title="Northbridge Bank Credit Risk Query Engine",
    page_icon="🏦",
    layout="wide",
)

DB_FILE = "credit_risk_portfolio.db"
LATEST_REPORTING_DATE = "2025-09-30"
LATEST_RATING_DATE = "2025-09-30"

DATABASE_SCHEMA = """
sector_master:
  sector_code (TEXT, PK)
  sector_name (TEXT)
  naics_code (TEXT)
  naics_description (TEXT)
  is_sensitive_sector (INTEGER)
loan_master:
  loan_account_number (TEXT, PK)
  borrower_id (TEXT)
  borrower_name (TEXT)
  borrower_type (TEXT)
  group_name (TEXT)
  state (TEXT)
  product_type (TEXT)
  loan_category (TEXT)
  sector_code (TEXT, FK -> sector_master.sector_code)
  sanctioned_amount (REAL)
  disbursed_amount (REAL)
  outstanding_principal (REAL)
  outstanding_interest (REAL)
  total_outstanding (REAL)
  interest_rate (REAL)
  rate_type (TEXT)
  sanction_date (DATE)
  maturity_date (DATE)
  repayment_frequency (TEXT)
  branch_code (TEXT)
  branch_name (TEXT)
  relationship_manager (TEXT)
  is_consortium (INTEGER)
  is_restructured (INTEGER)
  restructuring_date (DATE)
  is_secured (INTEGER)
  days_past_due (INTEGER)
  asset_classification (TEXT)
  classification_date (DATE)
borrower_rating:
  rating_id (INTEGER, PK)
  borrower_id (TEXT, FK -> loan_master.borrower_id)
  rating_date (DATE)
  internal_rating (TEXT)
  previous_rating (TEXT)
  rating_direction (TEXT)
  external_rating_agency (TEXT)
  external_rating (TEXT)
  pd_estimate (REAL)
  rating_model_version (TEXT)
provisioning:
  provision_id (INTEGER, PK)
  loan_account_number (TEXT, FK -> loan_master.loan_account_number)
  reporting_date (DATE)
  ifrs9_stage (INTEGER)
  stage_rationale (TEXT)
  pd_12_month (REAL)
  pd_lifetime (REAL)
  lgd_estimate (REAL)
  ead_amount (REAL)
  ecl_amount (REAL)
  provision_held (REAL)
  provision_coverage_ratio (REAL)
  is_individually_assessed (INTEGER)
Available provisioning dates: 2024-12-31, 2025-03-31, 2025-06-30, 2025-09-30
Available rating dates: 2024-09-30, 2024-12-31, 2025-03-31, 2025-06-30, 2025-09-30
Latest reporting_date: 2025-09-30
Latest rating_date: 2025-09-30
NPA definition: asset_classification IN ('Substandard', 'Doubtful', 'Loss')
"""

SQL_1 = """
SELECT sm.sector_name,
       ROUND(SUM(lm.total_outstanding) / 1000000.0, 2) AS total_outstanding_mn,
       ROUND(SUM(CASE WHEN lm.asset_classification IN ('Substandard','Doubtful','Loss')
                      THEN lm.total_outstanding ELSE 0 END) / 1000000.0, 2) AS npa_outstanding_mn
FROM loan_master lm
JOIN sector_master sm ON lm.sector_code = sm.sector_code
GROUP BY sm.sector_name
ORDER BY total_outstanding_mn DESC
"""
SQL_2 = """
SELECT loan_category, COUNT(*) AS loan_count,
       ROUND(SUM(total_outstanding) / 1000000.0, 2) AS total_outstanding_mn
FROM loan_master
GROUP BY loan_category
ORDER BY total_outstanding_mn DESC
"""
SQL_3 = f"""
SELECT ifrs9_stage, COUNT(*) AS loan_count,
       ROUND(SUM(ead_amount) / 1000000.0, 2) AS total_ead_mn,
       ROUND(SUM(ecl_amount) / 1000000.0, 2) AS total_ecl_mn
FROM provisioning
WHERE reporting_date = '{LATEST_REPORTING_DATE}'
GROUP BY ifrs9_stage
ORDER BY ifrs9_stage
"""
SQL_4 = f"""
SELECT sm.sector_name,
       ROUND(AVG(p.provision_coverage_ratio), 2) AS avg_provision_coverage_ratio
FROM provisioning p
JOIN loan_master lm ON p.loan_account_number = lm.loan_account_number
JOIN sector_master sm ON lm.sector_code = sm.sector_code
WHERE p.reporting_date = '{LATEST_REPORTING_DATE}'
GROUP BY sm.sector_name
ORDER BY avg_provision_coverage_ratio DESC
"""
SQL_5 = """
SELECT lm.borrower_name, sm.sector_name,
       ROUND(lm.total_outstanding / 1000000.0, 2) AS outstanding_mn,
       lm.asset_classification
FROM loan_master lm
JOIN sector_master sm ON lm.sector_code = sm.sector_code
ORDER BY lm.total_outstanding DESC
LIMIT 10
"""
SQL_6 = """
SELECT group_name, COUNT(*) AS loan_count,
       ROUND(SUM(total_outstanding) / 1000000.0, 2) AS total_outstanding_mn
FROM loan_master
WHERE group_name IS NOT NULL
GROUP BY group_name
ORDER BY SUM(total_outstanding) DESC
LIMIT 5
"""
SQL_7 = """
SELECT lm.loan_account_number, lm.borrower_name, sm.sector_name,
       ROUND(lm.total_outstanding / 1000000.0, 2) AS outstanding_mn,
       lm.days_past_due, lm.asset_classification
FROM loan_master lm
JOIN sector_master sm ON lm.sector_code = sm.sector_code
WHERE lm.days_past_due > 0
ORDER BY lm.days_past_due DESC
"""
SQL_8 = """
SELECT CASE
         WHEN days_past_due = 0 THEN '0 (Current)'
         WHEN days_past_due BETWEEN 1 AND 30 THEN '1-30'
         WHEN days_past_due BETWEEN 31 AND 60 THEN '31-60'
         WHEN days_past_due BETWEEN 61 AND 90 THEN '61-90'
         ELSE '90+'
       END AS dpd_bucket,
       COUNT(*) AS loan_count,
       ROUND(SUM(total_outstanding) / 1000000.0, 2) AS total_outstanding_mn
FROM loan_master
GROUP BY CASE
           WHEN days_past_due = 0 THEN '0 (Current)'
           WHEN days_past_due BETWEEN 1 AND 30 THEN '1-30'
           WHEN days_past_due BETWEEN 31 AND 60 THEN '31-60'
           WHEN days_past_due BETWEEN 61 AND 90 THEN '61-90'
           ELSE '90+'
         END
ORDER BY CASE dpd_bucket
           WHEN '0 (Current)' THEN 1
           WHEN '1-30' THEN 2
           WHEN '31-60' THEN 3
           WHEN '61-90' THEN 4
           WHEN '90+' THEN 5
         END
"""
SQL_9 = f"""
SELECT borrower_id, previous_rating, internal_rating, pd_estimate
FROM borrower_rating
WHERE rating_date = '{LATEST_RATING_DATE}'
  AND rating_direction = 'Downgraded'
ORDER BY pd_estimate DESC
"""
SQL_10 = """
SELECT reporting_date,
       ROUND(SUM(ecl_amount) / 1000000.0, 2) AS total_ecl_mn
FROM provisioning
GROUP BY reporting_date
ORDER BY reporting_date ASC
"""

VERIFIED_QUERY_LIBRARY = {
    "VQ1": {"description": "Sector-wise total outstanding and NPA amount breakdown across all sectors", "sql": SQL_1},
    "VQ2": {"description": "Total portfolio outstanding broken down by loan category (Corporate, Mid-Corporate, SME)", "sql": SQL_2},
    "VQ3": {"description": "IFRS 9 stage-wise summary showing loan count, exposure at default, and expected credit loss for the latest quarter", "sql": SQL_3},
    "VQ4": {"description": "Average provision coverage ratio by sector for the latest reporting quarter", "sql": SQL_4},
    "VQ5": {"description": "Top 10 largest loan exposures by outstanding amount at the borrower level", "sql": SQL_5},
    "VQ6": {"description": "Top 5 largest exposures aggregated at the business group level", "sql": SQL_6},
    "VQ7": {"description": "All overdue loan accounts with their days past due and asset classification", "sql": SQL_7},
    "VQ8": {"description": "Distribution of loans across days-past-due buckets showing aging profile of the portfolio", "sql": SQL_8},
    "VQ9": {"description": "Borrowers whose internal rating was downgraded in the latest rating cycle", "sql": SQL_9},
    "VQ10": {"description": "Expected credit loss trend across all reporting quarters showing provisioning movement over time", "sql": SQL_10},
}


def _secret(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)


@st.cache_resource
def get_llms():
    api_key = _secret("OPENAI_API_KEY")
    base_url = _secret("OPENAI_API_BASE") or _secret("OPENAI_BASE_URL")
    model = _secret("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured in Streamlit secrets or environment variables.")
    kwargs = {"model": model, "temperature": 0, "api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs), ChatOpenAI(**kwargs)


@st.cache_resource
def get_connection():
    db_path = Path(__file__).resolve().parent / DB_FILE
    if not db_path.exists():
        raise FileNotFoundError(f"Required database file not found: {DB_FILE}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)


def clean_llm_text(text: str) -> str:
    return re.sub(r"^```(?:sql)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()


def extract_json(text: str):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("The model response did not contain a JSON object.")
    return json.loads(match.group())


def classify_intent(user_question, query_library, llm):
    descriptions = "\n".join(f"{qid}: {entry['description']}" for qid, entry in query_library.items())
    prompt = f"""
You are an intent classifier for a commercial lending credit-risk query engine.
Choose verified only when the question has the same core analytical intent as a listed template. A template may return a broader result set than requested. Choose generated for calculations or breakdowns not covered by a template. Do not match only on keywords.

USER QUESTION:
{user_question}

VERIFIED QUERY LIBRARY:
{descriptions}

Return only valid JSON:
{{"route":"verified or generated","query_id":"VQ1-VQ10 or null","match_reason":"one short sentence"}}
"""
    try:
        result = extract_json(llm.invoke(prompt).content)
    except Exception as exc:
        return {"route": "generated", "query_id": None, "match_reason": f"Classification fallback: {exc}"}
    route = str(result.get("route", "generated")).lower()
    query_id = result.get("query_id")
    if route != "verified" or query_id not in query_library:
        return {"route": "generated", "query_id": None, "match_reason": result.get("match_reason", "No direct template match")}
    return {"route": "verified", "query_id": query_id, "match_reason": result.get("match_reason", "Verified template match")}


def generate_query(user_question, schema_context, llm):
    prompt = f"""
You are a credit risk SQL analyst. Generate one read-only SQLite query that exactly answers the question.
Rules: return SQL only; begin with SELECT or WITH; never modify the database; use only the schema; use total_outstanding for exposure; define NPA as asset_classification IN ('Substandard','Doubtful','Loss'); latest provisioning date is 2025-09-30; provisioning trends may use only 2024-12-31, 2025-03-31, 2025-06-30, 2025-09-30; latest rating date is 2025-09-30; convert monetary results to millions when appropriate; do not invent fields or rules.

USER QUESTION:
{user_question}

DATABASE SCHEMA:
{schema_context}
"""
    return clean_llm_text(llm.invoke(prompt).content)


def validate_query(user_question, candidate_sql, db_connection, query_library, evaluator_llm, query_id=None):
    result = {"passed": False, "failed_check": None, "details": "", "relevance_confidence": None}
    normalized = candidate_sql.strip().rstrip(";").strip()
    upper = normalized.upper()
    forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "REPLACE", "ATTACH", "CREATE", "PRAGMA"]
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        result.update(failed_check="read_only_shape", details="Query must start with SELECT or WITH")
        return result
    for keyword in forbidden:
        if re.search(rf"\b{keyword}\b", upper):
            result.update(failed_check="read_only_shape", details=f"Forbidden keyword detected: {keyword}")
            return result
    if ";" in normalized:
        result.update(failed_check="read_only_shape", details="Multiple statements are not allowed")
        return result
    if len(sqlparse.parse(normalized)) != 1:
        result.update(failed_check="read_only_shape", details="Exactly one SQL statement is required")
        return result
    cursor = db_connection.cursor()
    try:
        cursor.execute(f"EXPLAIN {normalized}")
        cursor.fetchall()
    except sqlite3.Error as exc:
        result.update(failed_check="parse_plan_dry_run", details=f"SQL failed to parse or plan: {exc}")
        return result

    verified_context = query_id is not None and query_id in query_library
    track = (
        "This is a verified template and may intentionally return a broad result set; judge its metric, tables, and aggregation, not absence of a narrow filter."
        if verified_context
        else "This generated SQL must be scoped to the user's exact question."
    )
    prompt = f"""
You are a SQL validation reviewer. Determine whether the candidate SQL correctly answers the question.
Check tables, columns, joins, metrics, filters, aggregation, NPA logic, and dates. Latest provisioning date is 2025-09-30 and latest rating date is 2025-09-30. Do not reject harmless alias or formatting differences.

TRACK CONTEXT:
{track}

USER QUESTION:
{user_question}

CANDIDATE SQL:
{normalized}

Return only JSON:
{{"verdict":"yes or no","confidence":0.0,"reason":"one short sentence"}}
"""
    try:
        relevance = extract_json(evaluator_llm.invoke(prompt).content)
        confidence = float(relevance.get("confidence", 0.0))
        verdict = str(relevance.get("verdict", "no")).lower()
    except Exception as exc:
        result.update(failed_check="llm_relevance", details=f"Could not parse evaluator response: {exc}", relevance_confidence=0.0)
        return result
    result["relevance_confidence"] = confidence
    if verdict != "yes" or confidence < 0.6:
        result.update(failed_check="llm_relevance", details=f"Relevance check failed: {relevance.get('reason', 'unknown')}")
        return result

    if verified_context:
        expected_sql = query_library[query_id]["sql"].strip().rstrip(";")
        try:
            expected_cols = [d[0] for d in cursor.execute(f"SELECT * FROM ({expected_sql}) AS expected_query LIMIT 0").description]
            actual_cols = [d[0] for d in cursor.execute(f"SELECT * FROM ({normalized}) AS actual_query LIMIT 0").description]
        except sqlite3.Error as exc:
            result.update(failed_check="template_integrity", details=f"Template integrity check failed: {exc}")
            return result
        if expected_cols != actual_cols:
            result.update(failed_check="template_integrity", details=f"Expected columns {expected_cols}; received {actual_cols}")
            return result

    result.update(passed=True, details="All validation checks passed")
    return result


def retry_generation(user_question, failed_sql, error_message, schema_context, llm):
    prompt = f"""
Correct this failed SQL while preserving the user's intent. Return one read-only SQLite SELECT/WITH query only. Use only the schema. Do not invent fields or business rules. Provisioning dates are 2024-12-31, 2025-03-31, 2025-06-30, and 2025-09-30.

USER QUESTION:
{user_question}

FAILED SQL:
{failed_sql}

VALIDATION ERROR:
{error_message}

DATABASE SCHEMA:
{schema_context}
"""
    return clean_llm_text(llm.invoke(prompt).content)


def execute_query(sql, connection):
    df = pd.read_sql_query(sql, connection)
    warnings = []
    if df.empty:
        warnings.append("Query returned an empty result")
    for col in df.select_dtypes(include="number").columns:
        if (df[col] < 0).any() and "deviation" not in col.lower() and "change" not in col.lower():
            warnings.append(f"Column {col} contains negative values")
        if df[col].isna().sum() > len(df) * 0.5:
            warnings.append(f"Column {col} contains more than 50% null values")
    return {"dataframe": df, "reasonable": len(warnings) <= 2, "warnings": warnings}


def generate_response(user_question, dataframe, llm):
    prompt = f"""
You are a credit risk analyst. Answer the question directly using only the query results. Preserve units; columns ending in _mn are millions. Focus on requested rows if the result is broader. For trends, describe movement. For lists, include all relevant rows. Do not invent values or discuss SQL unless asked.

USER QUESTION:
{user_question}

QUERY RESULTS:
{dataframe.to_string(index=False)}
"""
    return llm.invoke(prompt).content.strip()


def run_pipeline(user_question, connection, query_library, schema_context, llm, evaluator_llm):
    log = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "user_question": user_question,
        "route": None,
        "query_id": None,
        "match_reason": None,
        "candidate_sql": None,
        "gate_result": None,
        "retry_used": False,
        "escalated": False,
        "executed_sql": None,
        "row_count": None,
        "confidence": None,
        "narrative": None,
        "warnings": [],
    }
    classification = classify_intent(user_question, query_library, llm)
    log.update(route=classification["route"], query_id=classification.get("query_id"), match_reason=classification.get("match_reason"))
    if log["route"] == "verified" and log["query_id"] in query_library:
        candidate_sql = query_library[log["query_id"]]["sql"]
    else:
        candidate_sql = generate_query(user_question, schema_context, llm)
    log["candidate_sql"] = candidate_sql
    gate = validate_query(user_question, candidate_sql, connection, query_library, evaluator_llm, log["query_id"])
    log["gate_result"] = gate
    if not gate["passed"] and log["route"] == "generated":
        candidate_sql = retry_generation(user_question, candidate_sql, gate["details"], schema_context, llm)
        log["candidate_sql"] = candidate_sql
        log["retry_used"] = True
        gate = validate_query(user_question, candidate_sql, connection, query_library, evaluator_llm, None)
        log["gate_result"] = gate
    if not gate["passed"]:
        log.update(escalated=True, confidence="ESCALATED", narrative=f"Query could not be reliably resolved. Escalated to a human analyst. Failure: {gate['details']}")
        return {"log": log, "dataframe": None, **log}
    executed_sql = candidate_sql.strip().rstrip(";")
    executed = execute_query(executed_sql, connection)
    df = executed["dataframe"]
    log.update(executed_sql=executed_sql, row_count=len(df), warnings=executed["warnings"])
    log["narrative"] = generate_response(user_question, df, llm)
    log["confidence"] = gate.get("relevance_confidence")
    return {"log": log, "dataframe": df, **log}


st.title("Northbridge Bank Credit Risk Query Engine")
st.caption("Natural-language analysis with verified SQL templates, generated-query validation, one retry, and an auditable result trace.")

with st.sidebar:
    st.header("Application status")
    st.write(f"Database: `{DB_FILE}`")
    st.write(f"Verified templates: **{len(VERIFIED_QUERY_LIBRARY)}**")
    st.write(f"Latest provisioning date: **{LATEST_REPORTING_DATE}**")
    st.write(f"Latest rating date: **{LATEST_RATING_DATE}**")
    st.divider()
    st.subheader("Example questions")
    examples = [
        "How much of our book is in real estate, and how much of that is non-performing?",
        "Show me how our overdue book is distributed across DPD buckets.",
        "List every restructured loan, flag which are impaired, and show the overall impaired percentage.",
        "Show me the average interest rate by sector, sorted from highest to lowest.",
        "How has the Stage 3 book size and its expected credit loss moved over the last four quarters?",
    ]
    for example in examples:
        if st.button(example, use_container_width=True):
            st.session_state["question"] = example

question = st.text_area(
    "Ask a commercial lending portfolio question",
    key="question",
    height=100,
    placeholder="Example: Show me how our overdue book is distributed across DPD buckets.",
)
run_clicked = st.button("Run query", type="primary", disabled=not question.strip())

if run_clicked:
    try:
        llm, evaluator_llm = get_llms()
        connection = get_connection()
        with st.spinner("Classifying, validating, and running the query..."):
            result = run_pipeline(question.strip(), connection, VERIFIED_QUERY_LIBRARY, DATABASE_SCHEMA, llm, evaluator_llm)
        st.session_state["last_result"] = result
    except Exception as exc:
        st.error(f"Application error: {exc}")

result = st.session_state.get("last_result")
if result:
    if result["escalated"]:
        st.warning(result["narrative"])
    else:
        st.subheader("Answer")
        st.write(result["narrative"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Route", str(result["route"]).title())
    c2.metric("Template", result["query_id"] or "Generated")
    confidence_display = result["confidence"] if isinstance(result["confidence"], str) else f"{result['confidence']:.2f}"
    c3.metric("Confidence", confidence_display)
    c4.metric("Rows returned", result["row_count"] if result["row_count"] is not None else 0)

    if result.get("warnings"):
        st.warning("; ".join(result["warnings"]))

    if result.get("dataframe") is not None:
        st.subheader("Raw query results")
        st.dataframe(result["dataframe"], use_container_width=True, hide_index=True)
        st.download_button(
            "Download results as CSV",
            result["dataframe"].to_csv(index=False).encode("utf-8"),
            file_name="credit_risk_query_results.csv",
            mime="text/csv",
        )

    with st.expander("SQL used", expanded=False):
        st.code(result.get("executed_sql") or result.get("candidate_sql") or "No SQL executed", language="sql")

    with st.expander("Audit trace", expanded=False):
        audit_log = dict(result["log"])
        audit_log.pop("narrative", None)
        st.json(audit_log)
        st.download_button(
            "Download audit log",
            json.dumps(result["log"], default=str, indent=2),
            file_name="credit_risk_query_audit.json",
            mime="application/json",
        )

    with st.expander("Routing explanation", expanded=False):
        st.write(result.get("match_reason") or "No routing explanation available.")
