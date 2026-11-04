"""Verify one click produces one background sync across Streamlit reruns."""
from threading import Event
import time
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from report_inputs import prepare_source
from report_store import AUDIT_COLUMNS, REPORT_COLUMNS
from zoho_books import BooksConfig
from zoho_jobs import PullManager


class BackgroundUiTests(unittest.TestCase):
    def test_completion_and_page_reruns_do_not_start_another_zoho_pull(self):
        manager = PullManager()
        entered, release = Event(), Event()
        inputs = {"sales_cy": prepare_source(pd.DataFrame({"sku": ["047-003-24"], "brand": ["Spier"],
                                                          "quantity_sold": [50], "amount": [1000]}), "sales"),
                  "stock_cy": prepare_source(pd.DataFrame({"sku": ["047-003-24"], "closing_stock": [100]}), "stock")}
        inputs["purchase_orders"] = pd.DataFrame({"sku_key": ["047003"], "open_po_qty": [60.]})
        audit = pd.DataFrame({"PO Number": ["PO-1"], "PO Date": ["2026-10-30"], "SKU": ["047-003-24"],
                              "Ordered Qty": [100.], "Billed Qty": [40.], "Received Qty": [40.], "Outstanding Qty": [60.]})

        def fixture_pull(client, cutoff, today):
            client.progress("Step 2/7: 2026 sales — invoices: 10/200")
            entered.set()
            release.wait(15)
            return inputs, audit, {"sales_as_of": cutoff.isoformat(), "stock_as_of": today.isoformat(),
                                           "source": "Zoho Books", "transaction_cache": {"downloaded": 2, "reused": 198}}

        cfg = BooksConfig(organization_id="test", client_id="fake", client_secret="fake", refresh_token="fake")
        try:
            with patch("automation_ui.load_config", return_value=cfg), \
                 patch("automation_ui.get_pull_manager", return_value=manager), \
                 patch("report_store.publish_report") as publish, \
                 patch("automation_ui.fetch_weekly_inputs", side_effect=fixture_pull) as fetch:
                app = AppTest.from_file("app.py").run(timeout=45)
                app.sidebar.text_input[0].input("twc2026").run(timeout=45)
                app.sidebar.radio[0].set_value("Zoho Books").run(timeout=45)
                before = app.session_state["report"].copy()
                next(button for button in app.button if button.label == "Pull from Zoho Books").click().run(timeout=45)
                self.assertFalse(app.exception)
                self.assertTrue(entered.wait(2))
                self.assertEqual(fetch.call_count, 1)
                self.assertTrue(app.session_state["report"].equals(before))
                self.assertTrue(app.sidebar.radio[0].disabled)
                self.assertTrue(any("Step 2/7: 2026 sales" in item.value for item in app.markdown))
                job = manager.get(app.session_state["zoho_job_id"])
                release.set()
                deadline = time.monotonic() + 5
                while job.snapshot()["state"] == "running" and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(job.snapshot()["state"], "complete")
                app.run(timeout=45)
                self.assertFalse(app.exception)
                self.assertFalse(app.error, [item.value for item in app.error])
                self.assertNotIn("zoho_job_id", app.session_state)
                self.assertEqual(app.session_state["report"]["CY Sales (YTD)"].sum(), 50,
                                 {"source": app.sidebar.radio[0].value,
                                  "pending": app.session_state.filtered_state.get("zoho_generate_pending"),
                                  "notice": app.session_state.filtered_state.get("zoho_last_notice"),
                                  "source_metadata": app.session_state.filtered_state.get("source_metadata"),
                                  "input_rows": len(app.session_state["sales_cy"]),
                                  "success": [item.value for item in app.success]})
                self.assertIn("198 reused from cache", app.session_state["zoho_last_notice"][1])
                self.assertEqual([tab.label for tab in app.get("tab")], ["Sales and stock", "Purchase order audit"])
                self.assertTrue(any(list(item.value.columns) == AUDIT_COLUMNS for item in app.dataframe))
                self.assertTrue(any(list(item.value.columns) == list(REPORT_COLUMNS.values()) for item in app.dataframe))
                for _ in range(3):
                    app.run(timeout=45)
                    self.assertFalse(app.exception)
                    self.assertNotIn("zoho_job_id", app.session_state)
                self.assertEqual(fetch.call_count, 1)
                publish.assert_not_called()
        finally:
            release.set()
            manager.close()


if __name__ == "__main__":
    unittest.main()
