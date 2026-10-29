"""The client, usable from Python as well as from the CLI.

    from swarmchat.client import Client
    alice = Client("http://127.0.0.1:8484", token)
    alice.send(chat="bob", text="hi @bob")
    alice.wait(timeout=60, where="'urgent' in m.text")

Every server request is a method of the same name (`alice.chats(unread=True)`). read, chats and wait add what runs
on the caller's side: --where filters, and waits longer than the server holds one request.
"""
from __future__ import annotations

import ast
import http.client
import json
import math
import mimetypes
import os
import re
import select
import shutil
import socket
import ssl
import tempfile
import time
import types
import urllib.parse
import zipfile

from . import CONFIG_DIR, ENV
from .files import CHUNK, SPARE, pack, safe_name, unpack

HOLD = 280  # seconds per wait request. The server holds at most 300.
MAX_REPLY = 1 << 30  # the largest JSON reply taken from a server, in bytes


class ClientError(Exception):
    """A refused or failed request. status is the HTTP status, or 0 when the server could not be reached."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def read_saved_token() -> str | None:
    """The token `login` saved, if any."""
    return _read(os.path.join(CONFIG_DIR, "token"))


def _replace(path: str, text: str):
    """Replaces a file with one that only its owner can read, written in full first, then put in place. A file there
    already, with looser permissions or as a link to somewhere else, is replaced, never written through."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".swarmchat-")  # made readable by its owner alone
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def save_login(server: str, token: str):
    """Stores the server address and this user's token in the config folder, which only its owner may open."""
    os.makedirs(CONFIG_DIR, 0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(CONFIG_DIR, 0o700)  # a folder made before, or by something else
    _replace(os.path.join(CONFIG_DIR, "config.json"), json.dumps({"server": server}))
    _replace(os.path.join(CONFIG_DIR, "token"), token + "\n")


class Item(dict):
    """A message or chat for --where expressions: m.text, m.sender, c.unread. Missing fields are None."""

    def __getattr__(self, key):
        value = self.get(key)
        return Item(value) if isinstance(value, dict) else value


# What an expression may use: the item, these functions, re's matching functions, and the attributes of plain data.
# Nothing else is reachable, so an expression a model wrote cannot import, open files or run commands, even where the
# client runs with more access than the model has.
SAFE = {f.__name__: f for f in (abs, all, any, bool, dict, enumerate, float, int, len, list, max, min, range, reversed,
                                round, set, sorted, str, sum, tuple, zip)}
SAFE["re"] = types.SimpleNamespace(**{name: getattr(re, name) for name in (
    "search", "match", "fullmatch", "findall", "sub", "split", "escape", "IGNORECASE", "I", "MULTILINE", "M",
    "DOTALL", "S")})
PLAIN = (Item, dict, list, tuple, str, int, float, bool, set, frozenset, type(None), types.SimpleNamespace, re.Match,
         re.Pattern)


def _attr(value, name: str):
    """value.name, for plain data only. Generators, functions, frames and types are not, so they lead nowhere."""
    if name.startswith("_") or name in ("format", "format_map", "mro") or type(value) not in PLAIN:
        raise AttributeError(f"{type(value).__name__}.{name} cannot be used in an expression")
    return getattr(value, name)


class _Guard(ast.NodeTransformer):
    """Turns every `x.name` into `_attr(x, "name")`, and refuses names that start with _."""

    def visit_Name(self, node):
        if node.id.startswith("_"):
            raise ValueError(f"the name {node.id} cannot be used")
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if node.attr.startswith("_") or not isinstance(node.ctx, ast.Load):
            raise ValueError(f"the attribute {node.attr} cannot be used")
        call = ast.Call(func=ast.Name(id="_attr", ctx=ast.Load()), args=[node.value, ast.Constant(node.attr)],
                        keywords=[])
        return ast.copy_location(call, node)


def expression(expr: str | None, flag: str):
    """The value of a Python expression over `m` (a message) or `c` (a chat), as a function of the item, or None when
    there is no expression. `flag` names the option in errors."""
    if not expr:
        return None
    try:
        tree = ast.fix_missing_locations(_Guard().visit(ast.parse(expr, flag, "eval")))
        code = compile(tree, flag, "eval")
    except SyntaxError as e:
        raise ClientError(400, f"{flag} is not a Python expression: {e.msg}") from None
    except ValueError as e:
        raise ClientError(400, f"{flag}: {e}. Expressions see m (or c), plain data and simple functions.") from None

    def value(item: dict):
        item = Item(item)
        item.setdefault("sender", item.get("from"))
        try:  # m and c are globals, so generator expressions inside see them too
            return eval(code, {"__builtins__": {}, **SAFE, "_attr": _attr, "m": item, "c": item})  # noqa: S307
        except Exception as e:  # noqa: BLE001
            raise ClientError(400, f"{flag} failed on {item.get('id')}: {type(e).__name__}: {e}") from None

    return value


def matcher(expr: str | None):
    """A test from a --where expression, or None when there is none."""
    value = expression(expr, "--where")
    return None if value is None else lambda item: bool(value(item))


def sorter(expr: str | None):
    """A function that orders items by a --sort expression, smallest first, or None when there is none."""
    value = expression(expr, "--sort")
    if value is None:
        return None

    def order(items: list) -> list:
        try:
            return sorted(items, key=value)  # stable: equal values keep their order in time
        except TypeError as e:  # e.g. None next to a number
            raise ClientError(400, f"--sort cannot order these values: {e}") from None

    return order


class Client:
    def __init__(self, server: str | None = None, token: str | None = None):
        config = json.loads(_read(os.path.join(CONFIG_DIR, "config.json")) or "{}")
        self.server = server or os.environ.get(f"{ENV}_SERVER") or config.get("server") or "http://127.0.0.1:8484"
        self.token = token or os.environ.get(f"{ENV}_TOKEN") or _read(os.path.join(CONFIG_DIR, "token")) or ""
        url = urllib.parse.urlsplit(self.server if "://" in self.server else f"http://{self.server}")
        self.https = url.scheme == "https"
        self.host, self.port = url.hostname or "127.0.0.1", url.port or (443 if self.https else 80)
        self.conn = None
        self.ack = {}  # chat -> newest seq a wait handed over and not yet confirmed to the server

    def _connect(self, timeout: float):
        if self.https:
            # SWARMCHAT_CA trusts a self-signed server certificate (a closed network has no public authority).
            context = ssl.create_default_context(cafile=os.environ.get(f"{ENV}_CA") or None)
            if os.environ.get(f"{ENV}_INSECURE"):
                context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=context)
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn.connect()
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn

    def _closed_by_server(self) -> bool:
        """True when the kept-open connection can no longer be used: the server closed it (it timed out an idle
        connection, or restarted). Nothing is due on an idle connection, so a readable socket means it has ended.
        Checking first lets a request go out on a fresh connection instead of failing, without ever sending twice."""
        sock = self.conn.sock
        if sock is None:
            return True
        try:
            return bool(select.select([sock], [], [], 0)[0])
        except (OSError, ValueError):
            return True

    def _exchange(self, name: str, body, headers: dict, http_timeout: float, retry_safe: bool, read: bool = True,
                  method: str = "POST"):
        """Sends one request on the kept-open connection, or a new one. (response, its body), the body left unread
        when read is False. Raises ClientError when the server cannot be reached."""
        for attempt in (1, 2):
            if self.conn is not None and self._closed_by_server():
                self._drop()
            reused = self.conn is not None
            try:
                if not reused:
                    try:
                        self.conn = self._connect(http_timeout)
                    except OSError as e:  # nothing was sent, so nothing can have been applied
                        self._drop()
                        raise ClientError(0, f"Cannot reach {self.server}: {e}. Is the server running?") from None
                self.conn.sock.settimeout(http_timeout)
                self.conn.request(method, f"/v1/{name}", body, headers)
                response = self.conn.getresponse()
                if not read:
                    return response, None
                data = self._body(response)
                if response.will_close:
                    self._drop()
                return response, data
            except (OSError, http.client.HTTPException) as e:
                self._drop()
                if not retry_safe or not reused or attempt == 2:
                    detail = " The request may have been applied; it was not retried." if not retry_safe else ""
                    raise ClientError(0, f"Cannot reach {self.server}: {e}.{detail}") from None

    def _body(self, response) -> bytes:
        """A reply's body, refused past MAX_REPLY bytes. Read by its length, or else a piece at a time: before 3.12,
        read(n) zero-fills n bytes first, which for n = MAX_REPLY took longer than the request itself."""
        if response.length is not None and response.length <= MAX_REPLY:
            return response.read()
        parts, size = [], 0
        while response.length is None:
            part = response.read(1 << 16)
            if not part:
                return b"".join(parts)
            parts.append(part)
            size += len(part)
            if size > MAX_REPLY:
                break
        self._drop()
        raise ClientError(0, f"{self.server} sent a reply larger than {MAX_REPLY} bytes.")

    def _result(self, response, data: bytes, acked: bool) -> dict:
        try:
            result = json.loads(data or b"{}")
            if not isinstance(result, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, UnicodeError):
            self._drop()
            raise ClientError(response.status if response.status != 200 else 0,
                              f"Invalid JSON response from {self.server} (HTTP {response.status}).") from None
        if acked and response.status != 401:
            self.ack = {}  # the server applied them before anything else in this request
        if response.status != 200:
            raise ClientError(response.status, result.get("error") or f"HTTP {response.status}")
        return result

    def call(self, name: str, params: dict | None = None, http_timeout: float = 60) -> dict:
        """One request. Raises ClientError when the server refuses it or cannot be reached."""
        params = {**(params or {}), "ack": self.ack or None}  # confirmations ride along with the next request
        body = json.dumps({k: v for k, v in params.items() if v not in (None, False, "", [])}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        retry_safe = name in {"whoami", "people", "chats", "chat_info", "notifications", "settings_show", "policy_show"} \
            or (name in {"read", "wait"} and bool((params or {}).get("peek")))
        response, data = self._exchange(name, body, headers, http_timeout, retry_safe)
        return self._result(response, data, bool(params["ack"]))

    def send(self, chat: str, text: str = "", attach=(), reply_to: str | None = None) -> dict:
        """Sends a message, with files or folders attached if given. Their bytes are streamed to the server, so a
        large file never sits in memory. A folder is zipped on the way and unpacked again on download."""
        if not attach:
            return self.call("send", {"chat": chat, "text": text, "reply_to": reply_to})
        files, temps = [], []
        try:
            for path in attach:
                name = safe_name(os.path.basename(os.path.normpath(path)))
                if os.path.isdir(path):
                    try:
                        temps.append(pack(path))
                    except ValueError as e:
                        raise ClientError(400, f"Not sent: {e}.") from None
                    files.append((temps[-1], {"name": name, "type": "application/zip", "folder": True}))
                elif os.path.isfile(path):
                    files.append((path, {"name": name, "type": mimetypes.guess_type(name)[0] or ""}))
                else:
                    raise ClientError(400, f"There is no file or folder at {path}.")
            for p, meta in files:
                meta["size"] = os.path.getsize(p)
            limits = self.limits()
            if len(files) > limits.get("max_attachments", len(files)):
                raise ClientError(413, f"This server takes at most {limits['max_attachments']} files on one message.")
            for _, meta in files:
                if meta["size"] > limits.get("max_attachment", meta["size"]):
                    raise ClientError(413, f"{meta['name']} is {meta['size']} bytes, over this server's limit of "
                                           f"{limits['max_attachment']} bytes per file.")
            acked = bool(self.ack)
            header = (json.dumps({"chat": chat, "text": text, "reply_to": reply_to, "ack": self.ack or None,
                                  "files": [m for _, m in files]}) + "\n").encode()

            def body():
                yield header
                for p, _ in files:
                    with open(p, "rb") as f:
                        for chunk in iter(lambda: f.read(CHUNK), b""):
                            yield chunk

            headers = {"Content-Type": "application/octet-stream", "Authorization": f"Bearer {self.token}",
                       "Content-Length": str(len(header) + sum(m["size"] for _, m in files))}
            response, data = self._exchange("send_files", body(), headers, 60, retry_safe=False)
            return self._result(response, data, acked)
        finally:
            for t in temps:
                os.unlink(t)

    def limits(self) -> dict:
        """The server's limits on messages and attachments, so a file that is too large is refused before sending."""
        response, data = self._exchange("health", None, {}, 30, retry_safe=True, method="GET")
        return json.loads(data or b"{}").get("limits", {}) if response.status == 200 else {}

    def download(self, attachment_id: str, to: str = ".", force: bool = False) -> str:
        """Saves an attachment into the folder `to` under its own name, and returns that path. A folder attachment is
        unpacked into a folder of that name. Never replaces an existing file or folder unless force is set."""
        acked = bool(self.ack)
        body = json.dumps({"id": attachment_id, **({"ack": self.ack} if acked else {})}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        response, _ = self._exchange("download", body, headers, 60, retry_safe=True, read=False)
        if response.status != 200:
            data = response.read()
            if response.will_close:
                self._drop()
            self._result(response, data, acked)  # raises the server's error
        if acked:
            self.ack = {}
        try:
            info = json.loads(response.getheader("X-Attachment") or "{}")
            target = os.path.join(to, safe_name(info.get("name")))
            if os.path.lexists(target) and not force:
                raise ClientError(400, f"{target} already exists. Use --force to replace it.")
            os.makedirs(to, exist_ok=True)
            size = int(response.getheader("Content-Length") or 0)
            if size != info.get("size"):
                raise ClientError(0, f"{self.server} sent {size} bytes for a file of {info.get('size')}.")
            free = shutil.disk_usage(to).free
            if size > free - SPARE:
                raise ClientError(507, f"{info.get('name')} is {size:,} bytes, and this disk has {free:,} free.")
            stale = time.time() - 300  # a live download writes at least every minute, so older ones were killed
            for name in os.listdir(to):
                leftover = os.path.join(to, name)
                if name.startswith(".swarmchat-") and name.endswith((".part", ".unpack")) and \
                        os.path.getmtime(leftover) < stale:
                    shutil.rmtree(leftover) if os.path.isdir(leftover) else os.unlink(leftover)
            fd, tmp = tempfile.mkstemp(dir=to, prefix=".swarmchat-", suffix=".part")
            try:
                with os.fdopen(fd, "wb") as f:
                    left = size
                    while left:
                        chunk = response.read(min(CHUNK, left))
                        if not chunk:
                            raise ClientError(0, "The download ended before all its bytes arrived.")
                        f.write(chunk)
                        left -= len(chunk)
                if os.path.lexists(target):  # force
                    shutil.rmtree(target) if os.path.isdir(target) and not os.path.islink(target) else os.unlink(target)
                if info.get("folder"):
                    # Unpacked out of sight, then put in place whole: a folder that fails part way leaves nothing.
                    staging = tempfile.mkdtemp(dir=to, prefix=".swarmchat-", suffix=".unpack")
                    try:
                        unpack(tmp, staging)
                        os.replace(staging, target)
                    except (ValueError, OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as e:
                        raise ClientError(400, f"Not unpacked: {e}") from None
                    finally:
                        if os.path.exists(staging):
                            shutil.rmtree(staging)
                else:
                    os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except BaseException:
            self._drop()  # the rest of the reply was not read, so this connection cannot be reused
            raise
        if response.will_close:
            self._drop()
        return target

    def interrupt(self):
        """Breaks off what this client is waiting for, from another thread: a read blocked on the connection fails
        at once. Shutting the socket down wakes such a read on every platform, where closing it does not."""
        sock = self.conn.sock if self.conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _drop(self):
        """Closes the connection, nothing else. Used inside call(), so it must never make a request."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def close(self):
        """Confirms any messages a wait handed over, then closes the connection."""
        if self.ack:
            try:
                self.call("mark_read")
            except ClientError:
                pass  # not confirmed: those messages stay unread and come again
        self._drop()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **params: self.call(name, params)

    def mark_messages_read(self, messages: list, chat: str | None = None):
        """Marks these messages read, and every unread one before them in their chats, up to the first one that is
        not among them: messages a filter left out stay unread."""
        shown = {}
        for m in messages:
            shown.setdefault(m["chat"], []).append(m["seq"])
        if shown:
            return self.call("mark_read", {"shown": shown, "chat": chat})

    def read(self, where: str | None = None, peek: bool = False, limit: int | None = None, sort: str | None = None,
             **select) -> dict:
        """Messages are marked read only after they have arrived here, so a read cut off on the way (a killed
        command, a lost connection) never uses up messages: they stay unread and come again.

        `where` and `sort` run here. The other options choose the messages, `sort` orders them, and `limit` keeps
        that many from the top: sort="-len(m.reactions or {})", limit=10 is the ten with the most reactions.
        The server gives a page at a time, so this asks for more pages while it needs them: until it has enough
        matches, or every candidate when they are to be sorted."""
        test, order, last = matcher(where), sorter(sort), select.get("last")
        if test is not None and last:  # with a filter, "the last N" means the last N that match
            select["last"], select["all"] = None, True
        whole = test is not None or (order is not None and not last)  # every candidate comes, to filter or order
        params = {**select, "peek": True, "limit": 10 ** 9 if whole else None if order else limit}
        result = self.call("read", params)
        found, keep, titles, thread = [], result["keep"], {}, bool(select.get("thread"))
        # How many messages it takes, or None for every one: to sort them, or a whole thread.
        if test is not None:  # the last N that match, or enough matches to show
            enough = last or (None if order is not None else limit or (None if thread else 50 if keep == "first" else 20))
        elif order is not None:  # the last N to sort, or every candidate
            enough = last
        else:
            enough = limit or last
        while True:
            page = [m for m in result["messages"] if test is None or test(m)]
            found = found + page if keep == "first" else page + found  # history pages go back in time
            titles.update(result.get("chats") or {})
            if not result.get("more") or (len(found) >= enough if enough is not None else not (whole or thread)):
                break
            result = self.call("read", {**params, "cursor": result["cursor"]})
        if enough is not None and len(found) > enough:
            found = found[:enough] if keep == "first" else found[-enough:]
        if order is not None:
            found, keep = order(found), "first"
        if whole or order is not None:
            n = limit or (len(found) if last or select.get("thread") else 50 if keep == "first" else 20)
            found = found[:n] if keep == "first" else found[-n:]
        if not peek and found and not select.get("thread"):  # a thread moves no read position, see api.read
            scope = found[0]["chat"] if select.get("id") else select.get("chat")
            marked = self.mark_messages_read(found, scope)
            result["unread_left"] = marked.get("unread_left", result["unread_left"])
        return {**result, "messages": found, "chats": titles}

    def chats(self, where: str | None = None, limit: int | None = None, **select) -> dict:
        test = matcher(where)
        if test is None:
            return self.call("chats", {**select, "limit": limit})
        found = [c for c in self.call("chats", {**select, "limit": 10 ** 9})["chats"] if test(c)]
        return {"chats": found[:limit or 20]}

    def wait(self, timeout: float | None = None, where: str | None = None, peek: bool = False, **select) -> dict:
        """Blocks until a matching message arrives, for at most `timeout` seconds (None: forever). On a timeout the
        reply's messages list is empty. Messages are marked read only once they have arrived here: the client
        confirms them with its next request, or when it closes. If it never does, they stay unread and come again."""
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ClientError(400, "timeout must be a finite number of seconds at least 0.")
        test, after = matcher(where), None
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            hold = HOLD if deadline is None else max(0.0, min(HOLD, deadline - time.monotonic()))
            result = self.call("wait", {**select, "timeout": hold or 0.001, "after": after, "peek": True},
                               http_timeout=hold + 30)
            after = {**(after or {}), **result["positions"]}
            found = [m for m in result["messages"] if test is None or test(m)]
            if found:
                if not peek:  # confirmed with the next request (or close), so a wait cut off loses nothing
                    for m in found:
                        self.ack[m["chat"]] = max(self.ack.get(m["chat"], 0), m["seq"])
                return {**result, "messages": found}
            if deadline is not None and time.monotonic() >= deadline:
                return {**result, "messages": []}
