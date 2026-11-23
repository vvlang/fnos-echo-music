# -*- coding: utf-8 -*-
"""Library deduplication, organization, junk cleanup, and reversible recycle bin engine."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ConfigStore, get_data_dir
from .media_proc import (
    apply_metadata_to_copy,
    prepare_echo_cover,
    sanitize_filename,
    write_folder_cover,
    write_lrc_sidecar,
)
from .trim_music import AUDIO_EXTENSIONS, TrimMusicRepo

RECYCLE_DIR_NAME = ".echo_recycle"
JUNK_FILE_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

FORMAT_QUALITY_SCORE = {
    "dsf": 100,
    "dff": 98,
    "wav": 92,
    "aiff": 91,
    "aif": 91,
    "flac": 90,
    "ape": 85,
    "wv": 84,
    "alac": 88,
    "m4a": 62,
    "ogg": 60,
    "opus": 60,
    "mp3": 50,
    "aac": 52,
    "wma": 30,
    "dts": 70,
}

_SPAM_TITLE_PATTERNS = re.compile(
    r"(qq群|公众号|车载|dj舞曲|试音|track\s*\d+|未知|unknown|^[\d\s._-]+$|^[a-z]\+?$)",
    re.IGNORECASE,
)
_COPY_PATH_HINT = re.compile(r"(副本|copy|\(\d+\)|新建文件夹|备份|backup|temp|tmp)", re.IGNORECASE)
_BRACKET_CLEAN = re.compile(r"[\s\-_·・,.，。!！?？'\"`~()（）\[\]【】]+")


def _norm_key(text: str) -> str:
    s = (text or "").strip().lower()
    s = _BRACKET_CLEAN.sub("", s)
    return s


def _track_quality_score(t: Dict[str, Any]) -> Tuple[int, int, int, int, int, int, int]:
    """Higher tuple = better copy to KEEP."""
    suffix = (t.get("suffix") or "").lower().lstrip(".")
    fmt_score = FORMAT_QUALITY_SCORE.get(suffix, 40)
    # Treat high-bitrate M4A (>500kbps, likely ALAC) as lossless tier
    bitrate = int(t.get("bitrate") or 0)
    if suffix == "m4a" and bitrate >= 500_000:
        fmt_score = 88

    bit_depth = int(t.get("bit_depth") or 0)
    sample_rate = int(t.get("sample_rate") or 0)
    # Prioritize lossless > higher bit depth > higher sample rate > higher bitrate > has fav/cover/lyric > cleaner path > size
    is_fav = 1 if t.get("is_favorite") else 0
    has_meta = (1 if t.get("has_cover") else 0) + (1 if t.get("has_lyric") else 0)
    clean_path = 0 if _COPY_PATH_HINT.search(t.get("path") or "") else 1
    size = int(t.get("size") or 0)
    # Shorter path depth is generally better organized
    neg_path_len = -len(t.get("path") or "")
    return (fmt_score, bit_depth, sample_rate, bitrate // 1000, is_fav * 2 + has_meta, clean_path, size, neg_path_len)


def _quality_label(t: Dict[str, Any]) -> str:
    suf = (t.get("suffix") or "").upper()
    sr = int(t.get("sample_rate") or 0)
    bd = int(t.get("bit_depth") or 0)
    br = int(t.get("bitrate") or 0)
    if suf in ("DSF", "DFF"):
        return f"{suf} DSD"
    if suf in ("FLAC", "WAV", "APE", "AIFF", "WV"):
        if bd and sr:
            return f"{suf} {bd}bit/{sr / 1000:.1f}kHz"
        return f"{suf} 无损"
    if br:
        return f"{suf} {round(br / 1000)}kbps"
    return suf or "AUDIO"


def _match_owner_like(path: str, ref_dir: str) -> None:
    """Match uid:gid of newly created files/dirs to the library root so SMB/FileManager stays seamless."""
    try:
        st = os.stat(ref_dir)
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def _allowed_library_roots(repo: TrimMusicRepo) -> List[str]:
    ov = repo.get_overview()
    return [os.path.abspath(lib["path"]) for lib in ov.get("libraries", []) if lib.get("path")]


def _find_containing_root(path: str, roots: List[str]) -> Optional[str]:
    norm = os.path.abspath(path)
    for r in roots:
        if norm == r or norm.startswith(r + os.sep):
            return r
    return None


def _is_inside_recycle(path: str) -> bool:
    return f"{os.sep}{RECYCLE_DIR_NAME}{os.sep}" in path or path.endswith(f"{os.sep}{RECYCLE_DIR_NAME}")


# ==============================================================================
# 1. DEDUPLICATION SCANNER
# ==============================================================================

def scan_duplicates(
    repo: TrimMusicRepo,
    mode: str = "exact_hash",  # exact_hash | smart_song | same_filename
    library_id: Optional[int] = None,
    duration_tolerance_sec: int = 3,
    cross_format_only: bool = False,
    q: str = "",
    user_id: int = 1,
    page: int = 1,
    page_size: int = 25,
) -> Dict[str, Any]:
    """Scan trim.music DB for duplicate tracks and rank recommended keep vs clean candidates."""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    tol_ms = max(0, min(30, int(duration_tolerance_sec))) * 1000
    q_low = (q or "").strip().lower()

    groups: List[Dict[str, Any]] = []

    with repo._connect() as conn:
        lib_filter = ""
        base_params: List[Any] = []
        if library_id is not None:
            lib_filter = " AND t.shared_library_id = ?"
            base_params.append(library_id)

        fav_set = {
            r[0]
            for r in conn.execute("SELECT track_id FROM favorite_track WHERE user_id = ?", (user_id,))
        }
        lyric_set = {r[0] for r in conn.execute("SELECT DISTINCT track_id FROM lyric")}

        # Fetch all active non-CUE tracks in scope (fast in SQLite: ~150ms for 67k rows)
        rows = conn.execute(
            f"""
            SELECT t.id, t.title, t.album_id, t.cover_guid AS t_cover, t.duration_ms,
                   t.shared_library_id, t.year, t.disc_no, t.track_no, t.is_cue,
                   a.name AS album_name, a.cover_guid AS a_cover, a.metadata_mode AS a_mode,
                   af.id AS audio_file_id, af.path, af.name AS file_name, af.suffix,
                   af.size, af.bitrate, af.sample_rate, af.bit_depth, af.hash,
                   sl.path AS library_path,
                   (SELECT ar.name FROM track_artist ta JOIN artist ar ON ar.id=ta.artist_id
                    WHERE ta.track_id=t.id ORDER BY ta.artist_order, ta.id LIMIT 1) AS primary_artist
            FROM track t
            JOIN audio_file af ON af.id = t.audio_file_id
            JOIN shared_library sl ON sl.id = t.shared_library_id
            LEFT JOIN album a ON a.id = t.album_id
            WHERE t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0 AND t.is_cue = 0
              {lib_filter}
            """,
            base_params,
        ).fetchall()

    # Build lightweight dicts (skipping anything already moved into recycle or missing on disk)
    raw_items: List[Dict[str, Any]] = []
    seen_paths = set()
    for r in rows:
        p = r["path"] or ""
        if not p or p in seen_paths or _is_inside_recycle(p):
            continue
        if not os.path.isfile(p):
            continue
        seen_paths.add(p)
        tid = r["id"]
        t_cov = r["t_cover"]
        a_cov = r["a_cover"]
        cov_type = "album" if (a_cov and r["a_mode"] == 2) else ("track" if t_cov else ("album" if a_cov else None))
        cov_guid = a_cov if (a_cov and r["a_mode"] == 2) else (t_cov or a_cov)
        raw_items.append(
            {
                "id": tid,
                "audio_file_id": r["audio_file_id"],
                "title": r["title"] or "",
                "artist": r["primary_artist"] or "",
                "album": r["album_name"] or "",
                "duration_ms": int(r["duration_ms"] or 0),
                "path": p,
                "file_name": r["file_name"] or os.path.basename(p),
                "suffix": (r["suffix"] or "").lower().lstrip("."),
                "size": int(r["size"] or 0),
                "bitrate": int(r["bitrate"] or 0),
                "sample_rate": int(r["sample_rate"] or 0),
                "bit_depth": int(r["bit_depth"] or 0),
                "hash": r["hash"] or "",
                "shared_library_id": r["shared_library_id"],
                "library_path": r["library_path"] or "",
                "has_cover": bool(cov_guid),
                "cover_type": cov_type,
                "cover_guid": cov_guid,
                "has_lyric": tid in lyric_set,
                "is_favorite": tid in fav_set,
            }
        )

    buckets: Dict[str, List[Dict[str, Any]]] = {}

    if mode == "exact_hash":
        for it in raw_items:
            h = it["hash"]
            if h:
                buckets.setdefault(h, []).append(it)
    elif mode == "same_filename":
        for it in raw_items:
            stem = os.path.splitext(it["file_name"])[0].strip().lower()
            if len(stem) >= 2:
                buckets.setdefault(stem, []).append(it)
    else:  # smart_song: normalized (title + artist) + duration clustering
        prelim: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for it in raw_items:
            tk = _norm_key(it["title"])
            ak = _norm_key(it["artist"])
            if len(tk) < 2 or not ak:
                continue
            if _SPAM_TITLE_PATTERNS.search(it["title"]):
                continue
            if tk == ak:
                # Spam tags where title == artist (e.g. "魅派车载音乐")
                continue
            prelim.setdefault((tk, ak), []).append(it)

        cluster_idx = 0
        for (tk, ak), members in prelim.items():
            if len(members) < 2:
                continue
            # Sort by duration and split into duration-proximate clusters
            members.sort(key=lambda x: x["duration_ms"])
            clusters: List[List[Dict[str, Any]]] = []
            for m in members:
                if not clusters:
                    clusters.append([m])
                else:
                    prev_dur = clusters[-1][-1]["duration_ms"]
                    if m["duration_ms"] > 0 and prev_dur > 0 and abs(m["duration_ms"] - prev_dur) <= tol_ms:
                        clusters[-1].append(m)
                    elif m["duration_ms"] == 0 and prev_dur == 0:
                        clusters[-1].append(m)
                    else:
                        clusters.append([m])
            for cl in clusters:
                if len(cl) >= 2:
                    cluster_idx += 1
                    buckets[f"song_{cluster_idx}"] = cl

    # Score, rank, filter, and assemble groups
    total_reclaimable = 0
    total_dup_files = 0

    for gkey, members in buckets.items():
        if len(members) < 2:
            continue
        suffixes = {m["suffix"] for m in members}
        if cross_format_only and len(suffixes) < 2:
            continue
        if q_low:
            hay = " ".join(f"{m['title']} {m['artist']} {m['album']} {m['path']}" for m in members).lower()
            if q_low not in hay:
                continue

        # Sort descending by quality score -> index 0 is recommended keep
        ranked = sorted(members, key=_track_quality_score, reverse=True)
        keep_item = ranked[0]
        dup_items = ranked[1:]
        reclaim_bytes = sum(m["size"] for m in dup_items)
        total_reclaimable += reclaim_bytes
        total_dup_files += len(dup_items)

        enriched_members = []
        for idx, m in enumerate(ranked):
            enriched_members.append(
                {
                    **m,
                    "quality_label": _quality_label(m),
                    "recommended_keep": idx == 0,
                    "keep_reason": "最高音质/最佳版本" if idx == 0 else ("内容完全一致" if mode == "exact_hash" else "较低音质或重复副本"),
                }
            )

        groups.append(
            {
                "group_id": gkey,
                "title": keep_item["title"] or keep_item["file_name"],
                "artist": keep_item["artist"] or "未知歌手",
                "count": len(enriched_members),
                "is_cross_format": len(suffixes) > 1,
                "formats": sorted(suffixes),
                "reclaimable_bytes": reclaim_bytes,
                "items": enriched_members,
            }
        )

    # Sort groups by reclaimable bytes descending (biggest wins first!)
    groups.sort(key=lambda g: (g["reclaimable_bytes"], g["count"]), reverse=True)
    total_groups = len(groups)
    start = (page - 1) * page_size
    paged_groups = groups[start : start + page_size]

    return {
        "mode": mode,
        "total_groups": total_groups,
        "total_duplicate_files": total_dup_files,
        "total_reclaimable_bytes": total_reclaimable,
        "page": page,
        "page_size": page_size,
        "groups": paged_groups,
    }


def collect_recommended_duplicate_paths(
    repo: TrimMusicRepo,
    mode: str = "exact_hash",
    library_id: Optional[int] = None,
    duration_tolerance_sec: int = 3,
    cross_format_only: bool = False,
    q: str = "",
    user_id: int = 1,
) -> List[str]:
    """Return all non-recommended duplicate file paths across ALL pages of a dedup scan."""
    res = scan_duplicates(
        repo=repo,
        mode=mode,
        library_id=library_id,
        duration_tolerance_sec=duration_tolerance_sec,
        cross_format_only=cross_format_only,
        q=q,
        user_id=user_id,
        page=1,
        page_size=100_000,
    )
    paths: List[str] = []
    for g in res["groups"]:
        # Guardrail: always leave items[0] (recommended keep) untouched
        for item in g["items"][1:]:
            paths.append(item["path"])
    return paths


# ==============================================================================
# 2. REVERSIBLE RECYCLE BIN
# ==============================================================================

def _recycle_index_path() -> Path:
    return get_data_dir() / "recycle_batches.json"


def _load_recycle_batches() -> List[Dict[str, Any]]:
    fp = _recycle_index_path()
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _save_recycle_batches(batches: List[Dict[str, Any]]) -> None:
    fp = _recycle_index_path()
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(batches[:100], ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, fp)


def move_files_to_recycle(
    repo: TrimMusicRepo,
    file_paths: List[str],
    reason: str = "曲库去重清理",
    include_sidecar_lrc: bool = True,
) -> Dict[str, Any]:
    """Safely move duplicate audio files (and matching .lrc) to <library>/.echo_recycle/<batch_id>/."""
    roots = _allowed_library_roots(repo)
    if not roots:
        raise RuntimeError("未找到任何飞牛音乐媒体库路径")

    batch_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    moved_entries: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    total_bytes = 0

    unique_paths = list(dict.fromkeys(os.path.abspath(p) for p in file_paths if p))
    for src in unique_paths:
        root = _find_containing_root(src, roots)
        if not root:
            errors.append({"path": src, "error": "路径不在飞牛音乐媒体库内，已拒绝"})
            continue
        if _is_inside_recycle(src):
            continue
        if not os.path.isfile(src):
            continue

        # Collect main file + optional matching .lrc sidecar
        targets = [src]
        if include_sidecar_lrc:
            lrc = os.path.splitext(src)[0] + ".lrc"
            if os.path.isfile(lrc):
                targets.append(lrc)

        for fpath in targets:
            try:
                rel = os.path.relpath(fpath, root)
                dst = os.path.join(root, RECYCLE_DIR_NAME, batch_id, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _match_owner_like(os.path.join(root, RECYCLE_DIR_NAME), root)
                fsize = os.path.getsize(fpath)
                shutil.move(fpath, dst)
                moved_entries.append(
                    {
                        "orig_path": fpath,
                        "recycle_path": dst,
                        "library_root": root,
                        "size": fsize,
                    }
                )
                total_bytes += fsize
            except Exception as exc:
                errors.append({"path": fpath, "error": str(exc)[:200]})

    batch_record = {
        "batch_id": batch_id,
        "reason": reason,
        "created_at": int(time.time()),
        "file_count": len(moved_entries),
        "total_bytes": total_bytes,
        "items": moved_entries,
    }
    if moved_entries:
        batches = _load_recycle_batches()
        batches.insert(0, batch_record)
        _save_recycle_batches(batches)

    return {
        "batch_id": batch_id,
        "moved_count": len(moved_entries),
        "reclaimed_bytes": total_bytes,
        "errors": errors[:30],
    }


def list_recycle_batches() -> List[Dict[str, Any]]:
    batches = _load_recycle_batches()
    live: List[Dict[str, Any]] = []
    for b in batches:
        existing = [it for it in b.get("items", []) if os.path.exists(it.get("recycle_path", ""))]
        if existing:
            live.append(
                {
                    "batch_id": b["batch_id"],
                    "reason": b.get("reason", "去重清理"),
                    "created_at": b.get("created_at", 0),
                    "file_count": len(existing),
                    "total_bytes": sum(it.get("size", 0) for it in existing),
                    "sample_items": existing[:30],
                }
            )
    return live


def restore_recycle_batch(batch_id: str) -> Dict[str, Any]:
    batches = _load_recycle_batches()
    target = next((b for b in batches if b.get("batch_id") == batch_id), None)
    if not target:
        raise FileNotFoundError(f"回收站批次不存在: {batch_id}")

    restored = 0
    errors: List[Dict[str, str]] = []
    remaining = []
    touched_dirs = set()

    for it in target.get("items", []):
        rpath = it.get("recycle_path", "")
        opath = it.get("orig_path", "")
        if not os.path.exists(rpath):
            continue
        try:
            os.makedirs(os.path.dirname(opath), exist_ok=True)
            if os.path.exists(opath):
                # Avoid overwriting an already-existing file
                stem, ext = os.path.splitext(opath)
                opath = f"{stem}_restored{ext}"
            shutil.move(rpath, opath)
            touched_dirs.add(os.path.dirname(rpath))
            restored += 1
        except Exception as exc:
            errors.append({"path": rpath, "error": str(exc)[:200]})
            remaining.append(it)

    # Clean up empty dirs inside .echo_recycle/<batch_id>
    for lib_root in {it.get("library_root", "") for it in target.get("items", []) if it.get("library_root")}:
        batch_dir = os.path.join(lib_root, RECYCLE_DIR_NAME, batch_id)
        if os.path.isdir(batch_dir):
            _prune_empty_dirs_under(batch_dir, remove_root=True)

    if remaining:
        target["items"] = remaining
    else:
        batches = [b for b in batches if b.get("batch_id") != batch_id]
    _save_recycle_batches(batches)
    return {"batch_id": batch_id, "restored": restored, "errors": errors[:20]}


def purge_recycle_batch(batch_id: Optional[str] = None) -> Dict[str, Any]:
    """Permanently delete a specific batch (or all batches if batch_id is None)."""
    batches = _load_recycle_batches()
    to_purge = batches if batch_id is None else [b for b in batches if b.get("batch_id") == batch_id]
    if batch_id is not None and not to_purge:
        raise FileNotFoundError(f"回收站批次不存在: {batch_id}")

    deleted_files = 0
    freed_bytes = 0
    for b in to_purge:
        bid = b["batch_id"]
        roots = set()
        for it in b.get("items", []):
            rp = it.get("recycle_path", "")
            if it.get("library_root"):
                roots.add(it["library_root"])
            if os.path.isfile(rp):
                try:
                    freed_bytes += os.path.getsize(rp)
                    os.unlink(rp)
                    deleted_files += 1
                except OSError:
                    pass
        for lib_root in roots:
            bdir = os.path.join(lib_root, RECYCLE_DIR_NAME, bid)
            if os.path.isdir(bdir):
                shutil.rmtree(bdir, ignore_errors=True)

    new_batches = [] if batch_id is None else [b for b in batches if b.get("batch_id") != batch_id]
    _save_recycle_batches(new_batches)
    return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}


# ==============================================================================
# 3. JUNK FILES & EMPTY FOLDER CLEANER
# ==============================================================================

def _prune_empty_dirs_under(root_dir: str, remove_root: bool = False) -> int:
    removed = 0
    for cur, dirs, files in os.walk(root_dir, topdown=False):
        if not remove_root and os.path.abspath(cur) == os.path.abspath(root_dir):
            continue
        try:
            if not os.listdir(cur):
                os.rmdir(cur)
                removed += 1
        except OSError:
            pass
    return removed


def scan_library_health(repo: TrimMusicRepo, library_id: int) -> Dict[str, Any]:
    """Scan a library for junk files (._*, .DS_Store, Thumbs.db), empty directories, and orphan .lrc files."""
    ov = repo.get_overview()
    lib = next((l for l in ov.get("libraries", []) if l["id"] == library_id), None)
    if not lib:
        raise FileNotFoundError(f"媒体库不存在: {library_id}")
    root = os.path.abspath(lib["path"])
    if not os.path.isdir(root):
        raise FileNotFoundError(f"目录不存在: {root}")

    junk_files: List[Dict[str, Any]] = []
    empty_dirs: List[str] = []
    orphan_lrcs: List[Dict[str, Any]] = []
    total_junk_bytes = 0

    for cur, dirs, files in os.walk(root, topdown=False):
        # Skip recycle bin
        rel_cur = os.path.relpath(cur, root)
        if rel_cur == RECYCLE_DIR_NAME or rel_cur.startswith(RECYCLE_DIR_NAME + os.sep):
            continue

        audio_stems = set()
        non_junk_count = 0
        for fname in files:
            low = fname.lower()
            fpath = os.path.join(cur, fname)
            if fname.startswith("._") or low in JUNK_FILE_NAMES or low.endswith(".tmp_echo"):
                try:
                    sz = os.path.getsize(fpath)
                except OSError:
                    sz = 0
                junk_files.append({"path": fpath, "rel": os.path.relpath(fpath, root), "size": sz})
                total_junk_bytes += sz
            else:
                non_junk_count += 1
                stem, ext = os.path.splitext(fname)
                if ext.lower() in AUDIO_EXTENSIONS:
                    audio_stems.add(stem.lower())

        for fname in files:
            if fname.lower().endswith(".lrc") and not fname.startswith("._"):
                stem = os.path.splitext(fname)[0].lower()
                if stem not in audio_stems:
                    fpath = os.path.join(cur, fname)
                    try:
                        sz = os.path.getsize(fpath)
                    except OSError:
                        sz = 0
                    orphan_lrcs.append({"path": fpath, "rel": os.path.relpath(fpath, root), "size": sz})

        # Empty or contains only junk files and no non-empty subdirs
        real_subdirs = [
            d for d in dirs
            if d != RECYCLE_DIR_NAME and os.path.join(cur, d) not in empty_dirs
        ]
        if cur != root and non_junk_count == 0 and not real_subdirs:
            empty_dirs.append(cur)

    # Count missing physical files in DB
    missing_in_db = 0
    with repo._connect() as conn:
        for (p,) in conn.execute(
            """
            SELECT af.path FROM audio_file af
            WHERE af.shared_library_id = ? AND af.is_physical_file_deleted = 0
            """,
            (library_id,),
        ):
            if p and not os.path.isfile(p):
                missing_in_db += 1

    return {
        "library_id": library_id,
        "library_path": root,
        "junk_count": len(junk_files),
        "junk_bytes": total_junk_bytes,
        "junk_samples": junk_files[:100],
        "empty_dir_count": len(empty_dirs),
        "empty_dir_samples": [os.path.relpath(d, root) for d in empty_dirs[:100]],
        "orphan_lrc_count": len(orphan_lrcs),
        "orphan_lrc_samples": orphan_lrcs[:100],
        "missing_db_files": missing_in_db,
    }


def clean_library_junk(
    repo: TrimMusicRepo,
    library_id: int,
    remove_junk_files: bool = True,
    remove_empty_dirs: bool = True,
    remove_orphan_lrcs: bool = False,
) -> Dict[str, Any]:
    """Delete OS junk files, empty dirs, and optionally orphan .lrc files inside a library."""
    ov = repo.get_overview()
    lib = next((l for l in ov.get("libraries", []) if l["id"] == library_id), None)
    if not lib:
        raise FileNotFoundError(f"媒体库不存在: {library_id}")
    root = os.path.abspath(lib["path"])

    removed_junk = 0
    removed_orphan = 0
    freed_bytes = 0

    for cur, dirs, files in os.walk(root, topdown=False):
        rel_cur = os.path.relpath(cur, root)
        if rel_cur == RECYCLE_DIR_NAME or rel_cur.startswith(RECYCLE_DIR_NAME + os.sep):
            continue

        audio_stems = {
            os.path.splitext(f)[0].lower()
            for f in files
            if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS and not f.startswith("._")
        }

        for fname in files:
            low = fname.lower()
            fpath = os.path.join(cur, fname)
            if remove_junk_files and (fname.startswith("._") or low in JUNK_FILE_NAMES or low.endswith(".tmp_echo")):
                try:
                    freed_bytes += os.path.getsize(fpath)
                    os.unlink(fpath)
                    removed_junk += 1
                except OSError:
                    pass
            elif remove_orphan_lrcs and low.endswith(".lrc") and not fname.startswith("._"):
                if os.path.splitext(fname)[0].lower() not in audio_stems:
                    try:
                        freed_bytes += os.path.getsize(fpath)
                        os.unlink(fpath)
                        removed_orphan += 1
                    except OSError:
                        pass

    removed_dirs = 0
    if remove_empty_dirs:
        for cur, dirs, files in os.walk(root, topdown=False):
            rel_cur = os.path.relpath(cur, root)
            if cur == root or rel_cur == RECYCLE_DIR_NAME or rel_cur.startswith(RECYCLE_DIR_NAME + os.sep):
                continue
            try:
                if not os.listdir(cur):
                    os.rmdir(cur)
                    removed_dirs += 1
            except OSError:
                pass

    return {
        "removed_junk": removed_junk,
        "removed_orphan_lrcs": removed_orphan,
        "removed_empty_dirs": removed_dirs,
        "freed_bytes": freed_bytes,
    }


# ==============================================================================
# 4. DIRECTORY ORGANIZE & SCRAPED ASSET EXPORT (PREVIEW + BACKGROUND RUNNER)
# ==============================================================================

def _build_organized_rel_path(track: Dict[str, Any], layout: str, rename_file: bool) -> str:
    artist_dir = sanitize_filename(track.get("album_artist") or track.get("artist") or "未知歌手")
    album_dir = sanitize_filename(track.get("album") or "单曲集")
    orig_name = os.path.basename(track["path"])
    orig_stem, orig_ext = os.path.splitext(orig_name)

    if rename_file:
        tno = track.get("track_no")
        prefix = f"{int(tno):02d} " if tno and int(tno) > 0 else ""
        title = sanitize_filename(track.get("title") or orig_stem)
        fname = f"{prefix}{title}{orig_ext.lower()}"
    else:
        fname = sanitize_filename(orig_stem) + orig_ext.lower()

    if layout == "album":
        return os.path.join(album_dir, fname)
    return os.path.join(artist_dir, album_dir, fname)


def preview_organize_plan(
    repo: TrimMusicRepo,
    library_id: int,
    rel_subpath: str = "",
    layout: str = "artist_album",  # artist_album | album
    rename_files: bool = False,
    limit: int = 200,
) -> Dict[str, Any]:
    """Generate a dry-run preview of organizing files inside a library or subfolder."""
    ov = repo.get_overview()
    lib = next((l for l in ov.get("libraries", []) if l["id"] == library_id), None)
    if not lib:
        raise FileNotFoundError(f"媒体库不存在: {library_id}")
    lib_root = os.path.abspath(lib["path"])
    clean_sub = (rel_subpath or "").strip().strip("/")
    scope_dir = os.path.abspath(os.path.join(lib_root, clean_sub)) if clean_sub else lib_root
    if scope_dir != lib_root and not scope_dir.startswith(lib_root + os.sep):
        raise PermissionError("路径越界，已拒绝")

    tids = repo.get_track_ids_under_folder(scope_dir)
    tracks = repo.get_tracks_by_ids(tids)

    planned: List[Dict[str, Any]] = []
    already_clean = 0
    skipped_cue = 0
    conflicts = 0
    seen_targets = set()

    for t in tracks:
        src = t.get("path") or ""
        if not src or _is_inside_recycle(src) or not os.path.isfile(src):
            continue
        if t.get("is_cue"):
            skipped_cue += 1
            continue
        dst_rel = _build_organized_rel_path(t, layout=layout, rename_file=rename_files)
        dst = os.path.join(lib_root, dst_rel)
        if os.path.abspath(src) == os.path.abspath(dst):
            already_clean += 1
            continue
        if dst in seen_targets or os.path.exists(dst):
            conflicts += 1
            continue
        seen_targets.add(dst)
        planned.append(
            {
                "track_id": t["id"],
                "title": t["title"],
                "artist": t["artist"],
                "album": t["album"],
                "src": src,
                "src_rel": os.path.relpath(src, lib_root),
                "dst": dst,
                "dst_rel": dst_rel,
            }
        )

    return {
        "library_id": library_id,
        "library_path": lib_root,
        "scope_rel": clean_sub,
        "total_scanned": len(tracks),
        "to_move": len(planned),
        "already_clean": already_clean,
        "skipped_cue": skipped_cue,
        "conflicts": conflicts,
        "samples": planned[:limit],
    }


class OrganizeJobManager:
    """Background runner for heavy library operations (bulk dedup, directory organize, scraped asset export)."""

    def __init__(self, cfg_store: ConfigStore):
        self._cfg_store = cfg_store
        self._lock = threading.Lock()
        self._job: Optional[Dict[str, Any]] = None
        self._cancel = threading.Event()

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._job) if self._job else {"status": "idle"}

    def cancel(self) -> bool:
        with self._lock:
            if self._job and self._job["status"] == "running":
                self._cancel.set()
                self._job["cancelling"] = True
                return True
        return False

    def _begin(self, kind: str, label: str, total: int) -> Dict[str, Any]:
        with self._lock:
            if self._job and self._job["status"] == "running":
                raise RuntimeError("当前已有整理任务在运行，请等待完成或先取消")
            self._cancel.clear()
            self._job = {
                "id": uuid.uuid4().hex[:10],
                "kind": kind,
                "label": label,
                "status": "running",
                "cancelling": False,
                "started_at": int(time.time()),
                "finished_at": None,
                "total": total,
                "processed": 0,
                "succeeded": 0,
                "skipped": 0,
                "failed": 0,
                "reclaimed_bytes": 0,
                "current_item": "",
                "result": {},
                "errors": [],
            }
            return dict(self._job)

    def _tick(self, current: str = "", succeeded: int = 0, skipped: int = 0, failed: int = 0, bytes_delta: int = 0, err: Optional[str] = None) -> None:
        with self._lock:
            if not self._job:
                return
            self._job["processed"] += 1
            self._job["succeeded"] += succeeded
            self._job["skipped"] += skipped
            self._job["failed"] += failed
            self._job["reclaimed_bytes"] += bytes_delta
            if current:
                self._job["current_item"] = current
            if err and len(self._job["errors"]) < 40:
                self._job["errors"].append(err[:220])

    def _finish(self, result: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if not self._job:
                return
            self._job["status"] = "cancelled" if self._cancel.is_set() else "completed"
            self._job["finished_at"] = int(time.time())
            self._job["current_item"] = ""
            if result:
                self._job["result"] = result

    def start_organize_move(
        self,
        repo: TrimMusicRepo,
        library_id: int,
        rel_subpath: str = "",
        layout: str = "artist_album",
        rename_files: bool = False,
        export_scraped_assets: bool = False,
        clean_empty_dirs: bool = True,
        user_id: int = 1,
    ) -> Dict[str, Any]:
        plan = preview_organize_plan(
            repo, library_id, rel_subpath=rel_subpath, layout=layout, rename_files=rename_files, limit=200_000
        )
        items = plan["samples"]
        if not items:
            raise ValueError("所选范围内没有需要移动整理的曲目（均已规范或存在冲突）")

        job = self._begin("organize", f"目录归档整理 ({len(items)} 首)", len(items))
        lib_root = plan["library_path"]
        cfg = self._cfg_store.load()

        def _worker():
            moved_records = []
            written_covers: set[str] = set()
            track_map = {t["id"]: t for t in repo.get_tracks_by_ids([x["track_id"] for x in items], user_id=user_id)}
            for it in items:
                if self._cancel.is_set():
                    break
                src, dst = it["src"], it["dst"]
                try:
                    if not os.path.isfile(src) or os.path.exists(dst):
                        self._tick(it["title"], skipped=1)
                        continue
                    dst_dir = os.path.dirname(dst)
                    os.makedirs(dst_dir, exist_ok=True)
                    _match_owner_like(dst_dir, lib_root)
                    shutil.move(src, dst)
                    moved_records.append({"from": src, "to": dst})

                    # Move accompanying .lrc if present
                    src_lrc = os.path.splitext(src)[0] + ".lrc"
                    dst_lrc = os.path.splitext(dst)[0] + ".lrc"
                    if os.path.isfile(src_lrc) and not os.path.exists(dst_lrc):
                        shutil.move(src_lrc, dst_lrc)
                        moved_records.append({"from": src_lrc, "to": dst_lrc})

                    # Optionally export scraped folder.jpg & .lrc sidecar
                    if export_scraped_assets and it["track_id"] in track_map:
                        t = track_map[it["track_id"]]
                        if dst_dir not in written_covers:
                            cpath, _ = repo.resolve_cover_file(
                                t.get("track_cover_guid"), t.get("album_cover_guid"), t.get("album_metadata_mode"), dst
                            )
                            if cpath:
                                try:
                                    jpg, _, _ = prepare_echo_cover(
                                        cpath,
                                        max_size=int(cfg.get("cover_max_size", 500)),
                                        quality=int(cfg.get("cover_quality", 88)),
                                    )
                                    write_folder_cover(dst_dir, jpg, overwrite=False)
                                    _match_owner_like(os.path.join(dst_dir, "folder.jpg"), lib_root)
                                except Exception:
                                    pass
                            written_covers.add(dst_dir)
                        if not os.path.isfile(dst_lrc):
                            linfo = repo.resolve_lyric(t["id"], user_id=user_id)
                            if linfo and linfo.get("content"):
                                write_lrc_sidecar(dst, linfo["content"])
                                _match_owner_like(dst_lrc, lib_root)

                    self._tick(it["title"], succeeded=1)
                except Exception as exc:
                    self._tick(it["title"], failed=1, err=f"{it['src_rel']}: {exc}")

            removed_dirs = _prune_empty_dirs_under(lib_root, remove_root=False) if clean_empty_dirs else 0
            self._finish({"moved_files": len(moved_records), "removed_empty_dirs": removed_dirs})

        threading.Thread(target=_worker, daemon=True).start()
        return job

    def start_export_scraped_assets(
        self,
        repo: TrimMusicRepo,
        library_id: int,
        rel_subpath: str = "",
        export_folder_jpg: bool = True,
        export_lrc: bool = True,
        embed_into_audio: bool = False,
        overwrite: bool = False,
        user_id: int = 1,
    ) -> Dict[str, Any]:
        """Export fnOS Music scraped covers (folder.jpg) and .lrc sidecars directly into the NAS library."""
        ov = repo.get_overview()
        lib = next((l for l in ov.get("libraries", []) if l["id"] == library_id), None)
        if not lib:
            raise FileNotFoundError(f"媒体库不存在: {library_id}")
        lib_root = os.path.abspath(lib["path"])
        clean_sub = (rel_subpath or "").strip().strip("/")
        scope_dir = os.path.abspath(os.path.join(lib_root, clean_sub)) if clean_sub else lib_root
        if scope_dir != lib_root and not scope_dir.startswith(lib_root + os.sep):
            raise PermissionError("路径越界，已拒绝")

        tids = repo.get_track_ids_under_folder(scope_dir)
        tracks = repo.get_tracks_by_ids(tids, user_id=user_id)
        if not tracks:
            raise ValueError("所选范围内没有曲目")

        job = self._begin("export_assets", f"补齐 NAS 刮削封面与歌词 ({len(tracks)} 首)", len(tracks))
        cfg = self._cfg_store.load()

        def _worker():
            cover_cache: Dict[str, Tuple[bytes, int, int]] = {}
            written_dirs: set[str] = set()
            covers_ok = 0
            lyrics_ok = 0
            embedded_ok = 0

            for t in tracks:
                if self._cancel.is_set():
                    break
                src = t.get("path") or ""
                if not src or not os.path.isfile(src) or _is_inside_recycle(src):
                    self._tick(t["title"], skipped=1)
                    continue
                did_any = False
                try:
                    tdir = os.path.dirname(src)
                    cover_jpeg = None
                    cover_dims = (500, 500)
                    if export_folder_jpg or embed_into_audio:
                        cpath, _ = repo.resolve_cover_file(
                            t.get("track_cover_guid"), t.get("album_cover_guid"), t.get("album_metadata_mode"), src
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
                            if cover_cache[cpath][0]:
                                cover_jpeg, cover_dims = cover_cache[cpath][0], (cover_cache[cpath][1], cover_cache[cpath][2])

                    if export_folder_jpg and cover_jpeg and tdir not in written_dirs:
                        if write_folder_cover(tdir, cover_jpeg, overwrite=overwrite):
                            _match_owner_like(os.path.join(tdir, "folder.jpg"), lib_root)
                            covers_ok += 1
                            did_any = True
                        written_dirs.add(tdir)

                    lyric_text = None
                    if export_lrc or embed_into_audio:
                        linfo = repo.resolve_lyric(t["id"], user_id=user_id)
                        if linfo and linfo.get("content"):
                            lyric_text = linfo["content"]

                    if export_lrc and lyric_text:
                        lrc_dst = os.path.splitext(src)[0] + ".lrc"
                        if overwrite or not (os.path.isfile(lrc_dst) and os.path.getsize(lrc_dst) > 0):
                            write_lrc_sidecar(src, lyric_text)
                            _match_owner_like(lrc_dst, lib_root)
                            lyrics_ok += 1
                            did_any = True

                    if embed_into_audio and not t.get("is_cue"):
                        res = apply_metadata_to_copy(
                            src,
                            t,
                            cover_jpeg=cover_jpeg,
                            cover_dims=cover_dims,
                            lyric_text=lyric_text,
                            write_tags=True,
                            embed_cover=True,
                            embed_lyrics=True,
                        )
                        if any(res.values()):
                            embedded_ok += 1
                            did_any = True

                    self._tick(t["title"], succeeded=1 if did_any else 0, skipped=0 if did_any else 1)
                except Exception as exc:
                    self._tick(t["title"], failed=1, err=f"{t['title']}: {exc}")

            self._finish({"covers_written": covers_ok, "lyrics_written": lyrics_ok, "embedded_tracks": embedded_ok})

        threading.Thread(target=_worker, daemon=True).start()
        return job
