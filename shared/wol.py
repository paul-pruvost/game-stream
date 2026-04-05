"""Send a Wake-on-LAN magic packet to wake up a sleeping PC."""
import re
import socket


def send_magic_packet(mac: str, broadcast: str = "<broadcast>", port: int = 9):
    """
    Send a Wake-on-LAN magic packet.

    mac = 'AA:BB:CC:DD:EE:FF' or 'AA-BB-CC-DD-EE-FF'
    broadcast = broadcast address (default '<broadcast>' for LAN)
    port = WoL port (9 is standard; 7 also common)
    """
    # Normalize MAC address: remove separators, uppercase
    mac_clean = re.sub(r"[:\-\s]", "", mac).upper()
    if len(mac_clean) != 12 or not re.fullmatch(r"[0-9A-F]{12}", mac_clean):
        raise ValueError(f"Invalid MAC address: {mac!r}")

    # Convert hex string to bytes
    mac_bytes = bytes.fromhex(mac_clean)

    # Magic packet: 6 bytes of 0xFF + MAC repeated 16 times = 102 bytes
    magic = b"\xFF" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.connect_ex((broadcast, port))
        sock.send(magic)

    print(f"  💤  WoL magic packet sent to {mac} (broadcast={broadcast}:{port})")
