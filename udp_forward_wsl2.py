#!/usr/bin/env python3
"""
UDP forwarder: Windows adapter <-> WSL2
- Forwards LiDAR UDP packets arriving on Windows to WSL2.
- Optionally forwards responses from WSL2 back to the last LiDAR sender.

Usage examples:
  python udp_forward_wsl2.py --listen-ip 192.168.198.1 --ports 2368 2369
  python udp_forward_wsl2.py --listen-ip 0.0.0.0 --ports 2368 --bidirectional
  python udp_forward_wsl2.py --listen-ip 192.168.198.1 --wsl2-ip 172.20.153.45 --ports 2368

Notes:
- No admin/power rules required (user-space proxy).
- For high-rate LiDAR streams, keep this console focused and avoid heavy logging.
"""

import argparse
import ipaddress
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, Optional, Tuple, List

BUFFER_BYTES = 65535         # Max UDP datagram size
RECVBUF_BYTES = 4 * 1024**2  # 4MB socket receive buffer for bursty traffic
SENDBUF_BYTES = 4 * 1024**2  # 4MB send buffer


def detect_wsl2_ip() -> Optional[str]:
    """Return first IPv4 from `wsl.exe hostname -I`, or None if not found."""
    try:
        out = subprocess.check_output(
            ["wsl.exe", "hostname", "-I"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3.0,
        )
    except Exception:
        return None
    candidates = [t.strip() for t in out.split() if t.strip()]
    for tok in candidates:
        try:
            ip = ipaddress.ip_address(tok)
            if isinstance(ip, ipaddress.IPv4Address):
                return tok
        except Exception:
            pass
    return None


def make_udp_socket(bind_ip: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECVBUF_BYTES)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SENDBUF_BYTES)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind_ip, port))
    return s


class UdpForwarder(threading.Thread):
    """
    One forwarder per UDP port.

    Flow:
      (Windows listen_ip:port) <-> this script <-> (WSL2_ip:port)

    If bidirectional=True:
      - Packets from WSL2_ip:port are forwarded back to the last sender
        observed on the Windows side for the same port.
    """

    def __init__(
        self,
        listen_ip: str,
        port: int,
        wsl2_ip: str,
        bidirectional: bool = False,
        lidar_subnet: Optional[str] = None,
        quiet: bool = False,
    ):
        super().__init__(daemon=True)
        self.listen_ip = listen_ip
        self.port = port
        self.wsl2_ip = wsl2_ip
        self.bidirectional = bidirectional
        self.quiet = quiet
        self.sock = make_udp_socket(self.listen_ip, self.port)
        self.stop_event = threading.Event()
        self.last_lidar_sender: Optional[Tuple[str, int]] = None
        self.lidar_network = (
            ipaddress.ip_network(lidar_subnet, strict=False) if lidar_subnet else None
        )

        # Separate sending socket (optional but helps avoid contention)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SENDBUF_BYTES)

    def log(self, msg: str):
        if not self.quiet:
            print(f"[{self.port}] {msg}")

    def addr_in_lidar_subnet(self, addr_ip: str) -> bool:
        if not self.lidar_network:
            return True  # accept all if not constrained
        try:
            return ipaddress.ip_address(addr_ip) in self.lidar_network
        except Exception:
            return False

    def run(self):
        self.log(
            f"Listening on {self.listen_ip}:{self.port} → forwarding to {self.wsl2_ip}:{self.port} "
            f"{'(bidirectional)' if self.bidirectional else ''}"
        )
        if self.lidar_network:
            self.log(f"Restricting LiDAR source to subnet: {self.lidar_network}")

        self.sock.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                data, src = self.sock.recvfrom(BUFFER_BYTES)
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed

            src_ip, src_port = src

            # Heuristic: if packet originates from WSL2, forward to last LiDAR sender (bidirectional path)
            if self.bidirectional and src_ip == self.wsl2_ip:
                if self.last_lidar_sender:
                    try:
                        self.tx.sendto(data, self.last_lidar_sender)
                    except Exception as e:
                        self.log(f"Error sending to LiDAR {self.last_lidar_sender}: {e}")
                # else: no known LiDAR peer yet
                continue

            # Windows-side packet (likely from LiDAR or test tool) → forward to WSL2
            if not self.addr_in_lidar_subnet(src_ip):
                # Ignore stray traffic outside expected subnet if one was specified
                continue

            self.last_lidar_sender = (src_ip, src_port)
            try:
                self.tx.sendto(data, (self.wsl2_ip, self.port))
            except Exception as e:
                self.log(f"Error sending to WSL2 {self.wsl2_ip}:{self.port}: {e}")

        self.sock.close()
        self.tx.close()
        self.log("Stopped.")

    def stop(self):
        self.stop_event.set()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UDP forwarder: Windows <-> WSL2")
    p.add_argument(
        "--listen-ip",
        required=True,
        help="Windows adapter IP to bind (e.g., 192.168.198.1). Use 0.0.0.0 to listen on all.",
    )
    p.add_argument(
        "--ports",
        type=int,
        nargs="+",
        required=True,
        help="UDP ports to forward (e.g., 2368 2369).",
    )
    p.add_argument(
        "--wsl2-ip",
        default=None,
        help="Override WSL2 IPv4 (otherwise auto-detected via `wsl hostname -I`).",
    )
    p.add_argument(
        "--lidar-subnet",
        default=None,
        help="Optional CIDR (e.g., 192.168.198.0/24) to accept only LiDAR packets from this subnet.",
    )
    p.add_argument(
        "--bidirectional",
        action="store_true",
        help="Also forward packets from WSL2 back to the last LiDAR sender for each port.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce console output.",
    )
    return p.parse_args(argv)


def main():
    args = parse_args()

    wsl_ip = args.wsl2_ip or detect_wsl2_ip()
    if not wsl_ip:
        print("ERROR: Could not detect WSL2 IPv4. Start a WSL2 distro or pass --wsl2-ip.", file=sys.stderr)
        sys.exit(1)

    forwarders: List[UdpForwarder] = []
    try:
        for port in args.ports:
            fwd = UdpForwarder(
                listen_ip=args.listen_ip,
                port=port,
                wsl2_ip=wsl_ip,
                bidirectional=args.bidirectional,
                lidar_subnet=args.lidar_subnet,
                quiet=args.quiet,
            )
            fwd.start()
            forwarders.append(fwd)

        print("Press Ctrl+C to stop.")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        for f in forwarders:
            f.stop()
        for f in forwarders:
            f.join()


if __name__ == "__main__":
    main()
