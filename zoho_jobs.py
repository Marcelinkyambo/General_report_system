"""Background Zoho reads. Worker threads never call Streamlit or modify reports."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event, RLock
import logging
import time
import uuid

from zoho_books import ZohoError


class PullCancelled(ZohoError):
    pass


@dataclass(repr=False)
class PullJob:
    key: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "queued"
    message: str = "Waiting to start the Zoho pull"
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    result: object = None
    error: str = ""
    cancel_event: Event = field(default_factory=Event)
    lock: RLock = field(default_factory=RLock)

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise PullCancelled("Zoho pull cancelled. The previous report is still available.")

    def update(self, message):
        self.check_cancelled()
        with self.lock:
            self.message = str(message)

    def snapshot(self):
        with self.lock:
            return {"id": self.id, "state": self.state, "message": self.message, "error": self.error,
                    "elapsed_seconds": (self.finished or time.monotonic()) - self.started}


class PullManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zoho-pull")
        self.jobs = {}
        self.lock = RLock()

    def start(self, key, work):
        with self.lock:
            for job in self.jobs.values():
                if job.key == key and job.state in {"queued", "running"}:
                    return job
            # Keep only a few completed results in memory.
            completed = [job for job in self.jobs.values() if job.state not in {"queued", "running"}]
            for old in completed[:-2]:
                self.jobs.pop(old.id, None)
            job = PullJob(key=key)
            self.jobs[job.id] = job
            self.executor.submit(self._run, job, work)
            return job

    def _run(self, job, work):
        try:
            job.check_cancelled()
            with job.lock:
                job.state = "running"
            result = work(job)
            job.check_cancelled()
            with job.lock:
                job.result = result
                job.state = "complete"
                job.message = "Zoho data is ready"
        except PullCancelled as exc:
            with job.lock:
                job.state, job.error = "cancelled", str(exc)
        except Exception as exc:
            # API exceptions are deliberately sanitized by BooksClient.
            with job.lock:
                job.state = "error"
                job.error = str(exc) if isinstance(exc, (ValueError, OSError)) else "The Zoho pull failed. Check the local application log."
            logging.getLogger(__name__).exception("Zoho background pull failed")
        finally:
            with job.lock:
                job.finished = time.monotonic()

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id):
        job = self.get(job_id)
        if job:
            job.cancel_event.set()

    def close(self):
        for job in list(self.jobs.values()):
            job.cancel_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
