from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from statistics import median
from typing import Iterable


@dataclass(frozen=True)
class Article:
    id: int
    source_name: str
    original_url: str
    original_title: str
    rewritten_post: str | None
    image_url: str | None
    status: str
    created_at: str
    published_at: str | None
    event_id: int | None = None
    importance_score: int = 0
    decision: str = "REVIEW"
    tiktok_status: str | None = None
    tiktok_fail_reason: str | None = None


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name TEXT NOT NULL,
                    original_url TEXT NOT NULL UNIQUE,
                    original_title TEXT NOT NULL,
                    rewritten_post TEXT,
                    image_url TEXT,
                    status TEXT NOT NULL CHECK(status IN ('draft', 'published', 'skipped')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    published_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status)")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
            if "event_id" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN event_id INTEGER")
            if "importance_score" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN importance_score INTEGER NOT NULL DEFAULT 0")
            if "decision" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN decision TEXT NOT NULL DEFAULT 'REVIEW'")
            if "tiktok_status" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN tiktok_status TEXT")
            if "tiktok_fail_reason" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN tiktok_fail_reason TEXT")
            if "telegram_publish_mode" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN telegram_publish_mode TEXT NOT NULL DEFAULT 'manual'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_event ON articles(event_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_title TEXT NOT NULL,
                    summary TEXT,
                    category TEXT NOT NULL,
                    importance_score INTEGER NOT NULL DEFAULT 0,
                    source_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    best_article_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'review',
                    event_version INTEGER NOT NULL DEFAULT 1,
                    last_published_summary TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ,score_breakdown TEXT
                )
                """
            )
            event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
            if "score_breakdown" not in event_columns:
                conn.execute("ALTER TABLE events ADD COLUMN score_breakdown TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tiktok_publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    event_id INTEGER,
                    publish_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL,
                    image_url TEXT NOT NULL,
                    privacy_level TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fail_reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    published_at TEXT
                )
                """
            )
            tiktok_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tiktok_publications)")}
            if "title" not in tiktok_columns:
                conn.execute("ALTER TABLE tiktok_publications ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            if "mode" not in tiktok_columns:
                conn.execute("ALTER TABLE tiktok_publications ADD COLUMN mode TEXT NOT NULL DEFAULT 'manual'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tiktok_publications_article ON tiktok_publications(article_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tiktok_publications_publish_id ON tiktok_publications(publish_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    event_id INTEGER,
                    importance_score INTEGER NOT NULL DEFAULT 0,
                    telegram_status TEXT NOT NULL,
                    tiktok_status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_created ON automation_runs(created_at DESC)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS publication_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    article_id INTEGER,
                    event_id INTEGER,
                    publish_id TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            publication_columns = {row["name"] for row in conn.execute("PRAGMA table_info(publication_events)")}
            if "publish_id" not in publication_columns:
                conn.execute("ALTER TABLE publication_events ADD COLUMN publish_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_publication_events_created ON publication_events(created_at DESC)")

    def get_setting(self, name: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, name: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(name, value) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )

    def has_article(self, url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM articles WHERE original_url = ?", (url,)).fetchone()
            return row is not None

    def recent_event_candidates(self, since_hours: int, limit: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.event_id, a.original_title, a.rewritten_post, a.created_at,
                       e.canonical_title, e.summary, e.category, e.importance_score, e.status
                FROM articles a LEFT JOIN events e ON e.id = a.event_id
                WHERE a.created_at >= datetime('now', ?)
                ORDER BY a.created_at DESC LIMIT ?
                """,
                (f"-{max(since_hours, 1)} hours", max(limit, 1)),
            ).fetchall()
            return [dict(row) for row in rows]

    def create_event(self, title: str, summary: str, category: str, score: int, article_id: int, score_breakdown: dict | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO events
                (canonical_title, summary, category, importance_score, source_count, best_article_id)
                VALUES (?, ?, ?, ?, 1, ?)""",
                (title, summary, category, score, article_id),
            )
            event_id = int(cur.lastrowid)
            conn.execute("UPDATE articles SET event_id = ?, importance_score = ? WHERE id = ?", (event_id, score, article_id))
            if score_breakdown is not None:
                conn.execute("UPDATE events SET score_breakdown = ? WHERE id = ?", (json.dumps(score_breakdown, ensure_ascii=False), event_id))
            return event_id

    def set_event_score_breakdown(self, event_id: int, breakdown: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE events SET score_breakdown = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(breakdown, ensure_ascii=False), event_id),
            )

    def attach_to_event(self, article_id: int, event_id: int, score: int, best_article_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE articles SET event_id = ?, importance_score = ? WHERE id = ?", (event_id, score, article_id))
            conn.execute(
                """UPDATE events SET source_count=(SELECT COUNT(DISTINCT source_name) FROM articles WHERE event_id = ?),
                importance_score=?, last_seen_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP,
                best_article_id=COALESCE(?, best_article_id) WHERE id=?""",
                (event_id, score, best_article_id, event_id),
            )

    def get_event(self, event_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return dict(row) if row else None

    def event_articles(self, event_id: int) -> list[Article]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM articles WHERE event_id = ? ORDER BY created_at DESC", (event_id,)).fetchall()
            return [_to_article(row) for row in rows]

    def event_source_count(self, event_id: int | None) -> int:
        if not event_id:
            return 1
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT source_name) FROM articles WHERE event_id = ?", (event_id,)).fetchone()
            return int(row[0] or 1)

    def update_event(self, event_id: int, **fields: object) -> None:
        allowed = {"canonical_title", "summary", "category", "importance_score", "status", "last_published_summary", "event_version", "best_article_id"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = "CURRENT_TIMESTAMP"
        assignments = ", ".join(f"{key} = ?" if value != "CURRENT_TIMESTAMP" else f"{key} = CURRENT_TIMESTAMP" for key, value in values.items())
        params = [value for value in values.values() if value != "CURRENT_TIMESTAMP"] + [event_id]
        with self.connect() as conn:
            conn.execute(f"UPDATE events SET {assignments} WHERE id = ?", params)

    def create_draft(
        self,
        source_name: str,
        original_url: str,
        original_title: str,
        rewritten_post: str,
        image_url: str | None,
        importance_score: int = 0,
        decision: str = "REVIEW",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO articles (
                    source_name, original_url, original_title, rewritten_post, image_url, importance_score, decision, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')
                """,
                (source_name, original_url, original_title, rewritten_post, image_url, importance_score, decision),
            )
            return int(cursor.lastrowid)

    def update_draft(self, article_id: int, rewritten_post: str, image_url: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE articles
                SET rewritten_post = ?, image_url = ?, status = 'draft'
                WHERE id = ?
                """,
                (rewritten_post, image_url, article_id),
            )

    def set_article_ranking(self, article_id: int, score: int, decision: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE articles SET importance_score = ?, decision = ? WHERE id = ?", (score, decision, article_id))

    def set_tiktok_status(self, article_id: int, status: str | None, fail_reason: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE articles SET tiktok_status = ?, tiktok_fail_reason = ? WHERE id = ?",
                (status, fail_reason, article_id),
            )

    def set_status(self, article_id: int, status: str, mode: str | None = None) -> None:
        published_at = "CURRENT_TIMESTAMP" if status == "published" else "NULL"
        with self.connect() as conn:
            if mode is None:
                conn.execute(f"UPDATE articles SET status = ?, published_at = {published_at} WHERE id = ?", (status, article_id))
            else:
                conn.execute(f"UPDATE articles SET status = ?, telegram_publish_mode = ?, published_at = {published_at} WHERE id = ?", (status, mode, article_id))

    def get_article(self, article_id: int) -> Article | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
            return _to_article(row) if row else None

    def list_by_status(self, status: str, limit: int = 10) -> list[Article]:
        with self.connect() as conn:
            rows: Iterable[sqlite3.Row] = conn.execute(
                """
                SELECT * FROM articles
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
            return [_to_article(row) for row in rows]

    def list_recent(self, limit: int = 50) -> list[Article]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM articles ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_to_article(row) for row in rows]

    def counts_by_status(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM articles GROUP BY status").fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

    def get_tiktok_publication(self, article_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tiktok_publications WHERE article_id = ? ORDER BY id DESC LIMIT 1",
                (article_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_tiktok_publication(
        self,
        article_id: int,
        event_id: int | None,
        publish_id: str,
        title: str,
        caption: str,
        image_url: str,
        privacy_level: str,
        status: str,
        fail_reason: str = "",
        mode: str = "manual",
    ) -> int:
        published_at = "CURRENT_TIMESTAMP" if status.upper() in {"PUBLISHED", "SUCCESS"} else "NULL"
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO tiktok_publications
                    (article_id, event_id, publish_id, title, caption, image_url, privacy_level, status, fail_reason, mode, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {published_at})
                """,
                (article_id, event_id, publish_id, title, caption, image_url, privacy_level, status, fail_reason, mode),
            )
            return int(cursor.lastrowid)

    def has_successful_tiktok_publication(self, article_id: int, event_id: int | None = None) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM tiktok_publications
                WHERE status IN ('PUBLISHED', 'SUCCESS')
                  AND (article_id = ? OR (? IS NOT NULL AND event_id = ?))
                LIMIT 1
                """,
                (article_id, event_id, event_id),
            ).fetchone()
            return row is not None

    def record_automation_run(
        self,
        article_id: int,
        event_id: int | None,
        importance_score: int,
        telegram_status: str,
        tiktok_status: str,
        reason: str = "",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO automation_runs
                    (article_id, event_id, importance_score, telegram_status, tiktok_status, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (article_id, event_id, importance_score, telegram_status, tiktok_status, reason),
            )
            return int(cursor.lastrowid)

    def count_recent_telegram_auto_published(self, hours: int = 1) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM publication_events
                   WHERE platform = 'telegram' AND mode = 'auto' AND status = 'PUBLISHED'
                     AND created_at >= datetime('now', ?)""",
                (f"-{max(hours, 1)} hours",),
            ).fetchone()
            return int(row[0] or 0)

    def list_recent_automation_runs(self, limit: int = 10) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, a.original_title, a.image_url, a.decision, e.category, e.source_count
                FROM automation_runs r
                LEFT JOIN articles a ON a.id = r.article_id
                LEFT JOIN events e ON e.id = r.event_id
                ORDER BY r.id DESC LIMIT ?
                """,
                (max(limit, 1),),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_publication_event(
        self,
        platform: str,
        article_id: int | None,
        event_id: int | None,
        mode: str,
        status: str,
        reason: str = "",
        publish_id: str = "",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO publication_events(platform, article_id, event_id, publish_id, mode, status, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, article_id, event_id, publish_id[:200], mode, status.upper(), reason[:200]),
            )
            return int(cursor.lastrowid)

    def dashboard_24h(self) -> dict:
        with self.connect() as conn:
            article_count = int(conn.execute("SELECT COUNT(*) FROM articles WHERE created_at >= datetime('now', '-24 hours')").fetchone()[0])
            event_rows = conn.execute(
                "SELECT importance_score FROM events WHERE created_at >= datetime('now', '-24 hours') ORDER BY created_at"
            ).fetchall()
            event_scores = [int(row[0] or 0) for row in event_rows]
            pub_rows = conn.execute(
                """
                SELECT platform, mode, status, publish_id, COUNT(*) AS count
                FROM publication_events
                WHERE created_at >= datetime('now', '-24 hours')
                GROUP BY platform, mode, status, publish_id
                """
            ).fetchall()
            tiktok_publication_rows = conn.execute(
                """
                SELECT mode, status, COUNT(*) AS count
                FROM tiktok_publications
                WHERE created_at >= datetime('now', '-24 hours')
                GROUP BY mode, status
                """
            ).fetchall()
        if event_scores:
            ordered = sorted(event_scores)
            average = round(sum(ordered) / len(ordered), 2)
            med = round(float(median(ordered)), 2)
            p50 = _percentile(ordered, 50)
            p75 = _percentile(ordered, 75)
            p90 = _percentile(ordered, 90)
            maximum = max(ordered)
        else:
            average = med = p50 = p75 = p90 = maximum = 0
        distribution = {
            "0-39": sum(score < 40 for score in event_scores),
            "40-49": sum(40 <= score < 50 for score in event_scores),
            "50-59": sum(50 <= score < 60 for score in event_scores),
            "60-69": sum(60 <= score < 70 for score in event_scores),
            "70-79": sum(70 <= score < 80 for score in event_scores),
            "80-89": sum(80 <= score < 90 for score in event_scores),
            "90-100": sum(score >= 90 for score in event_scores),
        }
        stats = {
            "articles_found": article_count,
            "events_created": len(event_scores),
            "average_score": average,
            "median_score": med,
            "max_score": maximum,
            "count_50": sum(score >= 50 for score in event_scores),
            "count_60": sum(score >= 60 for score in event_scores),
            "count_70": sum(score >= 70 for score in event_scores),
            "count_80": sum(score >= 80 for score in event_scores),
            "distribution": distribution,
            "p50": p50,
            "p75": p75,
            "p90": p90,
            "max": maximum,
            "telegram": {"auto_published": 0, "manual_published": 0, "errors": 0},
            "tiktok": {"auto_published": 0, "manual_published": 0, "skipped_no_image": 0, "failed": 0, "processing": 0},
        }
        for mode, status, count in tiktok_publication_rows:
            count = int(count)
            status = str(status).upper()
            key = "auto_published" if str(mode).lower() == "auto" else "manual_published"
            if status in {"PUBLISHED", "SUCCESS"}:
                stats["tiktok"][key] += count
            elif status == "SKIPPED_NO_IMAGE":
                stats["tiktok"]["skipped_no_image"] += count
            elif status == "FAILED":
                stats["tiktok"]["failed"] += count
            elif status in {"PROCESSING", "PROCESSING_DOWNLOAD", "IN_PROGRESS", "INITIATED", "UPLOAD_COMPLETE"}:
                stats["tiktok"]["processing"] += count
        for platform, mode, status, publish_id, count in pub_rows:
            count = int(count)
            status = str(status).upper()
            mode = str(mode).lower()
            if platform == "telegram":
                if status == "PUBLISHED":
                    key = "auto_published" if mode == "auto" else "manual_published"
                    stats["telegram"][key] += count
                elif status == "FAILED":
                    stats["telegram"]["errors"] += count
            elif platform == "tiktok":
                if publish_id:
                    continue
                if status == "SKIPPED_NO_IMAGE":
                    stats["tiktok"]["skipped_no_image"] += count
                elif status == "FAILED":
                    stats["tiktok"]["failed"] += count
                elif status in {"PROCESSING", "PROCESSING_DOWNLOAD", "IN_PROGRESS", "INITIATED", "UPLOAD_COMPLETE"}:
                    stats["tiktok"]["processing"] += count
        return stats

    def update_tiktok_publication(self, publication_id: int, status: str, fail_reason: str = "") -> None:
        published_at = "CURRENT_TIMESTAMP" if status.upper() in {"PUBLISHED", "SUCCESS"} else "NULL"
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE tiktok_publications
                SET status = ?, fail_reason = ?, updated_at = CURRENT_TIMESTAMP, published_at = {published_at}
                WHERE id = ?
                """,
                (status, fail_reason, publication_id),
            )

    def update_publication_event_status(self, publish_id: str, status: str, reason: str = "") -> None:
        """Keep the matching audit event in sync after a TikTok status refresh."""
        if not publish_id:
            return
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE publication_events
                SET status = ?, reason = ?
                WHERE id = (
                    SELECT id FROM publication_events
                    WHERE platform = 'tiktok' AND publish_id = ?
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (status.upper(), reason[:200], publish_id),
            )


def _to_article(row: sqlite3.Row) -> Article:
    return Article(
        id=int(row["id"]),
        source_name=str(row["source_name"]),
        original_url=str(row["original_url"]),
        original_title=str(row["original_title"]),
        rewritten_post=row["rewritten_post"],
        image_url=row["image_url"],
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        published_at=row["published_at"],
        event_id=row["event_id"] if "event_id" in row.keys() else None,
        importance_score=int(row["importance_score"] or 0) if "importance_score" in row.keys() else 0,
        decision=str(row["decision"] or "REVIEW") if "decision" in row.keys() else "REVIEW",
        tiktok_status=str(row["tiktok_status"] or "") if "tiktok_status" in row.keys() and row["tiktok_status"] else None,
        tiktok_fail_reason=str(row["tiktok_fail_reason"] or "") if "tiktok_fail_reason" in row.keys() and row["tiktok_fail_reason"] else None,
    )


def _percentile(values: list[int], percentile: int) -> float:
    if not values:
        return 0
    if len(values) == 1:
        return float(values[0])
    position = (len(values) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return round(values[lower] + (values[upper] - values[lower]) * fraction, 2)
