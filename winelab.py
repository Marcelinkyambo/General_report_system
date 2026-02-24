import streamlit as st
import mysql.connector
import pandas as pd
import numpy as np
import hashlib
import plotly.express as px
from io import BytesIO
from fpdf import FPDF
import openpyxl
from sqlalchemy import create_engine
from datetime import datetime, date

# ----------------- Database Connection ----------------- #
def connect_to_database():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="Root@123",
        database="thewinelab",  # Uses thewinelab database
    )

def get_sqlalchemy_engine():
    return create_engine("mysql+pymysql://root:Root%40123@localhost/thewinelab")  # Uses thewinelab database

# ----------------- User Auth Helpers ----------------- #
def create_users_table():
    conn = connect_to_database()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username VARCHAR(50) PRIMARY KEY,
            password_hash CHAR(64) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    conn.commit()
    cur.close()
    conn.close()

def create_transfer_order_table():
    conn = connect_to_database()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transfer_order_details (
            transfer_order_id VARCHAR(50),
            order_number VARCHAR(20),
            date DATE,
            transferred_date DATE,
            item_name VARCHAR(100),
            quantity_transfer INT,
            cost_price DECIMAL(10,2),
            from_warehouse_name VARCHAR(50),
            to_warehouse_name VARCHAR(50),
            status VARCHAR(20)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transfer_order_details_previous (
            transfer_order_id VARCHAR(50),
            order_number VARCHAR(20),
            date DATE,
            transferred_date DATE,
            item_name VARCHAR(100),
            quantity_transfer INT,
            cost_price DECIMAL(10,2),
            from_warehouse_name VARCHAR(50),
            to_warehouse_name VARCHAR(50),
            status VARCHAR(20)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    conn.commit()
    cur.close()
    conn.close()

def create_stock_and_sales_tables():
    conn = connect_to_database()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_summary (
            sku VARCHAR(50),
            `Item Name` VARCHAR(100),
            `Opening Stock` INT,
            `Closing Stock` INT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_summary_previous (
            sku VARCHAR(50),
            `Item Name` VARCHAR(100),
            `Opening Stock` INT,
            `Closing Stock` INT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_by_item (
            item_name VARCHAR(100),
            category_name VARCHAR(50),
            quantity_sold INT,
            rate DECIMAL(10,2)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_by_item_previous (
            item_name VARCHAR(100),
            category_name VARCHAR(50),
            quantity_sold INT,
            rate DECIMAL(10,2)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    conn.commit()
    cur.close()
    conn.close()

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def register_user(username: str, password: str) -> bool:
    conn = connect_to_database()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users(username, password_hash) VALUES (%s, %s)",
            (username, hash_password(password))
        )
        conn.commit()
        return True
    except mysql.connector.IntegrityError:
        return False
    finally:
        cur.close()
        conn.close()

def login_user(username: str, password: str) -> bool:
    conn = connect_to_database()
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM users WHERE username=%s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return bool(row and row[0] == hash_password(password))

create_users_table()
create_stock_and_sales_tables()
create_transfer_order_table()

# ----------------- Helpers for Downloads ----------------- #
def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode('utf-8')

def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Sheet1')
    return output.getvalue()

def df_to_pdf_bytes(df: pd.DataFrame) -> bytes:
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", size=7)
    padding = 2
    col_widths = []
    for col in df.columns:
        max_w = pdf.get_string_width(str(col)) + padding
        for val in df[col]:
            w = pdf.get_string_width(str(val)) + padding
            if w > max_w:
                max_w = w
        col_widths.append(max_w)
    table_w = sum(col_widths)
    epw = pdf.w - 2 * pdf.l_margin
    if table_w > epw:
        scale = epw / table_w
        col_widths = [w * scale for w in col_widths]
    row_h = pdf.font_size * 1.2
    for i, header in enumerate(df.columns):
        pdf.cell(col_widths[i], row_h, str(header), border=1, align='C')
    pdf.ln(row_h)
    for _, row in df.iterrows():
        for i, cell in enumerate(row):
            pdf.cell(col_widths[i], row_h, str(cell), border=1)
        pdf.ln(row_h)
    out = pdf.output(dest='S')
    if isinstance(out, str):
        return out.encode('latin-1')
    return out

# ----------------- Layout & Title ----------------- #
st.set_page_config(page_title="The Wine Lab Report System", layout="wide")
st.markdown("<h1 style='text-align:center;'>📦 The Wine Lab Report & P&S Forecast</h1>", unsafe_allow_html=True)

# ----------------- Session State Init ----------------- #
for key, default in [
    ("logged_in", False),
    ("username", ""),
    ("report_df", pd.DataFrame()),
    ("forecast_df", pd.DataFrame())
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ----------------- Authentication ----------------- #
if not st.session_state.logged_in:
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        st.subheader("🔐 Please log in or register")
        mode = st.radio("", ["Login", "Register"], horizontal=True)
        user = st.text_input("Username")
        pwd = st.text_input("Password", type="password")
        if mode == "Register" and st.button("Create Account"):
            if register_user(user, pwd):
                st.success("Account created! Please log in.")
            else:
                st.error("Username already exists.")
        if mode == "Login" and st.button("Log In"):
            if login_user(user, pwd):
                st.session_state.logged_in = True
                st.session_state.username = user
                st.rerun()
            else:
                st.error("Invalid credentials.")
    st.stop()

# ----------------- Sidebar: CSV Import & Delete ----------------- #
with st.sidebar:
    st.write(f"👤 Logged in as **{st.session_state.username}**")
    if st.button("Log Out"):
        st.session_state.logged_in = False
        st.rerun()

    if st.session_state.username.lower() == "marcelin":
        st.header("📄 Import CSV to Table")
        base_table = st.selectbox("Table", ["stock_summary", "sales_by_item", "transfer_order_details"])
        year_opt = st.selectbox("Report Year", ["Current Year", "Previous Year"])
        uploaded = st.file_uploader("Upload CSV", type=["csv"])
        if st.button("Upload to DB"):
            if uploaded:
                df = pd.read_csv(uploaded).where(pd.notna(pd.read_csv(uploaded)), None)
                if not df.empty:
                    table_name = base_table + ("" if year_opt == "Current Year" else "_previous")
                    conn = connect_to_database()
                    cur = conn.cursor()
                    for _, row in df.iterrows():
                        cols = ",".join(f"`{c}`" for c in row.index)
                        ph = ",".join(["%s"] * len(row))
                        cur.execute(f"INSERT INTO {table_name} ({cols}) VALUES ({ph})", tuple(row))
                    conn.commit()
                    cur.close()
                    conn.close()
                    st.success("Uploaded!")
                else:
                    st.warning("Empty file.")

        st.header("🗑️ Delete Options")
        if st.button("Delete All Data"):
            table_name = base_table + ("" if year_opt == "Current Year" else "_previous")
            conn = connect_to_database()
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {table_name}")
            conn.commit()
            cur.close()
            conn.close()
            st.success(f"Cleared {table_name}")

        if st.button("Clear Entire Database"):
            for t in [
                "stock_summary", "sales_by_item", "transfer_order_details",
                "stock_summary_previous", "sales_by_item_previous", "transfer_order_details_previous"
            ]:
                conn = connect_to_database()
                cur = conn.cursor()
                cur.execute(f"DELETE FROM {t}")
                conn.commit()
                cur.close()
                conn.close()
            st.success("All tables cleared!")
    else:
        st.info("Welcome!")

    # Warehouse selection
    st.header("🏬 Select Warehouse")
    conn = connect_to_database()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT from_warehouse_name FROM transfer_order_details
        UNION
        SELECT DISTINCT to_warehouse_name FROM transfer_order_details
    """)
    warehouses = [row[0] for row in cur.fetchall() if row[0] is not None]
    cur.close()
    conn.close()
    if warehouses:
        selected_warehouse = st.selectbox("Warehouse", warehouses, key="selected_warehouse")
    else:
        st.warning("No warehouses available. Please upload transfer_order_details data.")
        selected_warehouse = None
        st.session_state.selected_warehouse = None

# ----------------- General Report Query ----------------- #
def generate_general_report(selected_warehouse):
    if not selected_warehouse:
        return pd.DataFrame()
    engine = get_sqlalchemy_engine()
    q = f"""
    SELECT
      SUBSTRING_INDEX(s.sku, '-', 2) AS SKU,
      ANY_VALUE(s.`Item Name`) AS item_name,
      MAX(sa.`category_name`) AS category,
      SUM(COALESCE(s.`Opening Stock`, 0)) AS opening_balance,
      SUM(COALESCE(t.quantity_transfer, 0)) AS transfers_in,
      SUM(COALESCE(t_out.quantity_transfer, 0)) AS transfers_out,
      SUM(COALESCE(t.quantity_transfer, 0) - COALESCE(t_out.quantity_transfer, 0)) AS net_transfers,
      SUM(COALESCE(sa.quantity_sold, 0)) AS sales_current_year,
      SUM(COALESCE(sa.quantity_sold, 0) * COALESCE(sa.rate, 0)) AS sales_value_current_year,
      SUM(COALESCE(sp.quantity_sold, 0)) AS sales_previous_year,
      SUM(COALESCE(sp.quantity_sold, 0) * COALESCE(sp.rate, 0)) AS sales_value_previous_year,
      SUM(COALESCE(s.`Opening Stock`, 0)) + 
      SUM(COALESCE(t.quantity_transfer, 0) - COALESCE(t_out.quantity_transfer, 0)) - 
      SUM(COALESCE(sa.quantity_sold, 0)) AS closing_balance,
      (SUM(COALESCE(s.`Opening Stock`, 0)) + 
       SUM(COALESCE(t.quantity_transfer, 0) - COALESCE(t_out.quantity_transfer, 0)) - 
       SUM(COALESCE(sa.quantity_sold, 0)) - 
       SUM(COALESCE(s.`Closing Stock`, 0))) AS discrepancies
    FROM stock_summary s
    LEFT JOIN (
        SELECT item_name, SUM(quantity_transfer) as quantity_transfer
        FROM transfer_order_details
        WHERE to_warehouse_name = %s
        AND YEAR(date) = YEAR(CURDATE())
        GROUP BY item_name
    ) t ON s.`Item Name` = t.item_name
    LEFT JOIN (
        SELECT item_name, SUM(quantity_transfer) as quantity_transfer
        FROM transfer_order_details
        WHERE from_warehouse_name = %s
        AND YEAR(date) = YEAR(CURDATE())
        GROUP BY item_name
    ) t_out ON s.`Item Name` = t_out.item_name
    LEFT JOIN sales_by_item sa ON s.`Item Name` = sa.item_name
    LEFT JOIN sales_by_item_previous sp ON s.`Item Name` = sp.item_name
    GROUP BY SUBSTRING_INDEX(s.sku, '-', 2)
    ORDER BY SKU;
    """
    try:
        df = pd.read_sql(q, engine, params=(selected_warehouse, selected_warehouse))
    except Exception as e:
        st.error(f"Error: {e}")
        df = pd.DataFrame()
    return df

# ----------------- Main ----------------- #
if st.button("Generate Report"):
    if 'selected_warehouse' in st.session_state and st.session_state.selected_warehouse:
        st.session_state.report_df = generate_general_report(st.session_state.selected_warehouse)
    else:
        st.warning("Please select a warehouse from the sidebar.")

if not st.session_state.report_df.empty:
    rpt = st.session_state.report_df.copy()
    st.subheader(f"📊 General Report for {st.session_state.selected_warehouse}")
    st.dataframe(rpt)

    fmt = st.selectbox("Download General Report As", ["CSV", "Excel", "PDF"], key="fmt_gen")
    if fmt == "CSV":
        st.download_button("📥 Download CSV", df_to_csv_bytes(rpt), "general_report.csv", "text/csv")
    elif fmt == "Excel":
        st.download_button("📥 Download XLSX", df_to_excel_bytes(rpt), "general_report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.download_button("📥 Download PDF", df_to_pdf_bytes(rpt), "general_report.pdf", "application/pdf")

    if st.button("Generate P&S Forecast"):
        today = datetime.now().date()
        year_start = date(today.year, 1, 1)
        days_elapsed = (today - year_start).days
        is_leap_year = today.year % 4 == 0 and (today.year % 100 != 0 or today.year % 400 == 0)
        total_days = 366 if is_leap_year else 365
        year_fraction_elapsed = days_elapsed / total_days
        year_fraction_elapsed = max(year_fraction_elapsed, 0.01)
        year_fraction_elapsed_percent = year_fraction_elapsed * 100
        year_fraction_remaining_percent = (1 - year_fraction_elapsed) * 100

        f = rpt.rename(columns={
            "net_transfers": "transfers",
            "sales_current_year": "sales",
            "sales_previous_year": "previous_sales"
        })[[
            "SKU", "item_name", "category", "opening_balance", "transfers_in", "transfers_out", "transfers",
            "sales", "sales_value_current_year", "previous_sales", "sales_value_previous_year",
            "closing_balance", "discrepancies"
        ]].copy()

        f["max_forecast"] = (f[["sales", "previous_sales"]].max(axis=1) / year_fraction_elapsed_percent * year_fraction_remaining_percent).clip(lower=0).round().astype(int)
        f["min_forecast"] = (f[["sales", "previous_sales"]].min(axis=1) / year_fraction_elapsed_percent * year_fraction_remaining_percent).clip(lower=0).round().astype(int)
        f["avg_forecast"] = ((f["max_forecast"] + f["min_forecast"]) / 2).round().astype(int)

        f["max_transfer_forecast"] = (f["max_forecast"] - f["closing_balance"]).clip(lower=0).round().astype(int)
        f["min_transfer_forecast"] = (f["min_forecast"] - f["closing_balance"]).clip(lower=0).round().astype(int)
        f["avg_transfer_forecast"] = ((f["max_transfer_forecast"] + f["min_transfer_forecast"]) / 2).round().astype(int)

        denom_stock = f["opening_balance"] + f["transfers"]
        f["% Sales on stock"] = np.where(denom_stock == 0, 0, f["sales"] / denom_stock * 100)

        den = f["previous_sales"]
        num = f["sales"]
        f["% sales difference"] = np.where(
            den == 0,
            np.where(num > 0, 1000, 0),
            np.where(
                num == 0,
                np.where(den > 0, -1000, 0),
                (num - den) / den * 100
            )
        )

        f["% Sales on stock"] = f["% Sales on stock"].round(2).astype(str) + "%"
        f["% sales difference"] = f["% sales difference"].round(2).astype(str) + "%"

        st.session_state.forecast_df = f
        st.rerun()

if not st.session_state.forecast_df.empty:
    df = st.session_state.forecast_df.copy()
    st.subheader(f"📈 P&S Forecast for {st.session_state.selected_warehouse}")
    st.dataframe(df)

    fmt2 = st.selectbox("Download P&S Forecast As", ["CSV", "Excel", "PDF"], key="fmt_ps")
    if fmt2 == "CSV":
        st.download_button("📥 Download CSV", df_to_csv_bytes(df), "ps_forecast.csv", "text/csv")
    elif fmt2 == "Excel":
        st.download_button("📥 Download XLSX", df_to_excel_bytes(df), "ps_forecast.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.download_button("📥 Download PDF", df_to_pdf_bytes(df), "ps_forecast.pdf", "application/pdf")

    cat_sales = df.groupby("category")["sales"].sum().reset_index()
    fig1 = px.pie(cat_sales, names="category", values="sales", title=f"Current Year Sales by Category for {st.session_state.selected_warehouse}")
    st.plotly_chart(fig1, use_container_width=True)

    cat_prev = df.groupby("category")["previous_sales"].sum().reset_index()
    fig2 = px.pie(cat_prev, names="category", values="previous_sales", title=f"Previous Year Sales by Category for {st.session_state.selected_warehouse}")
    st.plotly_chart(fig2, use_container_width=True)