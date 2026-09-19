"""Rule based search keyword expansion (section 8 of the specification).

No LLM is used here on purpose: the templates are deterministic, free and easy
to tune.  ``苹果干`` expands to exactly the ten queries listed in the
specification.
"""

from __future__ import annotations

import logging
from typing import Iterable

LOGGER = logging.getLogger(__name__)

# Product form suffixes that may end a material name.  Deliberately excludes
# "果": "苹果" is the material itself, not "苹" in dried form.
PRODUCT_FORM_SUFFIXES: tuple[str, ...] = (
    "干",
    "片",
    "粉",
    "条",
    "丝",
    "块",
    "粒",
    "叶",
)

#: a stripped base must still look like a material name
MIN_BASE_LENGTH = 2

# How a material is normally cut before drying.
CATEGORY_PROCESS_FORM: dict[str, str] = {
    "fruit": "片",
    "vegetable": "片",
    "root": "片",
    "herb": "",
    "seafood": "",
    "grain": "",
}

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fruit": (
        "苹果",
        "香蕉",
        "梨",
        "桃",
        "杏",
        "枣",
        "葡萄",
        "芒果",
        "菠萝",
        "柠檬",
        "猕猴桃",
        "山楂",
        "无花果",
    ),
    "vegetable": ("辣椒", "番茄", "茄子", "豆角", "香菇", "木耳", "萝卜", "白菜", "南瓜", "洋葱"),
    "root": ("山药", "红薯", "土豆", "芋头", "葛根", "姜", "蒜", "百合"),
    "herb": ("药材", "枸杞", "黄芪", "人参", "当归", "陈皮", "菊花", "金银花", "甘草", "灵芝"),
    "seafood": ("鱼", "虾", "海参", "鲍鱼", "鱿鱼", "海带", "紫菜", "贝"),
    "grain": ("玉米", "稻谷", "小麦", "花生", "豆"),
}

#: process words that may be appended to a material in a full search phrase
PROCESS_WORDS: tuple[str, ...] = (
    "热泵烘干",
    "烘干机",
    "烘干房",
    "烘干设备",
    "干燥机",
    "生产线",
    "烘干",
    "干燥",
    "加工",
    "生产",
    "制作",
    "设备",
    "流程",
    "工艺",
    "技术",
    "车间",
    "厂家",
    "价格",
)


def strip_process_words(text: str) -> str:
    """``"苹果干烘干"`` -> ``"苹果干"``.

    Users type either a material (``苹果干``) or a whole search phrase
    (``苹果干烘干``); the library and the keyword template both want the
    material, so trailing process words are removed.
    """

    result = (text or "").strip()
    changed = True
    while changed and result:
        changed = False
        for word in PROCESS_WORDS:
            if result.endswith(word) and len(result) > len(word):
                result = result[: -len(word)]
                changed = True
                break
    return result or (text or "").strip()


class KeywordExpander:
    """Expands one material name into a small, ordered set of search queries."""

    def __init__(
        self,
        *,
        max_queries: int = 10,
        extra_templates: Iterable[str] | None = None,
    ) -> None:
        self.max_queries = max(1, max_queries)
        # Optional extra ``str.format`` templates, e.g. ``"{base}{form}批发"``.
        self.extra_templates = tuple(extra_templates or ())

    def expand(self, material: str) -> list[str]:
        """Return the search queries for ``material``, most specific first."""

        material = (material or "").strip()
        if not material:
            raise ValueError("material must not be empty")

        base, form, category = self.split_material(material)
        process_form = CATEGORY_PROCESS_FORM.get(category, "片")

        queries: list[str] = [material]
        queries.append(f"{base}烘干")
        if process_form:
            queries.append(f"{base}{process_form}烘干")
        queries.append(f"{base}热泵烘干")
        queries.append(f"{base}烘干机")
        queries.append(f"{base}烘干房")
        queries.append(f"{base}{form}制作")
        queries.append(f"{base}{form}加工")
        queries.append(f"{base}{form}生产")
        queries.append(f"{base}{process_form}干燥" if process_form else f"{base}干燥")
        for template in self.extra_templates:
            queries.append(template.format(base=base, form=form, material=material))

        expanded = self._dedupe(queries)
        LOGGER.debug("expanded %r into %s queries", material, len(expanded))
        return expanded[: self.max_queries]

    @staticmethod
    def split_material(material: str) -> tuple[str, str, str]:
        """Split ``苹果干`` into ``("苹果", "干", "fruit")``."""

        base = material
        form = ""
        for suffix in PRODUCT_FORM_SUFFIXES:
            stripped = material[: -len(suffix)]
            if material.endswith(suffix) and len(stripped) >= MIN_BASE_LENGTH:
                base = material[: -len(suffix)]
                form = suffix
                break

        category = "unknown"
        for name, keywords in CATEGORY_KEYWORDS.items():
            if any(keyword in material for keyword in keywords):
                category = name
                break

        # A drying material library defaults to the dried product form.
        if not form and category not in ("herb", "seafood", "grain"):
            form = "干"
        return base, form, category

    @staticmethod
    def _dedupe(values: Iterable[str]) -> list[str]:
        ordered: list[str] = []
        for value in values:
            cleaned = value.strip()
            if cleaned and cleaned not in ordered:
                ordered.append(cleaned)
        return ordered
