#!/usr/bin/env python3
"""Restricted CONNECT proxy for a QEMU isolated CI guest.

Usage:
  python3 proxy.py
  python3 proxy.py --listen 127.0.0.1 --port 18080

QEMU user networking (keep `restrict=on`):
  -netdev user,id=isolated,restrict=on,guestfwd=tcp:10.0.2.100:18080-cmd:/usr/bin/nc 127.0.0.1 18080

This is not a general proxy. It permits only HTTPS CONNECT to allowlisted DNS
names on TCP/443. DNS is queried over TLS to the pinned public DoH IP and the
outbound socket is connected to the exact vetted A record returned by that
query. TLS between guest and destination remains opaque to this proxy.
"""

import argparse
import ipaddress
import http.client
import io
import json
import socket
import socketserver
import ssl
import threading
import urllib.parse

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18080
DOH_IP = "1.1.1.1"
DOH_HOST = "cloudflare-dns.com"
DOH_PATH = "/dns-query"
CONNECT_TIMEOUT = 10.0
CLIENT_TIMEOUT = 20.0
MAX_HEADER_BYTES = 16 * 1024
MAX_DOH_BYTES = 64 * 1024
MAX_CONCURRENT_CLIENTS = 32

# These suffixes include their leading dot deliberately: "notgithub.com" does
# not match ".github.com". Add domains only when the guest actually needs them.
ALLOWED_EXACT_HOSTS = frozenset({
    "github.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
    "nodejs.org", "ports.ubuntu.com",
})
ALLOWED_SUFFIXES = (
    ".github.com", ".githubusercontent.com", ".githubassets.com",
    ".githubapp.com", ".actions.githubusercontent.com", ".actions.github.com",
    ".blob.core.windows.net",  # GitHub logs/artifacts; DNS must still be public
)


class ProxyError(Exception):
    """A client-visible request failure."""


def normalize_hostname(value):
    """Return a strict ASCII DNS hostname, rejecting literals and odd syntax."""
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ProxyError("invalid hostname")
    if value.endswith("."):
        value = value[:-1]
    try:
        ascii_name = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ProxyError("invalid hostname")
    if not ascii_name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in ascii_name):
        raise ProxyError("invalid hostname")
    if ascii_name.startswith(".") or ".." in ascii_name:
        raise ProxyError("invalid hostname")
    # This catches dotted decimal, IPv6 (which already contains ':'), and any
    # unusual presentation accepted by ipaddress.
    try:
        ipaddress.ip_address(ascii_name)
    except ValueError:
        pass
    else:
        raise ProxyError("IP literal authorities are forbidden")
    for label in ascii_name.split("."):
        if not label or len(label) > 63 or label[0] == "-" or label[-1] == "-":
            raise ProxyError("invalid hostname")
    return ascii_name


def allowed_hostname(hostname):
    return hostname in ALLOWED_EXACT_HOSTS or any(hostname.endswith(suffix) for suffix in ALLOWED_SUFFIXES)


def parse_connect_request(header):
    """Parse one complete HTTP CONNECT header and return (hostname, port)."""
    if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\r\n\r\n"):
        raise ProxyError("invalid request headers")
    try:
        text = header[:-4].decode("ascii")
    except UnicodeDecodeError:
        raise ProxyError("request must be ASCII")
    lines = text.split("\r\n")
    if not lines or any(not line or "\x00" in line for line in lines):
        raise ProxyError("malformed request")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] != "HTTP/1.1":
        raise ProxyError("only CONNECT HTTP/1.1 is supported")
    authority = parts[1]
    if any(ch in authority for ch in "/?#@[]") or authority.count(":") != 1:
        raise ProxyError("authority must be hostname:443")
    raw_host, raw_port = authority.rsplit(":", 1)
    if raw_port != "443":
        raise ProxyError("only port 443 is allowed")
    hostname = normalize_hostname(raw_host)
    if not allowed_hostname(hostname):
        raise ProxyError("hostname is not allowlisted")
    # Reject malformed header fields rather than trying to recover from a
    # request-smuggling shaped request. They are not forwarded anywhere.
    for line in lines[1:]:
        if ":" not in line or line[0] in " \t":
            raise ProxyError("malformed header")
    return hostname, 443


def _read_limited(sock, limit):
    chunks = []
    total = 0
    while True:
        data = sock.recv(min(4096, limit + 1 - total))
        if not data:
            break
        chunks.append(data)
        total += len(data)
        if total > limit:
            raise ProxyError("response too large")
    return b"".join(chunks)


def resolve_public_a(hostname, timeout=CONNECT_TIMEOUT):
    """Resolve only through Cloudflare DoH and return global IPv4 addresses.

    The TLS peer name is cloudflare-dns.com while the TCP endpoint is pinned to
    1.1.1.1, avoiding the host resolver entirely.
    """
    query = DOH_PATH + "?" + urllib.parse.urlencode({"name": hostname, "type": "A"})
    context = ssl.create_default_context()
    raw = socket.create_connection((DOH_IP, 443), timeout=timeout)
    try:
        tls = context.wrap_socket(raw, server_hostname=DOH_HOST)
        try:
            tls.settimeout(timeout)
            request = (
                "GET " + query + " HTTP/1.1\r\n"
                "Host: " + DOH_HOST + "\r\n"
                "Accept: application/dns-json\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            tls.sendall(request)
            response = _read_limited(tls, MAX_DOH_BYTES)
        finally:
            tls.close()
    except Exception:
        raw.close()
        raise
    # Parse framing only after the wire response has passed the strict size bound.
    # HTTPResponse handles both Content-Length and chunked encoding over this buffer.
    class BufferedResponse:
        def makefile(self, *_args):
            return io.BytesIO(response)

    try:
        parsed = http.client.HTTPResponse(BufferedResponse())
        parsed.begin()
        if parsed.status != 200:
            raise ProxyError("DoH lookup failed")
        if parsed.getheader("Content-Type", "").lower().split(";", 1)[0] != "application/dns-json":
            raise ProxyError("unexpected DoH content type")
        body = parsed.read(MAX_DOH_BYTES + 1)
        parsed.close()
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise ProxyError("invalid DoH response") from exc
    if len(body) > MAX_DOH_BYTES:
        raise ProxyError("DoH response too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProxyError("invalid DoH JSON")
    addresses = []
    for answer in payload.get("Answer", []):
        if answer.get("type") != 1 or not isinstance(answer.get("data"), str):
            continue
        try:
            address = ipaddress.ip_address(answer["data"])
        except ValueError:
            continue
        if address.version != 4 or not address.is_global:
            raise ProxyError("DoH returned a non-global address")
        addresses.append(str(address))
    if not addresses:
        raise ProxyError("DoH returned no public A record")
    return tuple(dict.fromkeys(addresses))


def read_connect_header(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) >= MAX_HEADER_BYTES:
            raise ProxyError("request headers too large")
        block = sock.recv(min(4096, MAX_HEADER_BYTES - len(data)))
        if not block:
            raise ProxyError("client closed before request")
        data.extend(block)
    end = data.index(b"\r\n\r\n") + 4
    return bytes(data[:end]), bytes(data[end:])


def relay(left, right):
    """Bidirectional opaque byte relay until both peers close or timeout."""
    left.settimeout(CLIENT_TIMEOUT)
    right.settimeout(CLIENT_TIMEOUT)
    done = threading.Event()

    def abort():
        done.set()
        for peer in (left, right):
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def copy(source, destination):
        try:
            while not done.is_set():
                block = source.recv(64 * 1024)
                if not block:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                destination.sendall(block)
        except (OSError, socket.timeout):
            abort()

    first = threading.Thread(target=copy, args=(left, right), daemon=True)
    first.start()
    try:
        copy(right, left)
        # Ordinary EOF preserves the opposite direction until its own EOF or
        # idle timeout. Fatal I/O aborts both sockets and wakes a blocked recv.
        first.join()
    finally:
        if first.is_alive():
            abort()
            first.join()


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.client_slots.acquire(blocking=False):
            self.request.sendall(b"HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n")
            return
        upstream = None
        try:
            self.request.settimeout(CLIENT_TIMEOUT)
            header, buffered = read_connect_header(self.request)
            hostname, _port = parse_connect_request(header)
            addresses = resolve_public_a(hostname)
            # The connection deliberately uses this vetted IP, never a second
            # hostname resolution performed by the OS resolver.
            upstream = socket.create_connection((addresses[0], 443), timeout=CONNECT_TIMEOUT)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if buffered:
                upstream.sendall(buffered)
            relay(self.request, upstream)
        except ProxyError as exc:
            try:
                self.request.sendall(("HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n").encode("ascii"))
            except OSError:
                pass
        except (OSError, socket.timeout):
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()
            self.server.client_slots.release()


class ProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address=(LISTEN_HOST, LISTEN_PORT)):
        super().__init__(address, ConnectHandler)
        self.client_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CLIENTS)


def main():
    parser = argparse.ArgumentParser(description="restricted HTTPS CONNECT proxy for a QEMU guest")
    parser.add_argument("--listen", default=LISTEN_HOST)
    parser.add_argument("--port", default=LISTEN_PORT, type=int)
    args = parser.parse_args()
    # Refuse accidental external exposure even if invoked with --listen.
    if args.listen != LISTEN_HOST:
        parser.error("proxy may bind only to 127.0.0.1")
    with ProxyServer((args.listen, args.port)) as server:
        print("Restricted CONNECT proxy listening on %s:%d" % server.server_address, flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
