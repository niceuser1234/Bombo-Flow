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
