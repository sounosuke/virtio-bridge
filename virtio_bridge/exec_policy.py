"""
Exec policy — controls which commands the bridge server may execute.

The policy file lives on the Mac side (NOT in the bridge directory) so
that VM-side clients cannot modify it.

Default location: ~/.config/virtio-bridge/exec-policy.json

Policy format:
{
  "policies": [
    {
      "cmd": "git",
      "args_pattern": ["status", "log", "diff"],
      "level": "allow",
      "working_dirs": ["~/Documents/buddy-*"]
    },
    {
      "cmd": "git",
      "args_pattern": ["commit", "push", "add"],
      "level": "confirm",
      "working_dirs": ["~/Documents/buddy-*"]
    }
  ],
  "default": "deny"
}

Levels:
  - allow:   Execute without asking.
  - confirm: Show macOS dialog (osascript) and wait for human approval.
  - deny:    Reject immediately.
"""

import fnmatch
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("virtio-bridge.exec-policy")

# Single Source of Truth: the same data flows to display AND execution.
# There is NO separate "display_message" field in exec requests.
# The server renders the confirm dialog from the same cmd+args+cwd
# that it will pass to subprocess.run().

DEFAULT_POLICY_PATH = Path.home() / ".config" / "virtio-bridge" / "exec-policy.json"

VALID_LEVELS = {"allow", "confirm", "deny"}


@dataclass
class PolicyRule:
    """A single rule in the exec policy."""
    cmd: str
    args_pattern: List[str] = field(default_factory=list)
    level: str = "deny"
    working_dirs: List[str] = field(default_factory=list)

    def matches_cmd(self, cmd: str) -> bool:
        return self.cmd == cmd

    def matches_args(self, args: List[str]) -> bool:
        """Check if the command's subcommand matches the allowed patterns.

        The first positional (non-flag) argument is treated as the subcommand
        and must match at least one pattern.  Subsequent args (flags, values,
        commit messages) are not restricted — the security boundary is
        cmd + subcommand + working_dir, not free-text values.
        """
        if not self.args_pattern:
            # No pattern restriction = matches any args
            return True
        if not args:
            # No args provided but patterns exist — no match
            return False
        # Find the first positional arg (subcommand)
        for arg in args:
            if not arg.startswith("-"):
                return any(fnmatch.fnmatch(arg, p) for p in self.args_pattern)
        # All args are flags — check if any flag matches a pattern
        return any(
            any(fnmatch.fnmatch(a, p) for p in self.args_pattern)
            for a in args
        )

    def matches_cwd(self, cwd: str) -> bool:
        """Check if cwd falls within allowed working directories."""
        if not self.working_dirs:
            return True
        resolved = Path(cwd).expanduser().resolve()
        for pattern in self.working_dirs:
            expanded = Path(pattern).expanduser().resolve()
            pattern_str = str(expanded)
            # Support glob patterns in directory paths
            if fnmatch.fnmatch(str(resolved), pattern_str):
                return True
            # Also check if resolved is under the pattern directory
            if "*" not in pattern_str and str(resolved).startswith(pattern_str):
                return True
        return False


class ExecPolicy:
    """
    Loads and evaluates exec policy from a JSON file on the Mac side.

    The policy file MUST NOT be in the bridge directory (which is
    writable from the VM).  Default: ~/.config/virtio-bridge/exec-policy.json
    """

    def __init__(self, policy_path: Optional[str | Path] = None):
        self.policy_path = Path(policy_path) if policy_path else DEFAULT_POLICY_PATH
        self.rules: List[PolicyRule] = []
        self.default_level: str = "deny"
        self._loaded = False

    def load(self) -> None:
        """Load policy from file. Raises FileNotFoundError if not found."""
        if not self.policy_path.exists():
            raise FileNotFoundError(
                f"Exec policy not found: {self.policy_path}\n"
                f"Create it to enable remote command execution.\n"
                f"Without a policy file, all exec requests are denied."
            )

        # Security: reject if the policy file is a symlink
        if self.policy_path.is_symlink():
            raise PermissionError(
                f"Exec policy file is a symlink (rejected for security): {self.policy_path}"
            )

        with open(self.policy_path) as f:
            data = json.load(f)

        self.default_level = data.get("default", "deny")
        if self.default_level not in VALID_LEVELS:
            raise ValueError(f"Invalid default level: {self.default_level}")

        self.rules = []
        for rule_data in data.get("policies", []):
            level = rule_data.get("level", "deny")
            if level not in VALID_LEVELS:
                raise ValueError(f"Invalid level in policy rule: {level}")
            self.rules.append(PolicyRule(
                cmd=rule_data["cmd"],
                args_pattern=rule_data.get("args_pattern", []),
                level=level,
                working_dirs=rule_data.get("working_dirs", []),
            ))

        self._loaded = True
        logger.info(f"Loaded exec policy: {len(self.rules)} rules from {self.policy_path}")

    def check(self, cmd: str, args: List[str], cwd: str) -> str:
        """
        Evaluate a command against the policy.

        Returns: "allow", "confirm", or "deny"
        """
        if not self._loaded:
            self.load()

        # Path traversal defense: resolve cwd to absolute path
        resolved_cwd = str(Path(cwd).expanduser().resolve())
        if ".." in cwd:
            logger.warning(f"Path traversal detected in cwd: {cwd}")
            return "deny"

        # Find the first matching rule (order matters)
        for rule in self.rules:
            if rule.matches_cmd(cmd) and rule.matches_args(args) and rule.matches_cwd(resolved_cwd):
                logger.debug(f"Policy match: {cmd} {args} in {resolved_cwd} → {rule.level}")
                return rule.level

        logger.debug(f"No policy match: {cmd} {args} in {resolved_cwd} → {self.default_level}")
        return self.default_level


def osascript_confirm(cmd: str, args: List[str], cwd: str) -> bool:
    """
    Show a macOS confirmation dialog via osascript.

    Single Source of Truth: the displayed text is generated from the SAME
    cmd/args/cwd that will be passed to subprocess.run().  There is no
    separate "display" field that could diverge from the actual execution.

    Returns True if the user clicks "Allow", False otherwise.
    """
    # Build the display string from the exact execution parameters
    cmd_display = f"{cmd} {' '.join(args)}"
    cwd_display = cwd

    # Escape for AppleScript string (backslash and double-quote)
    def escape_applescript(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    msg = escape_applescript(f"Execute command:\\n\\n{cmd_display}\\n\\nin: {cwd_display}")

    script = (
        f'display dialog "{msg}" '
        f'buttons {{"Deny", "Allow"}} default button "Allow" '
        f'with title "virtio-bridge exec" '
        f'with icon caution '
        f'giving up after 60'
    )

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=70,
        )
        # osascript returns "button returned:Allow" on success
        approved = "Allow" in result.stdout
        if approved:
            logger.info(f"User APPROVED: {cmd_display}")
        else:
            logger.info(f"User DENIED: {cmd_display}")
        return approved
    except subprocess.TimeoutExpired:
        logger.warning(f"Confirm dialog timed out for: {cmd_display}")
        return False
    except FileNotFoundError:
        logger.error("osascript not found — not running on macOS?")
        return False
    except Exception as e:
        logger.error(f"osascript error: {e}")
        return False
