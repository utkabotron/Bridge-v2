"""bridge_shared.glossary_match: finding names in Hebrew text by words, not substrings."""
from __future__ import annotations

from bridge_shared.glossary_match import GlossaryIndex, covers, key_of, words

E = {"translation": "x"}


def test_prefixes_are_stripped_but_substrings_do_not_match():
    index = GlossaryIndex({"גבעולים": E, "גיל": E})
    assert set(index.find("ובגבעולים היה כיף")) == {"גבעולים"}   # ו + ב stripped
    assert set(index.find("זה רגיל לגמרי")) == set()              # גיל inside רגיל
    assert set(index.find("שאלתי את גיל")) == {"גיל"}
    assert set(index.find("לגיל")) == {"גיל"}


def test_phrases_win_over_their_words_and_quotes_are_normalised():
    index = GlossaryIndex({"ביה״ס גבעולים": {"translation": "школа Гиволим"}, "גבעולים": E})
    assert set(index.find('מחר בביה"ס גבעולים')) == {"ביה״ס גבעולים"}
    assert set(index.find("ילדי גבעולים")) == {"גבעולים"}


def test_niqqud_maqaf_and_latin_case():
    index = GlossaryIndex({"שלום": E, "בית ספר": E, "Kinderon": E})
    assert set(index.find("שָׁלוֹם")) == {"שלום"}
    assert set(index.find("בית־ספר")) == {"בית ספר"}
    assert set(index.find("נרשמנו בKINDERON? לא, ב-Kinderon")) == {"Kinderon"}


def test_geresh_is_part_of_the_word_but_quotes_around_it_are_not():
    assert words("״גבעולים״ ג׳ורג׳") == ["גבעולים", "ג׳ורג׳"]
    assert key_of("  ביה\"ס   גבעולים ") == "ביה״ס גבעולים"
    assert key_of("א-1") == "א 1"


def test_covers_is_whole_words():
    assert covers("ביה״ס גבעולים", "גבעולים") and covers("גבעולים", "ביה״ס גבעולים")
    assert not covers("רגיל", "גיל")
    assert not covers("", "גיל")


def test_lookup_word_is_exact():
    index = GlossaryIndex({"דנה": {"translation": "Дана"}})
    assert index.lookup_word("דנה") == {"translation": "Дана"}
    assert index.lookup_word("לדנה") is None
    assert len(index) == 1
    assert GlossaryIndex({}).find("דנה") == {}


def test_render_name_only_when_every_hebrew_word_is_a_known_person():
    from bridge_shared.glossary_match import render_name

    people = GlossaryIndex({"גילה": {"translation": "Гила"}, "דוד": {"translation": "Давид"},
                            "דנה": {"translation": "Дана"}, "ג׳ורג׳": {"translation": "Джордж"}})
    assert render_name("גילה דוד", people) == "Гила Давид"
    assert render_name("דנה 🌸", people) == "Дана 🌸"
    assert render_name("ג'ורג'", people) == "Джордж"                      # ASCII geresh
    assert render_name("דנה דרחי", people) is None                         # half-known: keep
    assert render_name("מבצעים באושר 5 💥", people) is None                # a channel, not a person
    assert render_name("Isaac - אייזיק", people) is None                   # already readable
    assert render_name("Марина דנה", people) is None
    assert render_name("", people) is None and render_name("💥", people) is None
