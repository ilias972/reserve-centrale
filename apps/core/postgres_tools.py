"""Resolve PostgreSQL client tools without starting or changing a server."""

import os
import re
import shutil
import subprocess
from pathlib import Path

_ALLOWED_TOOLS = {"pg_dump", "pg_restore"}


def _client_major(path: Path) -> int | None:
    try:
        # Fixed PostgreSQL utility and argument; no request data reaches the command.
        result = subprocess.run(  # noqa: S603
            [str(path), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"PostgreSQL\)\s+(\d+)(?:\.|\s|$)", result.stdout + result.stderr)
    return int(match.group(1)) if match else None


def binary(name: str, *, server_major: int | None = None) -> str:
    """Find an allowed client tool compatible with the connected server major."""
    if name not in _ALLOWED_TOOLS:
        raise ValueError("Outil PostgreSQL non autorisé.")
    configured = os.environ.get("PG_BIN")
    candidates = []
    if configured:
        candidates.append(Path(configured) / name)
    pg_config = shutil.which("pg_config")
    if pg_config:
        try:
            # Resolve a system PostgreSQL tool directory using a fixed argument.
            result = subprocess.run(  # noqa: S603
                [pg_config, "--bindir"], check=True, capture_output=True, text=True, timeout=5
            )
            bindir = result.stdout.strip()
            if bindir:
                candidates.append(Path(bindir) / name)
        except (OSError, subprocess.SubprocessError):
            pass
    discovered = shutil.which(name)
    if discovered:
        candidates.append(Path(discovered))
    seen = set()
    for candidate in candidates:
        identity = str(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if server_major is not None and _client_major(candidate) != server_major:
            continue
        return identity
    if server_major is None:
        raise FileNotFoundError("Installez les outils client PostgreSQL ou définissez PG_BIN.")
    raise FileNotFoundError(
        f"Installez pg_dump/pg_restore de la version majeure PostgreSQL {server_major} "
        "ou définissez PG_BIN."
    )
