import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import re
import json
import os
from pathlib import Path
from datetime import date, timedelta
from io import BytesIO
from fpdf import FPDF
import unicodedata
import math

# =====================================================
# CONFIG
# =====================================================
st.set_page_config(page_title="Weekly Sales & Stock Report", layout="wide")

CURRENT_YEAR = date.today().year
WEEKS_IN_YEAR = 52
MONTHS_IN_YEAR = 12

# Paths for saved report data (committed to repo for sharing)
DATA_DIR = Path(__file__).parent / "data"
SAVED_REPORT_CSV = DATA_DIR / "report.csv"
SAVED_REPORT_META = DATA_DIR / "report_meta.json"

ADMIN_PASSWORD = "twc2026"  # Change this to your preferred password

# =====================================================
# HELPERS
# =====================================================

def normalize_columns(df):
    """Lowercase, strip whitespace, remove parens/%, replace spaces with underscores."""
    df = df.copy()
    df.columns = (
        df.columns.str.strip()
        .str.lower()
        .str.replace("(", "", regex=False)
        .str.replace(")", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace(" ", "_", regex=False)
    )
    return df


def normalize_sku(sku):
    """Convert 3-segment SKU (047-003-22) → 2-segment group (047-003)."""
    if pd.isna(sku):
        return ""
    s = str(sku).strip()
    if s.endswith(".0"):
        s = s[:-2]
    parts = [p.strip() for p in s.split("-") if p is not None]
    if len(parts) >= 2:
        p1, p2 = parts[0], parts[1]
        if p1.isdigit():
            p1 = p1.zfill(3)
        if p2.isdigit():
            p2 = p2.zfill(3)
        return f"{p1}-{p2}"
    return s


def sku_to_group_key(sku):
    """Numeric-only 6-digit key for grouping (e.g. '047003')."""
    s = normalize_sku(sku)
    parts = s.split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0].zfill(3)}{parts[1].zfill(3)}"
    return re.sub(r"[^0-9]", "", str(s))[:6].zfill(6)


def read_uploaded(uploaded_file):
    """Read CSV or Excel and normalize columns."""
    if uploaded_file.name.lower().endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)
    return normalize_columns(df)


def find_col(df, must_have=None, must_not_have=None):
    """Find first column whose name contains all of *must_have* but none of *must_not_have*."""
    must_have = must_have or []
    must_not_have = must_not_have or []
    for col in df.columns:
        if all(k in col for k in must_have) and not any(k in col for k in must_not_have):
            return col
    return None


def safe_div(a, b, fill=np.nan):
    """Element-wise a/b, returning *fill* where b == 0 or NaN."""
    return np.where((b > 0) & np.isfinite(b), a / b, fill)

def first_non_empty(values):
    """Return first meaningful (non-empty) value from a Series-like input."""
    for v in values:
        if pd.isna(v):
            continue
        s = str(v).strip()
        if s and s.lower() not in {"nan", "none", "null", "--"}:
            return s
    return np.nan

def item_name_key(name):
    """Normalize item names for metadata lookup across vintage/format variants."""
    if pd.isna(name):
        return ""
    s = str(name).lower().strip()
    s = s.replace("#vintage#", " ")
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def health_label(pace):
    if pd.isna(pace):
        return ""
    if pace >= 0.10:
        return "🟢 Ahead"
    if pace >= -0.10:
        return "🟡 On Track"
    if pace >= -0.30:
        return "🟠 Behind"
    return "🔴 Critical"


def week_number_now():
    """ISO week number for today."""
    return date.today().isocalendar()[1]


def weeks_elapsed_in_year():
    """Completed weeks so far (at least 1)."""
    return max(1, date.today().isocalendar()[1] - 1)


# ── PDF / Excel export helpers ──────────────────────

def sanitize_pdf(v):
    if isinstance(v, str):
        return v.replace("–", "-").replace("—", "-").replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return v

def sanitize_pdf_safe(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return ""
    s = sanitize_pdf(str(v))
    s = str(s).replace("…", "...")
    s = unicodedata.normalize("NFKC", s)
    return s.encode("latin-1", errors="replace").decode("latin-1")

def df_to_excel(df):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    return buf.getvalue()

def df_to_pdf(df):
    pdf = FPDF(orientation="L")
    pdf.add_page()
    pdf.set_font("Arial", size=6)
    col_w = [max(24, len(str(c)) * 2.5) for c in df.columns]
    rh = 5
    for i, c in enumerate(df.columns):
        pdf.cell(col_w[i], rh, sanitize_pdf_safe(c), border=1)
    pdf.ln(rh)
    for _, row in df.iterrows():
        for i, v in enumerate(row):
            pdf.cell(col_w[i], rh, sanitize_pdf_safe(v), border=1)
        pdf.ln(rh)
    output = pdf.output(dest="S")
    if isinstance(output, str):
        return output.encode("latin-1")
    return bytes(output)


# =====================================================
# ADMIN MODE CHECK
# =====================================================
# Sidebar: admin login
st.sidebar.markdown("---")
admin_pwd = st.sidebar.text_input("🔒 Admin Login", type="password", help="Enter admin password to upload data & generate reports")
is_admin = (admin_pwd == ADMIN_PASSWORD)

if is_admin:
    st.sidebar.success("✅ Admin mode active")

# =====================================================
# LOAD SAVED REPORT (for viewer / shared dashboard)
# =====================================================
def load_saved_report():
    """Load previously saved report from data/ folder."""
    if SAVED_REPORT_CSV.exists() and SAVED_REPORT_META.exists():
        try:
            rpt = pd.read_csv(SAVED_REPORT_CSV)
            with open(SAVED_REPORT_META, "r") as f:
                meta = json.load(f)
            return rpt, meta
        except Exception:
            return pd.DataFrame(), {}
    return pd.DataFrame(), {}

# Initialize report from saved data if available
if "report" not in st.session_state or st.session_state.get("report", pd.DataFrame()).empty:
    saved_rpt, saved_meta = load_saved_report()
    if not saved_rpt.empty:
        st.session_state["report"] = saved_rpt
        st.session_state["health_stats"] = saved_meta.get("health_stats", {})
        st.session_state["saved_report_week"] = saved_meta.get("report_week", 0)
        st.session_state["saved_weeks_elapsed"] = saved_meta.get("weeks_elapsed", 0)

# =====================================================
# ADMIN: SESSION STATE
# =====================================================
_state_keys = {
    "sales_cy": pd.DataFrame(),
    "sales_py": pd.DataFrame(),
    "stock_cy": pd.DataFrame(),
    "stock_py": pd.DataFrame(),
    "purchases_cy": pd.DataFrame(),
    "purchases_py": pd.DataFrame(),
    "twc_stock": pd.DataFrame(),
    "item_details": pd.DataFrame(),
    "planning": pd.DataFrame(),
}
for k, default in _state_keys.items():
    if k not in st.session_state:
        st.session_state[k] = default.copy()
if "report" not in st.session_state:
    st.session_state["report"] = pd.DataFrame()
# Default upload vars (only populated in admin mode)
sales_cy_file = sales_py_file = stock_cy_file = stock_py_file = None
purch_cy_file = purch_py_file = twc_file = item_details_file = planning_file = None

# =====================================================
# ADMIN: SIDEBAR — FILE UPLOADS
# =====================================================
if is_admin:
    st.title("📊 Weekly Sales & Stock Planning Report — Admin")

    st.sidebar.header("📁 Upload Weekly Data Files")
    st.sidebar.caption(
        "Upload this week's exports. The system will auto-detect columns. "
        "Files follow the naming pattern: *YTD {Month} week {N} {YY}*."
    )

    # --- Sales files ---
    st.sidebar.subheader("Sales by Item (YTD)")
    sales_cy_file = st.sidebar.file_uploader("Current Year Sales", type=["csv", "xlsx"], key="up_sales_cy")
    sales_py_file = st.sidebar.file_uploader("Previous Year Sales", type=["csv", "xlsx"], key="up_sales_py")

    # --- Stock Summary files ---
    st.sidebar.subheader("Stock Summary (YTD)")
    stock_cy_file = st.sidebar.file_uploader("Current Year Stock Summary", type=["csv", "xlsx"], key="up_stock_cy")
    stock_py_file = st.sidebar.file_uploader("Previous Year Stock Summary", type=["csv", "xlsx"], key="up_stock_py")

    # --- Purchases files ---
    st.sidebar.subheader("Purchases by Item (YTD)")
    purch_cy_file = st.sidebar.file_uploader("Current Year Purchases", type=["csv", "xlsx"], key="up_purch_cy")
    purch_py_file = st.sidebar.file_uploader("Previous Year Purchases", type=["csv", "xlsx"], key="up_purch_cy2")

    # --- TWC Stock ---
    st.sidebar.subheader("TWC Stock")
    twc_file = st.sidebar.file_uploader("TWC Stock Summary", type=["csv", "xlsx"], key="up_twc")

    # --- Item Details (metadata) ---
    st.sidebar.subheader("Item Details (Optional)")
    item_details_file = st.sidebar.file_uploader(
        "Item Details / Master",
        type=["csv", "xlsx"],
        key="up_item_details",
        help="Optional file containing SKU/Item Name with Brand and Country of Origin.",
    )

    # --- Planning ---
    st.sidebar.subheader("Sales Planning / Targets")
    planning_file = st.sidebar.file_uploader("Stock Planning 2026 (XLSX)", type=["csv", "xlsx"], key="up_plan")
else:
    st.title("📊 Weekly Sales & Stock Planning Report")


# =====================================================
# PROCESS UPLOADS INTO SESSION STATE
# =====================================================

def _process_sales(uploaded, state_key):
    """Read a Sales-by-Item file → grouped by SKU prefix."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    item_col = find_col(raw, ["item", "name"]) or find_col(raw, ["item_name"])
    qty_col = find_col(raw, ["quantity_sold"]) or find_col(raw, ["quantity"]) or find_col(raw, ["sales"])
    amt_col = find_col(raw, ["amount"], ["stock", "opening", "closing"])
    cat_col = find_col(raw, ["category_name"])
    origin_col = find_col(raw, ["country"]) or find_col(raw, ["origin"])
    brand_col = find_col(raw, ["brand"])

    if not sku_col or not qty_col:
        st.sidebar.error(f"Cannot detect SKU or Quantity column in {uploaded.name}")
        return

    raw["_sku"] = raw[sku_col].apply(normalize_sku)
    raw["_sku_grp"] = raw["_sku"].apply(sku_to_group_key)
    raw = raw[raw["_sku_grp"].str.replace("0", "") != ""].copy()
    raw["_qty"] = pd.to_numeric(raw[qty_col], errors="coerce").fillna(0)
    raw["_amt"] = pd.to_numeric(raw[amt_col], errors="coerce").fillna(0) if amt_col else 0

    agg = {"_sku": first_non_empty, "_qty": "sum", "_amt": "sum"}
    if item_col:
        raw["_item"] = raw[item_col]
        agg["_item"] = first_non_empty
    if cat_col:
        raw["_cat"] = raw[cat_col]
        agg["_cat"] = first_non_empty
    if origin_col:
        raw["_origin"] = raw[origin_col]
        agg["_origin"] = first_non_empty
    if brand_col:
        raw["_brand"] = raw[brand_col]
        agg["_brand"] = first_non_empty

    grouped = raw.groupby("_sku_grp", as_index=False).agg(agg)
    grouped.rename(columns={
        "_sku_grp": "sku_key", "_sku": "sku", "_qty": "quantity_sold",
        "_amt": "revenue", "_item": "item_name", "_cat": "category",
        "_origin": "country_of_origin", "_brand": "brand",
    }, inplace=True)

    st.session_state[state_key] = grouped


def _process_stock(uploaded, state_key):
    """Read a Stock Summary file → grouped by SKU prefix."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    open_col = find_col(raw, ["opening_stock"], ["amount"]) or find_col(raw, ["opening"], ["amount"])
    close_col = (
        find_col(raw, ["closing_stock"], ["amount"])
        or find_col(raw, ["closing_balance"], ["amount"])
        or find_col(raw, ["closing"], ["amount"])
    )
    qty_in_col = find_col(raw, ["quantity_in"], ["amount"]) or find_col(raw, ["qty_in"])
    qty_out_col = find_col(raw, ["quantity_out"], ["amount"]) or find_col(raw, ["qty_out"])
    cat_col = find_col(raw, ["category_name"])
    item_col = find_col(raw, ["item", "name"]) or find_col(raw, ["item_name"])
    origin_col = find_col(raw, ["country"]) or find_col(raw, ["origin"])
    brand_col = find_col(raw, ["brand"])

    if not sku_col or not close_col:
        st.sidebar.error(f"Cannot detect SKU or Closing Stock column in {uploaded.name}")
        return

    raw["_sku"] = raw[sku_col].apply(normalize_sku)
    raw["_sku_grp"] = raw["_sku"].apply(sku_to_group_key)
    raw = raw[raw["_sku_grp"].str.replace("0", "") != ""].copy()

    for src, tgt in [
        (open_col, "_opening"), (close_col, "_closing"),
        (qty_in_col, "_qty_in"), (qty_out_col, "_qty_out"),
    ]:
        if src:
            raw[tgt] = pd.to_numeric(raw[src], errors="coerce").fillna(0)
        else:
            raw[tgt] = 0

    agg = {"_sku": first_non_empty, "_opening": "sum", "_closing": "sum", "_qty_in": "sum", "_qty_out": "sum"}
    if item_col:
        raw["_item"] = raw[item_col]
        agg["_item"] = first_non_empty
    if cat_col:
        raw["_cat"] = raw[cat_col]
        agg["_cat"] = first_non_empty
    if origin_col:
        raw["_origin"] = raw[origin_col]
        agg["_origin"] = first_non_empty
    if brand_col:
        raw["_brand"] = raw[brand_col]
        agg["_brand"] = first_non_empty

    grouped = raw.groupby("_sku_grp", as_index=False).agg(agg)
    grouped.rename(columns={
        "_sku_grp": "sku_key", "_sku": "sku", "_opening": "opening_stock",
        "_closing": "closing_stock", "_qty_in": "qty_in", "_qty_out": "qty_out",
        "_item": "item_name", "_cat": "category", "_origin": "country_of_origin",
        "_brand": "brand",
    }, inplace=True)

    st.session_state[state_key] = grouped


def _process_purchases(uploaded, state_key):
    """Read a Purchases-by-Item file → grouped by SKU prefix."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    qty_col = find_col(raw, ["quantity_purchased"]) or find_col(raw, ["quantity"])
    amt_col = find_col(raw, ["amount"], ["stock", "opening", "closing"])
    item_col = find_col(raw, ["item", "name"]) or find_col(raw, ["item_name"])
    cat_col = find_col(raw, ["category_name"])
    origin_col = find_col(raw, ["country"]) or find_col(raw, ["origin"])
    brand_col = find_col(raw, ["brand"])

    if not sku_col or not qty_col:
        st.sidebar.error(f"Cannot detect SKU or Quantity column in {uploaded.name}")
        return

    raw["_sku"] = raw[sku_col].apply(normalize_sku)
    raw["_sku_grp"] = raw["_sku"].apply(sku_to_group_key)
    raw = raw[raw["_sku_grp"].str.replace("0", "") != ""].copy()
    raw["_qty"] = pd.to_numeric(raw[qty_col], errors="coerce").fillna(0)
    raw["_amt"] = pd.to_numeric(raw[amt_col], errors="coerce").fillna(0) if amt_col else 0

    agg = {"_sku": first_non_empty, "_qty": "sum", "_amt": "sum"}
    if item_col:
        raw["_item"] = raw[item_col]
        agg["_item"] = first_non_empty
    if cat_col:
        raw["_cat"] = raw[cat_col]
        agg["_cat"] = first_non_empty
    if origin_col:
        raw["_origin"] = raw[origin_col]
        agg["_origin"] = first_non_empty
    if brand_col:
        raw["_brand"] = raw[brand_col]
        agg["_brand"] = first_non_empty

    grouped = raw.groupby("_sku_grp", as_index=False).agg(agg)
    grouped.rename(columns={
        "_sku_grp": "sku_key", "_sku": "sku",
        "_qty": "quantity_purchased", "_amt": "purchase_amount",
        "_item": "item_name", "_cat": "category", "_origin": "country_of_origin",
        "_brand": "brand",
    }, inplace=True)

    st.session_state[state_key] = grouped


def _process_twc(uploaded):
    """Read TWC Stock Summary → grouped by SKU prefix."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    close_col = (
        find_col(raw, ["closing_stock"], ["amount"])
        or find_col(raw, ["closing_balance"], ["amount"])
        or find_col(raw, ["closing"], ["amount"])
        or find_col(raw, ["stock"], ["amount", "opening"])
    )
    item_col = find_col(raw, ["item", "name"]) or find_col(raw, ["item_name"])
    cat_col = find_col(raw, ["category_name"])
    origin_col = find_col(raw, ["country"]) or find_col(raw, ["origin"])
    brand_col = find_col(raw, ["brand"])
    if not sku_col or not close_col:
        st.sidebar.error(f"Cannot detect SKU or Stock column in {uploaded.name}")
        return

    raw["_sku"] = raw[sku_col].apply(normalize_sku)
    raw["_sku_grp"] = raw["_sku"].apply(sku_to_group_key)
    raw = raw[raw["_sku_grp"].str.replace("0", "") != ""].copy()
    raw[close_col] = pd.to_numeric(
        raw[close_col].replace("--", 0), errors="coerce"
    ).fillna(0)

    agg = {close_col: "sum", "_sku": first_non_empty}
    if item_col:
        raw["_item"] = raw[item_col]
        agg["_item"] = first_non_empty
    if cat_col:
        raw["_cat"] = raw[cat_col]
        agg["_cat"] = first_non_empty
    if origin_col:
        raw["_origin"] = raw[origin_col]
        agg["_origin"] = first_non_empty
    if brand_col:
        raw["_brand"] = raw[brand_col]
        agg["_brand"] = first_non_empty

    grouped = raw.groupby("_sku_grp", as_index=False).agg(agg)
    grouped.rename(columns={
        "_sku_grp": "sku_key", close_col: "twc_stock", "_sku": "sku",
        "_item": "item_name", "_cat": "category", "_origin": "country_of_origin",
        "_brand": "brand",
    }, inplace=True)
    st.session_state["twc_stock"] = grouped


def _process_item_details(uploaded):
    """Read optional item-details/master file for metadata backfill."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    item_col = find_col(raw, ["item", "name"]) or find_col(raw, ["item_name"]) or find_col(raw, ["name"])
    cat_col = find_col(raw, ["category_name"]) or find_col(raw, ["category"])
    origin_col = find_col(raw, ["country"]) or find_col(raw, ["origin"])
    brand_col = find_col(raw, ["brand"])

    if not sku_col and not item_col:
        st.sidebar.error(f"Cannot detect SKU or Item Name column in {uploaded.name}")
        return
    if not any([cat_col, origin_col, brand_col]):
        st.sidebar.error(f"No metadata columns found (category/country/brand) in {uploaded.name}")
        return

    work = raw.copy()
    agg = {}

    if sku_col:
        work["_sku"] = work[sku_col].apply(normalize_sku)
        work["_sku_grp"] = work["_sku"].apply(sku_to_group_key)
        work = work[work["_sku_grp"].str.replace("0", "") != ""].copy()
        agg["_sku"] = first_non_empty
    if item_col:
        work["_item"] = work[item_col]
        work["_item_key"] = work["_item"].apply(item_name_key)
        agg["_item"] = first_non_empty
        if sku_col:
            agg["_item_key"] = first_non_empty
    if cat_col:
        work["_cat"] = work[cat_col]
        agg["_cat"] = first_non_empty
    if origin_col:
        work["_origin"] = work[origin_col]
        agg["_origin"] = first_non_empty
    if brand_col:
        work["_brand"] = work[brand_col]
        agg["_brand"] = first_non_empty

    if sku_col:
        grouped = work.groupby("_sku_grp", as_index=False).agg(agg)
        grouped.rename(columns={"_sku_grp": "sku_key"}, inplace=True)
    else:
        if "_item_key" not in work.columns:
            st.sidebar.error(f"Cannot build metadata keys from {uploaded.name}")
            return
        work = work[work["_item_key"].astype(str).str.len() > 0].copy()
        grouped = work.groupby("_item_key", as_index=False).agg(agg)
        grouped.rename(columns={"_item_key": "item_name_key"}, inplace=True)

    grouped.rename(columns={
        "_sku": "sku",
        "_item": "item_name",
        "_item_key": "item_name_key",
        "_cat": "category",
        "_origin": "country_of_origin",
        "_brand": "brand",
    }, inplace=True)

    st.session_state["item_details"] = grouped


def _process_planning(uploaded):
    """Read Stock Planning file → monthly targets by SKU prefix."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    target_col = (
        find_col(raw, ["monthly", "sales", "target"])
        or find_col(raw, ["monthly_sales_target"])
        or find_col(raw, ["monthly", "target"])
    )
    budget_col = find_col(raw, ["next_year_budget"]) or find_col(raw, ["next", "year", "budget"])
    prev_sales_col = find_col(raw, ["previous_year_sales"]) or find_col(raw, ["previous", "year", "sales"])
    plan_growth_col = find_col(raw, ["growth_target"]) or find_col(raw, ["growth"])

    if not sku_col or not target_col:
        st.sidebar.error(
            f"Cannot detect SKU or Monthly Sales Target column. "
            f"Available: {list(raw.columns)}"
        )
        return

    raw["sku"] = raw[sku_col].apply(normalize_sku)
    result = pd.DataFrame({"sku": raw["sku"]})
    result["monthly_target_plan"] = pd.to_numeric(raw[target_col], errors="coerce").fillna(0)
    if budget_col:
        result["annual_budget_plan"] = pd.to_numeric(raw[budget_col], errors="coerce").fillna(0)
    else:
        result["annual_budget_plan"] = result["monthly_target_plan"] * 12
    if prev_sales_col:
        result["prev_year_sales_plan"] = pd.to_numeric(raw[prev_sales_col], errors="coerce").fillna(0)
    if plan_growth_col:
        result["growth_target_plan"] = pd.to_numeric(raw[plan_growth_col], errors="coerce").fillna(0)

    st.session_state["planning"] = result


# Run all processors (upload vars are None when not in admin mode — processors handle gracefully)
_process_sales(sales_cy_file, "sales_cy")
_process_sales(sales_py_file, "sales_py")
_process_stock(stock_cy_file, "stock_cy")
_process_stock(stock_py_file, "stock_py")
_process_purchases(purch_cy_file, "purchases_cy")
_process_purchases(purch_py_file, "purchases_py")
_process_twc(twc_file)
_process_item_details(item_details_file)
_process_planning(planning_file)


# =====================================================
# ADMIN: REPORT PARAMETERS & GENERATE
# =====================================================
if is_admin:

    st.header("⚙️ Report Parameters")

    p1, p2, p3 = st.columns(3)
    with p1:
        report_week = st.number_input(
            "Report Week #", min_value=1, max_value=52,
            value=week_number_now(),
            help="ISO week number of this report",
        )
    with p2:
        weeks_elapsed = st.number_input(
            "Weeks Elapsed (YTD period)", min_value=1, max_value=52,
            value=weeks_elapsed_in_year(),
            help="Number of completed weeks in the YTD data",
        )
    with p3:
        growth_pct = st.number_input(
            "Growth Target (%)", min_value=0.0, max_value=200.0,
            value=20.0,
        ) / 100

    months_elapsed = round(weeks_elapsed * 12 / 52, 2)
    remaining_weeks = WEEKS_IN_YEAR - weeks_elapsed
    remaining_months = round(remaining_weeks * 12 / 52, 2)

    st.caption(
        f"📅 Week {report_week} | {weeks_elapsed} weeks elapsed ≈ {months_elapsed:.1f} months | "
        f"{remaining_weeks} weeks / {remaining_months:.1f} months remaining"
    )


# =====================================================
# GENERATE REPORT (Admin only)
# =====================================================
if is_admin and st.button("🚀 Generate Weekly Report", type="primary"):

    # ── Validate minimum inputs ──
    if st.session_state.sales_cy.empty:
        st.error("Please upload the Current Year Sales file.")
        st.stop()
    if st.session_state.stock_cy.empty:
        st.error("Please upload the Current Year Stock Summary file.")
        st.stop()

    # ── Start from current-year sales ──
    df = st.session_state.sales_cy.copy()

    # ── Merge previous-year sales ──
    if not st.session_state.sales_py.empty:
        py = st.session_state.sales_py[["sku_key", "quantity_sold"]].rename(
            columns={"quantity_sold": "py_sales"}
        )
        df = df.merge(py, on="sku_key", how="left")
    else:
        df["py_sales"] = 0
    df["py_sales"] = df["py_sales"].fillna(0)

    # ── Merge current-year stock (outer join to include items with stock but no sales) ──
    stk = st.session_state.stock_cy.copy()
    stk.rename(columns={
        "opening_stock": "opening_stock_cy",
        "closing_stock": "total_stock",
        "qty_in": "qty_in_cy",
        "qty_out": "qty_out_cy",
    }, inplace=True)
    stk_merge_cols = ["sku_key", "opening_stock_cy", "total_stock", "qty_in_cy", "qty_out_cy"]
    for extra in ["sku", "item_name", "category"]:
        if extra in stk.columns:
            stk_merge_cols.append(extra)
    stk = stk[stk_merge_cols].copy()
    df = df.merge(stk, on="sku_key", how="outer", suffixes=("", "_stk"))

    # Fill metadata from stock for stock-only items (no sales)
    for col in ["sku", "item_name", "category"]:
        stk_col = f"{col}_stk"
        if stk_col in df.columns:
            if col in df.columns:
                df[col] = df[col].fillna(df[stk_col])
            else:
                df[col] = df[stk_col]
            df.drop(columns=[stk_col], inplace=True)
    df["quantity_sold"] = df["quantity_sold"].fillna(0)
    df["revenue"] = df["revenue"].fillna(0)

    # ── Merge previous-year stock ──
    if not st.session_state.stock_py.empty:
        stk_py = st.session_state.stock_py[["sku_key", "closing_stock"]].rename(
            columns={"closing_stock": "total_stock_py"}
        )
        df = df.merge(stk_py, on="sku_key", how="left")
    else:
        df["total_stock_py"] = np.nan

    # ── Merge purchases ──
    if not st.session_state.purchases_cy.empty:
        pur_cy = st.session_state.purchases_cy[["sku_key", "quantity_purchased"]].rename(
            columns={"quantity_purchased": "purchases_cy"}
        )
        df = df.merge(pur_cy, on="sku_key", how="left")
    else:
        df["purchases_cy"] = 0

    if not st.session_state.purchases_py.empty:
        pur_py = st.session_state.purchases_py[["sku_key", "quantity_purchased"]].rename(
            columns={"quantity_purchased": "purchases_py"}
        )
        df = df.merge(pur_py, on="sku_key", how="left")
    else:
        df["purchases_py"] = 0

    # ── Merge TWC stock ──
    if not st.session_state.twc_stock.empty:
        twc = st.session_state.twc_stock[["sku_key", "twc_stock"]].copy()
        df = df.merge(twc, on="sku_key", how="left")
    else:
        df["twc_stock"] = np.nan

    # ── Merge planning targets ──
    has_planning = not st.session_state.planning.empty
    if has_planning:
        plan = st.session_state.planning.copy()
        plan["sku_key"] = plan["sku"].apply(sku_to_group_key)
        plan_cols = [c for c in plan.columns if c != "sku"]
        df = df.merge(plan[plan_cols], on="sku_key", how="left")

    # ── Metadata backfill from all uploaded sources ──
    metadata_cols = ["sku", "item_name", "category", "country_of_origin", "brand"]
    metadata_sources = []
    for state_key in ["sales_cy", "sales_py", "stock_cy", "stock_py", "purchases_cy", "purchases_py", "twc_stock", "item_details"]:
        src = st.session_state.get(state_key, pd.DataFrame())
        if not isinstance(src, pd.DataFrame) or src.empty or "sku_key" not in src.columns:
            continue
        cols = ["sku_key"] + [c for c in metadata_cols if c in src.columns]
        if len(cols) > 1:
            metadata_sources.append(src[cols].copy())

    if metadata_sources:
        meta_all = pd.concat(metadata_sources, ignore_index=True, sort=False)
        meta_agg_spec = {}
        for col in metadata_cols:
            if col in meta_all.columns:
                meta_agg_spec[col] = first_non_empty
        meta = meta_all.groupby("sku_key", as_index=False).agg(meta_agg_spec)
        df = df.merge(meta, on="sku_key", how="left", suffixes=("", "_meta"))

        for col in metadata_cols:
            meta_col = f"{col}_meta"
            if meta_col not in df.columns:
                continue
            if col in df.columns:
                current = df[col]
                current_text = current.astype(str).str.strip().str.lower()
                missing_mask = current.isna() | current_text.isin(["", "nan", "none", "null", "--"])
                df[col] = np.where(missing_mask, df[meta_col], current)
            else:
                df[col] = df[meta_col]
            df.drop(columns=[meta_col], inplace=True)

    # ── Metadata enrichment for non-sales items ──
    for col in ["brand", "country_of_origin"]:
        if col not in df.columns:
            df[col] = np.nan

    if "item_name" in df.columns:
        df["_item_name_key"] = df["item_name"].apply(item_name_key)
        has_key = df["_item_name_key"].astype(str).str.len() > 0
        has_brand = df["brand"].notna() & ~df["brand"].astype(str).str.strip().str.lower().isin(["", "nan", "none", "null", "--"])
        has_country = df["country_of_origin"].notna() & ~df["country_of_origin"].astype(str).str.strip().str.lower().isin(["", "nan", "none", "null", "--"])

        by_name_sources = []
        base_name_cols = ["_item_name_key"]
        if has_brand.any():
            base_name_cols.append("brand")
        if has_country.any():
            base_name_cols.append("country_of_origin")
        if len(base_name_cols) > 1:
            by_name_sources.append(df.loc[has_key, base_name_cols].copy())

        details = st.session_state.get("item_details", pd.DataFrame())
        if isinstance(details, pd.DataFrame) and not details.empty:
            details_name = details.copy()
            if "item_name_key" not in details_name.columns and "item_name" in details_name.columns:
                details_name["item_name_key"] = details_name["item_name"].apply(item_name_key)
            if "item_name_key" in details_name.columns:
                dcols = ["item_name_key"]
                if "brand" in details_name.columns:
                    dcols.append("brand")
                if "country_of_origin" in details_name.columns:
                    dcols.append("country_of_origin")
                if len(dcols) > 1:
                    dn = details_name[dcols].copy().rename(columns={"item_name_key": "_item_name_key"})
                    dn = dn[dn["_item_name_key"].astype(str).str.len() > 0]
                    by_name_sources.append(dn)

        if by_name_sources:
            by_name_all = pd.concat(by_name_sources, ignore_index=True, sort=False)
            by_name_spec = {}
            if "brand" in by_name_all.columns:
                by_name_spec["brand"] = first_non_empty
            if "country_of_origin" in by_name_all.columns:
                by_name_spec["country_of_origin"] = first_non_empty
            by_name = (
                by_name_all
                .groupby("_item_name_key", as_index=False)
                .agg(by_name_spec)
                .rename(columns={"brand": "_brand_by_name", "country_of_origin": "_country_by_name"})
            )
            df = df.merge(by_name, on="_item_name_key", how="left")
            if "_brand_by_name" in df.columns:
                brand_missing = ~has_brand
                df["brand"] = np.where(brand_missing, df["_brand_by_name"], df["brand"])
                df.drop(columns=["_brand_by_name"], inplace=True)
            if "_country_by_name" in df.columns:
                country_missing = ~has_country
                df["country_of_origin"] = np.where(country_missing, df["_country_by_name"], df["country_of_origin"])
                df.drop(columns=["_country_by_name"], inplace=True)

        df.drop(columns=["_item_name_key"], inplace=True)

    # Fill country from known brand-country pairs where possible
    brand_vals = df["brand"].astype(str).str.strip()
    country_vals = df["country_of_origin"].astype(str).str.strip()
    valid_brand = df["brand"].notna() & ~brand_vals.str.lower().isin(["", "nan", "none", "null", "--"])
    valid_country = df["country_of_origin"].notna() & ~country_vals.str.lower().isin(["", "nan", "none", "null", "--"])
    brand_country = (
        df[valid_brand & valid_country]
        .groupby("brand", as_index=False)
        .agg({"country_of_origin": first_non_empty})
        .rename(columns={"country_of_origin": "_country_by_brand"})
    )
    if not brand_country.empty:
        df = df.merge(brand_country, on="brand", how="left")
        country_missing = ~valid_country
        df["country_of_origin"] = np.where(country_missing, df["_country_by_brand"], df["country_of_origin"])
        df.drop(columns=["_country_by_brand"], inplace=True)

    # Ensure report columns always exist (explicit fallback if unknown)
    for col in ["brand", "country_of_origin"]:
        vals = df[col].astype(str).str.strip()
        missing = df[col].isna() | vals.str.lower().isin(["", "nan", "none", "null", "--"])
        df[col] = np.where(missing, "Unknown", df[col])

    # ── Fill NaN numerics ──
    num_fills = {
        "total_stock": 0, "total_stock_py": 0, "twc_stock": 0,
        "opening_stock_cy": 0, "qty_in_cy": 0, "qty_out_cy": 0,
        "purchases_cy": 0, "purchases_py": 0, "revenue": 0,
        "py_sales": 0, "quantity_sold": 0,
    }
    for col, fill in num_fills.items():
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(fill)

    # ── Derived: bonded stock (reconciled) ──
    df["total_stock"] = df["total_stock"].clip(lower=0)
    df["twc_stock_raw"] = df["twc_stock"].fillna(0).clip(lower=0)
    _twc_over_total = int((df["twc_stock_raw"] > df["total_stock"]).sum())
    # Enforce stock identity: Total = TWC + Bonded
    df["twc_stock"] = np.minimum(df["twc_stock_raw"], df["total_stock"])
    df["bonded_stock"] = df["total_stock"] - df["twc_stock"]

    # ── YoY Sales Comparison ──
    df["yoy_sales_change"] = safe_div(
        df["quantity_sold"] - df["py_sales"], df["py_sales"]
    )

    # ── Weekly run rate ──
    df["weekly_run_rate"] = safe_div(df["quantity_sold"], weeks_elapsed)
    df["py_weekly_run_rate"] = safe_div(df["py_sales"], weeks_elapsed)
    remaining_weeks_safe = max(remaining_weeks, 0)
    df["remaining_year_demand"] = df["weekly_run_rate"] * remaining_weeks_safe

    # ── Monthly Sales Target (from planning or fallback) ──
    if has_planning and "monthly_target_plan" in df.columns:
        fallback = df["quantity_sold"] * (1 + growth_pct) / 12
        df["monthly_sales_target"] = df["monthly_target_plan"].where(
            df["monthly_target_plan"].notna() & (df["monthly_target_plan"] > 0),
            fallback,
        )
    else:
        df["monthly_sales_target"] = df["quantity_sold"] * (1 + growth_pct) / 12

    df["weekly_sales_target"] = df["monthly_sales_target"] * 12 / WEEKS_IN_YEAR
    df["annual_target"] = df["monthly_sales_target"] * 12

    # ── Sales Health vs Target ──
    df["expected_ytd_sales"] = df["weekly_sales_target"] * weeks_elapsed
    df["ytd_variance"] = np.where(
        df["expected_ytd_sales"] > 0,
        df["quantity_sold"] - df["expected_ytd_sales"],
        np.nan,
    )
    df["sales_pace_pct"] = safe_div(
        df["quantity_sold"] - df["expected_ytd_sales"],
        df["expected_ytd_sales"],
    )
    df["sales_health"] = df["sales_pace_pct"].apply(health_label)

    # ── What's needed to hit the annual target ──
    df["remaining_target"] = np.where(
        df["annual_target"] > 0,
        np.maximum(df["annual_target"] - df["quantity_sold"], 0),
        np.nan,
    )
    if remaining_weeks > 0:
        df["required_weekly_sales"] = safe_div(df["remaining_target"], remaining_weeks)
        df["required_monthly_sales"] = df["required_weekly_sales"] * 52 / 12
    else:
        df["required_weekly_sales"] = np.nan
        df["required_monthly_sales"] = np.nan

    df["effort_multiplier"] = safe_div(df["required_weekly_sales"], df["weekly_sales_target"])

    # ── Stock Coverage (based on current sales run rate) ──
    df["monthly_run_rate"] = safe_div(df["quantity_sold"], months_elapsed)
    df["total_stock_coverage_months"] = safe_div(df["total_stock"], df["monthly_run_rate"])
    df["total_stock_coverage_weeks"] = safe_div(df["total_stock"], df["weekly_run_rate"])
    df["twc_coverage_months"] = safe_div(df["twc_stock"], df["monthly_run_rate"])
    df["twc_coverage_weeks"] = safe_div(df["twc_stock"], df["weekly_run_rate"])

    # ── Stock Movement (current year) ──
    df["stock_turnover"] = safe_div(df["qty_out_cy"], df["opening_stock_cy"])

    # ── Reorder flags (based on current-sales coverage) ──
    # Total stock: reorder when coverage is 5 months or less
    # TWC stock: reorder when coverage is 1 month or less
    df["bonded_reorder"] = np.where(
        df["total_stock_coverage_months"].notna() & (df["total_stock_coverage_months"] <= 5),
        "REORDER", "",
    )
    df["twc_reorder"] = np.where(
        df["twc_coverage_months"].notna() & (df["twc_coverage_months"] <= 1),
        "REORDER", "",
    )
    bonded_gap = np.maximum(df["remaining_year_demand"] - df["total_stock"], 0)
    twc_gap = np.maximum(df["remaining_year_demand"] - df["twc_stock"], 0)
    df["bonded_est_reorder_qty"] = np.where(df["bonded_reorder"] == "REORDER", np.ceil(bonded_gap), 0)
    df["twc_est_reorder_qty"] = np.where(df["twc_reorder"] == "REORDER", np.ceil(twc_gap), 0)

    # ── Projected stock-out week ──
    df["weeks_until_stockout"] = np.where(
        df["weekly_run_rate"] > 0,
        df["total_stock"] / df["weekly_run_rate"],
        np.nan,
    )
    df["stockout_risk"] = np.where(
        df["weeks_until_stockout"] < 4, "⚠️ HIGH",
        np.where(df["weeks_until_stockout"] < 8, "⚡ MEDIUM", ""),
    )

    # ── Build final report ──
    report = pd.DataFrame({
        "SKU": df["sku"],
        "Item Name": df.get("item_name", ""),
        "Category": df.get("category", ""),
        "Country": df.get("country_of_origin", ""),
        "Brand": df.get("brand", ""),
        # Sales
        "CY Sales (YTD)": df["quantity_sold"],
        "PY Sales (YTD)": df["py_sales"],
        "YoY Sales %": df["yoy_sales_change"],
        "CY Revenue (YTD)": df["revenue"],
        "Weekly Run Rate": df["weekly_run_rate"],
        # Targets
        "Monthly Target": df["monthly_sales_target"],
        "Weekly Target": df["weekly_sales_target"],
        "Annual Target": df["annual_target"],
        "Expected YTD": df["expected_ytd_sales"],
        "YTD Variance": df["ytd_variance"],
        "Sales Pace %": df["sales_pace_pct"],
        "Sales Health": df["sales_health"],
        "Remaining Target": df["remaining_target"],
        "Req. Weekly Sales": df["required_weekly_sales"],
        "Req. Monthly Sales": df["required_monthly_sales"],
        "Effort Multiplier": df["effort_multiplier"],
        # Stock
        "Total Stock": df["total_stock"],
        "TWC Stock": df["twc_stock"],
        "Bonded Stock": df["bonded_stock"],
        "Stock Coverage (Mo)": df["total_stock_coverage_months"],
        "Stock Coverage (Wk)": df["total_stock_coverage_weeks"],
        "TWC Coverage (Mo)": df["twc_coverage_months"],
        "TWC Coverage (Wk)": df["twc_coverage_weeks"],
        "Wks Until Stockout": df["weeks_until_stockout"],
        "Stockout Risk": df["stockout_risk"],
        "Bonded Reorder": df["bonded_reorder"],
        "TWC Reorder": df["twc_reorder"],
        "Bonded Est. Reorder Qty": df["bonded_est_reorder_qty"],
        "TWC Est. Reorder Qty": df["twc_est_reorder_qty"],
        # Purchases
        "CY Purchases": df["purchases_cy"],
        "PY Purchases": df["purchases_py"],
        # Movement
        "Stock Turnover": df["stock_turnover"],
    })

    st.session_state.report = report

    # ── Health summary for KPIs ──
    has_t = df["monthly_sales_target"] > 0
    st.session_state["health_stats"] = {
        "total_skus": len(df),
        "skus_with_target": int(has_t.sum()),
        "ahead": int((df["sales_health"] == "🟢 Ahead").sum()),
        "on_track": int((df["sales_health"] == "🟡 On Track").sum()),
        "behind": int((df["sales_health"] == "🟠 Behind").sum()),
        "critical": int((df["sales_health"] == "🔴 Critical").sum()),
        "total_cy_sales": int(df["quantity_sold"].sum()),
        "total_py_sales": int(df["py_sales"].sum()),
        "total_stock": int(df["total_stock"].sum()),
        "total_twc_stock": int(df["twc_stock"].sum()),
        "total_bonded": int(df["bonded_stock"].sum()),
        "reorder_bonded": int((df["bonded_reorder"] == "REORDER").sum()),
        "reorder_twc": int((df["twc_reorder"] == "REORDER").sum()),
        "stockout_high": int((df["stockout_risk"] == "⚠️ HIGH").sum()),
        "twc_over_total_count": _twc_over_total,
    }

    # Save report_week to session state for dashboard display
    st.session_state["saved_report_week"] = report_week
    st.session_state["saved_weeks_elapsed"] = weeks_elapsed

# ── Save for Sharing button (Admin only) ──
if is_admin and not st.session_state.get("report", pd.DataFrame()).empty:
    st.markdown("---")
    if st.button("💾 Save Report for Sharing", help="Saves report to data/ folder. Then git push to update the shared dashboard."):
        DATA_DIR.mkdir(exist_ok=True)
        rpt_to_save = st.session_state["report"]
        rpt_to_save.to_csv(SAVED_REPORT_CSV, index=False)
        # Convert numpy types to native Python for JSON serialization
        hs_raw = st.session_state.get("health_stats", {})
        hs_clean = {k: int(v) if isinstance(v, (np.integer,)) else float(v) if isinstance(v, (np.floating,)) else v for k, v in hs_raw.items()}
        meta = {
            "report_week": int(st.session_state.get("saved_report_week", week_number_now())),
            "weeks_elapsed": int(st.session_state.get("saved_weeks_elapsed", weeks_elapsed_in_year())),
            "health_stats": hs_clean,
            "saved_date": str(date.today()),
        }
        with open(SAVED_REPORT_META, "w") as f:
            json.dump(meta, f, indent=2)
        st.success(
            f"✅ Report saved to `data/` folder!\n\n"
            f"Now run in terminal:\n```\ngit add data/\ngit commit -m \"Update weekly report\"\ngit push\n```\n"
            f"Streamlit Cloud will auto-redeploy with the new data."
        )


# =====================================================
# OUTPUT DASHBOARD
# =====================================================
# Determine report_week for display (from admin input or saved metadata)
report_week_display = st.session_state.get("saved_report_week", week_number_now())

rpt = st.session_state.get("report", pd.DataFrame())
if isinstance(rpt, pd.DataFrame) and not rpt.empty:
    hs = st.session_state.get("health_stats", {})

    # ── Top-level KPIs ──
    st.header(f"📋 Weekly Report — Week {report_week_display}")
    st.markdown("---")

    st.subheader("🔑 Key Performance Indicators")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total SKUs", hs.get("total_skus", 0))
    m2.metric("CY Sales (YTD)", f"{hs.get('total_cy_sales', 0):,}")
    m3.metric("PY Sales (YTD)", f"{hs.get('total_py_sales', 0):,}")
    yoy_total = (
        (hs["total_cy_sales"] - hs["total_py_sales"]) / hs["total_py_sales"] * 100
        if hs.get("total_py_sales", 0) > 0 else 0
    )
    m4.metric("YoY Growth", f"{yoy_total:+.1f}%")
    m5.metric("Total Stock (All)", f"{hs.get('total_stock', 0):,}")

    m6, m7, m8, m9, m10 = st.columns(5)
    m6.metric("TWC Stock", f"{hs.get('total_twc_stock', 0):,}")
    m7.metric("Bonded Stock", f"{hs.get('total_bonded', 0):,}")
    m8.metric("Bonded Reorders", hs.get("reorder_bonded", 0))
    m9.metric("TWC Reorders", hs.get("reorder_twc", 0))
    m10.metric("⚠️ Stockout Risk", hs.get("stockout_high", 0))

    # ── Data quality note ──
    _twc_over = hs.get("twc_over_total_count", hs.get("neg_bonded_count", 0))
    if _twc_over > 0:
        st.warning(
            f"**{_twc_over} SKUs** had TWC stock above total stock in the raw files. "
            "Report values were reconciled so TWC never exceeds Total (Bonded = Total - TWC). "
            "Please verify the two stock exports are for the same cutoff period."
        )

    st.markdown("---")

    # ── Sales Health Dashboard ──
    st.subheader("📈 Sales Health vs Targets")
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("🟢 Ahead (≥+10%)", hs.get("ahead", 0))
    h2.metric("🟡 On Track (-10% to +10%)", hs.get("on_track", 0))
    h3.metric("🟠 Behind (-30% to -10%)", hs.get("behind", 0))
    h4.metric("🔴 Critical (<-30%)", hs.get("critical", 0))

    # Health distribution chart
    health_order = ["🟢 Ahead", "🟡 On Track", "🟠 Behind", "🔴 Critical"]
    health_colors = {"🟢 Ahead": "#2ecc71", "🟡 On Track": "#f1c40f", "🟠 Behind": "#e67e22", "🔴 Critical": "#e74c3c"}
    health_counts = rpt["Sales Health"].value_counts()
    hdf = pd.DataFrame({
        "Status": health_order,
        "Count": [health_counts.get(s, 0) for s in health_order],
    })
    hdf = hdf[hdf["Count"] > 0]
    if not hdf.empty:
        fig_health = px.bar(
            hdf, x="Status", y="Count", color="Status",
            color_discrete_map=health_colors,
            title="Sales Health Distribution",
        )
        fig_health.update_layout(showlegend=False, height=300)
        st.plotly_chart(fig_health, use_container_width=True)

    # ── Critical & Behind tables ──
    fmt_health_table = {
        "Sales Pace %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
        "Effort Multiplier": lambda x: "" if pd.isna(x) else f"{x:.1f}x",
        "YTD Variance": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
        "Monthly Target": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
        "Weekly Target": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
        "CY Sales (YTD)": "{:,.0f}",
        "Expected YTD": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
        "Req. Weekly Sales": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
        "Req. Monthly Sales": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
    }
    health_display_cols = [
        "SKU", "Item Name", "CY Sales (YTD)", "Monthly Target", "Weekly Target",
        "Expected YTD", "YTD Variance", "Sales Pace %",
        "Req. Weekly Sales", "Req. Monthly Sales", "Effort Multiplier",
    ]

    with st.expander("🔴 Critical — Immediate Attention Required", expanded=True):
        crit = rpt[rpt["Sales Health"] == "🔴 Critical"].sort_values("Sales Pace %")
        if crit.empty:
            st.success("No critical items! 🎉")
        else:
            st.dataframe(
                crit[health_display_cols].style.format(fmt_health_table),
                use_container_width=True,
            )

    with st.expander("🟠 Behind Target — Needs Attention", expanded=False):
        behind = rpt[rpt["Sales Health"] == "🟠 Behind"].sort_values("Sales Pace %")
        if behind.empty:
            st.info("No items behind target.")
        else:
            st.dataframe(
                behind[health_display_cols].style.format(fmt_health_table),
                use_container_width=True,
            )

    with st.expander("🟡 On Track", expanded=False):
        on_track = rpt[rpt["Sales Health"] == "🟡 On Track"].sort_values("Sales Pace %", ascending=False)
        if on_track.empty:
            st.info("No items on track.")
        else:
            st.dataframe(
                on_track[health_display_cols].style.format(fmt_health_table),
                use_container_width=True,
            )

    with st.expander("🟢 Ahead of Target", expanded=False):
        ahead = rpt[rpt["Sales Health"] == "🟢 Ahead"].sort_values("Sales Pace %", ascending=False)
        if ahead.empty:
            st.info("No items ahead of target.")
        else:
            st.dataframe(
                ahead[health_display_cols].style.format(fmt_health_table),
                use_container_width=True,
            )

    st.markdown("---")

    # ── Stock Health Section ──
    st.subheader("📦 Stock Health & Reorder Alerts")

    with st.expander("⚠️ Stockout Risk — Less than 4 weeks of stock", expanded=True):
        risk = rpt[rpt["Stockout Risk"] == "⚠️ HIGH"].sort_values("Wks Until Stockout")
        if risk.empty:
            st.success("No high stockout risk items.")
        else:
            st.dataframe(
                risk[[
                    "SKU", "Item Name", "Weekly Run Rate", "Total Stock",
                    "TWC Stock", "Bonded Stock", "Wks Until Stockout",
                    "Stock Coverage (Mo)", "Bonded Reorder", "TWC Reorder",
                    "Bonded Est. Reorder Qty", "TWC Est. Reorder Qty",
                ]].style.format({
                    "Weekly Run Rate": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    "Wks Until Stockout": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    "Stock Coverage (Mo)": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    "Bonded Est. Reorder Qty": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
                    "TWC Est. Reorder Qty": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
                }),
                use_container_width=True,
            )

    with st.expander("🔄 Bonded Warehouse Reorders", expanded=False):
        bonded_ro = rpt[rpt["Bonded Reorder"] == "REORDER"].sort_values("Stock Coverage (Mo)")
        if bonded_ro.empty:
            st.success("No bonded reorders needed.")
        else:
            st.dataframe(
                bonded_ro[[
                    "SKU", "Item Name", "Total Stock", "Bonded Stock",
                    "Stock Coverage (Mo)", "Monthly Target", "Bonded Est. Reorder Qty",
                ]].style.format({
                    "Stock Coverage (Mo)": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    "Monthly Target": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    "Bonded Est. Reorder Qty": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
                }),
                use_container_width=True,
            )

    with st.expander("🏪 TWC Reorders (< 1 month coverage)", expanded=False):
        twc_ro = rpt[rpt["TWC Reorder"] == "REORDER"].sort_values("TWC Coverage (Mo)")
        if twc_ro.empty:
            st.success("No TWC reorders needed.")
        else:
            st.dataframe(
                twc_ro[[
                    "SKU", "Item Name", "TWC Stock", "Bonded Stock",
                    "TWC Coverage (Mo)", "Monthly Target", "TWC Est. Reorder Qty",
                ]].style.format({
                    "TWC Coverage (Mo)": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    "Monthly Target": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    "TWC Est. Reorder Qty": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
                }),
                use_container_width=True,
            )

    st.markdown("---")

    # ── Top / Bottom Performers ──
    st.subheader("🏆 Performance Rankings")
    rank_col1, rank_col2 = st.columns(2)

    with rank_col1:
        st.markdown("**Top 15 — Best Sellers (YTD)**")
        top15 = rpt.nlargest(15, "CY Sales (YTD)")[
            ["SKU", "Item Name", "CY Sales (YTD)", "PY Sales (YTD)", "YoY Sales %", "Sales Health"]
        ]
        st.dataframe(
            top15.style.format({
                "YoY Sales %": lambda x: "" if pd.isna(x) else f"{x:+.1%}",
                "CY Sales (YTD)": "{:,.0f}",
                "PY Sales (YTD)": "{:,.0f}",
            }),
            use_container_width=True, hide_index=True,
        )

    with rank_col2:
        st.markdown("**Top 15 — Biggest YoY Decliners**")
        decliners = rpt[rpt["PY Sales (YTD)"] > 0].nsmallest(15, "YoY Sales %")[
            ["SKU", "Item Name", "CY Sales (YTD)", "PY Sales (YTD)", "YoY Sales %", "Sales Health"]
        ]
        st.dataframe(
            decliners.style.format({
                "YoY Sales %": lambda x: "" if pd.isna(x) else f"{x:+.1%}",
                "CY Sales (YTD)": "{:,.0f}",
                "PY Sales (YTD)": "{:,.0f}",
            }),
            use_container_width=True, hide_index=True,
        )

    st.markdown("---")

    # ── Category Breakdown ──
    if "Category" in rpt.columns and rpt["Category"].notna().any():
        st.subheader("📊 Sales by Category")
        cat_df = rpt.groupby("Category", as_index=False).agg({
            "CY Sales (YTD)": "sum",
            "PY Sales (YTD)": "sum",
            "CY Revenue (YTD)": "sum",
            "Total Stock": "sum",
        }).sort_values("CY Sales (YTD)", ascending=False)
        cat_df["YoY %"] = safe_div(
            cat_df["CY Sales (YTD)"] - cat_df["PY Sales (YTD)"],
            cat_df["PY Sales (YTD)"],
        )

        fig_cat = px.bar(
            cat_df.head(15), x="Category", y=["CY Sales (YTD)", "PY Sales (YTD)"],
            barmode="group", title="Top 15 Categories — CY vs PY Sales",
        )
        fig_cat.update_layout(height=400)
        st.plotly_chart(fig_cat, use_container_width=True)

        st.dataframe(
            cat_df.style.format({
                "CY Sales (YTD)": "{:,.0f}",
                "PY Sales (YTD)": "{:,.0f}",
                "CY Revenue (YTD)": "{:,.0f}",
                "Total Stock": "{:,.0f}",
                "YoY %": lambda x: "" if pd.isna(x) else f"{x:+.1%}",
            }),
            use_container_width=True, hide_index=True,
        )
        st.markdown("---")

    # ── Sales Pace Scatter ──
    st.subheader("🎯 Sales Pace vs Stock Coverage")
    scatter_df = rpt[rpt["Sales Pace %"].notna() & rpt["Stock Coverage (Mo)"].notna()].copy()
    if not scatter_df.empty:
        fig_scatter = px.scatter(
            scatter_df,
            x="Sales Pace %",
            y="Stock Coverage (Mo)",
            color="Sales Health",
            hover_data=["SKU", "Item Name", "CY Sales (YTD)", "Monthly Target"],
            color_discrete_map={
                "🟢 Ahead": "#2ecc71", "🟡 On Track": "#f1c40f",
                "🟠 Behind": "#e67e22", "🔴 Critical": "#e74c3c",
            },
            title="Each dot = 1 SKU: are you selling fast enough AND have enough stock?",
        )
        fig_scatter.add_vline(x=0.0, line_dash="dash", line_color="gray", annotation_text="On Target (0%)")
        fig_scatter.add_hline(y=5.0, line_dash="dash", line_color="gray", annotation_text="5-Mo Coverage")
        fig_scatter.update_layout(height=500)
        st.plotly_chart(fig_scatter, use_container_width=True)

    st.markdown("---")

    # ── Full Report Table ──
    st.subheader("📋 Full Report Data")
    fmt_full = {
        "YoY Sales %": lambda x: "" if pd.isna(x) else f"{x:+.1%}",
        "Sales Pace %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
        "Effort Multiplier": lambda x: "" if pd.isna(x) else f"{x:.2f}x",
        "Stock Coverage (Mo)": lambda x: "" if pd.isna(x) else f"{x:.1f}",
        "Stock Coverage (Wk)": lambda x: "" if pd.isna(x) else f"{x:.1f}",
        "TWC Coverage (Mo)": lambda x: "" if pd.isna(x) else f"{x:.1f}",
        "TWC Coverage (Wk)": lambda x: "" if pd.isna(x) else f"{x:.1f}",
        "Wks Until Stockout": lambda x: "" if pd.isna(x) else f"{x:.1f}",
        "Stock Turnover": lambda x: "" if pd.isna(x) else f"{x:.2f}",
        "Weekly Run Rate": lambda x: "" if pd.isna(x) else f"{x:.1f}",
        "Bonded Est. Reorder Qty": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
        "TWC Est. Reorder Qty": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
    }
    st.dataframe(rpt.style.format(fmt_full), use_container_width=True)

    # ── Downloads ──
    st.subheader("⬇️ Download Report")
    dl1, dl2, dl3 = st.columns(3)
    filename_base = f"weekly_report_wk{report_week_display}_{CURRENT_YEAR}"
    with dl1:
        st.download_button(
            "📄 CSV", rpt.to_csv(index=False),
            f"{filename_base}.csv", "text/csv",
        )
    with dl2:
        st.download_button(
            "📊 Excel", df_to_excel(rpt),
            f"{filename_base}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with dl3:
        st.download_button(
            "📑 PDF", df_to_pdf(rpt),
            f"{filename_base}.pdf", "application/pdf",
        )
else:
    if not is_admin:
        st.info("📊 No report data available yet. The report will appear here once it has been published.")
