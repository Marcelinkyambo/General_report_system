"""Short-lived stock reads and durable, complete weekly inputs. No credentials."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import hashlib
import json

import pandas as pd

from zoho_sync_cache import timestamp, write_json


def account_folder(client):
    root = getattr(client, "cache_dir", None)
    if root is None:
        return None
    key = hashlib.sha256((client.api + client.config.organization_id).encode()).hexdigest()[:16]
    return root / key


def _read(path, minutes=None):
    if path is None:
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved["version"] != 1:
            return None
        age = datetime.now(timezone.utc) - timestamp(saved["saved_at"])
        if age < timedelta(0) or (minutes is not None and age > timedelta(minutes=minutes)):
            return None
        return saved
    except (OSError, KeyError, TypeError, ValueError):
        return None


# Bump when PO quantities change meaning, so saved pulls without the new fields are re-read.
PO_QUANTITY_BASIS = "billed_with_unreceived_credit_v3_batch_bills_capped"


def load_item_snapshot(client):
    folder = account_folder(client)
    if folder is None or getattr(client, "force_refresh", False):
        return None
    saved = _read(folder / "item_stock.json", client.config.stock_cache_minutes)
    if saved and isinstance(saved.get("items"), dict) and saved["items"]:
        if all(isinstance(row, dict) and str(row.get("item_id", "")) == key for key, row in saved["items"].items()):
            return saved
    return None


def load_item_snapshot_at(client, saved_at):
    """Return the saved items only if they are the exact stock read taken at saved_at."""
    folder = account_folder(client)
    if folder is None or not saved_at:
        return None
    saved = _read(folder / "item_stock.json")
    if saved and saved.get("saved_at") == saved_at and isinstance(saved.get("items"), dict) and saved["items"]:
        return saved["items"]
    return None


def save_item_snapshot(client, items):
    saved_at = datetime.now(timezone.utc).isoformat()
    client.stock_snapshot_at = saved_at
    folder = account_folder(client)
    if folder is not None:
        write_json(folder / "item_stock.json", {"version": 1, "saved_at": saved_at, "items": items})


def _weekly_path(client, cutoff):
    folder = account_folder(client)
    if folder is None:
        return None
    fields = ["warehouse_mapping_mode", "locations", "include_credit_notes", "include_vendor_credits", "include_sales_receipts"]
    options = {key: getattr(client.config, key) for key in fields}
    signature = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:16]
    return folder / "weekly_inputs" / f"{cutoff.isoformat()}_{signature}.json"


def _decode_frame(value):
    return pd.read_json(StringIO(value), orient="table")


def load_weekly_snapshot(client, cutoff):
    if getattr(client, "force_refresh", False) or getattr(client, "recheck_history", False):
        return None
    folder = account_folder(client)
    if folder is None:
        return None
    minutes = None if getattr(client, "cache_only", False) else client.config.pull_cache_minutes
    saved = _read(_weekly_path(client, cutoff), minutes)
    if not saved:
        return None
    try:
        if saved["metadata"]["sales_as_of"] != cutoff.isoformat():
            return None
        # A long first import must not reset the age of its earlier stock read.
        stock_at = saved["metadata"].get("stock_snapshot_at")
        if stock_at and not getattr(client, "cache_only", False):
            stock_age = datetime.now(timezone.utc) - timestamp(stock_at)
            if stock_age < timedelta(0) or stock_age > timedelta(minutes=client.config.stock_cache_minutes):
                return None
        inputs = {key: _decode_frame(value) for key, value in saved["inputs"].items()}
        audit = _decode_frame(saved["audit"])
        if "sales_cy" not in inputs or "stock_cy" not in inputs:
            return None
        metadata = dict(saved["metadata"])
        if not audit.empty and metadata.get("po_quantity_basis") != PO_QUANTITY_BASIS:
            # Earlier pulls excluded fully billed POs. Their scope is incomplete
            # for billed quantities awaiting receipt, so never relabel that data.
            return None
        metadata.update(served_from_cache=True, api_calls=0, api_calls_by_resource={})
        return inputs, audit, metadata
    except (KeyError, TypeError, ValueError):
        return None


def save_weekly_snapshot(client, cutoff, result):
    path = _weekly_path(client, cutoff)
    if path is None:
        return
    inputs, audit, metadata = result
    encode = lambda frame: frame.to_json(orient="table", index=False, date_format="iso", double_precision=15)
    write_json(path, {"version": 1, "saved_at": datetime.now(timezone.utc).isoformat(),
                      "inputs": {key: encode(frame) for key, frame in inputs.items()},
                      "audit": encode(audit), "metadata": metadata})
