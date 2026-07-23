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


class TruncatedResponseTests(unittest.TestCase):
    """A response cut off at the token limit holds a *prefix* of the
    dictation. Because that prefix is non-empty, the `cleaned or text`
    fallback would paste it and the user would silently lose the tail."""

    RAW = "der anfang des satzes und der ganze lange rest davon"

    def test_anthropic_max_tokens_falls_back_to_the_raw_transcript(self):
        resp = _Resp([
            _Block("tool_use", input={
                "cleaned_text": "Der Anfang des Sat",
                "new_terms": ["Sat"],
            }),
        ])
        resp.stop_reason = "max_tokens"

        result = Cleaner._parse_anthropic_response(resp, self.RAW)

        self.assertEqual(result.text, self.RAW)
        self.assertEqual(result.new_terms, [])

    def test_anthropic_complete_response_is_still_used(self):
        resp = _Resp([
            _Block("tool_use", input={
                "cleaned_text": "Der Anfang des Satzes.",
                "new_terms": [],
            }),
        ])
        resp.stop_reason = "tool_use"

        result = Cleaner._parse_anthropic_response(resp, self.RAW)

        self.assertEqual(result.text, "Der Anfang des Satzes.")

    def test_openai_length_finish_reason_is_treated_as_truncation(self):
        from flow.cleanup import _was_truncated

        self.assertTrue(_was_truncated({"finish_reason": "length"}))
        self.assertFalse(_was_truncated({"finish_reason": "stop"}))
        self.assertFalse(_was_truncated({}))

    def test_gemini_style_max_tokens_reason_is_recognised(self):
        """Gemini and some OpenRouter passthroughs word it differently."""
        from flow.cleanup import _was_truncated

        self.assertTrue(_was_truncated({"finish_reason": "MAX_TOKENS"}))
        self.assertTrue(_was_truncated({"finish_reason": "max_tokens"}))


class ParseContentTests(unittest.TestCase):
    """Nothing that was meant to be our JSON envelope may reach the cursor
    unparsed. Regression for history entry #160, where a reply cut off after
    13 characters was pasted verbatim into the user's document."""

    RAW = ("Okay, was steht jetzt als nächstes an? Jetzt steht der ganz rohe "
           "Prototyp und ich habe ein Bild hochgeladen.")

    def test_truncated_json_falls_back_to_the_raw_transcript(self):
        result = Cleaner._parse_content('{\n  "cleaned_', self.RAW)
        self.assertEqual(result.text, self.RAW)
        self.assertEqual(result.new_terms, [])

    def test_json_without_cleaned_text_falls_back(self):
        result = Cleaner._parse_content('{"something_else": "hallo"}', self.RAW)
        self.assertEqual(result.text, self.RAW)

    def test_partial_json_naming_our_keys_falls_back(self):
        result = Cleaner._parse_content('{"cleaned_text": "Okay, was ste',
                                        self.RAW)
        self.assertEqual(result.text, self.RAW)

    def test_well_formed_json_is_used(self):
        result = Cleaner._parse_content(
            '{"cleaned_text": "Alles gut.", "new_terms": ["Prototyp"]}',
            self.RAW,
        )
        self.assertEqual(result.text, "Alles gut.")
        self.assertEqual(result.new_terms, ["Prototyp"])

    def test_json_wrapped_in_a_code_fence_is_unwrapped(self):
        result = Cleaner._parse_content(
            '```json\n{"cleaned_text": "Alles gut.", "new_terms": []}\n```',
            self.RAW,
        )
        self.assertEqual(result.text, "Alles gut.")

    def test_prose_reply_is_still_accepted(self):
        """A provider that ignores response_format returns a perfectly good
        cleaned transcript as plain text."""
        result = Cleaner._parse_content("Alles gut, der Prototyp steht.",
                                        self.RAW)
        self.assertEqual(result.text, "Alles gut, der Prototyp steht.")

    def test_empty_reply_falls_back(self):
        self.assertEqual(Cleaner._parse_content("   ", self.RAW).text, self.RAW)


class StoreApiKeyTests(unittest.TestCase):
    def test_key_never_appears_in_the_command_line(self):
        """Process arguments are readable by every other process on the Mac,
        so a secret passed as `-w <key>` is exposed for the lifetime of the
        call — which would defeat storing it in the Keychain at all."""
        from unittest.mock import patch

        import flow.cleanup as cleanup_mod

        secret = "sk-super-secret-value"
        with patch.object(cleanup_mod.subprocess, "run") as run:
            cleanup_mod.store_api_key("anthropic", secret)

        argv = run.call_args.args[0]
        self.assertNotIn(secret, argv)
        self.assertIn("-w", argv)
        # security prompts for the value and then for a confirmation.
        self.assertEqual(run.call_args.kwargs["input"], f"{secret}\n{secret}\n")
        self.assertTrue(run.call_args.kwargs["check"])


class ListFormattingPromptTests(unittest.TestCase):
    def test_system_prompt_instructs_list_formatting(self):
        from flow.cleanup import SYSTEM_PROMPT
        self.assertIn('"- "', SYSTEM_PROMPT)
        self.assertIn("erstens", SYSTEM_PROMPT)
        self.assertIn("NEVER be reformatted", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
