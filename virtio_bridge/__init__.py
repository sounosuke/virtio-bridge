"""virtio-bridge: HTTP relay over shared filesystem for VMs with restricted networking."""

__version__ = "0.7.0"

# Direct (no-listen) clients — the primary API for VM-side use
from .direct import DirectClient, DirectTcpClient

__all__ = ["DirectClient", "DirectTcpClient", "__version__"]
