"""SQLite-backed cache and state store.

One file holds everything: raw HTTP bodies, parsed listings, detail payloads,
amenity verdicts, routing results and user marks. Opened per thread; WAL mode
plus a single writer thread keeps contention out of the picture.
"""

from __future__ import annotations

import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    status     INTEGER NOT NULL,
    body       BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id           INTEGER PRIMARY KEY,
    title        TEXT,
    deal         TEXT,
    disposition  TEXT,
    price_czk    INTEGER,
    price_unit   TEXT,
    price_per_m2 INTEGER,
    area_m2      REAL,
    city         TEXT,
    city_part    TEXT,
    district     TEXT,
    street       TEXT,
    lat          REAL,
    lon          REAL,
    url          TEXT,
    image_url    TEXT,
    published_on TEXT,
    edited_on    TEXT,
    furnished    TEXT,
    first_seen   REAL,
    last_seen    REAL,
    removed_at   REAL,
    raw_json     TEXT
);

CREATE TABLE IF NOT EXISTS listing_profiles (
    listing_id INTEGER NOT NULL,
    profile    TEXT NOT NULL,
    first_seen REAL,
    last_seen  REAL,
    PRIMARY KEY (listing_id, profile)
);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id INTEGER NOT NULL,
    seen_at    REAL NOT NULL,
    price_czk  INTEGER,
    PRIMARY KEY (listing_id, seen_at)
);

CREATE TABLE IF NOT EXISTS details (
    listing_id     INTEGER PRIMARY KEY,
    fetched_at     REAL NOT NULL,
    description    TEXT,
    params_json    TEXT,
    transport_json TEXT
);

CREATE TABLE IF NOT EXISTS amenities (
    listing_id INTEGER NOT NULL,
    key        TEXT NOT NULL,
    state      TEXT NOT NULL,
    evidence   TEXT,
    PRIMARY KEY (listing_id, key)
);

CREATE TABLE IF NOT EXISTS routes (
    key        TEXT PRIMARY KEY,
    listing_id INTEGER,
    mode       TEXT NOT NULL,
    arrive_by  TEXT NOT NULL,
    status     TEXT NOT NULL,
    duration_s INTEGER,
    transfers  INTEGER,
    summary    TEXT,
    legs_json  TEXT,
    fetched_at REAL NOT NULL
);

-- One row per marked listing. `state` is the exclusive shortlist/hide choice
-- ('' = neither); `viewed` is an independent flag, so a listing can be both.
CREATE TABLE IF NOT EXISTS marks (
    listing_id INTEGER PRIMARY KEY,
    state      TEXT NOT NULL,
    note       TEXT,
    viewed     INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_routes_listing ON routes (listing_id, mode);
CREATE INDEX IF NOT EXISTS idx_http_kind ON http_cache (kind, fetched_at);
"""


# Columns added after the first release; applied to existing databases on connect.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("listings", "published_on", "TEXT"),
    ("listings", "edited_on", "TEXT"),
    ("listings", "furnished", "TEXT"),
    ("marks", "viewed", "INTEGER NOT NULL DEFAULT 0"),
)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an older database file up to date. Idempotent."""
    applied = []
    for table, column, decl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    applied += _rekey_timeless_walks(conn)
    if applied:
        conn.commit()
    return applied


def _rekey_timeless_walks(conn: sqlite3.Connection) -> list[str]:
    """Drop the arrival stamp from walk route keys written by earlier versions.

    Walking time does not depend on the timetable, but the old key ended with the
    arrival moment, so every walk expired when the arrival date rolled to the next
    week. The key's last segment is rewritten in place; duplicates that collapse onto
    the same key are dropped, keeping the most recent.
    """
    stale = conn.execute(
        "SELECT key, fetched_at FROM routes WHERE mode = 'walk' AND key NOT LIKE '%|any'"
    ).fetchall()
    if not stale:
        return []
    kept: set[str] = set()
    for row in sorted(stale, key=lambda r: r["fetched_at"], reverse=True):
        new_key = row["key"].rsplit("|", 1)[0] + "|any"
        if new_key in kept or conn.execute(
            "SELECT 1 FROM routes WHERE key = ?", (new_key,)
        ).fetchone():
            conn.execute("DELETE FROM routes WHERE key = ?", (row["key"],))
            continue
        conn.execute(
            "UPDATE routes SET key = ?, arrive_by = 'any' WHERE key = ?", (new_key, row["key"])
        )
        kept.add(new_key)
    return [f"routes.key: re-keyed {len(stale)} walk route(s)"]


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


class Store:
    """Thin data-access layer. Instantiate one per thread."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = connect(path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- http

    def http_get(self, url: str, max_age_s: float) -> str | None:
        row = self.conn.execute(
            "SELECT body, fetched_at, status FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if not row or row["status"] != 200:
            return None
        if time.time() - row["fetched_at"] > max_age_s:
            return None
        body = row["body"]
        # sreality pages are ~700 KB of HTML each, so they are stored deflated.
        if isinstance(body, bytes):
            return zlib.decompress(body).decode("utf-8")
        return body

    def http_put(self, url: str, kind: str, status: int, body: str) -> None:
        blob = zlib.compress(body.encode("utf-8"), 6)
        self.conn.execute(
            "INSERT INTO http_cache (url, kind, fetched_at, status, body) VALUES (?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, fetched_at=excluded.fetched_at, "
            "status=excluded.status, body=excluded.body",
            (url, kind, time.time(), status, sqlite3.Binary(blob)),
        )
        self.conn.commit()

    # ------------------------------------------------------------ listings

    def upsert_listing(self, listing: dict[str, Any], profile: str) -> dict[str, Any]:
        """Insert or update a listing. Returns flags: {'is_new': bool, 'price_delta': int|None}."""
        now = time.time()
        lid = listing["id"]
        prev = self.conn.execute(
            "SELECT price_czk, first_seen FROM listings WHERE id = ?", (lid,)
        ).fetchone()
        is_new = prev is None
        price_delta = None
        if prev is not None and prev["price_czk"] != listing.get("price_czk"):
            if prev["price_czk"] and listing.get("price_czk"):
                price_delta = listing["price_czk"] - prev["price_czk"]

        cols = (
            "title", "deal", "disposition", "price_czk", "price_unit", "price_per_m2",
            "area_m2", "city", "city_part", "district", "street", "lat", "lon",
            "url", "image_url", "raw_json",
        )
        values = [listing.get(c) for c in cols]
        if is_new:
            self.conn.execute(
                f"INSERT INTO listings (id, {', '.join(cols)}, first_seen, last_seen, removed_at) "
                f"VALUES (?{', ?' * len(cols)}, ?, ?, NULL)",
                [lid, *values, now, now],
            )
        else:
            self.conn.execute(
                f"UPDATE listings SET {', '.join(c + ' = ?' for c in cols)}, "
                "last_seen = ?, removed_at = NULL WHERE id = ?",
                [*values, now, lid],
            )
        if is_new or price_delta is not None:
            self.conn.execute(
                "INSERT OR REPLACE INTO price_history (listing_id, seen_at, price_czk) VALUES (?,?,?)",
                (lid, now, listing.get("price_czk")),
            )
        self.conn.execute(
            "INSERT INTO listing_profiles (listing_id, profile, first_seen, last_seen) VALUES (?,?,?,?) "
            "ON CONFLICT(listing_id, profile) DO UPDATE SET last_seen = excluded.last_seen",
            (lid, profile, now, now),
        )
        self.conn.commit()
        return {"is_new": is_new, "price_delta": price_delta}

    def mark_removed(self, profile: str, seen_ids: Iterable[int], since: float) -> int:
        """Flag listings of a profile that were not seen in the latest sweep."""
        ids = list(seen_ids)
        placeholders = ",".join("?" * len(ids)) or "NULL"
        cur = self.conn.execute(
            f"UPDATE listings SET removed_at = ? WHERE removed_at IS NULL AND id NOT IN ({placeholders}) "
            "AND id IN (SELECT listing_id FROM listing_profiles WHERE profile = ?)",
            [since, *ids, profile],
        )
        self.conn.commit()
        return cur.rowcount

    def listings(self, profiles: str | Iterable[str] | None = None) -> list[sqlite3.Row]:
        """No profile (or an empty selection) means every profile."""
        names = [profiles] if isinstance(profiles, str) else list(profiles or [])
        sql = (
            "SELECT l.* FROM listings l "
            "JOIN listing_profiles lp ON lp.listing_id = l.id "
        )
        if names:
            sql += f"WHERE lp.profile IN ({','.join('?' * len(names))}) "
        return self.conn.execute(sql + "GROUP BY l.id", names).fetchall()

    def profiles_of(self, listing_id: int) -> list[str]:
        return [
            r["profile"]
            for r in self.conn.execute(
                "SELECT profile FROM listing_profiles WHERE listing_id = ?", (listing_id,)
            )
        ]

    def price_history(self, listing_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT seen_at, price_czk FROM price_history WHERE listing_id = ? ORDER BY seen_at",
            (listing_id,),
        ).fetchall()

    # -------------------------------------------------------------- details

    def detail(self, listing_id: int, max_age_s: float | None = None) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM details WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if row and max_age_s is not None and time.time() - row["fetched_at"] > max_age_s:
            return None
        return row

    def set_dates(self, listing_id: int, published_on: str | None, edited_on: str | None) -> None:
        self.conn.execute(
            "UPDATE listings SET published_on = ?, edited_on = ? WHERE id = ?",
            (published_on, edited_on, listing_id),
        )
        self.conn.commit()

    def set_furnished(self, listing_id: int, state: str) -> None:
        self.conn.execute(
            "UPDATE listings SET furnished = ? WHERE id = ?", (state, listing_id)
        )
        self.conn.commit()

    def put_detail(
        self, listing_id: int, description: str, params: dict, transport: Any
    ) -> None:
        self.conn.execute(
            "INSERT INTO details (listing_id, fetched_at, description, params_json, transport_json) "
            "VALUES (?,?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET fetched_at=excluded.fetched_at, "
            "description=excluded.description, params_json=excluded.params_json, "
            "transport_json=excluded.transport_json",
            (
                listing_id,
                time.time(),
                description,
                json.dumps(params, ensure_ascii=False),
                json.dumps(transport, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------ amenities

    def put_amenities(self, listing_id: int, results: dict[str, dict]) -> None:
        self.conn.executemany(
            "INSERT INTO amenities (listing_id, key, state, evidence) VALUES (?,?,?,?) "
            "ON CONFLICT(listing_id, key) DO UPDATE SET state=excluded.state, evidence=excluded.evidence",
            [(listing_id, k, v["state"], v.get("evidence")) for k, v in results.items()],
        )
        self.conn.commit()

    def amenities_map(self) -> dict[int, dict[str, dict]]:
        out: dict[int, dict[str, dict]] = {}
        for r in self.conn.execute("SELECT * FROM amenities"):
            out.setdefault(r["listing_id"], {})[r["key"]] = {
                "state": r["state"],
                "evidence": r["evidence"],
            }
        return out

    # --------------------------------------------------------------- routes

    def route(self, key: str, max_age_s: float) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM routes WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] > max_age_s:
            return None
        return row

    def put_route(self, key: str, listing_id: int, mode: str, arrive_by: str, result: dict) -> None:
        self.conn.execute(
            "INSERT INTO routes (key, listing_id, mode, arrive_by, status, duration_s, transfers, "
            "summary, legs_json, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET status=excluded.status, duration_s=excluded.duration_s, "
            "transfers=excluded.transfers, summary=excluded.summary, legs_json=excluded.legs_json, "
            "fetched_at=excluded.fetched_at",
            (
                key,
                listing_id,
                mode,
                arrive_by,
                result.get("status", "ok"),
                result.get("duration_s"),
                result.get("transfers"),
                result.get("summary"),
                json.dumps(result.get("legs"), ensure_ascii=False),
                time.time(),
            ),
        )
        self.conn.commit()

    def routes_by_key(self) -> dict[str, sqlite3.Row]:
        """Keyed by route key, not listing id: listings in the same building share a route.

        The key already encodes coordinates, mode and (for transit) the arrival moment,
        so no further filtering is needed; stale rows are simply never looked up.
        """
        return {r["key"]: r for r in self.conn.execute("SELECT * FROM routes")}

    # ---------------------------------------------------------------- marks

    def set_mark(self, listing_id: int, state: str, note: str | None = None) -> None:
        if state == "clear":
            # "clear" drops the shortlist/hide choice and its note, as it always has,
            # but a row still flagged `viewed` has to survive it.
            self.conn.execute(
                "UPDATE marks SET state = '', note = NULL, updated_at = ? "
                "WHERE listing_id = ?",
                (time.time(), listing_id),
            )
            self.conn.execute(
                "DELETE FROM marks WHERE listing_id = ? AND viewed = 0", (listing_id,)
            )
        else:
            self.conn.execute(
                "INSERT INTO marks (listing_id, state, note, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(listing_id) DO UPDATE SET state=excluded.state, "
                "note=COALESCE(excluded.note, marks.note), updated_at=excluded.updated_at",
                (listing_id, state, note, time.time()),
            )
        self.conn.commit()

    def set_viewed(self, listing_id: int, viewed: bool) -> None:
        """Independent of star/hide: both live on the same row."""
        self.conn.execute(
            "INSERT INTO marks (listing_id, state, note, viewed, updated_at) "
            "VALUES (?,'',NULL,?,?) "
            "ON CONFLICT(listing_id) DO UPDATE SET viewed=excluded.viewed, "
            "updated_at=excluded.updated_at",
            (listing_id, int(viewed), time.time()),
        )
        self.conn.execute(
            "DELETE FROM marks WHERE listing_id = ? AND viewed = 0 AND state = '' "
            "AND (note IS NULL OR note = '')",
            (listing_id,),
        )
        self.conn.commit()

    def marks_map(self) -> dict[int, dict[str, Any]]:
        return {
            r["listing_id"]: {
                "state": r["state"] or None,
                "note": r["note"],
                "viewed": bool(r["viewed"]),
            }
            for r in self.conn.execute("SELECT * FROM marks")
        }

    # ---------------------------------------------------------------- admin

    def stats(self) -> dict[str, Any]:
        def count(table: str) -> int:
            return self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

        oldest = self.conn.execute("SELECT MIN(fetched_at) m FROM http_cache").fetchone()["m"]
        return {
            "listings": count("listings"),
            "active_listings": self.conn.execute(
                "SELECT COUNT(*) c FROM listings WHERE removed_at IS NULL"
            ).fetchone()["c"],
            "details": count("details"),
            "routes": count("routes"),
            "http_cache": count("http_cache"),
            "marks": count("marks"),
            "db_bytes": Path(self.path).stat().st_size if Path(self.path).exists() else 0,
            "oldest_http_entry": oldest,
        }

    def purge(self, kind: str = "http", older_than_s: float | None = None) -> int:
        cutoff = time.time() - older_than_s if older_than_s else time.time() + 1
        tables = {"http": "http_cache", "routes": "routes", "details": "details"}
        if kind == "all":
            total = 0
            for t in tables.values():
                total += self.conn.execute(
                    f"DELETE FROM {t} WHERE fetched_at < ?", (cutoff,)
                ).rowcount
            self.conn.commit()
            return total
        table = tables[kind]
        n = self.conn.execute(f"DELETE FROM {table} WHERE fetched_at < ?", (cutoff,)).rowcount
        self.conn.commit()
        return n
