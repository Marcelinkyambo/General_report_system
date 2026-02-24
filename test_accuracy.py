import pandas as pd, numpy as np, re
from pathlib import Path
from datetime import datetime

FILES = Path(r'C:\Users\itwineclub\Desktop\Report\files')
def _sku_prefix(sku, n=6):
    digits = re.sub(r'[^0-9]', '', str(sku))
    return digits[:n] if digits else ''

def _safe_numeric(series):
    return pd.to_numeric(series, errors='coerce').fillna(0)

# Load data
cy = pd.read_csv(FILES / 'Sales by Item YTD Feb week 2 26.csv', encoding='utf-8-sig')
py = pd.read_csv(FILES / 'Sales by Item YTD Feb week 2 25.csv', encoding='utf-8-sig')
stk = pd.read_csv(FILES / 'Stock Summary Report YTD Feb week 2 26.csv', encoding='utf-8-sig')
cy.columns = cy.columns.str.strip().str.lower()
py.columns = py.columns.str.strip().str.lower()
stk.columns = stk.columns.str.strip().str.lower()

# Clean and group
cy = cy[cy['sku'].notna()].copy()
py = py[py['sku'].notna()].copy()
stk = stk[stk['sku'].notna()].copy()
cy['sku_group'] = cy['sku'].apply(_sku_prefix)
py['sku_group'] = py['sku'].apply(_sku_prefix)
stk['sku_group'] = stk['sku'].apply(_sku_prefix)
cy = cy[cy['sku_group'] != '']
py = py[py['sku_group'] != '']
stk = stk[stk['sku_group'] != '']

# Aggregate
agg_cy = cy.groupby('sku_group', as_index=False).agg(sales=('quantity_sold', 'sum'))
agg_py = py.groupby('sku_group', as_index=False).agg(previous_sales=('quantity_sold', 'sum'))
agg_stk = stk.groupby('sku_group', as_index=False).agg(
    opening_balance=('opening stock', 'sum'),
    purchases=('quantity in', 'sum'),
    closing_balance=('closing stock', 'sum')
)
item_name_map = stk.dropna(subset=['item name']).groupby('sku_group')['item name'].first().rename('item_name')

# Merge
merged = agg_cy.merge(agg_py, on='sku_group', how='outer').merge(agg_stk, on='sku_group', how='outer').fillna(0)
merged = merged.merge(item_name_map, on='sku_group', how='left')
merged['item_name'] = merged['item_name'].fillna('')

# Safe numeric
for c in ['sales', 'previous_sales', 'opening_balance', 'purchases', 'closing_balance']:
    merged[c] = _safe_numeric(merged[c]).round(0).astype(int)

# Discrepancies
merged['discrepancies'] = merged['closing_balance'] - (merged['opening_balance'] + merged['purchases'] - merged['sales'])

# Load planning
planning_path = Path(r'C:\Users\itwineclub\Desktop\Report\Sales Stock Planning 2026, M.xlsx')
if planning_path.exists():
    df_planning = pd.read_excel(planning_path)
    df_planning.columns = df_planning.columns.str.strip().str.lower()
    df_planning['sku_group'] = df_planning['sku'].apply(_sku_prefix)
    planning_agg = df_planning.groupby('sku_group', as_index=False).agg(
        budget=('next year budget', 'sum'),
        growth=('growth target %', 'mean')
    )
else:
    planning_agg = pd.DataFrame(columns=['sku_group', 'budget', 'growth'])

merged = merged.merge(planning_agg, on='sku_group', how='left')
merged['budget'] = _safe_numeric(merged['budget']).fillna(0)
merged['growth'] = _safe_numeric(merged['growth']).fillna(0)

# Year progress
today = datetime.today()
elapsed_days = (today - datetime(today.year, 1, 1)).days + 1
total_days = (datetime(today.year, 12, 31) - datetime(today.year, 1, 1)).days + 1
elapsed_percent = elapsed_days / total_days
remaining_percent = 1 - elapsed_percent
safe_elapsed = max(elapsed_percent, 1e-9)

# Forecasts
merged['max_forecast_old'] = (merged[['sales', 'previous_sales']].max(axis=1) / safe_elapsed * remaining_percent).clip(lower=0).round(0).astype(int)
merged['min_forecast_old'] = (merged[['sales', 'previous_sales']].min(axis=1) / safe_elapsed * remaining_percent).clip(lower=0).round(0).astype(int)

merged['remaining_budget'] = (merged['budget'] - merged['sales']).clip(lower=0).round(0).astype(int)
merged['max_forecast'] = merged.apply(lambda row: row['remaining_budget'] if row['budget'] > 0 else row['max_forecast_old'], axis=1).astype(int)
merged['min_forecast'] = merged.apply(lambda row: max(0, row['remaining_budget'] * (1 - row['growth'])) if row['budget'] > 0 else row['min_forecast_old'], axis=1).astype(int)
merged['avg_forecast'] = ((merged['max_forecast'] + merged['min_forecast']) / 2).round(0).astype(int)

# Purchase forecasts
merged['max_purchase_forecast'] = (merged['max_forecast'] - merged['closing_balance']).clip(lower=0).round(0).astype(int)
merged['min_purchase_forecast'] = (merged['min_forecast'] - merged['closing_balance']).clip(lower=0).round(0).astype(int)
merged['avg_purchase_forecast'] = ((merged['max_purchase_forecast'] + merged['min_purchase_forecast']) / 2).round(0).astype(int)

# Final
final = merged[['sku_group', 'item_name', 'sales', 'previous_sales', 'opening_balance', 'purchases', 'closing_balance', 'discrepancies', 'max_forecast', 'min_forecast', 'avg_forecast', 'max_purchase_forecast', 'min_purchase_forecast', 'avg_purchase_forecast']].sort_values('sku_group').reset_index(drop=True)

print('Final Report Summary:')
print(f'Total SKU groups: {len(final)}')
print(f'Year elapsed: {elapsed_percent:.2%}')
print(f'Groups with budget data: {(merged["budget"] > 0).sum()}')
print(f'Groups with discrepancies != 0: {(final["discrepancies"] != 0).sum()}')
print()
print('Column Totals:')
print(f'Sales: {final["sales"].sum()}')
print(f'Previous Sales: {final["previous_sales"].sum()}')
print(f'Opening Balance: {final["opening_balance"].sum()}')
print(f'Purchases: {final["purchases"].sum()}')
print(f'Closing Balance: {final["closing_balance"].sum()}')
print(f'Discrepancies: {final["discrepancies"].sum()}')
print(f'Max Forecast: {final["max_forecast"].sum()}')
print(f'Min Forecast: {final["min_forecast"].sum()}')
print(f'Avg Forecast: {final["avg_forecast"].sum()}')
print()
print('Sample rows:')
print(final.head(10).to_string(index=False))