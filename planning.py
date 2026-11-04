"""Brand growth controls and annual sales targets. Rates are stored as decimals."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from report_inputs import normalize_columns, sku_to_group_key, normalize_sku, first_non_empty, find_col


def brand_key(value):
    return " ".join(str(value).split()).casefold() if pd.notna(value) else "unknown"


def validate_rate(value):
    rate = float(value)
    if not math.isfinite(rate) or rate < -1 or rate > 10:
        raise ValueError("Growth must be between -100% and 1,000%.")
    return rate


def load_growth(path, year):
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {brand_key(k): validate_rate(v) for k, v in raw.get(str(year), {}).items()}


def save_growth(path, year, rates):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[str(year)] = {brand_key(k): validate_rate(v) for k, v in rates.items() if pd.notna(v)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def prepare_planning(raw):
    raw = normalize_columns(raw)
    sku = find_col(raw, ["sku"])
    monthly = find_col(raw, ["monthly", "target"])
    annual = find_col(raw, ["next", "year", "budget"]) or find_col(raw, ["annual", "target"])
    current = find_col(raw, ["current", "year", "sales"]) or find_col(raw, ["base", "sales"])
    growth = find_col(raw, ["growth", "target"])
    if not sku or not (monthly or annual or current):
        raise ValueError("Planning needs SKU and a sales baseline, monthly target or annual target.")
    plan = pd.DataFrame({"sku": raw[sku].map(normalize_sku)})
    plan["sku_key"] = plan.sku.map(sku_to_group_key)
    if plan.sku_key.eq("").any():
        raise ValueError("Planning contains a blank SKU.")
    # The benchmark already combines vintages. Reject repeats instead of multiplying report rows.
    if plan.sku_key.duplicated().any():
        raise ValueError("Planning contains duplicate product SKUs after combining vintages.")
    for col, source in [("monthly_target_plan", monthly), ("annual_budget_plan", annual),
                        ("baseline_sales_plan", current), ("growth_target_plan", growth)]:
        if source:
            text = raw[source]
            if col == "growth_target_plan":
                plan[col] = text.map(lambda v: float(v.strip().rstrip("%")) / 100 if isinstance(v, str) and "%" in v else v)
            else:
                plan[col] = text
            plan[col] = pd.to_numeric(plan[col], errors="raise")
    if "annual_budget_plan" not in plan and monthly:
        plan["annual_budget_plan"] = plan.monthly_target_plan * 12
    if "monthly_target_plan" not in plan and "annual_budget_plan" in plan:
        plan["monthly_target_plan"] = plan.annual_budget_plan / 12
    if "baseline_sales_plan" not in plan and "growth_target_plan" in plan:
        denominator = 1 + plan.growth_target_plan
        plan["baseline_sales_plan"] = plan.annual_budget_plan / denominator.where(denominator.ne(0))
    return plan


def apply_brand_targets(df, rates, default_rate, weeks_elapsed):
    result = df.copy()
    rates = {brand_key(k): validate_rate(v) for k, v in rates.items()}
    default_rate = validate_rate(default_rate)
    override = result.brand.map(brand_key).map(rates)
    stored = result.get("growth_target_plan", pd.Series(np.nan, index=result.index))
    result["growth_target"] = override.fillna(stored).fillna(default_rate)
    baseline = pd.to_numeric(result.get("baseline_sales_plan", pd.Series(np.nan, index=result.index)), errors="coerce")
    fixed = pd.to_numeric(result.get("annual_budget_plan", pd.Series(np.nan, index=result.index)), errors="coerce")
    # A fixed benchmark remains fixed until a brand override is explicitly saved.
    has_fixed = fixed.notna()
    cannot_rebase = override.notna() & has_fixed & baseline.isna()
    if cannot_rebase.any():
        raise ValueError("A brand override needs base-year sales or the benchmark's original growth rate.")
    # Products outside the benchmark get no target: one derived from their own
    # sales would always show the same pace and say nothing about performance.
    result["target_base_sales"] = baseline
    result["annual_target"] = result.target_base_sales * (1 + result.growth_target)
    keep_fixed = has_fixed & override.isna()
    result.loc[keep_fixed, "annual_target"] = fixed[keep_fixed]
    result["target_source"] = np.where(baseline.notna() | keep_fixed, "Annual benchmark", "No planning target")
    result["monthly_sales_target"] = result.annual_target / 12
    result["weekly_sales_target"] = result.annual_target / 52
    return result


def brand_history(current, previous, metadata=None):
    """Aggregate units before dividing; include products sold only in the prior year."""
    frames = []
    availability = {}
    for source, column in [(current, "CY Sales"), (previous, "PY Sales")]:
        availability[column] = source is not None and "quantity_sold" in source
        if source is not None and not source.empty:
            part = source[["sku_key", "quantity_sold"]].rename(columns={"quantity_sold": column})
            frames.append(part.set_index("sku_key"))
    if not frames:
        return pd.DataFrame(columns=["Brand", "CY Sales", "PY Sales", "Historical Growth %"])
    combined = pd.concat(frames, axis=1).fillna(0)
    for col in ["CY Sales", "PY Sales"]:
        if col not in combined:
            combined[col] = 0
        if not availability[col]:
            combined[col] = np.nan
    lookups = [s[["sku_key", "brand"]] for s in [metadata, current, previous]
               if s is not None and not s.empty and "brand" in s]
    if lookups:
        lookup = pd.concat(lookups).groupby("sku_key").brand.agg(first_non_empty)
        combined["Brand"] = combined.index.map(lookup).fillna("Unknown")
    else:
        combined["Brand"] = "Unknown"
    combined["brand_key"] = combined.Brand.map(brand_key)
    summary = combined.groupby("brand_key").agg({"Brand": "first", "CY Sales": lambda s: s.sum(min_count=1), "PY Sales": lambda s: s.sum(min_count=1)}).reset_index(drop=True)
    summary["Historical Growth %"] = ((summary["CY Sales"] - summary["PY Sales"]) / summary["PY Sales"].where(summary["PY Sales"].gt(0))) * 100
    return summary.sort_values("Brand").reset_index(drop=True)


def build_annual_plan(current, previous, rates, default_rate=.2):
    """Call with two complete calendar years of actuals, never with partial YTD."""
    metadata = current.copy()
    prior = previous[["sku_key", "quantity_sold"]].rename(columns={"quantity_sold": "previous_year_sales"})
    plan = metadata.merge(prior, on="sku_key", how="outer", validate="one_to_one")
    for col in ["sku", "item_name", "brand"]:
        if col not in plan:
            plan[col] = np.nan
        if col in previous:
            lookup = previous.set_index("sku_key")[col]
            plan[col] = plan[col].fillna(plan.sku_key.map(lookup))
    plan["brand"] = plan.brand.fillna("Unknown")
    plan["quantity_sold"] = plan.quantity_sold.fillna(0)
    plan["previous_year_sales"] = plan.previous_year_sales.fillna(0)
    rate_map = {brand_key(k): validate_rate(v) for k, v in rates.items()}
    growth = plan.brand.map(brand_key).map(rate_map).fillna(validate_rate(default_rate))
    budget = plan.quantity_sold.clip(lower=0) * (1 + growth)
    return pd.DataFrame({"SKU": plan.sku, "Item Name": plan.item_name, "Brand": plan.brand,
                         "Previous Year Sales": plan.previous_year_sales, "Current Year Sales": plan.quantity_sold,
                         "% Sales Difference": (plan.quantity_sold - plan.previous_year_sales) / plan.previous_year_sales.where(plan.previous_year_sales.gt(0)),
                         "Growth Target %": growth, "Next Year Budget": budget, "Monthly Sales Target": budget / 12})
