#!/usr/bin/env python3
"""Read and atomically write OmaYap settings without following local objects."""

from __future__ import annotations

import json
import math
import os
import secrets
import stat
import sys
from pathlib import Path


MAX_BYTES = 4096
EXIT_USAGE = 64
EXIT_UNSAFE = 65
EXIT_TOO_LARGE = 66
EXIT_IO = 74
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
VALID_CLEANUP_PROFILES = {"off", "safe", "article"}


def _settings_path(raw: str) -> Path | None:
    if not raw or "\x00" in raw or "\n" in raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path == Path("/") or path.name != "settings.json":
        return None
    return path


def _open_parent(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.parent, flags)
    info = os.fstat(descriptor)
    if not (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == PRIVATE_DIRECTORY_MODE
    ):
        os.close(descriptor)
        raise PermissionError
    return descriptor


def _private_regular(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == PRIVATE_FILE_MODE
    )


def _open_settings(parent: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    if not _private_regular(os.fstat(descriptor)):
        os.close(descriptor)
        raise PermissionError
    return descriptor


def read_settings(path: Path) -> int:
    try:
        parent = _open_parent(path)
    except FileNotFoundError:
        return 0
    except OSError:
        return EXIT_UNSAFE

    try:
        try:
            descriptor = _open_settings(parent, path.name)
        except FileNotFoundError:
            return 0
        except OSError:
            return EXIT_UNSAFE
        try:
            info = os.fstat(descriptor)
            if info.st_size > MAX_BYTES:
                return EXIT_TOO_LARGE
            output = bytearray()
            while len(output) <= MAX_BYTES:
                chunk = os.read(descriptor, MAX_BYTES + 1 - len(output))
                if not chunk:
                    break
                output.extend(chunk)
            if len(output) > MAX_BYTES:
                return EXIT_TOO_LARGE
            sys.stdout.buffer.write(bytes(output))
            sys.stdout.buffer.flush()
            return 0
        finally:
            os.close(descriptor)
    except OSError:
        return EXIT_IO
    finally:
        os.close(parent)


def _validated_payload() -> bytes | None:
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        parsed = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"speed", "cleanupProfile"}:
        return None
    speed = parsed["speed"]
    profile = parsed["cleanupProfile"]
    if (
        isinstance(speed, bool)
        or not isinstance(speed, (int, float))
        or not math.isfinite(speed)
        or not 0.5 <= float(speed) <= 2.0
        or profile not in VALID_CLEANUP_PROFILES
    ):
        return None
    canonical = {
        "speed": round(float(speed), 3),
        "cleanupProfile": profile,
    }
    return (json.dumps(canonical, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _target_is_safe_or_missing(parent: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return _private_regular(info)


def _create_stage(parent: int) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(16):
        name = f".settings.{secrets.token_hex(12)}.tmp"
        try:
            descriptor = os.open(name, flags, PRIVATE_FILE_MODE, dir_fd=parent)
        except FileExistsError:
            continue
        info = os.fstat(descriptor)
        if not _private_regular(info):
            os.close(descriptor)
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
            raise PermissionError
        return name, descriptor
    raise FileExistsError


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError
        offset += written


def write_settings(path: Path) -> int:
    payload = _validated_payload()
    if payload is None:
        return EXIT_USAGE
    try:
        parent = _open_parent(path)
    except OSError:
        return EXIT_UNSAFE

    stage_name = ""
    stage_descriptor = -1
    try:
        if not _target_is_safe_or_missing(parent, path.name):
            return EXIT_UNSAFE
        stage_name, stage_descriptor = _create_stage(parent)
        _write_all(stage_descriptor, payload)
        os.fchmod(stage_descriptor, PRIVATE_FILE_MODE)
        os.fsync(stage_descriptor)
        os.close(stage_descriptor)
        stage_descriptor = -1
        if not _target_is_safe_or_missing(parent, path.name):
            return EXIT_UNSAFE
        os.replace(stage_name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        stage_name = ""
        os.fsync(parent)
        return 0
    except OSError:
        return EXIT_IO
    finally:
        if stage_descriptor >= 0:
            try:
                os.close(stage_descriptor)
            except OSError:
                pass
        if stage_name:
            try:
                os.unlink(stage_name, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2 or arguments[0] not in {"read", "write"}:
        return EXIT_USAGE
    path = _settings_path(arguments[1])
    if path is None:
        return EXIT_UNSAFE
    try:
        return read_settings(path) if arguments[0] == "read" else write_settings(path)
    except Exception:
        # Never expose paths, settings, or exception text through diagnostics.
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
