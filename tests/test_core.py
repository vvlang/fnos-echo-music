# -*- coding: utf-8 -*-
"""Comprehensive unit and integration tests for Echo Music Sync."""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parent.parent / "fpk" / "app" / "server"
sys.path.insert(0, str(SERVER_DIR))

from app.config import ConfigStore  # noqa: E402
from app.media_proc import (  # noqa: E402
    prepare_echo_cover,
    sanitize_filename,
    write_folder_cover,
    write_lrc_sidecar,
)
from app.organizer import (  # noqa: E402
    OrganizeJobManager,
    clean_library_junk,
    list_recycle_batches,
    move_files_to_recycle,
    preview_organize_plan,
    purge_recycle_batch,
    restore_recycle_batch,
    scan_duplicates,
    scan_library_health,
)
from app.transfer import TransferManager, build_destination_path  # noqa: E402
from app.trim_music import TrimMusicRepo, _shift_lrc_timestamps  # noqa: E402


def _create_mock_trim_music(tmp_path: Path):
    """Build a realistic trim.music SQLite DB, cover store (WebP + PNG), and lyric shard DB."""
    db_path = tmp_path / "music.db"
    meta_dir = tmp_path / "meta"
    music_lib = tmp_path / "Muisc"
    books_lib = tmp_path / "books"
    (music_lib / "孙燕姿" / "遇见").mkdir(parents=True)
    (books_lib / "三体").mkdir(parents=True)

    # Create sample audio files
    mp3_path = music_lib / "孙燕姿" / "遇见" / "01 遇见.mp3"
    mp3_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 100)
    book_path = books_lib / "三体" / "01 科学边界.m4a"
    book_path.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 128)

    # Create WebP scraped album cover (tests Echo WebP -> baseline JPEG conversion!)
    album_cov_guid = "ab1234567890abcdef1234567890abcd"
    album_cov_path = meta_dir / "cover" / "album" / "ab" / album_cov_guid
    album_cov_path.parent.mkdir(parents=True)
    img = Image.new("RGBA", (1200, 1200), (40, 120, 240, 255))
    img.save(album_cov_path, format="WEBP")

    # Create lyric shard lyric-0a.db
    lyric_guid = "af0287ff224441c7bc90ed5d4ebb574f"
    lyric_dir = meta_dir / "lyric-sqlite"
    lyric_dir.mkdir(parents=True)
    lc = sqlite3.connect(lyric_dir / "lyric-0a.db")
    lc.execute("CREATE TABLE lyric_content (guid TEXT PRIMARY KEY NOT NULL, content BLOB NOT NULL)")
    lc.execute(
        "INSERT INTO lyric_content (guid, content) VALUES (?, ?)",
        (lyric_guid, "[00:10.50]听见冬天的离开\n[00:15.00]我在某年某月醒过来\n".encode("utf-8")),
    )
    lc.commit()
    lc.close()

    # Create music.db with core trim.music schema
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE user (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, role TEXT, status TEXT);
        CREATE TABLE shared_library (id INTEGER PRIMARY KEY, guid TEXT, path TEXT, metadata_preference TEXT);
        CREATE TABLE audio_file (
            id INTEGER PRIMARY KEY, shared_library_id INTEGER, path TEXT, name TEXT, suffix TEXT,
            size INTEGER, duration_ms INTEGER, bitrate INTEGER, sample_rate INTEGER, bit_depth INTEGER,
            codec TEXT, cue_path TEXT, is_physical_file_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE artist (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, cover_guid TEXT, metadata_mode INTEGER DEFAULT 1);
        CREATE TABLE album (
            id INTEGER PRIMARY KEY, guid TEXT, name TEXT, cover_guid TEXT,
            release_date TEXT, metadata_mode INTEGER DEFAULT 2
        );
        CREATE TABLE track (
            id INTEGER PRIMARY KEY, guid TEXT, audio_file_id INTEGER, shared_library_id INTEGER,
            title TEXT, album_id INTEGER, cover_guid TEXT, year INTEGER, disc_no INTEGER, track_no INTEGER,
            duration_ms INTEGER, is_cue INTEGER DEFAULT 0, start_offset TEXT, end_offset TEXT,
            start_offset_ms INTEGER, end_offset_ms INTEGER, metadata_mode INTEGER DEFAULT 2,
            cloud_scrape_status INTEGER DEFAULT 2, is_audio_file_deleted INTEGER DEFAULT 0,
            is_admin_deleted INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE album_artist (id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER, artist_order INTEGER DEFAULT 0);
        CREATE TABLE track_artist (id INTEGER PRIMARY KEY, track_id INTEGER, artist_id INTEGER, artist_order INTEGER DEFAULT 0);
        CREATE TABLE genre (id INTEGER PRIMARY KEY, guid TEXT, name TEXT);
        CREATE TABLE track_genre (id INTEGER PRIMARY KEY, track_id INTEGER, genre_id INTEGER);
        CREATE TABLE lyric (id INTEGER PRIMARY KEY, guid TEXT, track_id INTEGER, source INTEGER, stored_guid TEXT, path TEXT);
        CREATE TABLE user_track_lyric_preference (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER, lyric_id INTEGER);
        CREATE TABLE user_track_lyric_offset (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER, lyric_id INTEGER, offset_ms INTEGER);
        CREATE TABLE favorite_track (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE playlist (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, cover_guid TEXT, user_id INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE playlist_track (id INTEGER PRIMARY KEY, user_id INTEGER, playlist_id INTEGER, track_id INTEGER, added_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        """
    )
    conn.execute("INSERT INTO user VALUES (1, 'u1', 'vvlang', 'admin', 'active')")
    conn.execute("INSERT INTO shared_library VALUES (1, 'l1', ?, 'cloud_preferred')", (str(music_lib),))
    conn.execute("INSERT INTO shared_library VALUES (2, 'l2', ?, 'cloud_preferred')", (str(books_lib),))
    conn.execute("INSERT INTO artist VALUES (1, 'ar1', '孙燕姿', NULL, 2)")
    conn.execute("INSERT INTO album VALUES (1, 'al1', 'The Moment', ?, '2003-08-22', 2)", (album_cov_guid,))
    conn.execute("INSERT INTO album_artist VALUES (1, 1, 1, 0)")
    conn.execute(
        "INSERT INTO audio_file VALUES (1, 1, ?, '01 遇见.mp3', 'mp3', ?, 210000, 320000, 44100, 16, 'mp3', NULL, 0)",
        (str(mp3_path), mp3_path.stat().st_size),
    )
    conn.execute(
        "INSERT INTO audio_file VALUES (2, 2, ?, '01 科学边界.m4a', 'm4a', ?, 900000, 128000, 44100, 16, 'aac', NULL, 0)",
        (str(book_path), book_path.stat().st_size),
    )
    conn.execute(
        "INSERT INTO track VALUES (1, 't1', 1, 1, '遇见', 1, NULL, 2003, 1, 1, 210000, 0, NULL, NULL, NULL, NULL, 2, 2, 0, 0, '2026-11-20')"
    )
    conn.execute(
        "INSERT INTO track VALUES (2, 't2', 2, 2, '科学边界', NULL, NULL, 2024, 1, 1, 900000, 0, NULL, NULL, NULL, NULL, 1, 0, 0, 0, '2026-11-20')"
    )
    conn.execute("INSERT INTO track_artist VALUES (1, 1, 1, 0)")
    conn.execute("INSERT INTO genre VALUES (1, 'g1', '华语流行')")
    conn.execute("INSERT INTO track_genre VALUES (1, 1, 1)")
    conn.execute("INSERT INTO lyric VALUES (1, 'ly1', 1, 3, ?, NULL)", (lyric_guid,))
    conn.execute("INSERT INTO user_track_lyric_offset VALUES (1, 1, 1, 1, 500)")
    conn.execute("INSERT INTO favorite_track VALUES (1, 1, 1, '2026-11-21')")
    conn.execute("INSERT INTO playlist VALUES (1, 'p1', 'Echo 精选', NULL, 1, '2026-11-21', '2026-11-21')")
    conn.execute("INSERT INTO playlist_track VALUES (1, 1, 1, 1, '2026-11-21')")
    conn.commit()
    conn.close()
    return db_path, meta_dir, album_cov_path


def test_sanitize_filename():
    assert sanitize_filename('01: 遇见 / "Special"*?') == "01_ 遇见 _ _Special___"
    assert sanitize_filename("   ...  ", fallback="Track") == "Track"
    long_cn = "孙燕姿" * 40
    assert len(sanitize_filename(long_cn).encode("utf-8")) <= 180


def test_shift_lrc_timestamps():
    shifted = _shift_lrc_timestamps("[00:10.50]Hello\n[00:00.20]Start", 500)
    assert "[00:10.00]Hello" in shifted
    assert "[00:00.00]Start" in shifted


def test_prepare_echo_cover_webp_to_baseline_jpeg(tmp_path: Path):
    webp_path = tmp_path / "cover.webp"
    Image.new("RGBA", (1200, 900), (20, 80, 180, 128)).save(webp_path, format="WEBP")

    jpeg_bytes, w, h = prepare_echo_cover(str(webp_path), max_size=500, quality=88)
    assert jpeg_bytes.startswith(b"\xff\xd8\xff")
    assert max(w, h) == 500
    with Image.open(io.BytesIO(jpeg_bytes)) as out:
        assert out.format == "JPEG"
        assert out.mode == "RGB"
        assert not out.info.get("progressive", False)


def test_repo_and_end_to_end_transfer(tmp_path: Path, monkeypatch):
    db_path, meta_dir, _ = _create_mock_trim_music(tmp_path)
    repo = TrimMusicRepo(str(db_path), str(meta_dir))

    ov = repo.get_overview(user_id=1)
    assert ov["available"] is True
    assert ov["stats"]["tracks"] == 2
    assert ov["stats"]["favorites"] == 1
    assert ov["stats"]["playlists"] == 1

    # Lyric resolution with offset_ms=500 applied
    lyr = repo.resolve_lyric(1, user_id=1)
    assert lyr is not None
    assert lyr["is_synced"] is True
    assert "[00:10.00]听见冬天的离开" in lyr["content"]

    # Audiobook auto-routing check
    tracks = repo.get_tracks_by_ids([1, 2])
    cfg_store = ConfigStore(tmp_path / "cfg.json")
    cfg = cfg_store.load()
    dest_music = build_destination_path("/vol00/SD_card", tracks[0], cfg)
    dest_book = build_destination_path("/vol00/SD_card", tracks[1], cfg)
    assert dest_music == "/vol00/SD_card/Music/孙燕姿/The Moment/01 遇见.mp3"
    assert dest_book.startswith("/vol00/SD_card/Audiobooks/")

    # Full background transfer into a mock Echo external mount
    echo_mount = tmp_path / "mock_echo"
    echo_mount.mkdir()
    monkeypatch.setenv("ECHO_EXTRA_MOUNTS", str(echo_mount))

    mgr = TransferManager(cfg_store)
    job = mgr.start_job(
        repo=repo,
        track_ids=[1, 2],
        mountpoint=str(echo_mount),
        label="测试传输",
        playlist_name="Echo 精选",
        user_id=1,
    )
    assert job["total_tracks"] == 2

    for _ in range(100):
        st = mgr.get_status()["current"]
        if st and st["status"] != "running":
            break
        time.sleep(0.05)

    st = mgr.get_status()["current"]
    assert st["status"] == "completed"
    assert st["copied"] == 2
    assert st["failed"] == 0
    assert st["covers_written"] >= 1
    assert st["lyrics_written"] == 1

    out_mp3 = echo_mount / "Music" / "孙燕姿" / "The Moment" / "01 遇见.mp3"
    out_lrc = echo_mount / "Music" / "孙燕姿" / "The Moment" / "01 遇见.lrc"
    out_cov = echo_mount / "Music" / "孙燕姿" / "The Moment" / "folder.jpg"
    out_m3u = echo_mount / "Playlists" / "Echo 精选.m3u8"

    assert out_mp3.is_file()
    assert out_lrc.is_file()
    assert "[00:10.00]听见冬天的离开" in out_lrc.read_text(encoding="utf-8")
    assert out_cov.is_file()
    assert out_cov.read_bytes().startswith(b"\xff\xd8\xff")
    assert out_m3u.is_file()
    assert "../Music/孙燕姿/The Moment/01 遇见.mp3" in out_m3u.read_text(encoding="utf-8")


def test_dedup_recycle_restore_and_organize(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ECHO_SYNC_DATA_DIR", str(tmp_path / "appdata"))
    db_path = tmp_path / "music.db"
    meta_dir = tmp_path / "meta"
    lib = tmp_path / "MusicLib"
    (lib / "杂乱目录").mkdir(parents=True)
    (lib / "副本目录").mkdir(parents=True)
    (lib / "空目录").mkdir(parents=True)
    (lib / "杂乱目录" / ".DS_Store").write_bytes(b"junk")
    (lib / "杂乱目录" / "._ghost.mp3").write_bytes(b"appledouble")
    (lib / "杂乱目录" / "孤立歌词.lrc").write_text("[00:01.00]orphan", encoding="utf-8")

    # 1) High-quality FLAC + low-quality MP3 of same song (same duration within 1s)
    f_flac = lib / "杂乱目录" / "遇见.flac"
    f_mp3 = lib / "杂乱目录" / "遇见_128k.mp3"
    f_mp3_copy = lib / "副本目录" / "遇见_128k_copy.mp3"
    f_flac.write_bytes(b"fLaC" + b"\x00" * 5000)
    f_mp3.write_bytes(b"MP3DATA" * 200)
    f_mp3_copy.write_bytes(b"MP3DATA" * 200)
    (lib / "副本目录" / "遇见_128k_copy.lrc").write_text("[00:05.00]test", encoding="utf-8")

    # 2) Spam tag files ("魅派车载音乐") with different durations -> MUST NOT be flagged as smart_song duplicates!
    f_spam1 = lib / "杂乱目录" / "spam1.mp3"
    f_spam2 = lib / "杂乱目录" / "spam2.mp3"
    f_spam1.write_bytes(b"SPAM1" * 100)
    f_spam2.write_bytes(b"SPAM2" * 100)

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE user (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, role TEXT, status TEXT);
        CREATE TABLE shared_library (id INTEGER PRIMARY KEY, guid TEXT, path TEXT, metadata_preference TEXT);
        CREATE TABLE audio_file (
            id INTEGER PRIMARY KEY, shared_library_id INTEGER, path TEXT, name TEXT, suffix TEXT,
            size INTEGER, hash TEXT, duration_ms INTEGER, bitrate INTEGER, sample_rate INTEGER, bit_depth INTEGER,
            codec TEXT, cue_path TEXT, is_physical_file_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE artist (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, cover_guid TEXT, metadata_mode INTEGER DEFAULT 1);
        CREATE TABLE album (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, cover_guid TEXT, release_date TEXT, metadata_mode INTEGER DEFAULT 2);
        CREATE TABLE track (
            id INTEGER PRIMARY KEY, guid TEXT, audio_file_id INTEGER, shared_library_id INTEGER,
            title TEXT, album_id INTEGER, cover_guid TEXT, year INTEGER, disc_no INTEGER, track_no INTEGER,
            duration_ms INTEGER, is_cue INTEGER DEFAULT 0, start_offset TEXT, end_offset TEXT,
            start_offset_ms INTEGER, end_offset_ms INTEGER, metadata_mode INTEGER DEFAULT 2,
            cloud_scrape_status INTEGER DEFAULT 2, is_audio_file_deleted INTEGER DEFAULT 0,
            is_admin_deleted INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE album_artist (id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER, artist_order INTEGER DEFAULT 0);
        CREATE TABLE track_artist (id INTEGER PRIMARY KEY, track_id INTEGER, artist_id INTEGER, artist_order INTEGER DEFAULT 0);
        CREATE TABLE genre (id INTEGER PRIMARY KEY, guid TEXT, name TEXT);
        CREATE TABLE track_genre (id INTEGER PRIMARY KEY, track_id INTEGER, genre_id INTEGER);
        CREATE TABLE lyric (id INTEGER PRIMARY KEY, guid TEXT, track_id INTEGER, source INTEGER, stored_guid TEXT, path TEXT);
        CREATE TABLE favorite_track (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE playlist (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, cover_guid TEXT, user_id INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE playlist_track (id INTEGER PRIMARY KEY, user_id INTEGER, playlist_id INTEGER, track_id INTEGER, added_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        """
    )
    conn.execute("INSERT INTO user VALUES (1, 'u1', 'vvlang', 'admin', 'active')")
    conn.execute("INSERT INTO shared_library VALUES (1, 'l1', ?, 'cloud_preferred')", (str(lib),))
    conn.execute("INSERT INTO artist VALUES (1, 'ar1', '孙燕姿', NULL, 2)")
    conn.execute("INSERT INTO artist VALUES (2, 'ar2', '魅派车载音乐', NULL, 1)")
    conn.execute("INSERT INTO album VALUES (1, 'al1', 'The Moment', NULL, '2003', 2)")
    conn.execute("INSERT INTO album_artist VALUES (1, 1, 1, 0)")

    conn.execute("INSERT INTO audio_file VALUES (1, 1, ?, '遇见.flac', 'flac', ?, 'hash_flac', 210000, 950000, 44100, 16, 'flac', NULL, 0)", (str(f_flac), f_flac.stat().st_size))
    conn.execute("INSERT INTO audio_file VALUES (2, 1, ?, '遇见_128k.mp3', 'mp3', ?, 'hash_mp3_dup', 210800, 128000, 44100, 16, 'mp3', NULL, 0)", (str(f_mp3), f_mp3.stat().st_size))
    conn.execute("INSERT INTO audio_file VALUES (3, 1, ?, '遇见_128k_copy.mp3', 'mp3', ?, 'hash_mp3_dup', 210800, 128000, 44100, 16, 'mp3', NULL, 0)", (str(f_mp3_copy), f_mp3_copy.stat().st_size))
    conn.execute("INSERT INTO audio_file VALUES (4, 1, ?, 'spam1.mp3', 'mp3', ?, 'hash_s1', 180000, 64000, 44100, 16, 'mp3', NULL, 0)", (str(f_spam1), f_spam1.stat().st_size))
    conn.execute("INSERT INTO audio_file VALUES (5, 1, ?, 'spam2.mp3', 'mp3', ?, 'hash_s2', 240000, 64000, 44100, 16, 'mp3', NULL, 0)", (str(f_spam2), f_spam2.stat().st_size))

    conn.execute("INSERT INTO track VALUES (1, 't1', 1, 1, '遇见', 1, NULL, 2003, 1, 1, 210000, 0, NULL, NULL, NULL, NULL, 2, 2, 0, 0, '2026-11-20')")
    conn.execute("INSERT INTO track VALUES (2, 't2', 2, 1, '遇见', 1, NULL, 2003, 1, 1, 210800, 0, NULL, NULL, NULL, NULL, 2, 2, 0, 0, '2026-11-20')")
    conn.execute("INSERT INTO track VALUES (3, 't3', 3, 1, '遇见', 1, NULL, 2003, 1, 1, 210800, 0, NULL, NULL, NULL, NULL, 2, 2, 0, 0, '2026-11-20')")
    conn.execute("INSERT INTO track VALUES (4, 't4', 4, 1, '魅派车载音乐', NULL, NULL, 2020, 1, 1, 180000, 0, NULL, NULL, NULL, NULL, 1, 0, 0, 0, '2026-11-20')")
    conn.execute("INSERT INTO track VALUES (5, 't5', 5, 1, '魅派车载音乐', NULL, NULL, 2020, 1, 2, 240000, 0, NULL, NULL, NULL, NULL, 1, 0, 0, 0, '2026-11-20')")
    for tid, aid in [(1, 1), (2, 1), (3, 1), (4, 2), (5, 2)]:
        conn.execute("INSERT INTO track_artist (track_id, artist_id, artist_order) VALUES (?, ?, 0)", (tid, aid))
    conn.commit()
    conn.close()

    repo = TrimMusicRepo(str(db_path), str(meta_dir))

    # 1. Exact hash dedup finds the 2 identical MP3s
    exact = scan_duplicates(repo, mode="exact_hash")
    assert exact["total_groups"] == 1
    assert exact["total_duplicate_files"] == 1

    # 2. Smart song dedup finds 遇见 (FLAC + 2 MP3s), recommends keeping FLAC, ignores 魅派车载音乐 spam!
    smart = scan_duplicates(repo, mode="smart_song", cross_format_only=True)
    assert smart["total_groups"] == 1
    grp = smart["groups"][0]
    assert grp["items"][0]["suffix"] == "flac"
    assert grp["items"][0]["recommended_keep"] is True
    assert grp["items"][1]["recommended_keep"] is False

    # 3. Move MP3 copy to recycle bin -> verify moved + LRC moved -> restore -> verify restored -> move & purge
    rec = move_files_to_recycle(repo, [str(f_mp3_copy)], reason="测试去重")
    assert rec["moved_count"] == 2  # mp3 + matching .lrc
    assert not f_mp3_copy.exists()
    assert len(list_recycle_batches()) == 1

    rest = restore_recycle_batch(rec["batch_id"])
    assert rest["restored"] == 2
    assert f_mp3_copy.exists()

    rec2 = move_files_to_recycle(repo, [str(f_mp3_copy)], reason="二次去重")
    purged = purge_recycle_batch(rec2["batch_id"])
    assert purged["deleted_files"] == 2
    assert len(list_recycle_batches()) == 0

    # 4. Health scan & junk/empty-dir clean
    health = scan_library_health(repo, library_id=1)
    assert health["junk_count"] == 2
    assert health["empty_dir_count"] >= 1
    assert health["orphan_lrc_count"] == 1
    cleaned = clean_library_junk(repo, library_id=1, remove_junk_files=True, remove_empty_dirs=True, remove_orphan_lrcs=True)
    assert cleaned["removed_junk"] == 2
    assert cleaned["removed_orphan_lrcs"] == 1
    assert cleaned["removed_empty_dirs"] >= 1

    # 5. Directory organize preview & execute
    pv = preview_organize_plan(repo, library_id=1, rel_subpath="杂乱目录", layout="artist_album", rename_files=True)
    assert pv["to_move"] >= 1
    cfg_store = ConfigStore(tmp_path / "cfg2.json")
    omgr = OrganizeJobManager(cfg_store)
    omgr.start_organize_move(repo, library_id=1, rel_subpath="杂乱目录", layout="artist_album", rename_files=True)
    for _ in range(100):
        st = omgr.get_status()
        if st["status"] != "running":
            break
        time.sleep(0.05)
    assert omgr.get_status()["status"] == "completed"
    assert (lib / "孙燕姿" / "The Moment" / "01 遇见.flac").is_file()

