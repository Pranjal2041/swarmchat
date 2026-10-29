"""HTTP framing, TLS isolation, finite waits, and ambiguous transport failures."""
import http.client
import json
import math
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_swarmchat import Base, Server
from swarmchat import api, config
from swarmchat.client import ClientError
from swarmchat.server import Handler, make_server


class TransportSafety(Base):
    def test_ambiguous_body_framing_is_rejected_and_connection_closed(self):
        for framing, body in (("Content-Length: 2\r\nContent-Length: 2\r\n", b"{}"),
                              ("Transfer-Encoding: chunked\r\n", b"0\r\n\r\n"),
                              ("Content-Length: 8\r\n", b"{}")):
            with self.subTest(framing=framing):
                with socket.create_connection(self.server.httpd.server_address, timeout=2) as sock:
                    sock.sendall(("POST /v1/whoami HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                                  f"{self.alice.token}\r\n" + framing + "\r\n").encode() + body)
                    sock.shutdown(socket.SHUT_WR)
                    response = http.client.HTTPResponse(sock)
                    response.begin()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(response.getheader("Connection"), "close")
                    response.read()

    def test_nonfinite_time_settings_and_client_waits_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            for setting in ("max_wait", "save_every", "request_timeout"):
                with self.subTest(value=value, setting=setting), self.assertRaises(SystemExit):
                    config.load(data=self.data, **{setting: value})
            with self.assertRaises(ClientError):
                self.bob.wait(timeout=value)

    def test_send_is_not_duplicated_when_response_connection_is_lost(self):
        self.alice.whoami()  # Reuse a healthy HTTP connection, as normal clients do.
        original_reply = Handler.reply
        dropped = threading.Event()

        def drop_one_send_response(handler, status, body):
            if handler.path == "/v1/send" and status == 200 and not dropped.is_set():
                dropped.set()  # The message was already committed by api.send.
                handler.close_connection = True
                handler.connection.shutdown(socket.SHUT_RDWR)
                handler.connection.close()
                return
            return original_reply(handler, status, body)

        with patch.object(Handler, "reply", drop_one_send_response):
            try:
                self.alice.send(chat="bob", text="perform this action once")
            except ClientError:
                pass  # An ambiguous transport failure must not replay a mutation.
        self.assertTrue(dropped.is_set())
        self.assertEqual(self.texts(self.bob.read()), ["perform this action once"])


    def test_nonfinite_wait_timeout_is_rejected_without_busy_loop(self):
        pending_calls = 0
        original_pending = api._pending

        def bounded_pending(*args, **kwargs):
            nonlocal pending_calls
            pending_calls += 1
            if pending_calls > 20:
                # Terminate the defective request in the test; production has no such guard.
                raise api.Error(400, "Test guard stopped a non-terminating wait")
            return original_pending(*args, **kwargs)

        with patch.object(api, "_pending", side_effect=bounded_pending):
            with self.assertRaises(ClientError) as caught:
                self.bob.call("wait", {"timeout": "nan"}, http_timeout=2)
        self.assertEqual(caught.exception.status, 400)
        self.assertLessEqual(pending_calls, 1, "NaN timeout repeatedly polled instead of rejecting or blocking")

    def test_negative_content_length_cannot_bypass_max_body(self):
        self.server.store.settings["max_body"] = 128
        body = json.dumps({"ignored": "x" * 1024}).encode()
        headers = (
            "POST /v1/whoami HTTP/1.1\r\nHost: localhost\r\n"
            f"Authorization: Bearer {self.alice.token}\r\n"
            "Content-Length: -1\r\nConnection: close\r\n\r\n"
        ).encode()
        with socket.create_connection(self.server.httpd.server_address, timeout=2) as sock:
            sock.sendall(headers + body)
            sock.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(sock)
            response.begin()
            status = response.status
            response.read()
        self.assertIn(status, (400, 413), "Server accepted 1 KiB despite a 128-byte request limit")

    def test_oversized_request_does_not_poison_next_client_call(self):
        self.server.store.settings["max_body"] = 256
        with self.assertRaises(ClientError) as caught:
            self.alice.send(chat="bob", text="x" * 1024)
        self.assertEqual(caught.exception.status, 413)
        try:
            result = self.alice.whoami()
        except Exception as exc:
            self.fail(f"A valid request after HTTP 413 failed with {type(exc).__name__}: {exc}")
        self.assertEqual(result["user"]["id"], "alice")

    def test_a_refused_body_larger_than_the_socket_buffers_still_gets_its_answer(self):
        # The server answers before reading the body. Closing with the body unread would reset the connection and the
        # client, still sending, would never see the answer.
        self.server.store.settings["max_body"] = 256
        for _ in range(3):
            with self.assertRaises(ClientError) as caught:
                self.alice.send(chat="bob", text="x" * 3_000_000)
            self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.alice.whoami()["user"]["id"], "alice")



class RequestLimits(unittest.TestCase):
    """What a client can hold: without a known token, no body is read. A request that arrives too slowly is cut off,
    and no more than max_connections are open at once."""

    def start(self, **settings):
        data = tempfile.mkdtemp(prefix="swarmchat-test-")
        self.addCleanup(shutil.rmtree, data)
        server = Server(data, **settings)
        self.addCleanup(server.stop)
        return server

    def connect(self, server, sent: bytes = b"") -> socket.socket:
        sock = socket.create_connection(server.httpd.server_address, timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(sent)
        return sock

    def closed_after(self, sock, trickle: bytes) -> float:
        """Seconds until the server closes the connection, while one byte of `trickle` goes out every 0.1 seconds:
        never silent long enough for the idle timeout."""
        start, stop = time.monotonic(), threading.Event()

        def drip():
            for byte in trickle:
                if stop.wait(0.1):
                    return
                try:
                    sock.send(bytes([byte]))
                except OSError:
                    return
        threading.Thread(target=drip, daemon=True).start()
        try:
            while sock.recv(4096):
                pass
        except OSError:  # reset rather than closed: the same for the client
            pass
        stop.set()
        return time.monotonic() - start

    def test_an_unknown_token_is_refused_before_its_body_is_read(self):
        server = self.start()
        for name, status in (("whoami", 401), ("send_files", 401), ("user_add", 401), ("view_info", 401),
                             ("no_such_request", 404)):
            with self.subTest(name=name):
                sock = self.connect(server, f"POST /v1/{name} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer nope\r\n"
                                            "Content-Length: 100000000\r\n\r\n".encode())
                response = http.client.HTTPResponse(sock)
                response.begin()  # answered at once: the server did not wait for 100 MB that never come
                self.assertEqual((response.status, response.getheader("Connection")), (status, "close"))

    def test_headers_that_arrive_too_slowly_are_cut_off(self):
        server = self.start(request_timeout=1)
        sock = self.connect(server, b"POST /v1/whoami HTTP/1.1\r\n")
        self.assertLess(self.closed_after(sock, b"X" * 100), 3)
        self.assertEqual(server.admin.policy_show()["rules"], {})  # everyone else is served as before

    def test_a_body_that_arrives_too_slowly_is_cut_off(self):
        server = self.start(request_timeout=1, min_rate=10_000)
        token = server.user("a", "A").token
        sock = self.connect(server, f"POST /v1/whoami HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
                                    "Content-Length: 1000\r\n\r\n".encode())
        self.assertLess(self.closed_after(sock, b" " * 1000), 3)  # 1 second, and 0.1 for 1,000 bytes at 10,000 a second

    def test_connections_that_send_nothing_cannot_keep_others_out(self):
        server = self.start(max_connections=4, request_timeout=30)
        silent = [self.connect(server, b"POST /v1/whoami HTTP/1.1\r\nHost: x\r\n") for _ in range(50)]
        time.sleep(0.3)  # every slot taken, by connections that never finish their headers
        start = time.monotonic()
        self.assertEqual(server.admin.policy_show()["rules"], {})  # the longest waiting one made room
        self.assertLess(time.monotonic() - start, 3)

        def closed(sock):
            sock.settimeout(0.2)
            try:
                return sock.recv(1) == b""
            except socket.timeout:
                return False
        self.assertEqual([closed(sock) for sock in silent], [True] * 47 + [False] * 3)  # the oldest made room

    def test_connections_with_requests_under_way_are_never_closed_to_make_room(self):
        server = self.start(max_connections=2)
        a, b = server.user("a", "A"), server.user("b", "B")
        got = []
        holders = [threading.Thread(target=lambda c=c: got.append(c.call("wait", {"timeout": 2}, http_timeout=10)))
                   for c in (a, b)]
        for t in holders:
            t.start()
        time.sleep(0.5)
        turned = self.connect(server)
        reply = turned.recv(4096)
        self.assertTrue(reply.startswith(b"HTTP/1.1 503 "), reply)
        self.assertIn(b"2 connections are open", reply)
        for t in holders:
            t.join()
        self.assertEqual(len(got), 2)  # both waits ran to their end

    def test_one_user_has_at_most_max_requests_under_way(self):
        server = self.start(max_requests=1)
        a = server.user("a", "A")
        waiting = threading.Thread(target=lambda: a.call("wait", {"timeout": 2}, http_timeout=10))
        waiting.start()
        time.sleep(0.5)
        for name in ("wait", "whoami"):
            with self.subTest(name=name), self.assertRaises(ClientError) as e:
                server.client(a.token).call(name, {"timeout": 1})
            self.assertEqual(e.exception.status, 429)
        self.assertEqual(server.user("b", "B").whoami()["user"]["id"], "b")  # others are not held back
        waiting.join()
        self.assertEqual(server.client(a.token).call("wait", {"timeout": 0.01})["messages"], [])

    def test_headers_have_a_size_limit(self):
        server = self.start()
        sock = self.connect(server)
        try:
            sock.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n" + b"".join(
                f"X-Filler-{i}: {'a' * 4000}\r\n".encode() for i in range(90)) + b"\r\n")
            reply = sock.recv(4096)
        except (ConnectionResetError, BrokenPipeError):
            reply = b""
        self.assertEqual(reply, b"")  # 360 KB of headers: closed, never parsed
        self.assertEqual(server.admin.policy_show()["rules"], {})

    def test_no_header_value_can_hold_a_line_break(self):
        handler = Handler.__new__(Handler)
        handler.request_version = "HTTP/1.1"
        for value in ("text/plain\r\nX-Injected: 1", "a\nb", "a\x00b"):
            with self.assertRaises(ValueError):
                handler.send_header("Content-Type", value)
        handler.send_header("Content-Type", "text/plain")
        self.assertEqual(handler._headers_buffer, [b"Content-Type: text/plain\r\n"])


class TLSRegression(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl is needed for a temporary test certificate")
    def test_one_idle_tls_connection_does_not_block_other_clients(self):
        with tempfile.TemporaryDirectory(prefix="swarmchat-review-tls-") as temporary:
            cert, key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
            subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            httpd, store = make_server(config.load(data=str(Path(temporary) / "data"), port=0,
                                                   tls_cert=str(cert), tls_key=str(key)))
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            idle = None
            try:
                def healthy_request():
                    client = http.client.HTTPSConnection(*httpd.server_address, timeout=0.4,
                                                       context=ssl._create_unverified_context())
                    try:
                        client.request("GET", "/v1/health", headers={"Connection": "close"})
                        response = client.getresponse()
                        response.read()
                        return response.status
                    finally:
                        client.close()

                self.assertEqual(healthy_request(), 200)
                idle = socket.create_connection(httpd.server_address, timeout=1)
                time.sleep(0.05)
                try:
                    status = healthy_request()
                except (OSError, http.client.HTTPException) as exc:
                    idle.close()
                    idle = None
                    self.assertEqual(healthy_request(), 200, "Server failed to recover after idle peer closed")
                    self.fail(f"A single idle unauthenticated socket blocked HTTPS: {type(exc).__name__}: {exc}")
                self.assertEqual(status, 200)
            finally:
                if idle:
                    idle.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                store.close()


class IdleConnections(unittest.TestCase):
    def test_send_after_the_server_closed_an_idle_connection(self):
        data = tempfile.mkdtemp(prefix="swarmchat-test-")
        server = Server(data, request_timeout=0.5)
        try:
            a, b = server.user("a", "A"), server.user("b", "B")
            a.send(chat="b", text="before")
            time.sleep(1.2)  # the server has closed a's idle kept-open connection by now
            a.send(chat="b", text="after idle")  # goes out on a fresh connection, once
            self.assertEqual([m["text"] for m in b.read()["messages"]], ["before", "after idle"])
        finally:
            server.stop()
            shutil.rmtree(data)
