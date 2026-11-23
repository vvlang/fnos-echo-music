# -*- coding: utf-8 -*-
"""Read-only data engine for fnOS Music (trim.music).

Reads:
- /var/apps/trim.music/var/db/music.db (strictly read-only URI mode=ro)
- /var/apps/trim.music/meta/cover/{track,album,artist}/{guid[:2]}/{guid}
- /var/apps/trim.music/meta/lyric-sqlite/lyric-0{hex}.db
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ECHO_NATIVE_SUFFIXES = {"flac", "mp3", "m4a", "wav", "ogg", "dsf"}
ECHO_TRANSCODABLE_SUFFIXES = {"wma", "ape", "dff", "aiff", "aif", "dts", "opus", "wv", "aac"}
AUDIO_EXTENSIONS = {f".{s}" for s in ECHO_NATIVE_SUFFIXES | ECHO_TRANSCODABLE_SUFFIXES}
FOLDER_COVER_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
    "front.jpg", "front.jpeg", "front.png", "front.webp",
    "albumart.jpg", "albumart.jpeg", "albumart.png",
)

LYRIC_SOURCE_LABELS = {
    1: "文件内嵌",
    2: "伴随文件",
    3: "飞牛刮削",
    4: "用户关联",
}

LYRIC_SOURCE_PRIORITY = {
    4: 100,
    3: 90,
    2: 80,
    1: 70,
}

_LRC_TS_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def _shift_lrc_timestamps(text: str, offset_ms: int) -> str:
    """Apply user_track_lyric_offset (ms; positive=earlier, negative=later)."""
    if not offset_ms:
        return text

    def _repl(match: re.Match[str]) -> str:
        mins = int(match.group(1))
        secs = int(match.group(2))
        frac_str = match.group(3)
        if frac_str is None:
            ms = 0
        elif len(frac_str) == 1:
            ms = int(frac_str) * 100
        elif len(frac_str) == 2:
            ms = int(frac_str) * 10
        else:
            ms = int(frac_str[:3])
        total_ms = mins * 60_000 + secs * 1000 + ms - offset_ms
        if total_ms < 0:
            total_ms = 0
        new_m = total_ms // 60_000
        rem = total_ms % 60_000
        new_s = rem // 1000
        new_cs = (rem % 1000) // 10
        return f"[{new_m:02d}:{new_s:02d}.{new_cs:02d}]"

    return _LRC_TS_RE.sub(_repl, text)


def _is_synced_lrc(text: str) -> bool:
    return bool(_LRC_TS_RE.search(text or ""))


def _compat_status(suffix: str) -> str:
    s = (suffix or "").lower().lstrip(".")
    if s in ECHO_NATIVE_SUFFIXES:
        return "native"
    if s in ECHO_TRANSCODABLE_SUFFIXES:
        return "transcode"
    return "unsupported"


class TrimMusicRepo:
    def __init__(self, db_path: str, meta_dir: str):
        self.db_path = db_path
        self.meta_dir = meta_dir

    def _connect(self) -> sqlite3.Connection:
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"未找到飞牛音乐数据库: {self.db_path}")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def is_available(self) -> bool:
        return os.path.isfile(self.db_path)

    def get_overview(self, user_id: int = 1) -> Dict[str, Any]:
        if not self.is_available():
            return {"available": False, "db_path": self.db_path}
        with self._connect() as conn:
            users = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, name, role, status FROM user WHERE status != 'deleted' ORDER BY id"
                )
            ]
            libraries = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT sl.id, sl.path, sl.metadata_preference,
                           (SELECT count(*) FROM track t
                            WHERE t.shared_library_id = sl.id
                              AND t.is_audio_file_deleted = 0
                              AND t.is_admin_deleted = 0) AS track_count
                    FROM shared_library sl
                    ORDER BY sl.id
                    """
                )
            ]
            track_count = conn.execute(
                "SELECT count(*) FROM track WHERE is_audio_file_deleted=0 AND is_admin_deleted=0"
            ).fetchone()[0]
            album_count = conn.execute("SELECT count(*) FROM album").fetchone()[0]
            artist_count = conn.execute("SELECT count(*) FROM artist").fetchone()[0]
            lyric_count = conn.execute("SELECT count(DISTINCT track_id) FROM lyric").fetchone()[0]
            fav_count = conn.execute(
                "SELECT count(*) FROM favorite_track WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            playlist_count = conn.execute(
                "SELECT count(*) FROM playlist WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            formats = [
                {"suffix": (r[0] or "").lower(), "count": r[1], "compat": _compat_status(r[0])}
                for r in conn.execute(
                    """
                    SELECT lower(af.suffix), count(*)
                    FROM track t
                    JOIN audio_file af ON af.id = t.audio_file_id
                    WHERE t.is_audio_file_deleted=0 AND t.is_admin_deleted=0
                    GROUP BY lower(af.suffix)
                    ORDER BY 2 DESC
                    LIMIT 12
                    """
                )
            ]
            return {
                "available": True,
                "db_path": self.db_path,
                "users": users,
                "libraries": libraries,
                "stats": {
                    "tracks": track_count,
                    "albums": album_count,
                    "artists": artist_count,
                    "lyrics": lyric_count,
                    "favorites": fav_count,
                    "playlists": playlist_count,
                },
                "formats": formats,
            }

    def resolve_cover_file(
        self,
        track_cover_guid: Optional[str],
        album_cover_guid: Optional[str],
        album_metadata_mode: Optional[int] = None,
        audio_path: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """Resolve best cover file path & source label.

        Priority:
        1. Cloud-scraped album cover (album.metadata_mode == 2) when present
        2. Track-level cover in trim.music meta/cover/track
        3. Album-level cover in trim.music meta/cover/album
        4. Sidecar folder cover next to audio file
        """
        candidates: List[Tuple[str, str]] = []
        if album_cover_guid and album_metadata_mode == 2:
            candidates.append(
                (os.path.join(self.meta_dir, "cover", "album", album_cover_guid[:2], album_cover_guid), "album_cloud")
            )
        if track_cover_guid:
            candidates.append(
                (os.path.join(self.meta_dir, "cover", "track", track_cover_guid[:2], track_cover_guid), "track_meta")
            )
        if album_cover_guid:
            candidates.append(
                (os.path.join(self.meta_dir, "cover", "album", album_cover_guid[:2], album_cover_guid), "album_meta")
            )

        for path, source in candidates:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path, source

        if audio_path:
            folder = os.path.dirname(audio_path)
            if os.path.isdir(folder):
                try:
                    entries = {name.lower(): name for name in os.listdir(folder)}
                except OSError:
                    entries = {}
                for target in FOLDER_COVER_NAMES:
                    if target in entries:
                        fp = os.path.join(folder, entries[target])
                        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                            return fp, "folder_sidecar"
        return None, "none"

    def resolve_entity_cover_file(self, entity_type: str, guid: str) -> Optional[str]:
        if not guid or len(guid) < 2 or entity_type not in ("track", "album", "artist"):
            return None
        fp = os.path.join(self.meta_dir, "cover", entity_type, guid[:2], guid)
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            return fp
        return None

    def _read_lyric_blob(self, stored_guid: str) -> Optional[bytes]:
        if not stored_guid:
            return None
        shard_dir = os.path.join(self.meta_dir, "lyric-sqlite")
        first = stored_guid[0].lower()
        primary = os.path.join(shard_dir, f"lyric-0{first}.db")
        candidates = [primary] if os.path.isfile(primary) else []
        for ldb in candidates:
            try:
                lc = sqlite3.connect(f"file:{ldb}?mode=ro", uri=True, timeout=3.0)
                row = lc.execute(
                    "SELECT content FROM lyric_content WHERE guid = ?", (stored_guid,)
                ).fetchone()
                lc.close()
                if row and row[0]:
                    return bytes(row[0])
            except Exception:
                pass
        # Fallback: scan other shards if hash prefix ever differs
        for ldb in sorted(glob.glob(os.path.join(shard_dir, "lyric-*.db"))):
            if ldb == primary:
                continue
            try:
                lc = sqlite3.connect(f"file:{ldb}?mode=ro", uri=True, timeout=3.0)
                row = lc.execute(
                    "SELECT content FROM lyric_content WHERE guid = ?", (stored_guid,)
                ).fetchone()
                lc.close()
                if row and row[0]:
                    return bytes(row[0])
            except Exception:
                pass
        return None

    def resolve_lyric(self, track_id: int, user_id: int = 1) -> Optional[Dict[str, Any]]:
        """Resolve the best available lyric for a track from trim.music."""
        with self._connect() as conn:
            pref_row = conn.execute(
                "SELECT lyric_id FROM user_track_lyric_preference WHERE user_id = ? AND track_id = ?",
                (user_id, track_id),
            ).fetchone()
            preferred_id = pref_row["lyric_id"] if pref_row else None

            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, guid, source, stored_guid, path FROM lyric WHERE track_id = ?",
                    (track_id,),
                )
            ]
            if not rows:
                return None

            offsets = {
                r["lyric_id"]: r["offset_ms"]
                for r in conn.execute(
                    "SELECT lyric_id, offset_ms FROM user_track_lyric_offset WHERE user_id = ? AND track_id = ?",
                    (user_id, track_id),
                )
            }

        def _rank(item: Dict[str, Any]) -> Tuple[int, int, int]:
            is_pref = 1 if item["id"] == preferred_id else 0
            prio = LYRIC_SOURCE_PRIORITY.get(item["source"], 0)
            return (is_pref, prio, item["id"])

        rows.sort(key=_rank, reverse=True)

        # Prefer synced LRC among top candidates if first candidate is unsynced embedded
        loaded_candidates: List[Dict[str, Any]] = []
        for item in rows:
            raw_bytes: Optional[bytes] = None
            if item.get("stored_guid"):
                raw_bytes = self._read_lyric_blob(item["stored_guid"])
            if not raw_bytes and item.get("path") and os.path.isfile(item["path"]):
                try:
                    raw_bytes = Path(item["path"]).read_bytes()
                except OSError:
                    raw_bytes = None
            if not raw_bytes:
                continue
            text = _decode_text_bytes(raw_bytes).strip()
            if not text:
                continue
            offset_ms = offsets.get(item["id"], 0)
            if offset_ms:
                text = _shift_lrc_timestamps(text, offset_ms)
            entry = {
                "lyric_id": item["id"],
                "source": item["source"],
                "source_label": LYRIC_SOURCE_LABELS.get(item["source"], "未知"),
                "is_preferred": item["id"] == preferred_id,
                "is_synced": _is_synced_lrc(text),
                "offset_ms": offset_ms,
                "content": text,
            }
            if entry["is_preferred"]:
                return entry
            loaded_candidates.append(entry)

        if not loaded_candidates:
            return None
        # Prefer synced lyrics over unsynced lyrics
        loaded_candidates.sort(
            key=lambda x: (1 if x["is_synced"] else 0, LYRIC_SOURCE_PRIORITY.get(x["source"], 0)),
            reverse=True,
        )
        return loaded_candidates[0]

    def _enrich_tracks(self, conn: sqlite3.Connection, rows: List[sqlite3.Row], user_id: int = 1) -> List[Dict[str, Any]]:
        if not rows:
            return []
        track_ids = [r["id"] for r in rows]
        album_ids = list({r["album_id"] for r in rows if r["album_id"] is not None})
        placeholders = ",".join("?" for _ in track_ids)

        # Fetch artists for paged tracks in one fast query
        artists_map: Dict[int, List[str]] = {}
        for ar in conn.execute(
            f"""
            SELECT ta.track_id, a.name
            FROM track_artist ta
            JOIN artist a ON a.id = ta.artist_id
            WHERE ta.track_id IN ({placeholders})
            ORDER BY ta.track_id, ta.artist_order, ta.id
            """,
            track_ids,
        ):
            artists_map.setdefault(ar["track_id"], []).append(ar["name"])

        # Fetch album artists
        album_artists_map: Dict[int, List[str]] = {}
        if album_ids:
            ap = ",".join("?" for _ in album_ids)
            for ar in conn.execute(
                f"""
                SELECT aa.album_id, a.name
                FROM album_artist aa
                JOIN artist a ON a.id = aa.artist_id
                WHERE aa.album_id IN ({ap})
                ORDER BY aa.album_id, aa.artist_order, aa.id
                """,
                album_ids,
            ):
                album_artists_map.setdefault(ar["album_id"], []).append(ar["name"])

        # Fetch genres
        genres_map: Dict[int, List[str]] = {}
        for gr in conn.execute(
            f"""
            SELECT tg.track_id, g.name
            FROM track_genre tg
            JOIN genre g ON g.id = tg.genre_id
            WHERE tg.track_id IN ({placeholders})
            """,
            track_ids,
        ):
            genres_map.setdefault(gr["track_id"], []).append(gr["name"])

        # Fetch best lyric source per track
        lyric_map: Dict[int, int] = {}
        for lr in conn.execute(
            f"SELECT track_id, max(source) AS best_source FROM lyric WHERE track_id IN ({placeholders}) GROUP BY track_id",
            track_ids,
        ):
            lyric_map[lr["track_id"]] = lr["best_source"]

        # Fetch favorites
        fav_set = {
            fr["track_id"]
            for fr in conn.execute(
                f"SELECT track_id FROM favorite_track WHERE user_id = ? AND track_id IN ({placeholders})",
                [user_id] + track_ids,
            )
        }

        result: List[Dict[str, Any]] = []
        for r in rows:
            tid = r["id"]
            aid = r["album_id"]
            t_artists = artists_map.get(tid, [])
            a_artists = album_artists_map.get(aid, []) if aid else []
            suffix = (r["suffix"] or "").lower().lstrip(".")
            t_cover = r["t_cover"]
            a_cover = r["a_cover"]
            a_mode = r["a_mode"]
            best_cover_type = None
            best_cover_guid = None
            if a_cover and a_mode == 2:
                best_cover_type, best_cover_guid = "album", a_cover
            elif t_cover:
                best_cover_type, best_cover_guid = "track", t_cover
            elif a_cover:
                best_cover_type, best_cover_guid = "album", a_cover

            lyric_source = lyric_map.get(tid)
            result.append(
                {
                    "id": tid,
                    "guid": r["guid"],
                    "title": r["title"],
                    "album_id": aid,
                    "album": r["album_name"] or "",
                    "album_release_date": r["release_date"] or "",
                    "artists": t_artists,
                    "artist": " / ".join(t_artists) if t_artists else (" / ".join(a_artists) if a_artists else "未知歌手"),
                    "album_artist": " / ".join(a_artists) if a_artists else (" / ".join(t_artists) if t_artists else ""),
                    "genres": genres_map.get(tid, []),
                    "year": r["year"],
                    "disc_no": r["disc_no"],
                    "track_no": r["track_no"],
                    "duration_ms": r["duration_ms"] or 0,
                    "path": r["path"],
                    "file_name": r["file_name"],
                    "suffix": suffix,
                    "size": r["size"] or 0,
                    "bitrate": r["bitrate"] or 0,
                    "sample_rate": r["sample_rate"] or 0,
                    "bit_depth": r["bit_depth"] or 0,
                    "codec": r["codec"] or "",
                    "is_cue": bool(r["is_cue"]),
                    "cue_path": r["cue_path"],
                    "start_offset_ms": r["start_offset_ms"],
                    "end_offset_ms": r["end_offset_ms"],
                    "start_offset": r["start_offset"],
                    "end_offset": r["end_offset"],
                    "shared_library_id": r["shared_library_id"],
                    "library_path": r["library_path"] or "",
                    "track_cover_guid": t_cover,
                    "album_cover_guid": a_cover,
                    "album_metadata_mode": a_mode,
                    "cover_type": best_cover_type,
                    "cover_guid": best_cover_guid,
                    "has_cover": bool(best_cover_guid),
                    "has_lyric": lyric_source is not None,
                    "lyric_source": lyric_source,
                    "lyric_source_label": LYRIC_SOURCE_LABELS.get(lyric_source, "") if lyric_source else "",
                    "is_favorite": tid in fav_set,
                    "compat": _compat_status(suffix),
                    "cloud_scrape_status": r["cloud_scrape_status"],
                }
            )
        return result

    def list_tracks(
        self,
        q: str = "",
        library_id: Optional[int] = None,
        album_id: Optional[int] = None,
        artist_id: Optional[int] = None,
        playlist_id: Optional[int] = None,
        favorites_only: bool = False,
        has_lyric: Optional[bool] = None,
        has_cover: Optional[bool] = None,
        suffix: str = "",
        user_id: int = 1,
        sort: str = "default",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(500, page_size))
        offset = (page - 1) * page_size

        joins = [
            "JOIN audio_file af ON af.id = t.audio_file_id",
            "JOIN shared_library sl ON sl.id = t.shared_library_id",
            "LEFT JOIN album a ON a.id = t.album_id",
        ]
        where = ["t.is_audio_file_deleted = 0", "t.is_admin_deleted = 0"]
        params: List[Any] = []

        if library_id is not None:
            where.append("t.shared_library_id = ?")
            params.append(library_id)
        if album_id is not None:
            where.append("t.album_id = ?")
            params.append(album_id)
        if artist_id is not None:
            joins.append("JOIN track_artist fta ON fta.track_id = t.id")
            where.append("fta.artist_id = ?")
            params.append(artist_id)
        if playlist_id is not None:
            joins.append("JOIN playlist_track fpt ON fpt.track_id = t.id")
            where.append("fpt.playlist_id = ?")
            params.append(playlist_id)
        if favorites_only:
            joins.append("JOIN favorite_track fft ON fft.track_id = t.id")
            where.append("fft.user_id = ?")
            params.append(user_id)
        if suffix:
            where.append("lower(af.suffix) = ?")
            params.append(suffix.lower().lstrip("."))
        if has_cover is True:
            where.append("(t.cover_guid IS NOT NULL OR a.cover_guid IS NOT NULL)")
        elif has_cover is False:
            where.append("(t.cover_guid IS NULL AND a.cover_guid IS NULL)")
        if has_lyric is True:
            where.append("EXISTS (SELECT 1 FROM lyric l WHERE l.track_id = t.id)")
        elif has_lyric is False:
            where.append("NOT EXISTS (SELECT 1 FROM lyric l WHERE l.track_id = t.id)")

        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            where.append(
                """(
                    t.title LIKE ? COLLATE NOCASE
                    OR a.name LIKE ? COLLATE NOCASE
                    OR af.name LIKE ? COLLATE NOCASE
                    OR EXISTS (
                        SELECT 1 FROM track_artist sta
                        JOIN artist sa ON sa.id = sta.artist_id
                        WHERE sta.track_id = t.id AND sa.name LIKE ? COLLATE NOCASE
                    )
                )"""
            )
            params.extend([like, like, like, like])

        order_by = "t.id DESC"
        if album_id is not None:
            order_by = "COALESCE(t.disc_no, 1), COALESCE(t.track_no, 9999), t.id"
        elif playlist_id is not None:
            order_by = "fpt.added_at DESC, t.id"
        elif favorites_only:
            order_by = "fft.created_at DESC, t.id"
        elif sort == "title":
            order_by = "t.title COLLATE NOCASE ASC, t.id"
        elif sort == "size_desc":
            order_by = "af.size DESC, t.id"
        elif sort == "recent":
            order_by = "t.created_at DESC, t.id DESC"

        join_sql = "\n".join(joins)
        where_sql = " AND ".join(where)

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT count(*) FROM track t {join_sql} WHERE {where_sql}",
                params,
            ).fetchone()[0]

            rows = conn.execute(
                f"""
                SELECT t.id, t.guid, t.title, t.album_id, t.cover_guid AS t_cover,
                       t.year, t.disc_no, t.track_no, t.duration_ms,
                       t.is_cue, t.start_offset, t.end_offset, t.start_offset_ms, t.end_offset_ms,
                       t.shared_library_id, t.cloud_scrape_status,
                       a.name AS album_name, a.cover_guid AS a_cover, a.metadata_mode AS a_mode, a.release_date,
                       af.path, af.name AS file_name, af.suffix, af.size, af.bitrate,
                       af.sample_rate, af.bit_depth, af.codec, af.cue_path,
                       sl.path AS library_path
                FROM track t
                {join_sql}
                WHERE {where_sql}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                params + [page_size, offset],
            ).fetchall()

            items = self._enrich_tracks(conn, rows, user_id=user_id)
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def get_tracks_by_ids(self, track_ids: List[int], user_id: int = 1) -> List[Dict[str, Any]]:
        if not track_ids:
            return []
        unique_ids = list(dict.fromkeys(int(x) for x in track_ids))
        results: Dict[int, Dict[str, Any]] = {}
        with self._connect() as conn:
            # Chunk to stay well within SQLite parameter limits
            for i in range(0, len(unique_ids), 400):
                chunk = unique_ids[i : i + 400]
                ph = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT t.id, t.guid, t.title, t.album_id, t.cover_guid AS t_cover,
                           t.year, t.disc_no, t.track_no, t.duration_ms,
                           t.is_cue, t.start_offset, t.end_offset, t.start_offset_ms, t.end_offset_ms,
                           t.shared_library_id, t.cloud_scrape_status,
                           a.name AS album_name, a.cover_guid AS a_cover, a.metadata_mode AS a_mode, a.release_date,
                           af.path, af.name AS file_name, af.suffix, af.size, af.bitrate,
                           af.sample_rate, af.bit_depth, af.codec, af.cue_path,
                           sl.path AS library_path
                    FROM track t
                    JOIN audio_file af ON af.id = t.audio_file_id
                    JOIN shared_library sl ON sl.id = t.shared_library_id
                    LEFT JOIN album a ON a.id = t.album_id
                    WHERE t.id IN ({ph}) AND t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
                    """,
                    chunk,
                ).fetchall()
                for item in self._enrich_tracks(conn, rows, user_id=user_id):
                    results[item["id"]] = item
        return [results[tid] for tid in unique_ids if tid in results]

    def get_track_ids_for_albums(self, album_ids: List[int]) -> List[int]:
        if not album_ids:
            return []
        ph = ",".join("?" for _ in album_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id FROM track
                WHERE album_id IN ({ph}) AND is_audio_file_deleted = 0 AND is_admin_deleted = 0
                ORDER BY album_id, COALESCE(disc_no, 1), COALESCE(track_no, 9999), id
                """,
                album_ids,
            ).fetchall()
            return [r["id"] for r in rows]

    def get_track_ids_for_artists(self, artist_ids: List[int]) -> List[int]:
        if not artist_ids:
            return []
        ph = ",".join("?" for _ in artist_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT t.id
                FROM track t
                JOIN track_artist ta ON ta.track_id = t.id
                WHERE ta.artist_id IN ({ph}) AND t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
                ORDER BY t.album_id, COALESCE(t.disc_no, 1), COALESCE(t.track_no, 9999), t.id
                """,
                artist_ids,
            ).fetchall()
            return [r["id"] for r in rows]

    def get_track_ids_for_playlist(self, playlist_id: int) -> Tuple[Optional[str], List[int]]:
        with self._connect() as conn:
            pl = conn.execute("SELECT name FROM playlist WHERE id = ?", (playlist_id,)).fetchone()
            if not pl:
                return None, []
            rows = conn.execute(
                """
                SELECT pt.track_id
                FROM playlist_track pt
                JOIN track t ON t.id = pt.track_id
                WHERE pt.playlist_id = ? AND t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
                ORDER BY pt.added_at DESC, pt.id
                """,
                (playlist_id,),
            ).fetchall()
            return pl["name"], [r["track_id"] for r in rows]

    def get_track_ids_for_favorites(self, user_id: int = 1) -> List[int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ft.track_id
                FROM favorite_track ft
                JOIN track t ON t.id = ft.track_id
                WHERE ft.user_id = ? AND t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
                ORDER BY ft.created_at DESC, ft.id
                """,
                (user_id,),
            ).fetchall()
            return [r["track_id"] for r in rows]

    def get_track_ids_under_folder(self, folder_path: str) -> List[int]:
        norm = os.path.abspath(folder_path).rstrip("/")
        prefix = norm + "/"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.id
                FROM track t
                JOIN audio_file af ON af.id = t.audio_file_id
                WHERE t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
                  AND (af.path LIKE ? OR af.path = ?)
                ORDER BY af.path, COALESCE(t.disc_no, 1), COALESCE(t.track_no, 9999), t.id
                """,
                (prefix + "%", norm),
            ).fetchall()
            return [r["id"] for r in rows]

    def list_albums(
        self,
        q: str = "",
        library_id: Optional[int] = None,
        artist_id: Optional[int] = None,
        page: int = 1,
        page_size: int = 40,
    ) -> Dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        offset = (page - 1) * page_size

        where = ["1=1"]
        params: List[Any] = []
        joins: List[str] = []

        if artist_id is not None:
            joins.append("JOIN album_artist faa ON faa.album_id = a.id")
            where.append("faa.artist_id = ?")
            params.append(artist_id)
        if library_id is not None:
            where.append(
                "EXISTS (SELECT 1 FROM track t WHERE t.album_id = a.id AND t.shared_library_id = ? AND t.is_audio_file_deleted=0 AND t.is_admin_deleted=0)"
            )
            params.append(library_id)
        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            where.append(
                """(
                    a.name LIKE ? COLLATE NOCASE
                    OR EXISTS (
                        SELECT 1 FROM album_artist saa
                        JOIN artist sa ON sa.id = saa.artist_id
                        WHERE saa.album_id = a.id AND sa.name LIKE ? COLLATE NOCASE
                    )
                )"""
            )
            params.extend([like, like])

        join_sql = "\n".join(joins)
        where_sql = " AND ".join(where)

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT count(DISTINCT a.id) FROM album a {join_sql} WHERE {where_sql}",
                params,
            ).fetchone()[0]

            rows = conn.execute(
                f"""
                SELECT DISTINCT a.id, a.guid, a.name, a.cover_guid, a.release_date, a.metadata_mode
                FROM album a
                {join_sql}
                WHERE {where_sql}
                ORDER BY (a.cover_guid IS NOT NULL) DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,
                params + [page_size, offset],
            ).fetchall()

            if not rows:
                return {"items": [], "total": total, "page": page, "page_size": page_size}

            aids = [r["id"] for r in rows]
            ph = ",".join("?" for _ in aids)

            artists_map: Dict[int, List[str]] = {}
            for ar in conn.execute(
                f"""
                SELECT aa.album_id, ar.name
                FROM album_artist aa
                JOIN artist ar ON ar.id = aa.artist_id
                WHERE aa.album_id IN ({ph})
                ORDER BY aa.album_id, aa.artist_order, aa.id
                """,
                aids,
            ):
                artists_map.setdefault(ar["album_id"], []).append(ar["name"])

            stats_map: Dict[int, Dict[str, Any]] = {}
            for sr in conn.execute(
                f"""
                SELECT t.album_id, count(*) AS cnt, sum(af.size) AS total_size,
                       max(t.cover_guid) AS fallback_cover
                FROM track t
                JOIN audio_file af ON af.id = t.audio_file_id
                WHERE t.album_id IN ({ph}) AND t.is_audio_file_deleted=0 AND t.is_admin_deleted=0
                GROUP BY t.album_id
                """,
                aids,
            ):
                stats_map[sr["album_id"]] = dict(sr)

            items = []
            for r in rows:
                aid = r["id"]
                st = stats_map.get(aid, {})
                cov_type = "album" if r["cover_guid"] else ("track" if st.get("fallback_cover") else None)
                cov_guid = r["cover_guid"] or st.get("fallback_cover")
                items.append(
                    {
                        "id": aid,
                        "guid": r["guid"],
                        "name": r["name"],
                        "artist": " / ".join(artists_map.get(aid, [])) or "群星 / 未知歌手",
                        "release_date": r["release_date"] or "",
                        "track_count": st.get("cnt", 0),
                        "total_size": st.get("total_size", 0) or 0,
                        "cover_type": cov_type,
                        "cover_guid": cov_guid,
                        "metadata_mode": r["metadata_mode"],
                    }
                )
            return {"items": items, "total": total, "page": page, "page_size": page_size}

    def list_artists(self, q: str = "", page: int = 1, page_size: int = 48) -> Dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        offset = (page - 1) * page_size
        where = "1=1"
        params: List[Any] = []
        q = (q or "").strip()
        if q:
            where = "a.name LIKE ? COLLATE NOCASE"
            params.append(f"%{q}%")

        with self._connect() as conn:
            total = conn.execute(f"SELECT count(*) FROM artist a WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT a.id, a.guid, a.name, a.cover_guid,
                       (SELECT count(*) FROM track_artist ta WHERE ta.artist_id = a.id) AS track_count,
                       (SELECT count(*) FROM album_artist aa WHERE aa.artist_id = a.id) AS album_count
                FROM artist a
                WHERE {where}
                ORDER BY track_count DESC, a.id
                LIMIT ? OFFSET ?
                """,
                params + [page_size, offset],
            ).fetchall()
            items = [
                {
                    "id": r["id"],
                    "guid": r["guid"],
                    "name": r["name"],
                    "cover_type": "artist" if r["cover_guid"] else None,
                    "cover_guid": r["cover_guid"],
                    "track_count": r["track_count"],
                    "album_count": r["album_count"],
                }
                for r in rows
            ]
            return {"items": items, "total": total, "page": page, "page_size": page_size}

    def list_playlists(self, user_id: int = 1) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.guid, p.name, p.cover_guid, p.user_id, p.created_at, p.updated_at,
                       count(pt.id) AS track_count
                FROM playlist p
                LEFT JOIN playlist_track pt ON pt.playlist_id = p.id
                WHERE p.user_id = ?
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.id DESC
                """,
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def browse_folder(self, library_id: int, rel_path: str = "", user_id: int = 1) -> Dict[str, Any]:
        with self._connect() as conn:
            lib = conn.execute(
                "SELECT id, path FROM shared_library WHERE id = ?", (library_id,)
            ).fetchone()
            if not lib:
                raise FileNotFoundError(f"媒体库不存在: {library_id}")
            root = os.path.abspath(lib["path"])

        clean_rel = (rel_path or "").strip().strip("/")
        target = os.path.abspath(os.path.join(root, clean_rel)) if clean_rel else root
        if target != root and not target.startswith(root + os.sep):
            raise PermissionError("路径越界，已拒绝访问")
        if not os.path.isdir(target):
            raise FileNotFoundError(f"目录不存在: {target}")

        dirs: List[Dict[str, Any]] = []
        audio_paths: List[str] = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        sub_rel = os.path.relpath(entry.path, root)
                        dirs.append({"name": entry.name, "rel_path": sub_rel, "path": entry.path})
                    elif entry.is_file(follow_symlinks=False):
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in AUDIO_EXTENSIONS:
                            audio_paths.append(entry.path)
        except OSError as exc:
            raise RuntimeError(f"读取目录失败: {exc}") from exc

        dirs.sort(key=lambda x: x["name"].lower())
        tracks: List[Dict[str, Any]] = []
        if audio_paths:
            with self._connect() as conn:
                for i in range(0, len(audio_paths), 400):
                    chunk = audio_paths[i : i + 400]
                    ph = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"""
                        SELECT t.id, t.guid, t.title, t.album_id, t.cover_guid AS t_cover,
                               t.year, t.disc_no, t.track_no, t.duration_ms,
                               t.is_cue, t.start_offset, t.end_offset, t.start_offset_ms, t.end_offset_ms,
                               t.shared_library_id, t.cloud_scrape_status,
                               a.name AS album_name, a.cover_guid AS a_cover, a.metadata_mode AS a_mode, a.release_date,
                               af.path, af.name AS file_name, af.suffix, af.size, af.bitrate,
                               af.sample_rate, af.bit_depth, af.codec, af.cue_path,
                               sl.path AS library_path
                        FROM track t
                        JOIN audio_file af ON af.id = t.audio_file_id
                        JOIN shared_library sl ON sl.id = t.shared_library_id
                        LEFT JOIN album a ON a.id = t.album_id
                        WHERE af.path IN ({ph}) AND t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
                        ORDER BY af.path, COALESCE(t.disc_no, 1), COALESCE(t.track_no, 9999), t.id
                        """,
                        chunk,
                    ).fetchall()
                    tracks.extend(self._enrich_tracks(conn, rows, user_id=user_id))

        return {
            "library_id": library_id,
            "library_path": root,
            "rel_path": "" if target == root else os.path.relpath(target, root),
            "abs_path": target,
            "dirs": dirs,
            "tracks": tracks,
        }


def _decode_text_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
