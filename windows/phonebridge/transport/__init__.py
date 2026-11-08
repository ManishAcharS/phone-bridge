"""Transport layer: how bytes get between the PC and the phone."""

from .discovery import (  # noqa: F401
    BEACON_MAGIC,
    DISCOVERY_PORT,
    DiscoveredPhone,
    DiscoveryService,
)
from .link import DEFAULT_PORT, LinkError, WebSocketLink, websocket_uri  # noqa: F401
