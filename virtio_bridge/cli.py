"""
Command-line interface for virtio-bridge.

Usage:
    # --- v1: HTTP relay ---
    # Host side (Mac): watch shared dir and forward requests to localhost
    virtio-bridge server --target http://localhost:11434 --bridge-dir /path/to/shared/.bridge

    # VM side: start HTTP proxy that relays through filesystem
    virtio-bridge client --listen 127.0.0.1:11434 --bridge-dir /path/to/shared/.bridge

    # --- v2: TCP relay (SOCKS5) ---
    # Host side (Mac): relay TCP connections to real targets
    virtio-bridge tcp-relay --bridge-dir /path/to/shared/.bridge

    # VM side: SOCKS5 proxy that relays TCP through filesystem
    virtio-bridge socks --listen 127.0.0.1:1080 --bridge-dir /path/to/shared/.bridge

    # Quick test
    virtio-bridge test --bridge-dir /path/to/shared/.bridge
"""

import argparse
import json
import logging
import sys
import time

from . import __version__
from .config import load_config, apply_config
from .protocol import BridgeDirectory, BridgeRequest, DEFAULT_TIMEOUT
from .security import parse_allow_hosts

ALLOW_HOST_DEFAULT = "localhost,127.0.0.1,::1"


def _make_crypto(secret: str | None):
    """Create BridgeCrypto from --secret flag. Returns None if not set."""
    if not secret:
        return None
    from .crypto import BridgeCrypto
    return BridgeCrypto(secret)


def _negotiate_dh(bridge_dir: str, role: str, timeout: float = 30.0):
    """Run DH key exchange. Returns BridgeCrypto on success."""
    from .crypto import DHKeyExchange
    dh = DHKeyExchange(bridge_dir, role=role)
    logger_dh = logging.getLogger("virtio-bridge")
    logger_dh.info(f"DH key exchange: waiting for peer ({role} side)...")
    crypto = dh.negotiate(timeout=timeout)
    return crypto, dh


def _resolve_crypto(args, role: str):
    """Resolve encryption mode from CLI args. Returns (crypto, dh) tuple.

    --secret and --auto-encrypt are mutually exclusive.
    Returns (None, None) if neither is set.
    """
    secret = getattr(args, "secret", None)
    auto_encrypt = getattr(args, "auto_encrypt", False)

    if secret and auto_encrypt:
        print("Error: --secret and --auto-encrypt are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    if secret:
        return _make_crypto(secret), None
    if auto_encrypt:
        return _negotiate_dh(args.bridge_dir, role=role)
    return None, None


def _apply_config_if_present(args: argparse.Namespace, section: str, defaults: dict) -> None:
    """Load and apply config file if --config was specified."""
    config_path = getattr(args, "config", None)
    if config_path:
        config = load_config(config_path, section)
        if config:
            apply_config(args, config, defaults)


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
    _apply_config_if_present(args, "server", {
        "target": None, "bridge_dir": None,
        "allow_host": ALLOW_HOST_DEFAULT, "verbose": False,
    })
    setup_logging(args.verbose)
    allow_hosts = parse_allow_hosts(args.allow_host)
    crypto, dh = _resolve_crypto(args, role="host")
    run_server(
        bridge_dir=args.bridge_dir,
        target=args.target,
        allow_hosts=allow_hosts,
        crypto=crypto,
    )


def cmd_client(args: argparse.Namespace) -> None:
    """Run the VM-side client proxy."""
    from .client import run_client
    _apply_config_if_present(args, "client", {
        "listen": "127.0.0.1:8080", "bridge_dir": None,
        "timeout": DEFAULT_TIMEOUT, "verbose": False,
    })
    setup_logging(args.verbose)

    host, port = _parse_listen(args.listen)
    crypto, dh = _resolve_crypto(args, role="vm")
    run_client(
        bridge_dir=args.bridge_dir,
        listen_host=host,
        listen_port=port,
        timeout=args.timeout,
        crypto=crypto,
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


def cmd_socks(args: argparse.Namespace) -> None:
    """Run the VM-side SOCKS5 proxy."""
    from .socks import run_socks
    _apply_config_if_present(args, "socks", {
        "listen": "127.0.0.1:1080", "bridge_dir": None, "verbose": False,
    })
    setup_logging(args.verbose)

    host, port = _parse_listen(args.listen)
    crypto, dh = _resolve_crypto(args, role="vm")
    run_socks(
        bridge_dir=args.bridge_dir,
        listen_host=host,
        listen_port=port,
        crypto=crypto,
    )


def cmd_tcp_relay(args: argparse.Namespace) -> None:
    """Run the host-side TCP relay."""
    from .tcp_relay import run_tcp_relay
    _apply_config_if_present(args, "tcp-relay", {
        "bridge_dir": None, "allow_host": ALLOW_HOST_DEFAULT, "verbose": False,
    })
    setup_logging(args.verbose)
    allow_hosts = parse_allow_hosts(args.allow_host)
    crypto, dh = _resolve_crypto(args, role="host")
    run_tcp_relay(bridge_dir=args.bridge_dir, allow_hosts=allow_hosts, crypto=crypto)


def cmd_integration_test(args: argparse.Namespace) -> None:
    """Run self-contained integration tests."""
    setup_logging(args.verbose)
    # Import here to avoid circular imports
    from tests.test_integration import run_integration_test
    success = run_integration_test()
    sys.exit(0 if success else 1)


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
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to TOML config file (e.g., bridge.toml). CLI flags override config values.",
    )

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
    p_server.add_argument(
        "--allow-host",
        default=ALLOW_HOST_DEFAULT,
        help=f"Comma-separated list of allowed target hosts. Default: {ALLOW_HOST_DEFAULT}",
    )
    p_server.add_argument(
        "--secret", "-s",
        default=None,
        help="Shared secret for AES-256-GCM encryption. Both sides must use the same secret.",
    )
    p_server.add_argument(
        "--auto-encrypt", "-e",
        action="store_true",
        default=False,
        help="Enable zero-config encryption via X25519 DH key exchange. "
             "No shared secret needed — keys are exchanged automatically via the bridge directory. "
             "Mutually exclusive with --secret.",
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
    p_client.add_argument(
        "--secret", "-s",
        default=None,
        help="Shared secret for AES-256-GCM encryption. Must match the server's secret.",
    )
    p_client.add_argument(
        "--auto-encrypt", "-e",
        action="store_true",
        default=False,
        help="Enable zero-config encryption via X25519 DH key exchange. "
             "Mutually exclusive with --secret.",
    )
    p_client.add_argument("--verbose", "-v", action="store_true")
    p_client.set_defaults(func=cmd_client)

    # --- socks (v2) ---
    p_socks = subparsers.add_parser(
        "socks",
        help="Run on the VM side: SOCKS5 proxy that relays TCP through filesystem",
    )
    p_socks.add_argument(
        "--listen", "-l",
        default="127.0.0.1:1080",
        help="Listen address (host:port or just port). Default: 127.0.0.1:1080",
    )
    p_socks.add_argument(
        "--bridge-dir", "-d",
        required=True,
        help="Path to the shared bridge directory",
    )
    p_socks.add_argument(
        "--secret", "-s",
        default=None,
        help="Shared secret for AES-256-GCM encryption. Must match the tcp-relay's secret.",
    )
    p_socks.add_argument(
        "--auto-encrypt", "-e",
        action="store_true",
        default=False,
        help="Enable zero-config encryption via X25519 DH key exchange. "
             "Mutually exclusive with --secret.",
    )
    p_socks.add_argument("--verbose", "-v", action="store_true")
    p_socks.set_defaults(func=cmd_socks)

    # --- tcp-relay (v2) ---
    p_tcp_relay = subparsers.add_parser(
        "tcp-relay",
        help="Run on the host (Mac) side: relay TCP connections to real targets",
    )
    p_tcp_relay.add_argument(
        "--bridge-dir", "-d",
        required=True,
        help="Path to the shared bridge directory",
    )
    p_tcp_relay.add_argument(
        "--allow-host",
        default=ALLOW_HOST_DEFAULT,
        help=f"Comma-separated list of allowed destination hosts. Default: {ALLOW_HOST_DEFAULT}",
    )
    p_tcp_relay.add_argument(
        "--secret", "-s",
        default=None,
        help="Shared secret for AES-256-GCM encryption. Must match the socks proxy's secret.",
    )
    p_tcp_relay.add_argument(
        "--auto-encrypt", "-e",
        action="store_true",
        default=False,
        help="Enable zero-config encryption via X25519 DH key exchange. "
             "Mutually exclusive with --secret.",
    )
    p_tcp_relay.add_argument("--verbose", "-v", action="store_true")
    p_tcp_relay.set_defaults(func=cmd_tcp_relay)

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

    # --- integration-test ---
    p_integ = subparsers.add_parser(
        "integration-test",
        help="Run self-contained integration tests (no VirtioFS or external services needed)",
    )
    p_integ.add_argument("--verbose", "-v", action="store_true")
    p_integ.set_defaults(func=cmd_integration_test)

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
