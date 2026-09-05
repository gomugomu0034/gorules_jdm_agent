"""Best-effort scrubbing for a corpus that is about to leave the machine.

Deliberately narrow. The corpus records business policies, which are *made of* numbers -
thresholds, tiers, amounts, dates - and a redaction pass aggressive enough to catch every
phone number would destroy the very content that makes the data worth training on. So this
removes only shapes that are unambiguously a secret or an identifier and could not be part
of a policy: addresses, key-shaped tokens, card-length digit runs, credentials in URLs.

Phone numbers are deliberately *not* matched. Every pattern general enough to catch one
also catches "free shipping over 5551234" and half the thresholds in the corpus.

This is a reduction in risk, not a guarantee. Read an export before sending it anywhere.
"""

from __future__ import annotations

import re

# Order matters. `user:secret@host` looks like an address to the email rule, so the
# credential rule has to have taken it first.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[url-credentials]", re.compile(r"\b(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")),
    ("[email]", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    # Provider key shapes: a known prefix followed by a long opaque tail. Anchoring on the
    # prefix is what keeps this from matching ordinary identifiers.
    ("[key]", re.compile(
        r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abposr]|AIza|hf|api|key)"
        r"[-_][A-Za-z0-9_\-]{16,}\b", re.IGNORECASE)),
    ("[bearer]", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE)),
    # Card-length runs, optionally spaced or hyphenated. Bounded at 19 so it cannot eat a
    # long identifier, and starting at 13 so it cannot eat a price or a year.
    # Anchored so the run cannot end on a separator: `(?:\d[ -]?){13,19}` swallows the
    # space after the last digit and welds the redaction to the next word.
    ("[number]", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
)


def redact(value):
    """Scrub a string, or every string inside a structure, in place of the original."""
    if isinstance(value, str):
        for replacement, pattern in _PATTERNS:
            if replacement == "[url-credentials]":
                value = pattern.sub(lambda m: f"{m.group('scheme')}[credentials]@", value)
            else:
                value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
