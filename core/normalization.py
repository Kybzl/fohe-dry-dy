"""Canonical material ontology (Milestone 3.6, section 18).

Two different things share the name "material" in this project:

* the **user facing** material (``苹果干``): the library folder, the task row and
  the keyword templates.  It is stored exactly as the user typed it.
* the **structured semantic tag** (``material = 苹果`` + ``material_state =
  dried``): what a clip actually shows, used for filtering and retrieval.

Without a policy the same material ends up as ``苹果干`` in one clip and
``苹果`` in another, which makes the library unqueryable.  This module owns the
single normalization rule used by every tagging path (mock, Qwen and the
offline fallback).
"""

from __future__ import annotations

import logging

from core.keyword_expander import (
    MIN_BASE_LENGTH,
    PRODUCT_FORM_SUFFIXES,
    KeywordExpander,
    strip_process_words,
)
from core.models import ClipTagging, MaterialState

LOGGER = logging.getLogger(__name__)

#: product form suffix -> state of the material when the tagger could not tell.
#: Only the dried form carries a state by itself; a slice/strip/powder can be
#: fresh, half dried or dried, so it stays ``unknown`` unless the model says
#: otherwise.
STATE_BY_FORM_SUFFIX: dict[str, MaterialState] = {
    "干": MaterialState.DRIED,
}


def material_base(material: str) -> str:
    """``苹果干`` -> ``苹果``; ``苹果`` -> ``苹果``.

    Trailing process words (``苹果干烘干``) are removed first so the same policy
    applies to a task material and to a whole search phrase.
    """

    text = (material or "").strip()
    if not text:
        return ""
    base, _form, _category = KeywordExpander.split_material(strip_process_words(text))
    return base or text


def material_form_suffix(material: str) -> str:
    """The product form suffix of a material name (``苹果干`` -> ``干``)."""

    text = (material or "").strip()
    if not text:
        return ""
    for suffix in PRODUCT_FORM_SUFFIXES:
        if text.endswith(suffix) and len(text) - len(suffix) >= MIN_BASE_LENGTH:
            return suffix
    return ""


def default_material_state(material: str) -> MaterialState:
    """State implied by the user facing material name (``苹果干`` -> ``dried``)."""

    return STATE_BY_FORM_SUFFIX.get(material_form_suffix(material), MaterialState.UNKNOWN)


def canonical_material(value: str, *, reference: str = "") -> str:
    """Map a tag value onto the canonical material name.

    ``reference`` is the user facing material of the task.  When the tag names
    the same material (``苹果干`` / ``苹果`` for ``苹果干``) the reference base
    wins, so every clip of one task shares one ``material`` tag.
    """

    text = (value or "").strip()
    reference_base = material_base(reference)
    if not text:
        return reference_base
    base = material_base(text)
    if reference_base and base and (
        base == reference_base
        or base.endswith(reference_base)
        or reference_base.endswith(base)
    ):
        return reference_base
    return base or text


def normalize_tagging(tagging: ClipTagging, *, request_material: str) -> ClipTagging:
    """Apply the canonical material ontology to one tagging result.

    The model's answer always wins where it is meaningful (a slice stays a
    slice, ``drying`` stays ``drying``).  Only the material *name* is
    canonicalised so a task about 苹果干 and a task about 苹果片 put the same
    ``material = 苹果`` tag in the library.

    ``material_state`` is **never** derived from the requested material
    (Milestone 3.7, sections 1/2): a clip showing fresh apple slices must stay
    ``fresh``/``prepared``/``unknown`` even when the operator searched for
    苹果干.  The user-facing category lives in ``library_category`` instead.
    """

    material = canonical_material(tagging.material, reference=request_material)
    if material == tagging.material:
        return tagging
    LOGGER.debug(
        "normalized tagging material %r -> %r (observed state %s kept)",
        tagging.material,
        material,
        tagging.material_state,
    )
    return tagging.model_copy(update={"material": material})
