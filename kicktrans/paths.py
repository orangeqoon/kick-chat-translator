"""Repository-relative runtime paths; importing this module never writes files."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from scripts_core.paths import ROOT_DIR, STATE_DIR
except ModuleNotFoundError as exc:
    if exc.name not in ("scripts_core", "scripts_core.paths"):
        raise
    package_root = Path(__file__).resolve().parents[1]
    # Preserve the host repository's existing state directory, while a ZIP or
    # standalone clone keeps all runtime files inside its own folder.
    host_root = package_root.parent.parent
    integrated = package_root.parent.name == "streaming" and (host_root / "scripts_core").is_dir()
    default_root = host_root if integrated else package_root
    ROOT_DIR = Path(os.environ.get("SCRIPTS_ROOT") or default_root).resolve()
    if not ROOT_DIR.is_dir():
        raise RuntimeError("SCRIPTS_ROOT must refer to an existing directory")
    STATE_DIR = Path(os.environ.get("SCRIPTS_STATE") or ROOT_DIR / "state").resolve()

SERVICES_DIR = STATE_DIR / "services"
TOKENS_DIR = STATE_DIR / "tokens"
LOGS_DIR = STATE_DIR / "logs"
