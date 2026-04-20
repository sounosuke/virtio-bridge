"""Tests for virtio_bridge.exec_policy.

Focus: template substitution in build_command() and the matching
validate_params() checks. These are regression tests for the bug where
embedded placeholders like ``"gui/501/{service}"`` were passed to
subprocess as a literal string instead of being substituted, causing
``launchctl kickstart`` to fail with ``Could not find service "{service}"``.
"""

import pytest

from virtio_bridge.exec_policy import ActionTemplate


class TestBuildCommand:
    def test_whole_element_placeholder(self):
        """The original supported case: cmd element is exactly ``{name}``."""
        t = ActionTemplate(
            name="service_status",
            cmd=["launchctl", "list", "{service}"],
            params={"service": {"type": "string"}},
        )
        assert t.build_command({"service": "com.buddy.memory-api"}) == [
            "launchctl",
            "list",
            "com.buddy.memory-api",
        ]

    def test_embedded_placeholder(self):
        """Regression: embedded placeholders must be substituted in place."""
        t = ActionTemplate(
            name="service_restart",
            cmd=["launchctl", "kickstart", "-k", "gui/501/{service}"],
            params={"service": {"type": "string"}},
        )
        assert t.build_command({"service": "com.buddy.memory-api"}) == [
            "launchctl",
            "kickstart",
            "-k",
            "gui/501/com.buddy.memory-api",
        ]

    def test_embedded_in_shell_pipeline(self):
        """ps_grep-style: placeholder inside a bash -c string."""
        t = ActionTemplate(
            name="ps_grep",
            cmd=["bash", "-c", "ps aux | grep {pattern} | grep -v grep"],
            params={"pattern": {"type": "string"}},
        )
        assert t.build_command({"pattern": "memory_api"}) == [
            "bash",
            "-c",
            "ps aux | grep memory_api | grep -v grep",
        ]

    def test_multiple_placeholders_in_single_element(self):
        t = ActionTemplate(
            name="compose",
            cmd=["bash", "-c", "{cmd1} && {cmd2}"],
            params={"cmd1": {"type": "string"}, "cmd2": {"type": "string"}},
        )
        assert t.build_command({"cmd1": "echo a", "cmd2": "echo b"}) == [
            "bash",
            "-c",
            "echo a && echo b",
        ]

    def test_split_on_whole_element(self):
        """split=true on a whole-element placeholder splits by whitespace."""
        t = ActionTemplate(
            name="git_add",
            cmd=["git", "add", "{paths}"],
            params={"paths": {"type": "string", "split": True}},
        )
        assert t.build_command({"paths": "a b c"}) == ["git", "add", "a", "b", "c"]

    def test_no_placeholders(self):
        t = ActionTemplate(name="ls", cmd=["ls"])
        assert t.build_command({}) == ["ls"]

    def test_same_param_used_twice(self):
        t = ActionTemplate(
            name="repeat",
            cmd=["echo", "{x}-{x}"],
            params={"x": {"type": "string"}},
        )
        assert t.build_command({"x": "ab"}) == ["echo", "ab-ab"]


class TestValidateParams:
    def test_missing_whole_element_param(self):
        t = ActionTemplate(
            name="f",
            cmd=["echo", "{x}"],
            params={"x": {"type": "string"}},
        )
        assert "Missing required parameter: x" in (t.validate_params({}) or "")

    def test_missing_embedded_param(self):
        """Regression: embedded params must also be marked required."""
        t = ActionTemplate(
            name="f",
            cmd=["echo", "prefix-{x}-suffix"],
            params={"x": {"type": "string"}},
        )
        assert "Missing required parameter: x" in (t.validate_params({}) or "")

    def test_unknown_param_rejected(self):
        t = ActionTemplate(
            name="f",
            cmd=["echo", "{x}"],
            params={"x": {"type": "string"}},
        )
        err = t.validate_params({"x": "ok", "y": "bad"})
        assert err is not None and "Unknown parameter: y" in err

    def test_max_length(self):
        t = ActionTemplate(
            name="f",
            cmd=["echo", "{msg}"],
            params={"msg": {"type": "string", "max_length": 3}},
        )
        assert t.validate_params({"msg": "abc"}) is None
        err = t.validate_params({"msg": "abcd"})
        assert err is not None and "max_length" in err

    def test_enum(self):
        t = ActionTemplate(
            name="f",
            cmd=["echo", "{color}"],
            params={"color": {"type": "string", "enum": ["red", "blue"]}},
        )
        assert t.validate_params({"color": "red"}) is None
        err = t.validate_params({"color": "green"})
        assert err is not None and "must be one of" in err

    def test_split_with_embedded_is_rejected(self):
        """split only makes sense for whole-element placeholders."""
        t = ActionTemplate(
            name="f",
            cmd=["echo", "pre-{x}-post"],
            params={"x": {"type": "string", "split": True}},
        )
        err = t.validate_params({"x": "a b"})
        assert err is not None and "split" in err

    def test_all_referenced_params_discovered(self):
        """Both whole-element and embedded refs count as required."""
        t = ActionTemplate(
            name="f",
            cmd=["tool", "{a}", "prefix-{b}", "{c}-suffix"],
            params={
                "a": {"type": "string"},
                "b": {"type": "string"},
                "c": {"type": "string"},
            },
        )
        assert t.validate_params({"a": "1", "b": "2", "c": "3"}) is None
        err = t.validate_params({"a": "1", "b": "2"})
        assert err is not None and "Missing required parameter: c" in err
