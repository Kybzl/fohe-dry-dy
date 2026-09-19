"""Keyword expansion rules (specification section 8)."""

from __future__ import annotations

import pytest

from core.keyword_expander import KeywordExpander, strip_process_words

EXPECTED_APPLE = [
    "苹果干",
    "苹果烘干",
    "苹果片烘干",
    "苹果热泵烘干",
    "苹果烘干机",
    "苹果烘干房",
    "苹果干制作",
    "苹果干加工",
    "苹果干生产",
    "苹果片干燥",
]


def test_apple_dried_expansion_matches_specification() -> None:
    assert KeywordExpander().expand("苹果干") == EXPECTED_APPLE


def test_expansion_without_form_suffix_returns_ten_queries() -> None:
    queries = KeywordExpander().expand("苹果")
    assert len(queries) == 10
    assert queries[0] == "苹果"
    assert "苹果烘干" in queries
    assert "苹果片烘干" in queries
    assert "苹果干制作" in queries


def test_expansion_is_deduplicated_and_ordered() -> None:
    queries = KeywordExpander().expand("  苹果干  ")
    assert len(queries) == len(set(queries))
    assert queries == EXPECTED_APPLE


def test_max_queries_limit_is_respected() -> None:
    queries = KeywordExpander(max_queries=4).expand("苹果干")
    assert queries == EXPECTED_APPLE[:4]


def test_split_material_detects_form_and_category() -> None:
    assert KeywordExpander.split_material("苹果干") == ("苹果", "干", "fruit")
    assert KeywordExpander.split_material("辣椒") == ("辣椒", "干", "vegetable")
    assert KeywordExpander.split_material("药材")[2] == "herb"
    assert KeywordExpander.split_material("海参")[2] == "seafood"


def test_herb_material_does_not_get_a_slice_query() -> None:
    queries = KeywordExpander().expand("药材")
    assert "药材烘干" in queries
    assert all("片烘干" not in query for query in queries)


def test_extra_templates_are_appended() -> None:
    expander = KeywordExpander(max_queries=20, extra_templates=["{base}{form}批发"])
    assert "苹果干批发" in expander.expand("苹果干")


def test_empty_material_is_rejected() -> None:
    with pytest.raises(ValueError):
        KeywordExpander().expand("   ")


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("苹果干烘干", "苹果干"),
        ("苹果热泵烘干", "苹果"),
        ("辣椒烘干机", "辣椒"),
        ("药材干燥加工", "药材"),
        ("苹果干", "苹果干"),
        ("烘干", "烘干"),  # nothing left after stripping stays untouched
    ],
)
def test_strip_process_words(phrase: str, expected: str) -> None:
    assert strip_process_words(phrase) == expected


def test_search_phrase_still_expands_into_full_keyword_set() -> None:
    queries = KeywordExpander().expand(strip_process_words("苹果干烘干"))
    assert queries == EXPECTED_APPLE
