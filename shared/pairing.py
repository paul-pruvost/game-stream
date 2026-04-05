"""
GameStream Pairing — Known hosts for trust-on-first-use.

After a client connects to a host for the first time, the host's TLS
fingerprint is saved locally. Subsequent connections auto-pin the
certificate without requiring --fingerprint.

Similar to SSH known_hosts.

Storage: ~/.gamestream/known_hosts.json
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


KNOWN_HOSTS_FILE = Path.home() / ".gamestream" / "known_hosts.json"


class KnownHosts:
    """Manages paired host fingerprints for trust-on-first-use."""

    def __init__(self, path: Path = KNOWN_HOSTS_FILE):
        self._path = path
        self._hosts = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._hosts, indent=2), encoding="utf-8"
        )

    def get(self, host: str, port: int) -> Optional[dict]:
        """Return saved entry for host:port, or None."""
        return self._hosts.get(f"{host}:{port}")

    def fingerprint(self, host: str, port: int) -> Optional[str]:
        """Return saved fingerprint for host:port, or None."""
        entry = self.get(host, port)
        return entry["fingerprint"] if entry else None

    def save(self, host: str, port: int, fingerprint: str, name: str = ""):
        """Save or update a paired host."""
        key = f"{host}:{port}"
        now = datetime.now(timezone.utc).isoformat()
        old = self._hosts.get(key, {})
        self._hosts[key] = {
            "fingerprint": fingerprint,
            "name": name or old.get("name", ""),
            "paired_at": old.get("paired_at", now),
            "last_seen": now,
        }
        self._save()

    def remove(self, host: str, port: int):
        """Remove a paired host."""
        key = f"{host}:{port}"
        if key in self._hosts:
            del self._hosts[key]
            self._save()

    def all(self) -> dict:
        """Return all paired hosts."""
        return dict(self._hosts)
