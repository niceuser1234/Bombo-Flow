import tempfile
import threading
from pathlib import Path

import flow.store as store_mod
from flow.store import Store, get_store


def test_get_store_is_thread_safe_singleton():
    """Concurrent first-callers of get_store() must all see the same instance.

    Without a lock around the check-then-act in get_store(), two threads
    racing on the initial `_store is None` check could each construct a
    Store() — two independent sqlite3 connections to the same db file.
    """
    store_mod._store = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "store.db"
            # Patch Store's default path indirectly by constructing with an
            # explicit db_path via a thin wrapper, since get_store() itself
            # takes no arguments — instead monkeypatch the Store class used
            # by get_store() to always use the temp path.
            orig_store_cls = store_mod.Store

            class _TempStore(orig_store_cls):
                def __init__(self):
                    super().__init__(db_path=db_path)

            store_mod.Store = _TempStore
            try:
                n_threads = 8
                barrier = threading.Barrier(n_threads)
                results = [None] * n_threads

                def worker(i):
                    barrier.wait()
                    results[i] = get_store()

                threads = [
                    threading.Thread(target=worker, args=(i,))
                    for i in range(n_threads)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                distinct = {id(r) for r in results}
                assert len(distinct) == 1
            finally:
                store_mod.Store = orig_store_cls
    finally:
        store_mod._store = None


def test_shaped_candidate_promoted_after_single_sighting():
    """A term whose shape marks it as a product or technical name (digits,
    camelCase, short acronym) lands in the dictionary — and starts helping the
    cleanup LLM — on the first sighting. Waiting would only delay a correction
    the user wants. Ordinary-looking words take the slower path; see
    test_plain_words_need_a_second_sighting."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(db_path=Path(tmp) / "store.db")

        promoted = store.record_candidates(["PyTorch"])

        assert promoted == ["PyTorch"]
        assert "PyTorch" in store.dictionary_words()


def test_dictionary_words_ordered_by_usage():
    """Ordering must come from the real usage path (bump_uses), not from
    hand-edited rows — every dictation containing a term bumps its counter."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(db_path=Path(tmp) / "store.db")
        store.add_word("Rare")
        store.add_word("Common")

        store.bump_uses(["Common"])
        store.bump_uses(["Common"])

        assert store.dictionary_words() == ["Common", "Rare"]


def test_shaped_terms_promote_on_first_sighting():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(db_path=Path(tmp) / "store.db")
        # digits, camelCase and a short all-caps acronym
        promoted = store.record_candidates(["n8n", "LangGraph", "API"])
        assert sorted(promoted) == ["API", "LangGraph", "n8n"]


def test_plain_words_need_a_second_sighting():
    """Whisper mis-hearing a word once produces a plausible pseudo-word that
    filter_candidates cannot reject — "not a common dictionary word" is its
    criterion, and macOS ships an English word list only. Corroboration is
    what keeps a single mis-transcription out of the dictionary."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(db_path=Path(tmp) / "store.db")

        assert store.record_candidates(["Besprechung"]) == []
        assert [w["word"] for w in store.dictionary()] == []

        assert store.record_candidates(["Besprechung"]) == ["Besprechung"]
        assert [w["word"] for w in store.dictionary()] == ["Besprechung"]


def test_stt_bias_holds_back_unconfirmed_auto_terms():
    """An auto-learned term must not bias the decoder until it has been seen
    again — otherwise a single mis-transcription makes itself more likely."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(db_path=Path(tmp) / "store.db")
        store.add_word("Handverlesen", auto=False)  # user's explicit choice
        store.record_candidates(["n8n"])            # auto, uses = 0

        assert store.dictionary_words() == ["Handverlesen", "n8n"] or set(
            store.dictionary_words()
        ) == {"Handverlesen", "n8n"}
        assert store.stt_bias_words() == ["Handverlesen"]

        # Seen again in a later dictation — now it may bias STT.
        store.bump_uses(["n8n"])
        assert store.stt_bias_words() == ["n8n", "Handverlesen"]


def test_database_is_not_world_readable():
    """The store holds every transcript ever dictated. SQLite creates it 0644
    by default, i.e. readable by any other account on this Mac."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "store.db"
        store = Store(db_path=db_path)
        store.add_word("Alpha")  # force a write so the WAL file exists

        assert db_path.stat().st_mode & 0o077 == 0
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.exists():
            assert wal.stat().st_mode & 0o077 == 0


def test_bump_uses_is_case_insensitive_and_ignores_empty():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(db_path=Path(tmp) / "store.db")
        store.add_word("Alpha")
        store.add_word("Kubernetes")

        store.bump_uses([])  # no-op, must not raise
        store.bump_uses(["kubernetes"])  # NOCASE primary key must match

        assert store.dictionary_words() == ["Kubernetes", "Alpha"]
