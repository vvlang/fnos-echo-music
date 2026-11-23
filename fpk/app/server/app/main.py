# -*- coding: utf-8 -*-
"""FastAPI backend for Echo Music Sync (fnOS Music -> Echo DAP)."""
from __future__ import annotations

import mimetypes
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import config_store
from .media_proc import prepare_echo_cover
from .organizer import (
    OrganizeJobManager,
    clean_library_junk,
    collect_recommended_duplicate_paths,
    list_recycle_batches,
    move_files_to_recycle,
    preview_organize_plan,
    purge_recycle_batch,
    restore_recycle_batch,
    scan_duplicates,
    scan_library_health,
)
from .storage import (
    browse_device_files,
    delete_device_path,
    list_external_devices,
    sync_mount,
)
from .transfer import TransferManager
from .trim_music import TrimMusicRepo

APP_VERSION = "1.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
GATEWAY_PREFIX = os.environ.get("ECHO_GATEWAY_PREFIX", "").rstrip("/")

app = FastAPI(title="Echo 音乐传输", version=APP_VERSION)
transfer_manager = TransferManager(config_store)
organize_manager = OrganizeJobManager(config_store)


@app.middleware("http")
async def strip_gateway_prefix(request: Request, call_next):
    if GATEWAY_PREFIX:
        path = request.scope.get("path", "")
        if path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/"):
            new_path = path[len(GATEWAY_PREFIX) :] or "/"
            request.scope["path"] = new_path
            request.scope["raw_path"] = new_path.encode("utf-8")
    return await call_next(request)


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _get_repo() -> TrimMusicRepo:
    cfg = config_store.load()
    return TrimMusicRepo(cfg["trim_music_db"], cfg["trim_music_meta"])


def _detect_image_mime(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            hdr = fh.read(16)
        if hdr.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if hdr.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if hdr[:4] == b"RIFF" and hdr[8:12] == b"WEBP":
            return "image/webp"
        if hdr.startswith(b"GIF8"):
            return "image/gif"
    except OSError:
        pass
    return "application/octet-stream"


# ---------- Request Models ----------

class ConfigUpdateReq(BaseModel):
    updates: Dict[str, Any] = Field(default_factory=dict)


class TransferStartReq(BaseModel):
    mountpoint: str
    track_ids: List[int] = Field(default_factory=list)
    album_ids: List[int] = Field(default_factory=list)
    artist_ids: List[int] = Field(default_factory=list)
    playlist_id: Optional[int] = None
    favorites: bool = False
    folder_path: Optional[str] = None
    label: Optional[str] = None
    user_id: int = 1
    overrides: Optional[Dict[str, Any]] = None


class DeviceActionReq(BaseModel):
    mountpoint: str
    rel_path: Optional[str] = None


class DedupCleanReq(BaseModel):
    file_paths: List[str] = Field(default_factory=list)
    reason: str = "曲库去重清理"
    include_sidecar_lrc: bool = True


class DedupCleanAllReq(BaseModel):
    mode: str = "exact_hash"
    library_id: Optional[int] = None
    duration_tolerance_sec: int = 3
    cross_format_only: bool = False
    q: str = ""
    user_id: int = 1
    include_sidecar_lrc: bool = True


class RecycleBatchReq(BaseModel):
    batch_id: Optional[str] = None


class JunkCleanReq(BaseModel):
    library_id: int
    remove_junk_files: bool = True
    remove_empty_dirs: bool = True
    remove_orphan_lrcs: bool = False


class OrganizeExecuteReq(BaseModel):
    library_id: int
    rel_subpath: str = ""
    layout: str = "artist_album"
    rename_files: bool = False
    export_scraped_assets: bool = False
    clean_empty_dirs: bool = True
    user_id: int = 1


class ExportAssetsReq(BaseModel):
    library_id: int
    rel_subpath: str = ""
    export_folder_jpg: bool = True
    export_lrc: bool = True
    embed_into_audio: bool = False
    overwrite: bool = False
    user_id: int = 1


# ---------- Frontend Entry ----------

@app.get("/")
def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(index_path):
        raise HTTPException(404, "前端资源缺失")
    with open(index_path, "r", encoding="utf-8") as fh:
        html = fh.read()
    base_href = f"{GATEWAY_PREFIX}/" if GATEWAY_PREFIX else "/"
    html = html.replace("<!--__BASE_TAG__-->", f'<base href="{base_href}" />')
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/health")
def health():
    repo = _get_repo()
    devices = list_external_devices()
    return {
        "ok": True,
        "version": APP_VERSION,
        "trim_music_available": repo.is_available(),
        "external_device_count": len(devices),
    }


# ---------- Config & Overview ----------

@app.get("/api/overview")
def api_overview(user_id: int = Query(1, ge=1)):
    repo = _get_repo()
    data = repo.get_overview(user_id=user_id)
    data["config"] = config_store.load()
    data["devices"] = list_external_devices()
    data["version"] = APP_VERSION
    return data


@app.get("/api/config")
def api_get_config():
    return config_store.load()


@app.post("/api/config")
def api_update_config(req: ConfigUpdateReq):
    return config_store.update(req.updates)


# ---------- Library Browsing ----------

@app.get("/api/tracks")
def api_list_tracks(
    q: str = "",
    library_id: Optional[int] = None,
    album_id: Optional[int] = None,
    artist_id: Optional[int] = None,
    playlist_id: Optional[int] = None,
    favorites: bool = False,
    has_lyric: Optional[bool] = None,
    has_cover: Optional[bool] = None,
    suffix: str = "",
    user_id: int = 1,
    sort: str = "default",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    repo = _get_repo()
    try:
        return repo.list_tracks(
            q=q,
            library_id=library_id,
            album_id=album_id,
            artist_id=artist_id,
            playlist_id=playlist_id,
            favorites_only=favorites,
            has_lyric=has_lyric,
            has_cover=has_cover,
            suffix=suffix,
            user_id=user_id,
            sort=sort,
            page=page,
            page_size=page_size,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/tracks/{track_id}")
def api_track_detail(track_id: int, user_id: int = 1):
    repo = _get_repo()
    tracks = repo.get_tracks_by_ids([track_id], user_id=user_id)
    if not tracks:
        raise HTTPException(404, "曲目不存在")
    track = tracks[0]
    cover_path, cover_source = repo.resolve_cover_file(
        track.get("track_cover_guid"),
        track.get("album_cover_guid"),
        track.get("album_metadata_mode"),
        track.get("path"),
    )
    cover_mime = _detect_image_mime(cover_path) if cover_path else ""
    lyric = repo.resolve_lyric(track_id, user_id=user_id)
    track["cover_resolved"] = bool(cover_path)
    track["cover_source"] = cover_source
    track["cover_mime"] = cover_mime
    track["cover_size_bytes"] = os.path.getsize(cover_path) if cover_path and os.path.isfile(cover_path) else 0
    track["lyric"] = lyric
    track["file_exists"] = os.path.isfile(track.get("path") or "")
    return track


@app.get("/api/tracks/{track_id}/stream")
def api_stream_track(track_id: int):
    repo = _get_repo()
    tracks = repo.get_tracks_by_ids([track_id])
    if not tracks:
        raise HTTPException(404, "曲目不存在")
    fp = tracks[0].get("path") or ""
    if not os.path.isfile(fp):
        raise HTTPException(404, "音频文件不存在")
    mime, _ = mimetypes.guess_type(fp)
    return FileResponse(fp, media_type=mime or "application/octet-stream")


@app.get("/api/albums")
def api_list_albums(
    q: str = "",
    library_id: Optional[int] = None,
    artist_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=200),
):
    repo = _get_repo()
    try:
        return repo.list_albums(q=q, library_id=library_id, artist_id=artist_id, page=page, page_size=page_size)
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/artists")
def api_list_artists(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(48, ge=1, le=200),
):
    repo = _get_repo()
    try:
        return repo.list_artists(q=q, page=page, page_size=page_size)
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/playlists")
def api_list_playlists(user_id: int = 1):
    repo = _get_repo()
    try:
        return {"items": repo.list_playlists(user_id=user_id)}
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/folders")
def api_browse_folder(library_id: int, rel_path: str = "", user_id: int = 1):
    repo = _get_repo()
    try:
        return repo.browse_folder(library_id=library_id, rel_path=rel_path, user_id=user_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/cover/{entity_type}/{guid}")
def api_get_cover(entity_type: str, guid: str, thumb: int = Query(0, ge=0, le=1000)):
    if entity_type not in ("track", "album", "artist") or "/" in guid or ".." in guid:
        raise HTTPException(400, "非法封面参数")
    repo = _get_repo()
    fp = repo.resolve_entity_cover_file(entity_type, guid)
    if not fp:
        raise HTTPException(404, "封面不存在")
    if thumb and thumb > 0:
        try:
            jpg, _, _ = prepare_echo_cover(fp, max_size=thumb, quality=82)
            return Response(
                content=jpg,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception:
            pass
    mime = _detect_image_mime(fp)
    return FileResponse(
        fp,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------- Echo External Storage Devices ----------

@app.get("/api/devices")
def api_list_devices():
    return {"devices": list_external_devices()}


@app.get("/api/devices/browse")
def api_browse_device(mountpoint: str, rel_path: str = ""):
    try:
        return browse_device_files(mountpoint, rel_path)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/devices/delete")
def api_delete_device_path(req: DeviceActionReq):
    try:
        return delete_device_path(req.mountpoint, req.rel_path or "")
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/devices/sync")
def api_sync_device(req: DeviceActionReq):
    return sync_mount(req.mountpoint)


# ---------- Transfer Engine ----------

@app.post("/api/transfer/start")
def api_transfer_start(req: TransferStartReq):
    repo = _get_repo()
    collected_ids: List[int] = list(req.track_ids)
    playlist_name: Optional[str] = None
    label = req.label

    if req.album_ids:
        collected_ids.extend(repo.get_track_ids_for_albums(req.album_ids))
        label = label or f"发送 {len(req.album_ids)} 张专辑到 Echo"
    if req.artist_ids:
        collected_ids.extend(repo.get_track_ids_for_artists(req.artist_ids))
        label = label or f"发送 {len(req.artist_ids)} 位歌手曲目到 Echo"
    if req.playlist_id is not None:
        pl_name, pl_ids = repo.get_track_ids_for_playlist(req.playlist_id)
        if pl_name is None:
            raise HTTPException(404, "歌单不存在")
        collected_ids.extend(pl_ids)
        playlist_name = pl_name
        label = label or f"同步歌单「{pl_name}」到 Echo"
    if req.favorites:
        fav_ids = repo.get_track_ids_for_favorites(user_id=req.user_id)
        collected_ids.extend(fav_ids)
        playlist_name = playlist_name or "我喜欢的音乐"
        label = label or "同步「我喜欢的音乐」到 Echo"
    if req.folder_path:
        collected_ids.extend(repo.get_track_ids_under_folder(req.folder_path))
        label = label or f"发送目录「{os.path.basename(req.folder_path.rstrip('/'))}」到 Echo"

    unique_ids = list(dict.fromkeys(collected_ids))
    if not unique_ids:
        raise HTTPException(400, "没有选中的可发送曲目")

    label = label or f"发送 {len(unique_ids)} 首曲目到 Echo"

    try:
        # Remember chosen Echo mountpoint for convenience
        config_store.update({"target_mount": req.mountpoint})
        job = transfer_manager.start_job(
            repo=repo,
            track_ids=unique_ids,
            mountpoint=req.mountpoint,
            label=label,
            playlist_name=playlist_name,
            overrides=req.overrides,
            user_id=req.user_id,
        )
        return JSONResponse(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/transfer/status")
def api_transfer_status():
    return transfer_manager.get_status()


@app.post("/api/transfer/cancel")
def api_transfer_cancel():
    cancelled = transfer_manager.cancel_current()
    return {"cancelled": cancelled}


# ---------- Library Deduplication & Organization ----------

@app.get("/api/dedup/scan")
def api_dedup_scan(
    mode: str = Query("exact_hash"),
    library_id: Optional[int] = None,
    duration_tolerance_sec: int = Query(3, ge=0, le=30),
    cross_format_only: bool = False,
    q: str = "",
    user_id: int = 1,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    repo = _get_repo()
    try:
        return scan_duplicates(
            repo=repo,
            mode=mode,
            library_id=library_id,
            duration_tolerance_sec=duration_tolerance_sec,
            cross_format_only=cross_format_only,
            q=q,
            user_id=user_id,
            page=page,
            page_size=page_size,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/dedup/clean")
def api_dedup_clean(req: DedupCleanReq):
    if not req.file_paths:
        raise HTTPException(400, "未选择需要清理的重复文件")
    repo = _get_repo()
    try:
        return move_files_to_recycle(
            repo=repo,
            file_paths=req.file_paths,
            reason=req.reason,
            include_sidecar_lrc=req.include_sidecar_lrc,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/dedup/clean-all")
def api_dedup_clean_all(req: DedupCleanAllReq):
    repo = _get_repo()
    try:
        paths = collect_recommended_duplicate_paths(
            repo=repo,
            mode=req.mode,
            library_id=req.library_id,
            duration_tolerance_sec=req.duration_tolerance_sec,
            cross_format_only=req.cross_format_only,
            q=req.q,
            user_id=req.user_id,
        )
        if not paths:
            return {"batch_id": "", "moved_count": 0, "reclaimed_bytes": 0, "errors": []}
        mode_labels = {
            "exact_hash": "一键精确哈希去重",
            "smart_song": "一键同曲多音质去重(保留最高音质)",
            "same_filename": "一键同名文件去重",
        }
        return move_files_to_recycle(
            repo=repo,
            file_paths=paths,
            reason=mode_labels.get(req.mode, "一键去重清理"),
            include_sidecar_lrc=req.include_sidecar_lrc,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/recycle")
def api_list_recycle():
    return {"batches": list_recycle_batches()}


@app.post("/api/recycle/restore")
def api_restore_recycle(req: RecycleBatchReq):
    if not req.batch_id:
        raise HTTPException(400, "缺少回收站批次 ID")
    try:
        return restore_recycle_batch(req.batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/recycle/purge")
def api_purge_recycle(req: RecycleBatchReq):
    try:
        return purge_recycle_batch(req.batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/organize/health")
def api_organize_health(library_id: int):
    repo = _get_repo()
    try:
        return scan_library_health(repo, library_id=library_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/organize/clean-junk")
def api_organize_clean_junk(req: JunkCleanReq):
    repo = _get_repo()
    try:
        return clean_library_junk(
            repo,
            library_id=req.library_id,
            remove_junk_files=req.remove_junk_files,
            remove_empty_dirs=req.remove_empty_dirs,
            remove_orphan_lrcs=req.remove_orphan_lrcs,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/organize/preview")
def api_organize_preview(
    library_id: int,
    rel_subpath: str = "",
    layout: str = "artist_album",
    rename_files: bool = False,
    limit: int = Query(150, ge=10, le=500),
):
    repo = _get_repo()
    try:
        return preview_organize_plan(
            repo,
            library_id=library_id,
            rel_subpath=rel_subpath,
            layout=layout,
            rename_files=rename_files,
            limit=limit,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/organize/execute")
def api_organize_execute(req: OrganizeExecuteReq):
    repo = _get_repo()
    try:
        return organize_manager.start_organize_move(
            repo=repo,
            library_id=req.library_id,
            rel_subpath=req.rel_subpath,
            layout=req.layout,
            rename_files=req.rename_files,
            export_scraped_assets=req.export_scraped_assets,
            clean_empty_dirs=req.clean_empty_dirs,
            user_id=req.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/organize/export-assets")
def api_organize_export_assets(req: ExportAssetsReq):
    repo = _get_repo()
    try:
        return organize_manager.start_export_scraped_assets(
            repo=repo,
            library_id=req.library_id,
            rel_subpath=req.rel_subpath,
            export_folder_jpg=req.export_folder_jpg,
            export_lrc=req.export_lrc,
            embed_into_audio=req.embed_into_audio,
            overwrite=req.overwrite,
            user_id=req.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/organize/status")
def api_organize_status():
    return organize_manager.get_status()


@app.post("/api/organize/cancel")
def api_organize_cancel():
    return {"cancelled": organize_manager.cancel()}
