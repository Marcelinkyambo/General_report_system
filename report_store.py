"""Publish a complete report snapshot with a single atomic manifest change."""
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
from report_inputs import health_label, sku_to_group_key
from report_logic import WEEKS_PER_MONTH


# Public output is exactly the supplied report's 26 columns, in that order.
REPORT_COLUMNS = {
    "SKU": "SKU", "Item Name": "Item Name", "Category": "Category", "Country": "Country", "Brand": "Brand",
    "CY Sales (YTD)": "Current Year Sales YTD", "PY Sales (YTD)": "Previous Year Sales YTD",
    "YoY Sales %": "YoY Sales Diff %", "CY Revenue (YTD)": "Current Year Revenue (YTD)",
    "Weekly Run Rate": "Avg Weekly Sales", "Annual Target": "Annual Target", "Expected YTD": "Expected YTD Sales",
    "Sales Pace %": "Current Year Sales Vs. Target %", "Sales Health": "Sales Performance",
    "Total Stock": "Current Total Stock", "TWC Stock": "Cleared Stock", "Bonded Stock": "Bonded Stock", "In Transit": "In Transit",
    "Stock Coverage (Wk)": "Weeks Coverage", "TWC Coverage (Wk)": "Cleared Stock Weeks Coverage", "Stockout Risk": "Stockout Risk",
    "Bonded Reorder": "Reorder from Supplier", "Bonded Est. Reorder Qty": "Qty to order from Supplier", "Ordered Qty (PO)": "Ordered Qty (PO)",
    "TWC Reorder": "Clear bonded stock", "TWC Est. Reorder Qty": "Qty to be cleared from Bond",
}
AUDIT_COLUMNS = ["PO Number", "PO Date", "Supplier", "PO Approval", "Line #", "Brand", "Description", "Ordered Qty",
                 "Matched SKU", "Matched Report Item", "Status", "Confidence", "Effect on Report", "Notes"]


def weekly_report_view(report):
    """Use the reference's labels and order in both the full table and export."""
    if "CY Sales (YTD)" not in report:
        return report.copy()
    missing = set(REPORT_COLUMNS) - set(report.columns)
    if missing:
        raise ValueError("Weekly report is missing template columns: " + ", ".join(sorted(missing)))
    view = report.sort_values(["CY Sales (YTD)", "SKU"], ascending=[False, True], kind="stable")
    return view[[c for c in REPORT_COLUMNS if c in view]].rename(columns=REPORT_COLUMNS)


# Cell styles shared by the Excel export and the dashboard: (fill, text colour, bold).
STYLES = {
    "good": ("C6EFCE", "006100", False), "neutral": ("FFEB9C", "9C5700", False),
    "warn": ("FCE4D6", "C65911", False), "bad": ("FFC7CE", "9C0006", False),
    "info": ("DDEBF7", "1F4E78", False), "reorder": ("C00000", "FFFFFF", True),
    "clear": ("1F4E78", "FFFFFF", True), "reorder_qty": ("FFC7CE", "9C0006", True),
    "clear_qty": ("DDEBF7", "1F4E78", True), "up": (None, "006100", False),
    "down": (None, "C00000", False), "muted": (None, "A6A6A6", False),
}
COLOUR_KEY = [  # (style, example, column, meaning)
    ("good", "Ahead", "Sales Performance / vs Target", "10% or more above the expected YTD sales"),
    ("neutral", "On Track", "Sales Performance / vs Target", "Within 10% of the expected YTD sales"),
    ("warn", "Behind", "Sales Performance / vs Target", "10-30% below the expected YTD sales"),
    ("bad", "Critical", "Sales Performance / vs Target", "More than 30% below the expected YTD sales"),
    ("up", "12.0%", "YoY Sales Diff %", "Selling more than the same period last year"),
    ("down", "-12.0%", "YoY Sales Diff %", "Selling less than the same period last year"),
    ("bad", "3.0", "Weeks Coverage", "4.3 weeks of stock or less (1 month)"),
    ("neutral", "15.0", "Weeks Coverage", "21.7 weeks or less (5 months): inside the supplier reorder window"),
    ("info", "60.0", "Weeks Coverage", "More than 52 weeks of stock: possible overstock"),
    ("bad", "2.0", "Cleared Stock Weeks Coverage", "4.3 weeks of cleared stock or less (1 month)"),
    ("bad", "HIGH", "Stockout Risk", "Stock runs out in under 4 weeks at the current sales pace"),
    ("neutral", "MEDIUM", "Stockout Risk", "Stock runs out in 4-8 weeks"),
    ("reorder", "REORDER", "Reorder from Supplier", "Order from the supplier"),
    ("reorder_qty", "240", "Qty to order from Supplier", "Bottles to order, after billed POs not yet received"),
    ("up", "120", "Ordered Qty (PO)", "Bottles on POs not yet billed (minus bottles of that batch billed on another PO and sold since)"),
    ("clear", "CLEAR", "Clear bonded stock", "Clear stock out of bond"),
    ("clear_qty", "60", "Qty to be cleared from Bond", "Bottles to clear from bond"),
    ("bad", "047-003", "SKU", "Product needs a supplier reorder"),
    ("info", "047-003", "SKU", "Product needs stock cleared from bond"),
    ("muted", "0", "Any number", "Zero"),
]


def attention_styles(view):
    """Style key per cell of a report view (published labels); blank means no style."""
    styles = pd.DataFrame("", index=view.index, columns=view.columns)
    def mark(column, mask, style):
        if column in view:
            styles.loc[mask.fillna(False) & styles[column].eq(""), column] = style
    def number(column):
        return pd.to_numeric(view[column], errors="coerce") if column in view else pd.Series(np.nan, index=view.index)
    def text(column):
        return view[column].fillna("").astype(str) if column in view else pd.Series("", index=view.index)
    health = {"Ahead": "good", "On Track": "neutral", "Behind": "warn", "Critical": "bad"}
    for label, style in health.items():
        mark("Sales Performance", text("Sales Performance").str.contains(label), style)
    pace = number("Current Year Sales Vs. Target %").map(health_label).astype(str)
    for label, style in health.items():
        mark("Current Year Sales Vs. Target %", pace.str.contains(label), style)
    mark("YoY Sales Diff %", number("YoY Sales Diff %") > 0, "up")
    mark("YoY Sales Diff %", number("YoY Sales Diff %") < 0, "down")
    # Thresholds follow the reorder rules: 5 months total cover and 1 month cleared cover, shown in weeks.
    mark("Weeks Coverage", number("Weeks Coverage") <= WEEKS_PER_MONTH, "bad")
    mark("Weeks Coverage", number("Weeks Coverage") <= 5 * WEEKS_PER_MONTH, "neutral")
    mark("Weeks Coverage", number("Weeks Coverage") > 12 * WEEKS_PER_MONTH, "info")
    mark("Cleared Stock Weeks Coverage", number("Cleared Stock Weeks Coverage") <= WEEKS_PER_MONTH, "bad")
    mark("Stockout Risk", text("Stockout Risk").str.contains("HIGH"), "bad")
    mark("Stockout Risk", text("Stockout Risk").str.contains("MEDIUM"), "neutral")
    reorder, clear = text("Reorder from Supplier").eq("REORDER"), text("Clear bonded stock").eq("CLEAR")
    mark("Reorder from Supplier", reorder, "reorder")
    mark("Qty to order from Supplier", number("Qty to order from Supplier") > 0, "reorder_qty")
    mark("Ordered Qty (PO)", number("Ordered Qty (PO)") > 0, "up")
    mark("Clear bonded stock", clear, "clear")
    mark("Qty to be cleared from Bond", number("Qty to be cleared from Bond") > 0, "clear_qty")
    mark("SKU", reorder, "bad")
    mark("SKU", clear, "info")
    for column in view.select_dtypes(include="number"):
        mark(column, view[column].eq(0), "muted")
    return styles


def attention_css(view):
    """The same styles as CSS for a pandas Styler."""
    def css(key):
        if not key:
            return ""
        fill, colour, bold = STYLES[key]
        return (f"background-color: #{fill}; " if fill else "") + f"color: #{colour};" + (" font-weight: bold;" if bold else "")
    return attention_styles(view).map(css)


SUPPLIER_REORDER_COLUMNS = ["SKU", "Item Name", "Brand", "Avg Weekly Sales", "Current Total Stock", "Cleared Stock",
                            "Bonded Stock", "In Transit", "Weeks Coverage", "Stockout Risk", "Reorder from Supplier",
                            "Qty to order from Supplier", "Ordered Qty (PO)"]
CLEAR_FROM_BOND_COLUMNS = ["SKU", "Item Name", "Brand", "Avg Weekly Sales", "Cleared Stock", "Bonded Stock",
                           "Cleared Stock Weeks Coverage", "Stockout Risk", "Clear bonded stock", "Qty to be cleared from Bond"]
# Header colour per report section, so related columns read as one group.
SECTION_FILLS = [("A6A6A6", ["SKU", "Item Name", "Category", "Country", "Brand"]),
                 ("FFE699", ["Current Year Sales YTD", "Previous Year Sales YTD", "YoY Sales Diff %",
                             "Current Year Revenue (YTD)", "Avg Weekly Sales"]),
                 ("D9D2E9", ["Annual Target", "Expected YTD Sales", "Current Year Sales Vs. Target %", "Sales Performance"]),
                 ("C6E0B4", ["Current Total Stock", "Cleared Stock", "Bonded Stock", "In Transit", "Weeks Coverage",
                             "Cleared Stock Weeks Coverage", "Stockout Risk"]),
                 ("F8CBAD", ["Reorder from Supplier", "Qty to order from Supplier", "Ordered Qty (PO)"]),
                 ("9DC3E6", ["Clear bonded stock", "Qty to be cleared from Bond"])]
HEADER_FILLS = {column: fill for fill, columns in SECTION_FILLS for column in columns}


def action_lists(view):
    """Products needing a supplier order or bond clearance, most urgent first."""
    if "Reorder from Supplier" not in view:
        return pd.DataFrame(), pd.DataFrame()
    # Lowest cover first; among equal cover, the fastest sellers run out soonest.
    order = lambda frame, cover: frame.sort_values([cover, "Avg Weekly Sales", "SKU"], ascending=[True, False, True], na_position="last")
    reorders = order(view[view["Reorder from Supplier"].eq("REORDER")], "Weeks Coverage")
    clears = order(view[view["Clear bonded stock"].eq("CLEAR")], "Cleared Stock Weeks Coverage")
    return reorders[SUPPLIER_REORDER_COLUMNS], clears[CLEAR_FROM_BOND_COLUMNS]


def purchase_order_audit(report, audit):
    """Explain each exact line match and its effect on the supplier suggestion."""
    result = audit.copy() if audit is not None else pd.DataFrame()
    if result.empty:
        return result.reindex(columns=AUDIT_COLUMNS)
    if "SKU" in result and "SKU" in report:
        lookup = report.assign(_key=report.SKU.map(sku_to_group_key)).set_index("_key")
        if lookup.index.has_duplicates:
            raise ValueError("Cannot audit POs against duplicate report product keys.")
        keys = result.SKU.map(sku_to_group_key)
        result["Matched SKU"] = keys.map(lookup.SKU)
        result["Matched Report Item"] = keys.map(lookup["Item Name"]) if "Item Name" in lookup else ""
        result["Status"] = keys.map(lambda key: "Matched" if key in lookup.index else "Not in report")
        result["Confidence"] = "Exact item ID"
        for target, source in [("Total Open PO Qty (SKU)", "PO Reorder Credit Qty"),
                               ("Supplier Need Before PO", "Supplier Need Before PO"),
                               ("Supplier Qty After PO", "Bonded Est. Reorder Qty")]:
            if source in lookup:
                result[target] = keys.map(lookup[source])
        def effect(row):
            if row.get("Status") != "Matched":
                return "Product is outside this report selection"
            if row.get("Outstanding Qty", 0) <= 0:
                return "No billed units awaiting receipt"
            before, after, ordered = (row.get(c) for c in ("Supplier Need Before PO", "Supplier Qty After PO", "Total Open PO Qty (SKU)"))
            if pd.isna(before) or pd.isna(after) or pd.isna(ordered):
                return "Open quantity recorded"
            if before <= 0:
                return "No reorder flag; quantity recorded"
            if after <= 0:
                return f"Flag cleared (needed {before:,.0f}, on order {ordered:,.0f})"
            return f"Still flagged; short by {after:,.0f} (needed {before:,.0f})"
        result["Effect on Report"] = result.apply(effect, axis=1)
    if "PO Date" in result:
        result["PO Date"] = pd.to_datetime(result["PO Date"], errors="raise")
        order_columns = [c for c in ["PO Date", "PO Number", "Line #"] if c in result]
        result = result.sort_values(order_columns, ascending=[c == "Line #" for c in order_columns], kind="stable")
    for column in ["PO ID", "Line ID", "Item ID"]:
        if column in result:
            result[column] = result[column].fillna("").astype(str)
    return result


def purchase_order_audit_view(report, audit):
    """Keep additional source fields private; expose only the template's audit columns."""
    return purchase_order_audit(report, audit).reindex(columns=AUDIT_COLUMNS)


def publish_report(data_dir, report, metadata, audit=None, include_excel=False):
    data_dir = Path(data_dir)
    snapshot = data_dir / "snapshots" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8])
    snapshot.mkdir(parents=True)
    # Published CSVs have the same template columns as the workbook. Calculation
    # and source fields are separate private files used to restore the dashboard.
    weekly_report_view(report).to_csv(snapshot / "report.csv", index=False)
    report.to_csv(snapshot / "_report_data.csv", index=False)
    (snapshot / "report_meta.json").write_text(json.dumps(metadata, indent=2, default=_json_number), encoding="utf-8")
    if audit is not None:
        audit = purchase_order_audit(report, audit)
        audit.to_csv(snapshot / "_po_source.csv", index=False)
        purchase_order_audit_view(report, audit).to_csv(snapshot / "po_audit.csv", index=False)
    if include_excel:
        (snapshot / "report.xlsx").write_bytes(report_excel_bytes(report, audit))
    pending = data_dir / (".manifest_" + uuid.uuid4().hex + ".tmp")
    pending.write_text(json.dumps({"snapshot": snapshot.relative_to(data_dir).as_posix()}), encoding="utf-8")
    pending.replace(data_dir / "current_report.json")
    return snapshot


def _json_number(value):
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def load_report(data_dir):
    data_dir = Path(data_dir)
    manifest = data_dir / "current_report.json"
    folder = data_dir
    if manifest.exists():
        relative = json.loads(manifest.read_text(encoding="utf-8"))["snapshot"]
        folder = (data_dir / relative).resolve()
        if not folder.is_relative_to(data_dir.resolve()):
            raise ValueError("Invalid report snapshot location.")
    if not (folder / "report.csv").exists() or not (folder / "report_meta.json").exists():
        return pd.DataFrame(), {}, pd.DataFrame()
    calculation_file = folder / "_report_data.csv"
    report = pd.read_csv(calculation_file if calculation_file.exists() else folder / "report.csv")
    metadata = json.loads((folder / "report_meta.json").read_text(encoding="utf-8"))
    audit_file = folder / "_po_source.csv"
    if not audit_file.exists():
        audit_file = folder / "po_audit.csv"
    audit = pd.read_csv(audit_file, dtype={c: str for c in ["PO ID", "Line ID", "Item ID", "SKU", "Matched SKU"]}) if audit_file.exists() and audit_file.stat().st_size > 3 else pd.DataFrame()
    return report, metadata, audit


def report_excel_bytes(report, audit=None):
    """Export the complete report and PO audit with the reference's layout."""
    buffer = BytesIO()
    export = weekly_report_view(report)
    audit = purchase_order_audit_view(report, audit)
    reorders, clears = action_lists(export)
    report_sheets = {"Weekly Report": export}
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export.to_excel(writer, sheet_name="Weekly Report", index=False)
        if "Reorder from Supplier" in export:
            report_sheets.update({"Supplier Reorders": reorders, "Clear from Bond": clears})
            reorders.to_excel(writer, sheet_name="Supplier Reorders", index=False)
            clears.to_excel(writer, sheet_name="Clear from Bond", index=False)
        audit.to_excel(writer, sheet_name="PO Match Audit", index=False)
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
        body_font = Font(name="Calibri", size=11)
        body_alignment = Alignment(vertical="center")
        cell_styles = {key: (PatternFill("solid", fgColor=fill) if fill else None, Font(name="Calibri", size=11, color=colour, bold=bold))
                       for key, (fill, colour, bold) in STYLES.items()}
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "F2" if sheet.title == "Weekly Report" else "C2" if sheet.title in report_sheets else "D2"
            sheet.auto_filter.ref = sheet.dimensions
            sheet.row_dimensions[1].height = 56
            wide = {"Item Name": 48, "Matched Report Item": 48, "Description": 52, "Supplier": 28,
                    "Category": 22, "Country": 16, "Brand": 24, "Target Source": 32, "Effect on Report": 50,
                    "Notes": 76, "PO Number": 22, "PO Approval": 24, "PO Status": 20,
                    "PO ID": 24, "Line ID": 24, "Item ID": 24, "Billed Quantity Source": 34, "Match": 26, "Confidence": 20}
            for cell in sheet[1]:
                cell.font = Font(name="Calibri", size=11, bold=True, color="000000")
                cell.fill = PatternFill("solid", fgColor=HEADER_FILLS.get(cell.value, "A6A6A6") if sheet.title in report_sheets else "A6A6A6")
                cell.border = Border(bottom=Side(style="thin", color="666666"), right=Side(style="thin", color="666666"))
                cell.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
                sheet.column_dimensions[get_column_letter(cell.column)].width = wide.get(cell.value, 16 if cell.value != "SKU" else 12)
                values = [sheet.cell(row, cell.column).value for row in range(2, sheet.max_row + 1)]
                fractional = any(isinstance(v, (int, float)) and abs(v - round(v)) > 1e-8 for v in values)
                number_format = ("0.0%" if "%" in str(cell.value) else "#,##0.0" if
                                 "Coverage" in str(cell.value) or cell.value in {"Avg Weekly Sales", "Annual Target", "Expected YTD Sales", "Target Base Sales"}
                                 else "#,##0.00" if fractional else "#,##0")
                for row in range(2, sheet.max_row + 1):
                    data = sheet.cell(row, cell.column)
                    data.font = body_font
                    data.alignment = body_alignment
                    if isinstance(data.value, (int, float)):
                        data.number_format = number_format
                    if cell.value == "PO Date":
                        data.number_format = "dd/mm/yyyy"
            if sheet.title in report_sheets:
                # Static cell styles travel with their rows when the sheet is sorted or filtered.
                for (row, column), key in attention_styles(report_sheets[sheet.title].reset_index(drop=True)).stack().items():
                    if key:
                        fill, font = cell_styles[key]
                        data = sheet.cell(row + 2, report_sheets[sheet.title].columns.get_loc(column) + 1)
                        data.font = font
                        if fill:
                            data.fill = fill
        key_sheet = writer.book.create_sheet("Colour Key")
        key_sheet.append(["Colour", "Column", "Meaning"])
        for key, example, column, meaning in COLOUR_KEY:
            key_sheet.append([example, column, meaning])
            fill, font = cell_styles[key]
            sample = key_sheet.cell(key_sheet.max_row, 1)
            sample.font = font
            if fill:
                sample.fill = fill
        for cell in key_sheet[1]:
            cell.font = Font(name="Calibri", size=11, bold=True)
            cell.fill = PatternFill("solid", fgColor="A6A6A6")
        for letter, width in {"A": 12, "B": 32, "C": 64}.items():
            key_sheet.column_dimensions[letter].width = width
    return buffer.getvalue()
