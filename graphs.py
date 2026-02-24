import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import os

# Set default matplotlib style and enhance with seaborn for professional look
plt.style.use('default')
sns.set(style="whitegrid", font_scale=1.1)  # Whitegrid style with larger fonts
sns.set_palette("muted")  # Professional, muted color palette
plt.rcParams['font.family'] = 'Arial'  # Professional font
plt.rcParams['axes.titlesize'] = 16  # Title font size
plt.rcParams['axes.labelsize'] = 14  # Axis label font size
plt.rcParams['xtick.labelsize'] = 12  # Tick label font size
plt.rcParams['ytick.labelsize'] = 12
plt.rcParams['figure.dpi'] = 150  # High-quality rendering
plt.rcParams['axes.grid'] = True  # Enable gridlines
plt.rcParams['grid.linestyle'] = '--'  # Subtle gridlines
plt.rcParams['grid.alpha'] = 0.7  # Gridline transparency

# Define file path
file_path = "ps_forecast Year to Date December week 2 2025.xlsx"

# Check if the file exists
if not os.path.exists(file_path):
    print(f"Error: The file '{file_path}' was not found. Please ensure it is in the correct directory.")
    exit(1)

try:
    # Load the Excel file
    df = pd.read_excel(file_path)
except Exception as e:
    print(f"Error loading Excel file: {e}")
    exit(1)

# Print column names for debugging
print("Column names in the Excel file before renaming:")
print(df.columns.tolist())

# Define expected column names (including those with trailing spaces)
expected_columns = {
    'Sales Current Year': ['Sales Current Year  ', 'Sales Current Year', 'Current Year Sales', 'Sales 2025', 'sales current year'],
    'Sales Value Current Year': ['Sales Value Current Year', 'Sales Value 2025', 'sales value current year'],
    '% Sales Difference': ['% Sales Difference', 'Sales Growth %', 'Sales Difference', '% Growth', 'sales difference'],
    'Category': ['Category', 'category', 'Product Category'],
    'SKU': ['SKU', 'sku', 'Product Code'],
    'Item Name': ['Item Name', 'item name', 'Product Name'],
    'Max Forecast': ['Max Forecast', 'max forecast', 'Sales Max Forecast'],
    'Min Forecast': ['Min Forecast', 'min forecast', 'Sales Min Forecast'],
    'AVG Forecast': ['AVG Forecast', 'avg forecast', 'Sales AVG Forecast'],
    'Closing Balance': ['Closing Balance', 'closing balance', 'Closing Stock', 'Stock Balance', 'Available Stock']
}

# Map actual column names to expected ones, stripping whitespace
column_mapping = {}
for expected, possible_names in expected_columns.items():
    for col in df.columns:
        if col.strip().lower() in [name.strip().lower() for name in possible_names]:
            column_mapping[col] = expected
            break
    if expected not in [v for v in column_mapping.values()]:
        print(f"Error: Column for '{expected}' not found. Possible names: {possible_names}")
        exit(1)

# Rename columns in DataFrame to match expected names
df = df.rename(columns=column_mapping)

# Print column names after renaming for debugging
print("Column names in the DataFrame after renaming:")
print(df.columns.tolist())

# Ensure numeric columns are properly formatted
try:
    numeric_columns = ['Sales Current Year', 'Sales Value Current Year', '% Sales Difference', 
                       'Max Forecast', 'Min Forecast', 'AVG Forecast', 'Closing Balance']
    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
except KeyError as e:
    print(f"KeyError: {e}. Please check if the column renaming was successful.")
    exit(1)

df['Category'] = df['Category'].fillna('Unknown')  # Handle missing categories

# Create output directory for saving charts
output_dir = "visuals"
if not os.path.exists(output_dir):
    os.makedirs(output_dir)

# 1. Top 20 Best-Performing Items with Stock Available (by Sales Value Current Year)
top_20_best_with_stock = df[df['Closing Balance'] > 0].nlargest(20, 'Sales Value Current Year')[['SKU', 'Item Name', 'Sales Current Year', 'Sales Value Current Year', 'Closing Balance']]
plt.figure(figsize=(14, 10))
bars = plt.barh(top_20_best_with_stock['Item Name'], top_20_best_with_stock['Sales Value Current Year'], color='#1f77b4')
plt.xlabel('Sales Value (RWF) - 2025', weight='bold')
plt.title('Top 20 Best-Performing Items with Stock Available', weight='bold', pad=20)
plt.gca().invert_yaxis()

# Add data labels with both sales value (RWF), units, and stock
for i, bar in enumerate(bars):
    width = bar.get_width()
    units = top_20_best_with_stock['Sales Current Year'].iloc[i]
    stock = top_20_best_with_stock['Closing Balance'].iloc[i]
    plt.text(width + 10, bar.get_y() + bar.get_height()/2, f'RWF {width:,.0f}\n({int(units)} units, {int(stock)} in stock)', 
             va='center', ha='left', fontsize=10, color='black')

plt.tight_layout()
plt.savefig(os.path.join(output_dir, 'top_20_best_performing_with_stock.png'), dpi=300, bbox_inches='tight')
plt.close()

# 2. Top 20 Least-Performing Items with Non-Zero Sales and Stock Available (Bar Chart)
least_20_non_zero_with_stock = df[(df['Sales Value Current Year'] > 0) & (df['Closing Balance'] > 0)].nsmallest(20, 'Sales Value Current Year')[['SKU', 'Item Name', 'Sales Current Year', 'Sales Value Current Year', 'Closing Balance']]
plt.figure(figsize=(14, 10))
bars = plt.barh(least_20_non_zero_with_stock['Item Name'], least_20_non_zero_with_stock['Sales Value Current Year'], color='#ff7f0e')
plt.xlabel('Sales Value (RWF) - 2025', weight='bold')
plt.title('Top 20 Least-Performing Items with Stock Available (Non-Zero Sales)', weight='bold', pad=20)
plt.gca().invert_yaxis()

# Add data labels with both sales value (RWF), units, and stock
for i, bar in enumerate(bars):
    width = bar.get_width()
    units = least_20_non_zero_with_stock['Sales Current Year'].iloc[i]
    stock = least_20_non_zero_with_stock['Closing Balance'].iloc[i]
    plt.text(width + 2, bar.get_y() + bar.get_height()/2, f'RWF {width:,.0f}\n({int(units)} units, {int(stock)} in stock)', 
             va='center', ha='left', fontsize=10, color='black')

plt.tight_layout()
plt.savefig(os.path.join(output_dir, 'top_20_least_performing_non_zero_with_stock.png'), dpi=300, bbox_inches='tight')
plt.close()

# 3. Top 10 Growing Items (by % Sales Difference applied to Sales Value)
valid_growth = df[(df['% Sales Difference'].between(-5, 5)) & (df['% Sales Difference'] > 0)]
top_10_growth = valid_growth.nlargest(10, '% Sales Difference')[['SKU', 'Item Name', 'Sales Current Year', 'Sales Value Current Year', '% Sales Difference']]
plt.figure(figsize=(14, 8))
bars = plt.barh(top_10_growth['Item Name'], top_10_growth['% Sales Difference'], color='#2ca02c')
plt.xlabel('Sales Growth (%) - Current vs Previous Year', weight='bold')
plt.title('Top 10 Growing Items by Sales Value Growth', weight='bold', pad=20)
plt.gca().invert_yaxis()

# Add data labels with growth percentage and sales value (RWF)
for i, bar in enumerate(bars):
    width = bar.get_width()
    value = top_10_growth['Sales Value Current Year'].iloc[i]
    plt.text(width + 0.02, bar.get_y() + bar.get_height()/2, f'{width:.2%}\n(RWF {value:,.0f})', 
             va='center', ha='left', fontsize=10, color='black')

plt.tight_layout()
plt.savefig(os.path.join(output_dir, 'top_10_growing_items.png'), dpi=300, bbox_inches='tight')
plt.close()

# 4. Best Categories (by Total Sales Value Current Year)
category_sales = df.groupby('Category')[['Sales Current Year', 'Sales Value Current Year']].sum().sort_values(by='Sales Value Current Year', ascending=False).head(10)
if not category_sales.empty:
    plt.figure(figsize=(12, 12))
    colors = sns.color_palette("muted", len(category_sales))
    plt.pie(category_sales['Sales Value Current Year'], labels=category_sales.index.tolist(), autopct='%1.1f%%', startangle=140, colors=colors, 
            textprops={'fontsize': 12, 'weight': 'bold'})
    plt.title('Top Categories by Total Sales Value', weight='bold', pad=20)
    # Add legend with units and value (RWF)
    legend_labels = [f"{cat}: RWF {value:,.0f} ({units:,.0f} units)" 
                     for cat, (units, value) in category_sales.iterrows()]
    plt.legend(legend_labels, title="Categories (Value & Units)", loc="center left", bbox_to_anchor=(1, 0.5), fontsize=10)
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, 'best_categories_pie.png'), dpi=300, bbox_inches='tight')
    plt.close()
else:
    print("Warning: No data available for category sales pie chart.")

# 5. Top Brands (by Total Sales Value Current Year)
df['Brand'] = df['Item Name'].apply(lambda x: x.split()[0] if isinstance(x, str) else 'Unknown')
brand_sales = df.groupby('Brand')[['Sales Current Year', 'Sales Value Current Year']].sum().sort_values(by='Sales Value Current Year', ascending=False).head(10)
plt.figure(figsize=(14, 10))
bars = plt.barh(brand_sales.index, brand_sales['Sales Value Current Year'], color='#9467bd')
plt.xlabel('Sales Value (RWF) - 2025', weight='bold')
plt.title('Top 10 Brands by Total Sales Value', weight='bold', pad=20)
plt.gca().invert_yaxis()

# Add data labels with both sales value (RWF) and units
for i, bar in enumerate(bars):
    width = bar.get_width()
    units = brand_sales['Sales Current Year'].iloc[i]
    plt.text(width + 10, bar.get_y() + bar.get_height()/2, f'RWF {width:,.0f}\n({int(units)} units)', 
             va='center', ha='left', fontsize=10, color='black')

plt.tight_layout()
plt.savefig(os.path.join(output_dir, 'top_10_brands.png'), dpi=300, bbox_inches='tight')
plt.close()

# 6. Line Graph: Sales Forecasts for Top 10 Items by Sales Value
top_10_items = df.nlargest(10, 'Sales Value Current Year')[['Item Name', 'Max Forecast', 'Min Forecast', 'AVG Forecast']]
plt.figure(figsize=(14, 8))

# Plot sales forecasts
plt.plot(top_10_items['Item Name'], top_10_items['Max Forecast'], label='Sales Max Forecast', color='#1f77b4', linestyle='--', marker='o')
plt.plot(top_10_items['Item Name'], top_10_items['Min Forecast'], label='Sales Min Forecast', color='#1f77b4', linestyle=':', marker='s')
plt.plot(top_10_items['Item Name'], top_10_items['AVG Forecast'], label='Sales AVG Forecast', color='#1f77b4', linestyle='-', marker='^')

plt.xlabel('Items', weight='bold')
plt.ylabel('Sales Forecast (Units)', weight='bold')
plt.title('Sales Forecasts for Top 10 Items by Sales Value', weight='bold', pad=20)
plt.xticks(rotation=45, ha='right')
plt.legend(loc='upper right', fontsize=10)
plt.tight_layout()
plt.savefig(os.path.join(output_dir, 'sales_forecasts_line_graph.png'), dpi=300, bbox_inches='tight')
plt.close()

print("Visualizations generated and saved in the visuals directory.")