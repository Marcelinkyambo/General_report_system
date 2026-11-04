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
from weekly_pipeline import build_weekly_report
from planning import prepare_planning, brand_history, brand_key, load_growth, save_growth
from automation_ui import render_zoho_source, render_brand_editor, render_pull_progress
from automate_report import elapsed_weeks as elapsed_weeks_at_cutoff, load_year_plan, local_today
from report_store import attention_css, load_report, publish_report, report_excel_bytes, weekly_report_view, purchase_order_audit_view, REPORT_COLUMNS
from report_logic import (
    apply_reorder_flags,
    apply_stock_coverage,
    derive_stock_components,
    find_closing_stock_quantity_column,
)

# =====================================================
# CONFIG
# =====================================================
st.set_page_config(page_title="Weekly Sales & Stock Report", layout="wide")

CURRENT_YEAR = local_today().year
WEEKS_IN_YEAR = 52
MONTHS_IN_YEAR = 12

# Paths for saved report data (committed to repo for sharing)
DATA_DIR = Path(__file__).parent / "data"
SAVED_REPORT_CSV = DATA_DIR / "report.csv"
SAVED_REPORT_META = DATA_DIR / "report_meta.json"
BRAND_GROWTH_FILE = DATA_DIR / "brand_growth.json"

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
        last_err = None
        for enc in ["utf-8-sig", "utf-8", "cp1252", "latin1"]:
            try:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, encoding=enc)
                break
            except UnicodeDecodeError as e:
                last_err = e
        else:
            raise ValueError(f"Could not decode CSV file {uploaded_file.name}: {last_err}")
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
    return report_excel_bytes(df, st.session_state.get("report_po_audit"))

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
    try:
        rpt, meta, audit = load_report(DATA_DIR)
        st.session_state["report_po_audit"] = audit
        st.session_state["report_source_metadata"] = meta
        return rpt, meta
    except (ValueError, OSError):
        return pd.DataFrame(), {}

# Initialize report from saved data if available
_saved_source = DATA_DIR / "current_report.json"
if not _saved_source.exists():
    _saved_source = SAVED_REPORT_CSV
_saved_revision = _saved_source.stat().st_mtime_ns if _saved_source.exists() else 0
if "report" not in st.session_state or st.session_state.get("loaded_report_revision") != _saved_revision:
    saved_rpt, saved_meta = load_saved_report()
    if not saved_rpt.empty:
        st.session_state["report"] = saved_rpt
        st.session_state["health_stats"] = saved_meta.get("health_stats", {})
        st.session_state["saved_report_week"] = saved_meta.get("report_week", 0)
        st.session_state["saved_weeks_elapsed"] = saved_meta.get("weeks_elapsed", 0)
    st.session_state["loaded_report_revision"] = _saved_revision

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
    "bonded_stock_file": pd.DataFrame(),
    "in_transit": pd.DataFrame(),
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
purch_cy_file = purch_py_file = twc_file = bonded_stock_file = in_transit_file = item_details_file = planning_file = None

# =====================================================
# ADMIN: SIDEBAR — FILE UPLOADS
# =====================================================
source_mode = "File uploads"
if is_admin:
    source_mode = st.sidebar.radio("Report data source", ["File uploads", "Zoho Books"],
                                   disabled=bool(st.session_state.get("zoho_job_id")))
    if st.session_state.get("input_source_mode", source_mode) != source_mode:
        for key in _state_keys:
            st.session_state[key] = pd.DataFrame()
        for key in ["source_metadata", "purchase_orders", "po_audit"]:
            st.session_state.pop(key, None)
    st.session_state["input_source_mode"] = source_mode
    if source_mode == "Zoho Books":
        render_zoho_source(st)
    with st.sidebar:
        render_pull_progress()

if is_admin and source_mode == "File uploads":
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

    # --- Bonded Stock ---
    st.sidebar.subheader("Bonded Stock")
    bonded_stock_file = st.sidebar.file_uploader(
        "Bonded Stock Summary",
        type=["csv", "xlsx"],
        key="up_bonded_stock",
        help="Stock summary for bonded warehouse. Closing stock used as Bonded Stock quantity.",
    )

    # --- In Transit ---
    st.sidebar.subheader("In Transit")
    in_transit_file = st.sidebar.file_uploader(
        "In-Transit Stock Report",
        type=["csv", "xlsx"],
        key="up_in_transit",
        help="Stock summary for in-transit items. Closing stock used as In Transit quantity.",
    )

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
    planning_file = st.sidebar.file_uploader("Annual stock planning benchmark", type=["csv", "xlsx"], key="up_plan")
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
    close_col = find_closing_stock_quantity_column(raw.columns)
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
    close_col = find_closing_stock_quantity_column(raw.columns)
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


def _process_bonded_stock(uploaded):
    """Read Bonded Stock Summary → closing stock by SKU prefix."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    close_col = find_closing_stock_quantity_column(raw.columns)
    item_col = find_col(raw, ["item", "name"]) or find_col(raw, ["item_name"])
    cat_col = find_col(raw, ["category_name"])

    if not sku_col or not close_col:
        st.sidebar.error(f"Cannot detect SKU or Closing Stock column in {uploaded.name}")
        return

    raw["_sku"] = raw[sku_col].apply(normalize_sku)
    raw["_sku_grp"] = raw["_sku"].apply(sku_to_group_key)
    raw = raw[raw["_sku_grp"].str.replace("0", "") != ""].copy()
    raw[close_col] = pd.to_numeric(raw[close_col].replace("--", 0), errors="coerce").fillna(0)

    agg = {close_col: "sum", "_sku": first_non_empty}
    if item_col:
        raw["_item"] = raw[item_col]
        agg["_item"] = first_non_empty
    if cat_col:
        raw["_cat"] = raw[cat_col]
        agg["_cat"] = first_non_empty

    grouped = raw.groupby("_sku_grp", as_index=False).agg(agg)
    grouped.rename(columns={
        "_sku_grp": "sku_key", close_col: "bonded_stock_uploaded", "_sku": "sku",
        "_item": "item_name", "_cat": "category",
    }, inplace=True)
    st.session_state["bonded_stock_file"] = grouped


def _process_in_transit(uploaded):
    """Read In-Transit Stock report → in-transit quantity by SKU prefix."""
    if uploaded is None:
        return
    raw = read_uploaded(uploaded)
    sku_col = find_col(raw, ["sku"])
    qty_col = find_closing_stock_quantity_column(raw.columns)
    item_col = find_col(raw, ["item", "name"]) or find_col(raw, ["item_name"])
    cat_col = find_col(raw, ["category_name"])

    if not sku_col or not qty_col:
        st.sidebar.error(f"Cannot detect SKU or Closing Stock column in {uploaded.name}")
        return

    raw["_sku"] = raw[sku_col].apply(normalize_sku)
    raw["_sku_grp"] = raw["_sku"].apply(sku_to_group_key)
    raw = raw[raw["_sku_grp"].str.replace("0", "") != ""].copy()
    raw["_qty_in"] = pd.to_numeric(raw[qty_col], errors="coerce").fillna(0)

    agg = {"_sku": first_non_empty, "_qty_in": "sum"}
    if item_col:
        raw["_item"] = raw[item_col]
        agg["_item"] = first_non_empty
    if cat_col:
        raw["_cat"] = raw[cat_col]
        agg["_cat"] = first_non_empty

    grouped = raw.groupby("_sku_grp", as_index=False).agg(agg)
    grouped.rename(columns={
        "_sku_grp": "sku_key", "_sku": "sku",
        "_qty_in": "in_transit_qty",
        "_item": "item_name", "_cat": "category",
    }, inplace=True)
    # Only keep rows with actual in-transit quantities
    grouped = grouped[grouped["in_transit_qty"] > 0].copy()
    st.session_state["in_transit"] = grouped


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
    if uploaded is not None:
        try:
            st.session_state["planning"] = prepare_planning(read_uploaded(uploaded))
            st.session_state.pop("planning_error", None)
        except ValueError as exc:
            st.session_state["planning_error"] = str(exc)
            st.sidebar.error(str(exc))


# Run all processors (upload vars are None when not in admin mode — processors handle gracefully)
_process_sales(sales_cy_file, "sales_cy")
_process_sales(sales_py_file, "sales_py")
_process_stock(stock_cy_file, "stock_cy")
_process_stock(stock_py_file, "stock_py")
_process_purchases(purch_cy_file, "purchases_cy")
_process_purchases(purch_py_file, "purchases_py")
_process_twc(twc_file)
_process_bonded_stock(bonded_stock_file)
_process_in_transit(in_transit_file)
_process_item_details(item_details_file)
_process_planning(planning_file)


# =====================================================
# ADMIN: REPORT PARAMETERS & GENERATE
# =====================================================
if is_admin:

    st.header("⚙️ Report Parameters")

    source_meta = st.session_state.get("source_metadata", {})
    sales_cutoff = date.fromisoformat(source_meta["sales_as_of"]) if source_meta.get("sales_as_of") else None
    report_year = st.number_input("Report year", min_value=2000, max_value=2100,
                                  value=sales_cutoff.year if sales_cutoff else CURRENT_YEAR,
                                  disabled=bool(sales_cutoff))
    if planning_file is None and (st.session_state.planning.empty or st.session_state.get("planning_year_loaded") != report_year):
        try:
            st.session_state["planning"] = load_year_plan(report_year)
            st.session_state["planning_year_loaded"] = report_year
        except ValueError as exc:
            st.error(str(exc))

    p1, p2, p3 = st.columns(3)
    with p1:
        report_week = st.number_input(
            "Report Week #", min_value=1, max_value=53,
            value=sales_cutoff.isocalendar().week if sales_cutoff else week_number_now(),
            disabled=bool(sales_cutoff),
            help="ISO week number of this report",
        )
    with p2:
        weeks_elapsed = st.number_input(
            "Weeks Elapsed (YTD period)", min_value=0.01, max_value=52.0,
            value=elapsed_weeks_at_cutoff(sales_cutoff) if sales_cutoff else float(weeks_elapsed_in_year()),
            disabled=bool(sales_cutoff), format="%.3f",
            help="Number of completed weeks in the YTD data",
        )
    with p3:
        growth_pct = st.number_input(
            "Default Growth Target (%)", min_value=-100.0, max_value=1000.0,
            value=20.0,
        ) / 100

    months_elapsed = round(weeks_elapsed * 12 / 52, 2)
    remaining_weeks = WEEKS_IN_YEAR - weeks_elapsed
    remaining_months = round(remaining_weeks * 12 / 52, 2)

    st.caption(
        f"📅 Week {report_week} | {weeks_elapsed} weeks elapsed ≈ {months_elapsed:.1f} months | "
        f"{remaining_weeks} weeks / {remaining_months:.1f} months remaining"
    )
    if source_meta:
        st.caption(f"Sales through {source_meta['sales_as_of']}. Live stock and open POs dated {source_meta['stock_as_of']}.")
    render_brand_editor(st, report_year, st.session_state)


# =====================================================
# GENERATE REPORT (Admin only)
# =====================================================
_generate_clicked = is_admin and st.button("🚀 Generate Weekly Report", type="primary",
                                          disabled=bool(st.session_state.get("zoho_job_id")))
_generate_from_zoho = is_admin and st.session_state.pop("zoho_generate_pending", False)
if is_admin and (_generate_clicked or _generate_from_zoho):
    try:
        if planning_file is not None and st.session_state.get("planning_error"):
            raise ValueError(st.session_state["planning_error"])
        report, health_stats = build_weekly_report(
            {key: st.session_state[key] for key in _state_keys} | {
                "purchase_orders": st.session_state.get("purchase_orders", pd.DataFrame())
            },
            weeks_elapsed=weeks_elapsed, growth_pct=growth_pct,
            brand_rates=load_growth(BRAND_GROWTH_FILE, report_year),
        )
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    st.session_state["report"] = report
    st.session_state["health_stats"] = health_stats
    st.session_state["saved_report_week"] = report_week
    st.session_state["saved_weeks_elapsed"] = weeks_elapsed
    st.session_state["report_source_metadata"] = {
        **st.session_state.get("source_metadata", {}), "report_year": int(report_year)
    }
    st.session_state["report_po_audit"] = st.session_state.get("po_audit", pd.DataFrame()).copy()
    if source_mode == "Zoho Books":
        st.session_state["report"]["Stock Turnover"] = np.nan
    if _generate_from_zoho:
        st.success("Weekly report ready. Zoho will be checked again when you click Pull from Zoho Books.")

# ── Save for Sharing button (Admin only) ──
if is_admin and not st.session_state.get("report", pd.DataFrame()).empty:
    st.markdown("---")
    if st.button("💾 Save Report for Sharing", help="Publishes the completed report to the dashboard on this installation."):
        DATA_DIR.mkdir(exist_ok=True)
        rpt_to_save = st.session_state["report"]
        # Convert numpy types to native Python for JSON serialization
        hs_raw = st.session_state.get("health_stats", {})
        hs_clean = {k: int(v) if isinstance(v, (np.integer,)) else float(v) if isinstance(v, (np.floating,)) else v for k, v in hs_raw.items()}
        meta = {
            **st.session_state.get("report_source_metadata", {}),
            "report_week": int(st.session_state.get("saved_report_week", week_number_now())),
            "weeks_elapsed": float(st.session_state.get("saved_weeks_elapsed", weeks_elapsed_in_year())),
            "health_stats": hs_clean,
            "saved_date": str(local_today()),
        }
        publish_report(DATA_DIR, rpt_to_save, meta, st.session_state.get("report_po_audit"), include_excel=True)
        st.success("Report saved. The dashboard on this installation will load the completed snapshot.")


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
    _report_meta = st.session_state.get("report_source_metadata", {})
    if _report_meta.get("sales_as_of"):
        st.caption(f"Zoho Books sales through {_report_meta['sales_as_of']}. Stock dated {_report_meta['stock_as_of']}; open POs dated {_report_meta.get('po_as_of', _report_meta['stock_as_of'])}.")
        if _report_meta.get("stock_snapshot_at"):
            st.caption(f"Stock snapshot: {_report_meta['stock_snapshot_at']}. Books requests for this pull: {_report_meta.get('api_calls', 0):,}.")
    if _report_meta.get("excluded_non_sku_items"):
        with st.expander("Non-inventory items excluded from this SKU report"):
            st.write("Non-inventory items without SKUs, including loyalty points and service charges, are listed here and excluded from product sales and stock planning.")
            st.dataframe(pd.DataFrame(_report_meta["excluded_non_sku_items"]), hide_index=True)
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
        twc_ro = rpt[rpt["TWC Reorder"] == "CLEAR"].sort_values("TWC Coverage (Mo)")
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
        "Growth Target %": lambda x: "" if pd.isna(x) else f"{x:.1%}",
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
    sales_tab, po_tab = st.tabs(["Sales and stock", "Purchase order audit"])
    full_data = weekly_report_view(rpt)
    with sales_tab:
        st.caption("Ordered Qty (PO) shows bottles on purchase orders that are not yet billed, minus bottles of that batch billed on another PO and sold since. Only billed bottles not yet received reduce the supplier reorder suggestion; received stock is counted in warehouse balances.")
        display_formats = {REPORT_COLUMNS.get(key, key): value for key, value in fmt_full.items()}
        for column in full_data.select_dtypes(include="number"):
            display_formats.setdefault(column, "{:,.1f}" if column in {"Avg Weekly Sales", "Annual Target", "Expected YTD Sales", "Target Base Sales"} else "{:,.0f}")
        st.caption("Colours match the Excel download; its Colour Key sheet explains each one.")
        st.dataframe(full_data.style.format(display_formats).apply(lambda _: attention_css(full_data), axis=None),
                     use_container_width=True, hide_index=True)
    with po_tab:
        full_audit = purchase_order_audit_view(rpt, st.session_state.get("report_po_audit"))
        if full_audit.empty:
            st.info("No purchase-order audit lines are saved with this report.")
        else:
            st.caption("Notes show billed and received quantities and the balance applied to reordering. Ordered Qty retains the original PO line quantity, as in the supplied audit.")
            st.dataframe(full_audit, hide_index=True, use_container_width=True)

    # ── Downloads ──
    st.subheader("⬇️ Download Report")
    dl1, dl2, dl3 = st.columns(3)
    filename_base = f"weekly_report_wk{report_week_display}_{_report_meta.get('report_year', CURRENT_YEAR)}"
    with dl1:
        st.download_button(
            "📄 Sales and stock CSV", full_data.to_csv(index=False),
            f"{filename_base}.csv", "text/csv",
        )
    with dl2:
        st.download_button(
            "📊 Full report Excel (includes PO audit)", df_to_excel(rpt),
            f"{filename_base}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with dl3:
        st.download_button(
            "📑 PDF", df_to_pdf(full_data),
            f"{filename_base}.pdf", "application/pdf",
        )
else:
    if not is_admin:
        st.info("📊 No report data available yet. The report will appear here once it has been published.")


@st.fragment(run_every=60)
def _watch_published_report():
    if st.session_state.get("zoho_job_id"):
        return
    source = DATA_DIR / "current_report.json"
    if source.exists() and source.stat().st_mtime_ns != st.session_state.get("loaded_report_revision"):
        st.rerun()


_watch_published_report()
