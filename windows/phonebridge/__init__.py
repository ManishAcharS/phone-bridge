"""PhoneBridge Windows/PC side.

Release 1.0.0 scope: protocol, transport, pairing, encrypted session, CLI.
Audio/camera/virtual-device modules arrive in later phases; nothing in this
package imports them yet, so an install stays small and dependency-light.
"""

__version__ = "1.0.0"
PROTOCOL_VERSION = 1
