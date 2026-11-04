"""Read-only Zoho Books API access, with OAuth refresh and paginated reads.

Sales and purchases use dated transactions. Stock is an explicitly dated LIVE
snapshot of the configured locations, not a reconstruction of historical stock.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
import tomllib

import pandas as pd
import requests

from report_inputs import prepare_source, sku_to_group_key
from zoho_sync_cache import TransactionIndex, write_json
from zoho_pull_cache import PO_QUANTITY_BASIS, load_item_snapshot, load_item_snapshot_at, save_item_snapshot, load_weekly_snapshot, save_weekly_snapshot
from seed_invoice_cache import exported_detail, load_export_seed


class ZohoError(ValueError):
    pass


# Saved weekly inputs hold stock already split into groups; bump this when warehouse_group changes.
WAREHOUSE_RULES = "names_v2_ignore_punctuation"


class ZohoResponseError(ZohoError):
    """A successful Books response has an unsupported or inconsistent shape."""


EXCLUDED_DOCUMENT_STATUSES = {"draft", "void", "cancelled", "canceled", "pending_approval", "submitted", "rejected", "pending"}


def response_records(payload, key):
    """Read the documented key and the live Books vendor-credit key variant."""
    keys = ("vendorcredits", "vendor_credits") if key in {"vendorcredits", "vendor_credits"} else (key,)
    present = [name for name in keys if name in payload]
    if not present or any(not isinstance(payload[name], list) for name in present):
        raise ZohoResponseError(f"Books did not return the expected {key} list.")
    records = payload[present[0]]
    if any(payload[name] != records for name in present[1:]):
        raise ZohoResponseError(f"Books returned conflicting {key} lists.")
    return records


# Documented Books scopes for the endpoints used by the reporting pipeline.
SELF_CLIENT_SCOPES = ",".join([
    "ZohoBooks.settings.READ", "ZohoBooks.invoices.READ", "ZohoBooks.creditnotes.READ",
    "ZohoBooks.bills.READ", "ZohoBooks.debitnotes.READ", "ZohoBooks.purchaseorders.READ",
])


REGIONS = {
    "com": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "com.au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
    "com.cn": ("https://accounts.zoho.com.cn", "https://www.zohoapis.com.cn"),
    "sa": ("https://accounts.zoho.sa", "https://www.zohoapis.sa"),
}


@dataclass(repr=False)
class BooksConfig:
    organization_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    region: str = "com"
    warehouse_mapping_mode: str = "names"
    locations: dict = field(default_factory=dict)
    include_credit_notes: bool = True
    include_vendor_credits: bool = True
    include_sales_receipts: bool = False
    request_interval: float = .7
    max_api_calls: int = 250
    full_reconcile_hours: float = 24
    pull_cache_minutes: float = 15
    stock_cache_minutes: float = 15

    def validate(self, require_locations=True):
        missing = [k for k in ["organization_id", "client_id", "client_secret", "refresh_token"] if not getattr(self, k)]
        if missing:
            raise ZohoError("Zoho Books connection needs: " + ", ".join(missing) + ". Add these in .streamlit/secrets.toml under [zoho].")
        if self.region not in REGIONS:
            raise ZohoError("Choose a supported Zoho data-center region.")
        if self.warehouse_mapping_mode not in {"names", "ids"}:
            raise ZohoError("Warehouse mapping must use names or explicit location IDs.")
        if require_locations and self.warehouse_mapping_mode == "ids":
            if set(self.locations) != {"cleared", "bonded", "in_transit"}:
                raise ZohoError("Map the cleared, bonded and in_transit location groups in [zoho.locations].")
            ids = []
            for group in self.locations.values():
                if not isinstance(group, list):
                    raise ZohoError("Each location group must be an array of location IDs.")
                ids.extend(str(v) for v in group)
            if not ids or len(ids) != len(set(ids)):
                raise ZohoError("Location IDs must be mapped once each. No location can belong to two stock groups.")
        if self.request_interval < .6 or self.max_api_calls < 1:
            raise ZohoError("Request interval must be at least 0.6 seconds and the call budget must be positive.")
        if not math.isfinite(self.full_reconcile_hours) or self.full_reconcile_hours <= 0:
            raise ZohoError("The history reconciliation interval must be positive.")
        if any(not math.isfinite(value) or value < 0 for value in [self.pull_cache_minutes, self.stock_cache_minutes]):
            raise ZohoError("The short cache durations must be nonnegative.")
        return self


def load_config(path=None):
    path = Path(path) if path else Path(__file__).parent / ".streamlit" / "secrets.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw.get("zoho", {})
    keys = BooksConfig.__dataclass_fields__
    values = {k: v for k, v in raw.items() if k in keys}
    local = Path(__file__).parent / "zoho.local.json"
    if path == Path(__file__).parent / ".streamlit" / "secrets.toml" and local.exists():
        values.update({k: v for k, v in json.loads(local.read_text(encoding="utf-8")).items() if k in keys})
    for key in ["organization_id", "client_id", "client_secret", "refresh_token", "region"]:
        if os.environ.get("ZOHO_" + key.upper()):
            values[key] = os.environ["ZOHO_" + key.upper()]
    return BooksConfig(**values)


def save_local_config(config):
    config.validate(require_locations=False)
    destination = Path(__file__).parent / "zoho.local.json"
    pending = destination.with_suffix(".tmp")
    pending.write_text(json.dumps(config.__dict__, indent=2), encoding="utf-8")
    pending.replace(destination)


def exchange_grant_code(config, code):
    """Exchange a short-lived Self Client grant locally; never return or log secrets."""
    missing = [key for key in ["organization_id", "client_id", "client_secret"] if not str(getattr(config, key) or "").strip()]
    if missing or not str(code or "").strip():
        raise ZohoError("Self Client setup needs the organization ID, client ID, client secret and a fresh grant code.")
    if config.region not in REGIONS:
        raise ZohoError("Choose a supported data-center region.")
    try:
        response = requests.post(REGIONS[config.region][0] + "/oauth/v2/token", data={
            "grant_type": "authorization_code", "client_id": config.client_id,
            "client_secret": config.client_secret, "code": code.strip(),
        }, timeout=60)
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise ZohoError("Could not exchange the Zoho grant code. Check your connection and data-center region.") from None
    if response.status_code != 200 or not payload.get("refresh_token"):
        raise ZohoError("Zoho rejected the grant code. Generate a fresh Self Client code with the listed read scopes.")
    config.refresh_token = payload["refresh_token"]
    return config


def check_read_access(client):
    """Verify the selected organization and every enabled report data source."""
    response = client.get("organizations")
    if not any(str(org.get("organization_id")) == client.config.organization_id for org in response.get("organizations", [])):
        raise ZohoError("This Self Client cannot access the configured Books organization. Generate its code for that organization or correct the organization ID.")
    resources = [("Items and warehouse stock", "items", "ZohoBooks.settings.READ"),
                 ("Sales invoices", "invoices", "ZohoBooks.invoices.READ"),
                 ("Purchase bills", "bills", "ZohoBooks.bills.READ"),
                 ("Purchase orders", "purchaseorders", "ZohoBooks.purchaseorders.READ")]
    if client.config.include_credit_notes:
        resources.append(("Customer credit notes", "creditnotes", "ZohoBooks.creditnotes.READ"))
    if client.config.include_vendor_credits:
        resources.append(("Vendor credits", "vendorcredits", "ZohoBooks.debitnotes.READ"))
    if client.config.include_sales_receipts:
        resources.append(("Sales receipts", "salesreceipts", "ZohoBooks.invoices.READ"))
    rows = []
    for label, resource, scope in resources:
        try:
            payload = client.get(resource, {"page": 1, "per_page": 1})
            response_records(payload, resource)
            rows.append({"Dataset": label, "Read Scope": scope, "Status": "Ready", "Detail": "Read access confirmed"})
        except ZohoResponseError as exc:
            rows.append({"Dataset": label, "Read Scope": scope, "Status": "Unexpected response", "Detail": str(exc)})
        except ZohoError as exc:
            rows.append({"Dataset": label, "Read Scope": scope, "Status": "Needs access", "Detail": str(exc)})
    return pd.DataFrame(rows)


class BooksClient:
    def __init__(self, config, session=None, cache_dir=None, progress=None, cancel_check=None, recheck_history=False,
                 force_refresh=False, cache_only=False):
        self.config = config.validate(require_locations=False)
        self.session = session or requests.Session()
        self.accounts, self.api = REGIONS[config.region]
        self.token = None
        self.token_expires = 0
        self.last_request = 0
        self.calls = 0
        self.calls_by_resource = {}
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._progress = progress or (lambda message: None)
        self.stage = ""
        self.recheck_history = recheck_history
        self.force_refresh = force_refresh
        self.cache_only = cache_only
        self.stock_snapshot_at = None
        self._export_seeds = {}
        self.export_reused = 0
        self.export_reused_by_resource = {}
        self.sync_stats = {"downloaded": 0, "reused": 0, "incremental_checks": 0, "full_checks": 0}
        self.cancel_check = cancel_check or (lambda: None)

    def progress(self, message):
        message = f"{self.stage} — {message}" if self.stage else message
        self._progress(f"{message} ({self.calls:,} Books API calls)")

    def _refresh(self):
        self.cancel_check()
        try:
            response = self.session.post(self.accounts + "/oauth/v2/token", data={
                "grant_type": "refresh_token", "refresh_token": self.config.refresh_token,
                "client_id": self.config.client_id, "client_secret": self.config.client_secret,
            }, timeout=60)
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise ZohoError("Could not refresh Zoho access. Check the connection and data-center region.") from None
        if response.status_code != 200 or not payload.get("access_token"):
            raise ZohoError("Zoho rejected the OAuth refresh. Check the local client credentials and refresh token.")
        self.token = payload["access_token"]
        self.token_expires = time.monotonic() + min(float(payload.get("expires_in", 3600)), 3600) - 60

    def get(self, resource, params=None):
        if self.cache_only:
            raise ZohoError("Saved-data mode does not make Zoho API calls. No complete cache exists for this selection.")
        if not resource or resource.startswith("/") or ":" in resource or ".." in resource:
            raise ZohoError("Invalid Books resource path.")
        refreshed = False
        for attempt in range(5):
            self.cancel_check()
            if not self.token or time.monotonic() >= self.token_expires:
                self._refresh()
            if self.calls >= self.config.max_api_calls:
                raise ZohoError("The configured API call budget was reached. The saved report has not been replaced.")
            delay = self.config.request_interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.calls += 1
            category = resource.split("/")[0]
            self.calls_by_resource[category] = self.calls_by_resource.get(category, 0) + 1
            self.last_request = time.monotonic()
            try:
                response = self.session.get(self.api + "/books/v3/" + resource,
                    params={**(params or {}), "organization_id": self.config.organization_id},
                    headers={"Authorization": "Zoho-oauthtoken " + self.token}, timeout=90)
            except requests.RequestException:
                if attempt == 4:
                    raise ZohoError("Zoho Books could not be reached. The previous report is unchanged.") from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 401 and not refreshed:
                self._refresh()
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    raise ZohoError(f"Zoho Books is unavailable or rate limited (HTTP {response.status_code}).")
                try:
                    retry = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    retry = 2 ** attempt
                time.sleep(min(max(retry, 1), 30))
                continue
            try:
                payload = response.json()
            except ValueError:
                raise ZohoError("Zoho returned an unexpected non-JSON response.") from None
            if response.status_code >= 400 or payload.get("code") != 0:
                raise ZohoError(f"Books read failed for {resource.split('/')[0]} (HTTP {response.status_code}, code {payload.get('code', 'unknown')}). Check read scopes and organization access.")
            return payload
        raise ZohoError("Could not complete the Zoho read.")

    def list_records(self, resource, key, params=None):
        rows, page = [], 1
        while True:
            self.progress(f"{resource}: listing page {page}, {len(rows):,} records read")
            payload = self.get(resource, {**(params or {}), "page": page, "per_page": 200})
            batch = response_records(payload, key)
            if "has_more_page" not in payload.get("page_context", {}):
                raise ZohoError(f"Incomplete pagination data for {resource}; the report was not generated.")
            rows.extend(batch)
            if not payload["page_context"]["has_more_page"]:
                self.progress(f"{resource}: {len(rows):,} records listed")
                return rows
            if not batch or page >= 10000:
                raise ZohoError(f"Pagination did not complete for {resource}.")
            page += 1

    def documents(self, resource, singular, id_field, start=None, end=None, params=None):
        filters = dict(params or {})
        if start:
            filters.update(date_start=start.isoformat(), date_end=end.isoformat())
        self.progress("Reading " + resource)
        account_key = hashlib.sha256((self.api + self.config.organization_id).encode()).hexdigest()[:16]
        directory = self.cache_dir / account_key / resource if self.cache_dir else None
        index = None
        if directory and start and end and not params and resource in {"invoices", "creditnotes", "bills", "vendorcredits"}:
            index = TransactionIndex(directory / ("_index_" + start.isoformat() + ".json"), id_field, start, end,
                                     self.config.full_reconcile_hours, self.recheck_history)
            records = index.read(lambda query: self.list_records(resource, resource, query), self.progress)
            self.sync_stats["incremental_checks" if index.incremental else "full_checks"] += 1
        else:
            records = self.list_records(resource, resource, filters)
        if resource == "purchaseorders":
            records = [row for row in records if po_summary_is_relevant(row)]
        seen = set()
        for position, record in enumerate(records, 1):
            self.cancel_check()
            identifier = str(record.get(id_field, ""))
            if not identifier or identifier in seen:
                raise ZohoError(f"Missing or repeated {id_field} in Books pagination.")
            seen.add(identifier)
            # Date/status are checked locally even when the server applies filters.
            if start and not start <= _document_date(record) <= end:
                continue
            if str(record.get("status", "")).lower() in EXCLUDED_DOCUMENT_STATUSES:
                continue
            # Relative due-day labels change daily without changing sales lines.
            # Cache on source modifications and accounting fields, not display text.
            version = {key: record.get(key) for key in [id_field, "last_modified_time", "date", "status", "total", "exchange_rate"]}
            fingerprint = hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()
            cache = directory / (identifier + ".json") if directory else None
            # Only cache transactions whose list response carries a modification timestamp.
            can_cache = bool(record.get("last_modified_time")) and resource != "items"
            detail = None
            if can_cache and directory and resource in {"invoices", "bills"}:
                if resource not in self._export_seeds:
                    self._export_seeds[resource] = load_export_seed(directory, resource)
                bundle = self._export_seeds[resource]
                seeded = bundle[resource].get(identifier)
                if seeded is not None:
                    detail = exported_detail({"detail": seeded, "export_seed": bundle}, record, id_field)
                    if detail is not None:
                        self.export_reused += 1
                        self.export_reused_by_resource[resource] = self.export_reused_by_resource.get(resource, 0) + 1
            po_fresh = (resource != "purchaseorders" or (cache and cache.exists() and not self.force_refresh and
                        time.time() - cache.stat().st_mtime <= self.config.pull_cache_minutes * 60))
            if detail is None and can_cache and cache and cache.exists() and po_fresh:
                try:
                    saved = json.loads(cache.read_text(encoding="utf-8"))
                    if saved.get("fingerprint") == fingerprint:
                        detail = saved["detail"]
                    elif resource in {"invoices", "bills"} and saved.get("export_seed"):
                        detail = exported_detail(saved, record, id_field)
                        if detail is not None:
                            write_json(cache, {"fingerprint": fingerprint, "detail": detail, "export_seed": saved["export_seed"]})
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if detail is None:
                self.progress(f"{resource}: downloading new/changed record {position:,}/{len(records):,}; {self.calls:,} API calls")
                detail = self.get(resource + "/" + identifier).get(singular)
                if not isinstance(detail, dict):
                    raise ZohoError(f"Books did not return {singular} details.")
                self.sync_stats["downloaded"] += 1
                if can_cache and cache:
                    write_json(cache, {"fingerprint": fingerprint, "detail": detail})
            else:
                self.sync_stats["reused"] += 1
                if position % 100 == 0 or position == len(records):
                    self.progress(f"{resource}: {position:,}/{len(records):,} checked, using saved unchanged records")
            if start and not start <= _document_date(detail) <= end:
                continue
            if str(detail.get("status", "")).lower() not in EXCLUDED_DOCUMENT_STATUSES:
                yield detail
        self.cancel_check()
        if index is not None:
            index.commit()


def _document_date(document):
    try:
        return date.fromisoformat(document["date"][:10])
    except (KeyError, TypeError, ValueError):
        raise ZohoError("A Books transaction has no valid transaction date.") from None


def _number(value, label):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ZohoError(f"Books is missing a numeric {label}.") from None
    if not math.isfinite(number):
        raise ZohoError(f"Books returned an invalid {label}.")
    return number


def _metadata(item):
    custom = {str(f.get("api_name", f.get("label", ""))).lower().replace(" ", "_"): f.get("value")
              for f in item.get("custom_fields", [])}
    origin = next((v for k, v in custom.items() if "country" in k or "origin" in k), "") or item.get("cf_country_of_origin", "")
    brand = item.get("brand") or item.get("brand_name") or next((v for k, v in custom.items() if "brand" in k), "")
    return {"sku": item.get("sku", ""), "item_name": item.get("name", item.get("item_name", "")),
            "brand": brand, "country_of_origin": origin, "category_name": item.get("category_name", "")}


def load_items(client, include_stock=True):
    """Read current item stock in supported batches instead of one call per item."""
    saved = load_item_snapshot(client)
    if saved:
        client.stock_snapshot_at = saved["saved_at"]
        client.progress("Using warehouse/item snapshot from " + saved["saved_at"])
        return saved["items"]
    summaries = client.list_records("items", "items", {"filter_by": "Status.All"})
    items = {}
    for item in summaries:
        identifier = str(item.get("item_id", ""))
        if not identifier or identifier in items:
            raise ZohoResponseError("Books returned a missing or repeated item ID.")
        items[identifier] = item
    if not include_stock:
        return items
    identifiers = [identifier for identifier, item in items.items()
                   if item.get("track_inventory") is not False and item.get("product_type") != "service"]
    batch_size = 20
    for start in range(0, len(identifiers), batch_size):
        batch = identifiers[start:start + batch_size]
        client.progress(f"Warehouse stock: {start:,}/{len(identifiers):,} items, batch {start // batch_size + 1}")
        details = response_records(client.get("itemdetails", {"item_ids": ",".join(batch)}), "items")
        returned = set()
        for detail in details:
            identifier = str(detail.get("item_id", ""))
            if identifier not in batch or identifier in returned:
                raise ZohoResponseError("The item batch contained an unexpected or repeated item ID.")
            returned.add(identifier)
            # The list includes brand and tracking fields omitted by itemdetails.
            items[identifier] = {**items[identifier], **detail}
        if returned != set(batch):
            raise ZohoResponseError("Books omitted items from a stock batch. The report has not been replaced.")
        client.progress(f"Warehouse stock: {start + len(batch):,}/{len(identifiers):,} items read")
    save_item_snapshot(client, items)
    return items


def transaction_rows(documents, items, quantity_column, sign=1, exclusions=None):
    rows = []
    for doc in documents:
        if not isinstance(doc.get("line_items"), list):
            raise ZohoError("Books transaction details are missing line items.")
        exchange_rate = _number(doc.get("exchange_rate", 1), "exchange rate")
        if exchange_rate <= 0:
            raise ZohoError("Books returned a nonpositive exchange rate.")
        for line in doc["line_items"]:
            if line.get("line_item_category", "line_item") != "line_item" or not line.get("item_id"):
                continue
            item = items.get(str(line["item_id"]))
            if item is None:
                raise ZohoError("A transaction references an item not returned by the item master. Check access to inactive items.")
            if not item.get("sku"):
                if item.get("track_inventory") is False or item.get("product_type") in {"service", "services"}:
                    if exclusions is not None:
                        exclusions.append({"Item ID": str(line["item_id"]), "Item Name": _metadata(item)["item_name"],
                                           "Source": "Sales" if quantity_column == "quantity_sold" else "Purchases",
                                           "Lines": 1, "Quantity": _number(line.get("quantity"), "line quantity") * sign,
                                           "Amount (base currency)": _number(line.get("item_total"), "line item total") * exchange_rate * sign,
                                           "transaction_date": doc["date"][:10]})
                    continue
                raise ZohoError(f"Inventory product '{_metadata(item)['item_name']}' (item ID {line['item_id']}) has no SKU in Books. Add its SKU before refreshing.")
            quantity = _number(line.get("quantity"), "line quantity")
            amount = _number(line.get("item_total"), "line item total")
            # Item totals are the API's line amounts. Preserve their accounting meaning.
            rows.append({**_metadata(item), quantity_column: quantity * sign,
                         "amount": amount * exchange_rate * sign, "transaction_date": doc["date"][:10]})
    return rows


def _sales(client, items, start, end, exclusions=None):
    rows = transaction_rows(client.documents("invoices", "invoice", "invoice_id", start, end), items, "quantity_sold", exclusions=exclusions)
    if client.config.include_credit_notes:
        rows += transaction_rows(client.documents("creditnotes", "creditnote", "creditnote_id", start, end), items, "quantity_sold", -1, exclusions)
    if client.config.include_sales_receipts:
        rows += transaction_rows(client.documents("salesreceipts", "salesreceipt", "sales_receipt_id", start, end), items, "quantity_sold", exclusions=exclusions)
    return pd.DataFrame(rows, columns=["sku", "item_name", "brand", "country_of_origin", "category_name", "quantity_sold", "amount", "transaction_date"])


def warehouse_group(name):
    """Apply the user's warehouse rules without fuzzy matching other names."""
    # Books names the transit warehouse "In-transit", so ignore punctuation as well as case and spacing.
    normalized = "".join(ch for ch in str(name or "").casefold() if ch.isalnum())
    if not normalized:
        raise ZohoError("Books returned a warehouse without a name. Its stock group cannot be identified.")
    if normalized == "dpworld":
        return "bonded"
    if normalized in {"intraansit", "intransit"}:
        return "in_transit"
    return "cleared"


def warehouse_catalog(items):
    """Resolve names by stable location ID across all returned item details."""
    catalog = {}
    for item in items.values():
        for location in item.get("locations", item.get("warehouses", [])) or []:
            identifier = str(location.get("location_id", location.get("warehouse_id", "")))
            name = str(location.get("location_name", location.get("warehouse_name", "")) or "").strip()
            if not identifier:
                raise ZohoError("Books returned a warehouse without a location ID.")
            previous = catalog.get(identifier, "")
            if previous and name and "".join(previous.casefold().split()) != "".join(name.casefold().split()):
                raise ZohoError(f"Books returned conflicting names for warehouse {identifier}. Refresh its details before generating the report.")
            catalog[identifier] = name or previous
    return catalog


def groups_from_warehouse_names(items):
    groups = {"cleared": [], "bonded": [], "in_transit": []}
    for identifier, name in warehouse_catalog(items).items():
        groups[warehouse_group(name)].append(identifier)
    if not any(groups.values()):
        raise ZohoError("Books did not return any named warehouses. Check warehouse access before refreshing.")
    return groups


def stock_sources(items, groups=None):
    if groups is None:
        groups = groups_from_warehouse_names(items)
    assignment = {str(identifier): group for group, identifiers in groups.items() for identifier in identifiers}
    result = {"stock_cy": [], "twc_stock": [], "bonded_stock_file": [], "in_transit": []}
    observed = set()
    for item in items.values():
        if not item.get("sku"):
            continue
        # Non-inventory/service items still belong in the item master, not the stock snapshot.
        if item.get("track_inventory") is False or item.get("product_type") == "service":
            continue
        locations = item.get("locations", item.get("warehouses"))
        if not isinstance(locations, list):
            raise ZohoError("Books did not return location stock. Check warehouse access and the item API response before publishing.")
        quantities = {"cleared": 0., "bonded": 0., "in_transit": 0.}
        ids = set()
        for location in locations:
            identifier = str(location.get("location_id", location.get("warehouse_id", "")))
            if not identifier or identifier in ids:
                raise ZohoError("An item has missing or repeated location IDs.")
            ids.add(identifier)
            observed.add(identifier)
            value = location.get("location_stock_on_hand", location.get("warehouse_stock_on_hand"))
            amount = _number(value, "location stock on hand")
            if identifier not in assignment:
                if amount != 0:
                    raise ZohoError(f"Unmapped stock location {identifier}. Assign it to Cleared, Bonded or In Transit.")
                continue
            quantities[assignment[identifier]] += amount
        total = sum(quantities.values())
        if item.get("stock_on_hand") is not None and abs(total - _number(item["stock_on_hand"], "stock on hand")) > .001:
            raise ZohoError("The item stock total does not reconcile to the mapped locations. Check the stock tracking basis.")
        meta = _metadata(item)
        for key, amount in [("stock_cy", total), ("twc_stock", quantities["cleared"]),
                            ("bonded_stock_file", quantities["bonded"]), ("in_transit", quantities["in_transit"])]:
            result[key].append({**meta, "closing_stock": amount})
    if not set(assignment).issubset(observed):
        raise ZohoError("Some configured location IDs were not returned by Books. Check their IDs before refreshing.")
    return {key: pd.DataFrame(rows) for key, rows in result.items()}


def po_summary_is_relevant(order):
    """Read active POs awaiting goods, including fully billed purchase orders."""
    if str(order.get("status", "")).lower() not in {"approved", "open", "issued", "partially_billed", "partially_received", "billed"}:
        return False
    return not (str(order.get("received_status", "")).lower() in {"received", "fully_received"}
                and _number(order.get("quantity_yet_to_receive", 0), "PO quantity yet to receive") <= 0)


def _bill_line_po(line):
    """The PO a bill line was raised against: its number, "" if none, or None if unknown."""
    details = line.get("purchaseorder_details")
    if isinstance(details, dict) and details.get("purchaseorder_number"):
        return str(details["purchaseorder_number"])
    if "purchaseorder_number" in line:
        return str(line["purchaseorder_number"] or "")
    return "" if "purchaseorder_id" in line and not line["purchaseorder_id"] else None


def open_orders(orders, bills, items, sales=None):
    """Show billed PO quantities and credit only billed units not yet received."""
    billed, batch_bills, batch_sales = {}, {}, {}
    for bill in bills:
        for line in bill.get("line_items", []):
            key = str(line.get("purchaseorder_item_id", ""))
            if key:
                billed[key] = billed.get(key, 0) + _number(line.get("quantity"), "billed PO quantity")
            po_number = _bill_line_po(line)
            if line.get("item_id") and po_number is not None:
                batch_bills.setdefault(str(line["item_id"]), []).append(
                    (str(bill.get("date", ""))[:10], po_number, str(bill.get("bill_number", "")),
                     _number(line.get("quantity"), "bill quantity")))
    if sales is not None:
        for sku, day, quantity in zip(sales.sku, sales.transaction_date, sales.quantity_sold):
            if quantity > 0:
                batch_sales.setdefault(sku, []).append((str(day)[:10], quantity))
    rows, audit, seen_lines = [], [], set()
    for order in orders:
        status = str(order.get("status", "")).lower()
        if status not in {"approved", "open", "issued", "partially_billed", "partially_received", "billed"}:
            continue
        if str(order.get("approval_status", "approved")).lower() not in {"approved", "not_required", ""}:
            continue
        for position, line in enumerate(order.get("line_items", []), 1):
            if not line.get("item_id") or line.get("line_item_category", "line_item") != "line_item":
                continue
            item = items.get(str(line["item_id"]))
            if not item or not item.get("sku"):
                raise ZohoError("An open PO product has no exact item/SKU match. Fix it in Books before refresh.")
            line_id = str(line.get("line_item_id", ""))
            if not line_id:
                raise ZohoError("An open PO line is missing its ID; billed quantities cannot be reconciled.")
            if line_id in seen_lines:
                raise ZohoError("Books returned a repeated PO line ID; outstanding quantities would be counted twice.")
            seen_lines.add(line_id)
            qty = _number(line.get("quantity"), "PO quantity")
            native_billed = line.get("quantity_billed")
            consumed = _number(native_billed, "PO billed quantity") if native_billed is not None else billed.get(line_id, 0)
            cancelled = _number(line.get("quantity_cancelled", 0), "PO cancelled quantity")
            received = _number(line["quantity_received"], "PO received quantity") if line.get("quantity_received") is not None else None
            marked_received = _number(line.get("quantity_marked_as_received", 0), "PO manually received quantity")
            if received is None and consumed > 0:
                raise ZohoError("A billed PO line is missing received quantities; its reorder credit cannot be verified.")
            if min(qty, consumed, cancelled, received or 0, marked_received) < 0:
                raise ZohoError("Books returned a negative PO quantity; review the order before reordering.")
            unbilled = max(qty - cancelled - consumed, 0)
            # Bottles of this batch billed on another PO (or none) since this PO, and since sold,
            # have already arrived: the old PO line was never closed. Only those bottles are dropped.
            po_number, po_date = str(order.get("purchaseorder_number", "")), str(order.get("date", ""))[:10]
            other_bills = [b for b in batch_bills.get(str(line["item_id"]), []) if b[0] >= po_date and b[1] != po_number]
            sold_since = sum(q for day, q in batch_sales.get(item["sku"], []) if day >= po_date)
            arrived = min(unbilled, sum(b[3] for b in other_bills)) if sold_since > 0 else 0
            counted = unbilled - arrived
            unreceived = max(qty - cancelled - received - marked_received, 0) if received is not None else None
            remaining = max(consumed - (received or 0) - marked_received, 0)
            key = sku_to_group_key(item["sku"])
            rows.append({"sku_key": key, "open_po_qty": remaining, "billed_po_qty": consumed, "unbilled_po_qty": counted})
            audit.append({"PO Number": order.get("purchaseorder_number", ""), "PO Date": order.get("date", ""),
                          "Supplier": order.get("vendor_name", ""), "PO Status": status,
                          "PO Approval": order.get("approval_status") or "Approved / not required",
                          "Line #": position, "Brand": item.get("brand", ""),
                          "Description": line.get("description") or line.get("name") or item.get("name", ""),
                          "PO ID": str(order.get("purchaseorder_id", "")),
                          "Line ID": line_id, "Item ID": str(line["item_id"]), "SKU": item["sku"],
                          "Item Name": item.get("name", ""), "Ordered Qty": qty,
                          "Billed Qty": consumed, "Received Qty": received,
                          "Manually Received Qty": marked_received, "Cancelled Qty": cancelled,
                          "Unbilled Qty": unbilled, "Unbilled Qty Counted": counted,
                          "Batch Bills Elsewhere": "; ".join(f"Bill {n or '?'} {d} x{q:g} ({p or 'no PO'})" for d, p, n, q in other_bills),
                          "Batch Sold Since PO": sold_since,
                          "Unreceived Qty": unreceived, "Outstanding Qty": remaining,
                          "Billed Quantity Source": "Books PO quantity_billed" if native_billed is not None else "Bill line links",
                          "Match": "Exact Books item ID",
                          "Notes": f"Billed {consumed:g}; received {(received or 0) + marked_received:g}; for reorder {remaining:g}."
                                   + (f" {arrived:g} of {unbilled:g} unbilled ignored: this batch was billed on "
                                      + ", ".join(sorted({f"{p or 'no PO'} (bill {n or '?'})" for _, p, n, _ in other_bills}))
                                      + f" and {sold_since:g} sold since the PO date." if arrived else "")})
    quantities = ["open_po_qty", "billed_po_qty", "unbilled_po_qty"]
    frame = pd.DataFrame(rows, columns=["sku_key", *quantities])
    return frame.groupby("sku_key", as_index=False)[quantities].sum(), pd.DataFrame(audit)


def fetch_weekly_inputs(client, as_of, today):
    """Reuse a recent complete pull, or read changes and create a complete cache."""
    if as_of > today:
        raise ZohoError("The sales cutoff cannot be in the future.")
    cached = load_weekly_snapshot(client, as_of)
    if cached is not None and client.config.warehouse_mapping_mode == "names" \
            and cached[2].get("warehouse_rules") != WAREHOUSE_RULES:
        cached = _regroup_saved_stock(client, as_of, cached)
    if cached is not None:
        client.progress("Using complete saved Books data; no Zoho requests needed")
        return cached
    if getattr(client, "cache_only", False):
        raise ZohoError("No complete saved Books data exists for this cutoff and warehouse settings. Finish a Zoho pull first.")
    result = _fetch_weekly_inputs(client, as_of, today)
    save_weekly_snapshot(client, as_of, result)
    return result


STOCK_KINDS = {"stock_cy": "stock", "twc_stock": "twc_stock",
               "bonded_stock_file": "bonded_stock_uploaded", "in_transit": "in_transit_qty"}


def _regroup_saved_stock(client, as_of, cached):
    """Re-split saved stock under the current warehouse rules from the same item snapshot."""
    inputs, audit, metadata = cached
    items = load_item_snapshot_at(client, metadata.get("stock_snapshot_at"))
    if items is None:
        return None
    groups = groups_from_warehouse_names(items)
    sources = stock_sources(items, groups)
    inputs = {**inputs, **{key: prepare_source(sources[key], kind) for key, kind in STOCK_KINDS.items()}}
    metadata = {**metadata, "warehouse_groups": groups, "warehouse_rules": WAREHOUSE_RULES}
    save_weekly_snapshot(client, as_of, (inputs, audit, metadata))
    client.progress("Re-grouped saved warehouse stock under the current warehouse rules")
    return inputs, audit, metadata


def _fetch_weekly_inputs(client, as_of, today):
    """Sales use matching YTD cutoffs; location stock and outstanding POs are current."""
    client.config.validate()
    if as_of > today:
        raise ZohoError("The sales cutoff cannot be in the future.")
    previous_end = as_of.replace(year=as_of.year - 1, day=min(as_of.day, 28) if as_of.month == 2 else as_of.day)
    total_steps = 7 if client.config.include_vendor_credits else 6
    def stage(number, label):
        client.stage = f"Step {number}/{total_steps}: {label}"
        client.progress("Starting")

    stage(1, "Current warehouse stock")
    items = load_items(client)
    # Validate the stock snapshot before a potentially long historical backfill.
    groups = groups_from_warehouse_names(items) if client.config.warehouse_mapping_mode == "names" else client.config.locations
    sources = stock_sources(items, groups)
    exclusions = []
    stage(2, f"{as_of.year} sales through {as_of.isoformat()}")
    current = _sales(client, items, date(as_of.year, 1, 1), as_of, exclusions)
    stage(3, f"{as_of.year - 1} sales (YTD through {previous_end.isoformat()}; full year for PO checks)")
    # The full year shares the YTD transaction index; only the YTD part counts as sales.
    previous_year_exclusions = []
    previous_year = _sales(client, items, date(as_of.year - 1, 1, 1), date(as_of.year - 1, 12, 31), previous_year_exclusions)
    previous = previous_year[previous_year.transaction_date <= previous_end.isoformat()]
    exclusions += [row for row in previous_year_exclusions if row["transaction_date"] <= previous_end.isoformat()]
    stage(4, "Current open purchase orders")
    orders = list(client.documents("purchaseorders", "purchaseorder", "purchaseorder_id", params={"filter_by": "Status.All"}))
    orders = [o for o in orders if _document_date(o) <= today]
    # Native PO billed quantities avoid reading older bill history solely for POs.
    # Retain the linked-bill fallback for API responses without these quantities.
    native_po_quantities = all(line.get("quantity_billed") is not None for order in orders
                               for line in order.get("line_items", []) if line.get("item_id"))
    bill_start = (date(as_of.year - 1, 1, 1) if native_po_quantities else
                  min([date(as_of.year - 1, 1, 1)] + [_document_date(o) for o in orders]))
    stage(5, "Purchase bills and PO reconciliation")
    bills = list(client.documents("bills", "bill", "bill_id", bill_start, today))
    purchases = transaction_rows(bills, items, "quantity_purchased", exclusions=exclusions)
    if client.config.include_vendor_credits:
        stage(6, "Vendor credits")
        purchases += transaction_rows(client.documents("vendorcredits", "vendor_credit", "vendor_credit_id", bill_start, today), items, "quantity_purchased", -1, exclusions)
    stage(total_steps, "Preparing the report inputs")
    sources.update(sales_cy=current, sales_py=previous,
                   item_details=pd.DataFrame([_metadata(item) for item in items.values() if item.get("sku")]))
    for label, start, end in [("purchases_cy", date(as_of.year, 1, 1), as_of), ("purchases_py", date(as_of.year - 1, 1, 1), previous_end)]:
        subset = [r for r in purchases if start.isoformat() <= r["transaction_date"] <= end.isoformat()]
        sources[label] = pd.DataFrame(subset, columns=["sku", "item_name", "brand", "quantity_purchased", "amount"])
    kinds = {"sales_cy": "sales", "sales_py": "sales", **STOCK_KINDS, "item_details": "metadata",
             "purchases_cy": "purchases", "purchases_py": "purchases"}
    inputs = {key: prepare_source(frame, kinds[key]) for key, frame in sources.items()}
    inputs["purchase_orders"], audit = open_orders(orders, bills, items, pd.concat([previous_year, current], ignore_index=True))
    metadata = {"source": "Zoho Books", "organization_id": client.config.organization_id,
                "sales_as_of": as_of.isoformat(), "stock_as_of": today.isoformat(), "po_as_of": today.isoformat(),
                "stock_basis": "Books location stock_on_hand", "synced_at": datetime.now(timezone.utc).isoformat(),
                "po_quantity_basis": PO_QUANTITY_BASIS,
                "po_scope": "Active approved POs awaiting receipt, including fully billed POs",
                "warehouse_mapping_mode": client.config.warehouse_mapping_mode, "warehouse_groups": groups,
                "warehouse_rules": WAREHOUSE_RULES,
                "credit_notes_included": client.config.include_credit_notes,
                "sales_receipts_included": client.config.include_sales_receipts,
                "vendor_credits_included": client.config.include_vendor_credits,
                "api_calls": client.calls,
                "api_calls_by_resource": dict(getattr(client, "calls_by_resource", {})),
                "csv_export_reused": dict(getattr(client, "export_reused_by_resource", {})),
                "stock_snapshot_at": getattr(client, "stock_snapshot_at", None),
                "transaction_cache": dict(getattr(client, "sync_stats", {})),
                "revenue_basis": "Books line item_total multiplied by document exchange_rate",
                "unavailable_metrics": ["Historical stock", "Stock Turnover"]}
    if metadata["stock_snapshot_at"]:
        metadata["stock_as_of"] = datetime.fromisoformat(metadata["stock_snapshot_at"]).astimezone(timezone(timedelta(hours=2))).date().isoformat()
    metadata["excluded_non_sku_items"] = (pd.DataFrame(exclusions)
        .groupby(["Item ID", "Item Name", "Source"], as_index=False, dropna=False)
        [["Lines", "Quantity", "Amount (base currency)"]].sum().to_dict("records")) if exclusions else []
    return inputs, audit, metadata


def fetch_annual_sales(client, base_year, today):
    if date(base_year, 12, 31) > today:
        raise ZohoError("An annual benchmark requires a completed calendar year. Use the weekly report for YTD sales.")
    client.stage = "Step 1/3: Item metadata"
    items = load_items(client, include_stock=False)
    results = []
    for step, year in enumerate([base_year, base_year - 1], 2):
        client.stage = f"Step {step}/3: Full-year {year} sales"
        results.append(prepare_source(_sales(client, items, date(year, 1, 1), date(year, 12, 31)), "sales"))
    return tuple(results)


def discover_locations(client):
    locations = warehouse_catalog(load_items(client))
    labels = {"bonded": "Bonded", "in_transit": "In Transit", "cleared": "Cleared / TWC"}
    return pd.DataFrame([{"Location ID": key, "Location Name": name,
                          "Stock Group": labels[warehouse_group(name)] if name else "Missing warehouse name"}
                         for key, name in locations.items()])
