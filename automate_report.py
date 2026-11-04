"""Command-line entry point for scheduled Zoho refreshes and year-end benchmarks."""
import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pandas as pd

from planning import build_annual_plan, load_growth, prepare_planning
from report_store import publish_report
from weekly_pipeline import build_weekly_report
from zoho_books import BooksClient, ZohoError, check_read_access, discover_locations, fetch_annual_sales, fetch_weekly_inputs, load_config

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def local_today():
    # South Africa has a stable UTC+02:00 offset and no daylight-saving transitions.
    return datetime.now(timezone(timedelta(hours=2))).date()


def elapsed_weeks(as_of):
    year_days = (date(as_of.year + 1, 1, 1) - date(as_of.year, 1, 1)).days
    return ((as_of - date(as_of.year, 1, 1)).days + 1) / year_days * 52


def load_year_plan(year, override=None):
    path = Path(override) if override else DATA / f"planning_{year}.csv"
    if override and not path.exists():
        raise ValueError(f"Planning file not found: {path}")
    if not path.exists() and year == 2026:
        path = ROOT / "Sales Stock Planning 2026, M.xlsx"
    if not path.exists():
        return pd.DataFrame()
    raw = pd.read_csv(path, dtype={"SKU": str}) if path.suffix.lower() == ".csv" else pd.read_excel(path)
    return prepare_planning(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "access", "locations", "weekly", "annual"])
    parser.add_argument("--config", help="TOML secrets file; defaults to .streamlit/secrets.toml, with zoho.local.json overrides")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Sales cutoff; defaults to yesterday in South Africa. Stock remains current.")
    parser.add_argument("--target-year", type=int, help="Annual benchmark target year; requires a completed base year")
    parser.add_argument("--planning", help="Optional annual benchmark file for the weekly report")
    parser.add_argument("--publish", action="store_true", help="Publish the completed result to the local dashboard")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-api-calls", type=int, help="Maximum Books requests for this run; downloaded records are kept if reached")
    parser.add_argument("--recheck-history", action="store_true", help="Reconcile the complete transaction indexes; reuse unchanged cached details")
    cache_options = parser.add_mutually_exclusive_group()
    cache_options.add_argument("--cached", action="store_true", help="Weekly only: rebuild from complete saved inputs without any Zoho calls")
    cache_options.add_argument("--refresh-now", action="store_true", help="Bypass short pull/stock caches; still reuse unchanged transaction details")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.max_api_calls is not None:
            config = replace(config, max_api_calls=args.max_api_calls)
        if args.cached and (args.command != "weekly" or args.recheck_history):
            raise ValueError("Use --cached with weekly and without --recheck-history.")
        config.validate(require_locations=args.command in {"weekly", "check"})
        if args.command == "check":
            print("Configuration is complete. Use 'access' to verify Self Client read permissions, then 'locations' to preview warehouse stock groups.")
            return 0
        client = BooksClient(config, cache_dir=DATA / "books_cache", progress=lambda m: print(m, flush=True),
                             recheck_history=args.recheck_history, cache_only=args.cached, force_refresh=args.refresh_now)
        if args.command == "access":
            checks = check_read_access(client)
            print(checks.to_string(index=False))
            return 0 if checks.Status.eq("Ready").all() else 1
        if args.command == "locations":
            print(discover_locations(client).to_string(index=False))
            return 0
        today = local_today()
        if args.command == "annual":
            target_year = args.target_year or today.year + 1
            current, previous = fetch_annual_sales(client, target_year - 1, today)
            rates = load_growth(DATA / "brand_growth.json", target_year)
            plan = build_annual_plan(current, previous, rates)
            output = args.output_dir or ROOT / "outputs" / f"annual_{target_year}"
            output.mkdir(parents=True, exist_ok=True)
            destination = output / f"planning_{target_year}.csv"
            plan.to_csv(destination, index=False)
            if args.publish:
                DATA.mkdir(exist_ok=True)
                pending = DATA / f".planning_{target_year}.tmp"
                plan.to_csv(pending, index=False)
                pending.replace(DATA / f"planning_{target_year}.csv")
            print(f"Saved full-year benchmark for {target_year}: {destination}")
            return 0
        as_of = args.as_of or today - timedelta(days=1)
        inputs, audit, source_meta = fetch_weekly_inputs(client, as_of, today)
        inputs["planning"] = load_year_plan(as_of.year, args.planning)
        report, health = build_weekly_report(inputs, elapsed_weeks(as_of),
                                             brand_rates=load_growth(DATA / "brand_growth.json", as_of.year))
        # Historical stock movements cannot be inferred from a live location snapshot.
        report["Stock Turnover"] = float("nan")
        metadata = {**source_meta, "report_week": as_of.isocalendar().week,
                    "report_year": as_of.year, "weeks_elapsed": elapsed_weeks(as_of),
                    "health_stats": health, "saved_date": today.isoformat()}
        output = args.output_dir or ROOT / "outputs" / ("weekly_" + as_of.isoformat())
        snapshot = publish_report(output, report, metadata, audit, include_excel=True)
        if args.publish:
            publish_report(DATA, report, metadata, audit, include_excel=True)
        print(f"Saved {len(report)} products. Sales through {as_of}; live stock dated {today}. Output: {snapshot}")
        return 0
    except (ZohoError, ValueError, OSError) as exc:
        print(f"Report not refreshed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
