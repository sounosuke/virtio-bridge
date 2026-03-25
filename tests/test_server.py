"""Tests for the server-side path validation and security."""

import pytest

from virtio_bridge.server import _is_safe_path


class TestPathValidation:
    def test_valid_paths(self):
        assert _is_safe_path("/v1/models") is True
        assert _is_safe_path("/v1/chat/completions") is True
        assert _is_safe_path("/") is True
        assert _is_safe_path("/api/v2/users?page=1") is True

    def test_path_traversal_blocked(self):
        assert _is_safe_path("/../etc/passwd") is False
        assert _is_safe_path("/v1/../../secret") is False
        assert _is_safe_path("/..") is False

    def test_no_leading_slash_blocked(self):
        assert _is_safe_path("v1/models") is False
        assert _is_safe_path("") is False

    def test_null_bytes_blocked(self):
        assert _is_safe_path("/v1/models\x00evil") is False

    def test_none_blocked(self):
        assert _is_safe_path(None) is False
