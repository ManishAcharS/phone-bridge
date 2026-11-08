"""Local, offline-only storage for the PC identity and pinned phones.

Nothing here ever leaves the machine: no accounts, no cloud, no telemetry.  On
Windows the default location is ``%LOCALAPPDATA%\\PhoneBridge``; elsewhere (for
development on Linux/macOS) ``~/.config/phonebridge``.

File layout::

    <config_dir>/
        pc_identity.pem     # ECDH P-256 private key, mode 0600
        pc.json             # device name, last connection, UI preferences
        peers.json          # pinned phone identity keys
        known_hosts.json    # last seen endpoints (ip/port/transport)
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocol.crypto import Identity, b64d, b64e


def default_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "PhoneBridge"
    env = os.environ.get("PHONEBRIDGE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".config" / "phonebridge"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:  # belt and braces on POSIX; Windows ignores POSIX modes  # noqa: SIM105
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    _write_private(path, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))


@dataclass
class Peer:
    """A pinned phone."""

    peer_id: str
    peer_name: str
    static_key: bytes
    added_at: str
    last_seen: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "peer_name": self.peer_name,
            "static_key": b64e(self.static_key),
            "added_at": self.added_at,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Peer:
        return cls(
            peer_id=data["peer_id"],
            peer_name=data.get("peer_name", "phone"),
            static_key=b64d(data["static_key"]),
            added_at=data.get("added_at", ""),
            last_seen=data.get("last_seen"),
        )


class DeviceStore:
    def __init__(self, config_dir: Path | None = None) -> None:
        self.dir = config_dir or default_config_dir()
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- identity ----------------------------------------------------------- #
    @property
    def identity_path(self) -> Path:
        return self.dir / "pc_identity.pem"

    def load_or_create_identity(self, name: str | None = None) -> Identity:
        if self.identity_path.exists():
            ident = Identity.from_pem(self.identity_path.read_bytes(), name="")
            ident.name = self._pc_name(name or ident.name or "windows-pc")
            return ident
        ident = Identity.generate(name=self._pc_name(name or "windows-pc"))
        _write_private(self.identity_path, ident.to_pem())
        return ident

    # -- pc settings -------------------------------------------------------- #
    def _pc_file(self) -> Path:
        return self.dir / "pc.json"

    def pc_settings(self) -> dict[str, Any]:
        return _read_json(self._pc_file(), {})

    def _pc_name(self, fallback: str) -> str:
        name = self.pc_settings().get("device_name")
        if name:
            return str(name)
        return fallback or os.environ.get("COMPUTERNAME", "windows-pc")

    def set_pc_name(self, name: str) -> None:
        data = self.pc_settings()
        data["device_name"] = name
        _write_json(self._pc_file(), data)

    # -- peers -------------------------------------------------------------- #
    def _peers_file(self) -> Path:
        return self.dir / "peers.json"

    def list_peers(self) -> list[Peer]:
        raw = _read_json(self._peers_file(), {"peers": []})
        return [Peer.from_json(p) for p in raw.get("peers", [])]

    def find_peer(self, peer_id: str) -> Peer | None:
        return next((p for p in self.list_peers() if p.peer_id == peer_id), None)

    def pin_peer(self, peer_id: str, peer_name: str, static_key: bytes) -> Peer:
        peers = {p.peer_id: p for p in self.list_peers()}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        existing = peers.get(peer_id)
        peer = Peer(
            peer_id=peer_id,
            peer_name=peer_name,
            static_key=static_key,
            added_at=existing.added_at if existing else now,
            last_seen=now,
        )
        peers[peer_id] = peer
        _write_json(self._peers_file(), {"peers": [p.to_json() for p in peers.values()]})
        return peer

    def forget_peer(self, peer_id: str) -> bool:
        peers = {p.peer_id: p for p in self.list_peers()}
        if peer_id not in peers:
            return False
        del peers[peer_id]
        data = [p.to_json() for p in peers.values()]
        _write_json(self._peers_file(), {"peers": data})
        return True

    # -- last endpoints ----------------------------------------------------- #
    def _known_hosts_file(self) -> Path:
        return self.dir / "known_hosts.json"

    def remember_endpoint(self, transport: str, host: str, port: int) -> None:
        data = _read_json(self._known_hosts_file(), [])
        entry = {"transport": transport, "host": host, "port": port,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        data = [e for e in data if not (e.get("transport") == transport and e.get("host") == host)]
        data.insert(0, entry)
        _write_json(self._known_hosts_file(), data[:12])

    def known_endpoints(self) -> list[dict[str, Any]]:
        return _read_json(self._known_hosts_file(), [])

    def path_summary(self) -> str:
        return str(self.dir)
