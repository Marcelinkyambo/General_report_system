import streamlit as st
import mysql.connector
import pandas as pd
import io
import plotly.express as px

# ----------------- Database Connection ----------------- #
def connect_to_database():
    connection = mysql.connector.connect(
        host="localhost",
        user="root",
        password="Root@123",  # 🔥 Replace with your password
        database="inventory_db",  # 🔥 Replace with your database
        #auth_plugin="mysql_native_password"  # Specify the authentication plugin
    )
    return connection

# ----------------- General Report Query ----------------- #
def generate_general_report():
    conn = connect_to_database()

    # Updated query to generate the general report with an extra column for previous sales and comparison percentage
    query = """ 
    SELECT
      SUBSTRING_INDEX(s.sku, '-', 2) AS SKU,
      ANY_VALUE(COALESCE(s.`Item Name`, p.item_name, sa.item_name)) AS item_name,
      SUM(COALESCE(s.`Opening Stock`, 0)) AS opening_balance,
      SUM(COALESCE(p.quantity_purchased, 0)) AS purchase,
      SUM(COALESCE(sa.quantity_sold, 0)) AS sales_current_year,
      SUM(COALESCE(s.`Quantity Out`, 0) - COALESCE(sa.quantity_sold, 0)) AS write_offs_discrepancies,
      SUM(s.`Opening Stock`)
      + SUM(COALESCE(p.quantity_purchased, 0))
      - SUM(COALESCE(sa.quantity_sold, 0)) AS closing_balance,
      SUM(COALESCE(sp.quantity_sold, 0)) AS sales_previous_year,  -- New column for previous sales
      CASE 
        WHEN SUM(COALESCE(sp.quantity_sold, 0)) = 0 THEN NULL
        ELSE ROUND(
          (SUM(COALESCE(sa.quantity_sold, 0)) - SUM(COALESCE(sp.quantity_sold, 0)))
          / SUM(COALESCE(sp.quantity_sold, 0)) * 100,
          2
        )
      END AS sales_comparison_percentage  -- New column for sales comparison
    FROM stock_summary AS s
    LEFT JOIN purchases_by_item AS p 
      ON s.sku = p.sku
    LEFT JOIN sales_by_item AS sa 
      ON s.`Item ID` = sa.item_id
    LEFT JOIN sales_by_item_previous AS sp
      ON sa.item_id = sp.item_id
    GROUP BY 
      SUBSTRING_INDEX(s.sku, '-', 2)
    ORDER BY 
      SUBSTRING_INDEX(s.sku, '-', 2);
    """

    # Execute the query
    df = pd.read_sql(query, conn)

    conn.close()
    return df

# ----------------- CSV Upload to Table ----------------- #
def upload_csv_to_table(uploaded_file, table_name, year_option):
    conn = connect_to_database()
    cursor = conn.cursor()
    df = pd.read_csv(uploaded_file)
    df = df.where(pd.notna(df), None)  # Replace NaN with None for the entire DataFrame

    # Determine the target table based on the year option
    if year_option == "Previous Year":
        table_name = f"{table_name}_previous"

    print(f"Year Option: {year_option}")  # Debugging log
    print(f"Target Table: {table_name}")  # Debugging log

    # Check if the DataFrame is empty
    if df.empty:
        st.warning("⚠️ The uploaded CSV file is empty. Please upload a valid file.")
        return

    print(f"Number of rows in CSV: {len(df)}")  # Debugging log
    print(f"DataFrame Columns: {df.columns.tolist()}")  # Debugging log

    for _, row in df.iterrows():
        cols = ",".join([f"`{col}`" for col in row.index])  # Wrap columns with backticks
        placeholders = ",".join(["%s"] * len(row))
        sql = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"
        try:
            cursor.execute(sql, tuple(row))
        except mysql.connector.Error as err:
            print(f"Error: {err}")  # Log the error
            print(f"SQL: {sql}")
            print(f"Data: {tuple(row)}")
            st.error(f"⚠️ Error inserting data into {table_name}: {err}")
        else:
            print(f"Inserted row: {tuple(row)}")  # Log successful insertion
    conn.commit()
    cursor.close()
    conn.close()

# ----------------- Delete All Data From Table ----------------- #
def delete_all_data_from_table(table_name):
    conn = connect_to_database()
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM {table_name}")
    conn.commit()
    cursor.close()
    conn.close()

# ----------------- Clear Entire Database (Tables) ----------------- #
def clear_database():
    # List of all tables, including previous year tables
    tables = [
        "stock_summary", "sales_by_item", "purchases_by_item",
        "stock_summary_previous", "sales_by_item_previous", "purchases_by_item_previous"
    ]
    for table in tables:
        delete_all_data_from_table(table)

# ----------------- Streamlit App ----------------- #
st.set_page_config(page_title="General Report System", layout="wide")
st.title("📦 General Report Dashboard")

# Sidebar Actions
with st.sidebar:
    st.header("📤 Import CSV to Table")
    # Dropdown to select the base table
    base_table_option = st.selectbox("Select table", ["stock_summary", "sales_by_item", "purchases_by_item"])
    # Dropdown to select the year (Current Year or Previous Year)
    year_option = st.selectbox("Select Report Year", ["Current Year", "Previous Year"])

    # Determine the target table based on the year option
    table_option = base_table_option if year_option == "Current Year" else f"{base_table_option}_previous"

    # File uploader for the CSV file
    uploaded_file = st.file_uploader(f"Upload CSV File for {year_option} ({table_option})", type=["csv"])

    # Button to upload the file to the database
    if st.button("Upload to Database"):
        if uploaded_file is not None:
            # Pass the year option to the upload function
            upload_csv_to_table(uploaded_file, base_table_option, year_option)
            st.success(f"✅ Uploaded successfully to {table_option} for {year_option}!")
        else:
            st.warning("⚠️ Please upload a CSV file first.")

    # Delete options
    st.header("🗑️ Delete Options")
    if st.button(f"Delete All Data in {table_option}"):
        delete_all_data_from_table(table_option)
        st.success(f"✅ All records deleted from {table_option}!")

    if st.button("Clear Entire Database"):
        clear_database()
        st.success("✅ All tables cleared successfully!")

# Main Report Display
st.header("📊 General Report")

if st.button("Generate Report"):
    report_df = generate_general_report()
    st.dataframe(report_df)

    # Download Button
    csv = report_df.to_csv(index=False)
    st.download_button(
        label="📥 Download Report as CSV",
        data=csv,
        file_name="general_report.csv",
        mime="text/csv",
    )

    # Optional Graph Section
    st.subheader("📈 Sales Comparison (Current Year vs Previous Year)")
    if "sales_comparison_percentage" in report_df.columns:
        fig = px.bar(
            report_df,
            x="item_name",  # X-axis: Item names
            y="sales_comparison_percentage",  # Y-axis: Sales comparison percentage
            title="Sales Comparison (Current Year vs Previous Year)",
            labels={"sales_comparison_percentage": "Sales Comparison (%)", "item_name": "Item Name"},
            text="sales_comparison_percentage"  # Display percentage values on the bars
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("⚠️ The 'sales_comparison_percentage' column is missing from the report.")
