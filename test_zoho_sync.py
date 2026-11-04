"""Behavioral checks for saved history, deltas and interrupted syncs."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_automation import FakeResponse, FakeSession, config
from zoho_books import BooksClient, ZohoError


START, END = date(2026, 1, 1), date(2026, 10, 29)
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


def record(identifier="1", **changes):
    return {"invoice_id": identifier, "date": "2026-01-10", "status": "sent",
            "last_modified_time": "2026-10-01T10:00:00+0000", "total": 100, **changes}


def listing(rows):
    return FakeResponse({"code": 0, "invoices": rows, "page_context": {"has_more_page": False}})


def detail(row):
    return FakeResponse({"code": 0, "invoice": {**row, "line_items": []}})


@patch("zoho_books.time.sleep")
class IncrementalBooksTests(unittest.TestCase):
    def pull(self, directory, responses, now=NOW, end=END, force=False):
        session = FakeSession(responses)
        client = BooksClient(config(), session=session, cache_dir=directory, recheck_history=force)
        with patch("zoho_sync_cache.utcnow", return_value=now):
            rows = list(client.documents("invoices", "invoice", "invoice_id", START, end))
        self.assertFalse(session.responses, "Unused fake responses indicate an unexpected request sequence")
        return client, session, rows

    def seed(self, directory, rows=None):
        rows = rows if rows is not None else [record()]
        return self.pull(directory, [listing(rows)] + [detail(row) for row in rows if row["status"] != "void"])

    def test_repeat_refresh_checks_only_changes_and_uses_saved_history(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            client, session, rows = self.pull(directory, [listing([])], now=NOW + timedelta(hours=1))
            self.assertEqual([row["invoice_id"] for row in rows], ["1"])
            query = session.get_calls[0][1]["params"]
            self.assertEqual(query["last_modified_time"], "2026-10-30T09:55:00+0000")
            self.assertNotIn("date_start", query)
            self.assertEqual(client.sync_stats, {"downloaded": 0, "reused": 1, "incremental_checks": 1, "full_checks": 0})

    def test_backdated_new_and_edited_old_invoices_replace_without_double_counting(self, sleep):
        changed = record(total=150, last_modified_time="2026-10-30T10:30:00+0000")
        new = record("2", date="2026-02-01", last_modified_time="2026-10-30T10:30:00+0000")
        with TemporaryDirectory() as directory:
            self.seed(directory)
            client, _, rows = self.pull(directory, [listing([changed, new]), detail(changed), detail(new)], now=NOW + timedelta(hours=1))
            self.assertEqual(sum(row["total"] for row in rows), 250)
            self.assertEqual(client.sync_stats["downloaded"], 2)

    def test_voided_document_is_removed_from_totals_without_detail_download(self, sleep):
        voided = record(status="void", last_modified_time="2026-10-30T10:30:00+0000")
        with TemporaryDirectory() as directory:
            self.seed(directory)
            _, _, rows = self.pull(directory, [listing([voided])], now=NOW + timedelta(hours=1))
            self.assertEqual(rows, [])

    def test_date_moved_outside_report_is_not_left_in_cached_totals(self, sleep):
        moved = record(date="2025-12-01", last_modified_time="2026-10-30T10:30:00+0000")
        with TemporaryDirectory() as directory:
            self.seed(directory)
            _, session, rows = self.pull(directory, [listing([moved])], now=NOW + timedelta(hours=1))
            self.assertEqual(rows, [])
            self.assertNotIn("date_start", session.get_calls[0][1]["params"])

    def test_later_cutoff_loads_newly_covered_dates_even_if_not_recently_modified(self, sleep):
        new = record("2", date="2026-10-30")
        with TemporaryDirectory() as directory:
            self.seed(directory)
            _, session, rows = self.pull(directory, [listing([]), listing([new]), detail(new)],
                                         now=NOW + timedelta(hours=1), end=date(2026, 10, 30))
            self.assertEqual(len(rows), 2)
            self.assertEqual(session.get_calls[1][1]["params"]["date_start"], "2026-10-30")

    def test_earlier_cutoff_uses_history_without_losing_later_cached_coverage(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory, [record(), record("2", date="2026-10-29")])
            _, _, rows = self.pull(directory, [listing([])], now=NOW + timedelta(hours=1), end=date(2026, 10, 1))
            self.assertEqual([row["invoice_id"] for row in rows], ["1"])
            index = json.loads(next(Path(directory).glob("*/invoices/_index_*.json")).read_text())
            self.assertEqual(index["end"], END.isoformat())

    def test_daily_reconciliation_detects_hard_deleted_records(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            client, session, rows = self.pull(directory, [listing([])], now=NOW + timedelta(days=1))
            self.assertEqual(rows, [])
            self.assertNotIn("last_modified_time", session.get_calls[0][1]["params"])
            self.assertEqual(client.sync_stats["full_checks"], 1)

    def test_explicit_recheck_reconciles_index_but_reuses_unchanged_detail(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            client, session, rows = self.pull(directory, [listing([record()])], now=NOW + timedelta(hours=1), force=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(session.get_calls), 1)
            self.assertEqual(client.sync_stats["reused"], 1)

    def test_interrupted_initial_import_saves_details_but_does_not_advance_checkpoint(self, sleep):
        with TemporaryDirectory() as directory:
            first, second = record(), record("2")
            session = FakeSession([listing([first, second]), detail(first)])
            client = BooksClient(config(), session=session, cache_dir=directory)
            with patch("zoho_sync_cache.utcnow", return_value=NOW):
                stream = client.documents("invoices", "invoice", "invoice_id", START, END)
                self.assertEqual(next(stream)["invoice_id"], "1")
                stream.close()
            self.assertFalse(list(Path(directory).glob("*/invoices/_index_*.json")))
            client, _, rows = self.pull(directory, [listing([first, second]), detail(second)])
            self.assertEqual(len(rows), 2)
            self.assertEqual(client.sync_stats["reused"], 1)

    def test_failed_delta_does_not_advance_previous_checkpoint(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            index = next(Path(directory).glob("*/invoices/_index_*.json"))
            before = index.read_text()
            changed = record(total=150, last_modified_time="2026-10-30T10:30:00+0000")
            with self.assertRaises(ZohoError):
                self.pull(directory, [listing([changed]), FakeResponse({"code": 57}, 403)], now=NOW + timedelta(hours=1))
            self.assertEqual(index.read_text(), before)

    def test_unhonored_modified_filter_falls_back_to_full_index_reconciliation(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            client, session, rows = self.pull(directory, [listing([record()]), listing([])], now=NOW + timedelta(hours=1))
            self.assertEqual(rows, [])
            self.assertEqual(client.sync_stats["full_checks"], 1)
            self.assertNotIn("last_modified_time", session.get_calls[1][1]["params"])

    def test_corrupt_index_is_rebuilt_without_redownloading_valid_details(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            next(Path(directory).glob("*/invoices/_index_*.json")).write_text("bad json")
            client, _, rows = self.pull(directory, [listing([record()])])
            self.assertEqual(len(rows), 1)
            self.assertEqual(client.sync_stats["reused"], 1)

    def test_clock_rollback_forces_full_reconciliation(self, sleep):
        with TemporaryDirectory() as directory:
            self.seed(directory)
            client, _, rows = self.pull(directory, [listing([record()])], now=NOW - timedelta(hours=1))
            self.assertEqual(len(rows), 1)
            self.assertEqual(client.sync_stats["full_checks"], 1)


if __name__ == "__main__":
    unittest.main()
