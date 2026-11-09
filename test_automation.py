import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from automate_report import elapsed_weeks
from planning import apply_brand_targets, brand_history, build_annual_plan, load_growth, prepare_planning, save_growth
from report_inputs import prepare_source
from report_store import load_report, publish_report, report_excel_bytes, weekly_report_view
from weekly_pipeline import build_weekly_report
from zoho_books import BooksClient, BooksConfig, ZohoError, ZohoResponseError, fetch_annual_sales, fetch_weekly_inputs, open_orders, product_names, stock_sources, transaction_rows, groups_from_warehouse_names, warehouse_group, check_read_access, exchange_grant_code, response_records, load_items


class PlanningTests(unittest.TestCase):
    def test_brand_rates_rebase_full_year_sales_and_preserve_other_benchmarks(self):
        frame = pd.DataFrame({"brand": [" Spier ", "Other"], "quantity_sold": [100, 200],
                              "baseline_sales_plan": [2400, 1000], "annual_budget_plan": [2880, 1200],
                              "growth_target_plan": [.2, .2]})
        result = apply_brand_targets(frame, {"SPIER": .3}, .2, 26)
        self.assertEqual(result.annual_target.tolist(), [3120, 1200])
        self.assertEqual(result.monthly_sales_target.tolist(), [260, 100])

    def test_zero_and_negative_growth_are_valid_overrides(self):
        frame = pd.DataFrame({"brand": ["A", "B"], "quantity_sold": [20, 20], "baseline_sales_plan": [100, 100]})
        result = apply_brand_targets(frame, {"A": 0, "B": -.5}, .2, 26)
        self.assertEqual(result.annual_target.tolist(), [100, 50])

    def test_products_outside_the_benchmark_get_no_target_even_with_a_brand_override(self):
        # A target built from a product's own sales always shows the same pace.
        result = apply_brand_targets(pd.DataFrame({"brand": ["A"], "quantity_sold": [100]}), {"A": .5}, .2, 26)
        self.assertTrue(pd.isna(result.annual_target.iloc[0]))
        self.assertTrue(pd.isna(result.weekly_sales_target.iloc[0]))
        self.assertEqual(result["target_source"].iloc[0], "No planning target")

    def test_zero_benchmark_stays_zero_and_missing_baseline_is_not_guessed(self):
        frame = pd.DataFrame({"brand": ["A"], "quantity_sold": [100], "annual_budget_plan": [0]})
        self.assertEqual(apply_brand_targets(frame, {}, .2, 26).annual_target.iloc[0], 0)
        with self.assertRaisesRegex(ValueError, "base-year"):
            apply_brand_targets(frame, {"A": .5}, .2, 26)

    def test_planning_rejects_duplicate_products_instead_of_multiplying_rows(self):
        raw = pd.DataFrame({"SKU": ["047-003-22", "047-003-23"], "Current Year Sales": [100, 200]})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            prepare_planning(raw)

    def test_benchmark_percentage_string_and_missing_baseline_recover_correctly(self):
        plan = prepare_planning(pd.DataFrame({"SKU": ["047-003"], "Growth Target %": ["20%"], "Next Year Budget": [120]}))
        self.assertEqual(plan.baseline_sales_plan.iloc[0], 100)

    def test_historical_growth_uses_totals_and_includes_prior_only_items(self):
        cy = pd.DataFrame({"sku_key": ["a", "b", "new"], "brand": ["A", "A", "New"], "quantity_sold": [200, 0, 10]})
        py = pd.DataFrame({"sku_key": ["a", "b", "old"], "brand": ["A", "A", "A"], "quantity_sold": [100, 100, 200]})
        summary = brand_history(cy, py).set_index("Brand")
        self.assertEqual(summary.loc["A", "Historical Growth %"], -50)
        self.assertTrue(pd.isna(summary.loc["New", "Historical Growth %"]))

    def test_growth_settings_are_separate_by_year_and_zero_survives_reload(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "rates.json"
            save_growth(path, 2026, {"Spier": 0})
            save_growth(path, 2027, {"Spier": .3})
            self.assertEqual(load_growth(path, 2026), {"spier": 0})
            self.assertEqual(load_growth(path, 2027), {"spier": .3})

    def test_missing_prior_year_source_is_not_displayed_as_zero_sales(self):
        cy = pd.DataFrame({"sku_key": ["a"], "brand": ["A"], "quantity_sold": [10]})
        summary = brand_history(cy, pd.DataFrame())
        self.assertTrue(pd.isna(summary["PY Sales"].iloc[0]))
        self.assertTrue(pd.isna(summary["Historical Growth %"].iloc[0]))

    def test_annual_plan_includes_discontinued_products_and_applies_brand_rate(self):
        cy = pd.DataFrame({"sku_key": ["a"], "sku": ["001-001"], "item_name": ["One"], "brand": ["A"], "quantity_sold": [100]})
        py = pd.DataFrame({"sku_key": ["a", "b"], "sku": ["001-001", "001-002"], "item_name": ["One", "Old"], "brand": ["A", "A"], "quantity_sold": [50, 10]})
        result = build_annual_plan(cy, py, {"A": .5}).set_index("SKU")
        self.assertEqual(result.loc["001-001", "Next Year Budget"], 150)
        self.assertEqual(result.loc["001-002", "Current Year Sales"], 0)
        self.assertEqual(result.loc["001-002", "Brand"], "A")

    def test_year_end_and_leap_year_fraction(self):
        self.assertEqual(elapsed_weeks(date(2026, 12, 31)), 52)
        self.assertEqual(elapsed_weeks(date(2024, 12, 31)), 52)
        self.assertAlmostEqual(elapsed_weeks(date(2024, 6, 30)), 182 / 366 * 52)


def config():
    return BooksConfig(organization_id="123", client_id="client", client_secret="secret", refresh_token="refresh",
                       warehouse_mapping_mode="ids",
                       locations={"cleared": ["c"], "bonded": ["b"], "in_transit": ["t"]})


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.headers = data, status, {}

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.get_calls, self.post_calls = list(responses), [], []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse({"access_token": "token", "expires_in": 3600})

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.responses.pop(0)


class BooksTests(unittest.TestCase):
    @patch("zoho_books.time.sleep")
    def test_bulk_item_reads_preserve_metadata_and_return_every_requested_item(self, sleep):
        summaries = [{"item_id": str(i), "sku": f"001-{i:03}", "brand": "A", "track_inventory": True} for i in range(1, 24)]
        responses = [FakeResponse({"code": 0, "items": summaries, "page_context": {"has_more_page": False}}),
                     FakeResponse({"code": 0, "items": [{"item_id": row["item_id"], "warehouses": []} for row in summaries[:20]]}),
                     FakeResponse({"code": 0, "items": [{"item_id": row["item_id"], "warehouses": []} for row in summaries[20:]]})]
        session = FakeSession(responses)
        messages = []
        items = load_items(BooksClient(config(), session=session, progress=messages.append))
        self.assertEqual(len(items), 23)
        self.assertEqual(items["23"]["brand"], "A")
        self.assertEqual(len(session.get_calls), 3)
        self.assertTrue(any("23/23" in message for message in messages))

    @patch("zoho_books.time.sleep")
    def test_annual_item_metadata_uses_no_stock_detail_calls(self, sleep):
        session = FakeSession([FakeResponse({"code": 0, "items": [{"item_id": "1", "brand": "A"}], "page_context": {"has_more_page": False}})])
        self.assertEqual(load_items(BooksClient(config(), session=session), include_stock=False)["1"]["brand"], "A")
        self.assertEqual(len(session.get_calls), 1)

    @patch("zoho_books.time.sleep")
    def test_missing_bulk_item_does_not_become_zero_stock(self, sleep):
        session = FakeSession([FakeResponse({"code": 0, "items": [{"item_id": "1"}], "page_context": {"has_more_page": False}}),
                               FakeResponse({"code": 0, "items": []})])
        with self.assertRaisesRegex(ZohoResponseError, "omitted items"):
            load_items(BooksClient(config(), session=session))

    @patch("zoho_books.time.sleep")
    def test_access_check_accepts_live_vendor_credit_response_key(self, sleep):
        resources = ["items", "invoices", "bills", "purchaseorders", "creditnotes", "vendor_credits"]
        session = FakeSession([FakeResponse({"code": 0, "organizations": [{"organization_id": "123"}]})] +
                              [FakeResponse({"code": 0, resource: []}) for resource in resources])
        checks = check_read_access(BooksClient(config(), session=session))
        self.assertTrue(checks.Status.eq("Ready").all())
        self.assertEqual(checks.loc[checks.Dataset.eq("Vendor credits"), "Status"].iloc[0], "Ready")

    @patch("zoho_books.time.sleep")
    def test_vendor_credit_pagination_accepts_live_and_documented_keys(self, sleep):
        session = FakeSession([
            FakeResponse({"code": 0, "vendor_credits": [{"vendor_credit_id": "1"}], "page_context": {"has_more_page": True}}),
            FakeResponse({"code": 0, "vendorcredits": [{"vendor_credit_id": "2"}], "page_context": {"has_more_page": False}})])
        records = BooksClient(config(), session=session).list_records("vendorcredits", "vendorcredits")
        self.assertEqual([row["vendor_credit_id"] for row in records], ["1", "2"])

    def test_vendor_credit_parser_accepts_empty_lists_and_rejects_missing_or_conflicting_data(self):
        self.assertEqual(response_records({"vendor_credits": []}, "vendorcredits"), [])
        for payload in [{}, {"vendor_credits": None}, {"vendorcredits": [], "vendor_credits": [{"vendor_credit_id": "1"}]}]:
            with self.assertRaises(ZohoResponseError):
                response_records(payload, "vendorcredits")

    @patch("zoho_books.time.sleep")
    def test_response_shape_failure_is_not_labeled_as_a_permission_failure(self, sleep):
        resources = ["items", "invoices", "bills", "purchaseorders", "creditnotes", "vendorcredits"]
        session = FakeSession([FakeResponse({"code": 0, "organizations": [{"organization_id": "123"}]})] +
                              [FakeResponse({"code": 0}) if resource == "vendorcredits" else FakeResponse({"code": 0, resource: []}) for resource in resources])
        checks = check_read_access(BooksClient(config(), session=session))
        self.assertEqual(checks.loc[checks.Dataset.eq("Vendor credits"), "Status"].iloc[0], "Unexpected response")

    @patch("zoho_books.requests.post")
    def test_self_client_exchanges_one_time_code_and_retains_only_refresh_token(self, post):
        post.return_value = FakeResponse({"access_token": "short_lived", "refresh_token": "renewable", "expires_in": 3600})
        cfg = config()
        cfg.region = "eu"
        cfg.refresh_token = ""
        connected = exchange_grant_code(cfg, " fresh_code ")
        self.assertEqual(connected.refresh_token, "renewable")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://accounts.zoho.eu/oauth/v2/token")
        self.assertEqual(kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(kwargs["data"]["code"], "fresh_code")
        self.assertNotIn("code", connected.__dict__)
        self.assertNotIn("access_token", connected.__dict__)

    @patch("zoho_books.requests.post")
    def test_self_client_rejects_incomplete_setup_before_network(self, post):
        cfg = config()
        cfg.client_id = ""
        with self.assertRaisesRegex(ZohoError, "fresh grant code"):
            exchange_grant_code(cfg, "fresh_code")
        post.assert_not_called()

    @patch("zoho_books.requests.post")
    def test_rejected_grant_code_keeps_existing_token_and_does_not_echo_response(self, post):
        post.return_value = FakeResponse({"error": "invalid_code", "message": "sensitive grant value"}, 400)
        cfg = config()
        with self.assertRaises(ZohoError) as error:
            exchange_grant_code(cfg, "expired")
        self.assertEqual(cfg.refresh_token, "refresh")
        self.assertNotIn("sensitive grant value", str(error.exception))

    @patch("zoho_books.time.sleep")
    def test_access_check_reads_every_enabled_dataset(self, sleep):
        resources = ["items", "invoices", "bills", "purchaseorders", "creditnotes", "vendorcredits", "salesreceipts"]
        session = FakeSession([FakeResponse({"code": 0, "organizations": [{"organization_id": "123"}]})] +
                              [FakeResponse({"code": 0, resource: []}) for resource in resources])
        cfg = config()
        cfg.include_sales_receipts = True
        checks = check_read_access(BooksClient(cfg, session=session))
        self.assertEqual(len(checks), 7)
        self.assertTrue(checks.Status.eq("Ready").all())
        self.assertEqual([url.rsplit("/", 1)[-1] for url, kwargs in session.get_calls], ["organizations"] + resources)
        self.assertTrue(all(row["Read Scope"].endswith(".READ") for _, row in checks.iterrows()))
        self.assertEqual(len(session.post_calls), 1)
        self.assertTrue(session.post_calls[0][0].endswith("/oauth/v2/token"))

    @patch("zoho_books.time.sleep")
    def test_access_check_reports_missing_scope_without_marking_it_ready(self, sleep):
        resources = ["items", "invoices", "bills", "purchaseorders", "creditnotes", "vendorcredits"]
        session = FakeSession([FakeResponse({"code": 0, "organizations": [{"organization_id": "123"}]})] +
                              [FakeResponse({"code": 57}) if resource == "creditnotes" else FakeResponse({"code": 0, resource: []}) for resource in resources])
        checks = check_read_access(BooksClient(config(), session=session))
        self.assertEqual(checks.Status.eq("Needs access").sum(), 1)
        failed = checks.loc[checks.Status.eq("Needs access")].iloc[0]
        self.assertEqual(failed["Read Scope"], "ZohoBooks.creditnotes.READ")

    def test_access_check_stops_for_the_wrong_organization(self):
        session = FakeSession([FakeResponse({"code": 0, "organizations": [{"organization_id": "other"}]})])
        with self.assertRaisesRegex(ZohoError, "configured Books organization"):
            check_read_access(BooksClient(config(), session=session))
        self.assertEqual(len(session.get_calls), 1)

    @patch("zoho_books.time.sleep")
    def test_reads_all_pages_and_refreshes_once_on_401(self, sleep):
        session = FakeSession([FakeResponse({}, 401),
            FakeResponse({"code": 0, "items": [{"item_id": "1"}], "page_context": {"has_more_page": True}}),
            FakeResponse({"code": 0, "items": [{"item_id": "2"}], "page_context": {"has_more_page": False}})])
        records = BooksClient(config(), session=session).list_records("items", "items")
        self.assertEqual([r["item_id"] for r in records], ["1", "2"])
        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual(session.get_calls[-1][1]["params"]["page"], 2)
        self.assertEqual(session.get_calls[-1][1]["params"]["organization_id"], "123")

    def test_missing_pagination_does_not_publish_partial_data(self):
        client = BooksClient(config(), session=FakeSession([FakeResponse({"code": 0, "items": []})]))
        with self.assertRaisesRegex(ZohoError, "pagination"):
            client.list_records("items", "items")

    def test_api_failure_message_does_not_echo_response_secrets(self):
        client = BooksClient(config(), session=FakeSession([FakeResponse({"code": 57, "message": "sensitive token"})]))
        with self.assertRaises(ZohoError) as error:
            client.get("items")
        self.assertNotIn("sensitive token", str(error.exception))

    def test_configuration_rejects_overlap(self):
        cfg = config()
        cfg.locations["bonded"] = ["c"]
        with self.assertRaisesRegex(ZohoError, "once"):
            cfg.validate()

    def test_location_stock_reconciles_and_unmapped_stock_is_rejected(self):
        items = {"1": {"sku": "047-003-24", "stock_on_hand": 20, "locations": [
            {"location_id": "c", "location_stock_on_hand": 2}, {"location_id": "b", "location_stock_on_hand": 8},
            {"location_id": "t", "location_stock_on_hand": 10}]}}
        sources = stock_sources(items, config().locations)
        self.assertEqual(sources["stock_cy"].closing_stock.iloc[0], 20)
        self.assertEqual(sources["bonded_stock_file"].closing_stock.iloc[0], 8)
        items["1"]["locations"].append({"location_id": "unknown", "location_stock_on_hand": 1})
        with self.assertRaisesRegex(ZohoError, "Unmapped"):
            stock_sources(items, config().locations)

    def test_named_warehouse_rules_handle_case_spacing_and_transit_spelling(self):
        for name in ["DP World", " dp WORLD ", "DP   World"]:
            self.assertEqual(warehouse_group(name), "bonded")
        self.assertEqual(warehouse_group("DP-World"), "bonded")
        # "In-transit" is the name Books actually returns.
        for name in ["In Traansit", "In Transit", " IN  TRAANSIT ", "In-transit", "In - Transit"]:
            self.assertEqual(warehouse_group(name), "in_transit")
        for name in ["Main Warehouse", "TWC", "New Warehouse", "DP World Annex"]:
            self.assertEqual(warehouse_group(name), "cleared")

    def test_name_mapping_needs_no_manual_location_ids(self):
        cfg = config()
        cfg.warehouse_mapping_mode = "names"
        cfg.locations = {}
        self.assertIs(cfg.validate(), cfg)

    def test_named_warehouses_sum_all_cleared_locations_and_keep_bonded_transit_separate(self):
        items = {"1": {"sku": "047-003-24", "stock_on_hand": 100, "locations": [
            {"location_id": "b", "location_name": "DP World", "location_stock_on_hand": 30},
            {"location_id": "t", "location_name": "In Traansit", "location_stock_on_hand": 20},
            {"location_id": "c", "location_name": "Main Warehouse", "location_stock_on_hand": 40},
            {"location_id": "new", "location_name": "Another Warehouse", "location_stock_on_hand": 10}]}}
        sources = stock_sources(items)
        self.assertEqual(sources["stock_cy"].closing_stock.iloc[0], 100)
        self.assertEqual(sources["bonded_stock_file"].closing_stock.iloc[0], 30)
        self.assertEqual(sources["in_transit"].closing_stock.iloc[0], 20)
        self.assertEqual(sources["twc_stock"].closing_stock.iloc[0], 50)

    def test_combined_vintages_are_named_after_the_vintage_holding_the_stock(self):
        # Sales list the old vintage first; all stock is the new vintage in transit.
        items = {"23": {"sku": "039-002-23", "name": "Man Chardonnay 2023", "status": "inactive", "stock_on_hand": 0,
                        "created_time": "2024-04-25T14:29:46+0200"},
                 "25": {"sku": "039-002-25", "name": "Man Chardonnay 2025", "status": "active", "stock_on_hand": 0,
                        "created_time": "2025-10-16T11:09:33+0200"},
                 "26": {"sku": "039-002-26", "name": "Man Chardonnay 2026", "status": "active", "stock_on_hand": 360,
                        "created_time": "2026-09-04T15:57:04+0200"},
                 # Sold out everywhere: the active vintage, then the newest one.
                 "a": {"sku": "040-002-20", "name": "Glenelly Merlot 2020", "status": "inactive", "stock_on_hand": 0, "created_time": "2023"},
                 "b": {"sku": "040-002-21", "name": "Glenelly Merlot 2021", "status": "active", "stock_on_hand": 0, "created_time": "2024"},
                 "c": {"sku": "040-002-22", "name": "Glenelly Merlot 2022", "status": "inactive", "stock_on_hand": 0, "created_time": "2025"}}
        names = product_names(items).set_index("sku_key").item_name
        self.assertEqual(names["039002"], "Man Chardonnay 2026")
        self.assertEqual(names["040002"], "Glenelly Merlot 2021")
        sales = pd.DataFrame({"sku": ["039-002-23", "039-002-25"], "item_name": ["Man Chardonnay 2023", "Man Chardonnay 2025"],
                              "quantity_sold": [1, 5], "amount": [10, 50]})
        stock = pd.DataFrame({"sku": ["039-002-26"], "item_name": ["Man Chardonnay 2026"], "closing_stock": [360]})
        transit = pd.DataFrame({"sku": ["039-002-26"], "closing_stock": [360]})
        report, _ = build_weekly_report({"sales_cy": prepare_source(sales, "sales"), "stock_cy": prepare_source(stock, "stock"),
                                         "in_transit": prepare_source(transit, "in_transit_qty"),
                                         "product_names": product_names(items)}, 26)
        row = report.set_index("SKU").loc["039-002"]
        self.assertEqual(row["Item Name"], "Man Chardonnay 2026")
        self.assertEqual((row["CY Sales (YTD)"], row["Total Stock"], row["In Transit"]), (6, 360, 360))

    def test_unnamed_warehouses_are_not_silently_assigned_to_cleared(self):
        items = {"1": {"locations": [{"location_id": "b", "location_name": ""}]}}
        with self.assertRaisesRegex(ZohoError, "without a name"):
            groups_from_warehouse_names(items)
        items["2"] = {"locations": [{"location_id": "b", "location_name": "DP World"}]}
        self.assertEqual(groups_from_warehouse_names(items)["bonded"], ["b"])

    def test_customer_returns_and_exchange_rate_keep_sign_and_currency(self):
        items = {"1": {"sku": "047-003", "brand": "A"}}
        doc = {"date": "2026-10-01", "exchange_rate": 2, "line_items": [{"item_id": "1", "quantity": 3, "item_total": 20}]}
        result = transaction_rows([doc], items, "quantity_sold", -1)[0]
        self.assertEqual(result["quantity_sold"], -3)
        self.assertEqual(result["amount"], -40)

    def test_native_po_billed_and_cancelled_quantities_work_without_bill_line_links(self):
        items = {"1": {"sku": "047-003", "name": "Wine"}}
        orders = [{"status": "open", "line_items": [{"item_id": "1", "line_item_id": "L1", "quantity": 10,
                                                       "quantity_billed": 4, "quantity_cancelled": 1, "quantity_received": 0}]}]
        report, audit = open_orders(orders, [], items)
        self.assertEqual(report.open_po_qty.iloc[0], 4)
        self.assertEqual(report.billed_po_qty.iloc[0], 4)
        self.assertEqual(report.unbilled_po_qty.iloc[0], 5)
        self.assertEqual(audit["Billed Quantity Source"].iloc[0], "Books PO quantity_billed")

    def test_non_inventory_points_without_sku_are_audited_and_do_not_block_products(self):
        items = {"1": {"sku": "047-003", "brand": "Spier"},
                 "2": {"name": "Grape Points", "sku": "", "product_type": "goods", "track_inventory": False}}
        doc = {"date": "2026-10-01", "exchange_rate": 2,
               "line_items": [{"item_id": "1", "quantity": 3, "item_total": 20},
                              {"item_id": "2", "quantity": 100, "item_total": -5}]}
        exclusions = []
        rows = transaction_rows([doc], items, "quantity_sold", exclusions=exclusions)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity_sold"], 3)
        self.assertEqual(exclusions, [{"Item ID": "2", "Item Name": "Grape Points", "Source": "Sales",
                                      "Lines": 1, "Quantity": 100, "Amount (base currency)": -10,
                                      "transaction_date": exclusions[0]["transaction_date"]}])

    def test_inventory_product_without_sku_still_blocks_with_its_identity(self):
        doc = {"date": "2026-10-01", "line_items": [{"item_id": "1", "quantity": 3, "item_total": 20}]}
        with self.assertRaisesRegex(ZohoError, "Inventory product 'Wine'.*item ID 1.*no SKU"):
            transaction_rows([doc], {"1": {"name": "Wine", "sku": "", "track_inventory": True}}, "quantity_sold")

    def test_partially_billed_po_counts_only_outstanding_units_with_exact_item_match(self):
        items = {"1": {"sku": "047-003-24", "name": "Wine"}}
        orders = [{"status": "open", "purchaseorder_number": "PO-1", "line_items": [{"item_id": "1", "line_item_id": "line1", "quantity": 100, "quantity_received": 0}]}]
        bills = [{"line_items": [{"purchaseorder_item_id": "line1", "quantity": 40}]}]
        totals, audit = open_orders(orders, bills, items)
        self.assertEqual(totals.open_po_qty.iloc[0], 40)
        self.assertEqual(audit["Match"].iloc[0], "Exact Books item ID")
        orders[0]["status"] = "draft"
        self.assertTrue(open_orders(orders, bills, items)[0].empty)

    def test_annual_benchmark_rejects_partial_or_future_year_without_network(self):
        with self.assertRaisesRegex(ZohoError, "completed calendar year"):
            fetch_annual_sales(None, 2026, date(2026, 10, 30))

    @patch("zoho_books.time.sleep")
    def test_document_cache_invalidates_changes_and_excludes_deleted_records(self, sleep):
        record = {"invoice_id": "1", "date": "2026-01-01", "status": "sent", "last_modified_time": "v1"}
        def listing(rows):
            return FakeResponse({"code": 0, "invoices": rows, "page_context": {"has_more_page": False}})
        with TemporaryDirectory() as tmp:
            responses = [listing([record]), FakeResponse({"code": 0, "invoice": {**record, "line_items": []}})]
            first = list(BooksClient(config(), session=FakeSession(responses), cache_dir=tmp).documents("invoices", "invoice", "invoice_id"))
            cached = FakeSession([listing([record])])
            self.assertEqual(list(BooksClient(config(), session=cached, cache_dir=tmp).documents("invoices", "invoice", "invoice_id")), first)
            self.assertEqual(len(cached.get_calls), 1)
            changed = {**record, "last_modified_time": "v2", "status": "paid"}
            changed_session = FakeSession([listing([changed]), FakeResponse({"code": 0, "invoice": {**changed, "line_items": []}})])
            list(BooksClient(config(), session=changed_session, cache_dir=tmp).documents("invoices", "invoice", "invoice_id"))
            self.assertEqual(len(changed_session.get_calls), 2)
            deleted = FakeSession([listing([])])
            self.assertEqual(list(BooksClient(config(), session=deleted, cache_dir=tmp).documents("invoices", "invoice", "invoice_id")), [])

    @patch("zoho_books.time.sleep")
    def test_due_day_display_changes_do_not_redownload_unchanged_invoice_lines(self, sleep):
        record = {"invoice_id": "1", "date": "2026-01-01", "status": "sent", "last_modified_time": "v1", "due_days": "4 days ago"}
        def listing(row):
            return FakeResponse({"code": 0, "invoices": [row], "page_context": {"has_more_page": False}})
        with TemporaryDirectory() as tmp:
            first = FakeSession([listing(record), FakeResponse({"code": 0, "invoice": {**record, "line_items": []}})])
            list(BooksClient(config(), session=first, cache_dir=tmp).documents("invoices", "invoice", "invoice_id"))
            second = FakeSession([listing({**record, "due_days": "5 days ago"})])
            result = list(BooksClient(config(), session=second, cache_dir=tmp).documents("invoices", "invoice", "invoice_id"))
            self.assertEqual(len(result), 1)
            self.assertEqual(len(second.get_calls), 1)


class PipelineAndPublicationTests(unittest.TestCase):
    def inputs(self):
        sales = pd.DataFrame({"sku": ["047-003-24"], "brand": ["Spier"], "quantity_sold": [50], "amount": [1000]})
        stock = pd.DataFrame({"sku": ["047-003-24"], "closing_stock": [0]})
        return {"sales_cy": prepare_source(sales, "sales"), "stock_cy": prepare_source(stock, "stock"),
                "planning": prepare_planning(pd.DataFrame({"SKU": ["047-003"], "Current Year Sales": [1000], "Next Year Budget": [1200], "Growth Target %": [.2]}))}

    def test_shared_pipeline_applies_brand_growth_and_subtracts_po_without_changing_stock(self):
        inputs = self.inputs()
        report, health = build_weekly_report(inputs, weeks_elapsed=26, brand_rates={"Spier": .3})
        need = report["Bonded Est. Reorder Qty"].iloc[0]
        self.assertEqual(report["Annual Target"].iloc[0], 1300)
        inputs["purchase_orders"] = pd.DataFrame({"sku_key": ["047003"], "open_po_qty": [need - 5]})
        partial, _ = build_weekly_report(inputs, 26, brand_rates={"Spier": .3})
        self.assertEqual(partial["Bonded Est. Reorder Qty"].iloc[0], 5)
        self.assertEqual(partial["Bonded Reorder"].iloc[0], "REORDER")
        self.assertEqual(partial["Total Stock"].iloc[0], 0)
        inputs["purchase_orders"]["open_po_qty"] = need
        covered, _ = build_weekly_report(inputs, 26, brand_rates={"Spier": .3})
        self.assertEqual(covered["Bonded Reorder"].iloc[0], "")
        self.assertEqual(covered["Bonded Est. Reorder Qty"].iloc[0], 0)

    def test_failed_snapshot_keeps_previous_report_and_metadata_together(self):
        with TemporaryDirectory() as tmp:
            publish_report(tmp, pd.DataFrame({"SKU": ["old"]}), {"version": 1})
            with self.assertRaises(TypeError):
                publish_report(tmp, pd.DataFrame({"SKU": ["new"]}), {"unsupported": object()})
            report, meta, audit = load_report(tmp)
            self.assertEqual(report.SKU.tolist(), ["old"])
            self.assertEqual(meta, {"version": 1})

    def test_legacy_saved_dashboard_remains_readable(self):
        with TemporaryDirectory() as tmp:
            pd.DataFrame({"SKU": ["legacy"]}).to_csv(Path(tmp) / "report.csv", index=False)
            (Path(tmp) / "report_meta.json").write_text(json.dumps({"report_week": 37}))
            report, meta, _ = load_report(tmp)
            self.assertEqual(report.SKU.tolist(), ["legacy"])
            self.assertEqual(meta["report_week"], 37)

    def test_export_keeps_sample_labels_and_exact_po_audit(self):
        from io import BytesIO
        import openpyxl
        report, _ = build_weekly_report(self.inputs(), 26)
        audit = pd.DataFrame({"PO Number": ["PO-1"], "Ordered Qty": [100], "Outstanding Qty": [60]})
        workbook = openpyxl.load_workbook(BytesIO(report_excel_bytes(report, audit)), data_only=False)
        self.assertEqual(workbook.sheetnames, ["Weekly Report", "Supplier Reorders", "Clear from Bond", "PO Match Audit", "Colour Key"])
        headers = [cell.value for cell in workbook["Weekly Report"][1]]
        self.assertIn("Current Year Sales YTD", headers)
        self.assertIn("Qty to order from Supplier", headers)
        self.assertEqual(len(headers), 26)
        self.assertNotIn("Growth Target %", headers)
        self.assertEqual(workbook["Weekly Report"].cell(2, headers.index("Annual Target") + 1).value, 1200)
        audit_headers = [c.value for c in workbook["PO Match Audit"][1]]
        self.assertEqual(len(audit_headers), 14)
        self.assertEqual(workbook["PO Match Audit"].cell(2, audit_headers.index("Ordered Qty") + 1).value, 100)

    def test_attention_colours_follow_report_thresholds_and_actions(self):
        from report_store import attention_styles
        view = pd.DataFrame({"SKU": ["a", "b", "c"], "Sales Performance": ["🟢 Ahead", "🔴 Critical", ""],
                             "Current Year Sales Vs. Target %": [.15, -.2, None], "Weeks Coverage": [4, 21, 53],
                             "Reorder from Supplier": ["REORDER", "", ""], "Qty to order from Supplier": [240, 0, 0],
                             "Clear bonded stock": ["", "CLEAR", ""], "Qty to be cleared from Bond": [0, 60, 0]})
        styles = attention_styles(view)
        self.assertEqual(styles["Sales Performance"].tolist(), ["good", "bad", ""])
        self.assertEqual(styles["Current Year Sales Vs. Target %"].tolist(), ["good", "warn", ""])
        self.assertEqual(styles["Weeks Coverage"].tolist(), ["bad", "neutral", "info"])
        self.assertEqual(styles["Reorder from Supplier"].tolist(), ["reorder", "", ""])
        self.assertEqual(styles["Qty to order from Supplier"].tolist(), ["reorder_qty", "muted", "muted"])
        self.assertEqual(styles["Clear bonded stock"].tolist(), ["", "clear", ""])
        self.assertEqual(styles["Qty to be cleared from Bond"].tolist(), ["muted", "clear_qty", "muted"])
        self.assertEqual(styles["SKU"].tolist(), ["bad", "info", ""])

    def test_published_coverage_columns_count_weeks_of_stock(self):
        report, _ = build_weekly_report(self.inputs(), 26)
        view = weekly_report_view(report)
        rate = view["Avg Weekly Sales"].where(view["Avg Weekly Sales"] > 0)
        pd.testing.assert_series_equal(view["Weeks Coverage"], (view["Cleared Stock"] + view["Bonded Stock"] + view["In Transit"]) / rate,
                                       check_names=False)
        pd.testing.assert_series_equal(view["Cleared Stock Weeks Coverage"], view["Cleared Stock"] / rate, check_names=False)

    def test_action_sheets_list_only_flagged_products_most_urgent_first(self):
        from io import BytesIO
        import openpyxl
        report, _ = build_weekly_report(self.inputs(), 26)
        workbook = openpyxl.load_workbook(BytesIO(report_excel_bytes(report)))
        view = weekly_report_view(report)
        for sheet, flag, value in [("Supplier Reorders", "Reorder from Supplier", "REORDER"), ("Clear from Bond", "Clear bonded stock", "CLEAR")]:
            rows = list(workbook[sheet].iter_rows(min_row=2, values_only=True))
            self.assertEqual(len(rows), int(view[flag].eq(value).sum()))


class BooksPipelineIntegrationTests(unittest.TestCase):
    def test_full_pull_uses_matching_periods_and_keeps_live_stock_separate(self):
        item = {"item_id": "1", "sku": "047-003-24", "name": "Wine", "brand": "Spier", "stock_on_hand": 20,
                "locations": [{"location_id": "c", "location_stock_on_hand": 2},
                              {"location_id": "b", "location_stock_on_hand": 8},
                              {"location_id": "t", "location_stock_on_hand": 10}]}
        calls = []
        class FixtureClient:
            def __init__(self):
                self.config, self.calls = config(), 0

            def progress(self, message):
                pass

            def list_records(self, resource, key, params=None):
                return [item]

            def get(self, resource, params=None):
                return {"code": 0, "items": [item]}

            def documents(self, resource, singular, id_field, start=None, end=None, params=None):
                calls.append((resource, start, end))
                if resource == "items":
                    return iter([item])
                if resource == "invoices" and end.year == 2026:
                    return iter([{"date": end.isoformat(), "exchange_rate": 1,
                                  "line_items": [{"item_id": "1", "quantity": 10, "item_total": 100}]}])
                if resource == "invoices":
                    # Last year is read to 31 Dec for PO checks; only the matching YTD period is sales.
                    return iter([{"date": day, "exchange_rate": 1, "line_items": [{"item_id": "1", "quantity": qty, "item_total": 100}]}
                                 for day, qty in [("2025-10-25", 5), ("2025-12-20", 7)]])
                return iter([])
        inputs, audit, metadata = fetch_weekly_inputs(FixtureClient(), date(2026, 10, 25), date(2026, 10, 30))
        self.assertIn(("invoices", date(2026, 1, 1), date(2026, 10, 25)), calls)
        self.assertIn(("invoices", date(2025, 1, 1), date(2025, 12, 31)), calls)
        self.assertEqual(inputs["sales_cy"].quantity_sold.iloc[0], 10)
        self.assertEqual(inputs["sales_py"].quantity_sold.iloc[0], 5)
        self.assertEqual(inputs["stock_cy"].closing_stock.iloc[0], 20)
        self.assertEqual(inputs["in_transit"].in_transit_qty.iloc[0], 10)
        # Stock is dated by the snapshot actually taken (South Africa date), not the sales cutoff.
        snapshot_day = datetime.fromisoformat(metadata["stock_snapshot_at"]).astimezone(timezone(timedelta(hours=2))).date()
        self.assertEqual(metadata["stock_as_of"], snapshot_day.isoformat())
        self.assertEqual(metadata["sales_as_of"], "2026-10-25")
        report, _ = build_weekly_report(inputs, elapsed_weeks(date(2026, 10, 25)))
        self.assertEqual(report["Total Stock"].iloc[0], 20)
        self.assertEqual(report["Item Name"].iloc[0], "Wine")


class BackgroundPullTests(unittest.TestCase):
    def test_pull_returns_immediately_reuses_running_job_and_reports_progress(self):
        from threading import Event
        from zoho_jobs import PullManager
        manager = PullManager()
        entered, release = Event(), Event()
        def work(job):
            job.update("Reading invoices: 10/100")
            entered.set()
            release.wait(5)
            return "complete result"
        try:
            job = manager.start("same-source", work)
            self.assertTrue(entered.wait(2))
            self.assertEqual(job.snapshot()["state"], "running")
            self.assertIn("10/100", job.snapshot()["message"])
            self.assertIs(manager.start("same-source", work), job)
            release.set()
        finally:
            manager.executor.shutdown(wait=True)
        self.assertEqual(job.snapshot()["state"], "complete")
        self.assertEqual(job.result, "complete result")

    def test_cancelled_pull_does_not_expose_a_partial_result(self):
        from threading import Event
        from zoho_jobs import PullManager
        manager = PullManager()
        entered, release = Event(), Event()
        def work(job):
            entered.set()
            release.wait(5)
            job.check_cancelled()
            return "partial result"
        try:
            job = manager.start("cancel-source", work)
            self.assertTrue(entered.wait(2))
            manager.cancel(job.id)
            release.set()
        finally:
            manager.executor.shutdown(wait=True)
        self.assertEqual(job.snapshot()["state"], "cancelled")
        self.assertIsNone(job.result)


if __name__ == "__main__":
    unittest.main()
