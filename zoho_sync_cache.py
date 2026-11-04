"""Persistent transaction indexes for incremental Books reads.

Detail files remain compatible with the existing cache. An index is committed
only after its complete result has been consumed, so interruption cannot skip
unread changes. Daily index reconciliation also detects hard deletions.
"""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import uuid


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Missing timestamp timezone")
    return parsed.astimezone(timezone.utc)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        pending.write_text(json.dumps(value), encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


class TransactionIndex:
    def __init__(self, path, id_field, start, end, reconcile_hours=24, force=False):
        self.path, self.id_field = Path(path), id_field
        self.start, self.end = start, end
        self.reconcile_hours, self.force = reconcile_hours, force
        self.pending = None
        self.incremental = False

    def _records_by_id(self, records):
        result = {}
        for row in records:
            identifier = str(row.get(self.id_field, ""))
            if not identifier or identifier in result:
                raise ValueError(f"Missing or repeated {self.id_field} in Books pagination.")
            date.fromisoformat(row["date"][:10])
            result[identifier] = row
        return result

    def _load(self):
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved["version"] != 1 or saved["start"] != self.start.isoformat():
                return None
            date.fromisoformat(saved["end"])
            timestamp(saved["watermark"])
            timestamp(saved["reconciled_at"])
            self._records_by_id(saved["records"])
            return saved
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def read(self, read_list, progress):
        began = utcnow()
        saved = self._load()
        covered_end = max(self.end, date.fromisoformat(saved["end"])) if saved else self.end
        full = (self.force or not saved or
                began - timestamp(saved["reconciled_at"]) >= timedelta(hours=self.reconcile_hours) or
                timestamp(saved["watermark"]) > began)
        # A server response without source versions cannot support safe deltas.
        if saved and any(not row.get("last_modified_time") for row in saved["records"]):
            full = True
        if not full:
            since = timestamp(saved["watermark"]) - timedelta(minutes=5)
            progress("Checking new and changed records since " + since.strftime("%Y-%m-%d %H:%M UTC"))
            # Do not filter by transaction date: an edited invoice can move out
            # of this report's date range, or be backdated into it.
            changes = read_list({"last_modified_time": since.strftime("%Y-%m-%dT%H:%M:%S%z")})
            changed = self._records_by_id(changes)
            try:
                respected = all(timestamp(row["last_modified_time"]) >= since for row in changes)
            except (KeyError, ValueError, TypeError):
                respected = False
            if not respected:
                progress("Books did not honor the change filter; reconciling the transaction index")
                full = True
            else:
                records = self._records_by_id(saved["records"])
                for identifier, row in changed.items():
                    if identifier in records or self.start.isoformat() <= row["date"][:10] <= covered_end.isoformat():
                        records[identifier] = row
                # A later reporting cutoff can include previously unchanged
                # documents not covered by the earlier import.
                if covered_end > date.fromisoformat(saved["end"]):
                    extension = read_list({"date_start": (date.fromisoformat(saved["end"]) + timedelta(days=1)).isoformat(),
                                           "date_end": covered_end.isoformat()})
                    records.update(self._records_by_id(extension))
                self.incremental = True
                progress(f"Using saved history; {len(changed):,} new or changed records checked")
        if full:
            progress("Reconciling transaction index" if saved else "Importing transaction history")
            records = self._records_by_id(read_list({"date_start": self.start.isoformat(), "date_end": covered_end.isoformat()}))
        self.pending = {"version": 1, "start": self.start.isoformat(), "end": covered_end.isoformat(),
                        "watermark": began.isoformat(),
                        "reconciled_at": began.isoformat() if full else saved["reconciled_at"],
                        "records": list(records.values())}
        return self.pending["records"]

    def commit(self):
        if self.pending is not None:
            write_json(self.path, self.pending)
