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
        if len(text.split()) < self.cfg.cleanup_min_words:
            return CleanResult(text, [])
        try:
            if self._client is not None:
                return self._clean_anthropic(text, vocabulary)
            return self._clean_openai_compatible(text, vocabulary)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            log.warning("Cleanup HTTP %s — using raw transcript: %s",
                        e.code, body)
            return CleanResult(text, [])
        except Exception as e:
            log.warning("Cleanup failed (%s: %s) — using raw transcript",
                        type(e).__name__, e)
            return CleanResult(text, [])

    def _clean_anthropic(self, text: str, vocabulary=None) -> CleanResult:
        system = _build_system_prompt(vocabulary, want_terms=True)
        resp = self._client.messages.create(
            model=self.cfg.cleanup_model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": text}],
            tools=[_ANTHROPIC_TOOL],
            tool_choice={"type": "tool", "name": "submit_cleanup"},
        )
        return self._parse_anthropic_response(resp, text)

    @staticmethod
    def _parse_anthropic_response(resp, text: str) -> CleanResult:
        """resp.content normally holds a forced submit_cleanup tool_use block.
        If the model ignored tool forcing (or the SDK response is shaped
        unexpectedly), fall back to any plain text block, then to the raw
        transcript — cleanup must never break dictation."""
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

    def _post_openai(self, body: dict) -> dict:
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
            req, timeout=self.cfg.cleanup_timeout_s, context=_ssl_context()
        ) as resp:
            return json.loads(resp.read())

    def _clean_openai_compatible(self, text: str, vocabulary=None) -> CleanResult:
        base_body = {
            "model": self.cfg.cleanup_model,
            "max_tokens": 2048,
            "messages": [
                {"role": "system",
                 "content": _build_system_prompt(vocabulary, want_terms=True)},
                {"role": "user", "content": text},
            ],
        }
        try:
            data = self._post_openai({**base_body,
                                      "response_format": _RESPONSE_FORMAT})
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
            data = self._post_openai(plain_body)
            content = (data["choices"][0]["message"]["content"] or "").strip()
            return CleanResult(content or text, [])

        content = data["choices"][0]["message"]["content"] or ""
        return self._parse_content(content, text)

    @staticmethod
    def _parse_content(content: str, text: str) -> CleanResult:
        """Content may be a JSON {cleaned_text, new_terms} object or plain text."""
        content = content.strip()
        if not content:
            return CleanResult(text, [])
        try:
            obj = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return CleanResult(content, [])
        if isinstance(obj, dict) and "cleaned_text" in obj:
            cleaned = (obj.get("cleaned_text") or "").strip()
            raw_terms = obj.get("new_terms") or []
            terms = ([str(t) for t in raw_terms if isinstance(t, str)]
                     if isinstance(raw_terms, list) else [])
            return CleanResult(cleaned or text, terms)
        # Valid JSON but not our shape — fall back to treating it as text.
        return CleanResult(content, [])
