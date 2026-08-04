from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .page_perception import PagePerceptionService

logger = logging.getLogger(__name__)

CLIENT_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEFAULT_MAX_JOBS = 128


class PageCaptureJobCapacityError(RuntimeError):
    """Raised when every bounded job slot is occupied by active work."""


class PageCaptureJobService:
    """Run page perception outside the lifetime of an extension popup request."""

    def __init__(
        self,
        page_perception: PagePerceptionService,
        *,
        max_jobs: int = DEFAULT_MAX_JOBS,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        self.page_perception = page_perception
        self.max_jobs = max_jobs
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"capture_job_{uuid4().hex}")
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._request_jobs: dict[str, str] = {}

    def submit(
        self,
        *,
        client_request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = self._normalize_client_request_id(client_request_id)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        submitted_payload = copy.deepcopy(payload)
        payload_digest = self._payload_digest(submitted_payload)

        with self._lock:
            existing_job_id = self._request_jobs.get(request_id)
            if existing_job_id is not None:
                existing = self._jobs.get(existing_job_id)
                if existing is not None:
                    if existing["_payload_digest"] != payload_digest:
                        raise ValueError(
                            "client_request_id was already used with a different payload"
                        )
                    return self._public_job(existing)
                self._request_jobs.pop(request_id, None)

            self._prune_finished_locked(required_slots=1)
            if len(self._jobs) >= self.max_jobs:
                raise PageCaptureJobCapacityError(
                    "page capture job capacity is currently exhausted"
                )

            job_id = self._unique_job_id_locked()
            now = self._clock()
            job: dict[str, Any] = {
                "job_id": job_id,
                "client_request_id": request_id,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "_payload_digest": payload_digest,
            }
            self._jobs[job_id] = job
            self._request_jobs[request_id] = job_id
            worker = threading.Thread(
                target=self._run,
                args=(job_id, submitted_payload),
                name=f"kt6-{job_id}",
                daemon=True,
            )
            worker.start()
            return self._public_job(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        if not isinstance(job_id, str) or not job_id:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public_job(job) if job is not None else None

    def _run(self, job_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "running"
            job["updated_at"] = self._clock()

        try:
            capture = self.page_perception.ingest(payload)
        except Exception as exc:  # A worker must always publish a terminal state.
            logger.exception("page capture job %s failed", job_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job["status"] = "error"
                job["error"] = self._safe_error(exc)
                job["updated_at"] = self._clock()
            return

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "completed"
            job["capture"] = copy.deepcopy(capture)
            job["updated_at"] = self._clock()

    def _prune_finished_locked(self, *, required_slots: int) -> None:
        excess = len(self._jobs) + required_slots - self.max_jobs
        if excess <= 0:
            return
        finished = sorted(
            (
                job
                for job in self._jobs.values()
                if job.get("status") in {"completed", "error"}
            ),
            key=lambda job: (float(job.get("updated_at", 0)), job["job_id"]),
        )
        for job in finished[:excess]:
            self._jobs.pop(job["job_id"], None)
            if self._request_jobs.get(job["client_request_id"]) == job["job_id"]:
                self._request_jobs.pop(job["client_request_id"], None)

    def _unique_job_id_locked(self) -> str:
        for _ in range(10):
            job_id = str(self._id_factory()).strip()
            if (
                CLIENT_REQUEST_ID_PATTERN.fullmatch(job_id)
                and job_id not in self._jobs
            ):
                return job_id
        raise RuntimeError("could not allocate a unique page capture job id")

    @staticmethod
    def _normalize_client_request_id(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("client_request_id must be a string")
        request_id = value.strip()
        if not CLIENT_REQUEST_ID_PATTERN.fullmatch(request_id):
            raise ValueError(
                "client_request_id must be 1-128 safe identifier characters"
            )
        return request_id

    @staticmethod
    def _payload_digest(payload: dict[str, Any]) -> str:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must contain JSON-compatible values") from exc
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _safe_error(exc: Exception) -> dict[str, str]:
        if isinstance(exc, (TypeError, ValueError)):
            return {
                "code": "invalid_capture",
                "message": "page capture payload is invalid",
            }
        return {
            "code": "processing_failed",
            "message": "page perception failed; inspect the backend log",
        }

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: value
            for key, value in job.items()
            if not key.startswith("_")
        }
        return copy.deepcopy(public)
