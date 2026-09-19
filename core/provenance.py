"""Where a clip came from (Milestone 3.7, sections 22/23).

Legacy demo/placeholder clips must be identifiable **from data**, never from a
filename pattern, so the library can offer a safe cleanup command.

Classification is deliberately conservative: anything that is not provably a
mock placeholder or a real Douyin post stays ``unknown`` and is never removed
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

MOCK = "mock"
LOCAL_TEST = "local_test"
DOUYIN_REAL = "douyin_real"
UNKNOWN = "unknown"

ALL_PROVENANCE = (MOCK, LOCAL_TEST, DOUYIN_REAL, UNKNOWN)

#: source adapter name -> provenance
ADAPTER_PROVENANCE: dict[str, str] = {
    "mock": MOCK,
    "local": LOCAL_TEST,
    "douyin": DOUYIN_REAL,
}

#: below this size a frame is a placeholder rather than real footage
PLACEHOLDER_MAX_DIMENSION = 640
PLACEHOLDER_MAX_BYTES = 200 * 1024


def looks_like_douyin_video_id(value: str) -> bool:
    """Real Douyin ``aweme_id`` are long digit strings."""

    text = (value or "").strip()
    return len(text) >= 12 and text.isdigit()


def classify_provenance(
    platform: str,
    platform_video_id: str,
    *,
    source_adapter: str = "",
    source_url: str = "",
) -> str:
    """Classify one source/clip as mock, local test or real Douyin."""

    adapter = (source_adapter or "").strip().lower()
    if adapter in ADAPTER_PROVENANCE:
        mapped = ADAPTER_PROVENANCE[adapter]
        if mapped == DOUYIN_REAL and not looks_like_douyin_video_id(platform_video_id):
            # the mock source uses the douyin platform name with synthetic ids
            return MOCK
        return mapped

    platform_name = (platform or "").strip().lower()
    if platform_name == "local":
        return LOCAL_TEST
    if platform_name == "mock":
        return MOCK
    if platform_name == "douyin":
        if looks_like_douyin_video_id(platform_video_id) and (
            not source_url or "douyin.com" in source_url
        ):
            return DOUYIN_REAL
        return MOCK
    return UNKNOWN


@dataclass(frozen=True)
class ClipIntegrity:
    """Cheap physical signals used to flag placeholder-grade media."""

    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None

    @property
    def low_resolution(self) -> bool:
        largest = max(self.width or 0, self.height or 0)
        return 0 < largest <= PLACEHOLDER_MAX_DIMENSION

    @property
    def tiny_file(self) -> bool:
        return self.size_bytes is not None and self.size_bytes <= PLACEHOLDER_MAX_BYTES

    def placeholder_signals(self) -> list[str]:
        signals: list[str] = []
        if self.low_resolution:
            signals.append(f"low_resolution {self.width}x{self.height}")
        if self.tiny_file:
            signals.append(f"tiny_file {self.size_bytes}B")
        return signals


def demo_removal_verdict(
    *,
    provenance: str,
    integrity: ClipIntegrity,
    include_local_tests: bool = False,
) -> tuple[bool, str]:
    """Whether the cleanup command may remove this clip, and why.

    Only provably synthetic clips (``mock``) - optionally ``local_test`` - are
    removable.  Real Douyin clips and anything unknown are never removable,
    regardless of resolution.
    """

    if provenance == MOCK:
        signals = integrity.placeholder_signals()
        return True, "provenance=mock" + (f" ({', '.join(signals)})" if signals else "")
    if provenance == LOCAL_TEST and include_local_tests:
        return True, "provenance=local_test (explicitly requested)"
    if provenance == DOUYIN_REAL:
        return False, "real Douyin production clip - never removed automatically"
    if provenance == LOCAL_TEST:
        return False, "local test clip - pass --include-local-tests to remove"
    return False, "unclassified provenance - needs manual review"
