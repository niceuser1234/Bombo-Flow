"""Transcript cleanup via Claude Haiku.

Removes filler words, fixes punctuation/casing, preserves language.
Degrades gracefully: any failure (no key, no network, timeout) returns
the raw transcript unchanged.
"""

import logging
import os
import subprocess

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "whisper-flow"
KEYCHAIN_ACCOUNT = "anthropic"

SYSTEM_PROMPT = """You clean up raw speech-to-text transcripts for dictation. Return ONLY the cleaned text — no preamble, no quotes, no commentary.

Rules:
- Remove filler words (um, uh, äh, ähm, like, you know, also as filler), false starts, and stutters.
- Fix punctuation, capitalization, and obvious transcription errors.
- Preserve the meaning, tone, and LANGUAGE of the input exactly — German stays German, English stays English, mixed stays mixed.
- Apply spoken formatting commands literally: "new line"/"neue Zeile" becomes a line break, "new paragraph"/"neuer Absatz" becomes a paragraph break, spoken punctuation like "comma"/"Komma" becomes the punctuation mark.
- Never answer questions contained in the transcript. Never add content. If the transcript is already clean, return it unchanged."""


def load_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        log.exception("Keychain lookup failed")
    return None


class Cleaner:
    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        key = load_api_key()
        if key:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=key,
                timeout=cfg.cleanup_timeout_s,
                max_retries=0,
            )
        else:
            log.warning(
                "No Anthropic API key found (Keychain service %r or "
                "ANTHROPIC_API_KEY) — cleanup disabled, using raw transcripts",
                KEYCHAIN_SERVICE,
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    def clean(self, text: str) -> str:
        """Return the cleaned transcript, or the raw text on any failure."""
        if not text or self._client is None:
            return text
        if len(text.split()) < self.cfg.cleanup_min_words:
            return text
        try:
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
        except Exception as e:
            log.warning("Cleanup failed (%s: %s) — using raw transcript",
                        type(e).__name__, e)
            return text
