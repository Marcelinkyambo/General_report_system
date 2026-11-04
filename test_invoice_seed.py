import csv
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from seed_invoice_cache import read_invoice_exports, seed_cache, exported_detail
from test_automation import FakeResponse, FakeSession, config
from zoho_books import BooksClient


EXPORTED = "2026-10-30T12:26:19+02:00"
FIELDS = ["Invoice ID", "Invoice Date", "Invoice Status", "Total", "Exchange Rate", "Product ID", "Quantity", "Item Total", "Line Item Category"]


def row(**changes):
    return {"Invoice ID": "1", "Invoice Date": "2026-01-10", "Invoice Status": "Closed", "Total": "100",
            "Exchange Rate": "1", "Product ID": "42", "Quantity": "2", "Item Total": "100", "Line Item Category": "Item", **changes}


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class InvoiceSeedTests(unittest.TestCase):
    def test_split_invoice_and_legitimate_duplicate_lines_are_preserved(self):
        with TemporaryDirectory() as directory:
            files = [Path(directory) / name for name in ["a.csv", "b.csv"]]
            write_csv(files[0], [row()])
            write_csv(files[1], [row(), row(**{"Invoice ID": "2"})])
            docs, _ = read_invoice_exports(files)
            self.assertEqual(len(docs), 2)
            self.assertEqual(len(docs["1"]["line_items"]), 2)

    def test_identical_whole_file_is_not_imported_twice(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.csv"
            write_csv(path, [row()])
            docs, files = read_invoice_exports([path, path])
            self.assertEqual(len(docs["1"]["line_items"]), 1)
            self.assertEqual(len(files), 1)

    def test_conflicting_header_values_are_rejected_before_any_cache_write(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.csv"
            write_csv(path, [row(), row(Total="200")])
            with self.assertRaisesRegex(ValueError, "Conflicting invoice header"):
                seed_cache(config(), [path], Path(directory) / "cache", EXPORTED)
            self.assertFalse((Path(directory) / "cache").exists())

    def seed(self, directory):
        path = Path(directory) / "invoice.csv"
        write_csv(path, [row()])
        report = seed_cache(config(), [path], Path(directory) / "cache", EXPORTED)
        saved_path = next((Path(directory) / "cache").glob("*/invoices/_export_invoices.json"))
        bundle = json.loads(saved_path.read_text())
        saved = {"detail": bundle["invoices"]["1"], "export_seed": {"exported_at": bundle["exported_at"]}}
        return path, saved_path, saved, report

    def summary(self, **changes):
        return {"invoice_id": "1", "date": "2026-01-10", "status": "paid", "total": 100,
                "last_modified_time": "2026-10-01T10:00:00+0000", **changes}

    def test_existing_api_cache_is_preserved_during_repeated_seed(self):
        with TemporaryDirectory() as directory:
            path, saved_path, _, _ = self.seed(directory)
            api_path = saved_path.parent / "1.json"
            api_path.write_text('{"existing_api_record": true}')
            report = seed_cache(config(), [path], Path(directory) / "cache", EXPORTED)
            self.assertEqual(report["existing_records_preserved"], 1)
            self.assertEqual(json.loads(api_path.read_text()), {"existing_api_record": True})

    def test_recent_edits_and_export_races_are_not_served_from_csv(self):
        with TemporaryDirectory() as directory:
            _, _, saved, _ = self.seed(directory)
            self.assertIsNotNone(exported_detail(saved, self.summary()))
            for modified in ["2026-10-30T10:25:00+0000", "2026-10-30T11:00:00+0000"]:
                self.assertIsNone(exported_detail(saved, self.summary(last_modified_time=modified)))

    def test_different_id_date_total_rate_or_status_never_uses_seed(self):
        with TemporaryDirectory() as directory:
            _, _, saved, _ = self.seed(directory)
            for changes in [{"invoice_id": "2"}, {"date": "2026-01-11"}, {"total": 101},
                            {"exchange_rate": 2}, {"status": "sent"}]:
                self.assertIsNone(exported_detail(saved, self.summary(**changes)), changes)

    @patch("zoho_books.time.sleep")
    def test_bill_export_avoids_detail_request_and_preserves_product_quantities(self, sleep):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "Bill.csv"
            fields = [field.replace("Invoice", "Bill") for field in FIELDS] + ["Purchase Order Number"]
            values = {**{field.replace("Invoice", "Bill"): value for field, value in row().items()}, "Purchase Order Number": "PO-1"}
            with path.open("w", newline="", encoding="utf-8") as destination:
                writer = csv.DictWriter(destination, fieldnames=fields)
                writer.writeheader()
                writer.writerow(values)
            seed_cache(config(), [path], Path(directory) / "cache", EXPORTED, "bills")
            summary = self.summary()
            summary["bill_id"] = summary.pop("invoice_id")
            session = FakeSession([FakeResponse({"code": 0, "bills": [summary], "page_context": {"has_more_page": False}})])
            client = BooksClient(config(), session=session, cache_dir=Path(directory) / "cache")
            docs = list(client.documents("bills", "bill", "bill_id", date(2026, 1, 1), date(2026, 10, 29)))
            self.assertEqual(docs[0]["line_items"][0]["quantity"], 2)
            # The batch check needs to know which PO each exported bill line belongs to.
            self.assertEqual(docs[0]["line_items"][0]["purchaseorder_number"], "PO-1")
            self.assertEqual(len(session.get_calls), 1)
            self.assertEqual(client.export_reused_by_resource, {"bills": 1})

    @patch("zoho_books.time.sleep")
    def test_live_summary_validates_csv_and_avoids_invoice_detail_request(self, sleep):
        with TemporaryDirectory() as directory:
            _, saved_path, _, _ = self.seed(directory)
            summary = self.summary()
            session = FakeSession([FakeResponse({"code": 0, "invoices": [summary], "page_context": {"has_more_page": False}})])
            client = BooksClient(config(), session=session, cache_dir=Path(directory) / "cache")
            docs = list(client.documents("invoices", "invoice", "invoice_id", date(2026, 1, 1), date(2026, 10, 29)))
            self.assertEqual(len(session.get_calls), 1)
            self.assertEqual(docs[0]["line_items"][0]["quantity"], 2)
            self.assertEqual(client.sync_stats["downloaded"], 0)
            self.assertEqual(client.sync_stats["reused"], 1)
            self.assertEqual(client.export_reused, 1)
            self.assertFalse((saved_path.parent / "1.json").exists())

    @patch("zoho_books.time.sleep")
    def test_changed_invoice_is_downloaded_instead_of_using_stale_export(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            summary = self.summary(last_modified_time="2026-10-30T11:00:00+0000")
            updated = {**summary, "line_items": [{"item_id": "42", "quantity": 3, "item_total": 100}]}
            session = FakeSession([FakeResponse({"code": 0, "invoices": [summary], "page_context": {"has_more_page": False}}),
                                   FakeResponse({"code": 0, "invoice": updated})])
            client = BooksClient(config(), session=session, cache_dir=Path(directory) / "cache")
            docs = list(client.documents("invoices", "invoice", "invoice_id", date(2026, 1, 1), date(2026, 10, 29)))
            self.assertEqual(len(session.get_calls), 2)
            self.assertEqual(docs[0]["line_items"][0]["quantity"], 3)


if __name__ == "__main__":
    unittest.main()
