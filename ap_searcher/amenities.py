"""Three-state amenity detection.

  yes     - confirmed by a structured field on the sreality detail page
  maybe   - inferred from the Czech free-text description
  unknown - no evidence either way

Only parking and cellar have structured fields on sreality; dishwasher, induction
hob and bathtub exist solely in ad prose, which is why the distinction is worth
keeping.

Text matching runs on a diacritics-folded copy of the description that preserves
character offsets, so one ASCII pattern matches both "myčka" and "mycka" while the
evidence snippet is still quoted from the original Czech.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

YES, MAYBE, UNKNOWN = "yes", "maybe", "unknown"

KEYS = ("parking", "dishwasher", "induction", "bathtub", "cellar")

LABELS = {
    "parking": "Parking",
    "dishwasher": "Dishwasher",
    "induction": "Induction hob",
    "bathtub": "Bathtub",
    "cellar": "Cellar",
}

# Patterns are written diacritics-free; they run against the folded text.
TEXT_RULES: dict[str, list[str]] = {
    "parking": [
        r"\bparkov\w*",
        r"\bgaraz\w*",
        r"\bcarport\w*",
        # "stani" alone hits street names such as "namesti Povstani" - require context
        r"\b(?:garazov\w+|parkovac\w+|venkovn\w+|kryt\w+|vnejs\w+)\s+stan[ií]\w*",
        r"\bstani\s+(?:v\s+garazi|pro\s+\w+\s+(?:auto|vozy|vozidla))",
    ],
    "dishwasher": [r"\bmyck\w*"],
    "induction": [r"\bindukc\w*", r"\bindukcn\w*"],
    "bathtub": [r"\bvan(?:a|u|y|ou|e|ne)\b", r"\bvanou\b", r"\brohovou?\s+vanu?\b"],
    # Every Czech word for a cellar starts with "sklep" (sklep / sklepa / sklepem /
    # sklepni koje); "sklipek" is the diminutive. "komora" is a pantry, not a cellar.
    "cellar": [r"\bsklep\w*", r"\bsklipek\w*", r"\bsklipk\w*"],
}

# A match preceded by one of these inside NEGATION_WINDOW characters is discarded.
# "nenalezi" (nenáleží - does not belong to the flat) is the standard way an agent
# says a cellar or a parking space is not included. "nejde" is deliberately absent:
# "nejde pouze o parkovaci stani, ale o garaz" denies the wording, not the parking.
NEGATIONS = (r"bez", r"neni", r"nema", r"chybi", r"nejsou", r"nelze", r"nenalezi")
NEGATION_WINDOW = 24
_NEGATION_RE = re.compile(r"\b(?:%s)\b" % "|".join(NEGATIONS))

# Some agents paste the spec table into the description, where a missing amenity reads
# "Sklep: Není". A negation *after* the match is only trusted in that labelled form -
# free prose runs the other way ("parkovani neni problem" means parking is fine), so the
# separator is required.
_TRAILING_NEGATION_RE = re.compile(r"\s*[:\u2013\u2014-]\s*(?:neni|ne|nema|nejsou|nelze)\b")

# Structured detail-page fields that prove the amenity outright.
STRUCTURED_RULES: dict[str, tuple[str, ...]] = {
    "parking": ("garage", "parkingLots", "garageCount", "parking"),
    # `cellar` is a plain bool and `cellarArea` its size in m2. Both are present on
    # every detail page, so a false here is an explicit "no" - but the three states
    # have no slot for that, and a falsy field falls through to the text rules just
    # as parking does. An ad can therefore reach `maybe` against its own checkbox.
    "cellar": ("cellar", "cellarArea"),
}


def fold(text: str) -> str:
    """Lowercase and strip diacritics while keeping character offsets stable."""
    out = []
    for ch in text.lower():
        decomposed = unicodedata.normalize("NFD", ch)
        base = "".join(c for c in decomposed if not unicodedata.combining(c))
        out.append(base[0] if base else ch)
    return "".join(out)


def _snippet(text: str, start: int, end: int, pad: int = 28) -> str:
    left = max(0, start - pad)
    right = min(len(text), end + pad)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return prefix + " ".join(text[left:right].split()) + suffix


def _structured(params: dict[str, Any], key: str) -> str | None:
    for field in STRUCTURED_RULES.get(key, ()):
        value = params.get(field)
        if isinstance(value, dict):  # sreality enum: {"name": ..., "value": n}
            if value.get("value"):
                return f"{field}: {value.get('name')}"
        elif isinstance(value, bool):
            if value:
                return f"{field}: ano"
        elif isinstance(value, (int, float)) and value:
            return f"{field}: {value}"
        elif isinstance(value, str) and value.strip():
            return f"{field}: {value}"
    return None


def _text_match(description: str, folded: str, key: str) -> str | None:
    for pattern in TEXT_RULES.get(key, []):
        for m in re.finditer(pattern, folded):
            window = folded[max(0, m.start() - NEGATION_WINDOW) : m.start()]
            if _NEGATION_RE.search(window):
                continue
            if _TRAILING_NEGATION_RE.match(folded, m.end()):
                continue
            return _snippet(description, m.start(), m.end())
    return None


def detect(description: str | None, params: dict[str, Any] | None = None) -> dict[str, dict]:
    """Return {key: {'state': ..., 'evidence': ...}} for every amenity."""
    description = description or ""
    params = params or {}
    folded = fold(description)
    results: dict[str, dict] = {}
    for key in KEYS:
        evidence = _structured(params, key)
        if evidence:
            results[key] = {"state": YES, "evidence": evidence}
            continue
        snippet = _text_match(description, folded, key)
        if snippet:
            results[key] = {"state": MAYBE, "evidence": snippet}
        else:
            results[key] = {"state": UNKNOWN, "evidence": None}
    return results
