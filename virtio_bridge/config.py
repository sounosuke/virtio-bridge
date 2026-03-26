"""
Configuration file support for virtio-bridge.

Supports TOML config files (Python 3.11+ tomllib, fallback to tomli).
CLI flags always override config file values.

Example config (bridge.toml):

    [server]
    target = "http://localhost:11434"
    bridge_dir = "~/.bridge"
    allow_host = "localhost,127.0.0.1,::1"
    verbose = false

    [client]
    listen = "127.0.0.1:11434"
    bridge_dir = "~/.bridge"
    timeout = 30.0
    verbose = false

    [socks]
    listen = "127.0.0.1:1080"
    bridge_dir = "~/.bridge"
    verbose = false

    [tcp-relay]
    bridge_dir = "~/.bridge"
    allow_host = "localhost,127.0.0.1,::1"
    verbose = false
"""

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("virtio-bridge.config")

# TOML keys → CLI arg names mapping (per subcommand section)
# Keys with hyphens in TOML map to underscores in argparse
_KEY_MAP = {
    "bridge_dir": "bridge_dir",
    "bridge-dir": "bridge_dir",
    "target": "target",
    "listen": "listen",
    "allow_host": "allow_host",
    "allow-host": "allow_host",
    "timeout": "timeout",
    "verbose": "verbose",
    "max_age": "max_age",
    "max-age": "max_age",
    "secret": "secret",
    "auto_encrypt": "auto_encrypt",
    "auto-encrypt": "auto_encrypt",
}


def _load_toml(path: str) -> dict[str, Any]:
    """Load a TOML file, trying tomllib (3.11+) then tomli."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            raise ImportError(
                "TOML support requires Python 3.11+ or 'pip install tomli'"
            )

    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(config_path: str, section: str) -> dict[str, Any]:
    """Load config for a specific subcommand section.

    Args:
        config_path: Path to the TOML config file
        section: Subcommand name (e.g. "server", "client", "socks", "tcp-relay")

    Returns:
        Dict of normalized config values for the section.
        Empty dict if section doesn't exist.
    """
    path = os.path.expanduser(config_path)
    if not Path(path).exists():
        logger.warning(f"Config file not found: {path}")
        return {}

    data = _load_toml(path)
    raw = data.get(section, {})
    if not isinstance(raw, dict):
        return {}

    result = {}
    for key, value in raw.items():
        normalized = _KEY_MAP.get(key, key.replace("-", "_"))
        # Expand ~ in path-like values
        if isinstance(value, str) and normalized in ("bridge_dir",):
            value = os.path.expanduser(value)
        result[normalized] = value

    return result


def apply_config(args, config: dict[str, Any], defaults: dict[str, Any]) -> None:
    """Apply config values to argparse namespace where CLI didn't override.

    Only sets values from config if the arg is still at its default.
    CLI flags always take precedence.

    Args:
        args: argparse.Namespace
        config: Dict from load_config()
        defaults: Dict of argparse defaults for the subcommand
    """
    for key, value in config.items():
        current = getattr(args, key, None)
        default = defaults.get(key)
        # If current value equals the argparse default, config overrides it
        if current == default or current is None:
            setattr(args, key, value)
