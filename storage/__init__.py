"""Persistence: SQLite schema, database access, material library, dedup."""

from storage.database import Database
from storage.dedup import DeduplicationService, hamming_distance
from storage.library import MaterialLibrary, material_slug

__all__ = [
    "Database",
    "MaterialLibrary",
    "material_slug",
    "DeduplicationService",
    "hamming_distance",
]
