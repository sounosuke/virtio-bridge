"""
Security utilities for virtio-bridge.

Provides host allowlisting to restrict which hosts the bridge can connect to.
"""

import logging
from urllib.parse import urlparse

logger = logging.getLogger("virtio-bridge.security")

# Hosts considered "local" — default allow list
LOCAL_HOSTS = frozenset([
    "localhost",
    "127.0.0.1",
    "::1",
])


def parse_allow_hosts(value: str) -> frozenset[str]:
    """Parse comma-separated host list into a frozenset.

    Args:
        value: Comma-separated hostnames, e.g. "localhost,127.0.0.1,10.0.0.5"

    Returns:
        frozenset of normalized hostnames (lowercase, stripped)
    """
    hosts = set()
    for h in value.split(","):
        h = h.strip().lower()
        if h:
            hosts.add(h)
    return frozenset(hosts)


def is_host_allowed(host: str, allow_hosts: frozenset[str]) -> bool:
    """Check if a host is in the allow list.

    Args:
        host: The hostname or IP to check
        allow_hosts: Set of allowed hostnames

    Returns:
        True if allowed, False if blocked
    """
    return host.strip().lower() in allow_hosts


def validate_target_url(target: str, allow_hosts: frozenset[str]) -> None:
    """Validate that a --target URL points to an allowed host.

    Raises ValueError if the host is not allowed.
    """
    parsed = urlparse(target)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"Cannot parse host from target URL: {target}")
    if not is_host_allowed(host, allow_hosts):
        raise ValueError(
            f"Target host '{host}' is not in the allow list: {sorted(allow_hosts)}. "
            f"Use --allow-host to add it."
        )
