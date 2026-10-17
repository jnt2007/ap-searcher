"""Background job runner: one worker thread, observable status, cancellable."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any

from .cache import Store
from .config import Config
from .pipeline import RunOptions, run

log = logging.getLogger(__name__)


class JobManager:
    """Runs at most one pipeline job at a time and exposes its live status."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._status: dict[str, Any] = {"state": "idle"}
        self._seq = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel.set()
        with self._lock:
            self._status["state"] = "cancelling"
        return True

    def start(self, opts: RunOptions, label: str = "run") -> dict[str, Any]:
        if self.running:
            raise RuntimeError("a job is already running")
        self._cancel.clear()
        self._seq += 1
        job_id = f"job-{self._seq}"
        with self._lock:
            self._status = {
                "state": "running",
                "job_id": job_id,
                "label": label,
                "stage": "starting",
                "message": "",
                "done": 0,
                "total": 0,
                "started_at": time.time(),
                "profile": opts.profile,
            }
        self._thread = threading.Thread(
            target=self._run, args=(opts, job_id), name=job_id, daemon=True
        )
        self._thread.start()
        return self.status()

    # ------------------------------------------------------------------ internals

    def _progress(self, **kw: Any) -> None:
        with self._lock:
            self._status.update(kw)

    def _run(self, opts: RunOptions, job_id: str) -> None:
        store = Store(self.cfg.cache.path)
        try:
            result = run(self.cfg, opts, store, progress=self._progress, cancel=self._cancel)
            with self._lock:
                self._status.update(
                    {
                        "state": "cancelled" if self._cancel.is_set() else "done",
                        "stage": "finished",
                        "message": "",
                        "finished_at": time.time(),
                        "result": result.as_dict(),
                    }
                )
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            log.exception("job %s failed", job_id)
            with self._lock:
                self._status.update(
                    {
                        "state": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=5),
                        "finished_at": time.time(),
                    }
                )
        finally:
            store.close()
