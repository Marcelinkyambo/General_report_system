"""Run the year-end benchmark once, on or after January 1, with retry on failure."""
from automate_report import DATA, local_today, main


if __name__ == "__main__":
    today = local_today()
    destination = DATA / f"planning_{today.year}.csv"
    # The task runs daily so a missed Jan 1 or temporary API outage can recover.
    # Existing annual benchmarks remain fixed; updating requires a deliberate rebuild.
    if destination.exists():
        raise SystemExit(0)
    raise SystemExit(main(["annual", "--target-year", str(today.year), "--publish"]))
