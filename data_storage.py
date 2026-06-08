import csv
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tail_models import TailDiscoveryAttempt, TailDiscoveryResult, coerce_tail_attempt, coerce_tail_result


SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    route_key TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    return_date TEXT,
    cabin TEXT NOT NULL,
    passengers INTEGER NOT NULL,
    currency TEXT NOT NULL,
    price REAL NOT NULL,
    observed_at TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    notes TEXT,
    parser TEXT,
    parser_confidence TEXT,
    detail_quality TEXT,
    detail_source TEXT,
    browser_backend TEXT,
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_provider_route_time
ON price_snapshots(provider, route_key, observed_at);

CREATE TABLE IF NOT EXISTS tail_discovery_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    origin TEXT NOT NULL,
    transfer TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    cabin TEXT NOT NULL,
    passengers INTEGER NOT NULL,
    currency TEXT NOT NULL,
    price REAL NOT NULL,
    observed_at TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    flight_details TEXT,
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_tail_discovery_lookup
ON tail_discovery_results(origin, transfer, departure_date, observed_at);

CREATE TABLE IF NOT EXISTS tail_discovery_jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    payload TEXT,
    progress TEXT,
    result TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_tail_discovery_jobs_updated
ON tail_discovery_jobs(updated_at);

CREATE TABLE IF NOT EXISTS tail_discovery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    queue_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    origin TEXT NOT NULL,
    transfer TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    cabin TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT,
    method TEXT,
    price REAL,
    detail_source TEXT,
    detail_quality TEXT,
    error TEXT,
    observed_at TEXT NOT NULL,
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_tail_attempts_job
ON tail_discovery_attempts(job_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_tail_attempts_route
ON tail_discovery_attempts(origin, transfer, departure_date, destination, observed_at);
"""


@dataclass(slots=True)
class PriceSnapshot:
    provider: str
    route_key: str
    origin: str
    destination: str
    departure_date: str
    return_date: str | None
    cabin: str
    passengers: int
    currency: str
    price: float
    observed_at: str
    scraped_at: str
    notes: list[str]
    raw_payload: dict[str, Any]
    parser: str | None = None
    parser_confidence: str | None = None
    detail_quality: str | None = None
    detail_source: str | None = None
    browser_backend: str | None = None


class PriceRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(
                conn,
                "price_snapshots",
                {
                    "parser": "TEXT",
                    "parser_confidence": "TEXT",
                    "detail_quality": "TEXT",
                    "detail_source": "TEXT",
                    "browser_backend": "TEXT",
                },
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_quality
                ON price_snapshots(parser_confidence, detail_quality, detail_source)
                """
            )

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def insert_snapshot(self, snapshot: PriceSnapshot) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_snapshots (
                    provider, route_key, origin, destination, departure_date, return_date,
                    cabin, passengers, currency, price, observed_at, scraped_at, notes,
                    parser, parser_confidence, detail_quality, detail_source, browser_backend,
                    raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.provider,
                    snapshot.route_key,
                    snapshot.origin,
                    snapshot.destination,
                    snapshot.departure_date,
                    snapshot.return_date,
                    snapshot.cabin,
                    snapshot.passengers,
                    snapshot.currency,
                    snapshot.price,
                    snapshot.observed_at,
                    snapshot.scraped_at,
                    json.dumps(snapshot.notes, ensure_ascii=False),
                    snapshot.parser or snapshot.raw_payload.get("parser"),
                    snapshot.parser_confidence or snapshot.raw_payload.get("parser_confidence"),
                    snapshot.detail_quality or snapshot.raw_payload.get("detail_quality"),
                    snapshot.detail_source or snapshot.raw_payload.get("detail_source"),
                    snapshot.browser_backend or snapshot.raw_payload.get("browser_backend"),
                    json.dumps(snapshot.raw_payload, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def fetch_snapshots(
        self,
        provider: str | None = None,
        route_key: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if route_key:
            clauses.append("route_key = ?")
            params.append(route_key)
        if start_time:
            clauses.append("observed_at >= ?")
            params.append(start_time)
        if end_time:
            clauses.append("observed_at <= ?")
            params.append(end_time)

        sql = "SELECT * FROM price_snapshots"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at ASC"

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [dict(row) for row in rows]

    def export_csv(self, output_path: Path, snapshots: list[dict[str, Any]] | None = None) -> Path:
        rows = snapshots if snapshots is not None else self.fetch_snapshots()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "id",
            "provider",
            "route_key",
            "origin",
            "destination",
            "departure_date",
            "return_date",
            "cabin",
            "passengers",
            "currency",
            "price",
            "observed_at",
            "scraped_at",
            "notes",
            "parser",
            "parser_confidence",
            "detail_quality",
            "detail_source",
            "browser_backend",
            "raw_payload",
        ]
        with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return output_path

    def latest_snapshot(self, provider: str, route_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM price_snapshots
                WHERE provider = ? AND route_key = ?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (provider, route_key),
            ).fetchone()
        return dict(row) if row else None

    def count_snapshots(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM price_snapshots").fetchone()
        return int(row["count"]) if row else 0

    def insert_tail_discovery_result(self, result: TailDiscoveryResult | dict[str, Any]) -> int:
        result = coerce_tail_result(result).to_record()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tail_discovery_results (
                    provider, origin, transfer, destination, departure_date, cabin, passengers,
                    currency, price, observed_at, scraped_at, flight_details, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["provider"],
                    result["origin"],
                    result["transfer"],
                    result["destination"],
                    result["departure_date"],
                    result["cabin"],
                    int(result.get("passengers", 1)),
                    result.get("currency", "CNY"),
                    float(result["price"]),
                    result["observed_at"],
                    result["scraped_at"],
                    json.dumps(result.get("flight_details") or [], ensure_ascii=False),
                    json.dumps(result.get("raw_payload") or {}, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def fetch_tail_discovery_results(
        self,
        origin: str | None = None,
        transfer: str | None = None,
        departure_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if origin:
            clauses.append("origin = ?")
            params.append(origin)
        if transfer:
            clauses.append("transfer = ?")
            params.append(transfer)
        if departure_date:
            clauses.append("departure_date = ?")
            params.append(departure_date)

        sql = "SELECT * FROM tail_discovery_results"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at DESC, price ASC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key, default in [("flight_details", []), ("raw_payload", {})]:
                try:
                    item[key] = json.loads(item.get(key) or "null") or default
                except Exception:
                    item[key] = default
            results.append(item)
        return results

    def insert_tail_discovery_attempt(self, attempt: TailDiscoveryAttempt | dict[str, Any]) -> int:
        attempt = coerce_tail_attempt(attempt).to_record()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tail_discovery_attempts (
                    job_id, queue_key, provider, origin, transfer, destination, departure_date,
                    cabin, status, phase, method, price, detail_source, detail_quality,
                    error, observed_at, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.get("job_id"),
                    attempt["queue_key"],
                    attempt.get("provider", "ctrip"),
                    attempt["origin"],
                    attempt["transfer"],
                    attempt["destination"],
                    attempt["departure_date"],
                    attempt["cabin"],
                    attempt.get("status", "unknown"),
                    attempt.get("phase"),
                    attempt.get("method"),
                    attempt.get("price"),
                    attempt.get("detail_source"),
                    attempt.get("detail_quality"),
                    attempt.get("error"),
                    attempt.get("observed_at") or self.now_iso(),
                    json.dumps(
                        {
                            **(attempt.get("raw_payload") or {}),
                            **({"trace_path": attempt.get("trace_path")} if attempt.get("trace_path") else {}),
                            **(
                                {"diagnostic_artifact": attempt.get("diagnostic_artifact")}
                                if attempt.get("diagnostic_artifact")
                                else {}
                            ),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            return int(cursor.lastrowid)

    def fetch_tail_discovery_attempts(
        self,
        job_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        sql = "SELECT * FROM tail_discovery_attempts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["raw_payload"] = json.loads(item.get("raw_payload") or "{}")
            except Exception:
                item["raw_payload"] = {}
            results.append(item)
        return results

    def fetch_tail_destination_history(
        self,
        *,
        origin: str,
        transfer: str,
        candidates: list[str],
    ) -> dict[str, dict[str, Any]]:
        codes = [str(code or "").upper() for code in dict.fromkeys(candidates) if code]
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        stats: dict[str, dict[str, Any]] = {
            code: {
                "attempts": 0,
                "matched": 0,
                "failed": 0,
                "no_match": 0,
                "filtered": 0,
                "best_price": None,
                "last_observed_at": None,
            }
            for code in codes
        }
        with self.connect() as conn:
            attempt_rows = conn.execute(
                f"""
                SELECT destination,
                       COUNT(*) AS attempts,
                       SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) AS matched,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status = 'no_match' THEN 1 ELSE 0 END) AS no_match,
                       SUM(CASE WHEN phase = 'filtered' THEN 1 ELSE 0 END) AS filtered,
                       MAX(observed_at) AS last_observed_at
                FROM tail_discovery_attempts
                WHERE origin = ? AND transfer = ? AND destination IN ({placeholders})
                GROUP BY destination
                """,
                [origin, transfer, *codes],
            ).fetchall()
            result_rows = conn.execute(
                f"""
                SELECT destination,
                       MIN(price) AS best_price,
                       MAX(observed_at) AS last_result_at
                FROM tail_discovery_results
                WHERE origin = ? AND transfer = ? AND destination IN ({placeholders})
                GROUP BY destination
                """,
                [origin, transfer, *codes],
            ).fetchall()
        for row in attempt_rows:
            code = str(row["destination"] or "").upper()
            if code not in stats:
                continue
            stats[code].update(
                {
                    "attempts": int(row["attempts"] or 0),
                    "matched": int(row["matched"] or 0),
                    "failed": int(row["failed"] or 0),
                    "no_match": int(row["no_match"] or 0),
                    "filtered": int(row["filtered"] or 0),
                    "last_observed_at": row["last_observed_at"],
                }
            )
        for row in result_rows:
            code = str(row["destination"] or "").upper()
            if code not in stats:
                continue
            stats[code]["best_price"] = row["best_price"]
            stats[code]["last_observed_at"] = max(
                str(stats[code].get("last_observed_at") or ""),
                str(row["last_result_at"] or ""),
            ) or None
        return stats

    def upsert_tail_discovery_job(self, job: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tail_discovery_jobs (
                    job_id, status, created_at, updated_at, started_at, finished_at,
                    payload, progress, result, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    payload = excluded.payload,
                    progress = excluded.progress,
                    result = excluded.result,
                    error = excluded.error
                """,
                (
                    job["job_id"],
                    job.get("status", "queued"),
                    job.get("created_at") or self.now_iso(),
                    job.get("updated_at") or self.now_iso(),
                    job.get("started_at"),
                    job.get("finished_at"),
                    json.dumps(job.get("payload") or {}, ensure_ascii=False),
                    json.dumps(job.get("progress") or {}, ensure_ascii=False),
                    json.dumps(job.get("result"), ensure_ascii=False),
                    job.get("error"),
                ),
            )

    def fetch_tail_discovery_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tail_discovery_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._decode_tail_discovery_job(row)

    def latest_tail_discovery_job(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tail_discovery_jobs
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._decode_tail_discovery_job(row)

    def fetch_tail_discovery_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tail_discovery_jobs
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [
            item
            for row in rows
            if (item := self._decode_tail_discovery_job(row)) is not None
        ]

    @staticmethod
    def _decode_tail_discovery_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for key, default in [("payload", {}), ("progress", {}), ("result", None)]:
            try:
                value = json.loads(item.get(key) or "null")
            except Exception:
                value = default
            item[key] = default if value is None and default is not None else value
        return item

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
