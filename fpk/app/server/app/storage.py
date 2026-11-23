# -*- coding: utf-8 -*-
"""External storage / Echo USB device discovery and management for fnOS."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

EXTERNAL_MOUNT_PREFIXES = ("/vol00/", "/mnt/", "/media/")
ECHO_LABEL_HINTS = ("echo", "aiyeon", "fiio", "snowsky", "sd_card", "internal_storage", "removabledisk", "storage_device")
ECHO_USB_HINTS = ("aiyeon", "fiio", "snowsky", "echo", "rockchip", "rk2118")
IGNORED_FS_TYPES = {
    "proc", "sysfs", "devpts", "tmpfs", "devtmpfs", "cgroup", "cgroup2",
    "overlay", "squashfs", "autofs", "fusectl", "efivarfs", "mqueue",
    "debugfs", "tracefs", "securityfs", "pstore", "rpc_pipefs", "zfs",
}


def _unescape_mount_path(raw: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), raw)


def _sysfs_block_info(device: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {"removable": False, "vendor": "", "model": "", "block_name": ""}
    if not device.startswith("/dev/"):
        return info
    real_dev = os.path.realpath(device)
    part_name = os.path.basename(real_dev)
    sys_part = Path(f"/sys/class/block/{part_name}")
    if not sys_part.exists():
        return info

    # If partition, parent is the whole disk block dir
    disk_dir = sys_part.resolve()
    if (disk_dir / "partition").exists():
        disk_dir = disk_dir.parent
    info["block_name"] = disk_dir.name

    for key, rel in (("removable", "removable"), ("vendor", "device/vendor"), ("model", "device/model")):
        fp = disk_dir / rel
        if fp.exists():
            try:
                val = fp.read_text(encoding="utf-8", errors="ignore").strip()
                if key == "removable":
                    info["removable"] = val == "1"
                else:
                    info[key] = val
            except OSError:
                pass
    return info


def _build_device_entry(mountpoint: str, device: str, fstype: str) -> Optional[Dict[str, Any]]:
    if not os.path.isdir(mountpoint):
        return None
    try:
        usage = shutil.disk_usage(mountpoint)
    except OSError:
        return None

    label = os.path.basename(mountpoint.rstrip("/")) or mountpoint
    sys_info = _sysfs_block_info(device)
    has_echo_dir = os.path.isdir(os.path.join(mountpoint, ".echo"))
    has_music_dir = os.path.isdir(os.path.join(mountpoint, "Music"))
    has_audiobooks_dir = os.path.isdir(os.path.join(mountpoint, "Audiobooks"))

    low_label = label.lower()
    usb_text = f"{sys_info['vendor']} {sys_info['model']}".lower()
    is_echo = (
        has_echo_dir
        or any(h in usb_text for h in ECHO_USB_HINTS)
        or any(h in low_label for h in ECHO_LABEL_HINTS)
    )

    # Friendly role tag
    role_hint = "外挂存储"
    if has_echo_dir:
        role_hint = "Echo 播放器 (已检测到 .echo 索引)"
    elif "sd_card" in low_label or "sd card" in low_label:
        role_hint = "Echo TF/SD 卡"
    elif "internal" in low_label:
        role_hint = "Echo 机身内置存储"
    elif any(h in usb_text for h in ECHO_USB_HINTS):
        role_hint = f"Echo 设备 ({sys_info['vendor']} {sys_info['model']})".strip()
    elif sys_info["removable"] or mountpoint.startswith("/vol00/"):
        role_hint = "USB 外挂移动存储"

    writable = os.access(mountpoint, os.W_OK)
    return {
        "mountpoint": mountpoint,
        "label": label,
        "device": device,
        "fstype": fstype,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round((usage.used / usage.total * 100), 1) if usage.total else 0.0,
        "writable": writable,
        "removable": sys_info["removable"],
        "usb_vendor": sys_info["vendor"],
        "usb_model": sys_info["model"],
        "has_echo_dir": has_echo_dir,
        "has_music_dir": has_music_dir,
        "has_audiobooks_dir": has_audiobooks_dir,
        "is_echo_likely": is_echo,
        "role_hint": role_hint,
    }


def list_external_devices() -> List[Dict[str, Any]]:
    """Return currently mounted external USB storage devices (primarily /vol00/* on fnOS)."""
    devices: List[Dict[str, Any]] = []
    seen_mounts = set()

    if os.path.isfile("/proc/mounts"):
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    dev, raw_mp, fstype = parts[0], parts[1], parts[2]
                    if fstype in IGNORED_FS_TYPES:
                        continue
                    mp = _unescape_mount_path(raw_mp)
                    if mp in seen_mounts:
                        continue
                    if not any(mp.startswith(prefix) for prefix in EXTERNAL_MOUNT_PREFIXES):
                        continue
                    entry = _build_device_entry(mp, dev, fstype)
                    if entry:
                        devices.append(entry)
                        seen_mounts.add(mp)
        except OSError:
            pass

    # Optional extra test mounts via env var (colon-separated) for local/CI verification
    extra = os.environ.get("ECHO_EXTRA_MOUNTS", "").strip()
    if extra:
        for raw_mp in extra.split(":"):
            mp = os.path.abspath(raw_mp.strip())
            if mp and mp not in seen_mounts and os.path.isdir(mp):
                entry = _build_device_entry(mp, "local-dir", "local")
                if entry:
                    entry["role_hint"] = "自定义/测试存储目录"
                    devices.append(entry)
                    seen_mounts.add(mp)

    # Sort: confirmed .echo devices first, then Echo-likely / SD_card, then largest free space
    devices.sort(
        key=lambda d: (
            1 if d["has_echo_dir"] else 0,
            1 if d["is_echo_likely"] else 0,
            1 if "sd_card" in d["label"].lower() else 0,
            d["free_bytes"],
        ),
        reverse=True,
    )
    return devices


def validate_mount_path(mountpoint: str) -> Dict[str, Any]:
    """Ensure the requested mountpoint is an actively mounted external storage device."""
    norm = os.path.abspath(mountpoint)
    for dev in list_external_devices():
        if os.path.abspath(dev["mountpoint"]) == norm:
            return dev
    raise ValueError(f"目标外挂设备未挂载或已断开: {mountpoint}")


def browse_device_files(mountpoint: str, rel_path: str = "") -> Dict[str, Any]:
    """Browse directories and files on a mounted Echo device."""
    dev = validate_mount_path(mountpoint)
    root = os.path.abspath(dev["mountpoint"])
    clean_rel = (rel_path or "").strip().strip("/")
    target = os.path.abspath(os.path.join(root, clean_rel)) if clean_rel else root
    if target != root and not target.startswith(root + os.sep):
        raise PermissionError("路径越界，已拒绝访问")
    if not os.path.isdir(target):
        raise FileNotFoundError(f"目录不存在: {target}")

    items: List[Dict[str, Any]] = []
    with os.scandir(target) as it:
        for entry in it:
            if entry.name in (".Spotlight-V100", ".Trashes", ".fseventsd", "System Volume Information"):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
                sub_rel = os.path.relpath(entry.path, root)
                items.append(
                    {
                        "name": entry.name,
                        "rel_path": sub_rel,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime),
                        "is_hidden": entry.name.startswith("."),
                    }
                )
            except OSError:
                continue

    items.sort(key=lambda x: (x["is_hidden"], not x["is_dir"], x["name"].lower()))
    return {
        "device": dev,
        "rel_path": "" if target == root else os.path.relpath(target, root),
        "items": items,
    }


def delete_device_path(mountpoint: str, rel_path: str) -> Dict[str, Any]:
    """Safely delete a file or folder inside the mounted Echo storage."""
    dev = validate_mount_path(mountpoint)
    root = os.path.abspath(dev["mountpoint"])
    clean_rel = (rel_path or "").strip().strip("/")
    if not clean_rel or clean_rel in (".", "..", ".echo"):
        raise PermissionError("不允许删除根目录或 .echo 系统目录")
    target = os.path.abspath(os.path.join(root, clean_rel))
    if not target.startswith(root + os.sep):
        raise PermissionError("路径越界，已拒绝删除")
    if not os.path.exists(target):
        raise FileNotFoundError(f"目标不存在: {clean_rel}")
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.unlink(target)
    sync_mount(root)
    return {"deleted": clean_rel}


def sync_mount(mountpoint: Optional[str] = None) -> Dict[str, Any]:
    """Flush kernel filesystem buffers so external USB storage is safe to unplug."""
    synced_target = "all"
    if mountpoint and os.path.exists(mountpoint):
        try:
            subprocess.run(["sync", "-f", mountpoint], check=False, timeout=30)
            synced_target = mountpoint
        except Exception:
            os.sync()
    else:
        os.sync()
    return {"ok": True, "synced": synced_target}
