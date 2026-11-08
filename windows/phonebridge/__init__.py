"""PhoneBridge Windows/PC side.

Phase 1 scope: protocol, transport, pairing, encrypted session, CLI.
Audio/camera/virtual-device modules arrive in Phases 2-4; nothing in this
package imports them yet, so a Phase-1 install stays small.
"""

__version__ = "0.1.0"
PROTOCOL_VERSION = 1
