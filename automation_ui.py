"""Connection and brand planning controls used by the existing Streamlit app."""
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as streamlit_runtime

from automate_report import elapsed_weeks, load_year_plan, local_today
from planning import brand_history, brand_key, build_annual_plan, load_growth, save_growth
from zoho_books import (BooksClient, ZohoError, discover_locations, exchange_grant_code,
                        fetch_annual_sales, fetch_weekly_inputs, load_config, save_local_config, REGIONS,
                        SELF_CLIENT_SCOPES, check_read_access)
from zoho_jobs import PullManager

ROOT = Path(__file__).resolve().parent


@streamlit_runtime.cache_resource(show_spinner=False)
def get_pull_manager():
    return PullManager()


def _start_books_job(st, config, kind, cutoff=None, target_year=None, cache_only=False, force_refresh=False):
    config.validate(require_locations=kind == "weekly")
    manager = get_pull_manager()
    key = f"{config.region}:{config.organization_id}:{kind}:{cutoff or target_year or ''}:{cache_only}:{force_refresh}"

    def work(job):
        client = BooksClient(config, cache_dir=ROOT / "data" / "books_cache",
                             progress=job.update, cancel_check=job.check_cancelled,
                             cache_only=cache_only, force_refresh=force_refresh)
        if kind == "weekly":
            inputs, audit, metadata = fetch_weekly_inputs(client, cutoff, local_today())
            inputs["planning"] = load_year_plan(cutoff.year)
            return {"kind": kind, "payload": (inputs, audit, metadata)}
        if kind == "locations":
            return {"kind": kind, "payload": discover_locations(client)}
        current, previous = fetch_annual_sales(client, target_year - 1, local_today())
        return {"kind": "annual", "year": target_year, "payload": (current, previous)}

    job = manager.start(key, work)
    st.session_state["zoho_job_id"] = job.id
    st.session_state.pop("zoho_last_notice", None)


def _queue_books_job(config, kind, cutoff=None, target_year=None, cache_only=False, force_refresh=False):
    """Only a button click starts a read; fragment/app reruns never start one."""
    try:
        _start_books_job(streamlit_runtime, config, kind, cutoff, target_year, cache_only, force_refresh)
    except (ValueError, OSError) as exc:
        streamlit_runtime.session_state["zoho_last_notice"] = ("error", str(exc))


@streamlit_runtime.fragment(run_every=2)
def render_pull_progress():
    st = streamlit_runtime
    manager = get_pull_manager()
    identifier = st.session_state.get("zoho_job_id")
    if not identifier:
        return
    job = manager.get(identifier)
    if job is None:
        st.session_state.pop("zoho_job_id", None)
        st.warning("The app restarted during the previous pull. Start it again to reuse saved records.")
        return
    snapshot = job.snapshot()
    elapsed = int(snapshot["elapsed_seconds"])
    if snapshot["state"] in {"queued", "running"}:
        status = st.status("Reading from Zoho Books", state="running", expanded=True)
        status.write(snapshot["message"])
        status.caption(f"Elapsed: {elapsed // 60}m {elapsed % 60}s. The dashboard remains available during the pull.")
        status.caption("The first history import can take time. Unchanged records are saved for the next run.")
        if st.button("Cancel Zoho pull", key=f"cancel_zoho_{identifier}"):
            manager.cancel(identifier)
            st.caption("Cancelling after the current request finishes...")
        return
    if snapshot["state"] == "complete":
        result = job.result
        if result["kind"] == "weekly":
            inputs, audit, metadata = result["payload"]
            for key in ["sales_cy", "sales_py", "stock_cy", "stock_py", "purchases_cy", "purchases_py", "twc_stock", "bonded_stock_file", "in_transit", "item_details", "planning", "purchase_orders"]:
                st.session_state[key] = inputs.get(key, pd.DataFrame()).copy()
            st.session_state["po_audit"] = audit.copy()
            st.session_state["source_metadata"] = dict(metadata)
            st.session_state["zoho_generate_pending"] = True
            counts = metadata.get("transaction_cache", {})
            if metadata.get("served_from_cache"):
                message = "Loaded complete saved Books data. 0 API calls. Source dates are shown with the report."
            else:
                message = (f"Zoho sync complete: {counts.get('downloaded', 0):,} new/changed transaction records, "
                           f"{counts.get('reused', 0):,} reused from cache. {metadata.get('api_calls', 0):,} API calls.")
        elif result["kind"] == "locations":
            st.session_state["zoho_locations"] = result["payload"]
            message = "Warehouse groups loaded."
        else:
            current, previous = result["payload"]
            year = result["year"]
            plan = build_annual_plan(current, previous, load_growth(ROOT / "data" / "brand_growth.json", year))
            st.session_state["annual_plan_preview"] = (year, plan)
            message = f"Annual benchmark for {year} is ready to review."
        st.session_state["zoho_last_notice"] = ("success", message)
    else:
        st.session_state["zoho_last_notice"] = ("error" if snapshot["state"] == "error" else "info", snapshot["error"])
    st.session_state.pop("zoho_job_id", None)
    st.rerun()


def render_zoho_source(st):
    st.sidebar.header("Zoho Books connection")
    if st.session_state.pop("clear_zoho_grant_code", False):
        st.session_state.pop("zoho_grant_code", None)
    config = load_config()
    notice = st.session_state.get("zoho_last_notice")
    if notice:
        getattr(st.sidebar, notice[0])(notice[1])
    active_job = get_pull_manager().get(st.session_state.get("zoho_job_id"))
    busy = active_job is not None and active_job.snapshot()["state"] in {"queued", "running"}
    with st.sidebar.expander("One-time connection setup"):
        st.markdown("Create a **Self Client** in the [Zoho API Console](https://api-console.zoho.com/). Open **Generate Code**, paste these read scopes, select the Books organization and generate a code:")
        st.code(SELF_CLIENT_SCOPES, language=None)
        st.caption("The app reads the Books data needed to generate your sales, purchases, stock and PO reports. Paste the grant code before it expires. After saving, the app renews access automatically with its refresh token.")
        st.caption("Enter credentials here on your computer. They are saved locally in the ignored zoho.local.json file.")
        use_names = st.checkbox("Group stock by warehouse name", value=config.warehouse_mapping_mode == "names")
        if use_names:
            st.caption("DP World = Bonded. In Traansit (or In Transit) = In Transit. Every other named warehouse = Cleared / TWC.")
        with st.form("zoho_connection"):
            organization = st.text_input("Books organization ID", value=config.organization_id)
            region = st.selectbox("Zoho data center", list(REGIONS), index=list(REGIONS).index(config.region) if config.region in REGIONS else 0)
            client_id = st.text_input("Client ID", value=config.client_id)
            secret = st.text_input("Client secret", value=config.client_secret, type="password")
            refresh = st.text_input("Refresh token (if already available)", value=config.refresh_token, type="password")
            grant = st.text_input("New Self Client grant code", type="password", key="zoho_grant_code")
            location_fields = {}
            if not use_names:
                st.caption("Warehouse/location IDs: separate multiple IDs with commas. A location belongs in one group.")
                for key, label in [("cleared", "Cleared / TWC locations"), ("bonded", "Bonded locations"), ("in_transit", "In Transit locations")]:
                    location_fields[key] = st.text_input(label, value=", ".join(map(str, config.locations.get(key, []))))
            credit_notes = st.checkbox("Deduct customer credit notes", value=config.include_credit_notes)
            vendor_credits = st.checkbox("Deduct vendor credits", value=config.include_vendor_credits)
            receipts = st.checkbox("Include separate sales receipts", value=config.include_sales_receipts)
            if st.form_submit_button("Save connection"):
                try:
                    updated = replace(config, organization_id=organization.strip(), region=region,
                        client_id=client_id.strip(), client_secret=secret.strip(), refresh_token=refresh.strip(),
                        locations={key: [v.strip() for v in value.split(",") if v.strip()] for key, value in location_fields.items()},
                        warehouse_mapping_mode="names" if use_names else "ids",
                        include_credit_notes=credit_notes, include_vendor_credits=vendor_credits, include_sales_receipts=receipts)
                    if grant.strip():
                        updated = exchange_grant_code(updated, grant.strip())
                    save_local_config(updated)
                    config = updated
                    st.session_state["clear_zoho_grant_code"] = True
                    st.session_state.pop("zoho_access_checks", None)
                    st.success("Connection saved. Test it and preview the warehouse stock groups before pulling data.")
                except (ValueError, OSError) as exc:
                    st.error(str(exc))
    if st.sidebar.button("Test Books connection", disabled=busy):
        try:
            with st.spinner("Checking read access to every required Books dataset..."):
                checks = check_read_access(BooksClient(config))
            st.session_state["zoho_access_checks"] = checks
            if checks.Status.eq("Ready").all():
                st.sidebar.success("Self Client connected. All required Books datasets are readable.")
            else:
                st.sidebar.error("Some Books checks failed. See each dataset's status and details below.")
        except ValueError as exc:
            st.session_state.pop("zoho_access_checks", None)
            st.sidebar.error(str(exc))
    if "zoho_access_checks" in st.session_state:
        st.sidebar.dataframe(st.session_state.zoho_access_checks, hide_index=True)
    st.sidebar.button("Show Books location IDs", disabled=busy, on_click=_queue_books_job, args=(config, "locations"))
    if "zoho_locations" in st.session_state:
        st.sidebar.dataframe(st.session_state.zoho_locations, hide_index=True)
    if config.warehouse_mapping_mode == "names":
        st.sidebar.caption("Stock groups: DP World → Bonded; In Traansit / In Transit → In Transit; all other warehouses → Cleared / TWC.")
    cutoff = st.sidebar.date_input("Sales through", value=local_today() - timedelta(days=1), max_value=local_today())
    st.sidebar.caption(f"Repeat pulls reuse complete data for {config.pull_cache_minutes:g} minutes. Older pulls check transaction changes; warehouse stock is reused for up to {config.stock_cache_minutes:g} minutes. Source dates stay visible.")
    force_refresh = st.sidebar.checkbox("Refresh now", value=False, disabled=busy,
                                        help="Bypass the short pull/stock cache. Unchanged historical invoices are still reused.")
    call_limit = st.sidebar.number_input("API call limit for this pull", min_value=1, value=int(config.max_api_calls),
                                         step=10, disabled=busy,
                                         help="Stops the pull at this limit and preserves cached records. An incomplete pull keeps the previous report.")
    pull_config = replace(config, max_api_calls=int(call_limit))
    st.sidebar.button("Pull from Zoho Books", type="primary", disabled=busy, on_click=_queue_books_job,
                      args=(pull_config, "weekly"), kwargs={"cutoff": cutoff, "force_refresh": force_refresh},
                      help="Reuse recent data, or check changes and refresh expired stock. Then build the weekly report.")
    st.sidebar.button("Use saved Books data (0 API calls)", disabled=busy, on_click=_queue_books_job,
                      args=(config, "weekly"), kwargs={"cutoff": cutoff, "cache_only": True},
                      help="Rebuild from the last complete pull for this cutoff, even if it is older than 15 minutes.")
    last_meta = st.session_state.get("source_metadata", {})
    if "api_calls" in last_meta:
        st.sidebar.caption(f"Last pull: {last_meta['api_calls']:,} Books API calls. Data last synced: {last_meta.get('synced_at', 'unknown')}.")
        if last_meta.get("api_calls_by_resource"):
            with st.sidebar.expander("API calls by dataset"):
                st.dataframe(pd.DataFrame([{"Dataset": key, "Calls": value}
                                          for key, value in last_meta["api_calls_by_resource"].items()]), hide_index=True)


def render_brand_editor(st, report_year, state):
    st.subheader("Growth targets by brand")
    settings_year = st.number_input("Year for brand growth targets", min_value=2000, max_value=2100, value=int(report_year), step=1)
    path = ROOT / "data" / "brand_growth.json"
    rates = load_growth(path, settings_year)
    history = brand_history(state.get("sales_cy"), state.get("sales_py"), state.get("item_details"))
    if history.empty:
        report = state.get("report", pd.DataFrame())
        if {"SKU", "Brand", "CY Sales (YTD)", "PY Sales (YTD)"}.issubset(report.columns):
            shared = report[["SKU", "Brand"]].rename(columns={"SKU": "sku_key", "Brand": "brand"})
            current = shared.assign(quantity_sold=report["CY Sales (YTD)"])
            previous = shared.assign(quantity_sold=report["PY Sales (YTD)"])
            history = brand_history(current, previous)
        else:
            brands = sorted(report.Brand.dropna().unique()) if "Brand" in report else sorted(rates)
            history = pd.DataFrame({"Brand": brands, "CY Sales": np.nan, "PY Sales": np.nan, "Historical Growth %": np.nan})
    history["Target Growth %"] = history.Brand.map(brand_key).map(rates) * 100
    st.caption("Historical growth uses total units sold by each brand over matching sales periods. Values come from loaded inputs, or the last saved report. A blank history means there are no prior-year units or the prior-year source has not been loaded. Set each brand's target below; blank targets retain the annual benchmark or the default growth rate.")
    edited = st.data_editor(history, hide_index=True, num_rows="fixed", use_container_width=True,
        disabled=["Brand", "CY Sales", "PY Sales", "Historical Growth %"],
        column_config={"Historical Growth %": st.column_config.NumberColumn(format="%.1f%%"),
                       "Target Growth %": st.column_config.NumberColumn(min_value=-100., max_value=1000., format="%.1f%%")},
        key=f"brand_editor_{settings_year}")
    if st.button("Save brand growth targets"):
        try:
            updated = dict(rates)
            for _, row in edited.iterrows():
                key = brand_key(row["Brand"])
                if pd.isna(row["Target Growth %"]):
                    updated.pop(key, None)
                else:
                    updated[key] = row["Target Growth %"] / 100
            save_growth(path, settings_year, updated)
            st.success(f"Brand growth targets saved for {settings_year}. Generate the report again to apply them.")
        except (ValueError, OSError) as exc:
            st.error(str(exc))
    st.caption(f"The weekly report uses the saved targets for {report_year}. To prepare next year's benchmark, select that year above.")
    with st.expander("Create the annual benchmark from Books"):
        st.write(f"This uses complete calendar-year sales for {settings_year - 1} and {settings_year - 2}, then applies the saved {settings_year} growth rate for each brand.")
        if date(settings_year - 1, 12, 31) > local_today():
            st.info(f"The {settings_year} benchmark can be generated once {settings_year - 1} is complete.")
        else:
            st.button(f"Build benchmark for {settings_year}", disabled=bool(st.session_state.get("zoho_job_id")),
                      on_click=_queue_books_job, args=(load_config(), "annual"), kwargs={"target_year": settings_year})
        preview = st.session_state.get("annual_plan_preview")
        if preview and preview[0] == settings_year:
            plan = preview[1]
            st.dataframe(plan, hide_index=True)
            st.download_button("Download annual benchmark CSV", plan.to_csv(index=False), file_name=f"planning_{settings_year}.csv", mime="text/csv")
            if st.button("Use this annual benchmark"):
                destination = ROOT / "data" / f"planning_{settings_year}.csv"
                pending = destination.with_suffix(".tmp")
                plan.to_csv(pending, index=False)
                pending.replace(destination)
                if settings_year == report_year:
                    state["planning"] = load_year_plan(settings_year)
                st.success(f"Annual benchmark saved for {settings_year}.")
