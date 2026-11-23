# -*- coding: utf-8 -*-
"""Asynchronous background transfer manager for sending fnOS Music tracks to Echo."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ConfigStore, get_data_dir
from .media_proc import (
    apply_metadata_to_copy,
    copy_file_atomic,
    needs_transcode,
    prepare_echo_cover,
    sanitize_filename,
    transcode_or_split_to_flac,
    write_folder_cover,
    write_lrc_sidecar,
)
from .storage import sync_mount, validate_mount_path
from .trim_music import TrimMusicRepo


def _is_audiobook_track(track: Dict[str, Any]) -> bool:
    lib_path = (track.get("library_path") or "").lower()
    src_path = (track.get("path") or "").lower()
    return (
        "/books" in lib_path
        or "audiobook" in lib_path
        or "有声书" in src_path
        or "广播剧" in src_path
    )


def build_destination_path(
    mount_root: str,
    track: Dict[str, Any],
    cfg: Dict[str, Any],
    force_flac: bool = False,
) -> str:
    """Compute target audio path on Echo based on folder_layout and audiobook routing."""
    is_book = cfg.get("auto_route_audiobooks", True) and _is_audiobook_track(track)
    base_subdir = (
        cfg.get("target_audiobook_subdir", "Audiobooks")
        if is_book
        else cfg.get("target_music_subdir", "Music")
    ).strip("/")

    layout = cfg.get("folder_layout", "artist_album")
    artist_dir = sanitize_filename(track.get("album_artist") or track.get("artist") or "Unknown Artist")
    album_dir = sanitize_filename(track.get("album") or "Singles")

    orig_name = track.get("file_name") or os.path.basename(track.get("path") or "track")
    orig_stem, orig_ext = os.path.splitext(orig_name)
    out_ext = ".flac" if force_flac else (orig_ext.lower() or f".{track.get('suffix', 'mp3')}")

    if track.get("is_cue") and force_flac:
        tno = track.get("track_no")
        prefix = f"{int(tno):02d} " if tno else ""
        stem = sanitize_filename(f"{prefix}{track.get('title') or orig_stem}")
    else:
        stem = sanitize_filename(orig_stem)

    filename = f"{stem}{out_ext}"

    if layout == "preserve_rel" and track.get("library_path") and track.get("path"):
        try:
            rel_dir = os.path.relpath(os.path.dirname(track["path"]), track["library_path"])
            if rel_dir and not rel_dir.startswith(".."):
                clean_parts = [sanitize_filename(p) for p in Path(rel_dir).parts if p not in (".", "")]
                return os.path.join(mount_root, base_subdir, *clean_parts, filename)
        except ValueError:
            pass

    if layout == "flat":
        return os.path.join(mount_root, base_subdir, filename)
    if layout == "album" or is_book:
        return os.path.join(mount_root, base_subdir, album_dir, filename)
    # Default: artist_album -> Music/<Artist>/<Album>/<Track>
    return os.path.join(mount_root, base_subdir, artist_dir, album_dir, filename)


class TransferManager:
    def __init__(self, cfg_store: ConfigStore):
        self._cfg_store = cfg_store
        self._lock = threading.Lock()
        self._current_job: Optional[Dict[str, Any]] = None
        self._cancel_event = threading.Event()
        self._history_path = get_data_dir() / "history.json"
        self._history: List[Dict[str, Any]] = self._load_history()

    def _load_history(self) -> List[Dict[str, Any]]:
        if self._history_path.exists():
            try:
                data = json.loads(self._history_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[:50]
            except Exception:
                pass
        return []

    def _save_history(self) -> None:
        try:
            tmp = self._history_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._history[:50], ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._history_path)
        except OSError:
            pass

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "current": dict(self._current_job) if self._current_job else None,
                "history": list(self._history[:20]),
            }

    def cancel_current(self) -> bool:
        with self._lock:
            if self._current_job and self._current_job["status"] == "running":
                self._cancel_event.set()
                self._current_job["cancelling"] = True
                return True
        return False

    def start_job(
        self,
        repo: TrimMusicRepo,
        track_ids: List[int],
        mountpoint: str,
        label: str = "发送到 Echo",
        playlist_name: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        user_id: int = 1,
    ) -> Dict[str, Any]:
        dev = validate_mount_path(mountpoint)
        if not dev.get("writable"):
            raise PermissionError(f"目标设备不可写: {mountpoint}")

        tracks = repo.get_tracks_by_ids(track_ids, user_id=user_id)
        if not tracks:
            raise ValueError("未找到可传输的有效曲目")

        cfg = self._cfg_store.load()
        if overrides:
            for k, v in overrides.items():
                if k in cfg:
                    cfg[k] = v

        with self._lock:
            if self._current_job and self._current_job["status"] == "running":
                raise RuntimeError("当前已有传输任务正在进行，请等待完成或先取消")
            self._cancel_event.clear()
            total_bytes = sum(t.get("size", 0) for t in tracks)
            job: Dict[str, Any] = {
                "id": uuid.uuid4().hex[:12],
                "label": label,
                "mountpoint": dev["mountpoint"],
                "device_label": dev["label"],
                "playlist_name": playlist_name,
                "status": "running",
                "cancelling": False,
                "started_at": int(time.time()),
                "finished_at": None,
                "total_tracks": len(tracks),
                "processed_tracks": 0,
                "copied": 0,
                "skipped": 0,
                "transcoded": 0,
                "failed": 0,
                "covers_written": 0,
                "lyrics_written": 0,
                "total_bytes": total_bytes,
                "transferred_bytes": 0,
                "speed_bps": 0,
                "current_track": None,
                "errors": [],
                "recent_items": [],
            }
            self._current_job = job

        worker = threading.Thread(
            target=self._run_job,
            args=(job["id"], repo, tracks, dev["mountpoint"], cfg, playlist_name, user_id),
            daemon=True,
        )
        worker.start()
        return dict(job)

    def _run_job(
        self,
        job_id: str,
        repo: TrimMusicRepo,
        tracks: List[Dict[str, Any]],
        mount_root: str,
        cfg: Dict[str, Any],
        playlist_name: Optional[str],
        user_id: int,
    ) -> None:
        t0 = time.monotonic()
        cover_cache: Dict[str, Tuple[bytes, int, int]] = {}
        written_folder_covers: set[str] = set()
        copied_cue_sources: set[str] = set()
        playlist_rel_entries: List[str] = []

        for idx, track in enumerate(tracks, start=1):
            if self._cancel_event.is_set():
                break

            src_path = track.get("path") or ""
            with self._lock:
                if self._current_job and self._current_job["id"] == job_id:
                    self._current_job["current_track"] = {
                        "id": track["id"],
                        "title": track["title"],
                        "artist": track["artist"],
                        "album": track["album"],
                        "suffix": track["suffix"],
                        "cover_type": track["cover_type"],
                        "cover_guid": track["cover_guid"],
                        "index": idx,
                    }

            item_log: Dict[str, Any] = {
                "track_id": track["id"],
                "title": track["title"],
                "artist": track["artist"],
                "status": "ok",
                "cover": False,
                "lyric": False,
                "tags": False,
                "note": "",
            }

            try:
                if not os.path.isfile(src_path):
                    raise FileNotFoundError(f"源文件不存在: {src_path}")

                is_cue = bool(track.get("is_cue"))
                cue_split = is_cue and cfg.get("cue_mode", "split_flac") == "split_flac"
                will_transcode = cue_split or needs_transcode(
                    track.get("suffix", ""), cfg.get("transcode_unsupported", True)
                )

                dst_path = build_destination_path(mount_root, track, cfg, force_flac=will_transcode)
                dst_dir = os.path.dirname(dst_path)
                os.makedirs(dst_dir, exist_ok=True)

                # Resolve cover from trim.music
                cover_jpeg: Optional[bytes] = None
                cover_dims = (500, 500)
                if cfg.get("embed_cover", True) or cfg.get("write_folder_cover", True):
                    cpath, _ = repo.resolve_cover_file(
                        track.get("track_cover_guid"),
                        track.get("album_cover_guid"),
                        track.get("album_metadata_mode"),
                        src_path,
                    )
                    if cpath:
                        if cpath not in cover_cache:
                            try:
                                cover_cache[cpath] = prepare_echo_cover(
                                    cpath,
                                    max_size=int(cfg.get("cover_max_size", 500)),
                                    quality=int(cfg.get("cover_quality", 88)),
                                )
                            except Exception:
                                cover_cache[cpath] = (b"", 0, 0)
                        cached = cover_cache[cpath]
                        if cached[0]:
                            cover_jpeg, cover_dims = cached[0], (cached[1], cached[2])

                # Resolve lyric from trim.music
                lyric_text: Optional[str] = None
                if cfg.get("export_lrc", True) or cfg.get("embed_lyrics", True):
                    linfo = repo.resolve_lyric(track["id"], user_id=user_id)
                    if linfo and linfo.get("content"):
                        lyric_text = linfo["content"]

                # Copy or transcode audio file
                skip_existing = bool(cfg.get("skip_existing", True))
                already_exists = os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0
                bytes_done = 0

                if is_cue and not cue_split:
                    # Copy whole CUE audio + .cue once
                    cue_key = src_path
                    if cue_key not in copied_cue_sources:
                        if already_exists and skip_existing:
                            item_log["status"] = "skipped"
                            item_log["note"] = "已存在"
                        else:
                            bytes_done = copy_file_atomic(src_path, dst_path, self._cancel_event.is_set)
                        cue_src = track.get("cue_path")
                        if cue_src and os.path.isfile(cue_src):
                            cue_dst = os.path.splitext(dst_path)[0] + ".cue"
                            if not (os.path.isfile(cue_dst) and skip_existing):
                                copy_file_atomic(cue_src, cue_dst, self._cancel_event.is_set)
                        copied_cue_sources.add(cue_key)
                    else:
                        item_log["status"] = "skipped"
                        item_log["note"] = "整轨已复制"
                elif already_exists and skip_existing:
                    item_log["status"] = "skipped"
                    item_log["note"] = "已存在(仅补齐封面/歌词)"
                elif will_transcode:
                    bytes_done = transcode_or_split_to_flac(
                        src_path,
                        dst_path,
                        start_ms=track.get("start_offset_ms") if cue_split else None,
                        end_ms=track.get("end_offset_ms") if cue_split else None,
                    )
                    item_log["status"] = "transcoded"
                    item_log["note"] = "CUE分轨FLAC" if cue_split else "转码FLAC"
                else:
                    bytes_done = copy_file_atomic(src_path, dst_path, self._cancel_event.is_set)
                    item_log["status"] = "copied"

                # Write folder.jpg once per album folder
                if cover_jpeg and cfg.get("write_folder_cover", True) and dst_dir not in written_folder_covers:
                    if write_folder_cover(dst_dir, cover_jpeg, overwrite=not skip_existing):
                        item_log["cover"] = True
                    written_folder_covers.add(dst_dir)

                # Write tags + embedded cover + embedded lyrics on freshly copied/transcoded files
                if item_log["status"] in ("copied", "transcoded"):
                    meta_res = apply_metadata_to_copy(
                        dst_path,
                        track,
                        cover_jpeg=cover_jpeg,
                        cover_dims=cover_dims,
                        lyric_text=lyric_text,
                        write_tags=bool(cfg.get("write_scraped_tags", True)),
                        embed_cover=bool(cfg.get("embed_cover", True)),
                        embed_lyrics=bool(cfg.get("embed_lyrics", True)),
                    )
                    item_log["tags"] = meta_res["tags"]
                    item_log["cover"] = item_log["cover"] or meta_res["cover"]

                # Always ensure sidecar .lrc exists if enabled
                if lyric_text and cfg.get("export_lrc", True):
                    lrc_dst = os.path.splitext(dst_path)[0] + ".lrc"
                    if not (skip_existing and os.path.isfile(lrc_dst) and os.path.getsize(lrc_dst) > 0):
                        write_lrc_sidecar(dst_path, lyric_text)
                        item_log["lyric"] = True

                rel_for_m3u = os.path.relpath(dst_path, mount_root).replace(os.sep, "/")
                playlist_rel_entries.append(rel_for_m3u)

                with self._lock:
                    if self._current_job and self._current_job["id"] == job_id:
                        self._current_job["processed_tracks"] += 1
                        self._current_job["transferred_bytes"] += bytes_done
                        elapsed = max(0.05, time.monotonic() - t0)
                        self._current_job["speed_bps"] = int(self._current_job["transferred_bytes"] / elapsed)
                        if item_log["status"] == "copied":
                            self._current_job["copied"] += 1
                        elif item_log["status"] == "transcoded":
                            self._current_job["transcoded"] += 1
                        elif item_log["status"] == "skipped":
                            self._current_job["skipped"] += 1
                        if item_log["cover"]:
                            self._current_job["covers_written"] += 1
                        if item_log["lyric"]:
                            self._current_job["lyrics_written"] += 1
                        self._current_job["recent_items"] = (
                            [item_log] + self._current_job["recent_items"]
                        )[:30]

            except InterruptedError:
                break
            except Exception as exc:
                item_log["status"] = "failed"
                item_log["note"] = str(exc)[:200]
                with self._lock:
                    if self._current_job and self._current_job["id"] == job_id:
                        self._current_job["processed_tracks"] += 1
                        self._current_job["failed"] += 1
                        if len(self._current_job["errors"]) < 50:
                            self._current_job["errors"].append(
                                {"track_id": track["id"], "title": track["title"], "error": str(exc)[:240]}
                            )
                        self._current_job["recent_items"] = (
                            [item_log] + self._current_job["recent_items"]
                        )[:30]

        # Optionally generate .m3u8 playlist on Echo
        if playlist_name and playlist_rel_entries and cfg.get("generate_m3u", True) and not self._cancel_event.is_set():
            try:
                pl_subdir = (cfg.get("target_playlist_subdir", "Playlists") or "Playlists").strip("/")
                pl_dir = os.path.join(mount_root, pl_subdir)
                os.makedirs(pl_dir, exist_ok=True)
                pl_file = os.path.join(pl_dir, f"{sanitize_filename(playlist_name)}.m3u8")
                # Relative paths from Playlists/ back to root (../Music/...)
                depth = len(Path(pl_subdir).parts)
                prefix = "../" * depth
                lines = ["#EXTM3U"] + [f"{prefix}{entry}" for entry in playlist_rel_entries]
                tmp = pl_file + ".tmp_echo"
                with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("\n".join(lines) + "\n")
                os.replace(tmp, pl_file)
            except Exception:
                pass

        # Flush USB storage cache automatically
        if cfg.get("auto_sync_disk", True):
            try:
                sync_mount(mount_root)
            except Exception:
                pass

        with self._lock:
            if self._current_job and self._current_job["id"] == job_id:
                self._current_job["status"] = "cancelled" if self._cancel_event.is_set() else "completed"
                self._current_job["finished_at"] = int(time.time())
                self._current_job["current_track"] = None
                summary = {
                    "id": self._current_job["id"],
                    "label": self._current_job["label"],
                    "device_label": self._current_job["device_label"],
                    "mountpoint": self._current_job["mountpoint"],
                    "status": self._current_job["status"],
                    "started_at": self._current_job["started_at"],
                    "finished_at": self._current_job["finished_at"],
                    "total_tracks": self._current_job["total_tracks"],
                    "copied": self._current_job["copied"],
                    "transcoded": self._current_job["transcoded"],
                    "skipped": self._current_job["skipped"],
                    "failed": self._current_job["failed"],
                    "covers_written": self._current_job["covers_written"],
                    "lyrics_written": self._current_job["lyrics_written"],
                    "transferred_bytes": self._current_job["transferred_bytes"],
                }
                self._history.insert(0, summary)
                self._history = self._history[:50]
                self._save_history()
