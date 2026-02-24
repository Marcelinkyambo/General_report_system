import streamlit as st
import pandas as pd
import numpy as np
from io import BytesIO
from fpdf import FPDF
from datetime import datetime, date

# =====================================================
# CONFIG
# =====================================================
st.set_page_config(page_title="P&S Forecast System", layout="wide")
st.markdown("<h1 style='text-align:center;'>📊 P&S Forecast Report</h1>", unsafe_allow_html=True)

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
    parts = str(sku).strip().split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else str(sku)

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

def df_to_excel_bytes(df):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    return buf.getvalue()

def df_to_pdf_bytes(df):
    pdf = FPDF(orientation="L", unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", size=6)

    padding = 2
    col_widths = []
    for col in df.columns:
        max_w = pdf.get_string_width(str(col)) + padding
        for val in df[col].head(100):
            w = pdf.get_string_width(str(val)) + padding
            if w > max_w:
                max_w = w
        col_widths.append(max_w)

    table_w = sum(col_widths)
    epw = pdf.w - 2 * pdf.l_margin
    if table_w > epw:
        scale = epw / table_w
        col_widths = [w * scale for w in col_widths]

    row_h = pdf.font_size * 1.5

    for i, c in enumerate(df.columns):
        pdf.cell(col_widths[i], row_h, str(c)[:15], border=1, align='C')
    pdf.ln(row_h)

    for _, r in df.iterrows():
        for i, v in enumerate(r):
            pdf.cell(col_widths[i], row_h, str(v)[:15], border=1)
        pdf.ln(row_h)

    out = pdf.output(dest="S")
    if isinstance(out, str):
        return out.encode("latin-1")
    return out

# =====================================================
# UPLOAD SECTION
# =====================================================
st.header("1️⃣ Upload Source Data (Current & Previous Year)")

c1, c2, c3 = st.columns(3)

with c1:
    sales_cy_file = st.file_uploader("Sales by Item — Current Year", ["csv", "xlsx"])
    sales_py_file = st.file_uploader("Sales by Item — Previous Year", ["csv", "xlsx"])

with c2:
    stock_cy_file = st.file_uploader("Stock Summary — Current Year", ["csv", "xlsx"])
    purchases_cy_file = st.file_uploader("Purchases by Item — Current Year", ["csv", "xlsx"])

# Date input for YTD calculation
st.subheader("📅 YTD Data Period")
col_date1, col_date2 = st.columns(2)
with col_date1:
    data_year = st.number_input("Data Year", min_value=2020, max_value=2030, value=datetime.now().year)
with col_date2:
    data_end_date = st.date_input("YTD Data End Date", value=datetime.now().date())

# Calculate year fraction elapsed
year_start = date(data_end_date.year, 1, 1)
days_elapsed = (data_end_date - year_start).days + 1
is_leap_year = data_end_date.year % 4 == 0 and (data_end_date.year % 100 != 0 or data_end_date.year % 400 == 0)
total_days_in_year = 366 if is_leap_year else 365
year_fraction_elapsed = max(days_elapsed / total_days_in_year, 0.01)
year_fraction_remaining = 1 - year_fraction_elapsed

st.info(f"📊 YTD Period: {days_elapsed} days ({year_fraction_elapsed*100:.1f}% of year elapsed, {year_fraction_remaining*100:.1f}% remaining)")

# =====================================================
# GENERATE REPORT
# =====================================================
st.header("2️⃣ Generate P&S Forecast Report")

if st.button("🚀 Generate Report"):

    if not all([sales_cy_file, sales_py_file, stock_cy_file, purchases_cy_file]):
        st.error("❌ Please upload all required files: Sales (Current & Previous Year), Stock Summary, and Purchases.")
        st.stop()

    # ---------------- LOAD FILES ----------------
    sales_cy = read_uploaded_file(sales_cy_file)
    sales_py = read_uploaded_file(sales_py_file)
    stock_cy = read_uploaded_file(stock_cy_file)
    purchases_cy = read_uploaded_file(purchases_cy_file)

    # ---------------- COLUMN RESOLUTION ----------------
    # Stock Summary columns
    stock_sku_col = find_column(stock_cy, ["sku"])
    stock_item_col = find_column(stock_cy, ["item", "name"]) or find_column(stock_cy, ["item_name"])
    stock_category_col = find_column(stock_cy, ["category", "name"]) or find_column(stock_cy, ["category"])
    opening_col = find_column(stock_cy, ["opening", "stock"]) or find_column(stock_cy, ["opening"])
    closing_col = find_column(stock_cy, ["closing", "stock"]) or find_column(stock_cy, ["closing"])
    
    # Sales columns
    sales_item_col = find_column(sales_cy, ["item", "name"]) or find_column(sales_cy, ["item_name"])
    sales_category_col = find_column(sales_cy, ["category", "name"]) or find_column(sales_cy, ["category"])
    qty_sold_col = find_column(sales_cy, ["quantity", "sold"]) or find_column(sales_cy, ["quantity"])
    sales_amount_col = find_column(sales_cy, ["amount"])
    
    # Previous year sales
    qty_sold_py_col = find_column(sales_py, ["quantity", "sold"]) or find_column(sales_py, ["quantity"])
    sales_amount_py_col = find_column(sales_py, ["amount"])
    sales_py_item_col = find_column(sales_py, ["item", "name"]) or find_column(sales_py, ["item_name"])
    
    # Purchases columns
    purchases_sku_col = find_column(purchases_cy, ["sku"])
    qty_purchased_col = find_column(purchases_cy, ["quantity", "purchased"]) or find_column(purchases_cy, ["quantity"])

    # ---------------- NORMALIZE SKUs ----------------
    stock_cy["sku_norm"] = stock_cy[stock_sku_col].apply(normalize_sku)
    purchases_cy["sku_norm"] = purchases_cy[purchases_sku_col].apply(normalize_sku)
    
    # ---------------- BUILD BASE FROM STOCK SUMMARY ----------------
    # Group stock by normalized SKU
    agg_dict = {
        stock_item_col: "first",
        opening_col: "sum",
        closing_col: "sum"
    }
    if stock_category_col:
        agg_dict[stock_category_col] = "first"
    
    stock_grouped = stock_cy.groupby("sku_norm", as_index=False).agg(agg_dict)
    
    if stock_category_col:
        stock_grouped.columns = ["SKU", "Item Name", "Opening Balance", "Closing Stock", "Category"]
    else:
        stock_grouped.columns = ["SKU", "Item Name", "Opening Balance", "Closing Stock"]
        stock_grouped["Category"] = ""
    
    # Group purchases by normalized SKU
    purchases_grouped = purchases_cy.groupby("sku_norm", as_index=False).agg({
        qty_purchased_col: "sum"
    })
    purchases_grouped.columns = ["SKU", "Purchases"]
    
    # Group current year sales by item name
    agg_sales = {qty_sold_col: "sum"}
    if sales_amount_col:
        agg_sales[sales_amount_col] = "sum"
    if sales_category_col:
        agg_sales[sales_category_col] = "first"
        
    sales_cy_grouped = sales_cy.groupby(sales_item_col, as_index=False).agg(agg_sales)
    
    col_names = ["Item Name", "Sales Current Year"]
    if sales_amount_col:
        col_names.append("Sales Value Current Year")
    if sales_category_col:
        col_names.append("Category_Sales")
    sales_cy_grouped.columns = col_names
    
    if "Sales Value Current Year" not in sales_cy_grouped.columns:
        sales_cy_grouped["Sales Value Current Year"] = 0
    
    # Group previous year sales by item name
    agg_sales_py = {qty_sold_py_col: "sum"}
    if sales_amount_py_col:
        agg_sales_py[sales_amount_py_col] = "sum"
        
    sales_py_grouped = sales_py.groupby(sales_py_item_col, as_index=False).agg(agg_sales_py)
    
    col_names_py = ["Item Name", "Previous Year Sales"]
    if sales_amount_py_col:
        col_names_py.append("Sales Value Previous Year")
    sales_py_grouped.columns = col_names_py
    
    if "Sales Value Previous Year" not in sales_py_grouped.columns:
        sales_py_grouped["Sales Value Previous Year"] = 0
    
    # ---------------- MERGE ALL DATA ----------------
    df = stock_grouped.copy()
    
    # Merge purchases
    df = df.merge(purchases_grouped, on="SKU", how="left")
    
    # Merge current year sales by Item Name
    merge_cols = ["Item Name", "Sales Current Year", "Sales Value Current Year"]
    if "Category_Sales" in sales_cy_grouped.columns:
        merge_cols.append("Category_Sales")
    df = df.merge(sales_cy_grouped[merge_cols], on="Item Name", how="left")
    
    # Merge previous year sales by Item Name
    df = df.merge(sales_py_grouped, on="Item Name", how="left")
    
    # Fill NaN with 0 for numeric columns
    numeric_cols = ["Opening Balance", "Purchases", "Sales Current Year", "Sales Value Current Year", 
                    "Previous Year Sales", "Sales Value Previous Year", "Closing Stock"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].fillna(0)
    
    # Use Category from sales if stock doesn't have it
    if "Category_Sales" in df.columns:
        df["Category"] = df["Category"].replace("", np.nan).fillna(df["Category_Sales"])
        df = df.drop(columns=["Category_Sales"])
    
    # ---------------- CALCULATE CLOSING BALANCE & DISCREPANCIES ----------------
    df["Closing Balance"] = (df["Opening Balance"] + df["Purchases"] - df["Sales Current Year"]).round(0).astype(int)
    df["Discrepancies"] = (df["Closing Balance"] - df["Closing Stock"]).round(0).astype(int)
    
    # ---------------- CALCULATE FORECASTS ----------------
    # Max forecast = max(current, previous) / elapsed_fraction * remaining_fraction
    df["Max Forecast"] = (
        df[["Sales Current Year", "Previous Year Sales"]].max(axis=1) 
        / year_fraction_elapsed * year_fraction_remaining
    ).clip(lower=0).round(0).astype(int)
    
    # Min forecast = min(current, previous) / elapsed_fraction * remaining_fraction  
    df["Min Forecast"] = (
        df[["Sales Current Year", "Previous Year Sales"]].min(axis=1) 
        / year_fraction_elapsed * year_fraction_remaining
    ).clip(lower=0).round(0).astype(int)
    
    # AVG forecast = average of max and min
    df["AVG Forecast"] = ((df["Max Forecast"] + df["Min Forecast"]) / 2).round(0).astype(int)
    
    # Purchase forecasts = forecast - closing balance (what we need to buy)
    df["Max Purchase Forecast"] = (df["Max Forecast"] - df["Closing Balance"]).clip(lower=0).round(0).astype(int)
    df["Min Purchase Forecast"] = (df["Min Forecast"] - df["Closing Balance"]).clip(lower=0).round(0).astype(int)
    df["AVG Purchase Forecast"] = ((df["Max Purchase Forecast"] + df["Min Purchase Forecast"]) / 2).round(0).astype(int)
    
    # ---------------- CALCULATE PERCENTAGES ----------------
    total_available = df["Opening Balance"] + df["Purchases"]
    df["% Sales on Stock"] = np.where(
        total_available > 0,
        df["Sales Current Year"] / total_available,
        0
    ).round(4)
    
    df["% Sales Difference"] = np.where(
        df["Previous Year Sales"] > 0,
        (df["Sales Current Year"] - df["Previous Year Sales"]) / df["Previous Year Sales"],
        np.where(df["Sales Current Year"] > 0, 1, 0)
    ).round(4)
    
    # ---------------- FORMAT FINAL OUTPUT ----------------
    int_cols = ["Opening Balance", "Purchases", "Sales Current Year", "Sales Value Current Year",
                "Previous Year Sales", "Sales Value Previous Year", "Closing Balance", "Discrepancies",
                "Max Forecast", "Min Forecast", "AVG Forecast", 
                "Max Purchase Forecast", "Min Purchase Forecast", "AVG Purchase Forecast"]
    for col in int_cols:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype(int)
    
    # Select and order columns to match expected output
    final_df = df[[
        "SKU", "Item Name", "Category", "Opening Balance", "Purchases",
        "Sales Current Year", "Sales Value Current Year", 
        "Previous Year Sales", "Sales Value Previous Year",
        "Closing Balance", "Discrepancies",
        "Max Forecast", "Min Forecast", "AVG Forecast",
        "Max Purchase Forecast", "Min Purchase Forecast", "AVG Purchase Forecast",
        "% Sales on Stock", "% Sales Difference"
    ]].copy()
    
    # Sort by SKU
    final_df = final_df.sort_values("SKU").reset_index(drop=True)
    
    st.session_state["final_df"] = final_df
    st.success(f"✅ Report generated with {len(final_df)} items!")

# =====================================================
# OUTPUT
# =====================================================
if "final_df" in st.session_state:
    df = st.session_state["final_df"]
    
    st.header("📦 P&S Forecast Report")
    st.dataframe(df, use_container_width=True)
    
    # Summary statistics
    st.subheader("📈 Summary")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Items", len(df))
    col2.metric("Total Current Sales", f"{df['Sales Current Year'].sum():,}")
    col3.metric("Total Previous Sales", f"{df['Previous Year Sales'].sum():,}")
    col4.metric("Total AVG Purchase Forecast", f"{df['AVG Purchase Forecast'].sum():,}")
    
    # Downloads
    st.subheader("📥 Download Report")
    c1, c2, c3 = st.columns(3)
    c1.download_button("⬇ CSV", df.to_csv(index=False), "ps_forecast.csv", "text/csv")
    c2.download_button("⬇ Excel", df_to_excel_bytes(df), "ps_forecast.xlsx", 
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    c3.download_button("⬇ PDF", df_to_pdf_bytes(df), "ps_forecast.pdf", "application/pdf")
