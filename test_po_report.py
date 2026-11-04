from copy import deepcopy
from io import BytesIO
from tempfile import TemporaryDirectory
import unittest

import openpyxl
import pandas as pd

from report_store import AUDIT_COLUMNS, REPORT_COLUMNS, load_report, publish_report, purchase_order_audit, report_excel_bytes
import test_automation
from weekly_pipeline import build_weekly_report
from zoho_books import ZohoError, open_orders, po_summary_is_relevant

EXPECTED_REPORT_HEADERS = [
    'SKU', 'Item Name', 'Category', 'Country', 'Brand', 'Current Year Sales YTD', 'Previous Year Sales YTD',
    'YoY Sales Diff %', 'Current Year Revenue (YTD)', 'Avg Weekly Sales', 'Annual Target', 'Expected YTD Sales',
    'Current Year Sales Vs. Target %', 'Sales Performance', 'Current Total Stock', 'Cleared Stock', 'Bonded Stock',
    'In Transit', 'Weeks Coverage', 'Cleared Stock Weeks Coverage', 'Stockout Risk', 'Reorder from Supplier',
    'Qty to order from Supplier', 'Ordered Qty (PO)', 'Clear bonded stock', 'Qty to be cleared from Bond']
EXPECTED_AUDIT_HEADERS = ['PO Number', 'PO Date', 'Supplier', 'PO Approval', 'Line #', 'Brand', 'Description',
                          'Ordered Qty', 'Matched SKU', 'Matched Report Item', 'Status', 'Confidence', 'Effect on Report', 'Notes']


class PurchaseOrderReportTests(unittest.TestCase):
    def reconcile(self, **quantities):
        items = {"1": {"sku": "047-003-24", "name": "Wine", "brand": "Spier"}}
        line = {"item_id": "1", "line_item_id": "5020021000049722165", "quantity": 100,
                "quantity_billed": 0, "quantity_received": 0, **quantities}
        orders = [{"status": "partially_billed", "purchaseorder_number": "PO-1", "date": "2026-10-30",
                   "vendor_name": "Supplier", "line_items": [line]}]
        return open_orders(orders, [], items)

    def test_billed_column_is_60_and_only_20_unreceived_reduce_reordering(self):
        totals, audit = self.reconcile(quantity_billed=60, quantity_received=40, quantity_cancelled=10)
        self.assertEqual(totals.billed_po_qty.iloc[0], 60)
        self.assertEqual(totals.open_po_qty.iloc[0], 20)
        inputs = test_automation.PipelineAndPublicationTests().inputs()
        before, _ = build_weekly_report(inputs, 26)
        inputs["purchase_orders"] = totals
        report, _ = build_weekly_report(inputs, 26)
        # 100 ordered - 10 cancelled - 60 billed leaves 30 bottles not yet billed.
        self.assertEqual(report["Ordered Qty (PO)"].iloc[0], 30)
        self.assertEqual(report["Bonded Est. Reorder Qty"].iloc[0], before["Bonded Est. Reorder Qty"].iloc[0] - 20)

    def batch_check(self, bills, sales):
        # Monin Caramel: old PO-2025-00074 for 180 was never closed; the bottles came in on PO-2026-00055.
        items = {"1": {"sku": "032-058-0129", "name": "Monin Syrup Caramel 01-2029"}}
        orders = [{"status": "open", "purchaseorder_number": "PO-2025-00074", "date": "2025-11-25",
                   "line_items": [{"item_id": "1", "line_item_id": "L1", "quantity": 180, "quantity_billed": 0, "quantity_received": 0}]}]
        frame = pd.DataFrame(sales, columns=["sku", "transaction_date", "quantity_sold"])
        return open_orders(orders, bills, items, frame)

    def test_unbilled_batch_billed_on_another_po_and_sold_is_not_on_order(self):
        bills = [{"bill_number": "62006257", "date": "2026-01-23",
                  "line_items": [{"item_id": "1", "quantity": 180, "purchaseorder_number": "PO-2026-00055"}]}]
        totals, audit = self.batch_check(bills, [("032-058-0129", "2026-08-28", 180)])
        self.assertEqual(totals.unbilled_po_qty.iloc[0], 0)
        self.assertEqual(audit["Unbilled Qty"].iloc[0], 180)
        self.assertIn("62006257", audit["Batch Bills Elsewhere"].iloc[0])
        self.assertIn("ignored", audit["Notes"].iloc[0])

    def test_unbilled_batch_stays_on_order_without_both_a_bill_elsewhere_and_sales(self):
        elsewhere = [{"bill_number": "B1", "date": "2026-01-23", "line_items": [{"item_id": "1", "quantity": 180, "purchaseorder_number": ""}]}]
        same_po = [{"bill_number": "B2", "date": "2026-01-23", "line_items": [{"item_id": "1", "quantity": 10, "purchaseorder_number": "PO-2025-00074"}]}]
        before_po = [{"bill_number": "B3", "date": "2025-06-01", "line_items": [{"item_id": "1", "quantity": 180, "purchaseorder_number": "PO-X"}]}]
        unknown_po = [{"bill_number": "B4", "date": "2026-01-23", "line_items": [{"item_id": "1", "quantity": 180}]}]
        sold = [("032-058-0129", "2026-08-28", 180)]
        for bills, sales in [(elsewhere, []), ([], sold), (same_po, sold), (before_po, sold), (unknown_po, sold),
                             (elsewhere, [("032-058-0129", "2025-11-01", 180)]), (elsewhere, [("032-058-0129", "2026-08-28", -5)])]:
            self.assertEqual(self.batch_check(bills, sales)[0].unbilled_po_qty.iloc[0], 180)
        # A bill with no PO link still counts as a bill elsewhere.
        self.assertEqual(self.batch_check(elsewhere, sold)[0].unbilled_po_qty.iloc[0], 0)

    def test_only_bottles_covered_by_bills_elsewhere_are_dropped(self):
        # A 6-bottle bill on another PO explains 6 bottles, not the whole 180.
        small = [{"bill_number": "B5", "date": "2026-10-20", "line_items": [{"item_id": "1", "quantity": 6, "purchaseorder_number": "PO-2026-00173"}]}]
        totals, audit = self.batch_check(small, [("032-058-0129", "2026-10-25", 38)])
        self.assertEqual(totals.unbilled_po_qty.iloc[0], 174)
        self.assertIn("6 of 180 unbilled ignored", audit["Notes"].iloc[0])
        self.assertIn("PO-2026-00173 (bill B5)", audit["Notes"].iloc[0])

    def test_api_bill_lines_name_their_po_in_purchaseorder_details(self):
        bills = [{"bill_number": "57302", "date": "2026-01-23", "line_items": [
            {"item_id": "1", "quantity": 180, "purchaseorder_id": "9", "purchaseorder_details": {"purchaseorder_number": "PO-2026-00055"}}]}]
        self.assertEqual(self.batch_check(bills, [("032-058-0129", "2026-08-28", 1)])[0].unbilled_po_qty.iloc[0], 0)

    def test_complete_receipt_leaves_no_reorder_credit(self):
        totals, _ = self.reconcile(quantity_received=100)
        self.assertEqual(totals.open_po_qty.iloc[0], 0)

    def test_fully_billed_unreceived_quantity_is_credited(self):
        totals, _ = self.reconcile(quantity_billed=100)
        self.assertEqual(totals.open_po_qty.iloc[0], 100)

    def test_manual_receipts_reduce_credit_and_cancellation_does_not_reduce_billed_units(self):
        totals, audit = self.reconcile(quantity_billed=80, quantity_received=20, quantity_marked_as_received=30, quantity_cancelled=10)
        self.assertEqual(totals.open_po_qty.iloc[0], 30)
        self.assertEqual(audit["Manually Received Qty"].iloc[0], 30)

    def test_overbilled_and_fractional_quantities(self):
        self.assertEqual(self.reconcile(quantity_billed=110)[0].billed_po_qty.iloc[0], 110)
        self.assertEqual(self.reconcile(quantity=10.5, quantity_billed=7.25, quantity_received=3.25)[0].open_po_qty.iloc[0], 4)

    def test_missing_receipts_block_billed_credit_but_not_zero_billed_lines(self):
        totals, audit = self.reconcile(quantity_received=None)
        self.assertEqual(totals.open_po_qty.iloc[0], 0)
        self.assertTrue(pd.isna(audit["Received Qty"].iloc[0]))
        with self.assertRaisesRegex(ZohoError, "missing received quantities"):
            self.reconcile(quantity_billed=60, quantity_received=None)
        with self.assertRaisesRegex(ZohoError, "negative PO quantity"):
            self.reconcile(quantity_billed=-1)

    def test_multiple_orders_and_vintages_sum_once_and_duplicate_lines_fail(self):
        items = {"1": {"sku": "047-003-24"}, "2": {"sku": "047-003-23"}}
        orders = [{"status": "open", "line_items": [{"item_id": "1", "line_item_id": "a", "quantity": 12, "quantity_received": 2},
                                                     {"item_id": "2", "line_item_id": "b", "quantity": 6, "quantity_received": 0}]}]
        totals, _ = open_orders(orders, [], items)
        self.assertEqual(totals.open_po_qty.tolist(), [0])
        with self.assertRaisesRegex(ZohoError, "repeated PO line ID"):
            open_orders(orders + deepcopy(orders), [], items)

    def test_receipts_change_credit_without_changing_billed_column_or_warehouse_stock(self):
        inputs = test_automation.PipelineAndPublicationTests().inputs()
        inputs["purchase_orders"], _ = self.reconcile(quantity_billed=60, quantity_received=0)
        before, _ = build_weekly_report(inputs, 26)
        inputs["purchase_orders"], audit = self.reconcile(quantity_billed=60, quantity_received=40)
        report, _ = build_weekly_report(inputs, 26)
        self.assertEqual(before["Ordered Qty (PO)"].iloc[0], report["Ordered Qty (PO)"].iloc[0])
        self.assertEqual(report["Bonded Est. Reorder Qty"].iloc[0], before["Bonded Est. Reorder Qty"].iloc[0] + 40)
        self.assertEqual(before["Total Stock"].iloc[0], report["Total Stock"].iloc[0])
        decorated = purchase_order_audit(report, audit)
        self.assertIn("Still flagged", decorated["Effect on Report"].iloc[0])

    def test_audit_and_report_reconcile_by_sku_without_changing_stock(self):
        inputs = test_automation.PipelineAndPublicationTests().inputs()
        inputs["purchase_orders"], audit = self.reconcile(quantity=1000, quantity_billed=1000, quantity_received=40)
        report, _ = build_weekly_report(inputs, 26)
        decorated = purchase_order_audit(report, audit)
        self.assertEqual(report["Ordered Qty (PO)"].sum(), audit["Unbilled Qty"].sum())
        self.assertEqual(report["PO Reorder Credit Qty"].sum(), audit["Outstanding Qty"].sum())
        self.assertEqual(decorated["Matched SKU"].tolist(), ["047-003"])
        self.assertIn("Flag cleared", decorated["Effect on Report"].iloc[0])
        self.assertEqual(report["Total Stock"].sum(), 0)
        self.assertEqual(report["Bonded Est. Reorder Qty"].sum(), 0)

    def test_export_order_empty_audit_and_identifiers_survive_saved_report_reload(self):
        inputs = test_automation.PipelineAndPublicationTests().inputs()
        inputs["purchase_orders"], audit = self.reconcile()
        report, _ = build_weekly_report(inputs, 26)
        with TemporaryDirectory() as directory:
            snapshot = publish_report(directory, report, {}, audit)
            self.assertEqual(pd.read_csv(snapshot / 'report.csv').columns.tolist(), EXPECTED_REPORT_HEADERS)
            self.assertEqual(pd.read_csv(snapshot / 'po_audit.csv').columns.tolist(), EXPECTED_AUDIT_HEADERS)
            saved_report, _, saved_audit = load_report(directory)
        workbook = openpyxl.load_workbook(BytesIO(report_excel_bytes(saved_report, saved_audit)))
        self.assertEqual([c.value for c in workbook["Weekly Report"][1]], EXPECTED_REPORT_HEADERS)
        self.assertEqual(workbook["Weekly Report"]["F2"].number_format, "#,##0")
        self.assertEqual(workbook["Weekly Report"]["J2"].number_format, "#,##0.0")
        headers = [c.value for c in workbook["PO Match Audit"][1]]
        self.assertEqual(headers, EXPECTED_AUDIT_HEADERS)
        self.assertEqual(saved_audit["Line ID"].iloc[0], "5020021000049722165")
        self.assertNotIn("Line ID", headers)
        date_cell = workbook["PO Match Audit"].cell(2, headers.index("PO Date") + 1)
        self.assertEqual(date_cell.number_format, "dd/mm/yyyy")
        self.assertEqual(date_cell.value.year, 2026)
        empty = openpyxl.load_workbook(BytesIO(report_excel_bytes(report, pd.DataFrame())))
        self.assertEqual(empty.sheetnames, ["Weekly Report", "Supplier Reorders", "Clear from Bond", "PO Match Audit", "Colour Key"])
        self.assertEqual(empty["PO Match Audit"].max_row, 1)

    def test_fully_billed_po_awaiting_receipt_is_included_and_received_order_is_excluded(self):
        self.assertTrue(po_summary_is_relevant({"status": "billed", "received_status": "partially_received", "quantity_yet_to_receive": 20}))
        self.assertFalse(po_summary_is_relevant({"status": "billed", "received_status": "received", "quantity_yet_to_receive": 0}))
        self.assertFalse(po_summary_is_relevant({"status": "draft"}))

    def test_missing_category_uses_the_templates_others_label(self):
        report, _ = build_weekly_report(test_automation.PipelineAndPublicationTests().inputs(), 26)
        self.assertEqual(report['Category'].iloc[0], 'Others')


if __name__ == "__main__":
    unittest.main()
