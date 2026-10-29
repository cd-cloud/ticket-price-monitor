import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tail_models import TailDiscoveryAttempt, TailDiscoveryResult, coerce_tail_attempt, coerce_tail_result
from tail_quotes import itinerary_key


SCHEMA = """
CREATE TABLE IF NOT EXISTS tail_route_observations (
    root_job_id TEXT NOT NULL,
    source_candidate TEXT NOT NULL,
    observation_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    origin TEXT NOT NULL,
    transfer TEXT NOT NULL,
    destination TEXT NOT NULL,
    quality TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    context TEXT NOT NULL,
    flight_details TEXT NOT NULL,
    PRIMARY KEY(root_job_id, source_candidate, observation_key)
);
CREATE INDEX IF NOT EXISTS idx_tail_observations_route
ON tail_route_observations(origin, transfer, destination, observed_at);
CREATE TABLE IF NOT EXISTS tail_search_outcomes (
    root_job_id TEXT NOT NULL,
    source_candidate TEXT NOT NULL,
    destination TEXT NOT NULL,
    origin TEXT NOT NULL,
    transfer TEXT NOT NULL,
    query_scope TEXT NOT NULL,
    outcome TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    elapsed_seconds REAL,
    savings_amount REAL,
    currency TEXT,
    context TEXT NOT NULL,
    PRIMARY KEY(root_job_id, source_candidate, destination)
);
CREATE INDEX IF NOT EXISTS idx_tail_outcomes_route
ON tail_search_outcomes(origin, transfer, destination, observed_at);
CREATE TABLE IF NOT EXISTS tail_provider_cooldowns (
    provider TEXT PRIMARY KEY,
    until_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tail_candidate_checkpoints (
    job_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt TEXT NOT NULL,
    result_ids TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, destination)
);
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

CREATE TABLE IF NOT EXISTS tail_route_edges (
    provider TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    airline TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (provider, origin, destination, airline)
);

CREATE INDEX IF NOT EXISTS idx_tail_route_edges_lookup
ON tail_route_edges(provider, origin, destination, last_observed_at);

CREATE TABLE IF NOT EXISTS tail_negative_cache (
    signature TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    origin TEXT NOT NULL,
    transfer TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    cabin TEXT NOT NULL,
    passengers INTEGER NOT NULL,
    preferred_airlines TEXT,
    phase TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tail_negative_cache_expiry
ON tail_negative_cache(expires_at);
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
        self._transaction = threading.local()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._transaction, "connection", None)
        if active is not None:
            yield active
            return
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Nested repository calls share one transaction, isolated per worker thread."""
        if getattr(self._transaction, "connection", None) is not None:
            raise RuntimeError("Nested write transactions are not supported.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._transaction.connection = conn
            try:
                yield conn
            finally:
                self._transaction.connection = None

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(conn, "tail_discovery_results", {"itinerary_key": "TEXT"})
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tail_itinerary ON tail_discovery_results(itinerary_key, observed_at)"
            )
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
        key = itinerary_key(result)
        values = (
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
        )
        identity = values[:7] + (result["observed_at"], key)
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM tail_discovery_results
                WHERE provider = ? AND origin = ? AND transfer = ? AND destination = ?
                  AND departure_date = ? AND cabin = ? AND passengers = ? AND observed_at = ?
                  AND itinerary_key = ?
                """,
                identity,
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE tail_discovery_results
                    SET currency = ?, price = ?, scraped_at = ?, flight_details = ?, raw_payload = ?
                    WHERE id = ?
                    """,
                    (values[7], values[8], values[10], values[11], values[12], existing["id"]),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO tail_discovery_results (
                    provider, origin, transfer, destination, departure_date, cabin, passengers,
                    currency, price, observed_at, scraped_at, flight_details, raw_payload, itinerary_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, key),
            )
            return int(cursor.lastrowid)

    def fetch_tail_result(self, result_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tail_discovery_results WHERE id = ?", (result_id,)).fetchone()
        if row is None:
            raise KeyError(result_id)
        item = dict(row)
        item["flight_details"] = json.loads(item.get("flight_details") or "[]")
        item["raw_payload"] = json.loads(item.get("raw_payload") or "{}")
        return item

    def tail_known_destinations(self, *, provider: str, origin: str, transfer: str,
                                departure_date: str, cabin: str, passengers: int) -> set[str]:
        """Do not let the UI's history page limit truncate the refresh search scope."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT destination FROM tail_discovery_results
                   WHERE provider=? AND origin=? AND transfer=? AND departure_date=?
                   AND cabin=? AND passengers=?""",
                (provider, origin, transfer, departure_date, cabin, passengers),
            ).fetchall()
        return {str(row["destination"]) for row in rows}

    def checkpoint_tail_candidate(
        self, *, root_job_id: str, fingerprint: str, attempt: dict[str, Any],
        results: list[dict[str, Any]], negative_entry: dict[str, Any] | None = None,
        recheck_result_id: int = 0,
        positive_signature: str | None = None,
        route_observations: list[dict[str, Any]] | None = None,
        learning_context: dict[str, Any] | None = None,
    ) -> list[int]:
        """Commit results, evidence and completion together; safe to repeat a callback."""
        with self.transaction() as conn:
            previous = conn.execute(
                "SELECT status, result_ids FROM tail_candidate_checkpoints WHERE job_id=? AND destination=?",
                (root_job_id, attempt["destination"]),
            ).fetchone()
            if previous and previous["status"] in {"matched", "no_match"}:
                return json.loads(previous["result_ids"])
            ids = []
            for result in results:
                ids.append(self.insert_tail_discovery_result(result))
                if (result.get("raw_payload") or {}).get("validation", {}).get("status") == "valid":
                    self.record_tail_route_edges(result)
            if learning_context is not None:
                self.record_tail_learning(root_job_id, attempt, route_observations or [], learning_context)
            if ids and positive_signature:
                conn.execute("DELETE FROM tail_negative_cache WHERE signature=?", (positive_signature,))
            self.insert_tail_discovery_attempt(attempt)
            if negative_entry:
                self.cache_tail_negative(negative_entry)
            if recheck_result_id:
                previous_quote = self.fetch_tail_result(recheck_result_id)
                raw = previous_quote["raw_payload"]
                raw["recheck_outcome"] = {
                    "status": ("found" if ids else "inconclusive"
                               if attempt.get("recheck_outcome") == "inconclusive"
                               or attempt["status"] == "no_match" else "failed"),
                    "observed_at": self.now_iso(), "result_ids": ids,
                }
                # A failed recheck must not leave the old price looking current.
                if recheck_result_id not in ids:
                    raw["expires_at"] = self.now_iso()
                conn.execute("UPDATE tail_discovery_results SET raw_payload=? WHERE id=?",
                             (json.dumps(raw, ensure_ascii=False), recheck_result_id))
            conn.execute(
                """INSERT INTO tail_candidate_checkpoints
                   (job_id,destination,fingerprint,status,attempt,result_ids,updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(job_id,destination) DO UPDATE SET
                   fingerprint=excluded.fingerprint,status=excluded.status,attempt=excluded.attempt,
                   result_ids=excluded.result_ids,updated_at=excluded.updated_at""",
                (root_job_id, attempt["destination"], fingerprint, attempt["status"],
                 json.dumps(attempt, ensure_ascii=False), json.dumps(ids), self.now_iso()),
            )
        return ids

    def tail_checkpoints(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM tail_candidate_checkpoints WHERE job_id=?", (job_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["attempt"] = json.loads(item["attempt"])
            item["result_ids"] = json.loads(item["result_ids"])
            items.append(item)
        return items

    def set_tail_cooldown(self, provider: str, until_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO tail_provider_cooldowns(provider, until_at) VALUES (?,?)
                   ON CONFLICT(provider) DO UPDATE SET until_at=MAX(until_at, excluded.until_at)""",
                (provider, until_at),
            )

    def tail_cooldown(self, provider: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT until_at FROM tail_provider_cooldowns WHERE provider=?", (provider,)).fetchone()
        if row and datetime.fromisoformat(row["until_at"]) > datetime.now(timezone.utc):
            return str(row["until_at"])
        return None

    def tail_candidate_evidence(self, *, origin: str, transfer: str, candidates: list[str]) -> list[dict[str, Any]]:
        if not candidates:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT provider,cabin,destination,status,phase,observed_at,raw_payload
                    FROM tail_discovery_attempts WHERE origin=? AND transfer=?
                    AND destination IN ({','.join('?' for _ in candidates)})
                    ORDER BY observed_at DESC LIMIT 3000""",
                [origin, transfer, *candidates],
            ).fetchall()
        return [{**dict(row), "raw_payload": json.loads(row["raw_payload"] or "{}")} for row in rows]

    def record_tail_learning(self, root_job_id, attempt, observations, context):
        """Called within checkpoint transaction; retry replaces one query's learning."""
        from tail_service import OPEN_DESTINATION
        source = attempt["destination"]
        observed_at = self.now_iso()
        grouped = {}
        with self.connect() as conn:
            # Keep previous positive structural evidence if a retry returns nothing.
            for observation in observations:
                destination = observation["destination"]
                grouped.setdefault(destination, []).append(observation)
                if observation["quality"] == "valid":
                    self.record_route_edge_details(context["provider"], observation["flight_details"],
                                                   observation["observed_at"])
                conn.execute(
                    """INSERT INTO tail_route_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(root_job_id,source_candidate,observation_key) DO UPDATE SET
                       quality=excluded.quality,observed_at=excluded.observed_at,
                       context=excluded.context,flight_details=excluded.flight_details""",
                    (root_job_id, source, observation["observation_key"], context["provider"],
                     context["origin"], context["transfer"], destination, observation["quality"],
                     observation["observed_at"], json.dumps(context, ensure_ascii=False),
                     json.dumps(observation["flight_details"], ensure_ascii=False)))
            # Open search gives no evidence at all about unreturned destinations.
            destinations = list(grouped) if source == OPEN_DESTINATION else [source]
            for destination in destinations:
                rows = grouped.get(destination, [])
                valid = [r for r in rows if r["quality"] == "valid"]
                matched = [r for r in valid if r["quote_matched"]]
                # A recheck may intentionally discard other flights; not a bad-city signal.
                outcome = ("matched" if matched else "filtered" if valid and
                           all(r.get("quote_outcome") == "filtered" for r in valid) and
                           context.get("execution_mode") not in {"recheck"} else "unknown")
                savings = [r["savings_amount"] for r in matched
                           if r.get("savings_amount") is not None and r["currency"] == "CNY"]
                conn.execute(
                    """INSERT INTO tail_search_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(root_job_id,source_candidate,destination) DO UPDATE SET
                       outcome=excluded.outcome,observed_at=excluded.observed_at,
                       elapsed_seconds=excluded.elapsed_seconds,savings_amount=excluded.savings_amount,
                       currency=excluded.currency,context=excluded.context""",
                    (root_job_id, source, destination, context["origin"], context["transfer"],
                     "open" if source == OPEN_DESTINATION else "candidates", outcome, observed_at,
                     attempt.get("elapsed_seconds"), max(savings) if savings else None,
                     "CNY", json.dumps(context, ensure_ascii=False)))

    def tail_history_bundle(self, *, origin, transfer, candidates):
        if not candidates:
            return {"observations": [], "outcomes": [], "edges": [], "legacy": []}
        codes = list(dict.fromkeys(candidates))
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            bundle = {}
            for table, key in (("tail_route_observations", "observations"), ("tail_search_outcomes", "outcomes")):
                rows = conn.execute(
                    f"""SELECT * FROM {table} WHERE origin=? AND transfer=?
                        AND destination IN ({placeholders}) ORDER BY observed_at DESC LIMIT 6000""",
                    [origin, transfer, *codes]).fetchall()
                bundle[key] = [{**dict(row), "context": json.loads(row["context"])} for row in rows]
            rows = conn.execute(
                f"""SELECT * FROM tail_route_edges WHERE origin=? AND destination IN ({placeholders})
                    ORDER BY last_observed_at DESC LIMIT 3000""", [transfer, *codes]).fetchall()
            bundle["edges"] = [dict(row) for row in rows]
        # Avoid counting new-format attempts a second time as legacy evidence.
        bundle["legacy"] = [row for row in self.tail_candidate_evidence(
            origin=origin, transfer=transfer, candidates=codes)
            if not row["raw_payload"].get("learning_version")]
        return bundle

    def tail_history_destinations(self, *, origin, transfer, limit=200):
        """Candidate hints only: recent observed full paths or outgoing B→C edges."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT destination, MAX(stamp) AS last_seen FROM (
                    SELECT destination, observed_at AS stamp FROM tail_route_observations
                      WHERE origin=? AND transfer=? AND quality='valid'
                    UNION ALL
                    SELECT destination, last_observed_at AS stamp FROM tail_route_edges WHERE origin=?
                ) WHERE julianday(stamp) >= julianday('now', '-60 days') AND julianday(stamp) <= julianday('now')
                GROUP BY destination ORDER BY last_seen DESC, destination LIMIT ?""",
                (origin, transfer, transfer, limit)).fetchall()
        return [row["destination"] for row in rows
                if row["destination"] not in {origin, transfer}]

    def record_tail_route_edges(self, result: TailDiscoveryResult | dict[str, Any]) -> None:
        result = coerce_tail_result(result).to_record()
        self.record_route_edge_details(result["provider"], result.get("flight_details") or [], result["observed_at"])

    def record_route_edge_details(self, provider, details, observed_at):
        with self.connect() as conn:
            for detail in details:
                origin = str(detail.get("origin") or detail.get("departure_airport") or "").upper()
                destination = str(detail.get("destination") or detail.get("arrival_airport") or "").upper()
                if not origin or not destination or origin == destination:
                    continue
                airline = str(detail.get("airline") or "").strip().upper()
                conn.execute(
                    """
                    INSERT INTO tail_route_edges (
                        provider, origin, destination, airline, first_observed_at, last_observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, origin, destination, airline) DO UPDATE SET
                        first_observed_at = CASE WHEN julianday(excluded.first_observed_at) < julianday(tail_route_edges.first_observed_at)
                            THEN excluded.first_observed_at ELSE tail_route_edges.first_observed_at END,
                        last_observed_at = CASE WHEN julianday(excluded.last_observed_at) > julianday(tail_route_edges.last_observed_at)
                            THEN excluded.last_observed_at ELSE tail_route_edges.last_observed_at END
                    """,
                    (provider, origin, destination, airline, observed_at, observed_at),
                )

    def fetch_tail_route_edge_codes(
        self,
        *,
        provider: str,
        transfer: str,
        candidates: list[str],
        preferred_airlines: list[str],
    ) -> set[str]:
        codes = [str(code or "").upper() for code in candidates if code]
        if not codes:
            return set()
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT destination, airline FROM tail_route_edges
                WHERE provider = ? AND origin = ? AND destination IN ({placeholders})
                """,
                [provider, transfer.upper(), *codes],
            ).fetchall()
        requested = [code.upper() for code in preferred_airlines]
        return {
            str(row["destination"]).upper()
            for row in rows
            if not requested or not row["airline"] or any(code in str(row["airline"]).upper() for code in requested)
        }

    def cache_tail_negative(self, entry: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tail_negative_cache (
                    signature, provider, origin, transfer, destination, departure_date,
                    cabin, passengers, preferred_airlines, phase, observed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signature) DO UPDATE SET
                    phase = excluded.phase,
                    observed_at = excluded.observed_at,
                    expires_at = excluded.expires_at
                """,
                (
                    entry["signature"], entry["provider"], entry["origin"], entry["transfer"],
                    entry["destination"], entry["departure_date"], entry["cabin"],
                    int(entry.get("passengers", 1)), ",".join(entry.get("preferred_airlines") or []),
                    entry["phase"], entry["observed_at"], entry["expires_at"],
                ),
            )

    def active_negative_cache_codes(self, signatures: dict[str, str], *, now: str) -> set[str]:
        if not signatures:
            return set()
        by_signature = {signature: code for code, signature in signatures.items()}
        placeholders = ",".join("?" for _ in by_signature)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT signature FROM tail_negative_cache
                WHERE signature IN ({placeholders}) AND expires_at > ?
                  AND phase IN ('route_absent', 'no_via_transfer')
                """,
                [*by_signature, now],
            ).fetchall()
        return {by_signature[str(row["signature"])] for row in rows}

    def fetch_tail_discovery_results(
        self,
        origin: str | None = None,
        transfer: str | None = None,
        departure_date: str | None = None,
        limit: int = 100,
        *,
        checkpoint_job_id: str | None = None,
        offset: int = 0,
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
        if checkpoint_job_id:
            clauses.append("""id IN (
                SELECT CAST(j.value AS INTEGER)
                FROM tail_candidate_checkpoints AS c, json_each(c.result_ids) AS j
                WHERE c.job_id = ?
            )""")
            params.append(checkpoint_job_id)

        sql = "SELECT * FROM tail_discovery_results"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])

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
        provider: str | None = None,
        cabin: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        codes = [str(code or "").upper() for code in dict.fromkeys(candidates) if code]
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        scope = ""
        scope_params: list[Any] = []
        if provider:
            scope += " AND provider = ?"
            scope_params.append(provider)
        if cabin:
            scope += " AND cabin = ?"
            scope_params.append(cabin)
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
                       SUM(CASE WHEN phase IN ('filtered', 'price_filtered', 'departure_time_filtered') THEN 1 ELSE 0 END) AS filtered,
                       MAX(observed_at) AS last_observed_at
                FROM tail_discovery_attempts
                WHERE origin = ? AND transfer = ? AND destination IN ({placeholders}){scope}
                GROUP BY destination
                """,
                [origin, transfer, *codes, *scope_params],
            ).fetchall()
            result_rows = conn.execute(
                f"""
                SELECT destination,
                       MIN(price) AS best_price,
                       MAX(observed_at) AS last_result_at
                FROM tail_discovery_results
                WHERE origin = ? AND transfer = ? AND destination IN ({placeholders}){scope}
                GROUP BY destination
                """,
                [origin, transfer, *codes, *scope_params],
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
