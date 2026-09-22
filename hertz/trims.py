"""Trim ladders, per make: base < mid-low < mid-high < top, as 0 / 1 / 2 / 3.

One ordinal term per make. Manufacturers name trims differently, and the
same word means different rungs on different ladders: Audi's "Premium" is
its base trim, Subaru's is the second rung, Mazda's the third. A single
shared list ("premium" = mid) misplaces two of the three. Each ladder is a
sequence of (phrase, tier) tested in order, so the more specific phrase
("premium plus", "ex-l", "sel premium") is listed before the shorter one it
contains. Matching is whole-word: "s" must not fire inside "sport".

Two consumers. The hedonic and the Cars.com market curve use `trim_tier`
as an ordinal regressor (unknown trims sit mid-ladder at 1.5). The Hertz
sweep uses `is_base_trim` to drop base models, which only fires when the
ladder positively recognises the trim as base: a Lexus with no trim suffix
is unknown, not base, and stays.
"""
from __future__ import annotations

import re

Ladder = tuple[tuple[str, int], ...]

LADDERS: dict[str, Ladder] = {
    # Premium < Premium Plus < Prestige. Premium IS the base trim.
    "audi": (("prestige", 3), ("premium plus", 2), ("premium", 0), ("competition", 3)),
    # 2.5 S < Select < Preferred < Premium < Premium Plus / Signature;
    # CX-90/70: Preferred < Premium Sport / S Premium < Premium Plus.
    "mazda": (("premium plus", 3), ("signature", 3), ("s premium", 2), ("premium sport", 2),
              ("premium", 2), ("carbon", 2), ("meridian", 2), ("grand touring", 2),
              ("preferred", 0), ("select", 0), ("sport", 0), ("s", 0)),
    # Core < Plus < Ultra (older: Momentum < Inscription).
    "volvo": (("ultra", 3), ("ultimate", 3), ("inscription", 3), ("plus", 2),
              ("r-design", 2), ("momentum", 1), ("core", 0)),
    # SE < SEL < SEL Premium / N Line < Limited < Calligraphy. Lorenzo: a
    # Palisade SEL is fine, so SEL is one rung above base, not base.
    "hyundai": (("calligraphy", 3), ("limited", 3), ("sel premium", 2), ("n line", 2),
                ("xrt", 2), ("sel", 1), ("essential", 0), ("se", 0)),
    "kia": (("sx prestige", 3), ("x-pro", 3), ("sx", 3), ("x-line", 2), ("ex", 2),
            ("s", 1), ("lx", 0)),
    "toyota": (("platinum", 3), ("limited", 3), ("trd pro", 3), ("xse", 2), ("xle", 2),
               ("trd", 2), ("se", 1), ("le", 0), ("l", 0)),
    "honda": (("elite", 3), ("touring", 3), ("ex-l", 2), ("sport-l", 2), ("sport touring", 3),
              ("ex", 1), ("sport", 1), ("lx", 0)),
    # Base (no name) < Premium < Sport / Onyx / Wilderness < Limited < Touring.
    "subaru": (("touring", 3), ("limited", 3), ("onyx", 2), ("wilderness", 2), ("sport", 2),
               ("premium", 1), ("base", 0)),
    "volkswagen": (("sel premium r-line", 3), ("sel r-line", 3), ("sel", 3), ("se r-line", 2),
                   ("se", 1), ("s", 0)),
    # 2.5T (Standard) < Select < Advanced < Sport Advanced < Prestige.
    "genesis": (("prestige", 3), ("sport advanced", 3), ("advanced", 2), ("select", 1),
                ("standard", 0)),
    # A bare "RX 350" is the base and reads as unknown here, on purpose.
    "lexus": (("luxury", 3), ("f sport", 2), ("premium", 1)),
    "mercedes-benz": (("amg", 3), ("pinnacle", 3), ("exclusive", 2), ("premium", 1)),
    "acura": (("type s", 3), ("advance", 3), ("a-spec", 2), ("technology", 1)),
    "bmw": (("m sport", 2),),
    "nissan": (("platinum", 3), ("sl", 2), ("sv", 1), ("s", 0)),
    "ford": (("platinum", 3), ("limited", 3), ("titanium", 2), ("st", 2), ("sel", 1),
             ("se", 0), ("s", 0)),
    "chevrolet": (("premier", 3), ("rs", 2), ("lt", 1), ("ls", 0)),
    "jeep": (("summit", 3), ("overland", 3), ("limited", 2), ("latitude", 1), ("sport", 0)),
}

# For makes without a ladder of their own.
GENERIC: Ladder = (
    ("premium plus", 3), ("platinum", 3), ("limited", 3), ("ultimate", 3), ("signature", 3),
    ("prestige", 3), ("calligraphy", 3), ("premium", 2), ("plus", 2), ("touring", 2),
    ("xle", 2), ("preferred", 1), ("select", 1), ("sel", 1), ("core", 1), ("sport", 1),
    ("le", 0), ("se", 0), ("lx", 0), ("base", 0), ("s", 0),
)

UNKNOWN = 1.5
_COMPILED: dict[str, tuple[tuple[re.Pattern, int], ...]] = {}


# Wheel diameter by model and trim, from Mazda's own 2026 packaging release
# and confirmed against Wheel-Size's fitment data (2026-09-21). Lorenzo
# wants 19 inch or larger, which on the CX-50 is a real trim filter rather
# than a styling note: the Meridian Edition rides on 18s with all-terrain
# tires and the Hybrid keeps the Preferred's 17s all the way up to Premium,
# so "Premium or better" does NOT imply a big wheel. Ordered, first match
# wins, so the longer phrase precedes the shorter one it contains.
_WHEELS: dict[str, tuple[tuple[str, int], ...]] = {
    # 225/65R17, 18 inch all-terrain on the Meridian, 245/45R20 above.
    "cx-50": (("meridian", 18), ("premium plus", 20), ("premium", 20),
              ("turbo", 20), ("preferred", 17), ("select", 17)),
    # 225/65R17 on Preferred AND Premium; 225/55R19 only on Premium Plus.
    "cx-50 hybrid": (("premium plus", 19), ("premium", 17), ("preferred", 17)),
    "cx-70": (("", 21),)   # every 3.3 trim is on 21s
}


def wheel_inches(model: str | None, trim: str | None) -> int | None:
    """Wheel diameter for a model and trim, or None when unknown.

    None means "not in the table", and a caller filtering on wheel size
    keeps such a car rather than hiding it: an unclassified model must not
    vanish from the board, the same rule the colour preferences follow.
    """
    key = (model or "").strip().lower()
    rungs = _WHEELS.get(key)
    if rungs is None:
        return None
    text = (trim or "").strip().lower()
    for phrase, inches in rungs:
        if not phrase or phrase in text:
            return inches
    return None


def _ladder(make: str | None) -> tuple[tuple[re.Pattern, int], ...]:
    key = (make or "").strip().lower()
    if key not in _COMPILED:
        rungs = LADDERS.get(key, GENERIC)
        _COMPILED[key] = tuple(
            (re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"), tier)
            for phrase, tier in rungs
        )
    return _COMPILED[key]


def _lookup(trim: str | None, make: str | None) -> int | None:
    text = (trim or "").strip().lower()
    if not text:
        return None
    for pattern, tier in _ladder(make):
        if pattern.search(text):
            return tier
    return None


def trim_tier(trim: str | None, make: str | None = None) -> float:
    """Ordinal rung, 0 to 3; 1.5 when the ladder does not recognise the trim."""
    tier = _lookup(trim, make)
    return float(tier) if tier is not None else UNKNOWN


def is_base_trim(trim: str | None, make: str | None = None) -> bool:
    """True only when the make's ladder positively places the trim at its base."""
    return _lookup(trim, make) == 0
