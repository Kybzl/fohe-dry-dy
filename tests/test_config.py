"""Configuration: default library root, path anchoring, secrets."""

from __future__ import annotations

from pathlib import Path

from core.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_LIBRARY_ROOT,
    PROJECT_ROOT,
    PathSettings,
    is_absolute_path,
    load_settings,
)


def test_shipped_config_uses_the_material_library_root() -> None:
    """``config.yaml`` must default to ``D:/素材库2``."""

    settings = load_settings(config_path=DEFAULT_CONFIG_PATH)
    assert settings.paths.library_root == Path("D:/素材库2")
    assert DEFAULT_LIBRARY_ROOT == Path("D:/素材库2")


def test_sqlite_index_stays_inside_the_project() -> None:
    settings = load_settings(config_path=DEFAULT_CONFIG_PATH)
    assert settings.paths.database == PROJECT_ROOT / "data" / "library.db"
    assert settings.paths.database.parent.name == "data"
    assert settings.paths.cache_dir.is_relative_to(PROJECT_ROOT) or str(
        settings.paths.cache_dir
    ).startswith(str(PROJECT_ROOT))


def test_windows_drive_paths_count_as_absolute_everywhere() -> None:
    assert is_absolute_path(Path("D:/素材库2")) is True
    assert is_absolute_path("E:\\Codex\\fohe-dy") is True
    assert is_absolute_path("/var/lib/materials") is True
    assert is_absolute_path("library") is False
    assert is_absolute_path("") is False
    assert is_absolute_path(Path("素材库2")) is False


def test_absolute_library_root_is_kept_and_relative_paths_are_anchored() -> None:
    base = Path("E:/projects/agent")
    resolved = PathSettings(
        data_dir=Path("data"),
        cache_dir=Path("cache"),
        library_root=Path("D:/素材库2"),
        log_dir=Path("logs"),
        database=Path("data/library.db"),
    ).resolved(base)
    assert resolved.library_root == Path("D:/素材库2")
    assert resolved.data_dir == base / "data"
    assert resolved.database == base / "data" / "library.db"


def test_relative_library_root_is_resolved_against_the_project_root() -> None:
    base = Path("E:/projects/agent")
    resolved = PathSettings(library_root=Path("library")).resolved(base)
    assert resolved.library_root == base / "library"


def test_overrides_win_over_the_shipped_default(tmp_path: Path) -> None:
    custom = tmp_path / "素材库"
    settings = load_settings(
        config_path=DEFAULT_CONFIG_PATH,
        overrides={"storage": {"library_root": str(custom)}},
    )
    assert settings.paths.library_root == custom
    assert settings.paths.database == PROJECT_ROOT / "data" / "library.db"


def test_portable_paths_can_be_overridden_from_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "FOHE_LIBRARY_ROOT=F:/portable/library",
                "FOHE_DATABASE_PATH=state/library.db",
                "FOHE_CACHE_ROOT=state/cache",
                "FOHE_FFMPEG_PATH=tools/ffmpeg.exe",
                "FOHE_FFPROBE_PATH=tools/ffprobe.exe",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(config_path=DEFAULT_CONFIG_PATH, env_path=env_file)

    assert settings.paths.library_root == Path("F:/portable/library")
    assert settings.paths.database == PROJECT_ROOT / "state" / "library.db"
    assert settings.paths.cache_dir == PROJECT_ROOT / "state" / "cache"
    assert settings.media.ffmpeg_path == "tools/ffmpeg.exe"
    assert settings.media.ffprobe_path == "tools/ffprobe.exe"


def test_process_environment_wins_over_dotenv_paths(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOHE_LIBRARY_ROOT=D:/dotenv-library\n", encoding="utf-8")
    monkeypatch.setenv("FOHE_LIBRARY_ROOT", "F:/process-library")

    settings = load_settings(config_path=DEFAULT_CONFIG_PATH, env_path=env_file)

    assert settings.paths.library_root == Path("F:/process-library")


def test_storage_section_reconciles_with_legacy_paths_section(tmp_path: Path) -> None:
    """``storage`` is canonical; ``paths`` still works for old config files."""

    legacy = tmp_path / "legacy-config.yaml"
    legacy.write_text(
        """
paths:
  library_root: "D:/legacy-library"
  database: "data/legacy.db"
  cache_dir: "cache/legacy"
""",
        encoding="utf-8",
    )
    settings = load_settings(config_path=legacy, env_path=tmp_path / "missing.env")
    assert settings.paths.library_root == Path("D:/legacy-library")
    assert settings.paths.database == PROJECT_ROOT / "data" / "legacy.db"
    assert settings.paths.cache_dir == PROJECT_ROOT / "cache" / "legacy"


def test_storage_defaults_come_from_the_shipped_config() -> None:
    settings = load_settings(config_path=DEFAULT_CONFIG_PATH)
    assert settings.storage.library_root == Path("D:/素材库2")
    assert settings.paths.cache_dir == PROJECT_ROOT / "cache"
    # Milestone 3 makes Douyin the default acquisition source
    assert settings.sources.active_source == "douyin"
    assert settings.sources.douyin.base_url.startswith("http")


def test_library_service_uses_the_configured_root(tmp_path: Path) -> None:
    from storage.database import Database
    from storage.library import MaterialLibrary

    root = tmp_path / "素材库2"
    library = MaterialLibrary(Database(tmp_path / "data" / "library.db"), root)
    library.initialize()
    clip_path, thumb_path = library.next_clip_paths("苹果干")
    assert clip_path.parent.parent.parent == root
    assert clip_path.parent.name == "clips"
    assert thumb_path.parent.name == "thumbnails"
    assert clip_path.parent.is_relative_to(root)
