"""Transcript cleanup via an LLM.

Supports two providers:
  - "anthropic": Claude via the official SDK (native Messages API).
  - "openrouter" (or any OpenAI-compatible endpoint): a plain HTTPS POST to
    /chat/completions, so no extra dependency is needed.

Removes filler words, fixes punctuation/casing, preserves language.
Degrades gracefully: any failure (no key, no network, timeout, bad model)
returns the raw transcript unchanged.
"""

import json
import logging
import os
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "whisper-flow"


@dataclass
class CleanResult:
    text: str
    new_terms: list[str]


def _ssl_context() -> ssl.SSLContext:
    """CA bundle from certifi — the python.org framework build ships without
    configured system roots, so the default context fails to verify HTTPS."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

# provider -> (keychain account, env var) used to locate the API key.
_KEY_SOURCES = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
}

SYSTEM_PROMPT = """You clean up raw speech-to-text transcripts for dictation. Return ONLY the cleaned text — no preamble, no quotes, no commentary.

Rules:
- Remove filler words (um, uh, äh, ähm, like, you know, also as filler), false starts, and stutters.
- Fix punctuation, capitalization, and obvious transcription errors.
- Preserve the meaning, tone, and LANGUAGE of the input exactly — German stays German, English stays English, mixed stays mixed.
- Foreign-language words and inserts (Anglicisms, Spanish/English expressions, or any other embedded language) must be kept verbatim — never germanized, translated, or replaced by a similar-sounding word of the main language.
- Apply spoken formatting commands literally: "new line"/"neue Zeile" becomes a line break, "new paragraph"/"neuer Absatz" becomes a paragraph break, spoken punctuation like "comma"/"Komma" becomes the punctuation mark.
- Structure enumerations as lists: when the speaker clearly enumerates parallel items ("erstens ... zweitens ...", "first ... second ...", "Punkt eins ... Punkt zwei ...") or explicitly asks for a list ("Aufzählung", "Liste", "Bullet Points"), put each item on its own line prefixed with "- ". Use "1. ", "2. " instead when the speaker numbers the items. Keep any introductory sentence as a normal line above the list. Ordinary prose without a clear enumeration must NEVER be reformatted into a list.
- Never answer questions contained in the transcript. Never add content. If the transcript is already clean, return it unchanged."""

VOCAB_PROMPT = """The user has a personal vocabulary. If a transcript token is phonetically similar to a vocabulary term but misspelled or split, replace it with the exact term. Match on sound, not spelling. Only correct clear phonetic matches; never insert vocabulary terms that were not spoken. A correctly transcribed word from another language is not a mis-transcription — do not "correct" it into a vocabulary term just because it sounds foreign.
VOCABULARY: {terms}"""

TERM_DETECTION_PROMPT = """Also return new_terms: proper nouns, brand/product names, technical terms or neologisms in the transcript that are NOT in the vocabulary and NOT common dictionary words; empty array if none. Include foreign-language words or expressions the user speaks (in any language, not just English) when they function as a distinct term worth remembering."""

# Anthropic has no response_format kwarg; forcing this tool is the
# equivalent way to get {cleaned_text, new_terms} structured output.
_ANTHROPIC_TOOL = {
    "name": "submit_cleanup",
    "description": "Submit the cleaned transcript and any newly detected vocabulary terms.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cleaned_text": {"type": "string"},
            "new_terms": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["cleaned_text", "new_terms"],
    },
}

# JSON schema forcing {cleaned_text, new_terms} structured output.
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "cleanup",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "cleaned_text": {"type": "string"},
                "new_terms": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["cleaned_text", "new_terms"],
            "additionalProperties": False,
        },
    },
}


def _build_system_prompt(vocabulary=None, want_terms: bool = False) -> str:
    """SYSTEM_PROMPT plus optional vocabulary block and term-detection ask."""
    prompt = SYSTEM_PROMPT
    if vocabulary:
        terms = ", ".join(str(t) for t in vocabulary)
        prompt += "\n\n" + VOCAB_PROMPT.format(terms=terms)
    if want_terms:
        prompt += "\n\n" + TERM_DETECTION_PROMPT
    return prompt


def _keychain_key(account: str) -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        log.exception("Keychain lookup failed")
    return None


def load_api_key(provider: str) -> str | None:
    """Find the API key for a provider: env var first, then Keychain."""
    account, env_var = _KEY_SOURCES.get(provider, _KEY_SOURCES["anthropic"])
    return os.environ.get(env_var) or _keychain_key(account)


def store_api_key(provider: str, key: str) -> None:
    """Write an API key to the Keychain.

    The key goes in over stdin rather than as `-w <key>`: process arguments
    are readable by any other process on this Mac (ps, sysctl), so passing a
    secret there would undo the point of using the Keychain. `security`
    prompts for the value and then for a confirmation, hence twice.

    Raises subprocess.CalledProcessError when the write fails.
    """
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", KEYCHAIN_SERVICE, "-a", provider, "-w"],
        input=f"{key}\n{key}\n", text=True,
        check=True, capture_output=True, timeout=5,
    )


# Providers disagree on the wording: OpenAI says "length", Gemini and some
# OpenRouter passthroughs say "max_tokens".
_TRUNCATED_REASONS = {"length", "max_tokens"}


def _was_truncated(choice: dict) -> bool:
    """Whether the provider cut the response off at the token limit. The
    partial text is a prefix of the dictation, so pasting it would silently
    drop the tail."""
    if str(choice.get("finish_reason") or "").lower() not in _TRUNCATED_REASONS:
        return False
    log.warning(
        "Cleanup response hit the token limit — using the raw transcript "
        "rather than a truncated one"
    )
    return True


def _strip_code_fence(content: str) -> str:
    """Unwrap ```json … ``` — models add it even when asked for raw JSON."""
    if not content.startswith("```"):
        return content
    body = content.split("\n", 1)[1] if "\n" in content else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def _looks_like_json(content: str) -> bool:
    """Whether content was *meant* to be our JSON envelope. Used to tell a
    broken structured reply from a provider that ignored response_format and
    answered in prose."""
    return (
        content[:1] in ("{", "[")
        or "cleaned_text" in content
        or "new_terms" in content
    )


REWRITE_TIMEOUT_S = 10.0

REWRITE_PROMPT = """You rewrite text according to the user's spoken instruction. Return ONLY the rewritten text — no preamble, no quotes, no commentary, no code fences.

Rules:
- Follow the instruction (for example: bullet points, more formal, shorter, as a poem, translate).
- Keep the language of the original text unless the instruction asks for a different language.
- Preserve the meaning unless the instruction asks to change it.
- The instruction was transcribed from speech and may contain recognition errors; interpret it charitably."""


class Cleaner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.provider = cfg.cleanup_provider
        self._client = None      # anthropic SDK client, when provider="anthropic"
        self._api_key = None     # bearer token, for OpenAI-compatible providers
        key = load_api_key(self.provider)
        if not key:
            account, env_var = _KEY_SOURCES.get(
                self.provider, _KEY_SOURCES["anthropic"]
            )
            log.warning(
                "No %s API key found (Keychain service %r account %r, or %s) "
                "— cleanup disabled, using raw transcripts",
                self.provider, KEYCHAIN_SERVICE, account, env_var,
            )
            return
        if self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=key,
                timeout=cfg.cleanup_timeout_s,
                max_retries=0,
            )
        else:
            self._api_key = key

    @property
    def available(self) -> bool:
        return self._client is not None or self._api_key is not None

    def clean(self, text: str, vocabulary=None) -> CleanResult:
        """Return a CleanResult, or the raw text (new_terms=[]) on any failure."""
        if not text or not self.available:
            return CleanResult(text, [])
        words = len(text.split())
        if words < self.cfg.cleanup_min_words:
            return CleanResult(text, [])
        # A long hands-free dictation outgrows the fixed limits: the reply
        # would be cut off (and the raw transcript pasted) or time out.
        # Up to ~120 words / ~800 words these stay at their old values.
        timeout = max(self.cfg.cleanup_timeout_s, 3.0 + words / 60.0)
        max_tokens = min(8192, max(2048, int(words * 2.5)))
        try:
            if self._client is not None:
                return self._clean_anthropic(
                    text, vocabulary, timeout=timeout, max_tokens=max_tokens
                )
            return self._clean_openai_compatible(
                text, vocabulary, timeout=timeout, max_tokens=max_tokens
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            log.warning("Cleanup HTTP %s — using raw transcript: %s",
                        e.code, body)
            return CleanResult(text, [])
        except Exception as e:
            log.warning("Cleanup failed (%s: %s) — using raw transcript",
                        type(e).__name__, e)
            return CleanResult(text, [])

    def _clean_anthropic(self, text: str, vocabulary=None, timeout=None,
                         max_tokens=2048) -> CleanResult:
        system = _build_system_prompt(vocabulary, want_terms=True)
        resp = self._client.messages.create(
            model=self.cfg.cleanup_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": text}],
            tools=[_ANTHROPIC_TOOL],
            tool_choice={"type": "tool", "name": "submit_cleanup"},
            timeout=timeout or self.cfg.cleanup_timeout_s,
        )
        return self._parse_anthropic_response(resp, text)

    @staticmethod
    def _parse_anthropic_response(resp, text: str) -> CleanResult:
        """resp.content normally holds a forced submit_cleanup tool_use block.
        If the model ignored tool forcing (or the SDK response is shaped
        unexpectedly), fall back to any plain text block, then to the raw
        transcript — cleanup must never break dictation."""
        if getattr(resp, "stop_reason", None) == "max_tokens":
            # The tool_use JSON is cut off mid-value. cleaned_text then holds
            # a prefix of the dictation, and since it is non-empty the
            # `cleaned or text` fallback below would happily paste it — the
            # user silently loses the tail of what they said.
            log.warning(
                "Cleanup response hit max_tokens — using the raw transcript "
                "rather than a truncated one"
            )
            return CleanResult(text, [])
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                obj = getattr(block, "input", None) or {}
                cleaned = (obj.get("cleaned_text") or "").strip()
                raw_terms = obj.get("new_terms") or []
                terms = ([str(t) for t in raw_terms if isinstance(t, str)]
                         if isinstance(raw_terms, list) else [])
                return CleanResult(cleaned or text, terms)
        plain = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), ""
        ).strip()
        return CleanResult(plain or text, [])

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

    def _post_openai(self, body: dict, timeout=None) -> dict:
        req = urllib.request.Request(
            self.cfg.cleanup_base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=timeout or self.cfg.cleanup_timeout_s, context=_ssl_context()
        ) as resp:
            return json.loads(resp.read())

    def _clean_openai_compatible(self, text: str, vocabulary=None, timeout=None,
                                 max_tokens=2048) -> CleanResult:
        timeout = timeout or self.cfg.cleanup_timeout_s
        base_body = {
            "model": self.cfg.cleanup_model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system",
                 "content": _build_system_prompt(vocabulary, want_terms=True)},
                {"role": "user", "content": text},
            ],
        }
        try:
            data = self._post_openai({**base_body,
                                      "response_format": _RESPONSE_FORMAT},
                                     timeout=timeout)
        except urllib.error.HTTPError as e:
            # A 400 typically means the provider rejected response_format.
            # Retry once as a plain (unstructured) request, no term detection.
            if e.code != 400:
                raise
            body = e.read().decode(errors="replace")[:500]
            log.warning("Cleanup structured HTTP 400 — retrying without "
                        "response_format: %s", body)
            plain_body = dict(base_body)
            plain_body["messages"] = [
                {"role": "system",
                 "content": _build_system_prompt(vocabulary, want_terms=False)},
                {"role": "user", "content": text},
            ]
            data = self._post_openai(plain_body, timeout=timeout)
            choice = data["choices"][0]
            if _was_truncated(choice):
                return CleanResult(text, [])
            content = (choice["message"]["content"] or "").strip()
            return CleanResult(content or text, [])

        choice = data["choices"][0]
        if _was_truncated(choice):
            return CleanResult(text, [])
        content = choice["message"]["content"] or ""
        return self._parse_content(content, text)

    @staticmethod
    def _parse_content(content: str, text: str) -> CleanResult:
        """Parse the structured reply, or fall back to the raw transcript.

        Anything that was meant to be our JSON envelope but cannot be parsed
        into it must never reach the user's cursor. Returning the content
        verbatim once pasted a half-written '{\\n  "cleaned_' into a
        document: the model's reply had been cut short and the provider did
        not report it in finish_reason, so _was_truncated could not catch it.

        A reply that does not look like JSON at all is a different case — the
        provider ignored response_format and answered in prose, which is
        still a perfectly good cleaned transcript.
        """
        content = _strip_code_fence(content.strip())
        if not content:
            return CleanResult(text, [])
        try:
            obj = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            if _looks_like_json(content):
                log.warning(
                    "Cleanup returned unparseable JSON (%d chars) — using the "
                    "raw transcript", len(content),
                )
                log.debug("Unparseable cleanup content: %r", content[:500])
                return CleanResult(text, [])
            return CleanResult(content, [])
        if isinstance(obj, dict) and "cleaned_text" in obj:
            cleaned = (obj.get("cleaned_text") or "").strip()
            raw_terms = obj.get("new_terms") or []
            terms = ([str(t) for t in raw_terms if isinstance(t, str)]
                     if isinstance(raw_terms, list) else [])
            return CleanResult(cleaned or text, terms)
        # Valid JSON, wrong shape — still not something to paste.
        log.warning("Cleanup returned JSON without cleaned_text — using the "
                    "raw transcript")
        return CleanResult(text, [])
