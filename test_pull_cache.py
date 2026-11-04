from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from test_automation import FakeResponse, FakeSession, config
from zoho_books import BooksClient, ZohoError, fetch_weekly_inputs, load_items
from zoho_pull_cache import save_item_snapshot, save_weekly_snapshot


DAY = date(2026, 10, 29)
TODAY = date(2026, 10, 30)


class PullCacheTests(unittest.TestCase):
    def result(self):
        return ({"sales_cy": pd.DataFrame({"sku_key": ["001002"], "quantity_sold": [3.5], "revenue": [123.45]}),
                 "stock_cy": pd.DataFrame({"sku_key": ["001002"], "closing_stock": [10.]}), "empty": pd.DataFrame()},
                pd.DataFrame(), {"sales_as_of": DAY.isoformat(), "stock_as_of": TODAY.isoformat(), "api_calls": 100,
                                 "synced_at": "2026-10-30T10:00:00+00:00"})

    def test_repeated_complete_pull_uses_zero_api_calls_and_preserves_sku_and_dates(self):
        with TemporaryDirectory() as directory:
            initial = BooksClient(config(), cache_dir=directory)
            save_weekly_snapshot(initial, DAY, self.result())
            session = FakeSession([])
            client = BooksClient(config(), session=session, cache_dir=directory)
            inputs, _, metadata = fetch_weekly_inputs(client, DAY, TODAY)
            self.assertEqual(inputs["sales_cy"].sku_key.tolist(), ["001002"])
            self.assertAlmostEqual(inputs["sales_cy"].revenue.iloc[0], 123.45)
            self.assertEqual(metadata["synced_at"], "2026-10-30T10:00:00+00:00")
            self.assertEqual(metadata["api_calls"], 0)
            self.assertTrue(metadata["served_from_cache"])
            self.assertEqual(session.get_calls, [])
            self.assertEqual(session.post_calls, [])

    def test_saved_mode_uses_older_complete_pull_without_network(self):
        with TemporaryDirectory() as directory:
            initial = BooksClient(config(), cache_dir=directory)
            save_weekly_snapshot(initial, DAY, self.result())
            path = next(Path(directory).glob("*/weekly_inputs/*.json"))
            saved = json.loads(path.read_text())
            saved["saved_at"] = "2026-01-01T00:00:00+00:00"
            path.write_text(json.dumps(saved))
            client = BooksClient(config(), session=FakeSession([]), cache_dir=directory, cache_only=True)
            self.assertEqual(fetch_weekly_inputs(client, DAY, TODAY)[2]["api_calls"], 0)

    def test_saved_stock_split_under_old_warehouse_rules_is_regrouped_from_its_item_snapshot(self):
        with TemporaryDirectory() as directory:
            cfg = config()
            cfg.warehouse_mapping_mode, cfg.locations = "names", {}
            client = BooksClient(cfg, cache_dir=directory, cache_only=True, session=FakeSession([]))
            items = {"1": {"item_id": "1", "sku": "047-003-25", "stock_on_hand": 1322, "locations": [
                {"warehouse_id": "c", "warehouse_name": "The Wine Lab", "warehouse_stock_on_hand": 2},
                {"warehouse_id": "b", "warehouse_name": "DP World", "warehouse_stock_on_hand": 0},
                {"warehouse_id": "t", "warehouse_name": "In-transit", "warehouse_stock_on_hand": 1320}]}}
            save_item_snapshot(client, items)
            inputs, audit, metadata = self.result()
            # The old rules counted "In-transit" as cleared stock.
            inputs = {**inputs, "twc_stock": pd.DataFrame({"sku_key": ["047003"], "twc_stock": [1322.]}),
                      "in_transit": pd.DataFrame()}
            metadata = {**metadata, "stock_snapshot_at": client.stock_snapshot_at}
            save_weekly_snapshot(client, DAY, (inputs, audit, metadata))
            inputs, _, metadata = fetch_weekly_inputs(client, DAY, TODAY)
            self.assertEqual(inputs["twc_stock"].twc_stock.tolist(), [2])
            self.assertEqual(inputs["in_transit"].in_transit_qty.tolist(), [1320])
            self.assertEqual(metadata["warehouse_groups"]["in_transit"], ["t"])
            self.assertEqual(client.calls, 0)
            # The corrected split is saved, so later runs no longer need the item snapshot.
            Path(directory, next(Path(directory).iterdir()).name, "item_stock.json").unlink()
            self.assertEqual(fetch_weekly_inputs(client, DAY, TODAY)[0]["in_transit"].in_transit_qty.tolist(), [1320])

    def test_saved_pull_without_batch_bill_and_sales_checks_is_not_reused(self):
        with TemporaryDirectory() as directory:
            client = BooksClient(config(), cache_dir=directory, cache_only=True, session=FakeSession([]))
            inputs, _, metadata = self.result()
            audit = pd.DataFrame({"SKU": ["047-003-24"], "Unbilled Qty": [30.], "Outstanding Qty": [20.]})
            metadata["po_quantity_basis"] = "billed_with_unreceived_credit_v1"
            save_weekly_snapshot(client, DAY, (inputs, audit, metadata))
            with self.assertRaisesRegex(ZohoError, "No complete saved Books data"):
                fetch_weekly_inputs(client, DAY, TODAY)

    def test_missing_saved_data_never_falls_back_to_network(self):
        with TemporaryDirectory() as directory:
            session = FakeSession([])
            client = BooksClient(config(), session=session, cache_dir=directory, cache_only=True)
            with self.assertRaisesRegex(ZohoError, "No complete saved Books data"):
                fetch_weekly_inputs(client, DAY, TODAY)
            self.assertEqual(session.get_calls + session.post_calls, [])

    def test_incomplete_old_po_inputs_cannot_reappear_from_a_complete_cache(self):
        with TemporaryDirectory() as directory:
            client = BooksClient(config(), cache_dir=directory, cache_only=True, session=FakeSession([]))
            inputs, _, metadata = self.result()
            audit = pd.DataFrame({"SKU": ["001-002"], "Outstanding Qty": [100.]})
            save_weekly_snapshot(client, DAY, (inputs, audit, metadata))
            with self.assertRaisesRegex(ZohoError, "No complete saved Books data"):
                fetch_weekly_inputs(client, DAY, TODAY)
            self.assertEqual(client.calls, 0)

    def test_old_open_only_po_cache_cannot_omit_fully_billed_incoming_orders(self):
        with TemporaryDirectory() as directory:
            session = FakeSession([])
            client = BooksClient(config(), cache_dir=directory, cache_only=True, session=session)
            inputs, _, metadata = self.result()
            metadata["po_quantity_basis"] = "unbilled_and_unreceived_v1"
            inputs["purchase_orders"] = pd.DataFrame({"sku_key": ["001002"], "open_po_qty": [0.]})
            audit = pd.DataFrame({"SKU": ["001-002-24"], "Line ID": ["1234567890123456789"], "Ordered Qty": [100.],
                                  "Billed Qty": [40.], "Cancelled Qty": [10.], "Received Qty": [90.], "Outstanding Qty": [0.]})
            save_weekly_snapshot(client, DAY, (inputs, audit, metadata))
            with self.assertRaisesRegex(ZohoError, "No complete saved Books data"):
                fetch_weekly_inputs(client, DAY, TODAY)
            self.assertEqual(session.get_calls + session.post_calls, [])
            self.assertEqual(client.calls, 0)

    def test_other_cutoff_or_warehouse_settings_cannot_reuse_wrong_complete_pull(self):
        with TemporaryDirectory() as directory:
            initial = BooksClient(config(), cache_dir=directory)
            save_weekly_snapshot(initial, DAY, self.result())
            cfg = config()
            cfg.locations["bonded"] = ["different"]
            client = BooksClient(cfg, session=FakeSession([]), cache_dir=directory, cache_only=True)
            with self.assertRaises(ZohoError):
                fetch_weekly_inputs(client, DAY, TODAY)
            client = BooksClient(config(), session=FakeSession([]), cache_dir=directory, cache_only=True)
            with self.assertRaises(ZohoError):
                fetch_weekly_inputs(client, date(2026, 10, 28), TODAY)

    def test_force_refresh_bypasses_complete_pull_cache(self):
        with TemporaryDirectory() as directory:
            initial = BooksClient(config(), cache_dir=directory)
            save_weekly_snapshot(initial, DAY, self.result())
            client = BooksClient(config(), cache_dir=directory, force_refresh=True)
            with patch("zoho_books._fetch_weekly_inputs", return_value=self.result()) as fetch:
                fetch_weekly_inputs(client, DAY, TODAY)
            self.assertEqual(fetch.call_count, 1)

    def test_recent_complete_pull_does_not_hide_an_old_stock_snapshot(self):
        with TemporaryDirectory() as directory:
            client = BooksClient(config(), cache_dir=directory)
            inputs, audit, metadata = self.result()
            metadata["stock_snapshot_at"] = "2026-01-01T00:00:00+00:00"
            save_weekly_snapshot(client, DAY, (inputs, audit, metadata))
            with patch("zoho_books._fetch_weekly_inputs", return_value=self.result()) as fetch:
                fetch_weekly_inputs(client, DAY, TODAY)
            self.assertEqual(fetch.call_count, 1)

    @patch("zoho_books.time.sleep")
    def test_repeat_stock_load_avoids_list_and_detail_calls(self, sleep):
        item = {"item_id": "1", "sku": "001-002", "track_inventory": True}
        responses = [FakeResponse({"code": 0, "items": [item], "page_context": {"has_more_page": False}}),
                     FakeResponse({"code": 0, "items": [{"item_id": "1", "warehouses": []}]})]
        with TemporaryDirectory() as directory:
            client = BooksClient(config(), session=FakeSession(responses), cache_dir=directory)
            first = load_items(client)
            session = FakeSession([])
            cached = BooksClient(config(), session=session, cache_dir=directory)
            self.assertEqual(load_items(cached), first)
            self.assertEqual(load_items(cached, include_stock=False), first)
            self.assertEqual(session.get_calls + session.post_calls, [])
            self.assertEqual(cached.stock_snapshot_at, client.stock_snapshot_at)


if __name__ == "__main__":
    unittest.main()
