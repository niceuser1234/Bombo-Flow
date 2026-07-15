import unittest

from flow.cleanup import Cleaner, _build_system_prompt


class SystemPromptTests(unittest.TestCase):
    def test_foreign_word_rule_present(self):
        prompt = _build_system_prompt()
        self.assertIn("never germanized", prompt)

    def test_vocab_prompt_excludes_correct_foreign_words(self):
        prompt = _build_system_prompt(vocabulary=["Kubernetes"])
        self.assertIn("VOCABULARY: Kubernetes", prompt)
        self.assertIn("not a mis-transcription", prompt)

    def test_term_detection_included_when_want_terms(self):
        prompt = _build_system_prompt(want_terms=True)
        self.assertIn("new_terms", prompt)
        self.assertIn("foreign-language words", prompt)

    def test_term_detection_absent_by_default(self):
        prompt = _build_system_prompt()
        self.assertNotIn("new_terms", prompt)


class _Block:
    def __init__(self, type_, text=None, input=None):
        self.type = type_
        self.text = text
        self.input = input


class _Resp:
    def __init__(self, content):
        self.content = content


class _Config:
    cleanup_model = "claude-haiku-test"
    cleanup_timeout_s = 5.0
    cleanup_min_words = 4


def _anthropic_cleaner(client):
    cleaner = Cleaner.__new__(Cleaner)
    cleaner.cfg = _Config()
    cleaner.provider = "anthropic"
    cleaner._client = client
    cleaner._api_key = None
    return cleaner


class AnthropicParseTests(unittest.TestCase):
    def test_tool_use_block_extracts_cleaned_text_and_terms(self):
        resp = _Resp([
            _Block("tool_use", input={
                "cleaned_text": "Hallo Welt",
                "new_terms": ["Kubernetes"],
            }),
        ])
        result = Cleaner._parse_anthropic_response(resp, "raw fallback")
        self.assertEqual(result.text, "Hallo Welt")
        self.assertEqual(result.new_terms, ["Kubernetes"])

    def test_plain_text_block_falls_back_when_tool_forcing_ignored(self):
        resp = _Resp([_Block("text", text="Hallo Welt")])
        result = Cleaner._parse_anthropic_response(resp, "raw fallback")
        self.assertEqual(result.text, "Hallo Welt")
        self.assertEqual(result.new_terms, [])

    def test_empty_content_falls_back_to_raw_text(self):
        resp = _Resp([])
        result = Cleaner._parse_anthropic_response(resp, "raw fallback")
        self.assertEqual(result.text, "raw fallback")
        self.assertEqual(result.new_terms, [])

    def test_tool_use_with_missing_cleaned_text_falls_back_to_raw(self):
        resp = _Resp([_Block("tool_use", input={"new_terms": ["X"]})])
        result = Cleaner._parse_anthropic_response(resp, "raw fallback")
        self.assertEqual(result.text, "raw fallback")
        self.assertEqual(result.new_terms, ["X"])

    def test_clean_anthropic_requests_forced_tool_with_term_detection(self):
        captured = {}

        class _Messages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _Resp([_Block("tool_use", input={
                    "cleaned_text": "cleaned",
                    "new_terms": [],
                })])

        class _Client:
            messages = _Messages()

        cleaner = _anthropic_cleaner(_Client())
        result = cleaner.clean("some raw transcript text here", vocabulary=["Foo"])

        self.assertEqual(result.text, "cleaned")
        self.assertEqual(captured["tool_choice"], {"type": "tool", "name": "submit_cleanup"})
        self.assertEqual(len(captured["tools"]), 1)
        self.assertIn("new_terms", captured["system"])

    def test_clean_anthropic_survives_malformed_response(self):
        """Any exception while talking to/parsing the SDK response must
        degrade to the raw transcript, never raise out of clean()."""

        class _Messages:
            def create(self, **kwargs):
                raise RuntimeError("boom")

        class _Client:
            messages = _Messages()

        cleaner = _anthropic_cleaner(_Client())
        result = cleaner.clean("some raw transcript text here", vocabulary=["Foo"])

        self.assertEqual(result.text, "some raw transcript text here")
        self.assertEqual(result.new_terms, [])


if __name__ == "__main__":
    unittest.main()
