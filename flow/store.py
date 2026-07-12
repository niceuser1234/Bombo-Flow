"""SQLite persistence for dictation history, the user dictionary, and insights.

Single connection shared across the worker thread (writes dictations as they
finish) and the main thread (menu bar UI reads/writes dictionary + history),
so every operation is guarded by one lock rather than using a connection
pool. WAL mode keeps reads from blocking on the occasional write.

All timestamps are stored as ISO-8601 UTC strings (e.g. "2026-07-11T14:03:22").
Day-grouping for history/insights converts to local time first since that's
what a user means by "today".
"""

import logging
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from flow.config import APP_SUPPORT

log = logging.getLogger(__name__)

AUTO_DICT_CAP = 400
PROMOTE_AT = 2


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _to_local_date(iso_utc: str) -> str:
    """Parse a stored UTC ISO timestamp and return its local calendar date."""
    ts = datetime.fromisoformat(iso_utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().date().isoformat()


class Store:
    def __init__(self, db_path=None):
        self.db_path = db_path or (APP_SUPPORT / "store.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema()

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                raw TEXT,
                text TEXT,
                pinned INTEGER DEFAULT 0,
                app TEXT,
                words INTEGER,
                duration_s REAL,
                corrected_words INTEGER DEFAULT 0,
                dict_fixes INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS dictionary (
                word TEXT PRIMARY KEY COLLATE NOCASE,
                auto INTEGER DEFAULT 0,
                added_ts TEXT,
                uses INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS staging (
                term TEXT PRIMARY KEY COLLATE NOCASE,
                count INTEGER DEFAULT 1,
                first_seen TEXT
            );
        """)
        self._conn.commit()

    # -- history ----------------------------------------------------------

    def add_dictation(self, raw, text, duration_s, app, corrected_words, dict_fixes) -> int:
        words = len(text.split()) if text else 0
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history "
                "(ts, raw, text, pinned, app, words, duration_s, corrected_words, dict_fixes) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (_utcnow(), raw, text, app, words, duration_s, corrected_words, dict_fixes),
            )
            self._conn.commit()
            return cur.lastrowid

    def history(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, raw, text, pinned, app, words, duration_s "
                "FROM history ORDER BY id DESC"
            ).fetchall()
        return [
            {
                "id": r["id"], "ts": r["ts"], "raw": r["raw"], "text": r["text"],
                "pinned": bool(r["pinned"]), "app": r["app"],
                "words": r["words"], "duration_s": r["duration_s"],
            }
            for r in rows
        ]

    def set_pinned(self, entry_id: int, pinned: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE history SET pinned = ? WHERE id = ?", (int(pinned), entry_id)
            )
            self._conn.commit()

    def delete_entry(self, entry_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history WHERE id = ?", (entry_id,))
            self._conn.commit()

    def get_text(self, entry_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM history WHERE id = ?", (entry_id,)
            ).fetchone()
        return row["text"] if row else None

    def purge_old(self, days: int = 14) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM history WHERE pinned = 0 AND ts < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount

    # -- dictionary ---------------------------------------------------------

    def dictionary(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT word, auto, added_ts FROM dictionary ORDER BY word COLLATE NOCASE"
            ).fetchall()
        return [{"word": r["word"], "auto": bool(r["auto"]), "added_ts": r["added_ts"]} for r in rows]

    def dictionary_words(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT word FROM dictionary ORDER BY word COLLATE NOCASE"
            ).fetchall()
        return [r["word"] for r in rows]

    def add_word(self, word: str, auto: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO dictionary (word, auto, added_ts, uses) VALUES (?, ?, ?, 0)",
                (word, int(auto), _utcnow()),
            )
            self._conn.commit()

    def delete_word(self, word: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM dictionary WHERE word = ?", (word,))
            self._conn.execute("DELETE FROM staging WHERE term = ?", (word,))
            self._conn.commit()

    def record_candidates(self, terms: list[str]) -> list[str]:
        """Track repeated unrecognized terms; auto-promote to the dictionary
        once a term has been seen PROMOTE_AT times."""
        promoted = []
        with self._lock:
            for term in terms:
                existing = self._conn.execute(
                    "SELECT 1 FROM dictionary WHERE word = ?", (term,)
                ).fetchone()
                if existing:
                    continue
                row = self._conn.execute(
                    "SELECT count FROM staging WHERE term = ?", (term,)
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO staging (term, count, first_seen) VALUES (?, 1, ?)",
                        (term, _utcnow()),
                    )
                    count = 1
                else:
                    count = row["count"] + 1
                    self._conn.execute(
                        "UPDATE staging SET count = ? WHERE term = ?", (count, term)
                    )
                if count >= PROMOTE_AT:
                    self._make_room_for_auto_entry()
                    self._conn.execute(
                        "INSERT OR IGNORE INTO dictionary (word, auto, added_ts, uses) "
                        "VALUES (?, 1, ?, 0)",
                        (term, _utcnow()),
                    )
                    self._conn.execute("DELETE FROM staging WHERE term = ?", (term,))
                    promoted.append(term)
            self._conn.commit()
        return promoted

    def _make_room_for_auto_entry(self):
        """Evict the least-used auto entry if the auto dictionary is at cap.
        Must be called with the lock already held, inside a transaction."""
        count = self._conn.execute(
            "SELECT COUNT(*) AS n FROM dictionary WHERE auto = 1"
        ).fetchone()["n"]
        if count < AUTO_DICT_CAP:
            return
        victim = self._conn.execute(
            "SELECT word FROM dictionary WHERE auto = 1 "
            "ORDER BY uses ASC, added_ts ASC LIMIT 1"
        ).fetchone()
        if victim:
            self._conn.execute("DELETE FROM dictionary WHERE word = ?", (victim["word"],))

    # -- insights -------------------------------------------------------

    def insights(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, words, duration_s, app, corrected_words, dict_fixes FROM history"
            ).fetchall()

        total_words = sum(r["words"] or 0 for r in rows)
        total_duration_s = sum(r["duration_s"] or 0 for r in rows)
        wpm = round(total_words / (total_duration_s / 60)) if total_duration_s > 0 else 0

        fixes_total = sum((r["corrected_words"] or 0) + (r["dict_fixes"] or 0) for r in rows)
        words_corrected = sum(r["corrected_words"] or 0 for r in rows)
        dict_fixes = sum(r["dict_fixes"] or 0 for r in rows)

        today_local = datetime.now().astimezone().date()
        cur_month = (today_local.year, today_local.month)
        prev_month_date = (today_local.replace(day=1) - timedelta(days=1))
        prev_month = (prev_month_date.year, prev_month_date.month)
        cur_month_words = 0
        prev_month_words = 0

        app_counts = defaultdict(int)
        daily_words = defaultdict(int)
        days_with_data = set()

        for r in rows:
            local_date = _to_local_date(r["ts"])
            y, m = int(local_date[0:4]), int(local_date[5:7])
            if (y, m) == cur_month:
                cur_month_words += r["words"] or 0
            elif (y, m) == prev_month:
                prev_month_words += r["words"] or 0
            app_name = r["app"] or "Unknown"
            app_counts[app_name] += 1
            daily_words[local_date] += r["words"] or 0
            days_with_data.add(local_date)

        if prev_month_words > 0:
            month_trend_pct = round(
                (cur_month_words - prev_month_words) / prev_month_words * 100
            )
        else:
            month_trend_pct = 0

        total_dictations = len(rows)
        apps = []
        for name, count in sorted(app_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]:
            pct = round(count / total_dictations * 100) if total_dictations else 0
            apps.append({"name": name, "count": count, "pct": pct})

        cutoff_date = today_local - timedelta(days=150)
        daily = [
            {"date": d, "words": w}
            for d, w in sorted(daily_words.items())
            if datetime.fromisoformat(d).date() >= cutoff_date
        ]

        streak = self._compute_streak(days_with_data, today_local)

        return {
            "wpm": wpm,
            "totalWords": total_words,
            "monthTrendPct": month_trend_pct,
            "fixesTotal": fixes_total,
            "wordsCorrected": words_corrected,
            "dictFixes": dict_fixes,
            "apps": apps,
            "daily": daily,
            "streak": streak,
        }

    @staticmethod
    def _compute_streak(days_with_data: set, today_local) -> dict:
        if not days_with_data:
            return {"current": 0, "longest": 0}

        # current streak: walk backward from today (today or yesterday must
        # be present for the streak to still be "alive").
        current = 0
        if today_local.isoformat() in days_with_data:
            cursor = today_local
        elif (today_local - timedelta(days=1)).isoformat() in days_with_data:
            cursor = today_local - timedelta(days=1)
        else:
            cursor = None
        while cursor is not None and cursor.isoformat() in days_with_data:
            current += 1
            cursor -= timedelta(days=1)

        # longest streak: scan the sorted set for consecutive runs.
        sorted_days = sorted(datetime.fromisoformat(d).date() for d in days_with_data)
        longest = 1
        run = 1
        for prev, cur in zip(sorted_days, sorted_days[1:]):
            if (cur - prev).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        longest = max(longest, current)

        return {"current": current, "longest": longest}


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
