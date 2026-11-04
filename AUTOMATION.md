# Zoho Books report automation

The existing Streamlit dashboard now supports Zoho Books as a data source. It also has an editable growth target for each brand and target year, a yearly benchmark builder, and commands for scheduled refreshes.

## Start and connect once

Use Python 3.11 or later with the project's requirements installed:

```powershell
python -m streamlit run app.py
```

1. Sign in to the existing Admin section. Choose **Zoho Books** as the report data source.
2. Open **One-time connection setup**. Create a **Self Client** in the [Zoho API Console](https://api-console.zoho.com/) for the data center used by your Books organization. Under **Generate Code**, paste the scope string below, choose the code's validity period, enter a description such as `Weekly sales and stock reporting`, and select your Books organization when prompted. Generate a fresh code immediately before saving the connection. The default code lifetime is three minutes.
3. Enter the Books organization ID, client ID, client secret, region and grant code. **Save connection** exchanges the code for a renewable token. An existing refresh token can be used instead. Credentials stay in the ignored local `zoho.local.json`; environment variables can override the credential fields. An alternative TOML configuration is in `zoho.example.toml`.
4. **Test Books connection** verifies the selected organization and read access to each enabled dataset. Resolve any dataset marked **Needs access** using the listed scope and the Books user's role permissions. Then **Show Books location IDs** to preview the stock groups. Warehouse names are mapped automatically: **DP World** is Bonded, **In-transit** (also accepting **In Transit** and **In Traansit**) is In Transit, and **all other named warehouses** are Cleared/TWC. Capitalization, spacing and punctuation such as hyphens do not affect matching. Newly added warehouses automatically join Cleared/TWC unless they use one of the two special names. Manual ID mapping remains available by turning off **Group stock by warehouse name**.
5. Review/save the brand targets, choose a sales cutoff and **Pull from Zoho Books**. The read runs in the background with live record counts, elapsed time and a **Cancel Zoho pull** button. The dashboard remains available. The weekly report is generated automatically after the complete pull succeeds; choose **Save Report for Sharing** to publish it. **Full Report Data** contains **Sales and stock** and **Purchase order audit** tabs. **Full report Excel (includes PO audit)** downloads both sheets in one workbook, with exactly the supplied template's 26 report columns and 14 audit columns in their original order. No extra calculation or target columns are exported. Brand growth controls remain in the app and feed Annual Target. The audit's Notes show billed/received quantities and the units applied to reordering; original PO quantity remains in Ordered Qty. Source IDs and additional reconciliation fields are retained privately for traceability. Even a report with no PO lines retains the audit headers.

Repeated pulls reuse a complete saved pull for 15 minutes. **Use saved Books data (0 API calls)** rebuilds from the last complete pull for the selected cutoff without contacting Zoho, even when it is older. **Refresh now** bypasses the short pull and warehouse caches while continuing to reuse unchanged invoice details. API counts and the stock snapshot timestamp are displayed. The original sync/source dates remain visible when cached inputs are used.

The local Self Client connection and read access have been verified. Scheduling is a separate setup step after report reconciliation.

### Self Client read scopes

Paste this as one comma-separated string in **Generate Code**:

```text
ZohoBooks.settings.READ,ZohoBooks.invoices.READ,ZohoBooks.creditnotes.READ,ZohoBooks.bills.READ,ZohoBooks.debitnotes.READ,ZohoBooks.purchaseorders.READ
```

| Scope | Data used by this automation |
| --- | --- |
| `ZohoBooks.settings.READ` | Organization, items, brands/custom fields and item warehouse stock |
| `ZohoBooks.invoices.READ` | Sales invoices and optional separate sales receipts |
| `ZohoBooks.creditnotes.READ` | Customer credits/returns |
| `ZohoBooks.bills.READ` | Purchase quantities and bills linked to PO lines |
| `ZohoBooks.debitnotes.READ` | Vendor credits/returns |
| `ZohoBooks.purchaseorders.READ` | Open purchase orders and ordered quantities |

These are the read permissions for the endpoints the application uses to rebuild the required reports. The Books user must also have access to those modules and the selected organization. Use the Self Client authorization-code flow: exchange the one-time grant code for a refresh token, then let scheduled runs renew access tokens automatically. The grant code is not saved as a credential. No callback URL is needed for this Self Client flow.

Official instructions: [Books OAuth scopes and Self Client](https://www.zoho.com/books/api/v3/oauth/), [Self Client authorization-code flow](https://www.zoho.com/developer/oauth/self-client/authorization-code-flow.html).

## Brand growth and annual planning

**Growth targets by brand** shows the brand's current and prior-year unit sales over matching periods. Historical growth is `(current units - prior units) / prior units`, calculated from brand totals. A brand with no prior-year units has a blank growth percentage. Products sold only in the prior year remain in the comparison.

Enter each brand's **Target Growth %** and save. Enter `15` for 15%, `0` for zero growth, or a negative value for a lower target. Settings are saved independently for each target year in `data/brand_growth.json`. A blank brand setting retains the annual benchmark's rate/target; products without a benchmark have no target (Target Source "No planning target"), so their Annual Target, Expected YTD, % vs. Target and Sales Performance are blank. A target derived from a product's own sales would show the same pace for every such product. Their reorder quantities use the actual sales pace.

For benchmarked products, a saved brand override calculates:

```text
Annual target = base-year full-year item sales × (1 + brand growth rate)
Monthly target = annual target / 12
```

The original 2026 benchmark is automatically loaded for 2026 unless `data/planning_2026.csv` has been explicitly saved. The original workbook is unchanged. A zero benchmark is a real zero target.

To create the next benchmark, choose the target year and open **Create the annual benchmark from Books**. The builder requires the base calendar year to be complete. For example, the 2027 benchmark uses full-year 2026 sales, compares them with full-year 2025, and applies your saved 2027 brand rates. Preview the results, then choose **Use this annual benchmark**. It is saved as `data/planning_2027.csv`. Weekly refreshes reuse this fixed benchmark.

The annual builder contains item sales, historical growth, brand target growth, annual budget and monthly target. Live stock and reorder calculations remain in the weekly report so that the annual benchmark stays fixed.

## Run without downloading source files

```powershell
python automate_report.py check
python automate_report.py access
python automate_report.py locations
python automate_report.py weekly --publish
```

The default weekly sales cutoff is yesterday in South Africa. An explicit cutoff is supported:

```powershell
python automate_report.py weekly --as-of 2026-10-25 --publish
python automate_report.py annual --target-year 2027 --publish
```

To immediately check the complete transaction indexes for removed historical records, while reusing unchanged cached details:

```powershell
python automate_report.py weekly --recheck-history --publish
```

For a local rebuild with no Zoho requests, or an explicitly fresh pull with a request cap:

```powershell
python automate_report.py weekly --cached --publish
python automate_report.py weekly --refresh-now --max-api-calls 250 --publish
```

## Populate initial invoice and bill history from CSV

An initial per-invoice API import can be expensive. Fresh Zoho invoice exports containing **Invoice ID**, **Product ID**, **Quantity**, **Item Total**, **Total**, **Exchange Rate**, dates and statuses can populate the cache offline:

```powershell
python seed_invoice_cache.py Invoice00.csv Invoice01.csv --exported-at 2026-10-30T12:26:19+02:00
python seed_invoice_cache.py Bill.csv --resource bills --exported-at 2026-10-30T12:43:49+02:00
```

Use the actual export timestamp with its timezone. The importer validates the complete input before writing, preserves existing API detail files, handles an invoice split between export parts, and retains legitimate repeated product lines. It stores the parsed export in one file to avoid thousands of small-file reads on Windows. Original exports are unchanged and excluded from Git.

Purchase bill exports use the same validation and caching rules. The imported bill export contains 1,676 bills; all 52 overlapping API-cached bills matched their dates, totals and product line items. This avoids individual history requests for 1,624 additional bills.

The normal pull still obtains current invoice summaries. It uses exported line items only when the exact invoice ID, date, total, exchange rate (if returned) and status agree, and Zoho's last modification predates the export by at least five minutes. New, changed or ambiguous invoices are downloaded normally. This also handles changes made while an export was being generated. The current export import covers 8,536 unique invoices, and all 3,514 overlapping API-cached invoices matched dates, totals and product line items.

The annual example becomes valid after December 31, 2026. `--config` selects a TOML credentials file, `--planning` selects a different benchmark, and `--output-dir` selects the export folder. The process returns exit code 1 if a refresh fails.

Each successful weekly run writes `report.xlsx`, `report.csv`, `po_audit.csv` and source metadata into a dated snapshot under `outputs/`. The workbook and public CSV files use only the template's columns. Separate private `_report_data.csv` and `_po_source.csv` files preserve calculations and source IDs for dashboard restoration and reconciliation. The command prints the completed snapshot's location. `--publish` also updates this installation's dashboard through an atomic manifest change. Failed runs leave the previously published snapshot selected. An open dashboard checks for a newly published snapshot every minute.

## Schedule on Windows

The selected execution host is this Windows PC. Reports and brand settings stay in this installation, and Windows Task Scheduler will run the refresh commands.

After the first successful live pull has been compared with a Books export, register tasks from this directory:

```powershell
.\schedule_reports.ps1 -Report Weekly -PythonPath 'C:\Program Files\Python313\python.exe'
.\schedule_reports.ps1 -Report Annual -PythonPath 'C:\Program Files\Python313\python.exe'
```

The defaults are Monday at 07:00 for the weekly report and a daily 07:00 check for a missing annual benchmark. Times use Windows local time; set Windows to South Africa time. Change `-WeeklyTime` or `-AnnualTime` if needed. The annual task creates the current year's benchmark once, using the preceding complete year, and retries a failed/missed run on later days. It leaves an existing annual CSV unchanged.

These tasks run as the signed-in Windows user. The computer must be awake and that user signed in. This script registers tasks; it does not start a Streamlit server. A cloud-hosted dashboard needs its own persistent storage and scheduler: local task updates do not automatically publish to Streamlit Cloud.

## Data definitions and first-run reconciliation

- Sales use invoices less customer credit notes by default. Separate sales receipts are optional. Draft, void, cancelled and pending-approval transactions are excluded. Purchases use bills less vendor credits. Check these switches against the filters in your existing exports during the first live reconciliation.
- Non-inventory items without SKUs (such as Grape Points) are excluded from product planning and summarized in the weekly report's exclusion audit. Inventory products sold without a SKU stop the pull and identify the item to correct.
- Missing product categories use **Others**, following the supplied report. Missing brands and countries remain **Unknown** rather than receiving an inferred value.
- Quantities use the Books line-item unit. Revenue is the sum of Books `item_total` multiplied by each document's exchange rate. Confirm invoice-level discounts, tax treatment, report filters and currency against your exported Sales by Item report before using the scheduled run as the authoritative replacement.
- Sales and purchases use matching Jan 1–cutoff periods for the current/prior years. Stock and outstanding POs are **live snapshots**, with their dates shown separately. Current stock is never labeled as historical closing stock. Historical stock turnover is left blank because the item API snapshot does not supply the period's inventory movements.
- Location stock uses `location_stock_on_hand` (or the warehouse equivalent). DP World is Bonded, In-transit (any spacing or hyphenation) is In Transit, and the other warehouses are Cleared/TWC. Saved weekly data split under older rules is re-grouped from its matching item snapshot without Zoho calls. Mapped components must reconcile to the item total when Books supplies one. Missing warehouse names or an unrecognized stock response stop the refresh. The returned names and stock tracking basis will be verified during the first live connection.
- PO lines match the exact Books item ID, then combine vintages using the first two SKU segments. **Ordered Qty (PO) shows bottles not yet billed**: `max(Ordered - Cancelled - Billed, 0)`. When the exact batch (Books item) has bills dated on/after the PO date that are not linked to this PO, and invoiced sales dated on/after the PO date, the bottles on those bills are treated as already arrived (the old PO line was never closed) and are not counted; only as many bottles as those bills cover are dropped. The PO Match Audit keeps the raw Unbilled Qty, the counted quantity, the matching bills and the batch sales. Bill CSV exports must include the line-level Purchase Order Number, and the previous year's invoices are read to 31 December for this check (only the matching YTD period counts as sales). Only `max(Billed - Received - Manually Received, 0)` reduces the supplier reorder suggestion, so 100 ordered / 60 billed / 40 received shows 40 in the PO column and credits 20 against reordering. Warehouse balances supply stock and do not include PO quantities a second time. Active approved POs awaiting receipt are included even when fully billed; fully received orders, drafts, rejected/cancelled orders and pending approvals are excluded. Missing receipt quantities on a billed line stop the report. When native billed quantities are absent, linked bill lines (`purchaseorder_item_id`) supply the fallback. PO summary reads are paginated, and only relevant orders need detail reads. PO details have a 15-minute maximum cache age and are refreshed by **Refresh now** because billed/received quantities can change independently of the PO's displayed modification time. Old caches limited to open POs cannot be reused under the broader scope until the missing source data is read.
- Vintages are combined by the first two SKU segments, as in the existing app. Duplicate benchmark product keys are rejected rather than multiplying report rows.
- Reads are paginated and throttled, with token renewal and bounded retries. Current warehouse stock uses batches of 20 items instead of one API call per item. Complete input pulls and warehouse reads are cached for 15 minutes (`pull_cache_minutes` and `stock_cache_minutes`). **Refresh now** bypasses these short caches. The stock snapshot's actual timestamp is retained. Once a short cache expires, stock is read again because warehouse transfers do not reliably change item metadata. Open POs are checked during live syncs and reuse unchanged cached details. Annual planning can reuse a recent item snapshot without fetching warehouse stock.
- Invoices, customer credits, bills and vendor credits have persistent transaction indexes under the ignored `data/books_cache/` directory. The first complete read establishes a checkpoint. Later reads request `last_modified_time` changes with a five-minute overlap, merge by exact document ID, and download only new/changed detail records. Backdated transactions, voids and edits moving a document out of the reporting period are handled. A later cutoff also reads the newly included dates. Existing detail caches are reused while the new indexes are established.
- A full index reconciliation runs after 24 hours (configurable as `full_reconcile_hours`) to detect hard deletions. It checks summary lists and reuses unchanged detail files. `--recheck-history` forces this reconciliation immediately. Optional sales receipts use the full summary-list check, with detail caching, because the incremental filter has not been verified for that endpoint. If Books ignores a change filter, that dataset falls back to full index reconciliation.
- Checkpoints advance only when the entire dataset has been consumed successfully. Cancellation or failure preserves the earlier checkpoint and already downloaded detail files. Detail and index writes use atomic replacement. Cache fingerprints use source modification timestamps and accounting fields; changing an overdue-day display label does not invalidate unchanged invoice lines.
- One weekly pull has numbered stages for warehouse stock, current-year sales, prior-year sales, open POs, bills, vendor credits and report preparation. Finishing current-year invoices and starting prior-year invoices is part of the same pull. Only button click callbacks start new jobs. Completion shows counts of downloaded/reused records and Books requests. The default per-run API limit is 250 and is editable in the sidebar or through `--max-api-calls`. The first history import can be populated from fresh exports to avoid thousands of detail requests. Reaching a limit preserves all completed cache files and leaves the previous published report selected.

## Validation

```powershell
python -m unittest test_automation test_report_logic test_zoho_sync test_invoice_seed test_pull_cache test_po_report test_automation_ui -v
```

The automated checks cover brand overrides, zero/negative rates, full-year baselines, matching-period aggregation, duplicate product keys, OAuth refresh, pagination, incremental merges, backdated changes, voids, hard deletions, cutoff changes, failed/interrupted checkpoints, bulk item reads, background progress/cancellation, non-inventory exclusions, stock mapping, partial PO billing, CSV cache validation, cache freshness, and atomic publication. A Streamlit AppTest confirms that completion followed by repeated page reruns generates one report from one click. The local six-export replay reconciled sales and stock to source totals. Live Books access, bulk item reads, and modification-time filters for all four incremental datasets are verified. After both fresh CSV caches were imported, the finishing live refresh produced 783 products using 14 API calls; a subsequent saved-data rebuild produced the same product count using zero API calls. These pipeline and source-line checks do not replace an independent aggregate comparison against the Books Sales by Item report.

API references: [OAuth](https://www.zoho.com/books/api/v3/oauth/), [Items](https://www.zoho.com/books/api/v3/items/), [Invoices](https://www.zoho.com/books/api/v3/invoices/), [Credit Notes](https://www.zoho.com/books/api/v3/credit-notes/), [Bills](https://www.zoho.com/books/api/v3/bills/), [Vendor Credits](https://www.zoho.com/books/api/v3/vendor-credits/), [Purchase Orders](https://www.zoho.com/books/api/v3/purchase-order/).
