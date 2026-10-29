"""HTTP transport: POST /v1/<request> with a JSON body and `Authorization: Bearer <token>`. GET /v1/health answers
without a token. GET /view is a form for the view token, and the viewer itself (view_page) takes the token too.
Each request runs in its own thread, so a held wait never blocks anyone else.

A request's token is checked before its body is read, so only a known token can make the server read one. Each request
has a deadline: its line and headers must arrive within request_timeout, and its body within that plus its size at
min_rate. Its headers may take at most HEADERS bytes. At most max_connections are open at once: when they are all
taken, the one that has waited longest for a request is closed to make room, so connections that send nothing, or
send their headers slowly, cannot keep anyone out. A request under way is never closed, and one user may have at most
max_requests under way.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import re
import select
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import DATA_DIR, NAME, __version__, api, view
from .files import CHUNK, mime
from .store import Error, Store

HEADER_LIMIT = 64 * 1024  # the JSON line that starts an upload
HEADERS = 64 * 1024  # a request's line and headers, in all. Python's parser alone allows 100 lines of 64 KB.
READ_ONLY = f"This server only shows a data folder to the viewer. Start `{NAME} serve` to use it."
VIEW = {*view.METHODS, "view_feed", "view_download"}  # the requests the view token may make
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def ready(sock, timeout: float, read: bool = True, write: bool = False) -> bool:
    """Whether a socket can be read (or written) within `timeout` seconds. poll() where there is one: select() fails
    on descriptors above 1023, which a server holding a thousand waits has."""
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(sock, (select.POLLIN if read else 0) | (select.POLLOUT if write else 0))
        return bool(poller.poll(timeout * 1000))
    return any(select.select([sock] if read else [], [sock] if write else [], [], timeout))


def open_files() -> int | None:
    """Raises this process's limit on open files as far as the system allows, up to 65,536, and returns it (None
    where there is no such limit). Each connection is an open file, and so is each chat's log while it is written."""
    try:
        import resource
    except ImportError:  # Windows
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for want in (65536, 32768, 16384, 10240, 4096, hard):
        if want == resource.RLIM_INFINITY or want <= soft:
            break
        if hard == resource.RLIM_INFINITY or want <= hard:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
                return want
            except (ValueError, OSError):  # above what this system allows one process: try less
                continue
    return soft


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Lets a restarted server take its port back at once. Not on Windows, where the same option lets a second server
    # take a port another one is already listening on.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 4096  # the listen backlog. The default of 5 drops agents that connect at the same moment.

    def __init__(self, address, handler, max_connections: int):
        self.max_connections, self.refused_at = max_connections, 0.0
        self.slots = threading.BoundedSemaphore(max_connections)
        self.idle, self.idle_lock = {}, threading.Lock()  # connection -> since when it has waited for a request
        body = json.dumps({"error": f"The server is busy: {max_connections} connections are open. Try again "
                                    "shortly."}).encode()
        self.busy = (f"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: "
                     f"{len(body)}\r\nConnection: close\r\n\r\n").encode() + body
        super().__init__(address, handler)

    def server_bind(self):
        # HTTPServer would also look up the host's full name in DNS, which nothing here uses. Where reverse DNS is slow,
        # that lookup alone held up every start by about 30 seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False) and not (self.evict() and self.slots.acquire(timeout=1)):
            return self.refuse(request)
        try:
            super().process_request(request, client_address)
        except BaseException:  # no thread was started, so none will give the slot back
            self.slots.release()
            raise

    def waiting(self, connection):
        """The connection waits for a request: it may be closed to make room."""
        with self.idle_lock:
            self.idle[connection] = time.monotonic()

    def working(self, connection):
        """The connection has a request under way, or is gone: it is not closed to make room."""
        with self.idle_lock:
            self.idle.pop(connection, None)

    def evict(self) -> bool:
        """Closes the connection that has waited longest for a request: one that sends nothing, or its headers too
        slowly, or that a client kept open between requests (that client simply connects again). Its thread then ends
        and gives back its slot."""
        with self.idle_lock:
            if not self.idle:
                return False
            connection = min(self.idle, key=self.idle.get)
            del self.idle[connection]
        with contextlib.suppress(OSError):
            socket.socket.shutdown(connection, socket.SHUT_RDWR)  # wakes its thread, as a closed connection
        return True

    def refuse(self, request):
        """Every connection has a request under way: a short answer where it can go out at once, and the connection
        closed."""
        if not isinstance(request, ssl.SSLSocket):  # TLS would need a handshake first
            with contextlib.suppress(OSError):
                request.setblocking(False)
                request.send(self.busy)
        self.shutdown_request(request)
        if time.monotonic() - self.refused_at > 60:
            self.refused_at = time.monotonic()
            sys.stderr.write(f"Refusing connections: {self.max_connections} are open (max_connections).\n")

    def process_request_thread(self, request, client_address):
        try:
            self.waiting(request)
            # Handshaking in accept() lets one silent peer block every new connection.
            if isinstance(request, ssl.SSLSocket) and not self.handshake(request):
                self.shutdown_request(request)
                return
            super().process_request_thread(request, client_address)
        finally:
            self.working(request)
            self.slots.release()

    def handshake(self, request) -> bool:
        """The TLS handshake, done within request_timeout in all, so a peer that sends it a byte at a time is cut off
        rather than holding a thread."""
        timeout = self.RequestHandlerClass.timeout
        deadline = time.monotonic() + timeout
        try:
            request.setblocking(False)
            while True:
                try:
                    request.do_handshake()
                    return True
                except ssl.SSLWantReadError:
                    wanted = (True, False)
                except ssl.SSLWantWriteError:
                    wanted = (False, True)
                left = deadline - time.monotonic()
                if left <= 0 or not ready(request, left, *wanted):
                    return False
        except OSError:
            return False
        finally:
            with contextlib.suppress(OSError):
                request.settimeout(timeout)

    def handle_error(self, request, client_address):
        # A client that hangs up (a killed command, a closed laptop) is normal: no traceback for that.
        if isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout, ssl.SSLError)):
            if self.RequestHandlerClass.verbose:
                sys.stderr.write(f"{client_address[0]} hung up: {sys.exc_info()[1]}\n")
            return
        super().handle_error(request, client_address)


class Reader(socket.SocketIO):
    """A connection's reads, each given only the time left before its handler's deadline. The socket's own timeout
    is for silence: a client that sends a byte every few seconds would never meet it."""

    def __init__(self, sock, handler):
        super().__init__(sock, "rb")
        self.handler = handler

    def readinto(self, b):
        n = self._read(b)
        if self.handler.heading and n:
            self.handler.heard += n
            if self.handler.heard > HEADERS + len(b):  # a read may run on into the body, up to its buffer's size
                raise ConnectionAbortedError("The request's headers are too large.")
        return n

    def _read(self, b):
        deadline = self.handler.deadline
        if deadline is None:
            return super().readinto(b)
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("The request did not arrive in time.")
        if left >= 0.9 * self.handler.timeout:  # near enough to the socket's own timeout, which then stands
            return super().readinto(b)
        self._sock.settimeout(left)
        try:
            return super().readinto(b)
        finally:
            self._sock.settimeout(self.handler.timeout)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # without this, each reply on Linux waits ~40 ms for the client's delayed ACK
    server_version = f"{NAME}/{__version__}"
    store: Store = None
    verbose = False
    unread = False  # whether the reply went out before the request's body was read
    deadline = None  # when what the client is sending must have arrived, or None while nothing is due from it
    heading, heard = False, 0  # whether the request's line and headers are being read, and their bytes so far
    LINGER_BYTES, LINGER_SECONDS = 8 << 20, 2.0

    def setup(self):
        super().setup()
        self.rfile.close()  # replaced by one that keeps to the request's deadline
        self.rfile = io.BufferedReader(Reader(self.connection, self))

    def handle_one_request(self):
        self.deadline = time.monotonic() + self.timeout  # the request line and headers, all within request_timeout
        self.heading, self.heard = True, 0
        self.server.waiting(self.connection)
        super().handle_one_request()

    def parse_request(self):
        parsed = super().parse_request()
        self.heading = False  # the headers are in: from here a request is under way, never closed to make room
        self.server.working(self.connection)
        return parsed

    def send_header(self, keyword, value):
        # A line break in a value would end the header there, and the rest would be read as more headers or a body.
        if CONTROL.search(str(value)):
            raise ValueError(f"The {keyword} header holds a control character.")
        super().send_header(keyword, value)

    def finish(self):
        self.server.working(self.connection)
        super().finish()
        if self.unread:
            self.linger()

    def linger(self):
        """Stops writing, then reads and drops what the client is still sending, before the server closes. Closing with
        bytes unread makes the system reset the connection, and a client still sending a refused body would then lose
        the reply already written to it. Bounded, so a huge upload is cut off rather than read to its end."""
        sock, left = self.connection, self.LINGER_BYTES
        deadline = time.monotonic() + self.LINGER_SECONDS
        try:
            sock.shutdown(socket.SHUT_WR)
            while left > 0 and time.monotonic() < deadline:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = sock.recv(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:  # the client hung up, or the time ran out
            pass

    def client_gone(self) -> bool:
        """Whether the caller has hung up. Nothing is due from a client while it waits for a reply, so a readable
        connection means it was closed. Peeks at the raw socket, which works under TLS too."""
        try:
            if not ready(self.connection, 0):
                return False
            return socket.socket.recv(self.connection, 1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def reply(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        packed = len(data) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if packed:  # browsers ask for it. The CLI does not, so agents' replies stay as they were.
            data = gzip.compress(data, 4)
        if self.unread:  # a body left unread must not be taken for the next request
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if packed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/health":
            limits = {k: self.store.settings[k] for k in ("max_text", "max_attachment", "max_attachments")}
            self.reply(200, {"ok": True, "name": NAME, "version": __version__, "limits": limits})
        elif path in ("/view", "/view/"):
            try:
                self.send_page(view.gate())
            except OSError as e:  # an install without the page's files
                self.reply(500, {"error": f"The viewer's page is missing from this install: {e}"})
        elif path == "/":
            self.send_response(302)
            self.send_header("Location", "/view")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.reply(404, {"error": "Not found."})

    def send_page(self, page: dict):
        if self.headers.get("If-None-Match") == page["etag"]:
            self.send_response(304)
            self.send_header("ETag", page["etag"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        packed = "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = page["gzip"] if packed else page["body"]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if packed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", page["etag"])
        self.send_header("Cache-Control", "no-cache")  # asked again each time, answered 304 while unchanged
        self.send_header("Content-Security-Policy", page["csp"])
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        name = self.path[len("/v1/"):] if self.path.startswith("/v1/") else ""
        self.unread = True
        try:
            lengths = self.headers.get_all("Content-Length", [])
            length_text = lengths[0].strip() if lengths else "0"
            if (self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1
                    or not length_text.isascii() or not length_text.isdecimal()):
                self.close_connection = True
                raise Error(400, "Use one nonnegative Content-Length and no Transfer-Encoding.")
            length = int(length_text)
            auth = self.headers.get("Authorization") or ""
            me = self.caller(name, auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else "")
            with self.store.request(me) if me else contextlib.nullcontext():  # one user's requests under way, counted
                self.answer(name, me, length)
        except (ConnectionError, socket.timeout):  # the caller hung up, or did not send the request in time
            self.close_connection = True
        except Error as e:
            self.reply(e.status, {"error": str(e)})
        except (ValueError, TypeError, KeyError) as e:
            self.reply(400, {"error": f"Bad request: {e}"})
        except Exception:  # noqa: BLE001 - one bad request must not take the server down
            traceback.print_exc()
            self.reply(500, {"error": "Server error. See the server's log."})

    def answer(self, name: str, me: str | None, length: int):
        """Reads the request's body, within its deadline, and replies."""
        self.deadline = time.monotonic() + self.timeout + length / self.store.settings["min_rate"]
        if name == "send_files":
            self.close_connection = True  # one upload per connection: an unread body is never parsed as a request
            result = self.send_files(me, length)
            self.unread, self.deadline = False, None
            return self.reply(200, result)
        if length > self.store.settings["max_body"]:
            raise Error(413, f"The request is larger than {self.store.settings['max_body']} bytes.")
        body = self.rfile.read(length)
        self.unread, self.deadline = False, None
        if len(body) != length:
            self.close_connection = True
            raise Error(400, "The request body ended before Content-Length bytes arrived.")
        params = json.loads(body or b"{}")
        if not isinstance(params, dict):
            raise Error(400, "The request body must be a JSON object.")
        if name in VIEW:
            if name == "view_download":
                return self.send_file(*view.download(self.store, params))
            if name == "view_feed":
                result = view.view_feed(self.store, params, gone=self.client_gone)
            else:
                result = view.METHODS[name](self.store, params)
        elif name in api.ADMIN:  # me is None for the super admin's token
            result = api.ADMIN[name](self.store, me, params)
        else:
            if params.get("ack"):  # messages a previous wait handed over, now confirmed received
                api.confirm(self.store, me, params.pop("ack"))
            if name == "download":
                return self.send_file(*api.download(self.store, me, params))
            if name == "wait":
                result = api.wait(self.store, me, params, gone=self.client_gone)
            else:
                result = api.METHODS[name](self.store, me, params)
        self.reply(200, result)

    def caller(self, name: str, token: str) -> str | None:
        """Who makes this request, from its token, checked before its body is read. The viewer's requests take the view
        token or the admin token, the admin requests the admin token (None) or a user's, and the rest a user's."""
        if name in VIEW:
            if not self.store.can_view(token):
                raise Error(401, "This request needs the view token or the admin token.")
            return None
        if self.store.read_only:
            raise Error(403, READ_ONLY)
        if name in api.ADMIN and self.store.is_admin(token):
            return None  # the super admin. Otherwise a user, whom the rules may allow.
        if name not in api.ADMIN and name not in api.METHODS and name not in ("download", "send_files"):
            raise Error(404, f"There is no request named '{name}'.")
        me = self.store.me(token)
        if not me:
            raise Error(401, "This request needs the admin token, or the token of a user the rules allow."
                        if name in api.ADMIN else f"Unknown token. Set {NAME.upper()}_TOKEN or run `{NAME} login`.")
        return me

    def send_files(self, me: str, length: int) -> dict:
        """A message with files: one line of JSON (like send's parameters, plus the files' names and sizes), then
        the files' bytes back to back. Streamed to disk, never held in memory."""
        settings = self.store.settings
        if length > settings["max_attachments"] * settings["max_attachment"] + HEADER_LIMIT:
            raise Error(413, "The request is larger than the attachment limits allow.")
        consumed = 0

        def read(n: int) -> bytes:
            nonlocal consumed
            chunk = self.rfile.read(n)
            consumed += len(chunk)
            return chunk

        try:
            line = self.rfile.readline(HEADER_LIMIT + 1)
            consumed += len(line)
            if len(line) > HEADER_LIMIT or not line.endswith(b"\n"):
                raise Error(400, "Start with one line of JSON describing the message and its files.")
            try:
                header = json.loads(line)
            except ValueError:
                raise Error(400, "The first line must be JSON.") from None
            if not isinstance(header, dict):
                raise Error(400, "The first line must be a JSON object.")
            if header.get("ack"):
                api.confirm(self.store, me, header.pop("ack"))
            return api.send_files(self.store, me, header, read, length - len(line))
        except Error:
            # Refused before all the bytes were read: read the rest, within the limits checked above, so the client
            # can finish sending and get this answer instead of a reset connection.
            left = length - consumed
            while left > 0:
                chunk = self.rfile.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
            raise

    def send_file(self, attachment: dict, path: str):
        """An attachment's bytes as the reply, streamed. Its name and type travel in the X-Attachment header."""
        with open(path, "rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", mime(attachment.get("type")) or "application/octet-stream")
            self.send_header("Content-Length", str(attachment["size"]))
            self.send_header("X-Attachment", json.dumps({k: v for k, v in attachment.items() if k != "sha256"}))
            self.end_headers()
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)


def make_server(settings: dict, read_only: bool = False):
    """(the HTTP server, its store), ready for serve_forever(). settings come from config.load(). Tests use this.
    read_only serves a folder to the viewer only, writing nothing to it (`swarmchat view --data DIR` with no server)."""
    store = Store(settings["data"], settings, read_only)
    handler = type("BoundHandler", (Handler,), {"store": store, "verbose": settings["verbose"],
                                                "timeout": settings["request_timeout"]})
    files = open_files()
    connections = settings["max_connections"] if files is None else min(settings["max_connections"], files // 2)
    if connections < settings["max_connections"]:  # the other half is for logs, uploads and the rest
        print(f"max_connections is {connections}: this system lets the server open {files} files.", file=sys.stderr)
    httpd = None
    try:
        httpd = Server((settings["host"], settings["port"]), handler, connections)
        if settings["tls_cert"]:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(settings["tls_cert"], settings["tls_key"])
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    except BaseException:
        if httpd:
            httpd.server_close()
        store.close()
        raise
    # Where this server listens, so `swarmchat admin --data DIR` reaches it without being told the address.
    host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(settings["host"], settings["host"])
    host = f"[{host}]" if ":" in host else host
    httpd.url = f"{'https' if settings['tls_cert'] else 'http'}://{host}:{httpd.server_address[1]}"
    if not read_only:
        with open(store.file("address"), "w", encoding="utf-8") as f:
            f.write(httpd.url + "\n")
    return httpd, store


def save_read_positions(store: Store, stopped: threading.Event):
    while not stopped.wait(store.settings["save_every"]):
        try:
            store.flush()
        except OSError as exc:
            print(f"Could not save read positions; will retry: {exc}", file=sys.stderr, flush=True)


def serve(settings: dict):
    httpd, store = make_server(settings)
    stopped = threading.Event()
    saver = threading.Thread(target=save_read_positions, args=(store, stopped), daemon=True)
    saver.start()

    def stop(signum, frame):  # a normal stop (SIGTERM) saves read positions, like Ctrl-C
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):  # Windows has no SIGTERM between processes: Ctrl-Break is its normal stop
        signal.signal(signal.SIGBREAK, stop)
    signal.signal(signal.SIGINT, signal.default_int_handler)  # a server started in the background ignores Ctrl-C
    shown = ", ".join(f"{k}={v}" for k, v in store.settings.items() if k not in ("data", "host", "port", "tls_cert", "tls_key"))
    print(f"{NAME} {__version__}: {len(store.users)} users, {len(store.chats)} chats from {settings['data']}, serving "
          f"{'https' if settings['tls_cert'] else 'http'}://{settings['host']}:{httpd.server_address[1]}", flush=True)
    print(f"Settings: {shown}", flush=True)
    print(f"The admin token is in {os.path.join(settings['data'], 'admin.token')}", flush=True)
    print(f"To watch everything: {NAME} view" + ("" if settings["data"] == DATA_DIR else f" --data {settings['data']}"),
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        saver.join()
        httpd.server_close()
        store.close()
