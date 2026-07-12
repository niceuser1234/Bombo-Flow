"""Filter auto-learn candidate terms to keep the personal dictionary clean.

The cleanup LLM proposes new_terms it thinks are proper nouns, brand/product
names, or neologisms. Left unchecked, that stream pollutes the vocabulary with
common words, filler, punctuation, numbers, and over-long phrases — which then
get force-injected back into future cleanup prompts, compounding the noise.

`filter_candidates` is the anti-pollution gate. It normalizes each candidate,
rejects the obvious garbage (too short/long, pure numbers/punctuation, long
phrases, duplicates, words we already know), and — crucially — drops plain
dictionary words. A lowercase single common word never earns a dictionary slot.
The one escape hatch is a strong *shape* signal (internal capitals/camelCase,
digits, or a short all-caps acronym): those are almost always real product or
technical names even when the lowercase form happens to be a dictionary word.
"""

import string

# Lazily loaded once: lowercased set of system dictionary words.
_DICT_WORDS = None

_EDGES = string.whitespace + string.punctuation


def _dict_words() -> set[str]:
    """Load /usr/share/dict/words once. Missing file => everything is unknown."""
    global _DICT_WORDS
    if _DICT_WORDS is None:
        try:
            with open("/usr/share/dict/words", encoding="utf-8",
                      errors="replace") as f:
                _DICT_WORDS = {
                    line.strip().lower() for line in f if line.strip()
                }
        except OSError:
            _DICT_WORDS = set()
    return _DICT_WORDS


def _has_strong_shape(term: str) -> bool:
    """A shape that marks a term as a likely proper noun / technical name."""
    # Digits anywhere (n8n, s3, gpt4).
    if any(c.isdigit() for c in term):
        return True
    # All-caps acronym of 2-6 chars (HTTP, API, SQL).
    if 2 <= len(term) <= 6 and term.isupper() and any(c.isalpha() for c in term):
        return True
    # Internal capitals / camelCase (LangGraph, PyTorch, iPhone).
    if any(c.isupper() for c in term[1:]):
        return True
    return False


def filter_candidates(terms: list[str], known_words: list[str]) -> list[str]:
    """Return the subset of `terms` worth adding to the personal dictionary."""
    dict_words = _dict_words()
    seen = {w.strip().lower() for w in known_words if w and w.strip()}
    accepted: list[str] = []

    for raw in terms:
        if not isinstance(raw, str):
            continue
        term = raw.strip().strip(_EDGES).strip()
        # Length bounds.
        if len(term) < 2 or len(term) > 30:
            continue
        # Pure numbers / punctuation (no letters at all).
        if not any(c.isalpha() for c in term):
            continue
        # Multi-word phrases longer than 3 words.
        if len(term.split()) > 3:
            continue
        # Case-insensitive dedupe within batch and against known words.
        key = term.lower()
        if key in seen:
            continue
        # Common dictionary word — reject unless it has a strong shape signal.
        if key in dict_words and not _has_strong_shape(term):
            continue
        seen.add(key)
        accepted.append(term)

    return accepted
