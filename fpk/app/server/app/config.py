# -*- coding: utf-8 -*-
"""Configuration and persistent state management for Echo Music Sync."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "trim_music_db": "/var/apps/trim.music/var/db/music.db",
    "trim_music_meta": "/var/apps/trim.music/meta",
    "target_mount": "",
    "target_music_subdir": "Music",
    "target_audiobook_subdir": "Audiobooks",
    "target_playlist_subdir": "Playlists",
    "folder_layout": "artist_album",  # artist_album | album | preserve_rel | flat
    "auto_route_audiobooks": True,
    "embed_cover": True,
    "write_folder_cover": True,
    "cover_max_size": 500,
    "cover_quality": 88,
    "export_lrc": True,
    "embed_lyrics": True,
    "write_scraped_tags": True,
    "skip_existing": True,
    "transcode_unsupported": True,
    "cue_mode": "split_flac",  # split_flac | copy_cue
    "generate_m3u": True,
    "auto_sync_disk": True,
    "default_user_id": 1,
}


def get_data_dir() -> Path:
    var_dir = os.environ.get("TRIM_PKGVAR", "").strip()
    if var_dir:
        p = Path(var_dir)
    else:
        p = Path(os.environ.get("ECHO_SYNC_DATA_DIR", "./data")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self._path = path or (get_data_dir() / "config.json")
        self._lock = threading.Lock()
        self._cache: Dict[str, Any] | None = None

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return dict(self._cache)
            cfg = dict(DEFAULT_CONFIG)
            if self._path.exists():
                try:
                    data = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        for k, v in data.items():
                            if k in DEFAULT_CONFIG:
                                cfg[k] = v
                except Exception:
                    pass
            # Allow environment overrides for testing / deployment
            if os.environ.get("TRIM_MUSIC_DB"):
                cfg["trim_music_db"] = os.environ["TRIM_MUSIC_DB"]
            if os.environ.get("TRIM_MUSIC_META"):
                cfg["trim_music_meta"] = os.environ["TRIM_MUSIC_META"]
            self._cache = cfg
            return dict(cfg)

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cfg = dict(self._cache or self._load_unlocked())
            for k, v in patch.items():
                if k in DEFAULT_CONFIG:
                    cfg[k] = v
            # Validate numeric bounds
            cfg["cover_max_size"] = max(128, min(1600, int(cfg.get("cover_max_size", 500))))
            cfg["cover_quality"] = max(50, min(98, int(cfg.get("cover_quality", 88))))
            if cfg.get("folder_layout") not in ("artist_album", "album", "preserve_rel", "flat"):
                cfg["folder_layout"] = "artist_album"
            if cfg.get("cue_mode") not in ("split_flac", "copy_cue"):
                cfg["cue_mode"] = "split_flac"
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
            self._cache = cfg
            return dict(cfg)

    def _load_unlocked(self) -> Dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k in DEFAULT_CONFIG:
                            cfg[k] = v
            except Exception:
                pass
        self._cache = cfg
        return cfg


config_store = ConfigStore()
