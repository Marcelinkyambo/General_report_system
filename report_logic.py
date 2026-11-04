import numpy as np
import pandas as pd

WEEKS_PER_MONTH = 52 / 12


def _normalized_column_name(column):
    return (
        str(column)
        .strip()
        .lower()
        .replace("(", "")
        .replace(")", "")
        .replace("%", "")
        .replace(" ", "_")
    )


def _find_column(columns, must_have=None, must_not_have=None):
    must_have = must_have or []
    must_not_have = must_not_have or []
    for column in columns:
        normalized = _normalized_column_name(column)
        if all(token in normalized for token in must_have) and not any(
            token in normalized for token in must_not_have
        ):
            return column
    return None


def find_closing_stock_quantity_column(columns):
    """Return the stock quantity column from a normalized Zoho stock summary export."""
    return (
        _find_column(columns, ["closing_stock"], ["amount"])
        or _find_column(columns, ["closing_balance"], ["amount"])
        or _find_column(columns, ["closing"], ["amount"])
    )


def _numeric_col(df, column, default=0):
    if column not in df.columns:
        if isinstance(default, pd.Series):
            return pd.to_numeric(default, errors="coerce").fillna(0)
        return pd.Series(default, index=df.index)
    return pd.to_numeric(df[column], errors="coerce").fillna(default)


def _safe_div(numerator, denominator):
    denominator = pd.to_numeric(denominator, errors="coerce")
    numerator = pd.to_numeric(numerator, errors="coerce")
    return np.where((denominator > 0) & np.isfinite(denominator), numerator / denominator, np.nan)


def derive_stock_components(df):
    """Reconcile cleared, bonded, and in-transit stock into separate components."""
    result = df.copy()

    result["total_stock"] = _numeric_col(result, "total_stock").clip(lower=0)
    result["twc_stock_raw"] = _numeric_col(result, "twc_stock").clip(lower=0)
    result["in_transit_qty"] = _numeric_col(result, "in_transit_qty").clip(lower=0)

    if "bonded_stock_uploaded" in result.columns:
        bonded_uploaded = pd.to_numeric(result["bonded_stock_uploaded"], errors="coerce")
        result["bonded_stock"] = (
            bonded_uploaded
            .fillna(0)
            .clip(lower=0)
        )
        has_bonded_component = bonded_uploaded.notna()
    else:
        result["bonded_stock"] = _numeric_col(result, "bonded_stock").clip(lower=0)
        has_bonded_component = result["bonded_stock"] > 0

    has_in_transit_component = result["in_transit_qty"] > 0
    has_component_stock = has_bonded_component | has_in_transit_component
    uploaded_cleared = np.minimum(result["twc_stock_raw"], result["total_stock"])
    calculated_cleared = (
        result["total_stock"] - result["bonded_stock"] - result["in_transit_qty"]
    ).clip(lower=0)
    result["twc_stock"] = np.where(has_component_stock, calculated_cleared, uploaded_cleared)

    return result


def apply_stock_coverage(df):
    """Calculate stock coverage from stock components and current sales rates."""
    result = df.copy()

    twc_stock = _numeric_col(result, "twc_stock").clip(lower=0)
    bonded_stock = _numeric_col(result, "bonded_stock").clip(lower=0)
    in_transit = _numeric_col(result, "in_transit_qty").clip(lower=0)
    monthly_run_rate = _numeric_col(result, "monthly_run_rate").clip(lower=0)
    weekly_run_rate = _numeric_col(result, "weekly_run_rate").clip(lower=0)

    result["total_stock_for_coverage"] = twc_stock + bonded_stock + in_transit
    result["cleared_stock_for_coverage"] = twc_stock
    result["total_stock_coverage_months"] = _safe_div(result["total_stock_for_coverage"], monthly_run_rate)
    result["total_stock_coverage_weeks"] = _safe_div(result["total_stock_for_coverage"], weekly_run_rate)
    result["twc_coverage_months"] = _safe_div(result["cleared_stock_for_coverage"], monthly_run_rate)
    result["twc_coverage_weeks"] = _safe_div(result["cleared_stock_for_coverage"], weekly_run_rate)

    return result


def apply_reorder_flags(df, remaining_weeks):
    """Apply bonded and TWC reorder flags to a report dataframe."""
    result = df.copy()
    remaining_weeks_safe = max(float(remaining_weeks), 0)

    bonded_reorder = (
        result["total_stock_coverage_months"].notna()
        & (result["total_stock_coverage_months"] <= 5)
        & (_numeric_col(result, "in_transit_qty").clip(lower=0) <= 0)
    )
    bonded_stock = _numeric_col(result, "bonded_stock").clip(lower=0)
    twc_reorder = (
        result["twc_coverage_months"].notna()
        & (result["twc_coverage_months"] <= 1)
        & (bonded_stock > 0)
    )

    result["bonded_reorder"] = np.where(bonded_reorder, "REORDER", "")
    result["twc_reorder"] = np.where(twc_reorder, "CLEAR", "")

    weekly_run_rate = _numeric_col(result, "weekly_run_rate").clip(lower=0)
    annual_target = _numeric_col(result, "annual_target").clip(lower=0)
    quantity_sold = _numeric_col(result, "quantity_sold").clip(lower=0)
    total_stock = _numeric_col(result, "total_stock_for_coverage", _numeric_col(result, "total_stock")).clip(lower=0)
    twc_stock = _numeric_col(result, "cleared_stock_for_coverage", _numeric_col(result, "twc_stock")).clip(lower=0)

    actual_remaining_demand = weekly_run_rate * remaining_weeks_safe
    target_remaining_demand = np.maximum(annual_target - quantity_sold, 0)
    smart_remaining_demand = np.maximum(actual_remaining_demand, target_remaining_demand)
    smart_weekly_demand = np.where(
        remaining_weeks_safe > 0,
        smart_remaining_demand / remaining_weeks_safe,
        0,
    )

    bonded_target_stock = np.minimum(smart_remaining_demand, smart_weekly_demand * 5 * WEEKS_PER_MONTH)
    bonded_gap = np.maximum(bonded_target_stock - total_stock, 0)
    result["bonded_est_reorder_qty"] = np.where(result["bonded_reorder"] == "REORDER", np.ceil(bonded_gap), 0)

    twc_target_stock = np.minimum(smart_remaining_demand, smart_weekly_demand * WEEKS_PER_MONTH)
    twc_gap = np.maximum(twc_target_stock - twc_stock, 0)
    twc_qty = np.minimum(np.ceil(twc_gap), bonded_stock)
    result["twc_est_reorder_qty"] = np.where(result["twc_reorder"] == "CLEAR", twc_qty, 0)

    return result
