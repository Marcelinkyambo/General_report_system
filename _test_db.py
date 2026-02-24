"""Quick DB test for new_app.py logic."""
import pandas as pd, numpy as np, re
import mysql.connector

def connect_to_database():
    return mysql.connector.connect(host="localhost",user="root",password="Root@123",database="data_analysis")

def _normalize_sku(sku):
    if sku is None or (isinstance(sku, float) and np.isnan(sku)): return ""
    s = str(sku).strip()
    if s.endswith(".0"): s = s[:-2]
    return s

def _sku_prefix(sku, n=6):
    digits = re.sub(r"[^0-9]", "", str(sku))
    return digits[:n] if digits else str(sku).strip()[:n]

def _to_number(s):
    return pd.to_numeric(s, errors="coerce").fillna(0)

def _fetch(t):
    conn = connect_to_database()
    cur = conn.cursor(dictionary=True)
    cur.execute(f"SELECT * FROM `{t}`")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return pd.DataFrame(rows)

stock = _fetch("stock_summary")
purch = _fetch("purchases_by_item")
sales_cy = _fetch("sales_by_item")
sales_py = _fetch("sales_by_item_previous")

print(f"stock={len(stock)}, purch={len(purch)}, sales_cy={len(sales_cy)}, sales_py={len(sales_py)}")

stock["SKU"] = stock["sku"].apply(_normalize_sku)
stock["SKU_prefix"] = stock["SKU"].apply(_sku_prefix)
stock["item_id"] = stock["Item ID"].astype(str).str.strip()
stock["opening_balance"] = _to_number(stock["Opening Stock"])
stock["closing_stock"] = _to_number(stock["Closing Stock"])

_id_to_prefix = stock[stock["item_id"]!=""][["item_id","SKU_prefix"]].drop_duplicates("item_id")

stock_grouped = stock.groupby("SKU_prefix", as_index=False).agg(
    opening_balance=("opening_balance","sum"), closing_stock=("closing_stock","sum"))
print(f"stock_grouped={len(stock_grouped)}")

# Purchases
purch["SKU"] = purch["sku"].apply(_normalize_sku)
purch["SKU_prefix"] = purch["SKU"].apply(_sku_prefix)
purch["purchase"] = _to_number(purch["quantity_purchased"])
pg = purch.groupby("SKU_prefix", as_index=False).agg(purchase=("purchase","sum"))
print(f"purch_grouped={len(pg)}")

# Sales CY — use sku if available, else item_id mapping
sc = sales_cy.copy()
has_sku = "sku" in sc.columns and sc["sku"].notna().any()
print(f"Sales CY has sku column with data: {has_sku}")
if has_sku:
    sc["SKU"] = sc["sku"].apply(_normalize_sku)
    sc["SKU_prefix"] = sc["SKU"].apply(_sku_prefix)
    sc = sc[sc["SKU"] != ""]
else:
    sc["item_id"] = sc["item_id"].astype(str).str.strip()
    sc = sc.merge(_id_to_prefix, on="item_id", how="left")
    sc = sc[sc["SKU_prefix"].notna()]
sc["qty"] = _to_number(sc["quantity_sold"])
sc["amt"] = _to_number(sc["amount"])
scg = sc.groupby("SKU_prefix", as_index=False).agg(qty=("qty","sum"), amt=("amt","sum"))
print(f"sales_cy_grouped={len(scg)}, total_qty={scg['qty'].sum()}, total_amt={scg['amt'].sum()}")

# Sales PY
sp = sales_py.copy()
has_sku_py = "sku" in sp.columns and sp["sku"].notna().any()
print(f"Sales PY has sku column with data: {has_sku_py}")
if has_sku_py:
    sp["SKU"] = sp["sku"].apply(_normalize_sku)
    sp["SKU_prefix"] = sp["SKU"].apply(_sku_prefix)
    sp = sp[sp["SKU"] != ""]
else:
    sp["item_id"] = sp["item_id"].astype(str).str.strip()
    sp = sp.merge(_id_to_prefix, on="item_id", how="left")
    sp = sp[sp["SKU_prefix"].notna()]
sp["qty"] = _to_number(sp["quantity_sold"])
sp["amt"] = _to_number(sp["amount"])
spg = sp.groupby("SKU_prefix", as_index=False).agg(qty=("qty","sum"), amt=("amt","sum"))
print(f"sales_py_grouped={len(spg)}, total_qty={spg['qty'].sum()}, total_amt={spg['amt'].sum()}")

# Merge
df = stock_grouped.merge(pg, on="SKU_prefix", how="left")
df["purchase"] = _to_number(df["purchase"])
df = df.merge(scg.rename(columns={"qty":"sales_cy","amt":"sales_val_cy"}), on="SKU_prefix", how="left")
df = df.merge(spg.rename(columns={"qty":"sales_py","amt":"sales_val_py"}), on="SKU_prefix", how="left")
for c in ["sales_cy","sales_val_cy","sales_py","sales_val_py"]:
    df[c] = _to_number(df[c])
df["closing_balance"] = df["closing_stock"].round(0)
expected = (df["opening_balance"] + df["purchase"] - df["sales_cy"]).round(0)
df["disc"] = (expected - df["closing_balance"]).round(0)

print(f"\nfinal_rows={len(df)}")
print(f"NaN: purchase={df['purchase'].isna().sum()}, sales_cy={df['sales_cy'].isna().sum()}")
print(f"Totals: opening={df['opening_balance'].sum()}, purchase={df['purchase'].sum()}, sales_cy={df['sales_cy'].sum()}, sales_val_cy={df['sales_val_cy'].sum()}, closing={df['closing_balance'].sum()}")
print(f"Totals: sales_py={df['sales_py'].sum()}, sales_val_py={df['sales_val_py'].sum()}")
