# Freeze fix, one-pass Whisper, hands-free and rewrite modes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the app from going deaf after a CoreAudio stall, cut ~0.9 s off every transcription, and add hands-free (double-tap fn) and rewrite (fn + Control) modes.

**Architecture:** The freeze fix bounds every CoreAudio call on the hotkey thread with a timeout and, once CoreAudio is wedged, finishes any rescued dictation and relaunches the process (the only cure that has ever worked). One-pass Whisper encodes the 30 s window once and reuses the features for language detection and all temperature fallbacks. The hotkey listener reports the Control flag (dictation vs. command); the pipeline owns the double-tap/hands-free state machine and the rewrite path (`flow/transform.py` + `Cleaner.rewrite`).

**Tech Stack:** Python 3.12, PyObjC (AppKit/Quartz), sounddevice/PortAudio, mlx-whisper 0.4.3, OpenRouter/Anthropic, SQLite, vanilla JS UI in WKWebView. Tests: `pytest` (unittest-style), run with `.venv/bin/python -m pytest -q`.

**Spec:** `docs/superpowers/specs/2026-10-15-handsfree-and-rewrite-design.md` (hands-free + rewrite). Freeze fix and one-pass Whisper were approved in chat; their requirements are written out in Tasks 1–2.

## Global Constraints

- Whisper stays the STT engine; never force a `language` (dictations mix German/English).
- Every CoreAudio call made from the hotkey-dispatch thread must be bounded by a timeout (`START_TIMEOUT_S = STOP_TIMEOUT_S = 2.0`).
- Never call `reset_portaudio()` while a stream may still be live (existing rule, see `flow/pipeline.py` comments).
- Double-tap window `DOUBLE_TAP_S = 0.4`; tap = press shorter than `cfg.min_duration_s` (0.3 s).
- `[audio] handsfree_max_s = 600` (new config key, default 600.0).
- Rewrite LLM timeout `REWRITE_TIMEOUT_S = 10.0`; `max_tokens = 4096`.
- Overlay texts verbatim: "What should I do with it?", "Rewriting…", "Select some text first", "Didn't catch that", "Rewrite needs an API key", "Rewrite failed".
- Menu bar icon for hands-free: `🔒` (state name `handsfree`).
- `history.kind` values: `'dictation'` (default) and `'rewrite'`; insights count only dictations.
- Run the full suite after every task: `.venv/bin/python -m pytest -q` (baseline: 167 passed).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Relaunch while a dictation is still being processed** — the rescued/pending dictation must be pasted before the process exits (sentinel ordering in the worker queue). Pinned in Task 1 (`test_relaunch_waits_for_queued_dictation`).
2. **A normal short tap (accidental fn press)** must still be discarded silently after the 0.4 s grace window and must not leave the recorder running. Pinned in Task 6 (`test_single_tap_is_discarded_after_grace`).
3. **Pressing Control while in hands-free** must not turn a long hands-free dictation into a rewrite. Pinned in Task 6 (`test_mode_change_ignored_in_handsfree`).
4. **Rewrite when the clipboard holds the user's own data** — the user's clipboard must be restored after grabbing the selection, including when nothing was selected. Pinned in Task 7 (`test_grab_selection_restores_clipboard_when_nothing_selected`).
5. **Very long hands-free dictation (> 30 s)** must still transcribe (windowed path), not crash the one-pass code. Pinned in Task 2 (`test_long_audio_uses_windowed_transcribe`).

---

### Task 1: Freeze fix — bounded start, rescued audio, auto-relaunch

**Why:** Log evidence (13 stalls since Aug): after `recorder.stop() did not return within 2.0s` not a single log line appears until the user restarts the app. The next `on_hold_start` blocks forever inside `sd.InputStream()`/`stream.start()` on the single hotkey-dispatch thread (holding `self._lock`), so every later press queues behind it. A wedged CoreAudio HAL does not recover in-process.

**Files:**
- Modify: `flow/lifecycle.py` (add `relaunch_command`, `relaunch`)
- Modify: `flow/audio.py` (add `Recorder.rescue`)
- Modify: `flow/pipeline.py` (`START_TIMEOUT_S`, `_start_recorder_safe`, `_stop_recorder_safe` returns `(audio, timed_out)`, `_request_relaunch`, worker sentinel)
- Test: `tests/test_lifecycle.py`, `tests/test_audio.py`, `tests/test_pipeline.py`

**Interfaces:**
- Produces: `lifecycle.relaunch(reason: str) -> NoReturn`; `lifecycle.relaunch_command(pid: int, executable: str, frozen: bool, project_root: Path) -> list[str]`; `Recorder.rescue() -> np.ndarray`; `Pipeline._stop_recorder_safe() -> tuple[np.ndarray, bool]`; `Pipeline._request_relaunch(reason: str) -> None`; `Pipeline._relaunch_pending: bool`; `Pipeline._relaunch: Callable[[str], None]` (defaults to `lifecycle.relaunch`). Queue items become `(captured, released_at, mode)` tuples — Task 1 introduces the 3-tuple with `mode="dictation"` so later tasks don't reshape it; `None` is the relaunch sentinel.

- [ ] **Step 1: Write failing tests for `relaunch_command` / `relaunch`** (append to `tests/test_lifecycle.py`)

```python
from pathlib import Path
from unittest import mock

from flow import lifecycle


def test_relaunch_command_frozen_opens_the_bundle_in_background():
    cmd = lifecycle.relaunch_command(
        pid=4242,
        executable="/Applications/Bomboclat Flow.app/Contents/MacOS/BomboclatFlow",
        frozen=True,
        project_root=Path("/unused"),
    )
    assert cmd[:3] == ["/bin/sh", "-c", lifecycle._WAIT_THEN_EXEC]
    assert cmd[3] == "4242"
    assert cmd[4:] == ["open", "/Applications/Bomboclat Flow.app", "--args", "--background"]


def test_relaunch_command_dev_runs_the_module():
    cmd = lifecycle.relaunch_command(
        pid=7, executable="/x/.venv/bin/python", frozen=False, project_root=Path("/proj"),
    )
    assert cmd[3] == "7"
    assert cmd[4:] == ["/x/.venv/bin/python", "-m", "flow", "--background"]


def test_relaunch_spawns_helper_then_exits():
    with mock.patch.object(lifecycle.subprocess, "Popen") as popen, \
         mock.patch.object(lifecycle.os, "_exit") as exit_, \
         mock.patch.object(lifecycle, "_dump_stacks"):
        lifecycle.relaunch("test")
    popen.assert_called_once()
    assert popen.call_args.kwargs["start_new_session"] is True
    exit_.assert_called_once_with(0)
```

- [ ] **Step 2: Run them — expect FAIL** (`AttributeError: module 'flow.lifecycle' has no attribute 'relaunch_command'`)

Run: `.venv/bin/python -m pytest tests/test_lifecycle.py -q`

- [ ] **Step 3: Implement in `flow/lifecycle.py`** (add `import subprocess` to the imports; place after `_dump_stacks`)

```python
# Waits for the old process ($0 = its pid) to exit — it holds the
# single-instance lock until then — and only then starts the new one.
_WAIT_THEN_EXEC = 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; exec "$@"'


def relaunch_command(pid: int, executable: str, frozen: bool, project_root: Path) -> list[str]:
    """Shell command that starts a fresh copy of the app once `pid` is gone.
    `--background` keeps the dashboard from popping up on a relaunch."""
    if frozen:
        bundle = str(Path(executable).resolve().parents[2])
        target = ["open", bundle, "--args", "--background"]
    else:
        target = [executable, "-m", "flow", "--background"]
    return ["/bin/sh", "-c", _WAIT_THEN_EXEC, str(pid), *target]


def relaunch(reason: str) -> None:
    """Replace this process with a fresh one.

    A CoreAudio HAL that wedged inside AudioDeviceStop never recovers within
    the process: every later stream open blocks forever. Restarting is the
    only cure, so do it for the user instead of leaving the hotkey dead.
    """
    from .config import IS_FROZEN, PROJECT_ROOT

    log.error("Relaunching the app: %s", reason)
    _dump_stacks()
    try:
        subprocess.Popen(
            relaunch_command(os.getpid(), sys.executable, IS_FROZEN, PROJECT_ROOT),
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        log.exception("Could not spawn the relaunch helper")
    # os._exit: an orderly shutdown would join threads stuck in CoreAudio.
    os._exit(0)
```

Check `flow/config.py` exports `PROJECT_ROOT` (it does — `flow/sound.py` imports it). Check `lifecycle.py` already imports `os`, `sys`, `Path`; add any missing.

- [ ] **Step 4: Run — expect PASS.** `.venv/bin/python -m pytest tests/test_lifecycle.py -q`

- [ ] **Step 5: Failing test for `Recorder.rescue`** (append to `tests/test_audio.py`)

```python
import numpy as np
from flow.audio import Recorder


def test_rescue_returns_captured_audio_without_touching_the_stream():
    rec = Recorder()
    rec._chunks = [np.ones((4, 1), dtype=np.float32), np.zeros((2, 1), dtype=np.float32)]
    out = rec.rescue()
    assert out.shape == (6,)
    assert out[:4].tolist() == [1.0] * 4
    assert rec._chunks, "rescue must not clear chunks — abandon() does that"


def test_rescue_of_empty_recorder_is_empty():
    assert len(Recorder().rescue()) == 0
```

- [ ] **Step 6: Run — expect FAIL**, then implement in `flow/audio.py` (inside `Recorder`, after `stop`):

```python
    def rescue(self) -> np.ndarray:
        """Audio captured so far, for a stream whose stop() wedged.

        Copies the chunk list first: the CoreAudio callback may still append
        to it from its own thread until abandon() flips _discard."""
        chunks = list(self._chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[:, 0]
```

Run: `.venv/bin/python -m pytest tests/test_audio.py -q` → PASS.

- [ ] **Step 7: Failing pipeline tests** (in `tests/test_pipeline.py`)

First extend `_build_pipeline()` with the new attributes (keep everything already there):

```python
    pipeline._relaunch_pending = False
    pipeline._relaunched = []
    pipeline._relaunch = lambda reason: pipeline._relaunched.append(reason)
```

Then add a test class:

```python
class _HangingRecorder(_Recorder):
    """start()/stop() block like a wedged CoreAudio HAL."""

    def __init__(self, device_name=None, hang_start=False, hang_stop=False):
        super().__init__(device_name)
        self._gate = threading.Event()
        self.hang_start = hang_start
        self.hang_stop = hang_stop
        self.chunks_audio = np.full(16000, 0.1, dtype=np.float32)

    def start(self):
        if self.hang_start:
            self._gate.wait(10)
        super().start()

    def stop(self):
        if self.hang_stop:
            self._gate.wait(10)
        return super().stop()

    def rescue(self):
        return self.chunks_audio


class FreezeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.p = _build_pipeline()
        self.p.engine = object()
        self.queued = []
        self.p._queue = type("_Q", (), {"put": lambda s, item: self.queued.append(item)})()

    def test_hanging_start_returns_and_requests_relaunch(self):
        self.p.recorder = _HangingRecorder(hang_start=True)
        with patch("flow.pipeline.START_TIMEOUT_S", 0.05), patch("flow.pipeline.sound"):
            t0 = time.monotonic()
            self.p.on_hold_start()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(self.p._relaunch_pending)
        self.assertEqual(self.queued, [None])  # sentinel only — nothing to rescue

    def test_hanging_stop_rescues_audio_then_relaunches(self):
        self.p.recorder = _HangingRecorder(hang_stop=True)
        self.p.recorder._recording = True
        self.p._press_time = time.monotonic() - 2.0  # a real 2 s dictation
        with patch("flow.pipeline.STOP_TIMEOUT_S", 0.05), patch("flow.pipeline.sound"):
            self.p._finish_recording()
        self.assertEqual(len(self.queued), 2)
        captured, _released_at, mode = self.queued[0]
        self.assertEqual(len(captured), 16000)
        self.assertEqual(mode, "dictation")
        self.assertIsNone(self.queued[1])
        self.assertTrue(self.p._relaunch_pending)

    def test_presses_are_ignored_once_relaunch_is_pending(self):
        self.p._relaunch_pending = True
        self.p.on_hold_start()
        self.assertFalse(self.p.recorder.recording)

    def test_relaunch_waits_for_queued_dictation(self):
        import queue as queue_mod

        p = _build_pipeline()
        p._queue = queue_mod.Queue()
        order = []
        p._process = lambda captured, released_at, mode="dictation": order.append("processed")
        p._relaunch = lambda reason: order.append("relaunch")
        p._load_model = lambda: True
        p._queue.put((np.zeros(10, dtype=np.float32), 0.0, "dictation"))
        p._queue.put(None)
        p._worker()  # returns after the sentinel
        self.assertEqual(order, ["processed", "relaunch"])
```

- [ ] **Step 8: Run — expect FAIL.** `.venv/bin/python -m pytest tests/test_pipeline.py -q`

- [ ] **Step 9: Implement in `flow/pipeline.py`**

Constants (next to `STOP_TIMEOUT_S`):

```python
START_TIMEOUT_S = 2.0  # max seconds to wait for recorder.start() before giving up.
```

Import: `from . import lifecycle`.

In `__init__` add:

```python
        # Set once CoreAudio has wedged: no new recordings, and the worker
        # relaunches the app after finishing whatever is still queued.
        self._relaunch_pending = False
        self._relaunch = lifecycle.relaunch
```

Generic helper (add above `_stop_recorder_safe`):

```python
    def _run_bounded(self, func, timeout, name):
        """Run func() on a throwaway thread. Returns (finished, result, error).
        A wedged CoreAudio call leaves its thread behind — daemon, harmless."""
        box: dict = {}
        done = threading.Event()

        def _run():
            try:
                box["result"] = func()
            except BaseException as e:  # re-raised by the caller
                box["error"] = e
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True, name=name).start()
        if not done.wait(timeout):
            return False, None, None
        return True, box.get("result"), box.get("error")
```

In `on_hold_start`, at the top inside the lock after the `engine is None` check:

```python
            if self._relaunch_pending:
                log.debug("on_hold_start skipped: relaunch pending")
                return
```

Replace the `try: self.recorder.start() except Exception: ...` block with:

```python
            finished, _, error = self._run_bounded(
                self.recorder.start, START_TIMEOUT_S, "recorder-start"
            )
            if not finished:
                log.warning(
                    "recorder.start() did not return within %.1fs — CoreAudio "
                    "is wedged", START_TIMEOUT_S,
                )
                self._request_relaunch("recorder.start() hung")
                return
            if error is not None:
                log.error(
                    "Could not start recording — resetting audio stack",
                    exc_info=error,
                )
                self._schedule_recovery()
                return
```

Replace `_stop_recorder_safe` with:

```python
    def _stop_recorder_safe(self) -> "tuple[np.ndarray, bool]":
        """Stop the recorder off the calling thread, with a hard timeout.

        CoreAudio can wedge inside AudioDeviceStop (its IO thread and the
        stopping thread deadlock over the HAL locks). On timeout the audio
        captured so far is rescued, the stream abandoned, and a fresh
        Recorder swapped in. Returns (audio, timed_out).
        """
        import numpy as np

        stuck = self.recorder
        finished, result, error = self._run_bounded(
            stuck.stop, STOP_TIMEOUT_S, "recorder-stop"
        )
        if not finished:
            log.warning(
                "recorder.stop() did not return within %.1fs — CoreAudio is "
                "stalled; rescuing the captured audio and abandoning the stream",
                STOP_TIMEOUT_S,
            )
            rescued = stuck.rescue()
            stuck.abandon()
            # The overlay's level_source closes over `self`, so it follows the
            # swap without further wiring.
            self.recorder = self._new_recorder()
            return rescued, True
        if error is not None:
            log.error("recorder.stop() raised", exc_info=error)
            return np.zeros(0, dtype=np.float32), False
        return result, False
```

Add:

```python
    def _request_relaunch(self, reason: str):
        """CoreAudio is wedged for the rest of this process. Stop taking new
        recordings; the worker relaunches once the queue ahead is drained."""
        if self._relaunch_pending:
            return
        self._relaunch_pending = True
        self.set_state("loading")
        self._queue.put(None)
        log.warning("Relaunch scheduled: %s", reason)
```

In `_finish_recording`, replace `captured = self._stop_recorder_safe()` and the whole `if len(captured) == 0:` block with:

```python
            captured, timed_out = self._stop_recorder_safe()
            if self.cfg.sounds:
                sound.play_stop()

            self.overlay.hide()
            if timed_out:
                if (duration >= self.cfg.min_duration_s and len(captured)
                        and not audio_mod.is_silent(captured)):
                    self.set_state("processing")
                    self._queue.put((captured, time.monotonic(), "dictation"))
                self._request_relaunch("recorder.stop() hung")
                return
            if duration < self.cfg.min_duration_s:
                log.info("Discarding %.2fs tap", duration)
                self.set_state("idle")
                return
            if len(captured) == 0:
                log.warning("recorder.stop() returned no audio")
                self._enter_error_then_idle()
                return
```

(keep the existing `is_silent` branch and change the final put to `self._queue.put((captured, time.monotonic(), "dictation"))`; delete the now-duplicated `play_stop`/`overlay.hide()`/duration lines that followed the old stop call).

Worker loop:

```python
        while True:
            item = self._queue.get()
            if item is None:
                self._relaunch("CoreAudio wedged")
                return
            captured, released_at, mode = item
            try:
                self._process(captured, released_at, mode)
            ...
```

and give `_process` the signature `def _process(self, captured, released_at, mode="dictation"):` (mode unused until Task 8).

Fix any existing pipeline tests that unpacked 2-tuples from the queue or asserted the old timed-out behaviour (`_enter_error_then_idle` after a stop timeout): the new contract is rescue → queue → sentinel. Do not weaken unrelated assertions.

- [ ] **Step 10: Run the full suite — expect PASS.** `.venv/bin/python -m pytest -q`

- [ ] **Step 11: Commit**

```bash
git add flow/lifecycle.py flow/audio.py flow/pipeline.py tests/
git commit -m "Relaunch instead of going deaf when CoreAudio wedges

After a stop() stall the next stream open blocked forever on the hotkey
thread, so fn did nothing until the user restarted the app. Bound start()
with a timeout too, rescue the audio captured before the stall, finish
that dictation, then relaunch the process.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: One-pass Whisper

**Why (measured on the M3):** `mlx_whisper.transcribe(language=None)` runs the encoder twice (language detection + decode), 0.93 s each over the padded 30 s window. Encoding once and passing the features to `model.decode()` (which accepts pre-encoded features — `DecodingTask._get_audio_features` skips the encoder when the input shape is `(n_audio_ctx, n_audio_state)`) cut 1.92 s → 1.01 s (short) and 2.06 s → 1.24 s (long) with identical transcripts on German, English and mixed clips.

**Files:**
- Modify: `flow/stt.py` (`WhisperEngine.transcribe`, new module function `decode_with_fallback`)
- Test: create `tests/test_stt.py`

**Interfaces:**
- Produces: `stt.decode_with_fallback(model, features, prompt_tokens) -> DecodingResult`; `WhisperEngine.transcribe(audio, initial_prompt=None) -> str` (unchanged signature).

- [ ] **Step 1: Failing tests** (`tests/test_stt.py`)

```python
import types
from unittest import mock

import numpy as np

from flow import stt


def _result(text="hi", cr=1.0, lp=-0.2, ns=0.0):
    return types.SimpleNamespace(text=text, compression_ratio=cr, avg_logprob=lp, no_speech_prob=ns)


class _FakeModel:
    def __init__(self, results):
        self.results = list(results)
        self.temps = []
        self.encodes = 0
        self.dims = types.SimpleNamespace(n_mels=128, n_audio_ctx=1500, n_audio_state=8)
        self.is_multilingual = True
        self.num_languages = 100

    def encoder(self, x):
        import mlx.core as mx

        self.encodes += 1
        return mx.zeros((1, 1500, 8), dtype=mx.float16)

    def decode(self, features, options):
        assert features.shape == (1500, 8)
        assert options.language is None  # language is always auto-detected
        self.temps.append(options.temperature)
        return self.results.pop(0)


def test_good_first_decode_stops_the_ladder():
    import mlx.core as mx
    m = _FakeModel([_result("Hallo")])
    r = stt.decode_with_fallback(m, mx.zeros((1500, 8), dtype=mx.float16), None)
    assert r.text == "Hallo" and m.temps == [0.0]


def test_repetitive_decode_falls_back_to_higher_temperature():
    import mlx.core as mx
    m = _FakeModel([_result(cr=3.0), _result(lp=-1.5), _result("ok")])
    r = stt.decode_with_fallback(m, mx.zeros((1500, 8), dtype=mx.float16), None)
    assert m.temps == [0.0, 0.2, 0.4] and r.text == "ok"


def test_silence_stops_the_ladder():
    import mlx.core as mx
    m = _FakeModel([_result(lp=-2.0, ns=0.9)])
    stt.decode_with_fallback(m, mx.zeros((1500, 8), dtype=mx.float16), None)
    assert m.temps == [0.0]


def test_transcribe_encodes_once_even_with_fallbacks():
    m = _FakeModel([_result(cr=3.0), _result(" Hallo Welt ")])
    eng = stt.WhisperEngine.__new__(stt.WhisperEngine)
    eng.model_id = "x"
    with mock.patch.object(stt.WhisperEngine, "_model", return_value=m):
        text = eng.transcribe(np.zeros(16000 * 3, dtype=np.float32))
    assert text == "Hallo Welt"
    assert m.encodes == 1


def test_silent_result_returns_empty_text():
    m = _FakeModel([_result("Untertitel von ZDF", lp=-1.8, ns=0.95)])
    eng = stt.WhisperEngine.__new__(stt.WhisperEngine)
    eng.model_id = "x"
    with mock.patch.object(stt.WhisperEngine, "_model", return_value=m):
        assert eng.transcribe(np.zeros(16000, dtype=np.float32)) == ""


def test_long_audio_uses_windowed_transcribe():
    eng = stt.WhisperEngine.__new__(stt.WhisperEngine)
    eng.model_id = "x"
    with mock.patch("mlx_whisper.transcribe", return_value={"text": " lang "}) as tr:
        out = eng.transcribe(np.zeros(16000 * 31, dtype=np.float32), initial_prompt="Foo")
    assert out == "lang"
    assert tr.call_args.kwargs["initial_prompt"] == "Foo"
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: ... decode_with_fallback`). `.venv/bin/python -m pytest tests/test_stt.py -q`

- [ ] **Step 3: Implement in `flow/stt.py`**

```python
# Same thresholds mlx_whisper.transcribe uses by default.
TEMPERATURES = (0.0, 0.2, 0.4)
COMPRESSION_RATIO_THRESHOLD = 2.4
LOGPROB_THRESHOLD = -1.0
NO_SPEECH_THRESHOLD = 0.6


def decode_with_fallback(model, features, prompt_tokens):
    """Decode pre-encoded features, retrying at higher temperatures when the
    result looks repetitive or unlikely — mirrors mlx_whisper.transcribe's
    ladder, minus the encoder pass it repeats on every call."""
    from mlx_whisper.decoding import DecodingOptions

    result = None
    for t in TEMPERATURES:
        result = model.decode(
            features, DecodingOptions(temperature=t, prompt=prompt_tokens, fp16=True)
        )
        if result.no_speech_prob > NO_SPEECH_THRESHOLD:
            break  # silence — a hotter decode will not help
        if (result.compression_ratio <= COMPRESSION_RATIO_THRESHOLD
                and result.avg_logprob >= LOGPROB_THRESHOLD):
            break
    return result
```

Replace `WhisperEngine` methods (keep `__init__`, `warmup`):

```python
    def _model(self):
        import importlib

        import mlx.core as mx

        # importlib: the package re-exports the transcribe *function* under
        # the same name as the module that holds ModelHolder.
        holder = importlib.import_module("mlx_whisper.transcribe").ModelHolder
        return holder.get_model(self.model_id, mx.float16)

    def transcribe(self, audio: np.ndarray, initial_prompt: str | None = None) -> str:
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES

        if len(audio) > N_SAMPLES:
            # Longer than one 30 s window (hands-free dictations): the
            # library's seek loop handles the windowing.
            return self._transcribe_windowed(audio, initial_prompt)

        import mlx.core as mx
        from mlx_whisper.audio import log_mel_spectrogram, pad_or_trim
        from mlx_whisper.tokenizer import get_tokenizer

        model = self._model()
        # Same framing as mlx_whisper.transcribe's decode: the content frames,
        # zero-padded to the 30 s window the encoder expects.
        mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
        content = mel[: mel.shape[-2] - N_FRAMES]
        segment = pad_or_trim(content, N_FRAMES, axis=-2).astype(mx.float16)
        # One encoder pass, reused for language detection and every
        # temperature fallback (transcribe() encodes at least twice).
        features = model.encoder(segment[None])[0]

        prompt_tokens = None
        if initial_prompt:
            tokenizer = get_tokenizer(
                model.is_multilingual, num_languages=model.num_languages, task="transcribe"
            )
            prompt_tokens = tokenizer.encode(" " + initial_prompt.strip())

        result = decode_with_fallback(model, features, prompt_tokens)
        if (result.no_speech_prob > NO_SPEECH_THRESHOLD
                and result.avg_logprob < LOGPROB_THRESHOLD):
            return ""  # transcribe() drops such segments as silence
        return result.text.strip()

    def _transcribe_windowed(self, audio, initial_prompt):
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model_id,
            initial_prompt=initial_prompt,
            # Each dictation is one independent utterance: carrying decoded
            # text across 30s windows costs time and propagates
            # hallucinations into later windows.
            condition_on_previous_text=False,
            temperature=TEMPERATURES,
        )
        return result["text"].strip()
```

- [ ] **Step 4: Run — expect PASS.** `.venv/bin/python -m pytest tests/test_stt.py -q` then the full suite.

- [ ] **Step 5: Real-model equivalence check (manual, required).** Write `/private/tmp/claude-501/-Users-admin-Desktop-Wispr-Lokal/9cdb730e-e109-4d37-8115-78946c75d787/scratchpad/onepass_check.py` that loads `scratchpad/short.wav`, `long.wav`, `perf/en.wav`, `perf/mix.wav` (16 kHz int16 → float32 / 32768), runs both `stt.WhisperEngine("mlx-community/whisper-large-v3-turbo-q4").transcribe(a)` and `stt.WhisperEngine(...)._transcribe_windowed(a, None)` 3× each with `HF_HUB_OFFLINE=1`, and prints best time + whether the texts are equal. Run with `.venv/bin/python <script>` from the repo root. Expected: one-pass ≈ 1.0–1.3 s vs ≈ 1.9–2.1 s, texts equal (minor punctuation differences acceptable; report any word differences to the orchestrator instead of committing).

- [ ] **Step 6: Commit**

```bash
git add flow/stt.py tests/test_stt.py
git commit -m "Encode each dictation once instead of twice

transcribe() ran the Whisper encoder for language detection and again for
decoding. Encode the 30 s window once and hand the features to decode():
~0.9 s faster per dictation on an M3, same transcripts.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Hotkey reports Control (dictation vs. command)

**Files:**
- Modify: `flow/hotkey.py` (`FnHotkeyListener`, `make_listener`)
- Modify: `flow/__main__.py` (wire `on_mode_change`)
- Test: `tests/test_hotkey.py`

**Interfaces:**
- Produces: `FnHotkeyListener(on_hold_start, on_hold_end, runner=None, on_mode_change=None)`; calls `on_hold_start(mode)` with `mode in {"dictation", "command"}`, `on_hold_end()`, `on_mode_change("command")`. `make_listener(key_name, on_hold_start, on_hold_end, on_mode_change=None)`. The pynput `HotkeyListener` keeps calling `on_hold_start()` with no argument (pipeline default `mode="dictation"`).
- Consumes (Task 6): `Pipeline.on_hold_start(mode="dictation")`, `Pipeline.on_mode_change(mode)`.

- [ ] **Step 1: Update the test helper and add failing tests** (`tests/test_hotkey.py`)

Change `_Recorder` so `start` accepts the mode:

```python
class _Recorder:
    def __init__(self):
        self.starts = 0
        self.ends = 0
        self.modes = []
        self.mode_changes = []

    def start(self, mode="dictation"):
        self.starts += 1
        self.modes.append(mode)

    def end(self):
        self.ends += 1

    def change(self, mode):
        self.mode_changes.append(mode)
```

Every other test in the file that passes a zero-arg lambda/function as `on_hold_start` to `FnHotkeyListener` must accept `*args` (e.g. `lambda *a: seen.append("start")`, `def on_start(*_):`). Add:

```python
class FnControlTests(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self.listener = FnHotkeyListener(
            self.rec.start, self.rec.end, runner=_direct, on_mode_change=self.rec.change
        )

    def test_plain_fn_is_dictation(self):
        self.listener._dispatch(fn_down=True)
        self.listener._dispatch(fn_down=False)
        self.assertEqual(self.rec.modes, ["dictation"])

    def test_control_then_fn_is_command(self):
        self.listener._dispatch(fn_down=True, ctrl_down=True)
        self.assertEqual(self.rec.modes, ["command"])
        self.assertEqual(self.rec.mode_changes, [])

    def test_fn_then_control_switches_to_command_once(self):
        self.listener._dispatch(fn_down=True)
        self.listener._dispatch(fn_down=True, ctrl_down=True)
        self.listener._dispatch(fn_down=True, ctrl_down=False)
        self.listener._dispatch(fn_down=True, ctrl_down=True)
        self.assertEqual(self.rec.modes, ["dictation"])
        self.assertEqual(self.rec.mode_changes, ["command"])

    def test_control_without_fn_does_nothing(self):
        self.listener._dispatch(fn_down=False, ctrl_down=True)
        self.assertEqual(self.rec.starts, 0)

    def test_handle_event_reads_control_flag(self):
        from AppKit import NSEventModifierFlagControl, NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        # Within the debounce window, but Control changed — must not be gated.
        self.listener._handle_event(
            FakeEvent(NSEventModifierFlagFunction | NSEventModifierFlagControl)
        )
        self.assertEqual(self.rec.mode_changes, ["command"])
```

- [ ] **Step 2: Run — expect FAIL.** `.venv/bin/python -m pytest tests/test_hotkey.py -q`

- [ ] **Step 3: Implement** (`flow/hotkey.py`, `FnHotkeyListener`)

`__init__` gains `on_mode_change=None` and state:

```python
    def __init__(self, on_hold_start, on_hold_end, runner=None, on_mode_change=None):
        self._on_hold_start = on_hold_start
        self._on_hold_end = on_hold_end
        self._on_mode_change = on_mode_change
        self._held = False
        self._ctrl = False
        self._mode = "dictation"
        ...  # rest unchanged
```

`_handle_event`:

```python
    def _handle_event(self, event):
        from AppKit import NSEventModifierFlagControl, NSEventModifierFlagFunction

        now = time.monotonic()
        flags = event.modifierFlags()
        fn_down = bool(flags & NSEventModifierFlagFunction)
        ctrl_down = bool(flags & NSEventModifierFlagControl)
        # Only gate events that change nothing we track (see DEBOUNCE_S).
        if (fn_down == self._held and ctrl_down == self._ctrl
                and now - self._last_event_time < DEBOUNCE_S):
            return
        self._last_event_time = now
        self._dispatch(fn_down, ctrl_down)
```

`_dispatch`:

```python
    def _dispatch(self, fn_down, ctrl_down=False):
        if fn_down and not self._held:
            self._held = True
            self._ctrl = ctrl_down
            # Holding Control turns the press into a rewrite of the selection.
            mode = "command" if ctrl_down else "dictation"
            self._mode = mode
            self._runner(lambda: _safe_call(lambda: self._on_hold_start(mode), "on_hold_start"))
        elif fn_down and self._held:
            if ctrl_down and not self._ctrl and self._mode == "dictation":
                self._mode = "command"
                if self._on_mode_change is not None:
                    self._runner(lambda: _safe_call(
                        lambda: self._on_mode_change("command"), "on_mode_change"))
            self._ctrl = ctrl_down
        elif not fn_down and self._held:
            self._held = False
            self._ctrl = ctrl_down
            self._runner(lambda: _safe_call(self._on_hold_end, "on_hold_end"))
        else:
            self._ctrl = ctrl_down
```

`make_listener`:

```python
def make_listener(key_name, on_hold_start, on_hold_end, on_mode_change=None):
    if key_name == "fn":
        return FnHotkeyListener(on_hold_start, on_hold_end, on_mode_change=on_mode_change)
    return HotkeyListener(key_name, on_hold_start, on_hold_end)
```

In `flow/__main__.py` pass `on_mode_change=pipeline.on_mode_change` to `make_listener(...)`. Until Task 6 lands, add a stub to `Pipeline` so the app runs:

```python
    def on_mode_change(self, mode):
        pass  # filled in by the hands-free/rewrite task
```

and give `Pipeline.on_hold_start` the signature `def on_hold_start(self, mode="dictation"):` (ignore `mode` for now).

- [ ] **Step 4: Run full suite — expect PASS.**

- [ ] **Step 5: Commit** — message: `Tell the pipeline whether Control is held with fn` (+ trailer).

---

### Task 4: Hands-free chime and config

**Files:**
- Modify: `packaging/make_sounds.py`, `flow/sound.py`, `flow/config.py`, `config.toml`
- Create (generated): `flow/ui/assets/rec_handsfree.wav`
- Test: `tests/test_sound.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `sound.play_handsfree() -> None`, `sound.handsfree_duration_s() -> float`, `Config.handsfree_max_s: float` (default 600.0).

- [ ] **Step 1: Failing tests**

`tests/test_sound.py` (append):

```python
from flow import sound


def test_handsfree_chime_asset_exists_and_is_longer_than_start():
    assert (sound._assets_dir() / "rec_handsfree.wav").exists()
    assert sound.handsfree_duration_s() > sound.start_duration_s()


def test_play_handsfree_never_raises(monkeypatch):
    monkeypatch.setattr(sound, "_ensure_loaded", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    sound.play_handsfree()  # logged, not raised
```

`tests/test_config.py` (append — follow the file's existing pattern for loading a temp config; if it builds a Config from a TOML string, use `[audio]\nhandsfree_max_s = 90`):

```python
def test_handsfree_max_default():
    from flow.config import Config
    assert Config().handsfree_max_s == 600.0
```

plus one test that a TOML value `handsfree_max_s = 90` loads as `90.0`, written in the same style as the existing config-loading tests in that file.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

`packaging/make_sounds.py` `main()` — add:

```python
    # Rising G5 -> C6 -> E6: "hands-free recording — no need to hold fn".
    _write_wav(ASSETS_DIR / "rec_handsfree.wav", _chime([783.99, 1046.50, 1318.51]))
    print(f"Wrote {ASSETS_DIR / 'rec_handsfree.wav'}")
```

Run `.venv/bin/python packaging/make_sounds.py` (regenerates all three; `git diff --stat` must show the two existing wavs unchanged — the generator is deterministic).

`flow/sound.py`: add `_HANDSFREE_NAME = "rec_handsfree.wav"`, a `_handsfree_sound` global loaded in `_ensure_loaded()` alongside the others, and:

```python
_handsfree_duration_s = None


def handsfree_duration_s() -> float:
    global _handsfree_duration_s
    if _handsfree_duration_s is None:
        _handsfree_duration_s = _wav_duration_s(_assets_dir() / _HANDSFREE_NAME)
    return _handsfree_duration_s


def play_handsfree():
    try:
        _ensure_loaded()
        if _handsfree_sound is not None:
            _handsfree_sound.play()
    except Exception:
        log.exception("Failed to play hands-free sound")
```

`flow/config.py`: dataclass field `handsfree_max_s: float = 600.0` (next to `max_duration_s`); in `load()`: `cfg.handsfree_max_s = float(audio.get("handsfree_max_s", cfg.handsfree_max_s))`.

`config.toml` `[audio]` after `max_duration_s`:

```toml
# Hands-free recordings (double-tap fn, tap fn again to finish) stop after
# this many seconds.
handsfree_max_s = 600
```

- [ ] **Step 4: Run full suite — PASS.** The `.app` bundles every file in `flow/ui/assets/` (`packaging/Flow.spec` globs it), so no spec change.

- [ ] **Step 5: Commit** — `Add the hands-free chime and time limit` (+ trailer). Include the new wav.

---

### Task 5: Overlay styles and status messages

**Files:**
- Modify: `flow/overlay.py`
- Test: `tests/test_overlay.py`

**Interfaces:**
- Produces: `Overlay.show(style="dictation")`, `Overlay.set_style(style)`, `Overlay.flash(message: str, seconds: float | None = 2.0)`, `Overlay.hide()`; module function `layout_for(style: str) -> dict` with keys `width, show_wave, show_lock, label`; `pill_colors(style) -> tuple[top_rgba, bottom_rgba, border_rgba]`. Styles: `"dictation"`, `"handsfree"`, `"command"`, `"message"`.

- [ ] **Step 1: Failing tests** (`tests/test_overlay.py`, append)

```python
from flow import overlay


def test_dictation_layout_is_unchanged():
    lay = overlay.layout_for("dictation")
    assert lay["width"] == overlay.PILL_WIDTH
    assert lay["show_wave"] and not lay["show_lock"] and lay["label"] is None


def test_handsfree_layout_shows_lock_and_wave():
    lay = overlay.layout_for("handsfree")
    assert lay["show_lock"] and lay["show_wave"]
    assert lay["width"] > overlay.PILL_WIDTH


def test_command_layout_asks_the_question():
    lay = overlay.layout_for("command")
    assert lay["label"] == "What should I do with it?"
    assert lay["show_wave"]


def test_message_layout_has_no_wave():
    lay = overlay.layout_for("message")
    assert not lay["show_wave"] and not lay["show_lock"]


def test_handsfree_pill_is_green_not_blue():
    top, _bottom, _border = overlay.pill_colors("handsfree")
    r, g, b, _a = top
    assert g > r and g > b


def test_unknown_style_falls_back_to_dictation():
    assert overlay.layout_for("bogus") == overlay.layout_for("dictation")
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** (`flow/overlay.py`)

Constants and pure helpers (after `positioned_pill_frame`; change `positioned_pill_frame(screen_frame, width=PILL_WIDTH)` to centre a pill of the given width):

```python
WIDE_PILL_WIDTH = 330.0
HANDSFREE_PILL_WIDTH = 232.0
LOCK_SLOT = 26.0
PULSE_SPEED = 0.09  # radians per tick of the border pulse (≈1 s period at 60 fps)

_LAYOUTS = {
    "dictation": {"width": PILL_WIDTH, "show_wave": True, "show_lock": False, "label": None},
    "handsfree": {"width": HANDSFREE_PILL_WIDTH, "show_wave": True, "show_lock": True, "label": None},
    "command": {"width": WIDE_PILL_WIDTH, "show_wave": True, "show_lock": False,
                "label": "What should I do with it?"},
    "message": {"width": WIDE_PILL_WIDTH, "show_wave": False, "show_lock": False, "label": ""},
}

_COLORS = {
    # (top, bottom, border) RGBA
    "dictation": ((0.12, 0.14, 0.20, 0.96), (0.07, 0.09, 0.13, 0.96), (0.26, 0.43, 0.58, 0.28)),
    "handsfree": ((0.10, 0.27, 0.19, 0.96), (0.05, 0.16, 0.11, 0.96), (0.36, 0.78, 0.55, 0.55)),
    "command": ((0.12, 0.14, 0.20, 0.96), (0.07, 0.09, 0.13, 0.96), (0.26, 0.43, 0.58, 0.28)),
    "message": ((0.12, 0.14, 0.20, 0.96), (0.07, 0.09, 0.13, 0.96), (0.26, 0.43, 0.58, 0.28)),
}


def layout_for(style: str) -> dict:
    return dict(_LAYOUTS.get(style, _LAYOUTS["dictation"]))


def pill_colors(style: str):
    return _COLORS.get(style, _COLORS["dictation"])
```

`_PillBackgroundView`: add `style` and `pulse` attributes (set in a `initWithFrame_` override like `_WaveformView`, defaults `"dictation"` / `0.0`), and draw with `pill_colors(self.style)`; for the border use alpha `border_a * (0.55 + 0.45 * self.pulse)` when `self.style == "handsfree"`, otherwise `border_a`, and line width `1.5` for hands-free.

`_build_window`: additionally create
- `self._label`: `NSTextField.labelWithString_("")` (import `NSTextField`, `NSFont`), font `NSFont.systemFontOfSize_weight_(13.0, 0.3)`, text colour white 0.92, hidden by default, autoresizing 18.
- `self._lock`: `NSTextField.labelWithString_("🔒")`, font size 14, hidden by default.

Add a main-thread `_apply_style_main(style)` that:
1. stores `self._style = style`; `lay = layout_for(style)`;
2. sets `self._pill.style = style; self._pill.setNeedsDisplay_(True)`;
3. resizes the window to `positioned_pill_frame(screen.frame(), lay["width"])` using the same screen lookup as `_position_window` (refactor `_position_window(width)` to take the width);
4. lays out, with `w = lay["width"]`, `h = PILL_HEIGHT`, `m = PILL_MARGIN`:
   - lock (if shown): frame `(m + 2, (h - 18) / 2, 20, 18)`;
   - label (if `lay["label"] is not None`): text = label, frame `(m + 6, (h - 18) / 2, (w * 0.58 if lay["show_wave"] else w - 2 * m - 12), 18)`;
   - wave (if shown): x starts after lock (`m + LOCK_SLOT`) or after the label (`m + w * 0.58 + 10`) or at `m`; width runs to `w - m`; hidden otherwise;
5. sets `setHidden_` on each subview accordingly.

Public API (thread-safe, all via `AppHelper.callAfter`):

```python
    def show(self, style="dictation"):
        AppHelper.callAfter(self._show_main, style)

    def set_style(self, style):
        AppHelper.callAfter(self._set_style_main, style)

    def flash(self, message, seconds=2.0):
        """Show a short status text; auto-hides after `seconds` (None = stay
        until hide() or the next show/flash)."""
        AppHelper.callAfter(self._flash_main, message, seconds)
```

`_show_main(self, style="dictation")`: call `self._apply_style_main(style)` right after `_prewarm_main()` (before the animation). `_set_style_main(style)`: if the window is visible, `_apply_style_main(style)`. `_flash_main(message, seconds)`: `_show_main("message")`-equivalent without restarting the wave timer when already visible — simplest: `self._show_main("message")`, then `self._label.setStringValue_(message)`, then if `seconds` is not None schedule `NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(seconds, self._timer_target, b"autoHide:", None, False)` stored in `self._auto_hide_timer` (invalidate any previous one in `_show_main`, `_hide_main`, `_flash_main`). Add `autoHide_` to `_TimerTarget` calling `self._overlay._hide_main()`.

`_on_tick`: when `self._style == "handsfree"`, advance `self._pulse_phase += PULSE_SPEED`, set `self._pill.pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)` and `self._pill.setNeedsDisplay_(True)`. When the style is `"message"`, skip the waveform update.

Keep `prewarm()` building the window in the `dictation` style.

- [ ] **Step 4: Run full suite — PASS.**

- [ ] **Step 5: Visual check (manual).** Run `.venv/bin/python -c` snippet from the repo root that creates `NSApplication.sharedApplication()`, an `Overlay(level_source=lambda: 0.5)`, calls `show("dictation")`, after 1.5 s `set_style("handsfree")`, after 1.5 s `show("command")`, after 1.5 s `flash("Select some text first")`, using `AppHelper.callLater` and `AppHelper.runEventLoop()`, stopping after 8 s. Take screenshots with `screencapture -x <scratchpad>/overlay_N.png` at each step (via `subprocess` inside the callLater callbacks) and look at them: no clipped text, lock left of the waveform, green pill in hands-free, question label legible.

- [ ] **Step 6: Commit** — `Give the overlay hands-free, rewrite and message styles` (+ trailer).

---

### Task 6: Pipeline — hands-free state machine and mode change

**Files:**
- Modify: `flow/pipeline.py`, `flow/menubar.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Task 3 (`on_hold_start(mode)`, `on_mode_change(mode)`), Task 4 (`sound.play_handsfree`, `sound.handsfree_duration_s`, `cfg.handsfree_max_s`), Task 5 (`overlay.show(style)`, `overlay.set_style(style)`), Task 1 (queue 3-tuples, `_relaunch_pending`).
- Produces: `Pipeline._mode: str | None`, `Pipeline._tap_timer: threading.Timer | None`, `DOUBLE_TAP_S = 0.4`, `Pipeline._finish_recording(discard=False)`, `Recorder.clear()`; queued `mode` is the recording's final mode (`"dictation"`, `"handsfree"`, `"command"`); menubar state `"handsfree"` → `🔒`.

Behaviour to implement:
- `on_hold_start(mode)`:
  - if a tap-grace timer is pending and `mode == "dictation"` → cancel it and **switch the running recording to hands-free** (`recorder.clear()`, `recorder.mute_for(sound.handsfree_duration_s() + CHIME_GUARD_S)`, `sound.play_handsfree()` if sounds on, `overlay.set_style("handsfree")`, `set_state("handsfree")`, restart the max timer with `cfg.handsfree_max_s`).
  - else if recording and `self._mode == "handsfree"` → finish the recording (call `_finish_recording()` **after** releasing `self._lock`).
  - else existing behaviour, storing `self._mode = mode`, `overlay.show(style=mode)` and `set_state("recording")`.
- `on_hold_end()`:
  - not recording or `_stopping` → return; `self._mode == "handsfree"` → return (release ignored);
  - `self._mode == "dictation"` and press shorter than `cfg.min_duration_s` → start `self._tap_timer = threading.Timer(DOUBLE_TAP_S, lambda: self._finish_recording(discard=True))` (daemon) and return;
  - otherwise `_finish_recording()`.
- `on_mode_change("command")`: under the lock, only when recording, not stopping, and `self._mode == "dictation"` → `self._mode = "command"`, `overlay.set_style("command")`.
- `_finish_recording(discard=False)`: clear `self._tap_timer` (cancel if it isn't the caller); when `discard` is true, stop the recorder (still via `_stop_recorder_safe`), play the stop sound, hide the overlay, log `"Discarding %.2fs tap"` with the press length, set idle, return. Queue `(captured, time.monotonic(), self._mode or "dictation")`. Reset `self._mode = None` in the `finally`.
- The `_force_stop` max timer path keeps working for both limits.

- [ ] **Step 1: Failing tests** (add to `tests/test_pipeline.py`; extend `_build_pipeline()` with `pipeline._mode = None` and `pipeline._tap_timer = None`, and give `_Overlay` `show(self, style="dictation")`, `set_style(self, style)` recording calls into `self.styles` list, `flash(self, message, seconds=2.0)` into `self.flashes`; give `_Recorder` a `clear()` that counts calls; add `handsfree_max_s = 600.0` to `_Config`)

```python
class HandsFreeTests(unittest.TestCase):
    def setUp(self):
        self.p = _build_pipeline()
        self.p.engine = object()
        self.states = []
        self.p.set_state = self.states.append
        self.queued = []
        self.p._queue = type("_Q", (), {"put": lambda s, item: self.queued.append(item)})()
        self.sound = patch("flow.pipeline.sound").start()
        self.sound.start_duration_s.return_value = 0.2
        self.sound.handsfree_duration_s.return_value = 0.3
        # The fake recorder returns zeros, which the silence check would send
        # down the recovery path instead of the queue.
        patch("flow.pipeline.audio_mod.is_silent", return_value=False).start()
        self.addCleanup(patch.stopall)

    def _tap(self):
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 0.1  # a 0.1 s tap
        self.p.on_hold_end()

    def test_double_tap_enters_handsfree_on_same_stream(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.assertEqual(self.p._mode, "handsfree")
        self.assertEqual(self.p.recorder.stops, 0)  # stream never restarted
        self.assertEqual(self.p.recorder.clears, 1)
        self.assertIn("handsfree", self.states)
        self.sound.play_handsfree.assert_called_once()

    def test_release_is_ignored_in_handsfree(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.p.on_hold_end()
        self.assertTrue(self.p.recorder.recording)

    def test_next_press_stops_handsfree_and_queues_it(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 5.0
        self.p.on_hold_start("dictation")
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued[-1][2], "handsfree")

    def test_single_tap_is_discarded_after_grace(self):
        with patch("flow.pipeline.DOUBLE_TAP_S", 0.05):
            self._tap()
            self.assertTrue(self.p.recorder.recording)  # grace window open
            time.sleep(0.2)
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued, [])

    def test_long_hold_is_a_normal_dictation(self):
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 2.0
        self.p.on_hold_end()
        self.assertEqual(self.queued[-1][2], "dictation")
        self.assertIsNone(self.p._tap_timer)

    def test_handsfree_uses_its_own_max_duration(self):
        self._tap()
        with patch("flow.pipeline.threading.Timer") as timer:
            self.p.on_hold_start("dictation")
        self.assertEqual(timer.call_args.args[0], 600.0)

    def test_mode_change_turns_dictation_into_command(self):
        self.p.on_hold_start("dictation")
        self.p.on_mode_change("command")
        self.assertEqual(self.p._mode, "command")
        self.assertEqual(self.p.overlay.styles[-1], "command")

    def test_mode_change_ignored_in_handsfree(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.p.on_mode_change("command")
        self.assertEqual(self.p._mode, "handsfree")

    def test_command_press_never_enters_handsfree(self):
        self._tap()
        self.p.on_hold_start("command")  # Control held on the second press
        self.assertNotEqual(self.p._mode, "handsfree")
```

Note on `test_command_press_never_enters_handsfree`: a command press during the grace window should first discard the pending tap (finish it with `discard=True` synchronously) and then start a fresh command recording. Implement exactly that.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** in `flow/pipeline.py` per the behaviour list above. Add `DOUBLE_TAP_S = 0.4` next to the other constants, with a comment: "A tap released this recently turns the next press into hands-free." Add `Recorder.clear()` to `flow/audio.py`:

```python
    def clear(self):
        """Drop what was captured so far (the double tap's first press)."""
        self._chunks = []
```

In `flow/menubar.py` add `"handsfree": "🔒",` to `STATE_ICONS`.

- [ ] **Step 4: Run full suite — PASS.**

- [ ] **Step 5: Commit** — `Double-tap fn for hands-free dictation` (+ trailer).

---

### Task 7: Rewrite backend — selection grab and LLM rewrite

**Files:**
- Create: `flow/transform.py`
- Modify: `flow/insert.py` (generalise `_post_cmd_v` → `_post_cmd_key(keycode)`; keep `_post_cmd_v` as a one-line wrapper), `flow/cleanup.py` (`Cleaner.rewrite`, `_post_openai(body, timeout=None)`)
- Test: create `tests/test_transform.py`; extend `tests/test_cleanup.py`

**Interfaces:**
- Produces: `transform.grab_selection() -> str | None`; `transform.build_rewrite_user_message(text: str, instruction: str) -> str`; `Cleaner.rewrite(text: str, instruction: str) -> str | None`; `cleanup.REWRITE_TIMEOUT_S = 10.0`; `cleanup.REWRITE_PROMPT`.

- [ ] **Step 1: Failing tests**

`tests/test_transform.py`:

```python
from unittest import mock

from flow import transform


class _PB:
    def __init__(self, text_after_copy=None):
        self.count = 1
        self.text = "users clipboard"
        self.text_after_copy = text_after_copy
        self.restored = None

    def changeCount(self):
        return self.count

    def stringForType_(self, _t):
        return self.text

    def copy(self):
        if self.text_after_copy is not None:
            self.count += 1
            self.text = self.text_after_copy


def _patched(pb):
    return [
        mock.patch.object(transform, "_pasteboard", return_value=pb),
        mock.patch.object(transform, "_on_main", side_effect=lambda f, timeout=None: f()),
        mock.patch.object(transform, "_wait_modifiers_released"),
        mock.patch.object(transform, "_post_cmd_key", side_effect=lambda code: pb.copy()),
        mock.patch.object(transform, "_snapshot_pasteboard", return_value=["SNAP"]),
        mock.patch.object(transform, "_restore_pasteboard",
                          side_effect=lambda p, items: setattr(pb, "restored", items)),
    ]


def _run(pb):
    patches = _patched(pb)
    for p in patches:
        p.start()
    try:
        return transform.grab_selection()
    finally:
        for p in patches:
            p.stop()


def test_grab_selection_returns_copied_text_and_restores_clipboard():
    pb = _PB(text_after_copy="selected words")
    assert _run(pb) == "selected words"
    assert pb.restored == ["SNAP"]


def test_grab_selection_restores_clipboard_when_nothing_selected():
    pb = _PB(text_after_copy=None)
    with mock.patch.object(transform, "COPY_WAIT_S", 0.05):
        assert _run(pb) is None
    # Nothing was written, so nothing needs restoring — and the user's data is intact.
    assert pb.text == "users clipboard"


def test_rewrite_message_delimits_instruction_and_text():
    msg = transform.build_rewrite_user_message("Hallo Welt", "mach Stichpunkte")
    assert "<instruction>\nmach Stichpunkte\n</instruction>" in msg
    assert "<text>\nHallo Welt\n</text>" in msg
```

`tests/test_cleanup.py` (append; reuse the file's existing fake-config/Cleaner construction pattern — e.g. build a `Cleaner` via `__new__` and set `cfg`, `_client=None`, `_api_key="k"`):

```python
def _rewrite_cleaner(response=None, exc=None):
    from flow import cleanup
    c = cleanup.Cleaner.__new__(cleanup.Cleaner)
    c.cfg = type("C", (), {"cleanup_model": "m", "cleanup_base_url": "https://x/api/v1",
                           "cleanup_timeout_s": 5.0})()
    c.provider = "openrouter"
    c._client = None
    c._api_key = "k"
    calls = []

    def post(body, timeout=None):
        calls.append((body, timeout))
        if exc:
            raise exc
        return response

    c._post_openai = post
    return c, calls


def test_rewrite_returns_model_text_with_long_timeout():
    from flow import cleanup
    c, calls = _rewrite_cleaner({"choices": [{"finish_reason": "stop",
                                              "message": {"content": "- a\n- b"}}]})
    assert c.rewrite("a b", "bullet points") == "- a\n- b"
    body, timeout = calls[0]
    assert timeout == cleanup.REWRITE_TIMEOUT_S
    assert body["messages"][0]["content"] == cleanup.REWRITE_PROMPT
    assert "bullet points" in body["messages"][1]["content"]
    assert "response_format" not in body


def test_rewrite_truncated_returns_none():
    c, _ = _rewrite_cleaner({"choices": [{"finish_reason": "length",
                                          "message": {"content": "- a"}}]})
    assert c.rewrite("a b", "bullet points") is None


def test_rewrite_failure_returns_none():
    c, _ = _rewrite_cleaner(exc=TimeoutError("slow"))
    assert c.rewrite("a b", "bullet points") is None


def test_rewrite_without_key_returns_none():
    c, _ = _rewrite_cleaner()
    c._api_key = None
    assert c.rewrite("a b", "x") is None
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**

`flow/insert.py`:

```python
def _post_cmd_key(keycode: int):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, keycode, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.01)


def _post_cmd_v():
    _post_cmd_key(_KEYCODE_V)
```

`flow/transform.py`:

```python
"""Rewrite mode helpers: read the user's current selection.

The selection is read the way a user would copy it — Cmd+C — because the
Accessibility selected-text attribute is missing in browsers and Electron
apps. The user's clipboard is snapshotted first and put back afterwards.
"""

import logging
import time

from AppKit import NSPasteboard, NSPasteboardTypeString

from .insert import (
    _on_main,
    _post_cmd_key,
    _restore_pasteboard,
    _snapshot_pasteboard,
    _wait_modifiers_released,
)

log = logging.getLogger(__name__)

_KEYCODE_C = 8
COPY_WAIT_S = 0.5  # how long the frontmost app gets to answer Cmd+C
_POLL_S = 0.02


def _pasteboard():
    return NSPasteboard.generalPasteboard()


def grab_selection() -> str | None:
    """The frontmost app's selected text, or None when nothing is selected
    (Cmd+C left the clipboard untouched) or the selection isn't text."""
    _wait_modifiers_released()

    def snapshot():
        pb = _pasteboard()
        return pb.changeCount(), _snapshot_pasteboard(pb)

    before, saved = _on_main(snapshot)
    _post_cmd_key(_KEYCODE_C)

    deadline = time.monotonic() + COPY_WAIT_S
    while time.monotonic() < deadline:
        if _on_main(lambda: _pasteboard().changeCount()) != before:
            break
        time.sleep(_POLL_S)
    else:
        log.info("Rewrite: Cmd+C changed nothing — no selection")
        return None

    def read_and_restore():
        pb = _pasteboard()
        text = pb.stringForType_(NSPasteboardTypeString)
        _restore_pasteboard(pb, saved)
        return str(text) if text else None

    return _on_main(read_and_restore)


def build_rewrite_user_message(text: str, instruction: str) -> str:
    return (
        f"<instruction>\n{instruction.strip()}\n</instruction>\n\n"
        f"<text>\n{text}\n</text>"
    )
```

`flow/cleanup.py`:

```python
REWRITE_TIMEOUT_S = 10.0

REWRITE_PROMPT = """You rewrite text according to the user's spoken instruction. Return ONLY the rewritten text — no preamble, no quotes, no commentary, no code fences.

Rules:
- Follow the instruction (for example: bullet points, more formal, shorter, as a poem, translate).
- Keep the language of the original text unless the instruction asks for a different language.
- Preserve the meaning unless the instruction asks to change it.
- The instruction was transcribed from speech and may contain recognition errors; interpret it charitably."""
```

`_post_openai(self, body, timeout=None)` → use `timeout or self.cfg.cleanup_timeout_s` in `urlopen`.

`Cleaner.rewrite`:

```python
    def rewrite(self, text: str, instruction: str) -> str | None:
        """Rewritten text, or None on any failure — the caller then leaves the
        user's selection untouched."""
        from .transform import build_rewrite_user_message

        if not text or not instruction or not self.available:
            return None
        user = build_rewrite_user_message(text, instruction)
        try:
            if self._client is not None:
                resp = self._client.messages.create(
                    model=self.cfg.cleanup_model,
                    max_tokens=4096,
                    system=REWRITE_PROMPT,
                    messages=[{"role": "user", "content": user}],
                    timeout=REWRITE_TIMEOUT_S,
                )
                if getattr(resp, "stop_reason", None) == "max_tokens":
                    log.warning("Rewrite hit max_tokens — keeping the original")
                    return None
                out = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                )
            else:
                data = self._post_openai(
                    {
                        "model": self.cfg.cleanup_model,
                        "max_tokens": 4096,
                        "messages": [
                            {"role": "system", "content": REWRITE_PROMPT},
                            {"role": "user", "content": user},
                        ],
                    },
                    timeout=REWRITE_TIMEOUT_S,
                )
                choice = data["choices"][0]
                if _was_truncated(choice):
                    return None
                out = choice["message"]["content"] or ""
        except Exception as e:
            log.warning("Rewrite failed (%s: %s) — keeping the original",
                        type(e).__name__, e)
            return None
        out = out.strip()
        return out or None
```

Note `flow/transform.py` imports AppKit at module level; `cleanup.rewrite` imports it lazily so `cleanup` stays importable without AppKit.

- [ ] **Step 4: Run full suite — PASS.**

- [ ] **Step 5: Commit** — `Add selection grabbing and the LLM rewrite call` (+ trailer).

---

### Task 8: Rewrite path in the pipeline, history kind, badge

**Files:**
- Modify: `flow/pipeline.py` (`_process` dispatch, `_process_rewrite`, `_finish_recording` overlay for command), `flow/store.py` (`kind` column + migration, `add_dictation(kind=...)`, `history()`, `insights()` filter), `flow/window.py` (pass `kind` to the UI), `flow/ui/app.js` (badge), `flow/ui/style.css` (`.ebadge`)
- Test: `tests/test_pipeline.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: Task 7 (`transform.grab_selection`, `Cleaner.rewrite`), Task 5 (`overlay.flash`, `overlay.hide`), Task 6 (queued mode `"command"`).
- Produces: `Store.add_dictation(raw, text, duration_s, app, corrected_words, dict_fixes, kind="dictation")`; history rows contain `"kind"`.

- [ ] **Step 1: Failing store tests** (`tests/test_store.py`, append; use a temp dir like the existing tests)

```python
import sqlite3


def _store(tmp):
    return Store(db_path=Path(tmp) / "store.db")


def test_kind_column_migrates_an_existing_db():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "store.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, ts TEXT, raw TEXT, text TEXT,"
                    " pinned INTEGER DEFAULT 0, app TEXT, words INTEGER, duration_s REAL,"
                    " corrected_words INTEGER DEFAULT 0, dict_fixes INTEGER DEFAULT 0)")
        con.execute("INSERT INTO history (ts, raw, text, words, duration_s) VALUES"
                    " ('2026-10-01T10:00:00+00:00', 'a', 'a', 1, 1.0)")
        con.commit()
        con.close()
        s = Store(db_path=db)
        Store(db_path=db)  # second open: migration must be idempotent
        assert s.history()[0]["kind"] == "dictation"


def test_insights_ignore_rewrites():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.add_dictation("r", "one two three", 1.0, "Notes", 0, 0)
        s.add_dictation("make it formal", "a b c d e f g h", 1.0, "Mail", 0, 0, kind="rewrite")
        ins = s.insights()
        assert ins["totalWords"] == 3
        kinds = [e["kind"] for e in s.history()]
        assert sorted(kinds) == ["dictation", "rewrite"]
```

(Check the actual key name for total words returned by `insights()` — `window.py` reads `insights.get("totalWords")` — and use it.)

- [ ] **Step 2: Run — FAIL. Implement store changes:**

In `_init_schema` after `executescript(...)`, before `commit()`:

```python
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(history)")}
        if "kind" not in cols:
            # Existing rows are all dictations — the DEFAULT backfills them.
            self._conn.execute(
                "ALTER TABLE history ADD COLUMN kind TEXT DEFAULT 'dictation'"
            )
```

`add_dictation(..., kind="dictation")` inserts `kind`. `history()` selects `kind` and returns `"kind": r["kind"] or "dictation"`. `insights()` query gets `WHERE COALESCE(kind, 'dictation') = 'dictation'`. Also check `purge_old` and any other aggregate (`grep -n "FROM history" flow/store.py`) — only `insights` aggregates.

`flow/window.py` `getHome` entries: add `"kind": e.get("kind", "dictation")`.

`flow/ui/app.js` `entryCard`: replace the emeta line with

```js
          ${e.app || e.kind === "rewrite"
            ? `<div class="emeta">${e.kind === "rewrite" ? `<span class="ebadge">Rewrite</span>` : ""}${e.app ? `<span class="eapp">${esc(e.app)}</span>` : ""}</div>`
            : ""}
```

`flow/ui/style.css` after `.entry .eapp`:

```css
.entry .ebadge {
  display: inline-block; margin-right: 6px; padding: 1px 7px;
  border-radius: 999px; font-size: 11px; font-weight: 600;
  background: var(--accent-soft); color: var(--accent-ink);
}
```

- [ ] **Step 3: Failing pipeline tests** (add to `tests/test_pipeline.py`)

```python
class RewritePathTests(unittest.TestCase):
    def setUp(self):
        self.p = _build_pipeline()
        self.p.engine = type("E", (), {"transcribe": lambda s, a, initial_prompt=None: "mach Stichpunkte"})()
        self.p.cleaner = mock.Mock()
        self.p.cleaner.available = True
        self.p.cleaner.rewrite.return_value = "- a\n- b"
        self.store = _FakeStore()
        self.store.add_dictation = mock.Mock()
        patch("flow.pipeline.get_store", return_value=self.store).start()
        self.grab = patch("flow.pipeline.transform.grab_selection", return_value="a b").start()
        self.insert = patch("flow.pipeline.insert_text").start()
        patch("flow.pipeline._frontmost_app", return_value="Notes").start()
        self.addCleanup(patch.stopall)

    def _run(self):
        self.p._process(np.zeros(16000, dtype=np.float32), time.monotonic(), "command")

    def test_success_pastes_rewrite_and_records_it(self):
        self._run()
        self.p.cleaner.rewrite.assert_called_once_with("a b", "mach Stichpunkte")
        self.insert.assert_called_once_with("- a\n- b", mode="paste")
        self.assertEqual(self.store.add_dictation.call_args.kwargs["kind"], "rewrite")

    def test_no_selection_pastes_nothing(self):
        self.grab.return_value = None
        self._run()
        self.insert.assert_not_called()
        self.assertIn(("Select some text first", 2.0), self.p.overlay.flashes)

    def test_failed_rewrite_pastes_nothing(self):
        self.p.cleaner.rewrite.return_value = None
        self._run()
        self.insert.assert_not_called()
        self.assertIn(("Rewrite failed", 2.0), self.p.overlay.flashes)

    def test_empty_instruction_pastes_nothing(self):
        self.p.engine = type("E", (), {"transcribe": lambda s, a, initial_prompt=None: ""})()
        self._run()
        self.grab.assert_not_called()
        self.assertIn(("Didn't catch that", 2.0), self.p.overlay.flashes)

    def test_no_api_key_pastes_nothing(self):
        self.p.cleaner.available = False
        self._run()
        self.grab.assert_not_called()
        self.assertIn(("Rewrite needs an API key", 2.0), self.p.overlay.flashes)

    def test_dictation_mode_still_uses_cleanup_path(self):
        self.p._process(np.zeros(16000, dtype=np.float32), time.monotonic(), "dictation")
        self.p.cleaner.rewrite.assert_not_called()
```

(`_Overlay.flash` from Task 6 appends `(message, seconds)` tuples to `self.flashes`.)

- [ ] **Step 4: Run — FAIL. Implement in `flow/pipeline.py`:**

Import `from . import transform`. In `_finish_recording`, for a non-discarded command recording replace `self.overlay.hide()` with `self.overlay.flash("Rewriting…", seconds=None)` (all other modes keep `hide()`; the timed-out and silent paths still `hide()`). In `_process`:

```python
    def _process(self, captured, released_at, mode="dictation"):
        if mode == "command":
            self._process_rewrite(captured, released_at)
            return
        ...  # existing body unchanged
```

```python
    def _process_rewrite(self, captured, released_at):
        """Rewrite mode: the recording is an instruction for the selection."""
        instruction = self.engine.transcribe(captured).strip()
        log.info("Rewrite instruction (%.2fs): %d chars",
                 time.monotonic() - released_at, len(instruction))
        if len(instruction) < 2:
            self.overlay.flash("Didn't catch that", 2.0)
            return
        if not self.cleaner.available:
            self.overlay.flash("Rewrite needs an API key", 2.0)
            return
        selection = transform.grab_selection()
        if not selection:
            self.overlay.flash("Select some text first", 2.0)
            return
        result = self.cleaner.rewrite(selection, instruction)
        if not result:
            self.overlay.flash("Rewrite failed", 2.0)
            return
        self.overlay.hide()
        try:
            get_store().add_dictation(
                instruction, result,
                duration_s=len(captured) / self.recorder.samplerate,
                app=_frontmost_app(), corrected_words=0, dict_fixes=0,
                kind="rewrite",
            )
        except Exception:
            log.exception("Storing rewrite failed")
        insert_text(result, mode=self.cfg.insert_mode)
        log.info("Rewrote %d -> %d chars after %.2fs total",
                 len(selection), len(result), time.monotonic() - released_at)
```

Note `insert_text` is called positionally with `mode=` as keyword — the test asserts `insert_text("- a\n- b", mode="paste")`.

- [ ] **Step 5: Run full suite — PASS.**

- [ ] **Step 6: Commit** — `Rewrite the selection with fn + Control` (+ trailer).

---

### Task 9: Build, install, and verify end to end

**Files:** none modified unless a defect is found (fix it in the owning module with a test, then continue).

- [ ] **Step 1:** Full suite: `.venv/bin/python -m pytest -q` — all pass; report the count.
- [ ] **Step 2:** Build: `./packaging/build.sh` — must finish without errors and produce `dist/Bomboclat Flow.app`.
- [ ] **Step 3:** Install (the memory gotchas apply): `pkill -f BomboclatFlow; sleep 1; hdiutil info | grep -q BomboclatFlow && echo "eject DMG first"`; `rm -rf "/Applications/Bomboclat Flow.app" && ditto "dist/Bomboclat Flow.app" "/Applications/Bomboclat Flow.app"`; `stat -f '%Sm' "/Applications/Bomboclat Flow.app"`; `ls "/Applications/Bomboclat Flow.app/Contents/Frameworks" | grep -c mlx_whisper` (≥ 1); `ls "/Applications/Bomboclat Flow.app/Contents/Resources/flow/ui/assets" 2>/dev/null || find "/Applications/Bomboclat Flow.app" -name rec_handsfree.wav` (must be found).
- [ ] **Step 4:** Add the new key to the live config (the bundled config only seeds on first launch): append `handsfree_max_s = 600` under `[audio]` in `~/Library/Application Support/whisper-flow/config.toml` if absent (the code default covers it, so this is documentation only).
- [ ] **Step 5:** `open "/Applications/Bomboclat Flow.app"`; wait for `Engine ready` in `~/Library/Logs/whisper-flow.log`.
- [ ] **Step 6:** Hand the manual UAT checklist to the user (the orchestrator does this):
  1. Hold fn, speak, release → text pasted; log `STT (~1.0–1.5s)`.
  2. Double-tap fn → three-note chime, green pill with 🔒, pulsing border, menu bar 🔒; speak; release does nothing; tap fn → text pasted.
  3. Single quick tap of fn → nothing pasted.
  4. Select a paragraph in Notes, hold fn+Control → "What should I do with it?"; say "Mach daraus Stichpunkte"; release → "Rewriting…" → replaced; Cmd+Z restores. Repeat in a browser text field and in VS Code.
  5. fn+Control with nothing selected → "Select some text first", clipboard unchanged (paste something afterwards to check).
  6. History shows the rewrite with a "Rewrite" badge; Insights totals unchanged by it.
