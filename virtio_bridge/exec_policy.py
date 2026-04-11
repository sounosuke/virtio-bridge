"""
Exec policy — Predefined Actions with template-based command construction.

Security model:
  - The client can ONLY request predefined action names + parameters.
  - The client CANNOT construct arbitrary commands.
  - The server expands action templates into commands using parameters.
  - The policy file lives on the Mac side (NOT in the bridge directory).
  - Only the human can add/modify action definitions.

Default location: ~/.config/virtio-bridge/exec-policy.json

Policy format:
{
  "actions": {
    "git_status": {
      "cmd": ["git", "status"],
      "level": "allow",
      "working_dirs": ["~/Documents/buddy-*"]
    },
    "git_commit": {
      "cmd": ["git", "commit", "-m", "{message}"],
      "level": "allow",
      "working_dirs": ["~/Documents/buddy-*"],
      "params": {
        "message": {"type": "string", "max_length": 500}
      }
    }
  }
}

Levels:
  - allow:   Execute without asking.
  - confirm: Show macOS dialog (osascript) and wait for human approval.
  - deny:    Reject immediately (useful to explicitly block an action).
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("virtio-bridge.exec-policy")

DEFAULT_POLICY_PATH = Path.home() / ".config" / "virtio-bridge" / "exec-policy.json"

VALID_LEVELS = {"allow", "confirm", "deny"}


@dataclass
class ActionTemplate:
    """A predefined action that the server knows how to execute."""
    name: str
    cmd: List[str]  # Command template, e.g. ["git", "commit", "-m", "{message}"]
    level: str = "allow"
    working_dirs: List[str] = field(default_factory=list)
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    description: str = ""  # Human-readable description for confirm dialog

    def validate_params(self, provided: Dict[str, str]) -> Optional[str]:
        """Validate provided parameters against the template spec.

        Returns None on success, or an error message string.
        """
        # Check for required params (any param in cmd template is required)
        for part in self.cmd:
            if part.startswith("{") and part.endswith("}"):
                param_name = part[1:-1]
                if param_name not in provided:
                    return f"Missing required parameter: {param_name}"

        # Validate each provided param
        for name, value in provided.items():
            if name not in self.params and not self._is_template_param(name):
                return f"Unknown parameter: {name}"

            spec = self.params.get(name, {})

            # Type check
            expected_type = spec.get("type", "string")
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter {name} must be a string"

            # Length check
            max_length = spec.get("max_length")
            if max_length and isinstance(value, str) and len(value) > max_length:
                return f"Parameter {name} exceeds max_length ({max_length})"

            # Enum check
            allowed_values = spec.get("enum")
            if allowed_values and value not in allowed_values:
                return f"Parameter {name} must be one of: {allowed_values}"

        return None

    def _is_template_param(self, name: str) -> bool:
        """Check if a param name appears in the cmd template."""
        return f"{{{name}}}" in " ".join(self.cmd)

    def build_command(self, params: Dict[str, str]) -> List[str]:
        """Expand the command template with provided parameters.

        Returns a list of strings ready for subprocess.run().
        No shell expansion — parameters are inserted as literal values.

        If a parameter spec has ``"split": true``, the value is split by
        whitespace and inserted as multiple arguments.  This is useful for
        commands like ``git add {paths}`` where paths is "file1 file2".
        """
        result = []
        for part in self.cmd:
            if part.startswith("{") and part.endswith("}"):
                param_name = part[1:-1]
                value = params[param_name]
                spec = self.params.get(param_name, {})
                if spec.get("split"):
                    result.extend(value.split())
                else:
                    result.append(value)
            else:
                result.append(part)
        return result

    def matches_cwd(self, cwd: str) -> bool:
        """Check if cwd falls within allowed working directories."""
        if not self.working_dirs:
            return True
        resolved = str(Path(cwd).expanduser().resolve())
        for pattern in self.working_dirs:
            import fnmatch
            expanded = str(Path(pattern).expanduser().resolve())
            if fnmatch.fnmatch(resolved, expanded):
                return True
            if "*" not in expanded and resolved.startswith(expanded):
                return True
        return False


class ExecPolicy:
    """
    Predefined Actions policy loader.

    The client sends an action name + parameters. The server looks up the
    action in the policy, validates parameters, builds the command from
    the template, and executes it. The client never constructs commands.
    """

    def __init__(self, policy_path: Optional[str | Path] = None):
        self.policy_path = Path(policy_path) if policy_path else DEFAULT_POLICY_PATH
        self.actions: Dict[str, ActionTemplate] = {}
        self._loaded = False

    def load(self) -> None:
        """Load policy from file. Raises FileNotFoundError if not found."""
        if not self.policy_path.exists():
            raise FileNotFoundError(
                f"Exec policy not found: {self.policy_path}\n"
                f"Create it to enable remote command execution.\n"
                f"Without a policy file, all exec requests are denied."
            )

        if self.policy_path.is_symlink():
            raise PermissionError(
                f"Exec policy file is a symlink (rejected for security): {self.policy_path}"
            )

        with open(self.policy_path) as f:
            data = json.load(f)

        self.actions = {}
        for name, action_data in data.get("actions", {}).items():
            level = action_data.get("level", "allow")
            if level not in VALID_LEVELS:
                raise ValueError(f"Invalid level for action '{name}': {level}")

            cmd = action_data.get("cmd", [])
            if not cmd:
                raise ValueError(f"Action '{name}' has no cmd")

            self.actions[name] = ActionTemplate(
                name=name,
                cmd=cmd,
                level=level,
                working_dirs=action_data.get("working_dirs", []),
                params=action_data.get("params", {}),
                description=action_data.get("description", ""),
            )

        self._loaded = True
        logger.info(
            f"Loaded exec policy: {len(self.actions)} actions from {self.policy_path}"
        )

    def resolve(self, action: str, params: Dict[str, str], cwd: str):
        """
        Resolve an action request into a validated, ready-to-execute command.

        Returns: (cmd_list, level, action_template) on success.
        Raises ValueError on validation failure.
        """
        if not self._loaded:
            self.load()

        # Action must exist
        template = self.actions.get(action)
        if template is None:
            available = ", ".join(sorted(self.actions.keys()))
            raise ValueError(
                f"Unknown action: '{action}'. Available: {available}"
            )

        # Level check
        if template.level == "deny":
            raise ValueError(f"Action '{action}' is explicitly denied by policy")

        # CWD check
        resolved_cwd = str(Path(cwd).expanduser().resolve())
        if ".." in cwd:
            raise ValueError(f"Path traversal detected in cwd: {cwd}")
        if not template.matches_cwd(resolved_cwd):
            raise ValueError(
                f"Action '{action}' not allowed in directory: {resolved_cwd}"
            )

        # Parameter validation
        error = template.validate_params(params)
        if error:
            raise ValueError(f"Parameter error for '{action}': {error}")

        # Build command from template
        cmd_list = template.build_command(params)

        return cmd_list, template.level, template

    def list_actions(self) -> Dict[str, dict]:
        """List available actions with their descriptions. For client discovery."""
        if not self._loaded:
            self.load()
        result = {}
        for name, tmpl in self.actions.items():
            result[name] = {
                "description": tmpl.description,
                "level": tmpl.level,
                "params": list(tmpl.params.keys()),
                "working_dirs": tmpl.working_dirs,
            }
        return result


def osascript_confirm(cmd_list: List[str], cwd: str, description: str = "") -> bool:
    """
    Show a macOS confirmation dialog via osascript.

    Single Source of Truth: the displayed text is generated from the SAME
    cmd_list/cwd that will be passed to subprocess.run().

    Returns True if the user clicks "Allow", False otherwise.
    """
    cmd_display = " ".join(cmd_list)
    cwd_display = cwd

    def escape_applescript(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    if description:
        msg = escape_applescript(
            f"{description}\\n\\n{cmd_display}\\n\\nin: {cwd_display}"
        )
    else:
        msg = escape_applescript(
            f"Execute command:\\n\\n{cmd_display}\\n\\nin: {cwd_display}"
        )

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
