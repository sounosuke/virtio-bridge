"""
TCP relay server (runs on the host / Mac side).

Watches for TCP connection requests in the shared filesystem,
establishes real TCP connections to the target hosts, and relays
data bidirectionally through the filesystem.
"""

import logging
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .tcp_protocol import TcpBridgeDirectory, TcpConnection

logger = logging.getLogger("virtio-bridge.tcp-relay")


class TcpRelayHandler:
    """Handles a single TCP connection relay."""

    def __init__(self, conn: TcpConnection, target_sock: socket.socket):
        self.conn = conn
        self.target = target_sock

    def relay(self) -> None:
        """Run bidirectional relay. Blocks until connection closes."""
        # Thread: read from upstream file → write to target socket
        def upstream_pump():
            try:
                for chunk in self.conn.iter_upstream(timeout=60):
                    try:
                        self.target.sendall(chunk)
                    except (socket.error, OSError):
                        break
            finally:
                try:
                    self.target.shutdown(socket.SHUT_WR)
                except (socket.error, OSError):
                    pass

        # Thread: read from target socket → write to downstream file
        def downstream_pump():
            try:
                while True:
                    try:
                        data = self.target.recv(8192)
                    except (socket.error, OSError):
                        break
                    if not data:
                        break
                    self.conn.write_downstream(data)
            finally:
                self.conn.close_downstream()

        up_thread = threading.Thread(target=upstream_pump, daemon=True)
        down_thread = threading.Thread(target=downstream_pump, daemon=True)
        up_thread.start()
        down_thread.start()

        up_thread.join(timeout=120)
        down_thread.join(timeout=120)

        self.target.close()
        logger.info(f"Relay finished: {self.conn.conn_id}")


class TcpRelayServer:
    """
    Host-side TCP relay server.
    Watches for connection requests and establishes real TCP connections.
    """

    def __init__(self, bridge_dir: str | Path):
        self.tcp_bridge = TcpBridgeDirectory(bridge_dir)
        self._running = False
        self._active_conns: set[str] = set()

    def stop(self) -> None:
        self._running = False

    def _process_pending(self) -> None:
        """Process any pending connection requests."""
        pending = self.tcp_bridge.list_pending_connections()
        for conn_id in pending:
            if conn_id not in self._active_conns:
                self._active_conns.add(conn_id)
                self._handle_connection(conn_id)

    def _start_polling(self) -> None:
        """Poll for new connection requests."""
        while self._running:
            pending = self.tcp_bridge.list_pending_connections()
            for conn_id in pending:
                self._handle_connection(conn_id)
            time.sleep(0.05)  # 50ms poll interval

    def _handle_connection(self, conn_id: str) -> None:
        """Handle a new connection request in a thread."""
        t = threading.Thread(
            target=self._do_handle_connection,
            args=(conn_id,),
            daemon=True,
        )
        t.start()

    def _do_handle_connection(self, conn_id: str) -> None:
        """Establish real TCP connection and start relay."""
        conn = self.tcp_bridge.new_connection(conn_id)
        req = conn.read_connect_request()
        if req is None:
            logger.warning(f"Connect request disappeared: {conn_id}")
            return

        logger.info(f"→ TCP CONNECT {req.host}:{req.port} (conn={conn_id})")

        try:
            target_sock = socket.create_connection(
                (req.host, req.port),
                timeout=10,
            )
            target_sock.settimeout(None)  # Switch to blocking after connect
        except (socket.error, OSError) as e:
            logger.error(f"← {conn_id} Connection failed: {e}")
            conn.signal_error(str(e))
            return

        conn.signal_established()
        logger.info(f"← {conn_id} Connected to {req.host}:{req.port}")

        handler = TcpRelayHandler(conn, target_sock)
        handler.relay()

    def start(self) -> None:
        """Start the relay server using polling. Blocks until stopped."""
        self.tcp_bridge.init()

        removed = self.tcp_bridge.cleanup_stale(max_age=300)
        if removed:
            logger.info(f"Cleaned up {removed} stale connections")

        self._process_pending()

        self._running = True
        logger.info(f"TCP relay server started: watching {self.tcp_bridge.tcp_dir}")

        try:
            self._start_polling()
        except KeyboardInterrupt:
            logger.info("TCP relay interrupted")
        finally:
            self._running = False
            logger.info("TCP relay stopped")


def run_tcp_relay(bridge_dir: str) -> None:
    """Entry point for running the TCP relay server."""
    server = TcpRelayServer(bridge_dir=bridge_dir)

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    server.start()
