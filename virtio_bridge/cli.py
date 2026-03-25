"""
Command-line interface for virtio-bridge.

Usage:
    # Host side (Mac): watch shared dir and forward requests to localhost
    virtio-bridge server --target http://localhost:11434 --bridge-dir /path/to/shared/.bridge

    # VM side: start HTTP proxy that relays through filesystem
    virtio-bridge client --listen 127.0.0.1:11434 --bridge-dir /path/to/shared/.bridge

    # Quick test
    virtio-bridge test --bridge-dir /path/to/shared/.bridge
"""

import argparse
import json
import logging
import sys
import time

from . import __version__
from .protocol import BridgeDirectory, BridgeRequest, DEFAULT_TIMEOUT


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_server(args: argparse.Namespace) -> None:
    """Run the host-side server."""
    from .server import run_server
    setup_logging(args.verbose)
    run_server(
        bridge_dir=args.bridge_dir,
        target=args.target,
    )


def cmd_client(args: argparse.Namespace) -> None:
    """Run the VM-side client proxy."""
    from .client import run_client
    setup_logging(args.verbose)

    host, port = _parse_listen(args.listen)
    run_client(
        bridge_dir=args.bridge_dir,
        listen_host=host,
        listen_port=port,
        timeout=args.timeout,
    )


def cmd_test(args: argparse.Namespace) -> None:
    """Run a quick roundtrip test."""
    setup_logging(verbose=True)
    logger = logging.getLogger("virtio-bridge.test")

    bridge = BridgeDirectory(args.bridge_dir)
    bridge.init()

    # Write a test request
    req = BridgeRequest(
        id="",
        method="GET",
        path="/v1/models",
        headers={"Accept": "application/json"},
    )
    logger.info(f"Writing test request: {req.method} {req.path} (id={req.id})")
    bridge.write_request(req)

    # Wait for response
    logger.info(f"Waiting for response (timeout={args.timeout}s)...")
    resp = bridge.wait_response(req.id, timeout=args.timeout)

    if resp is None:
        logger.error("TIMEOUT: No response received.")
        logger.error("Make sure the server is running on the host side:")
        logger.error(f"  virtio-bridge server --target <url> --bridge-dir {args.bridge_dir}")
        sys.exit(1)

    logger.info(f"Response received: status={resp.status}")
    if resp.error:
        logger.error(f"Error: {resp.error}")
        sys.exit(1)

    if resp.body:
        try:
            body = json.loads(resp.body)
            logger.info(f"Body: {json.dumps(body, indent=2, ensure_ascii=False)[:500]}")
        except json.JSONDecodeError:
            logger.info(f"Body: {resp.body[:500]}")

    logger.info("Test PASSED")


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Clean up stale request/response files."""
    setup_logging(args.verbose)
    logger = logging.getLogger("virtio-bridge.cleanup")

    bridge = BridgeDirectory(args.bridge_dir)
    removed = bridge.cleanup_stale(max_age=args.max_age)
    logger.info(f"Removed {removed} stale files (max_age={args.max_age}s)")


def _parse_listen(listen: str) -> tuple[str, int]:
    """Parse 'host:port' string."""
    if ":" in listen:
        host, port_str = listen.rsplit(":", 1)
        return host, int(port_str)
    return "127.0.0.1", int(listen)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="virtio-bridge",
        description="HTTP relay over shared filesystem for VMs with restricted networking",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- server ---
    p_server = subparsers.add_parser(
        "server",
        help="Run on the host (Mac) side: watch for requests and forward to target",
    )
    p_server.add_argument(
        "--target", "-t",
        required=True,
        help="Target URL to forward requests to (e.g., http://localhost:11434)",
    )
    p_server.add_argument(
        "--bridge-dir", "-d",
        required=True,
        help="Path to the shared bridge directory",
    )
    p_server.add_argument("--verbose", "-v", action="store_true")
    p_server.set_defaults(func=cmd_server)

    # --- client ---
    p_client = subparsers.add_parser(
        "client",
        help="Run on the VM side: start HTTP proxy that relays through filesystem",
    )
    p_client.add_argument(
        "--listen", "-l",
        default="127.0.0.1:8080",
        help="Listen address (host:port or just port). Default: 127.0.0.1:8080",
    )
    p_client.add_argument(
        "--bridge-dir", "-d",
        required=True,
        help="Path to the shared bridge directory",
    )
    p_client.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Response timeout in seconds. Default: {DEFAULT_TIMEOUT}",
    )
    p_client.add_argument("--verbose", "-v", action="store_true")
    p_client.set_defaults(func=cmd_client)

    # --- test ---
    p_test = subparsers.add_parser(
        "test",
        help="Run a roundtrip test (write request, wait for response)",
    )
    p_test.add_argument(
        "--bridge-dir", "-d",
        required=True,
        help="Path to the shared bridge directory",
    )
    p_test.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Test timeout in seconds. Default: 10",
    )
    p_test.add_argument("--verbose", "-v", action="store_true")
    p_test.set_defaults(func=cmd_test)

    # --- cleanup ---
    p_cleanup = subparsers.add_parser(
        "cleanup",
        help="Clean up stale request/response files",
    )
    p_cleanup.add_argument(
        "--bridge-dir", "-d",
        required=True,
        help="Path to the shared bridge directory",
    )
    p_cleanup.add_argument(
        "--max-age",
        type=float,
        default=300.0,
        help="Max age in seconds for stale files. Default: 300",
    )
    p_cleanup.add_argument("--verbose", "-v", action="store_true")
    p_cleanup.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
