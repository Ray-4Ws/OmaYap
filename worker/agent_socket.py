#!/usr/bin/env python3
"""Bounded, private Unix-socket transport for explicit agent announcements.

This module deliberately knows nothing about Piper or the desktop service.  A
caller supplies a metadata-only request handler; request text remains in
memory for the lifetime of one connection and is never logged or persisted.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import struct
import threading
from pathlib import Path
from typing import Any, Callable, Optional


PROTOCOL_VERSION = 1
MAX_CHARS = 20_000
MAX_TEXT_BYTES = MAX_CHARS * 4
# This allows a fully escaped 20,000-code-point JSON string while keeping the
# socket reader bounded.  It includes the command object and newline.
MAX_FRAME_BYTES = 128 * 1024
READ_TIMEOUT = 3.0
WRITE_TIMEOUT = 2.0
STALE_PROBE_TIMEOUT = 0.25
BACKLOG = 4


class AgentSocketServer:
    """Serve one newline-delimited request per same-UID Unix connection."""

    def __init__(self, handler: Callable[[Any], bytes], path: Path) -> None:
        self.handler = handler
        self.path = Path(path)
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._path_owned = False
        self._path_identity: Optional[tuple[int, int]] = None

    @staticmethod
    def _lstat(path: Path) -> Optional[os.stat_result]:
        try:
            return path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError("socket-path-unavailable") from exc

    @classmethod
    def _remove_stale_socket(cls, path: Path) -> None:
        """Remove only a stale same-UID socket, never a symlink or regular file."""

        info = cls._lstat(path)
        if info is None:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("socket-path-unsafe")

        probe: Optional[socket.socket] = None
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(STALE_PROBE_TIMEOUT)
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            # Refused/absent means the path is stale.  A timeout is treated as
            # active: never unlink a socket merely because its peer is busy.
            if isinstance(exc, TimeoutError) or getattr(exc, "errno", None) not in {
                errno.ECONNREFUSED,
                errno.ENOENT,
            }:
                raise RuntimeError("socket-active") from exc
        else:
            raise RuntimeError("socket-active")
        finally:
            if probe is not None:
                probe.close()

        original_identity = (int(info.st_dev), int(info.st_ino))
        current = cls._lstat(path)
        current_identity = (
            (int(current.st_dev), int(current.st_ino)) if current is not None else None
        )
        if (
            current is not None
            and stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.getuid()
            and current_identity == original_identity
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise RuntimeError("socket-cleanup-failed") from exc

    def _prepare_path(self) -> None:
        if not self.path.is_absolute() or "\x00" in str(self.path):
            raise RuntimeError("socket-path-unsafe")
        parent_info = self._lstat(self.path.parent)
        if parent_info is None or not stat.S_ISDIR(parent_info.st_mode):
            raise RuntimeError("socket-directory-missing")
        if parent_info.st_uid != os.getuid() or (parent_info.st_mode & 0o022):
            raise RuntimeError("socket-directory-unsafe")
        self._remove_stale_socket(self.path)

    @staticmethod
    def _peer_uid(connection: socket.socket) -> Optional[int]:
        """Return the kernel-provided peer UID where the platform supports it."""

        if hasattr(socket, "SO_PEERCRED"):
            try:
                raw = connection.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_PEERCRED,
                    struct.calcsize("3i"),
                )
                _pid, uid, _gid = struct.unpack("3i", raw)
                return int(uid)
            except (OSError, struct.error):
                return None
        getpeereid = getattr(connection, "getpeereid", None)
        if getpeereid is not None:
            try:
                uid, _gid = getpeereid()
                return int(uid)
            except (OSError, TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _ack(*, ok: bool, error: str = "") -> bytes:
        payload: dict[str, Any] = {"protocolVersion": PROTOCOL_VERSION, "ok": bool(ok)}
        if error:
            payload["error"] = error
        return (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")

    @staticmethod
    def _read_frame(connection: socket.socket) -> tuple[Optional[bytes], Optional[str]]:
        body = bytearray()
        while True:
            try:
                piece = connection.recv(min(4096, MAX_FRAME_BYTES + 1 - len(body)))
            except socket.timeout:
                return None, "socket-timeout"
            except OSError:
                return None, "socket-read-failed"
            if not piece:
                return None, "invalid-frame"
            newline = piece.find(b"\n")
            if newline >= 0:
                body.extend(piece[:newline])
                if len(body) > MAX_FRAME_BYTES:
                    return None, "payload-too-large"
                # One request per connection; do not execute hidden trailing
                # bytes that could otherwise become a second command.
                if piece[newline + 1 :].strip():
                    return None, "invalid-frame"
                return bytes(body), None
            body.extend(piece)
            if len(body) > MAX_FRAME_BYTES:
                return None, "payload-too-large"

    @classmethod
    def _decode_request(
        cls, frame: Optional[bytes], frame_error: Optional[str]
    ) -> tuple[Optional[Any], bytes]:
        if frame_error:
            return None, cls._ack(ok=False, error=frame_error)
        if frame is None or not frame:
            return None, cls._ack(ok=False, error="invalid-request")
        try:
            message = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None, cls._ack(ok=False, error="invalid-request")
        return message, b""

    def _handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(READ_TIMEOUT)
        if self._peer_uid(connection) != os.getuid():
            response = self._ack(ok=False, error="peer-not-authorized")
        else:
            frame, frame_error = self._read_frame(connection)
            message, response = self._decode_request(frame, frame_error)
            if message is not None:
                try:
                    response = self.handler(message)
                except Exception:
                    # The worker handler is expected to return a fixed ack. A
                    # transport-level fallback must remain fixed as well.
                    response = self._ack(ok=False, error="request-failed")
                if not isinstance(response, bytes):
                    response = self._ack(ok=False, error="request-failed")
        try:
            connection.settimeout(WRITE_TIMEOUT)
            connection.sendall(response)
        except OSError:
            return

    def start(self) -> None:
        self._prepare_path()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(BACKLOG)
            listener.settimeout(0.25)
        except FileExistsError as exc:
            listener.close()
            raise RuntimeError("socket-active") from exc
        except OSError as exc:
            listener.close()
            code = "socket-active" if exc.errno == errno.EADDRINUSE else "socket-bind-failed"
            raise RuntimeError(code) from exc
        self._listener = listener
        self._path_owned = True
        info = self._lstat(self.path)
        if info is None or not stat.S_ISSOCK(info.st_mode):
            listener.close()
            self._path_owned = False
            raise RuntimeError("socket-bind-failed")
        self._path_identity = (int(info.st_dev), int(info.st_ino))
        self._thread = threading.Thread(target=self._serve, name="omayap-agent", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop_event.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_connection(connection)
            finally:
                connection.close()

    def close(self) -> None:
        self._stop_event.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if self._path_owned:
            self._path_owned = False
            try:
                info = self._lstat(self.path)
            except RuntimeError:
                info = None
            identity = (
                (int(info.st_dev), int(info.st_ino)) if info is not None else None
            )
            if info is not None and stat.S_ISSOCK(info.st_mode) and identity == self._path_identity:
                try:
                    self.path.unlink()
                except OSError:
                    pass
            self._path_identity = None
