"""The same weekly report builder for Streamlit and scheduled Zoho runs."""
import numpy as np
import pandas as pd

from report_logic import derive_stock_components, apply_stock_coverage, apply_reorder_flags
from report_inputs import first_non_empty, item_name_key, sku_to_group_key, safe_div, health_label
from planning import apply_brand_targets

WEEKS_IN_YEAR = 52


def build_weekly_report(inputs, weeks_elapsed, growth_pct=.2, brand_rates=None):
    if not 0 < weeks_elapsed <= 52:
        raise ValueError("Weeks elapsed must be greater than zero and no more than 52.")
    keys = ["sales_cy", "sales_py", "stock_cy", "stock_py", "purchases_cy", "purchases_py",
            "twc_stock", "bonded_stock_file", "in_transit", "item_details", "planning"]
    inputs = {**{key: pd.DataFrame() for key in keys}, **inputs}
    for key in keys:
        frame = inputs[key]
        if "sku_key" in frame and frame.sku_key.duplicated().any():
            raise ValueError(f"{key} contains duplicate product SKUs.")
    if not {"sku_key", "quantity_sold", "revenue"}.issubset(inputs["sales_cy"].columns) or inputs["stock_cy"].empty:
        raise ValueError("Current-year sales and stock are required.")
    months_elapsed = weeks_elapsed * 12 / 52
    remaining_weeks = 52 - weeks_elapsed
    # ── Start from current-year sales ──
    df = inputs["sales_cy"].copy()
    if not inputs["sales_py"].empty:
        missing = inputs["sales_py"].loc[~inputs["sales_py"].sku_key.isin(df.sku_key), ["sku_key"]]
        df = pd.concat([df, missing], ignore_index=True)

    # ── Merge previous-year sales ──
    if not inputs["sales_py"].empty:
        py = inputs["sales_py"][["sku_key", "quantity_sold"]].rename(
            columns={"quantity_sold": "py_sales"}
        )
        df = df.merge(py, on="sku_key", how="left")
    else:
        df["py_sales"] = 0
    df["py_sales"] = df["py_sales"].fillna(0)

    # ── Merge current-year stock (outer join to include items with stock but no sales) ──
    stk = inputs["stock_cy"].copy()
    stk.rename(columns={
        "opening_stock": "opening_stock_cy",
        "closing_stock": "total_stock",
        "qty_in": "qty_in_cy",
        "qty_out": "qty_out_cy",
    }, inplace=True)
    stk_merge_cols = ["sku_key", "opening_stock_cy", "total_stock", "qty_in_cy", "qty_out_cy"]
    for extra in ["sku", "item_name", "category"]:
        if extra in stk.columns:
            stk_merge_cols.append(extra)
    stk = stk[stk_merge_cols].copy()
    df = df.merge(stk, on="sku_key", how="outer", suffixes=("", "_stk"))

    # Fill metadata from stock for stock-only items (no sales)
    for col in ["sku", "item_name", "category"]:
        stk_col = f"{col}_stk"
        if stk_col in df.columns:
            if col in df.columns:
                df[col] = df[col].fillna(df[stk_col])
            else:
                df[col] = df[stk_col]
            df.drop(columns=[stk_col], inplace=True)
    df["quantity_sold"] = df["quantity_sold"].fillna(0)
    df["revenue"] = df["revenue"].fillna(0)

    # ── Merge previous-year stock ──
    if not inputs["stock_py"].empty:
        stk_py = inputs["stock_py"][["sku_key", "closing_stock"]].rename(
            columns={"closing_stock": "total_stock_py"}
        )
        df = df.merge(stk_py, on="sku_key", how="left")
    else:
        df["total_stock_py"] = np.nan

    # ── Merge purchases ──
    if not inputs["purchases_cy"].empty:
        pur_cy = inputs["purchases_cy"][["sku_key", "quantity_purchased"]].rename(
            columns={"quantity_purchased": "purchases_cy"}
        )
        df = df.merge(pur_cy, on="sku_key", how="left")
    else:
        df["purchases_cy"] = 0

    if not inputs["purchases_py"].empty:
        pur_py = inputs["purchases_py"][["sku_key", "quantity_purchased"]].rename(
            columns={"quantity_purchased": "purchases_py"}
        )
        df = df.merge(pur_py, on="sku_key", how="left")
    else:
        df["purchases_py"] = 0

    # ── Merge TWC stock ──
    if not inputs["twc_stock"].empty:
        twc = inputs["twc_stock"][["sku_key", "twc_stock"]].copy()
        df = df.merge(twc, on="sku_key", how="left")
    else:
        df["twc_stock"] = np.nan

    # ── Merge Bonded Stock (uploaded file takes priority over calculated) ──
    if not inputs["bonded_stock_file"].empty:
        bnd = inputs["bonded_stock_file"][["sku_key", "bonded_stock_uploaded"]].copy()
        df = df.merge(bnd, on="sku_key", how="left")
    else:
        df["bonded_stock_uploaded"] = np.nan

    # ── Merge In-Transit quantities ──
    if not inputs["in_transit"].empty:
        it = inputs["in_transit"][["sku_key", "in_transit_qty"]].copy()
        df = df.merge(it, on="sku_key", how="left")
    else:
        df["in_transit_qty"] = np.nan

    # ── Merge planning targets ──
    has_planning = not inputs["planning"].empty
    if has_planning:
        plan = inputs["planning"].copy()
        plan["sku_key"] = plan["sku"].apply(sku_to_group_key)
        plan_cols = [c for c in plan.columns if c != "sku"]
        df = df.merge(plan[plan_cols], on="sku_key", how="left")

    # ── Metadata backfill from all uploaded sources ──
    metadata_cols = ["sku", "item_name", "category", "country_of_origin", "brand"]
    metadata_sources = []
    for state_key in ["sales_cy", "sales_py", "stock_cy", "stock_py", "purchases_cy", "purchases_py", "twc_stock", "item_details"]:
        src = inputs.get(state_key, pd.DataFrame())
        if not isinstance(src, pd.DataFrame) or src.empty or "sku_key" not in src.columns:
            continue
        cols = ["sku_key"] + [c for c in metadata_cols if c in src.columns]
        if len(cols) > 1:
            metadata_sources.append(src[cols].copy())

    if metadata_sources:
        meta_all = pd.concat(metadata_sources, ignore_index=True, sort=False)
        meta_agg_spec = {}
        for col in metadata_cols:
            if col in meta_all.columns:
                meta_agg_spec[col] = first_non_empty
        meta = meta_all.groupby("sku_key", as_index=False).agg(meta_agg_spec)
        df = df.merge(meta, on="sku_key", how="left", suffixes=("", "_meta"))

        for col in metadata_cols:
            meta_col = f"{col}_meta"
            if meta_col not in df.columns:
                continue
            if col in df.columns:
                current = df[col]
                current_text = current.astype(str).str.strip().str.lower()
                missing_mask = current.isna() | current_text.isin(["", "nan", "none", "null", "--"])
                df[col] = np.where(missing_mask, df[meta_col], current)
            else:
                df[col] = df[meta_col]
            df.drop(columns=[meta_col], inplace=True)

    # ── Metadata enrichment for non-sales items ──
    for col in ["brand", "country_of_origin"]:
        if col not in df.columns:
            df[col] = np.nan

    if "item_name" in df.columns:
        df["_item_name_key"] = df["item_name"].apply(item_name_key)
        has_key = df["_item_name_key"].astype(str).str.len() > 0
        has_brand = df["brand"].notna() & ~df["brand"].astype(str).str.strip().str.lower().isin(["", "nan", "none", "null", "--"])
        has_country = df["country_of_origin"].notna() & ~df["country_of_origin"].astype(str).str.strip().str.lower().isin(["", "nan", "none", "null", "--"])

        by_name_sources = []
        base_name_cols = ["_item_name_key"]
        if has_brand.any():
            base_name_cols.append("brand")
        if has_country.any():
            base_name_cols.append("country_of_origin")
        if len(base_name_cols) > 1:
            by_name_sources.append(df.loc[has_key, base_name_cols].copy())

        details = inputs.get("item_details", pd.DataFrame())
        if isinstance(details, pd.DataFrame) and not details.empty:
            details_name = details.copy()
            if "item_name_key" not in details_name.columns and "item_name" in details_name.columns:
                details_name["item_name_key"] = details_name["item_name"].apply(item_name_key)
            if "item_name_key" in details_name.columns:
                dcols = ["item_name_key"]
                if "brand" in details_name.columns:
                    dcols.append("brand")
                if "country_of_origin" in details_name.columns:
                    dcols.append("country_of_origin")
                if len(dcols) > 1:
                    dn = details_name[dcols].copy().rename(columns={"item_name_key": "_item_name_key"})
                    dn = dn[dn["_item_name_key"].astype(str).str.len() > 0]
                    by_name_sources.append(dn)

        if by_name_sources:
            by_name_all = pd.concat(by_name_sources, ignore_index=True, sort=False)
            by_name_spec = {}
            if "brand" in by_name_all.columns:
                by_name_spec["brand"] = first_non_empty
            if "country_of_origin" in by_name_all.columns:
                by_name_spec["country_of_origin"] = first_non_empty
            by_name = (
                by_name_all
                .groupby("_item_name_key", as_index=False)
                .agg(by_name_spec)
                .rename(columns={"brand": "_brand_by_name", "country_of_origin": "_country_by_name"})
            )
            df = df.merge(by_name, on="_item_name_key", how="left")
            if "_brand_by_name" in df.columns:
                brand_missing = ~has_brand
                df["brand"] = np.where(brand_missing, df["_brand_by_name"], df["brand"])
                df.drop(columns=["_brand_by_name"], inplace=True)
            if "_country_by_name" in df.columns:
                country_missing = ~has_country
                df["country_of_origin"] = np.where(country_missing, df["_country_by_name"], df["country_of_origin"])
                df.drop(columns=["_country_by_name"], inplace=True)

        df.drop(columns=["_item_name_key"], inplace=True)

    # Fill country from known brand-country pairs where possible
    brand_vals = df["brand"].astype(str).str.strip()
    country_vals = df["country_of_origin"].astype(str).str.strip()
    valid_brand = df["brand"].notna() & ~brand_vals.str.lower().isin(["", "nan", "none", "null", "--"])
    valid_country = df["country_of_origin"].notna() & ~country_vals.str.lower().isin(["", "nan", "none", "null", "--"])
    brand_country = (
        df[valid_brand & valid_country]
        .groupby("brand", as_index=False)
        .agg({"country_of_origin": first_non_empty})
        .rename(columns={"country_of_origin": "_country_by_brand"})
    )
    if not brand_country.empty:
        df = df.merge(brand_country, on="brand", how="left")
        country_missing = ~valid_country
        df["country_of_origin"] = np.where(country_missing, df["_country_by_brand"], df["country_of_origin"])
        df.drop(columns=["_country_by_brand"], inplace=True)

    # Ensure report columns always exist (explicit fallback if unknown)
    for col in ["brand", "country_of_origin"]:
        vals = df[col].astype(str).str.strip()
        missing = df[col].isna() | vals.str.lower().isin(["", "nan", "none", "null", "--"])
        df[col] = np.where(missing, "Unknown", df[col])
    # The supplied report uses Others for products without a source category.
    if "category" not in df:
        df["category"] = "Others"
    else:
        df["category"] = df.category.astype(object)
        category = df.category.astype(str).str.strip().str.lower()
        df.loc[df.category.isna() | category.isin(["", "nan", "none", "null", "--"]), "category"] = "Others"

    # ── Fill NaN numerics ──
    num_fills = {
        "total_stock": 0, "total_stock_py": 0, "twc_stock": 0,
        "opening_stock_cy": 0, "qty_in_cy": 0, "qty_out_cy": 0,
        "purchases_cy": 0, "purchases_py": 0, "revenue": 0,
        "py_sales": 0, "quantity_sold": 0, "in_transit_qty": 0,
    }
    for col, fill in num_fills.items():
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(fill)

    # ── Derived: separate cleared, bonded, and in-transit stock components ──
    df = derive_stock_components(df)
    _twc_over_total = int((df["twc_stock_raw"] > df["total_stock"]).sum())

    # ── YoY Sales Comparison ──
    df["yoy_sales_change"] = safe_div(
        df["quantity_sold"] - df["py_sales"], df["py_sales"]
    )

    # ── Weekly run rate ──
    df["weekly_run_rate"] = safe_div(df["quantity_sold"], weeks_elapsed)
    df["py_weekly_run_rate"] = safe_div(df["py_sales"], weeks_elapsed)
    remaining_weeks_safe = max(remaining_weeks, 0)
    df["remaining_year_demand"] = df["weekly_run_rate"] * remaining_weeks_safe

    df = apply_brand_targets(df, brand_rates or {}, growth_pct, weeks_elapsed)

    # ── Sales Health vs Target ──
    df["expected_ytd_sales"] = df["weekly_sales_target"] * weeks_elapsed
    df["ytd_variance"] = np.where(
        df["expected_ytd_sales"] > 0,
        df["quantity_sold"] - df["expected_ytd_sales"],
        np.nan,
    )
    df["sales_pace_pct"] = safe_div(
        df["quantity_sold"] - df["expected_ytd_sales"],
        df["expected_ytd_sales"],
    )
    df["sales_health"] = df["sales_pace_pct"].apply(health_label)

    # ── What's needed to hit the annual target ──
    df["remaining_target"] = np.where(
        df["annual_target"] > 0,
        np.maximum(df["annual_target"] - df["quantity_sold"], 0),
        np.nan,
    )
    if remaining_weeks > 0:
        df["required_weekly_sales"] = safe_div(df["remaining_target"], remaining_weeks)
        df["required_monthly_sales"] = df["required_weekly_sales"] * 52 / 12
    else:
        df["required_weekly_sales"] = np.nan
        df["required_monthly_sales"] = np.nan

    df["effort_multiplier"] = safe_div(df["required_weekly_sales"], df["weekly_sales_target"])

    # ── Stock Coverage (based on current sales run rate) ──
    df["monthly_run_rate"] = safe_div(df["quantity_sold"], months_elapsed)
    df = apply_stock_coverage(df)

    # ── Stock Movement (current year) ──
    df["stock_turnover"] = safe_div(df["qty_out_cy"], df["opening_stock_cy"])

    # ── Projected stock-out week ──
    df["weeks_until_stockout"] = np.where(
        df["weekly_run_rate"] > 0,
        df["total_stock_for_coverage"] / df["weekly_run_rate"],
        np.nan,
    )
    df["stockout_risk"] = np.where(
        df["weeks_until_stockout"] < 4, "⚠️ HIGH",
        np.where(df["weeks_until_stockout"] < 8, "⚡ MEDIUM", ""),
    )

    # ── Reorder flags (based on current-sales coverage) ──
    # Bonded stock: reorder when total stock coverage is 5 months or less and nothing is in transit.
    # TWC stock: reorder when TWC coverage is 1 month or less and bonded stock exists.
    df = apply_reorder_flags(df, remaining_weeks=remaining_weeks)

    orders = inputs.get("purchase_orders", pd.DataFrame())
    if not orders.empty:
        orders = orders.copy()
        if "billed_po_qty" not in orders:
            orders["billed_po_qty"] = orders.open_po_qty
        if "unbilled_po_qty" not in orders:
            orders["unbilled_po_qty"] = 0
        df = df.merge(orders[["sku_key", "open_po_qty", "billed_po_qty", "unbilled_po_qty"]], on="sku_key", how="left", validate="one_to_one")
        for column in ["open_po_qty", "billed_po_qty", "unbilled_po_qty"]:
            df[column] = df[column].fillna(0)
    else:
        df["open_po_qty"] = 0
        df["billed_po_qty"] = 0
        df["unbilled_po_qty"] = 0
    df["supplier_need_before_po"] = df.bonded_est_reorder_qty
    df["bonded_est_reorder_qty"] = (df.supplier_need_before_po - df.open_po_qty).clip(lower=0)
    df.loc[df.bonded_est_reorder_qty.eq(0), "bonded_reorder"] = ""

    # ── Build final report ──
    report = pd.DataFrame({
        "SKU": df["sku"],
        "Item Name": df.get("item_name", ""),
        "Category": df.get("category", ""),
        "Country": df.get("country_of_origin", ""),
        "Brand": df.get("brand", ""),
        # Sales
        "CY Sales (YTD)": df["quantity_sold"],
        "PY Sales (YTD)": df["py_sales"],
        "YoY Sales %": df["yoy_sales_change"],
        "CY Revenue (YTD)": df["revenue"],
        "Weekly Run Rate": df["weekly_run_rate"],
        # Targets
        "Growth Target %": df["growth_target"],
        "Target Base Sales": df["target_base_sales"],
        "Target Source": df["target_source"],
        "Monthly Target": df["monthly_sales_target"],
        "Weekly Target": df["weekly_sales_target"],
        "Annual Target": df["annual_target"],
        "Expected YTD": df["expected_ytd_sales"],
        "YTD Variance": df["ytd_variance"],
        "Sales Pace %": df["sales_pace_pct"],
        "Sales Health": df["sales_health"],
        "Remaining Target": df["remaining_target"],
        "Req. Weekly Sales": df["required_weekly_sales"],
        "Req. Monthly Sales": df["required_monthly_sales"],
        "Effort Multiplier": df["effort_multiplier"],
        # Stock
        "Total Stock": df["total_stock"],
        "TWC Stock": df["twc_stock"],
        "Bonded Stock": df["bonded_stock"],
        "In Transit": df["in_transit_qty"],
        "Stock Coverage (Mo)": df["total_stock_coverage_months"],
        "Stock Coverage (Wk)": df["total_stock_coverage_weeks"],
        "TWC Coverage (Mo)": df["twc_coverage_months"],
        "TWC Coverage (Wk)": df["twc_coverage_weeks"],
        "Wks Until Stockout": df["weeks_until_stockout"],
        "Stockout Risk": df["stockout_risk"],
        "Bonded Reorder": df["bonded_reorder"],
        "TWC Reorder": df["twc_reorder"],
        "Bonded Est. Reorder Qty": df["bonded_est_reorder_qty"],
        "TWC Est. Reorder Qty": df["twc_est_reorder_qty"],
        # Purchases
        # Bottles still on order with the supplier: ordered, not cancelled and not yet billed.
        "Ordered Qty (PO)": df["unbilled_po_qty"],
        "Billed Qty (PO)": df["billed_po_qty"],
        "PO Reorder Credit Qty": df["open_po_qty"],
        "Supplier Need Before PO": df["supplier_need_before_po"],
        "CY Purchases": df["purchases_cy"],
        "PY Purchases": df["purchases_py"],
        # Movement
        "Stock Turnover": df["stock_turnover"],
    })


    # ── Health summary for KPIs ──
    has_t = df["monthly_sales_target"] > 0
    health_stats = {
        "total_skus": len(df),
        "skus_with_target": int(has_t.sum()),
        "ahead": int((df["sales_health"] == "🟢 Ahead").sum()),
        "on_track": int((df["sales_health"] == "🟡 On Track").sum()),
        "behind": int((df["sales_health"] == "🟠 Behind").sum()),
        "critical": int((df["sales_health"] == "🔴 Critical").sum()),
        "total_cy_sales": int(df["quantity_sold"].sum()),
        "total_py_sales": int(df["py_sales"].sum()),
        "total_stock": int(df["total_stock"].sum()),
        "total_twc_stock": int(df["twc_stock"].sum()),
        "total_bonded": int(df["bonded_stock"].sum()),
        "reorder_bonded": int((df["bonded_reorder"] == "REORDER").sum()),
        "reorder_twc": int((df["twc_reorder"] == "CLEAR").sum()),
        "stockout_high": int((df["stockout_risk"] == "⚠️ HIGH").sum()),
        "twc_over_total_count": _twc_over_total,
    }

    return report, health_stats
