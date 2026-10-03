"""
Abstract base class for all registry collectors.

Every data-source collector inherits from BaseCollector and implements:
  - fetch_new_or_updated(since: str | None) → list[RawRecord]
  - normalise(raw: dict) → NormalisedRecord
  - source_short_name() → str   (e.g. 'NCT', 'ChiCTR')

The run() method on BaseCollector handles the full pipeline:
  1. Initialise sync status tracking
  2. Mark sync attempt
  3. Fetch raw data from the source
  4. Save raw JSON to disk
  5. Normalise into the common schema
  6. Persist to registry_records (with versioning)
  7. Detect changes vs the previous version → trial_events (deduped by event_hash)
  8. Update crawl_log
  9. Mark sync completed
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import CONFIG, RAW_JSON_DIR, ensure_dirs
from db.connection import get_connection, transaction

logger = logging.getLogger(__name__)


def to_json_array(val: Any) -> Optional[str]:
    """Normalise a list-type field to the canonical JSON string-array format.

    All collectors must store conditions/interventions/sponsors/locations/
    secondary_endpoints in this format so that change detection, entity
    resolution and report rendering can treat sources uniformly.

    Accepts a string (split on newlines into items; otherwise a single
    element — lossless) or an already-parsed list.  Returns None for empty
    input.
    """
    if val is None:
        return None
    if isinstance(val, list):
        items = [str(v).strip() for v in val]
    elif isinstance(val, str):
        items = val.split("\n") if "\n" in val else [val]
    else:
        return None
    items = [i.strip() for i in items if i.strip()]
    return json.dumps(items, ensure_ascii=False) if items else None


def or_terms(query: str) -> List[str]:
    """Split an NCT-style "A OR B" boolean query into its bare terms.

    query_cond is authored in ClinicalTrials.gov API syntax (the shared
    disease-profile format), but each registry speaks its own dialect:
    CTIS containAny wants comma-joined terms, ISRCTN/EUCTR want
    quoted-phrase OR.  Splitting here gives every adapter one canonical
    starting point.
    """
    return [term.strip().strip('"') for term in (query or "").split(" OR ") if term.strip()]


@dataclass
class NormalisedRecord:
    """Canonical representation of a trial record, regardless of source."""
    source_trial_id: str
    title: str
    scientific_title: Optional[str] = None
    study_type: Optional[str] = None
    status: Optional[str] = None
    enrollment: Optional[int] = None
    registration_date: Optional[str] = None
    start_date: Optional[str] = None
    primary_completion_date: Optional[str] = None
    completion_date: Optional[str] = None
    last_updated_at_source: Optional[str] = None
    conditions: Optional[str] = None          # JSON array
    interventions: Optional[str] = None       # JSON array
    countries: Optional[str] = None           # JSON array
    locations: Optional[str] = None           # JSON array
    sponsors: Optional[str] = None            # JSON array
    study_phase: Optional[str] = None
    study_design: Optional[str] = None
    eligibility_criteria: Optional[str] = None
    primary_endpoint: Optional[str] = None
    secondary_endpoints: Optional[str] = None
    arm_group_interventions: Optional[str] = None
    source_url: Optional[str] = None
    raw_payload: Optional[str] = None         # Original JSON response

    def compute_hash(self) -> str:
        """SHA-256 of the canonical fields (excluding housekeeping ones)."""
        d = {k: v for k, v in self.__dict__.items()
             if k not in ("raw_payload",)}
        raw = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BaseCollector(ABC):
    """Subclass per registry.  Call .run() to execute one crawl cycle."""

    def __init__(self, source_key: str):
        self.source_key = source_key          # matches config key, e.g. "clinicaltrials_gov"
        cfg = CONFIG.sources[source_key]
        self.cfg = cfg
        self._source_id: Optional[int] = None

    # ── Subclass must implement ────────────────────────────────────

    @abstractmethod
    def fetch_new_or_updated(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Hit the source API/website and return raw dicts.
        'since' is an ISO datetime for incremental crawling (None = full fetch).
        """

    @abstractmethod
    def normalise(self, raw: Dict[str, Any]) -> NormalisedRecord:
        """Convert a raw source dict into a NormalisedRecord."""

    # ── Source identity ────────────────────────────────────────────

    @property
    def source_id(self) -> int:
        if self._source_id is None:
            conn = get_connection()
            cur = conn.execute(
                "SELECT source_id FROM registry_sources WHERE short_name = ?",
                (self.cfg.short_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"Source {self.cfg.short_name} not found in DB")
            self._source_id = row["source_id"]
        return self._source_id

    # ── Main pipeline ──────────────────────────────────────────────

    def run(self, since: Optional[str] = None,
            is_bootstrap: bool = False) -> Dict[str, Any]:
        """Execute one full crawl-save-detect cycle.

        Parameters
        ----------
        since : str or None
            ISO-8601 timestamp for incremental fetching.  None = full fetch.
        is_bootstrap : bool
            When True, all imported records are marked is_bootstrap=1 so they
            are excluded from "new records" counts in reports.

        Returns a summary dict: {found, new, updated, skipped, changes}.
        """
        ensure_dirs()
        self._init_sync_status()
        self._mark_sync_attempted()

        log_id = self._start_crawl_log()
        summary: Dict[str, Any] = {"found": 0, "new": 0, "updated": 0,
                                   "skipped": 0, "changes": 0, "failed": 0}

        # ── Fetch with retry ───────────────────────────────────────
        attempts = 0
        last_error: Optional[str] = None
        raw_records: List[Dict] = []

        while attempts <= CONFIG.crawl.retry_attempts:
            try:
                raw_records = self.fetch_new_or_updated(since)
                summary["found"] = len(raw_records)
                break
            except NotImplementedError:
                # Placeholder collector — surface immediately instead of
                # burning through the retry loop.
                raise
            except Exception as exc:
                attempts += 1
                last_error = str(exc)
                logger.warning("Fetch attempt %d/%d failed: %s",
                               attempts, CONFIG.crawl.retry_attempts, exc)
                if attempts > CONFIG.crawl.retry_attempts:
                    self._finish_crawl_log(log_id, "failed", error=last_error)
                    self._mark_sync_failed()
                    summary["status"] = "failed"
                    summary["failure_stage"] = "fetch"
                    summary["error_message"] = last_error
                    return summary
                time.sleep(CONFIG.crawl.retry_delay_sec)

        # ── Process each record ────────────────────────────────────
        error_count = 0
        for raw in raw_records:
            try:
                normalised = self.normalise(raw)

                # Persist raw JSON to disk
                self._save_raw_json(normalised.source_trial_id, raw)

                # Upsert → version chain & change detection
                result = self._upsert_record(normalised, is_bootstrap=is_bootstrap)
                action = result["action"]
                summary[action] += 1

                if action == "updated":
                    n = self._detect_and_persist_events(
                        record_id=result["record_id"],
                        previous_record_id=result["previous_record_id"],
                        source_trial_id=normalised.source_trial_id,
                    )
                    summary["changes"] += n

                # Polite delay happens at the network layer inside
                # fetch_new_or_updated(); records here arrive as a batch, so
                # sleeping per record would only slow local DB work down.

            except Exception as exc:
                logger.error("Error processing record: %s", exc)
                error_count += 1
                continue

        # ── Finalise ───────────────────────────────────────────────
        crawl_status = "partial" if (error_count > 0 and summary["found"] > 0) else "completed"
        self._finish_crawl_log(log_id, crawl_status,
                               new=summary["new"], updated=summary["updated"])
        self._mark_sync_completed(is_bootstrap=is_bootstrap,
                                  record_count=summary["found"])
        summary["failed"] = error_count
        summary["status"] = "partially_succeeded" if crawl_status == "partial" else "succeeded"
        return summary

    # ── Change detection hook ──────────────────────────────────────

    def _detect_and_persist_events(self, record_id: int,
                                   previous_record_id: int,
                                   source_trial_id: str) -> int:
        """Detect field-level changes and persist deduped trial_events."""
        from core.change_detection import detect_and_save_events
        return detect_and_save_events(
            record_id=record_id,
            previous_record_id=previous_record_id,
            source_id=self.source_id,
            source_trial_id=source_trial_id,
        )

    def _upsert_and_emit_events(self, norm: NormalisedRecord) -> Dict[str, Any]:
        """Upsert a normalised record and emit trial_events on change.

        Shared by the detail-page re-visit paths (enrich / refresh), which
        call ``_upsert_record`` directly instead of going through ``run()``'s
        loop — without this hook their version bumps would never surface as
        trial_events.  Hash-skip keeps repeated re-fetches a no-op for the
        version chain.

        Returns ``{"action": "new"|"updated"|"skipped", "changes": int}``.
        """
        result = self._upsert_record(norm)
        changes = 0
        if result["action"] == "updated":
            changes = self._detect_and_persist_events(
                record_id=result["record_id"],
                previous_record_id=result["previous_record_id"],
                source_trial_id=norm.source_trial_id,
            )
        return {"action": result["action"], "changes": changes}

    # ── Raw JSON persistence ───────────────────────────────────────

    def _save_raw_json(self, trial_id: str, raw_data: dict) -> None:
        """Write the raw API response to disk as pretty-printed JSON."""
        dest_dir = RAW_JSON_DIR / self.cfg.short_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_id = trial_id.replace("/", "_")
        dest_path = dest_dir / f"{safe_id}.json"
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, ensure_ascii=False, indent=2)

    # ── Sync status management ─────────────────────────────────────

    def _init_sync_status(self) -> None:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO sync_status (source_id) VALUES (?)",
            (self.source_id,),
        )
        conn.commit()

    def _mark_sync_attempted(self) -> None:
        conn = get_connection()
        conn.execute(
            "UPDATE sync_status SET last_attempted_sync = datetime('now'), "
            "updated_at = datetime('now') WHERE source_id = ?",
            (self.source_id,),
        )
        conn.commit()

    def _mark_sync_completed(self, is_bootstrap: bool = False,
                             record_count: int = 0) -> None:
        conn = get_connection()
        # Use actual DB count (more reliable than per-run count)
        cur = conn.execute(
            "SELECT count(*) FROM registry_records WHERE source_id = ? "
            "AND is_latest = 1",
            (self.source_id,),
        )
        actual_count = cur.fetchone()[0]

        updates = [
            "last_successful_sync = datetime('now')",
            "updated_at = datetime('now')",
            "last_record_count = ?",
        ]
        params: List[Any] = [actual_count]
        if is_bootstrap:
            updates.append("bootstrap_completed = 1")
        conn.execute(
            f"UPDATE sync_status SET {', '.join(updates)} WHERE source_id = ?",
            (*params, self.source_id),
        )
        conn.commit()

    def _mark_sync_failed(self) -> None:
        conn = get_connection()
        conn.execute(
            "UPDATE sync_status SET updated_at = datetime('now') WHERE source_id = ?",
            (self.source_id,),
        )
        conn.commit()

    # ── Upsert logic ───────────────────────────────────────────────

    def _upsert_record(self, norm: NormalisedRecord,
                       is_bootstrap: bool = False) -> Dict[str, Any]:
        """Insert or version a registry_record.

        Returns dict::
            {"action": "new"|"updated"|"skipped",
             "record_id": int|None,
             "previous_record_id": int|None}
        """
        conn = get_connection()
        cur = conn.execute(
            "SELECT record_id, data_hash, version_number FROM registry_records "
            "WHERE source_id = ? AND source_trial_id = ? AND is_latest = 1",
            (self.source_id, norm.source_trial_id),
        )
        existing = cur.fetchone()
        new_hash = norm.compute_hash()

        if existing is None:
            # Brand new record — commit explicitly: durability must not depend
            # on some later incidental commit on the same connection.
            with transaction():
                new_id = self._insert_record(norm, version=1, is_bootstrap=is_bootstrap)
            return {"action": "new", "record_id": new_id, "previous_record_id": None}

        if existing["data_hash"] == new_hash:
            # No change — touch last_crawled_at only
            with transaction():
                conn.execute(
                    "UPDATE registry_records SET last_crawled_at = datetime('now') "
                    "WHERE record_id = ?",
                    (existing["record_id"],),
                )
            return {"action": "skipped", "record_id": existing["record_id"],
                    "previous_record_id": None}

        # Data changed — version the old row and insert new
        old_id = existing["record_id"]
        new_version = existing["version_number"] + 1

        with transaction():
            conn.execute(
                "UPDATE registry_records SET is_latest = 0 WHERE record_id = ?",
                (old_id,),
            )
            new_id = self._insert_record(norm, new_version,
                                         is_bootstrap=is_bootstrap)
            conn.execute(
                "UPDATE registry_records SET superseded_by = ? WHERE record_id = ?",
                (new_id, old_id),
            )

        return {"action": "updated", "record_id": new_id,
                "previous_record_id": old_id}

    def _insert_record(self, norm: NormalisedRecord, version: int,
                       is_bootstrap: bool = False) -> int:
        """Insert a new registry_record row and return its record_id."""
        conn = get_connection()
        cur = conn.execute("""
            INSERT INTO registry_records (
                source_id, source_trial_id, source_url,
                title, scientific_title, study_type_id, status_id,
                enrollment, registration_date, start_date,
                primary_completion_date, completion_date, last_updated_at_source,
                conditions, interventions, countries, locations, sponsors,
                study_phase, study_design, eligibility_criteria,
                primary_endpoint, secondary_endpoints, arm_group_interventions,
                raw_payload, data_hash, version_number, is_latest, is_bootstrap
            ) VALUES (
                ?, ?, ?,
                ?, ?,
                (SELECT study_type_id FROM study_types WHERE label = ?),
                (SELECT status_type_id FROM status_types WHERE label = ?),
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, 1, ?
            )
        """, (
            self.source_id, norm.source_trial_id, norm.source_url,
            norm.title, norm.scientific_title,
            norm.study_type, norm.status,
            norm.enrollment, norm.registration_date, norm.start_date,
            norm.primary_completion_date, norm.completion_date, norm.last_updated_at_source,
            norm.conditions, norm.interventions, norm.countries, norm.locations, norm.sponsors,
            norm.study_phase, norm.study_design, norm.eligibility_criteria,
            norm.primary_endpoint, norm.secondary_endpoints, norm.arm_group_interventions,
            norm.raw_payload, norm.compute_hash(), version,
            1 if is_bootstrap else 0,
        ))
        return cur.lastrowid

    # ── Crawl log ──────────────────────────────────────────────────

    def _start_crawl_log(self) -> int:
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO crawl_log (source_id, started_at, status) "
            "VALUES (?, datetime('now'), 'running')",
            (self.source_id,),
        )
        conn.commit()
        return cur.lastrowid

    def _finish_crawl_log(self, log_id: int, status: str,
                          new: int = 0, updated: int = 0,
                          error: Optional[str] = None) -> None:
        conn = get_connection()
        conn.execute("""
            UPDATE crawl_log
            SET completed_at = datetime('now'),
                records_new = ?, records_updated = ?,
                status = ?, error_message = ?
            WHERE log_id = ?
        """, (new, updated, status, error, log_id))
        conn.commit()
