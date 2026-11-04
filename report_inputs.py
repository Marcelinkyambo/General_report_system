"""Shared file/API normalization for the weekly reporting pipeline."""
import re

import numpy as np
import pandas as pd

from report_logic import find_closing_stock_quantity_column


def normalize_columns(df):
    result = df.copy()
    result.columns = [re.sub(r"\s+", "_", str(c).strip().lower().translate(str.maketrans("", "", "()%"))) for c in result.columns]
    return result


def normalize_sku(value):
    if pd.isna(value):
        return ""
    parts = str(value).strip().removesuffix(".0").split("-")
    return "-".join(p.strip().zfill(3) if p.strip().isdigit() else p.strip() for p in parts[:2])


def sku_to_group_key(value):
    sku = normalize_sku(value)
    parts = sku.split("-")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return "".join(parts)
    # Keep non-numeric SKUs distinct; never merge them into an all-zero key.
    return sku


def first_non_empty(values):
    for value in values:
        if pd.notna(value) and str(value).strip().lower() not in {"", "nan", "none", "null", "--"}:
            return str(value).strip()
    return np.nan


def item_name_key(value):
    if pd.isna(value):
        return ""
    text = re.sub(r"\b(19|20)\d{2}\b", " ", str(value).lower().replace("#vintage#", " "))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def safe_div(a, b, fill=np.nan):
    a, b = np.broadcast_arrays(np.asarray(a, dtype=float), np.asarray(b, dtype=float))
    return np.divide(a, b, out=np.full(a.shape, fill, dtype=float), where=(b > 0) & np.isfinite(b))


def health_label(pace):
    if pd.isna(pace):
        return ""
    if pace >= .10:
        return "🟢 Ahead"
    if pace >= -.10:
        return "🟡 On Track"
    if pace >= -.30:
        return "🟠 Behind"
    return "🔴 Critical"


def find_col(df, must_have, must_not_have=()):
    return next((c for c in df if all(k in c for k in must_have) and not any(k in c for k in must_not_have)), None)


def _base(raw):
    raw = normalize_columns(raw)
    sku = find_col(raw, ["sku"])
    if not sku:
        raise ValueError("The source is missing an SKU column.")
    work = pd.DataFrame(index=raw.index)
    work["sku"] = raw[sku].map(normalize_sku)
    work["sku_key"] = work["sku"].map(sku_to_group_key)
    hints = {"item_name": (["item", "name"], ["name"]), "category": (["category"],),
             "brand": (["brand"],), "country_of_origin": (["country"], ["origin"])}
    for dest, alternatives in hints.items():
        source = next((find_col(raw, h) for h in alternatives if find_col(raw, h)), None)
        if source:
            work[dest] = raw[source]
    return raw, work


def prepare_source(raw, kind):
    """Return one row per product (the first two SKU segments combine vintages)."""
    raw, work = _base(raw)
    if kind == "sales":
        fields = {"quantity_sold": find_col(raw, ["quantity_sold"]) or find_col(raw, ["quantity"]),
                  "revenue": find_col(raw, ["amount"], ["stock", "opening", "closing"]) or find_col(raw, ["revenue"])}
        required = ["quantity_sold"]
    elif kind == "purchases":
        fields = {"quantity_purchased": find_col(raw, ["quantity_purchased"]) or find_col(raw, ["quantity"]),
                  "purchase_amount": find_col(raw, ["amount"])}
        required = ["quantity_purchased"]
    elif kind == "stock":
        fields = {"closing_stock": find_closing_stock_quantity_column(raw.columns),
                  "opening_stock": find_col(raw, ["opening"], ["amount"]),
                  "qty_in": find_col(raw, ["quantity_in"], ["amount"]),
                  "qty_out": find_col(raw, ["quantity_out"], ["amount"])}
        required = ["closing_stock"]
    elif kind in {"twc_stock", "bonded_stock_uploaded", "in_transit_qty"}:
        fields = {kind: find_closing_stock_quantity_column(raw.columns)}
        required = [kind]
    elif kind == "metadata":
        fields, required = {}, []
    else:
        raise ValueError(f"Unknown source kind: {kind}")
    for dest, source in fields.items():
        if source is None and dest in required:
            raise ValueError(f"The {kind} source is missing {dest}.")
        work[dest] = pd.to_numeric(raw[source].replace("--", 0), errors="raise") if source else np.nan
    work = work[work.sku_key.ne("") & ~work.sku_key.str.fullmatch("0+")]
    agg = {c: (lambda s: s.sum(min_count=1)) if c in fields else first_non_empty for c in work if c != "sku_key"}
    return work.groupby("sku_key", as_index=False).agg(agg)
