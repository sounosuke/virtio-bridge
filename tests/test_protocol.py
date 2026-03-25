"""Tests for the HTTP protocol layer."""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from virtio_bridge.protocol import (
    BridgeDirectory,
    BridgeRequest,
    BridgeResponse,
)


@pytest.fixture
def bridge(tmp_path):
    """Create a BridgeDirectory in a temp directory."""
    bd = BridgeDirectory(tmp_path / ".bridge")
    bd.init()
    return bd


class TestBridgeRequest:
    def test_auto_id(self):
        req = BridgeRequest(id="", method="GET", path="/test")
        assert req.id != ""
        assert len(req.id) == 12

    def test_auto_timestamp(self):
        before = time.time()
        req = BridgeRequest(id="", method="GET", path="/test")
        after = time.time()
        assert before <= req.timestamp <= after

    def test_json_roundtrip(self):
        req = BridgeRequest(
            id="abc123",
            method="POST",
            path="/v1/chat",
            headers={"Content-Type": "application/json"},
            body='{"test": true}',
            stream=True,
        )
        json_str = req.to_json()
        req2 = BridgeRequest.from_json(json_str)
        assert req2.id == "abc123"
        assert req2.method == "POST"
        assert req2.path == "/v1/chat"
        assert req2.headers == {"Content-Type": "application/json"}
        assert req2.body == '{"test": true}'
        assert req2.stream is True


class TestBridgeResponse:
    def test_json_roundtrip(self):
        resp = BridgeResponse(
            id="abc123",
            status=200,
            headers={"Content-Type": "application/json"},
            body='{"result": "ok"}',
        )
        json_str = resp.to_json()
        resp2 = BridgeResponse.from_json(json_str)
        assert resp2.id == "abc123"
        assert resp2.status == 200
        assert resp2.body == '{"result": "ok"}'

    def test_error_response(self):
        resp = BridgeResponse(id="err1", status=502, error="Connection refused")
        json_str = resp.to_json()
        resp2 = BridgeResponse.from_json(json_str)
        assert resp2.error == "Connection refused"
        assert resp2.body is None


class TestBridgeDirectory:
    def test_init_creates_dirs(self, bridge):
        assert bridge.requests_dir.exists()
        assert bridge.responses_dir.exists()

    def test_write_read_request(self, bridge):
        req = BridgeRequest(id="test1", method="GET", path="/models")
        bridge.write_request(req)
        read_req = bridge.read_request("test1")
        assert read_req is not None
        assert read_req.method == "GET"
        assert read_req.path == "/models"

    def test_consume_request(self, bridge):
        req = BridgeRequest(id="test2", method="POST", path="/chat")
        bridge.write_request(req)

        consumed = bridge.consume_request("test2")
        assert consumed is not None
        assert consumed.id == "test2"

        # Should be gone after consume
        assert bridge.read_request("test2") is None

    def test_list_request_ids(self, bridge):
        for i in range(3):
            req = BridgeRequest(id=f"req{i}", method="GET", path=f"/test{i}")
            bridge.write_request(req)
            time.sleep(0.01)  # Ensure different mtimes

        ids = bridge.list_request_ids()
        assert len(ids) == 3
        assert ids == ["req0", "req1", "req2"]

    def test_write_read_response(self, bridge):
        resp = BridgeResponse(id="resp1", status=200, body='{"ok": true}')
        bridge.write_response(resp)
        read_resp = bridge.read_response("resp1")
        assert read_resp is not None
        assert read_resp.status == 200

    def test_wait_response(self, bridge):
        """Test that wait_response returns when response file appears."""
        import threading

        def write_delayed():
            time.sleep(0.2)
            resp = BridgeResponse(id="wait1", status=200, body="ok")
            bridge.write_response(resp)

        t = threading.Thread(target=write_delayed)
        t.start()
        result = bridge.wait_response("wait1", timeout=5.0)
        t.join()
        assert result is not None
        assert result.status == 200

    def test_wait_response_timeout(self, bridge):
        result = bridge.wait_response("nonexistent", timeout=0.2)
        assert result is None

    def test_read_nonexistent_request(self, bridge):
        assert bridge.read_request("nope") is None

    def test_read_nonexistent_response(self, bridge):
        assert bridge.read_response("nope") is None

    def test_cleanup_stale(self, bridge):
        req = BridgeRequest(id="old1", method="GET", path="/old")
        bridge.write_request(req)

        # Should not clean up recent files
        removed = bridge.cleanup_stale(max_age=300)
        assert removed == 0

        # Should clean up with 0 max_age
        removed = bridge.cleanup_stale(max_age=0)
        assert removed == 1


class TestSecurityChecks:
    def test_symlink_rejected_in_read_request(self, bridge):
        """Symlink files should be rejected."""
        # Create a real file
        target = bridge.requests_dir / "real.txt"
        target.write_text("not a request")

        # Create symlink masquerading as a request
        link = bridge.requests_dir / "symlink.json"
        link.symlink_to(target)

        result = bridge.read_request("symlink")
        assert result is None

    def test_symlink_rejected_in_read_response(self, bridge):
        target = bridge.responses_dir / "real.txt"
        target.write_text("not a response")

        link = bridge.responses_dir / "symlink.json"
        link.symlink_to(target)

        result = bridge.read_response("symlink")
        assert result is None

    def test_malformed_json_rejected(self, bridge):
        """Malformed JSON should return None, not crash."""
        path = bridge.requests_dir / "bad.json"
        path.write_text("not json at all")
        assert bridge.read_request("bad") is None

    def test_wrong_fields_rejected(self, bridge):
        """JSON with wrong fields should return None."""
        path = bridge.requests_dir / "wrong.json"
        path.write_text('{"foo": "bar"}')
        assert bridge.read_request("wrong") is None


class TestStreaming:
    def test_stream_write_read(self, bridge):
        bridge.append_stream("s1", b"chunk1\n")
        bridge.append_stream("s1", b"chunk2\n")
        bridge.finish_stream("s1", status=200)

        chunks = list(bridge.read_stream("s1", timeout=2.0))
        combined = b"".join(chunks)
        assert b"chunk1\n" in combined
        assert b"chunk2\n" in combined

    def test_stream_timeout(self, bridge):
        # No stream data, should timeout quickly
        chunks = list(bridge.read_stream("nostream", timeout=0.3))
        assert chunks == []
