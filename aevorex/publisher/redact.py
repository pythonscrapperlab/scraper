"""Strip contact details from free text before it reaches the cloud serving cache.

Listing descriptions and AI summaries are readable by every plan, while agent contact data is
Pro-only (``serving.agents``). Text that names or dials the listing agent would bypass that
boundary, so the publisher redacts it: phone numbers, e-mail addresses, web addresses, social
handles, and "call/text/contact <person>" phrases. Generic phrases such as "call the listing
agent", "call home" or "contact us" carry no personal data and are left alone.

Known limits (documented in ``docs/schema.md``): spelled-out or obfuscated numbers
("four oh seven ...") and addresses written as "name at example dot com" are not detected.
"""

from __future__ import annotations

import re

REMOVED = "[contact removed]"
GENERIC_AGENT = "contact the listing agent"

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_URL = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.(?:com|net|org|io|co|us|biz|info|realty|homes)\b(?:/\S*)?",
    re.IGNORECASE,
)
_PHONE = re.compile(
    r"(?<![\w$.])(?:\+?1[\s.\-]*)?(?:\(\s*\d{3}\s*\)|\d{3})[\s.\-]*\d{3}[\s.\-]*\d{4}(?![\d])"
)
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_.]{3,}")

_VERB = r"(?i:call|text|contact|e-?mail|phone|ask\s+for|speak\s+(?:to|with)|reach\s+out\s+to)"
_TITLE = r"(?:(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+)?"
_NAME_WORD = r"[A-Z][A-Za-z'’\-]*\.?"
_NAME_PHRASE = re.compile(
    rf"\b{_VERB}\s+(?P<name>{_TITLE}{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}})"
)
# Words that follow these verbs without naming anyone ("call home", "contact us today").
_GENERIC = frozenset(
    "home homes your our my us me you today now the a an listing agent agents broker brokers "
    "office owner owners seller sellers realtor realtors team anytime directly for to at on by "
    "with it this that property info information details showing showings appointment "
    "schedule more soon tonight tomorrow asap first anyone someone email phone text call "
    "contact title company lender lenders builder management la if any before will not "
    "financial video statements photos virtual tour".split()
)
_REPEATED = re.compile(rf"(?:{re.escape(REMOVED)}[\s,;/&]*(?:(?:or|and|at|on)\s+)?)+")


def _name_replacement(match: re.Match[str]) -> str:
    words = match.group("name").replace(".", " ").split()
    words = [w for w in words if w.lower() not in {"mr", "mrs", "ms", "miss", "dr"}] or words
    kept: list[str] = []
    for word in words:
        if len(word.strip(".")) < 2 or word.lower().strip("'’") in _GENERIC:
            break
        kept.append(word)
    if not kept:
        return match.group(0)  # generic phrase: nothing personal to remove
    suffix = " ".join(words[len(kept):])
    # "Call Anna Today" keeps its tail; the person is replaced by the generic agent.
    return GENERIC_AGENT + (f" {suffix}" if suffix else "")


def redact_text(text: str | None) -> str | None:
    """Remove contact details; returns ``None`` for ``None`` and trims surrounding space."""
    if text is None:
        return None
    cleaned = _EMAIL.sub(REMOVED, text)
    cleaned = _URL.sub(REMOVED, cleaned)
    cleaned = _PHONE.sub(REMOVED, cleaned)
    cleaned = _HANDLE.sub(REMOVED, cleaned)
    cleaned = _NAME_PHRASE.sub(_name_replacement, cleaned)
    cleaned = _REPEATED.sub(REMOVED + " ", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def cap_text(text: str | None, limit: int) -> str | None:
    """Cut to ``limit`` characters at a word boundary, ending with an ellipsis."""
    if text is None or len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit - 80:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


def serving_text(text: str | None, limit: int | None) -> str | None:
    """Redact first, then cap, so the cap can never leave half a phone number behind."""
    redacted = redact_text(text)
    if redacted is None:
        return None
    redacted = redacted or None
    return cap_text(redacted, limit) if limit is not None else redacted
