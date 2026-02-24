import streamlit as st
import pandas as pd
import numpy as np
import re
from datetime import date
from io import BytesIO
from fpdf import FPDF
import unicodedata

# =====================================================
# CONFIG
# =====================================================
st.set_page_config(page_title="Sales & Stock Planning System", layout="wide")
st.title("📊 Sales & Stock Planning Report")

# =====================================================
# HELPERS
# =====================================================
def normalize_columns(df):
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
    if pd.isna(sku):
        return ""
    s = str(sku).strip()
    # Handle numeric coercion like "111-001-20.0"
    if s.endswith(".0"):
        s = s[:-2]
    parts = s.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else s

def read_uploaded_file(uploaded_file):
    if uploaded_file.name.lower().endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)
    return normalize_columns(df)

def find_column(df, must_have=None, must_not_have=None):
    must_have = must_have or []
    must_not_have = must_not_have or []
    for col in df.columns:
        if all(k in col for k in must_have) and not any(k in col for k in must_not_have):
            return col
    return None

def sanitize_for_pdf(v):
    if isinstance(v, str):
        return (
            v.replace("–", "-")
             .replace("—", "-")
             .replace("’", "'")
             .replace("“", '"')
             .replace("”", '"')
        )
    return v

def sanitize_for_pdf_safe(v):
    # Start with the existing sanitizer (handles common quote/dash issues),
    # then guarantee latin-1 output so FPDF doesn't crash on export.
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return ""

    s = sanitize_for_pdf(str(v))
    s = str(s).replace("…", "...")
    s = unicodedata.normalize("NFKC", s)
    return s.encode("latin-1", errors="replace").decode("latin-1")

def df_to_excel_bytes(df):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    return buf.getvalue()

def df_to_pdf_bytes(df):
    pdf = FPDF(orientation="L")
    pdf.add_page()
    pdf.set_font("Arial", size=7)

    col_widths = [max(28, len(c) * 3) for c in df.columns]
    row_h = 6

    for i, c in enumerate(df.columns):
        pdf.cell(col_widths[i], row_h, sanitize_for_pdf_safe(c), border=1)
    pdf.ln(row_h)

    for _, r in df.iterrows():
        for i, v in enumerate(r):
            pdf.cell(col_widths[i], row_h, sanitize_for_pdf_safe(v), border=1)
        pdf.ln(row_h)

    return pdf.output(dest="S").encode("latin-1")

# =====================================================
# SESSION STATE
# =====================================================
for k in ["ps_df", "twc_df", "final_df", "planning_df"]:
    if k not in st.session_state:
        st.session_state[k] = pd.DataFrame()
for k in ["ps_mapping", "twc_mapping"]:
    if k not in st.session_state:
        st.session_state[k] = {}

# =====================================================
# STEP 1 — P&S FORECAST
# =====================================================
st.header("1️⃣ Upload P&S Forecast (YTD)")

ps_file = st.file_uploader("Upload P&S Forecast (CSV / XLSX)", type=["csv", "xlsx"])

if ps_file:
    df = read_uploaded_file(ps_file)

    sku = find_column(df, ["sku"]) or find_column(df, ["sku_group"])
    item = find_column(df, ["item", "name"]) or find_column(df, ["item_name"])
    origin = find_column(df, ["country"]) or find_column(df, ["origin"])   # <-- ADDED
    cy = find_column(df, ["quantity_sold"]) or find_column(df, ["sales"], ["previous"]) or find_column(df, ["sales"])
    py = find_column(df, ["sales", "previous"]) or find_column(df, ["previous_sales"])

    # Total Stock is the *closing balance* from the P&S Forecast (YTD) upload.
    # Prefer quantity columns and explicitly avoid any "*amount" money columns.
    closing_balance_col = (
        find_column(df, ["closing", "balance"], ["amount"])
        or find_column(df, ["closing_balance"], ["amount"])
        or find_column(df, ["closing_stock"], ["amount"])
        or find_column(df, ["closing"], ["amount"])
        or find_column(df, ["closing_balance"])
    )

    # Optional stock fields (only used for diagnostics/fallbacks)
    total_stock_col = find_column(df, ["total_stock"]) or find_column(df, ["total", "stock"])
    bonded_stock_col = find_column(df, ["bonded", "stock"]) or find_column(df, ["bonded_stock"])
    stock_at_hand_col = find_column(df, ["stock_at_hand"]) or find_column(df, ["stock", "hand"])
    closing_stock_col = find_column(df, ["closing_stock"], ["amount"]) or find_column(df, ["closing"], ["amount"])

    if not sku:
        st.error("Couldn't find a SKU column in the P&S upload. Make sure the file has a 'SKU' column.")
        st.stop()

    if not closing_balance_col:
        st.error(
            "Couldn't find the 'Closing Balance' column in the P&S upload (used as Total Stock). "
            "Make sure the file includes a Closing Balance / Closing Stock quantity column (not an amount column)."
        )
        st.stop()

    # Total Stock must come from Closing Balance per requirements.
    stock = closing_balance_col

    if not py:
        df["_py"] = 0
        py = "_py"

    if not origin:
        df["_origin"] = ""
        origin = "_origin"

    # Prepare stock fields for aggregation.
    if stock:
        df["_total_stock"] = pd.to_numeric(df[stock], errors="coerce").fillna(0)
    else:
        df["_total_stock"] = 0

    if bonded_stock_col:
        df["_bonded_stock_input"] = pd.to_numeric(df[bonded_stock_col], errors="coerce").fillna(0)
    else:
        df["_bonded_stock_input"] = 0

    if stock_at_hand_col:
        df["_stock_at_hand_ps"] = pd.to_numeric(df[stock_at_hand_col], errors="coerce").fillna(0)
    else:
        df["_stock_at_hand_ps"] = 0

    df["sku_group"] = df[sku].apply(lambda x: re.sub(r"[^0-9]", "", str(x))[:6].zfill(6))
    df = df[df["sku_group"].str.replace("0", "") != ""].copy()

    df["sku"] = df["sku_group"] if sku == "sku_group" else df[sku].apply(normalize_sku)

    # Build aggregation dict with only valid columns
    agg_dict = {
        item: "first",
        origin: "first",
        cy: "sum",
        py: "sum",
        "_total_stock": "sum",
        "_bonded_stock_input": "sum",
        "_stock_at_hand_ps": "sum",
    }

    st.session_state.ps_df = (
        df.groupby("sku_group", as_index=False)
        .agg(agg_dict)
        .rename(columns={
            "sku_group": "sku",
            item: "item_name",
            origin: "country_of_origin",
            cy: "current_year_sales",
            py: "previous_year_sales",
            "_total_stock": "total_stock",
            "_bonded_stock_input": "bonded_stock_input",
            "_stock_at_hand_ps": "stock_at_hand_ps",
        })
    )

    st.session_state.ps_mapping = {
        "sku": sku,
        "item_name": item,
        "country_of_origin": origin,
        "current_year_sales": cy,
        "previous_year_sales": py,
        "closing_balance_col_used_as_total_stock": closing_balance_col,
        "total_stock_source_actual": stock,
        "total_stock_col": total_stock_col,
        "bonded_stock_col": bonded_stock_col,
        "stock_at_hand_col": stock_at_hand_col,
        "closing_stock_col": closing_stock_col,
    }

    with st.expander("Detected P&S columns", expanded=False):
        st.json(st.session_state.ps_mapping)

# =====================================================
# STEP 2 — TWC STOCK
# =====================================================
st.header("2️⃣ Upload TWC Stock Summary")

twc_file = st.file_uploader("Upload TWC Stock (CSV / XLSX)", type=["csv", "xlsx"])

if twc_file:
    df = read_uploaded_file(twc_file)

    # Debug: show all columns so we can see what's available
    st.caption(f"TWC columns after normalization: {list(df.columns)}")

    sku = find_column(df, ["sku"])
    stock = (
        find_column(df, ["closing_stock"], ["amount"])
        or find_column(df, ["closing_balance"], ["amount"])
        or find_column(df, ["closing"], ["amount"])
        or find_column(df, ["stock_at_hand"])
        or find_column(df, ["stock", "hand"])
        or find_column(df, ["stock"], ["amount"])
    )

    if not sku:
        st.error("Couldn't find a SKU column in the TWC upload.")
        st.stop()
    if not stock:
        st.error(f"Couldn't find a Closing Stock column in the TWC upload. Available columns: {list(df.columns)}")
        st.stop()

    st.info(f"TWC: Using **{stock}** as stock column (from original '{stock}')")

    df["sku_group"] = df[sku].apply(lambda x: re.sub(r"[^0-9]", "", str(x))[:6].zfill(6))
    df = df[df["sku_group"].str.replace("0", "") != ""].copy()

    # Ensure stock column is numeric
    df[stock] = pd.to_numeric(df[stock], errors="coerce").fillna(0)

    twc_result = (
        df.groupby("sku_group", as_index=False)[stock]
        .sum()
        .rename(columns={stock: "stock_at_hand", "sku_group": "sku"})
    )

    st.info(f"TWC grouped: {len(twc_result)} SKU groups, "
            f"{(twc_result['stock_at_hand'] > 0).sum()} with stock > 0, "
            f"total stock = {twc_result['stock_at_hand'].sum()}")

    st.session_state.twc_df = twc_result

    st.session_state.twc_mapping = {"sku": sku, "stock_at_hand": stock}
    with st.expander("Detected TWC columns", expanded=False):
        st.json(st.session_state.twc_mapping)
    with st.expander("TWC data sample (top 10 with stock)", expanded=False):
        st.dataframe(twc_result[twc_result["stock_at_hand"] > 0].head(10))

# =====================================================
# STEP 3 — STOCK PLANNING FILE (Monthly Sales Targets)
# =====================================================
st.header("3️⃣ Upload Stock Planning File")

planning_file = st.file_uploader(
    "Upload Stock Planning 2026 (XLSX / CSV) — provides monthly sales targets",
    type=["csv", "xlsx"],
    key="planning_upload",
)

if planning_file:
    planning_raw = read_uploaded_file(planning_file)

    # Locate the SKU and monthly-target columns
    plan_sku = find_column(planning_raw, ["sku"])
    plan_target = (
        find_column(planning_raw, ["monthly", "sales", "target"])
        or find_column(planning_raw, ["monthly_sales_target"])
        or find_column(planning_raw, ["monthly", "target"])
    )

    if not plan_sku:
        st.error("Couldn't find a SKU column in the Stock Planning file.")
        st.stop()
    if not plan_target:
        st.error(
            f"Couldn't find a Monthly Sales Target column. Available columns: {list(planning_raw.columns)}"
        )
        st.stop()

    # Normalize SKU to 6-digit prefix (same format as P&S / TWC)
    planning_raw["sku"] = planning_raw[plan_sku].apply(normalize_sku)
    planning_raw["monthly_sales_target_plan"] = pd.to_numeric(
        planning_raw[plan_target], errors="coerce"
    ).fillna(0)

    st.session_state.planning_df = planning_raw[["sku", "monthly_sales_target_plan"]].copy()

    st.info(
        f"Loaded {len(st.session_state.planning_df)} SKU targets from planning file. "
        f"Column used: **{plan_target}**"
    )
    with st.expander("Planning targets sample (top 10)", expanded=False):
        st.dataframe(st.session_state.planning_df.head(10))

# =====================================================
# STEP 4 — FINAL REPORT
# =====================================================
st.header("4️⃣ Generate Sales & Stock Planning Report")

col_g, col_m = st.columns(2)
with col_g:
    growth = st.number_input("Growth Target (%)", 0.0, 200.0, 20.0) / 100
with col_m:
    # Default months elapsed: completed months so far in 2026
    _today = date.today()
    _default_months = max(1.0, round(_today.month - 1 + _today.day / 30, 1))
    months_elapsed = st.number_input(
        "Months elapsed (YTD period)",
        min_value=0.5, max_value=12.0,
        value=_default_months, step=0.5,
        help="Number of months covered by the YTD sales data (e.g. 1.5 = mid-Feb)",
    )

if st.button("Generate Report"):
    # Ensure sku columns are strings for merging
    st.session_state.ps_df["sku"] = st.session_state.ps_df["sku"].astype(str)
    st.session_state.twc_df["sku"] = st.session_state.twc_df["sku"].astype(str)

    # Debug: show merge inputs
    with st.expander("🔍 Debug: Merge inputs", expanded=True):
        st.write(f"P&S rows: {len(st.session_state.ps_df)}, TWC rows: {len(st.session_state.twc_df)}")
        st.write(f"P&S SKU sample: {st.session_state.ps_df['sku'].head(5).tolist()}")
        st.write(f"TWC SKU sample: {st.session_state.twc_df['sku'].head(5).tolist()}")
        st.write(f"TWC columns: {list(st.session_state.twc_df.columns)}")
        if "stock_at_hand" in st.session_state.twc_df.columns:
            st.write(f"TWC stock_at_hand sum: {st.session_state.twc_df['stock_at_hand'].sum()}")
        common = set(st.session_state.ps_df["sku"]) & set(st.session_state.twc_df["sku"])
        st.write(f"Matching SKUs: {len(common)} out of {len(st.session_state.ps_df)} P&S / {len(st.session_state.twc_df)} TWC")

    df = st.session_state.ps_df.merge(
        st.session_state.twc_df, on="sku", how="left"
    )

    for c in [
        "current_year_sales",
        "previous_year_sales",
        "total_stock",
        "bonded_stock_input",
        "stock_at_hand_ps",
        "stock_at_hand",
    ]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Prefer TWC stock at hand when provided; otherwise fall back to the P&S file (if present).
    if "stock_at_hand" not in df.columns:
        df["stock_at_hand"] = np.nan
    df["stock_at_hand"] = df["stock_at_hand"].where(df["stock_at_hand"].notna(), df.get("stock_at_hand_ps", 0))
    df["stock_at_hand"] = df["stock_at_hand"].fillna(0)

    df["total_stock"] = df.get("total_stock", 0).fillna(0)
    df["bonded_stock_input"] = df.get("bonded_stock_input", 0).fillna(0)

    df["has_demand"] = (
        (df["current_year_sales"] > 0) |
        (df["previous_year_sales"] > 0)
    )

    df["sales_diff"] = np.where(
        df["previous_year_sales"] > 0,
        (df["current_year_sales"] - df["previous_year_sales"])
        / df["previous_year_sales"],
        np.nan
    )

    df["next_year_budget"] = np.where(
        df["has_demand"],
        df["current_year_sales"] * (1 + growth),
        np.nan
    )

    # ── Monthly Sales Target: prefer planning file, fall back to budget / 12 ──
    if not st.session_state.planning_df.empty:
        df = df.merge(
            st.session_state.planning_df, on="sku", how="left"
        )
        # Use planning target when available; otherwise fall back to computed value
        computed = df["next_year_budget"] / 12
        df["monthly_sales_target"] = df["monthly_sales_target_plan"].where(
            df["monthly_sales_target_plan"].notna() & (df["monthly_sales_target_plan"] > 0),
            computed,
        )
        df.drop(columns=["monthly_sales_target_plan"], inplace=True)
    else:
        df["monthly_sales_target"] = df["next_year_budget"] / 12

    df["total_stock_coverage"] = np.where(
        df["monthly_sales_target"] > 0,
        df["total_stock"] / df["monthly_sales_target"],
        np.nan
    )

    df["twc_stock_coverage"] = np.where(
        df["monthly_sales_target"] > 0,
        df["stock_at_hand"] / df["monthly_sales_target"],
        np.nan
    )

    # ── Sales Health Analysis ──
    remaining_months = 12 - months_elapsed

    df["annual_target"] = df["monthly_sales_target"] * 12

    df["expected_ytd_sales"] = df["monthly_sales_target"] * months_elapsed

    df["ytd_variance"] = np.where(
        df["expected_ytd_sales"] > 0,
        df["current_year_sales"] - df["expected_ytd_sales"],
        np.nan,
    )

    df["sales_pace_pct"] = np.where(
        df["expected_ytd_sales"] > 0,
        df["current_year_sales"] / df["expected_ytd_sales"],
        np.nan,
    )

    def _health_label(pace):
        if pd.isna(pace):
            return ""
        if pace >= 1.10:
            return "🟢 Ahead"
        if pace >= 0.90:
            return "🟡 On Track"
        if pace >= 0.70:
            return "🟠 Behind"
        return "🔴 Critical"

    df["sales_health"] = df["sales_pace_pct"].apply(_health_label)

    df["remaining_target"] = np.where(
        df["annual_target"] > 0,
        np.maximum(df["annual_target"] - df["current_year_sales"], 0),
        np.nan,
    )

    if remaining_months > 0:
        df["required_monthly_sales"] = np.where(
            df["remaining_target"] > 0,
            df["remaining_target"] / remaining_months,
            np.nan,
        )
    else:
        df["required_monthly_sales"] = np.nan

    df["required_vs_target_pct"] = np.where(
        df["monthly_sales_target"] > 0,
        df["required_monthly_sales"] / df["monthly_sales_target"],
        np.nan,
    )

    # Bonded Stock = Total Stock - Stock at Hand
    df["bonded_stock"] = df["total_stock"] - df["stock_at_hand"]

    negatives = df[df["bonded_stock"] < 0][["sku", "total_stock", "stock_at_hand", "bonded_stock"]].copy()
    if not negatives.empty:
        st.warning(
            f"{len(negatives)} SKU(s) have negative Bonded Stock (Total Stock < Stock at Hand). "
            "This means one of the stock fields is not mapped to the right quantity column."
        )
        with st.expander("Negative bonded stock details", expanded=False):
            st.dataframe(negatives.sort_values("bonded_stock").head(200), use_container_width=True)

    df["bonded_reorder"] = np.where(
        df["has_demand"] & (df["total_stock_coverage"] < 4),
        "REORDER",
        ""
    )

    df["twc_reorder"] = np.where(
        df["has_demand"] & (df["twc_stock_coverage"] < 1),
        "REORDER",
        ""
    )

    st.session_state.final_df = pd.DataFrame({
        "SKU": df["sku"],
        "Item Name": df["item_name"],
        "Country of Origin": df["country_of_origin"],
        "Previous Year Sales": df["previous_year_sales"],
        "Current Year Sales": df["current_year_sales"],
        "% Sales Difference": df["sales_diff"],
        "Growth Target %": growth,
        "Next Year Budget": df["next_year_budget"],
        "Monthly Sales Target": df["monthly_sales_target"],
        "Annual Target": df["annual_target"],
        "Expected YTD Sales": df["expected_ytd_sales"],
        "YTD Variance": df["ytd_variance"],
        "Sales Pace %": df["sales_pace_pct"],
        "Sales Health": df["sales_health"],
        "Remaining Target": df["remaining_target"],
        "Required Monthly Sales": df["required_monthly_sales"],
        "Required vs Target %": df["required_vs_target_pct"],
        "Total Stock": df["total_stock"],
        "Total Stock Coverage (Months)": df["total_stock_coverage"],
        "Bonded Reorder Point": df["bonded_reorder"],
        "Bonded Stock": df["bonded_stock"],
        "Stock at Hand": df["stock_at_hand"],
        "TWC Stock Coverage (Months)": df["twc_stock_coverage"],
        "TWC Stock Reorder Point": df["twc_reorder"]
    })

    # ── Store health summary for dashboard ──
    has_target = df["monthly_sales_target"] > 0
    st.session_state["health_summary"] = {
        "months_elapsed": months_elapsed,
        "remaining_months": remaining_months,
        "total_skus_w_target": int(has_target.sum()),
        "ahead": int((df["sales_health"] == "🟢 Ahead").sum()),
        "on_track": int((df["sales_health"] == "🟡 On Track").sum()),
        "behind": int((df["sales_health"] == "🟠 Behind").sum()),
        "critical": int((df["sales_health"] == "🔴 Critical").sum()),
    }

# =====================================================
# OUTPUT
# =====================================================
if not st.session_state.final_df.empty:

    # ── Sales Health Dashboard ──
    hs = st.session_state.get("health_summary")
    if hs and hs["total_skus_w_target"] > 0:
        st.subheader("📈 Sales Health Dashboard")
        st.caption(
            f"Based on **{hs['months_elapsed']} months** elapsed — "
            f"**{hs['remaining_months']:.1f} months** remaining to hit annual targets."
        )

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("🟢 Ahead (≥110%)", hs["ahead"])
        k2.metric("🟡 On Track (90-110%)", hs["on_track"])
        k3.metric("🟠 Behind (70-90%)", hs["behind"])
        k4.metric("🔴 Critical (<70%)", hs["critical"])

        # Quick breakdown tables
        fdf = st.session_state.final_df
        with st.expander("🔴 Critical items — need immediate attention", expanded=True):
            crit = fdf[fdf["Sales Health"] == "🔴 Critical"].sort_values("Sales Pace %")
            if crit.empty:
                st.success("No critical items!")
            else:
                st.dataframe(
                    crit[[
                        "SKU", "Item Name", "Monthly Sales Target",
                        "Current Year Sales", "Expected YTD Sales",
                        "YTD Variance", "Sales Pace %",
                        "Required Monthly Sales", "Required vs Target %",
                    ]].style.format({
                        "Sales Pace %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
                        "Required vs Target %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
                        "YTD Variance": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
                        "Monthly Sales Target": "{:,.1f}",
                        "Current Year Sales": "{:,.0f}",
                        "Expected YTD Sales": "{:,.1f}",
                        "Required Monthly Sales": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    }),
                    use_container_width=True,
                )

        with st.expander("🟠 Behind target items", expanded=False):
            behind = fdf[fdf["Sales Health"] == "🟠 Behind"].sort_values("Sales Pace %")
            if behind.empty:
                st.info("No items behind target.")
            else:
                st.dataframe(
                    behind[[
                        "SKU", "Item Name", "Monthly Sales Target",
                        "Current Year Sales", "Expected YTD Sales",
                        "YTD Variance", "Sales Pace %",
                        "Required Monthly Sales", "Required vs Target %",
                    ]].style.format({
                        "Sales Pace %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
                        "Required vs Target %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
                        "YTD Variance": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
                        "Monthly Sales Target": "{:,.1f}",
                        "Current Year Sales": "{:,.0f}",
                        "Expected YTD Sales": "{:,.1f}",
                        "Required Monthly Sales": lambda x: "" if pd.isna(x) else f"{x:,.1f}",
                    }),
                    use_container_width=True,
                )

        with st.expander("🟢 Ahead of target items", expanded=False):
            ahead = fdf[fdf["Sales Health"] == "🟢 Ahead"].sort_values("Sales Pace %", ascending=False)
            if ahead.empty:
                st.info("No items ahead of target.")
            else:
                st.dataframe(
                    ahead[[
                        "SKU", "Item Name", "Monthly Sales Target",
                        "Current Year Sales", "Expected YTD Sales",
                        "YTD Variance", "Sales Pace %",
                    ]].style.format({
                        "Sales Pace %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
                        "YTD Variance": lambda x: "" if pd.isna(x) else f"{x:,.0f}",
                        "Monthly Sales Target": "{:,.1f}",
                        "Current Year Sales": "{:,.0f}",
                        "Expected YTD Sales": "{:,.1f}",
                    }),
                    use_container_width=True,
                )

        st.markdown("---")

    # ── Full Report Table ──
    st.subheader("📋 Full Report")
    styled_df = st.session_state.final_df.style.format({
        "% Sales Difference": lambda x: "" if pd.isna(x) else f"{x:.2%}",
        "Sales Pace %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
        "Required vs Target %": lambda x: "" if pd.isna(x) else f"{x:.0%}",
    })

    st.dataframe(styled_df, use_container_width=True)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "⬇ CSV",
            st.session_state.final_df.to_csv(index=False),
            "sales_stock_planning.csv"
        )
    with c2:
        st.download_button(
            "⬇ Excel",
            df_to_excel_bytes(st.session_state.final_df),
            "sales_stock_planning.xlsx"
        )
    with c3:
        st.download_button(
            "⬇ PDF",
            df_to_pdf_bytes(st.session_state.final_df),
            "sales_stock_planning.pdf"
        )
