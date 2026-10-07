"""Find glossary names in a message — Hebrew-aware, built once, cheap per message.

Substring search was wrong for Hebrew: a short name like גיל (Gil) sits inside רגיל
("ordinary"). Matching is by words instead, and a word may carry up to two of the
one-letter prefixes Hebrew glues onto it (ו ה ב ל מ ש כ: בגבעולים = "at Givolim"), so
those are tried stripped. Names of up to MAX_WORDS words match as phrases; the longest
phrase at a position wins.

The same normalisation is applied to keys and text: niqqud dropped, maqaf and hyphens
split words, ASCII quotes turned into geresh/gershayim (ביה"ס = ביה״ס), Latin lowercased.

Cost: one dict lookup per (position × phrase length × prefix variant) — a few hundred for
a long message, independent of how big the glossary grows.
"""
from __future__ import annotations

import re

PREFIXES = "והבלמשכ"
MAX_PREFIXES = 2
MIN_STEM = 2      # letters left after stripping a prefix
MAX_WORDS = 4

_NIQQUD = re.compile("[֑-ֽֿ-ׇ]")       # cantillation + vowel points
_SPLIT = re.compile("[־\\-–—/]")                        # maqaf, hyphens, slash
_QUOTES = str.maketrans({'"': "״", "“": "״", "”": "״", "'": "׳", "’": "׳", "`": "׳"})
_WORD = re.compile(r"[\w׳״]+")
_HEBREW = re.compile("[א-ת]")


def normalize(text: str) -> str:
    text = _NIQQUD.sub("", text or "")
    text = _SPLIT.sub(" ", text).translate(_QUOTES)
    return text.casefold()


def words(text: str) -> list[str]:
    """Normalised words. Leading quote marks and a trailing gershayim are dropped (a quoted
    name); a trailing geresh is kept — in Hebrew it is part of the word (ג׳, ג׳ורג׳)."""
    out = []
    for w in _WORD.findall(normalize(text)):
        w = w.lstrip("׳״").rstrip("״").strip("_")
        if w:
            out.append(w)
    return out


def key_of(text: str) -> str:
    """Canonical form of a glossary key: its normalised words joined by one space."""
    return " ".join(words(text))


def _variants(word: str):
    """The word, then the word with one and two Hebrew prefixes stripped."""
    yield word
    stem = word
    for _ in range(MAX_PREFIXES):
        if len(stem) - 1 < MIN_STEM or stem[0] not in PREFIXES or not _HEBREW.match(stem):
            return
        stem = stem[1:]
        yield stem


def covers(a: str, b: str) -> bool:
    """Whether one name contains the other as whole words, either way round:
    ביה״ס גבעולים ⊇ גבעולים, but רגיל does not contain גיל."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    n = len(short)
    return any(long_[i:i + n] == short for i in range(len(long_) - n + 1))


class GlossaryIndex:
    """{key: entry} → find(text) returns {key: entry} for every key the text mentions."""

    def __init__(self, entries: dict[str, dict] | None = None):
        self._by_phrase: dict[str, tuple[str, dict]] = {}
        self.max_words = 1
        for key, entry in (entries or {}).items():
            phrase = key_of(key)
            n = len(phrase.split()) if phrase else 0
            if not n or n > MAX_WORDS:
                continue
            self._by_phrase[phrase] = (key, entry)
            self.max_words = max(self.max_words, n)

    def __len__(self) -> int:
        return len(self._by_phrase)

    def find(self, text: str) -> dict[str, dict]:
        if not self._by_phrase or not text:
            return {}
        tokens = words(text)
        hits: dict[str, dict] = {}
        i = 0
        while i < len(tokens):
            matched = 0
            for n in range(min(self.max_words, len(tokens) - i), 0, -1):
                rest = tokens[i + 1:i + n]
                for first in _variants(tokens[i]):
                    found = self._by_phrase.get(" ".join([first, *rest]))
                    if found:
                        hits[found[0]] = found[1]
                        matched = n
                        break
                if matched:
                    break
            i += matched or 1
        return hits

    def lookup_word(self, word: str) -> dict | None:
        """Exact entry for one word, no prefix stripping (a name in a header, not in prose)."""
        found = self._by_phrase.get(key_of(word))
        return found[1] if found else None


_HEBREW_WORD = re.compile(r"[\u05d0-\u05ea][\u05d0-\u05ea׳״'\"]*")
_OTHER_LETTERS = re.compile(r"[A-Za-z\u0400-\u04ff]")


def render_name(name: str, people: GlossaryIndex) -> str | None:
    """A Hebrew display name in the target script, word by word — or None to keep it as is.

    Only when EVERY Hebrew word is a known person's name: "גילה דוד" → "Гила Давид", but a
    channel ("מבצעים באושר 5 💥") or a half-known name stays as it is — half-transliterated
    reads worse than either. A name that already has Latin or Cyrillic letters ("Isaac -
    אייזיק") is readable as is. Emoji, digits and punctuation are kept in place.
    """
    if not name or _OTHER_LETTERS.search(name) or not _HEBREW_WORD.search(name):
        return None
    missing = False

    def swap(m: re.Match) -> str:
        nonlocal missing
        entry = people.lookup_word(m.group(0))
        if not entry or not entry.get("translation"):
            missing = True
            return m.group(0)
        return entry["translation"]

    out = _HEBREW_WORD.sub(swap, name)
    return None if missing else out
