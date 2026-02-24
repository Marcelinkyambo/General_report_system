import streamlit as st
import pandas as pd
import numpy as np
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

# ────────────────────────────────────────────────────────
# Page config
# ────────────────────────────────────────────────────────
st.set_page_config(page_title="Inventory Forecast & Purchase Planning", layout="wide")
st.markdown(
    "<h1 style='text-align:center;'>📦 Inventory Forecast &amp; Purchase Planning</h1>",
    unsafe_allow_html=True,
)

FILES_DIR = Path(__file__).resolve().parent / "files"


# ────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────
def _read_file(source) -> pd.DataFrame:
    """Read a CSV or Excel file from an UploadedFile or a Path."""
    if isinstance(source, Path):
        if source.suffix.lower() in (".xlsx", ".xls"):
            return pd.read_excel(source)
        return pd.read_csv(source, encoding="utf-8-sig")
    # Streamlit UploadedFile
    name = source.name.lower()
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(source)
    return pd.read_csv(source, encoding="utf-8-sig")


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Strip spaces from column names and lowercase them."""
    df = df.copy()
    df.columns = df.columns.str.strip().str.lower()
    return df


def _find_sku_col(df: pd.DataFrame) -> str | None:
    """Return the first column whose name contains 'sku'."""
    for c in df.columns:
        if "sku" in c:
            return c
    return None


def _sku_prefix(sku: str, n: int = 6) -> str:
    """Return the first *n* digit characters from *sku* for grouping.
    Strips all non-digit chars first so hyphens don't eat into the prefix.
    Zero-pads to *n* digits for consistent matching across sources.
    """
    digits = re.sub(r"[^0-9]", "", str(sku))
    return digits[:n].zfill(n) if digits else ""


def _safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _find_col(df: pd.DataFrame, hints: list[str]) -> str | None:
    """Find the first column matching any of the hint substrings.
    Exact matches are preferred over substring matches.
    """
    # Pass 1: exact match
    for hint in hints:
        for c in df.columns:
            if c == hint:
                return c
    # Pass 2: substring match (skip very short hints to avoid false positives)
    for hint in hints:
        if len(hint) < 4:
            continue
        for c in df.columns:
            if hint in c and "amount" not in c:
                return c
    # Pass 3: substring match including short hints (last resort)
    for hint in hints:
        for c in df.columns:
            if hint in c and "amount" not in c:
                return c
    return None


def _discover_files(folder: Path) -> dict[str, list[Path]]:
    """Scan the files/ folder and classify by report type."""
    out: dict[str, list[Path]] = {
        "sales_cy": [], "sales_py": [],
        "purchases_cy": [], "purchases_py": [],
        "stock": [],
    }
    if not folder.exists():
        return out
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in (".csv", ".xlsx", ".xls"):
            continue
        low = p.name.lower()
        if low.startswith("sales by item") and "26" in low:
            out["sales_cy"].append(p)
        elif low.startswith("sales by item") and "25" in low:
            out["sales_py"].append(p)
        elif low.startswith("purchases by item") and "26" in low:
            out["purchases_cy"].append(p)
        elif low.startswith("purchases by item") and "25" in low:
            out["purchases_py"].append(p)
        elif low.startswith("stock summary") and "26" in low:
            out["stock"].append(p)
    return out


# ────────────────────────────────────────────────────────
# Year progress
# ────────────────────────────────────────────────────────
today = datetime.today()
start_of_year = datetime(today.year, 1, 1)
end_of_year = datetime(today.year, 12, 31)

elapsed_days = (today - start_of_year).days + 1
total_days = (end_of_year - start_of_year).days + 1
elapsed_percent = elapsed_days / total_days if total_days > 0 else 1
remaining_percent = 1 - elapsed_percent

st.info(
    f"📅 Year progress: **{elapsed_percent:.2%}** elapsed, "
    f"**{remaining_percent:.2%}** remaining  "
    f"({elapsed_days} / {total_days} days)"
)


# ────────────────────────────────────────────────────────
# Data sources — upload OR pick from files/ folder
# ────────────────────────────────────────────────────────
st.subheader("1️⃣  Upload Reports")
st.caption("Upload five reports (CSV or XLSX). If not uploaded the app will use matching files in the `files/` folder.")

col1, col2, col3 = st.columns(3)
with col1:
    upload_cy = st.file_uploader("Current Year Sales YTD", type=["csv", "xlsx"], key="up_cy")
with col2:
    upload_py = st.file_uploader("Previous Year Sales YTD", type=["csv", "xlsx"], key="up_py")
with col3:
    upload_stock = st.file_uploader("Stock Summary", type=["csv", "xlsx"], key="up_stock")

col4, col5 = st.columns(2)
with col4:
    upload_purch_cy = st.file_uploader("Current Year Purchases YTD", type=["csv", "xlsx"], key="up_purch_cy")
with col5:
    upload_purch_py = st.file_uploader("Previous Year Purchases YTD", type=["csv", "xlsx"], key="up_purch_py")

# Fall back to files/ folder if nothing uploaded
local = _discover_files(FILES_DIR)

src_cy = upload_cy or (local["sales_cy"][0] if local["sales_cy"] else None)
src_py = upload_py or (local["sales_py"][0] if local["sales_py"] else None)
src_stock = upload_stock or (local["stock"][0] if local["stock"] else None)
src_purch_cy = upload_purch_cy or (local["purchases_cy"][0] if local["purchases_cy"] else None)
src_purch_py = upload_purch_py or (local["purchases_py"][0] if local["purchases_py"] else None)

if not all([src_cy, src_py, src_stock]):
    missing = []
    if not src_cy:
        missing.append("Current Year Sales")
    if not src_py:
        missing.append("Previous Year Sales")
    if not src_stock:
        missing.append("Stock Summary")
    st.warning(f"Missing: {', '.join(missing)}. Upload them above or place files in `files/`.")
    st.stop()


# ────────────────────────────────────────────────────────
# Load & process
# ────────────────────────────────────────────────────────
def load_and_process(cy_src, py_src, stk_src, purch_cy_src=None, purch_py_src=None):
    df_cy = _clean(_read_file(cy_src))
    df_py = _clean(_read_file(py_src))
    df_stock = _clean(_read_file(stk_src))

    # ── Load purchase reports (if available) ──
    df_purch_cy = _clean(_read_file(purch_cy_src)) if purch_cy_src else None
    df_purch_py = _clean(_read_file(purch_py_src)) if purch_py_src else None

    # ── Load planning file if exists ──
    planning_path = Path(__file__).resolve().parent / "Sales Stock Planning 2026, M.xlsx"
    if planning_path.exists():
        df_planning = _clean(pd.read_excel(planning_path))
        df_planning["sku_group"] = df_planning["sku"].apply(_sku_prefix)
        planning_agg = df_planning.groupby("sku_group", as_index=False).agg(
            budget=("next year budget", "sum"),
            growth=("growth target %", "mean")
        )
    else:
        planning_agg = pd.DataFrame(columns=["sku_group", "budget", "growth"])

    # ── Identify SKU columns ──
    sku_cy = _find_sku_col(df_cy)
    sku_py = _find_sku_col(df_py)
    sku_st = _find_sku_col(df_stock)
    sku_pcy = _find_sku_col(df_purch_cy) if df_purch_cy is not None else None
    sku_ppy = _find_sku_col(df_purch_py) if df_purch_py is not None else None

    errors = []
    if sku_cy is None:
        errors.append("Current Year Sales: no column containing 'sku'")
    if sku_py is None:
        errors.append("Previous Year Sales: no column containing 'sku'")
    if sku_st is None:
        errors.append("Stock Summary: no column containing 'sku'")
    if df_purch_cy is not None and sku_pcy is None:
        errors.append("Current Year Purchases: no column containing 'sku'")
    if df_purch_py is not None and sku_ppy is None:
        errors.append("Previous Year Purchases: no column containing 'sku'")
    if errors:
        return None, errors, {}

    # ── Drop rows with missing / empty SKU ──
    df_cy = df_cy[df_cy[sku_cy].notna() & (df_cy[sku_cy].astype(str).str.strip() != "")].copy()
    df_py = df_py[df_py[sku_py].notna() & (df_py[sku_py].astype(str).str.strip() != "")].copy()
    df_stock = df_stock[df_stock[sku_st].notna() & (df_stock[sku_st].astype(str).str.strip() != "")].copy()

    # ── Build sku_group (first 6 DIGITS, stripping hyphens/non-digits) ──
    df_cy["sku_group"] = df_cy[sku_cy].apply(_sku_prefix)
    df_py["sku_group"] = df_py[sku_py].apply(_sku_prefix)
    df_stock["sku_group"] = df_stock[sku_st].apply(_sku_prefix)

    if df_purch_cy is not None and sku_pcy:
        df_purch_cy = df_purch_cy[df_purch_cy[sku_pcy].notna() & (df_purch_cy[sku_pcy].astype(str).str.strip() != "")].copy()
        df_purch_cy["sku_group"] = df_purch_cy[sku_pcy].apply(_sku_prefix)
        df_purch_cy = df_purch_cy[df_purch_cy["sku_group"] != ""].copy()
    if df_purch_py is not None and sku_ppy:
        df_purch_py = df_purch_py[df_purch_py[sku_ppy].notna() & (df_purch_py[sku_ppy].astype(str).str.strip() != "")].copy()
        df_purch_py["sku_group"] = df_purch_py[sku_ppy].apply(_sku_prefix)
        df_purch_py = df_purch_py[df_purch_py["sku_group"] != ""].copy()

    # ── Remove rows where sku_group is empty (no digits in SKU) ──
    df_cy = df_cy[df_cy["sku_group"] != ""].copy()
    df_py = df_py[df_py["sku_group"] != ""].copy()
    df_stock = df_stock[df_stock["sku_group"] != ""].copy()

    # ── Find quantity / name columns ──
    qty_cy_col = _find_col(df_cy, ["quantity_sold", "quantity", "qty"])
    qty_py_col = _find_col(df_py, ["quantity_sold", "quantity", "qty"])
    closing_col = _find_col(df_stock, ["closing stock", "closing_stock", "closing"])
    opening_col = _find_col(df_stock, ["opening stock", "opening_stock", "opening"])
    in_col = _find_col(df_stock, ["quantity in", "quantity_in"])
    out_col = _find_col(df_stock, ["quantity out", "quantity_out"])
    name_col_st = _find_col(df_stock, ["item name", "item_name"])
    name_col_cy = _find_col(df_cy, ["item_name", "item name"])

    # Purchase quantity column from purchase reports
    qty_pcy_col = _find_col(df_purch_cy, ["quantity_purchased", "quantity", "qty"]) if df_purch_cy is not None else None
    qty_ppy_col = _find_col(df_purch_py, ["quantity_purchased", "quantity", "qty"]) if df_purch_py is not None else None

    if qty_cy_col is None:
        errors.append("Current Year Sales: no quantity column found")
    if qty_py_col is None:
        errors.append("Previous Year Sales: no quantity column found")
    if closing_col is None:
        errors.append("Stock Summary: no closing stock column found")
    if opening_col is None:
        errors.append("Stock Summary: no opening stock column found")
    if in_col is None:
        errors.append("Stock Summary: no quantity in column found")
    if out_col is None:
        errors.append("Stock Summary: no quantity out column found")
    if errors:
        return None, errors, {}

    # ── Debug info (column detection results) ──
    debug_info = {
        "CY quantity column": qty_cy_col,
        "PY quantity column": qty_py_col,
        "Stock closing column": closing_col,
        "Stock opening column": opening_col,
        "Stock qty-in column (for discrepancies)": in_col,
        "Stock qty-out column": out_col,
        "Stock name column": name_col_st,
        "CY name column": name_col_cy,
        "CY purchases column": qty_pcy_col,
        "PY purchases column": qty_ppy_col,
        "Purchases source": "Purchase Reports" if df_purch_cy is not None else "Not available",
    }

    # ── Safe numeric conversion ──
    df_cy["_qty"] = _safe_numeric(df_cy[qty_cy_col])
    df_py["_qty"] = _safe_numeric(df_py[qty_py_col])
    df_stock["_closing"] = _safe_numeric(df_stock[closing_col])
    df_stock["_opening"] = _safe_numeric(df_stock[opening_col])
    df_stock["_in"] = _safe_numeric(df_stock[in_col])
    df_stock["_out"] = _safe_numeric(df_stock[out_col])

    # ── Aggregate by sku_group ──
    agg_cy = df_cy.groupby("sku_group", as_index=False).agg(sales=("_qty", "sum"))
    agg_py = df_py.groupby("sku_group", as_index=False).agg(previous_sales=("_qty", "sum"))
    agg_stock = df_stock.groupby("sku_group", as_index=False).agg(
        opening_balance=("_opening", "sum"),
        stock_in=("_in", "sum"),
        quantity_out=("_out", "sum"),
        closing_balance=("_closing", "sum"),
    )

    # ── Aggregate purchases from Purchase Reports ──
    if df_purch_cy is not None and qty_pcy_col:
        df_purch_cy["_pqty"] = _safe_numeric(df_purch_cy[qty_pcy_col])
        agg_purch_cy = df_purch_cy.groupby("sku_group", as_index=False).agg(purchases=("_pqty", "sum"))
    else:
        agg_purch_cy = pd.DataFrame(columns=["sku_group", "purchases"])

    if df_purch_py is not None and qty_ppy_col:
        df_purch_py["_pqty"] = _safe_numeric(df_purch_py[qty_ppy_col])
        agg_purch_py = df_purch_py.groupby("sku_group", as_index=False).agg(previous_purchases=("_pqty", "sum"))
    else:
        agg_purch_py = pd.DataFrame(columns=["sku_group", "previous_purchases"])

    # ── Representative item_name per sku_group ──
    # Pick the name from the row with the highest closing stock (most relevant variant)
    if name_col_st:
        _name_df = df_stock.dropna(subset=[name_col_st]).copy()
        if not _name_df.empty:
            _name_df["_cs"] = _safe_numeric(_name_df[closing_col])
            _name_df = _name_df.sort_values("_cs", ascending=False)
            name_map = (
                _name_df.groupby("sku_group")[name_col_st]
                .first()
                .rename("item_name")
            )
        else:
            name_map = pd.Series(dtype=str, name="item_name")
    elif name_col_cy:
        name_map = (
            df_cy.dropna(subset=[name_col_cy])
            .groupby("sku_group")[name_col_cy]
            .first()
            .rename("item_name")
        )
    else:
        name_map = pd.Series(dtype=str, name="item_name")

    # ── Outer-join all sources ──
    merged = (
        agg_cy
        .merge(agg_py, on="sku_group", how="outer")
        .merge(agg_stock, on="sku_group", how="outer")
        .merge(agg_purch_cy, on="sku_group", how="left")
        .merge(agg_purch_py, on="sku_group", how="left")
    )
    merged = merged.fillna(0)

    # ── Merge planning data ──
    merged = merged.merge(planning_agg, on="sku_group", how="left")
    merged["budget"] = _safe_numeric(merged["budget"]).fillna(0)
    merged["growth"] = _safe_numeric(merged["growth"]).fillna(0)

    # ── Calculate discrepancies: Closing Balance - (Opening Balance + Purchases - Sales) ──
    merged["discrepancies"] = (
        merged["closing_balance"] - (merged["opening_balance"] + merged["purchases"] - merged["sales"])
    ).round(0).astype(int)

    # Attach item_name
    merged = merged.merge(name_map, on="sku_group", how="left")
    merged["item_name"] = merged["item_name"].fillna("")

    for c in ["sales", "previous_sales", "closing_balance"]:
        merged[c] = _safe_numeric(merged[c]).round(0).astype(int)

    # ── Forecast calculations ──
    safe_elapsed = max(elapsed_percent, 1e-9)  # guard div-by-zero

    # Fallback forecasts (current method)
    merged["max_forecast_old"] = (
        merged[["sales", "previous_sales"]].max(axis=1) / safe_elapsed * remaining_percent
    ).clip(lower=0).round(0).astype(int)

    merged["min_forecast_old"] = (
        merged[["sales", "previous_sales"]].min(axis=1) / safe_elapsed * remaining_percent
    ).clip(lower=0).round(0).astype(int)

    # Budget-based forecasts
    merged["remaining_budget"] = (merged["budget"] - merged["sales"]).clip(lower=0).round(0).astype(int)

    merged["max_forecast"] = merged.apply(
        lambda row: row["remaining_budget"] if row["budget"] > 0 else row["max_forecast_old"],
        axis=1
    ).astype(int)

    merged["min_forecast"] = merged.apply(
        lambda row: max(0, row["remaining_budget"] * (1 - row["growth"])) if row["budget"] > 0 else row["min_forecast_old"],
        axis=1
    ).astype(int)

    merged["avg_forecast"] = (
        (merged["max_forecast"] + merged["min_forecast"]) / 2
    ).round(0).astype(int)

    merged["max_purchase_forecast"] = (
        (merged["max_forecast"] - merged["closing_balance"]).clip(lower=0)
    ).round(0).astype(int)

    merged["min_purchase_forecast"] = (
        (merged["min_forecast"] - merged["closing_balance"]).clip(lower=0)
    ).round(0).astype(int)

    merged["avg_purchase_forecast"] = (
        (merged["max_purchase_forecast"] + merged["min_purchase_forecast"]) / 2
    ).round(0).astype(int)

    # ── Final column selection ──
    final = merged[[
        "sku_group",
        "item_name",
        "sales",
        "previous_sales",
        "opening_balance",
        "purchases",
        "closing_balance",
        "discrepancies",
        "max_forecast",
        "min_forecast",
        "avg_forecast",
        "max_purchase_forecast",
        "min_purchase_forecast",
        "avg_purchase_forecast",
    ]].sort_values("sku_group").reset_index(drop=True)

    return final, [], debug_info


# ────────────────────────────────────────────────────────
# Run
# ────────────────────────────────────────────────────────
result, errs, debug = load_and_process(src_cy, src_py, src_stock, src_purch_cy, src_purch_py)

if errs:
    for e in errs:
        st.error(e)
    st.stop()

if result is None or result.empty:
    st.warning("No data produced. Check your uploaded files.")
    st.stop()

# Debug expander — helps verify column detection
with st.expander("Column Detection & Data Quality", expanded=False):
    st.json(debug)
    st.caption(f"{len(result)} SKU groups total")


# ────────────────────────────────────────────────────────
# Display
# ────────────────────────────────────────────────────────
st.subheader("2️⃣  Forecast Report")
st.caption(f"{len(result)} SKU groups · Year elapsed {elapsed_percent:.2%}")
st.dataframe(result, width="stretch", hide_index=True)


# ────────────────────────────────────────────────────────
# Downloads
# ────────────────────────────────────────────────────────
st.subheader("3️⃣  Download")

dl1, dl2 = st.columns(2)

with dl1:
    csv_bytes = result.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download CSV",
        csv_bytes,
        file_name="Forecast_Report.csv",
        mime="text/csv",
    )

with dl2:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="Forecast Report", index=False)
    st.download_button(
        "📥 Download Excel",
        buf.getvalue(),
        file_name="Forecast_Report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
