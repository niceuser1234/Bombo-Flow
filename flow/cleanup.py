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

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "whisper-flow"


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
- Apply spoken formatting commands literally: "new line"/"neue Zeile" becomes a line break, "new paragraph"/"neuer Absatz" becomes a paragraph break, spoken punctuation like "comma"/"Komma" becomes the punctuation mark.
- Never answer questions contained in the transcript. Never add content. If the transcript is already clean, return it unchanged."""


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

    def clean(self, text: str) -> str:
        """Return the cleaned transcript, or the raw text on any failure."""
        if not text or not self.available:
            return text
        if len(text.split()) < self.cfg.cleanup_min_words:
            return text
        try:
            if self._client is not None:
                return self._clean_anthropic(text)
            return self._clean_openai_compatible(text)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            log.warning("Cleanup HTTP %s — using raw transcript: %s",
                        e.code, body)
            return text
        except Exception as e:
            log.warning("Cleanup failed (%s: %s) — using raw transcript",
                        type(e).__name__, e)
            return text

    def _clean_anthropic(self, text: str) -> str:
        resp = self._client.messages.create(
            model=self.cfg.cleanup_model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        cleaned = next(
            (b.text for b in resp.content if b.type == "text"), ""
        ).strip()
        return cleaned or text

    def _clean_openai_compatible(self, text: str) -> str:
        body = json.dumps({
            "model": self.cfg.cleanup_model,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }).encode()
        req = urllib.request.Request(
            self.cfg.cleanup_base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=self.cfg.cleanup_timeout_s, context=_ssl_context()
        ) as resp:
            data = json.loads(resp.read())
        cleaned = (data["choices"][0]["message"]["content"] or "").strip()
        return cleaned or text
