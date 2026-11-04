"""Seed Books line-item caches from fresh Zoho invoice CSV exports, offline."""
import argparse
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path

from zoho_sync_cache import timestamp, write_json


def number(value, field):
    try:
        result = float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        raise ValueError(f"Invoice export has a missing/invalid {field}.") from None
    if not math.isfinite(result):
        raise ValueError(f"Invoice export has a nonfinite {field}.")
    return result


def read_transaction_exports(paths, resource="invoices"):
    import csv
    if resource not in {"invoices", "bills"}:
        raise ValueError("CSV seeding supports invoices or bills.")
    label = "Invoice" if resource == "invoices" else "Bill"
    id_field = "invoice_id" if resource == "invoices" else "bill_id"
    invoices, files, seen_files = {}, [], set()
    required = {label + " ID", label + " Date", label + " Status", "Total", "Exchange Rate",
                "Product ID", "Quantity", "Item Total", "Line Item Category"} | ({"Purchase Order Number"} if resource == "bills" else set())
    for path in map(Path, paths):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen_files:
            continue
        seen_files.add(digest)
        lines = 0
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if not required.issubset(reader.fieldnames or []):
                raise ValueError(f"{path.name} is not a Zoho {label.lower()} export with individual line items.")
            for row in reader:
                lines += 1
                identifier = row[label + " ID"].strip()
                if not identifier.isdigit():
                    raise ValueError(f"{path.name} has a missing or invalid Invoice ID at row {lines + 1}.")
                invoice_date = date.fromisoformat(row[label + " Date"].strip()).isoformat()
                status = row[label + " Status"].strip().casefold().replace(" ", "").replace("_", "")
                header = {id_field: identifier, "date": invoice_date, "export_status": status,
                          "total": number(row["Total"], "invoice total"),
                          "exchange_rate": number(row["Exchange Rate"] or "1", "exchange rate")}
                if header["exchange_rate"] <= 0:
                    raise ValueError("Invoice export has a nonpositive exchange rate.")
                if identifier not in invoices:
                    invoices[identifier] = {**header, "line_items": []}
                elif any(invoices[identifier][key] != value for key, value in header.items()):
                    raise ValueError(f"Conflicting invoice header values in {path.name} at row {lines + 1}.")
                category = row["Line Item Category"].strip().casefold()
                if category not in {"item", "line_item", ""}:
                    raise ValueError(f"Unrecognized line item category in {path.name} at row {lines + 1}.")
                item_id = row["Product ID"].strip()
                if item_id and not item_id.isdigit():
                    raise ValueError(f"Invalid Product ID in {path.name} at row {lines + 1}.")
                # Keep every row, including legitimate repeated product lines
                # and an invoice split at the export's 25,000-row boundary.
                line = {"item_id": item_id, "quantity": number(row["Quantity"], "line quantity"),
                        "item_total": number(row["Item Total"], "line item total"), "line_item_category": "line_item"}
                if resource == "bills":
                    line["purchaseorder_number"] = (row["Purchase Order Number"] or "").strip()
                    invoices[identifier].setdefault("bill_number", (row.get("Bill Number") or "").strip())
                invoices[identifier]["line_items"].append(line)
        files.append({"name": path.name, "sha256": digest, "lines": lines})
    if not invoices:
        raise ValueError("The invoice exports contain no invoice rows.")
    return invoices, files


def read_invoice_exports(paths):
    return read_transaction_exports(paths, "invoices")


def load_export_seed(folder, resource="invoices"):
    try:
        saved = json.loads((folder / f"_export_{resource}.json").read_text(encoding="utf-8"))
        if saved["version"] == 1 and isinstance(saved[resource], dict):
            timestamp(saved["exported_at"])
            return saved
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return {resource: {}}


def exported_detail(saved, summary, id_field="invoice_id"):
    """Trust a seed only after source-version and accounting metadata checks."""
    try:
        seed, detail = saved["export_seed"], saved["detail"]
        modified = timestamp(summary["last_modified_time"])
        # Re-fetch invoices edited near/during the export to cover snapshot races.
        if modified > timestamp(seed["exported_at"]) - timedelta(minutes=5):
            return None
        if str(summary[id_field]) != detail[id_field] or summary["date"][:10] != detail["date"]:
            return None
        if abs(number(summary["total"], "summary total") - detail["total"]) > .005:
            return None
        if summary.get("exchange_rate") not in (None, "") and abs(number(summary["exchange_rate"], "exchange rate") - detail["exchange_rate"]) > .000001:
            return None
        allowed = {"closed": {"paid"}, "paid": {"paid"}, "open": {"sent", "unpaid", "viewed", "open"},
                   "sent": {"sent"}, "overdue": {"overdue"}, "partiallypaid": {"partially_paid"},
                   "approved": {"approved", "sent", "unpaid"}, "void": {"void"}, "draft": {"draft"},
                   "pending": {"draft", "pending_approval", "submitted"}}
        status = str(summary["status"]).casefold()
        if status not in allowed.get(detail["export_status"], set()):
            return None
        return {**detail, "status": status, "last_modified_time": summary["last_modified_time"]}
    except (KeyError, TypeError, ValueError):
        return None


def seed_cache(config, paths, cache_root, exported_at, resource="invoices"):
    from zoho_books import REGIONS
    timestamp(exported_at)
    invoices, sources = read_transaction_exports(paths, resource)
    key = hashlib.sha256((REGIONS[config.region][1] + config.organization_id).encode()).hexdigest()[:16]
    folder = Path(cache_root) / key / resource
    existing = {path.stem for path in folder.glob("*.json") if path.stem.isdigit()}
    preserved = len(set(invoices).intersection(existing))
    created = len(invoices) - preserved
    # One file avoids thousands of small file reads/writes on Windows. Existing
    # API detail files are retained and used if a source version invalidates CSV.
    write_json(folder / f"_export_{resource}.json", {"version": 1, "exported_at": exported_at,
                                                  resource: invoices})
    summary = {resource + "_in_exports": len(invoices), "new_seed_records": created,
               "existing_records_preserved": preserved, "exported_at": exported_at,
               "date_start": min(row["date"] for row in invoices.values()),
               "date_end": max(row["date"] for row in invoices.values()), "sources": sources}
    write_json(folder / "_export_seed.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--exported-at", required=True, help="Actual export timestamp with timezone")
    parser.add_argument("--resource", choices=["invoices", "bills"], default="invoices")
    args = parser.parse_args()
    from zoho_books import load_config
    result = seed_cache(load_config(), args.files, Path(__file__).parent / "data" / "books_cache", args.exported_at, args.resource)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
