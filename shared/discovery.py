"""
GameStream mDNS Service Discovery.
Requires: pip install zeroconf
"""
import socket
from typing import Optional, Callable

try:
    from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser, ServiceStateChange
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False
    print("  [!] zeroconf not installed -- mDNS discovery disabled (pip install zeroconf)")

SERVICE_TYPE = "_gamestream._tcp.local."


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class ServiceAnnouncer:
    """Register this host on the LAN so clients can find it without typing IP."""

    def __init__(self, name: str, port: int, properties: dict = None):
        self.name = name
        self.port = port
        self.properties = properties or {}
        self._zeroconf: Optional["Zeroconf"] = None
        self._info: Optional["ServiceInfo"] = None

    def start(self):
        if not HAS_ZEROCONF:
            return
        try:
            ip = _local_ip()
            # Encode properties as bytes
            props = {
                k: str(v).encode() for k, v in self.properties.items()
            }
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.name}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip)],
                port=self.port,
                properties=props,
                server=f"{socket.gethostname()}.local.",
            )
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(self._info)
            print(f"  📡  mDNS announced: {self.name} on {ip}:{self.port}")
        except Exception as e:
            print(f"  ⚠️  mDNS announce failed: {e}")

    def stop(self):
        if not HAS_ZEROCONF or self._zeroconf is None:
            return
        try:
            if self._info:
                self._zeroconf.unregister_service(self._info)
            self._zeroconf.close()
        except Exception:
            pass
        self._zeroconf = None
        self._info = None


class ServiceDiscovery:
    """Browse for GameStream hosts. on_found(name, ip, port) called for each."""

    def __init__(self, on_found: Callable[[str, str, int], None]):
        self.on_found = on_found
        self._zeroconf: Optional["Zeroconf"] = None
        self._browser = None

    def _on_service_state_change(self, zeroconf: "Zeroconf", service_type: str,
                                  name: str, state_change: "ServiceStateChange"):
        if state_change is ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                display_name = name.replace(f".{SERVICE_TYPE}", "")
                try:
                    self.on_found(display_name, ip, port)
                except Exception:
                    pass

    def start(self):
        if not HAS_ZEROCONF:
            return
        try:
            self._zeroconf = Zeroconf()
            self._browser = ServiceBrowser(
                self._zeroconf,
                SERVICE_TYPE,
                handlers=[self._on_service_state_change],
            )
        except Exception as e:
            print(f"  ⚠️  mDNS discovery failed: {e}")

    def stop(self):
        if not HAS_ZEROCONF or self._zeroconf is None:
            return
        try:
            self._zeroconf.close()
        except Exception:
            pass
        self._zeroconf = None
        self._browser = None
