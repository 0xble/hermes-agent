#!/usr/bin/env python3
"""Behavioral tests for proxy.py. Run: python3 -m unittest -v test_proxy.py"""

import json
import os
import socket
import ssl
import threading
import unittest
from unittest import mock

from scripts.ci import isolated_egress as proxy


class FakeRaw:
    def close(self):
        pass


class FakeTLS:
    def __init__(self, response):
        self.response = response
        self.sent = b""

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        if self.response:
            result, self.response = self.response, b""
            return result
        return b""

    def close(self):
        pass


class FakeContext:
    def __init__(self, tls):
        self.tls = tls
        self.server_name = None

    def wrap_socket(self, _raw, server_hostname):
        self.server_name = server_hostname
        return self.tls


def doh_response(addresses, content_type="application/dns-json"):
    payload = json.dumps({"Status": 0, "Answer": [{"type": 1, "data": x} for x in addresses]}).encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type.encode() +
            b"\r\nConnection: close\r\n\r\n" + payload)


class ParserTests(unittest.TestCase):
    def test_allowed_and_normalized_hosts(self):
        self.assertEqual(proxy.parse_connect_request(b"CONNECT API.GITHUB.COM:443 HTTP/1.1\r\nHost: ignored\r\n\r\n"),
                         ("api.github.com", 443))
        self.assertEqual(proxy.parse_connect_request(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n"),
                         ("pypi.org", 443))
        self.assertEqual(proxy.parse_connect_request(
            b"CONNECT results.blob.core.windows.net:443 HTTP/1.1\r\nHost: x\r\n\r\n"),
            ("results.blob.core.windows.net", 443))

    def test_malicious_authorities_are_rejected(self):
        bad = [
            b"localhost:443", b"127.0.0.1:443", b"[::1]:443", b"169.254.169.254:443",
            b"github.com.evil.test:443", b"evilgithub.com:443", b"github.com:80",
            b"results.blob.core.windows.net.evil.test:443", b"evilblob.core.windows.net:443",
            b"user@github.com:443", b"github.com:443/path", b"github.com",
        ]
        for authority in bad:
            with self.subTest(authority=authority), self.assertRaises(proxy.ProxyError):
                proxy.parse_connect_request(b"CONNECT " + authority + b" HTTP/1.1\r\nHost: x\r\n\r\n")

    def test_method_version_headers_and_size_are_rejected(self):
        for request in (
            b"GET github.com:443 HTTP/1.1\r\nHost: x\r\n\r\n",
            b"CONNECT github.com:443 HTTP/1.0\r\nHost: x\r\n\r\n",
            b"CONNECT github.com:443 HTTP/1.1\r\n folded: x\r\n\r\n",
            b"CONNECT github.com:443 HTTP/1.1\n\n",
        ):
            with self.subTest(request=request), self.assertRaises(proxy.ProxyError):
                proxy.parse_connect_request(request)
        with self.assertRaises(proxy.ProxyError):
            proxy.parse_connect_request(b"CONNECT github.com:443 HTTP/1.1\r\nX: " + b"x" * proxy.MAX_HEADER_BYTES + b"\r\n\r\n")


class DohTests(unittest.TestCase):
    def test_doh_is_pinned_and_accepts_only_global_a_records(self):
        tls = FakeTLS(doh_response(["8.8.8.8", "1.1.1.1"]))
        context = FakeContext(tls)
        with mock.patch.object(proxy.socket, "create_connection", return_value=FakeRaw()) as connect, \
             mock.patch.object(proxy.ssl, "create_default_context", return_value=context):
            self.assertEqual(proxy.resolve_public_a("github.com"), ("8.8.8.8", "1.1.1.1"))
        connect.assert_called_once_with(("1.1.1.1", 443), timeout=proxy.CONNECT_TIMEOUT)
        self.assertEqual(context.server_name, "cloudflare-dns.com")
        self.assertIn(b"Host: cloudflare-dns.com\r\n", tls.sent)
        self.assertIn(b"Accept: application/dns-json\r\n", tls.sent)

    def test_doh_rejects_private_loopback_and_non_dns_json(self):
        for addresses in (["10.0.0.1"], ["127.0.0.1"], ["192.168.1.1"], ["198.18.0.1"]):
            tls = FakeTLS(doh_response(addresses))
            with self.subTest(addresses=addresses), \
                 mock.patch.object(proxy.socket, "create_connection", return_value=FakeRaw()), \
                 mock.patch.object(proxy.ssl, "create_default_context", return_value=FakeContext(tls)), \
                 self.assertRaises(proxy.ProxyError):
                proxy.resolve_public_a("github.com")
        tls = FakeTLS(doh_response(["8.8.8.8"], "text/plain"))
        with mock.patch.object(proxy.socket, "create_connection", return_value=FakeRaw()), \
             mock.patch.object(proxy.ssl, "create_default_context", return_value=FakeContext(tls)), \
             self.assertRaises(proxy.ProxyError):
            proxy.resolve_public_a("github.com")


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = proxy.ProxyServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.address = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def connect(self, authority):
        client = socket.create_connection(self.address, timeout=10)
        client.settimeout(15)
        client.sendall(b"CONNECT " + authority.encode("ascii") + b" HTTP/1.1\r\nHost: test\r\n\r\n")
        return client

    def test_denied_authority_never_calls_dns(self):
        with mock.patch.object(proxy, "resolve_public_a", side_effect=AssertionError("must not resolve")):
            client = self.connect("github.com.evil.test:443")
            try:
                self.assertTrue(client.recv(1024).startswith(b"HTTP/1.1 403"))
            finally:
                client.close()

    @unittest.skipUnless(os.environ.get("HERMES_CI_LIVE_EGRESS_TEST") == "1", "explicit live egress opt-in")
    def test_real_github_tls_through_opaque_connect_tunnel(self):
        # This hits live Cloudflare DoH and then GitHub; a TLS request can only
        # succeed if CONNECT is relaying opaque bytes to the vetted IP.
        client = self.connect("github.com:443")
        try:
            self.assertTrue(client.recv(1024).startswith(b"HTTP/1.1 200"))
            context = ssl.create_default_context()
            with context.wrap_socket(client, server_hostname="github.com") as tls:
                tls.sendall(b"HEAD / HTTP/1.1\r\nHost: github.com\r\nConnection: close\r\n\r\n")
                response = tls.recv(4096)
            self.assertTrue(response.startswith(b"HTTP/1.1 "), response[:100])
        finally:
            try:
                client.close()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_doh_decodes_chunked_response_on_pinned_transport():
    payload = json.dumps({'Answer': [{'type': 1, 'data': '8.8.8.8'}]}).encode()
    response = (b'HTTP/1.1 200 OK\r\nContent-Type: application/dns-json\r\n'
                b'Transfer-Encoding: chunked\r\n\r\n' +
                f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n')
    tls = FakeTLS(response)
    with mock.patch.object(proxy.socket, 'create_connection', return_value=FakeRaw()), \
         mock.patch.object(proxy.ssl, 'create_default_context', return_value=FakeContext(tls)):
        assert proxy.resolve_public_a('github.com') == ('8.8.8.8',)
