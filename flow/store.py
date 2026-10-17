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
from flow.learn import has_strong_shape

log = logging.getLogger(__name__)

AUTO_DICT_CAP = 400
# A term with a distinctive shape (n8n, LangGraph, API) is promoted the first
# time it is seen: it is unmistakably a product or technical name, so waiting
# only delays a correction the user wants.
PROMOTE_AT = 1
# An ordinary-looking word needs to be heard twice. Whisper mis-hearing a word
# once is enough to produce a plausible pseudo-word that the cleanup LLM
# reports as a new term and that filter_candidates cannot reject — "not a
# common dictionary word" is precisely its criterion, and the only word list
# macOS ships is English, so every German noun looks novel to it. Requiring a
# second sighting is what keeps a single mis-transcription out of the
# dictionary; see flow/learn.py.
PROMOTE_AT_PLAIN = 2


def _restrict_permissions(db_path) -> None:
    """Make the store owner-only. It holds every transcript ever dictated, and
    SQLite creates the database (and its -wal/-shm siblings) 0644 by default,
    i.e. readable by any other account on the Mac."""
    for path in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        try:
            path.chmod(0o600)
        except OSError:
            pass  # not created yet, or not ours — best effort


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
        _restrict_permissions(self.db_path)
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
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(history)")}
        if "kind" not in cols:
            # Existing rows are all dictations — the DEFAULT backfills them.
            self._conn.execute(
                "ALTER TABLE history ADD COLUMN kind TEXT DEFAULT 'dictation'"
            )
        if self._conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Until version 1 the app was the process's short name, which is
            # "Code" for VS Code, and was looked up after processing, when
            # our own dashboard could be frontmost and take the credit.
            self._conn.execute(
                "UPDATE history SET app = 'Visual Studio Code' WHERE app = 'Code'"
            )
            self._conn.execute(
                "UPDATE history SET app = NULL WHERE app = 'Bomboclat Flow'"
            )
            self._conn.execute("PRAGMA user_version = 1")
        self._conn.commit()

    # -- history ----------------------------------------------------------

    def add_dictation(self, raw, text, duration_s, app, corrected_words, dict_fixes,
                     kind="dictation") -> int:
        words = len(text.split()) if text else 0
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history "
                "(ts, raw, text, pinned, app, words, duration_s, corrected_words, dict_fixes, kind) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                (_utcnow(), raw, text, app, words, duration_s, corrected_words, dict_fixes, kind),
            )
            self._conn.commit()
            return cur.lastrowid

    def history(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, raw, text, pinned, app, words, duration_s, kind "
                "FROM history ORDER BY id DESC"
            ).fetchall()
        return [
            {
                "id": r["id"], "ts": r["ts"], "raw": r["raw"], "text": r["text"],
                "pinned": bool(r["pinned"]), "app": r["app"],
                "words": r["words"], "duration_s": r["duration_s"],
                "kind": r["kind"] or "dictation",
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
        """Words ordered by usage (most-used first), for prompt injection —
        biasing STT and the cleanup LLM towards the terms that matter most."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT word FROM dictionary ORDER BY uses DESC, added_ts DESC"
            ).fetchall()
        return [r["word"] for r in rows]

    def stt_bias_words(self) -> list[str]:
        """Words safe to inject into Whisper's initial_prompt.

        Auto-learned terms are held back until they have been seen again in a
        later dictation (uses > 0). Biasing the decoder towards a term the
        model produced only once is self-reinforcing: if that sighting was a
        mis-transcription, the prompt makes the same mistake more likely next
        time, and bump_uses then counts the repeat as confirmation. Routing
        new terms through the cleanup LLM first — which sees the surrounding
        text and is told not to insert terms that were not spoken — breaks
        that loop.

        Manually added words are the user's explicit choice and bias from the
        moment they are added.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT word FROM dictionary WHERE auto = 0 OR uses > 0 "
                "ORDER BY uses DESC, added_ts DESC"
            ).fetchall()
        return [r["word"] for r in rows]

    def add_word(self, word: str, auto: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO dictionary (word, auto, added_ts, uses) VALUES (?, ?, ?, 0)",
                (word, int(auto), _utcnow()),
            )
            self._conn.commit()

    def bump_uses(self, words: list[str]) -> None:
        """Increment the usage counter of words that appeared in a dictation.
        Feeds the usage-ordering of dictionary_words() (prompt biasing) and
        the least-used eviction in _make_room_for_auto_entry(). The IN
        comparison inherits the column's COLLATE NOCASE, so matching is
        case-insensitive like the primary key itself. Each term counts once
        per dictation regardless of how many times it occurs in the text —
        callers pass a de-duplicated list of terms present, not per-occurrence
        counts."""
        if not words:
            return
        placeholders = ", ".join("?" for _ in words)
        with self._lock:
            self._conn.execute(
                f"UPDATE dictionary SET uses = uses + 1 "
                f"WHERE word IN ({placeholders})",
                words,
            )
            self._conn.commit()

    def delete_word(self, word: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM dictionary WHERE word = ?", (word,))
            self._conn.execute("DELETE FROM staging WHERE term = ?", (word,))
            self._conn.commit()

    def record_candidates(self, terms: list[str]) -> list[str]:
        """Track repeated unrecognized terms; auto-promote to the dictionary
        once a term has been seen often enough — immediately for a term with a
        distinctive shape, on the second sighting otherwise."""
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
                threshold = (
                    PROMOTE_AT if has_strong_shape(term) else PROMOTE_AT_PLAIN
                )
                if count >= threshold:
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
                "SELECT ts, words, duration_s, app, corrected_words, dict_fixes FROM history "
                "WHERE COALESCE(kind, 'dictation') = 'dictation'"
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
            if r["app"]:
                app_counts[r["app"]] += 1
            daily_words[local_date] += r["words"] or 0
            days_with_data.add(local_date)

        if prev_month_words > 0:
            month_trend_pct = round(
                (cur_month_words - prev_month_words) / prev_month_words * 100
            )
        else:
            month_trend_pct = 0

        attributed = sum(app_counts.values())
        apps = []
        for name, count in sorted(app_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]:
            pct = round(count / attributed * 100) if attributed else 0
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
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    # Fast path: reference read/assignment is GIL-atomic, so once _store is
    # set this never touches the lock.
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Store()
    return _store
