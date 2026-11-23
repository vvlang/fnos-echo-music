# -*- coding: utf-8 -*-
"""Echo/AIYeon media processor: cover normalization, tag writing, LRC export, CUE split & transcode."""
from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image

from .trim_music import ECHO_NATIVE_SUFFIXES

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")


def sanitize_filename(name: str, fallback: str = "Unknown", max_bytes: int = 180) -> str:
    """Clean a filename/dirname component for FAT32/exFAT compatibility."""
    s = (name or "").strip()
    s = _INVALID_FS_CHARS.sub("_", s)
    s = _MULTI_SPACE.sub(" ", s).strip(" .")
    if not s:
        s = fallback
    # Reserve space for extension / .lrc by capping UTF-8 byte length
    encoded = s.encode("utf-8")
    if len(encoded) > max_bytes:
        while len(s.encode("utf-8")) > max_bytes and len(s) > 1:
            s = s[:-1]
        s = s.rstrip(" .")
    return s or fallback


def prepare_echo_cover(
    cover_path: str,
    max_size: int = 500,
    quality: int = 88,
) -> Tuple[bytes, int, int]:
    """Convert any image (WebP/PNG/CMYK/large JPEG) to Echo-safe baseline RGB JPEG.

    Echo AIYeon & FiiO stock firmware reject WebP, CMYK, 12-bit/lossless/arithmetic JPEGs,
    and slow down on oversized images. Baseline RGB JPEG <= 500px loads instantaneously.
    """
    with Image.open(cover_path) as img:
        img.load()
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (0, 0, 0))
            bg.paste(rgba, mask=rgba.split()[3])
            rgb = bg
        elif img.mode != "RGB":
            rgb = img.convert("RGB")
        else:
            rgb = img.copy()

        if max(rgb.size) > max_size:
            rgb.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        rgb.save(
            buf,
            format="JPEG",
            quality=quality,
            progressive=False,
            optimize=True,
            subsampling="4:2:0",
        )
        return buf.getvalue(), rgb.width, rgb.height


def write_folder_cover(target_dir: str, jpeg_bytes: bytes, overwrite: bool = False) -> bool:
    """Write folder.jpg into the target album directory on Echo."""
    dst = os.path.join(target_dir, "folder.jpg")
    if not overwrite and os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return False
    tmp = dst + ".tmp_echo"
    try:
        with open(tmp, "wb") as fh:
            fh.write(jpeg_bytes)
        os.replace(tmp, dst)
        return True
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_lrc_sidecar(audio_dst_path: str, lyric_text: str) -> str:
    """Write UTF-8 .lrc sidecar file with exact matching basename alongside target audio."""
    stem, _ = os.path.splitext(audio_dst_path)
    lrc_path = stem + ".lrc"
    tmp = lrc_path + ".tmp_echo"
    normalized = lyric_text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(normalized)
        os.replace(tmp, lrc_path)
        return lrc_path
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _year_str(track: Dict[str, Any]) -> str:
    if track.get("year"):
        return str(track["year"])
    rd = (track.get("album_release_date") or "").strip()
    if len(rd) >= 4 and rd[:4].isdigit():
        return rd[:4]
    return ""


def apply_metadata_to_copy(
    dst_path: str,
    track: Dict[str, Any],
    cover_jpeg: Optional[bytes] = None,
    cover_dims: Tuple[int, int] = (500, 500),
    lyric_text: Optional[str] = None,
    write_tags: bool = True,
    embed_cover: bool = True,
    embed_lyrics: bool = True,
) -> Dict[str, bool]:
    """Apply scraped tags, baseline JPEG cover, and lyrics to the COPIED file on Echo.

    Never called on NAS source files. Failures are caught gracefully per-stage so audio
    files are never corrupted or lost.
    """
    status = {"tags": False, "cover": False, "lyrics": False}
    if not (write_tags or (embed_cover and cover_jpeg) or (embed_lyrics and lyric_text)):
        return status

    ext = os.path.splitext(dst_path)[1].lower().lstrip(".")
    title = track.get("title") or ""
    artist = track.get("artist") or ""
    album = track.get("album") or ""
    album_artist = track.get("album_artist") or artist
    year = _year_str(track)
    track_no = track.get("track_no")
    disc_no = track.get("disc_no")
    genre = " / ".join(track.get("genres") or [])

    try:
        if ext == "mp3":
            _apply_id3(
                dst_path,
                "mp3",
                title, artist, album, album_artist, year, track_no, disc_no, genre,
                cover_jpeg if embed_cover else None,
                lyric_text if embed_lyrics else None,
                write_tags,
                status,
            )
        elif ext == "flac":
            _apply_flac(
                dst_path,
                title, artist, album, album_artist, year, track_no, disc_no, genre,
                cover_jpeg if embed_cover else None,
                cover_dims,
                lyric_text if embed_lyrics else None,
                write_tags,
                status,
            )
        elif ext in ("m4a", "mp4", "aac"):
            _apply_mp4(
                dst_path,
                title, artist, album, album_artist, year, track_no, disc_no, genre,
                cover_jpeg if embed_cover else None,
                lyric_text if embed_lyrics else None,
                write_tags,
                status,
            )
        elif ext == "ogg":
            _apply_ogg(
                dst_path,
                title, artist, album, album_artist, year, track_no, disc_no, genre,
                cover_jpeg if embed_cover else None,
                cover_dims,
                lyric_text if embed_lyrics else None,
                write_tags,
                status,
            )
        elif ext in ("dsf", "wav"):
            _apply_id3(
                dst_path,
                ext,
                title, artist, album, album_artist, year, track_no, disc_no, genre,
                cover_jpeg if embed_cover else None,
                lyric_text if embed_lyrics else None,
                write_tags,
                status,
            )
    except Exception:
        # Never fail a track transfer just because an exotic container rejected tagging
        pass
    return status


def _apply_id3(
    dst_path: str,
    kind: str,
    title: str,
    artist: str,
    album: str,
    album_artist: str,
    year: str,
    track_no: Optional[int],
    disc_no: Optional[int],
    genre: str,
    cover_jpeg: Optional[bytes],
    lyric_text: Optional[str],
    write_tags: bool,
    status: Dict[str, bool],
) -> None:
    from mutagen.dsf import DSF
    from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, USLT, ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.wave import WAVE

    if kind == "mp3":
        try:
            audio = MP3(dst_path)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
        except ID3NoHeaderError:
            tags = ID3()
            audio = None
    elif kind == "dsf":
        audio = DSF(dst_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
    else:
        audio = WAVE(dst_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

    if write_tags:
        if title:
            tags.delall("TIT2")
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=album))
        if album_artist:
            tags.delall("TPE2")
            tags.add(TPE2(encoding=3, text=album_artist))
        if year:
            tags.delall("TDRC")
            tags.add(TDRC(encoding=3, text=year))
        if track_no:
            tags.delall("TRCK")
            tags.add(TRCK(encoding=3, text=str(track_no)))
        if disc_no:
            tags.delall("TPOS")
            tags.add(TPOS(encoding=3, text=str(disc_no)))
        if genre:
            tags.delall("TCON")
            tags.add(TCON(encoding=3, text=genre))
        status["tags"] = True

    if cover_jpeg:
        tags.delall("APIC")
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=cover_jpeg,
            )
        )
        status["cover"] = True

    if lyric_text:
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="chi", desc="", text=lyric_text))
        status["lyrics"] = True

    if audio is not None:
        audio.save(v2_version=3)
    else:
        tags.save(dst_path, v2_version=3)


def _apply_flac(
    dst_path: str,
    title: str,
    artist: str,
    album: str,
    album_artist: str,
    year: str,
    track_no: Optional[int],
    disc_no: Optional[int],
    genre: str,
    cover_jpeg: Optional[bytes],
    cover_dims: Tuple[int, int],
    lyric_text: Optional[str],
    write_tags: bool,
    status: Dict[str, bool],
) -> None:
    from mutagen.flac import FLAC, Picture

    audio = FLAC(dst_path)
    if write_tags:
        if title:
            audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        if album:
            audio["album"] = [album]
        if album_artist:
            audio["albumartist"] = [album_artist]
        if year:
            audio["date"] = [year]
        if track_no:
            audio["tracknumber"] = [str(track_no)]
        if disc_no:
            audio["discnumber"] = [str(disc_no)]
        if genre:
            audio["genre"] = [genre]
        status["tags"] = True

    if cover_jpeg:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.width, pic.height = cover_dims
        pic.depth = 24
        pic.data = cover_jpeg
        audio.clear_pictures()
        audio.add_picture(pic)
        status["cover"] = True

    if lyric_text:
        audio["lyrics"] = [lyric_text]
        status["lyrics"] = True

    audio.save()


def _apply_mp4(
    dst_path: str,
    title: str,
    artist: str,
    album: str,
    album_artist: str,
    year: str,
    track_no: Optional[int],
    disc_no: Optional[int],
    genre: str,
    cover_jpeg: Optional[bytes],
    lyric_text: Optional[str],
    write_tags: bool,
    status: Dict[str, bool],
) -> None:
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(dst_path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    if write_tags:
        if title:
            tags["\xa9nam"] = [title]
        if artist:
            tags["\xa9ART"] = [artist]
        if album:
            tags["\xa9alb"] = [album]
        if album_artist:
            tags["aART"] = [album_artist]
        if year:
            tags["\xa9day"] = [year]
        if track_no:
            tags["trkn"] = [(int(track_no), 0)]
        if disc_no:
            tags["disk"] = [(int(disc_no), 0)]
        if genre:
            tags["\xa9gen"] = [genre]
        status["tags"] = True

    if cover_jpeg:
        tags["covr"] = [MP4Cover(cover_jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        status["cover"] = True

    if lyric_text:
        tags["\xa9lyr"] = [lyric_text]
        status["lyrics"] = True

    audio.save()


def _apply_ogg(
    dst_path: str,
    title: str,
    artist: str,
    album: str,
    album_artist: str,
    year: str,
    track_no: Optional[int],
    disc_no: Optional[int],
    genre: str,
    cover_jpeg: Optional[bytes],
    cover_dims: Tuple[int, int],
    lyric_text: Optional[str],
    write_tags: bool,
    status: Dict[str, bool],
) -> None:
    from mutagen.flac import Picture
    from mutagen.oggvorbis import OggVorbis

    audio = OggVorbis(dst_path)
    if write_tags:
        if title:
            audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        if album:
            audio["album"] = [album]
        if album_artist:
            audio["albumartist"] = [album_artist]
        if year:
            audio["date"] = [year]
        if track_no:
            audio["tracknumber"] = [str(track_no)]
        if disc_no:
            audio["discnumber"] = [str(disc_no)]
        if genre:
            audio["genre"] = [genre]
        status["tags"] = True

    if cover_jpeg:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.width, pic.height = cover_dims
        pic.depth = 24
        pic.data = cover_jpeg
        b64 = base64.b64encode(pic.write()).decode("ascii")
        audio["metadata_block_picture"] = [b64]
        status["cover"] = True

    if lyric_text:
        audio["lyrics"] = [lyric_text]
        status["lyrics"] = True

    audio.save()


def copy_file_atomic(src_path: str, dst_path: str, cancel_check=None) -> int:
    """Copy a file to dst_path atomically using a temporary file."""
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    tmp = dst_path + ".tmp_echo"
    total = 0
    chunk_size = 1024 * 1024  # 1 MiB chunks for responsive progress & cancellation
    try:
        with open(src_path, "rb") as src, open(tmp, "wb") as dst:
            while True:
                if cancel_check and cancel_check():
                    raise InterruptedError("传输已取消")
                buf = src.read(chunk_size)
                if not buf:
                    break
                dst.write(buf)
                total += len(buf)
        os.replace(tmp, dst_path)
        return total
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def transcode_or_split_to_flac(
    src_path: str,
    dst_flac_path: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> int:
    """Use ffmpeg to transcode an unsupported format or split a CUE track into FLAC."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("系统未安装 ffmpeg，无法转码或切分 CUE 音轨")

    os.makedirs(os.path.dirname(dst_flac_path), exist_ok=True)
    tmp = dst_flac_path + ".tmp_echo.flac"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if start_ms is not None and start_ms > 0:
        cmd.extend(["-ss", f"{start_ms / 1000.0:.3f}"])
    cmd.extend(["-i", src_path])
    if end_ms is not None and end_ms > 0:
        dur_ms = end_ms - (start_ms or 0)
        if dur_ms > 0:
            cmd.extend(["-t", f"{dur_ms / 1000.0:.3f}"])
    cmd.extend(["-vn", "-c:a", "flac", "-compression_level", "5", tmp])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg 处理失败: {(res.stderr or '').strip()[:240]}")
        os.replace(tmp, dst_flac_path)
        return os.path.getsize(dst_flac_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def needs_transcode(suffix: str, transcode_enabled: bool) -> bool:
    s = (suffix or "").lower().lstrip(".")
    return transcode_enabled and s not in ECHO_NATIVE_SUFFIXES
