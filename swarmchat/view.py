"""What `swarmchat view` asks the server: read-only requests for the operator's viewer, a web page served at /view.

Each request takes the store and its parameters and returns a JSON-ready dict, like api.py. They see every chat and
change nothing: no read position moves and no line is written. Seeing as an agent calls the same functions the CLI
calls, as that agent, with nothing marked read, so it shows exactly what the agent would get.
"""
from __future__ import annotations

import base64
import bisect
import codecs
import collections
import gzip
import hashlib
import heapq
import json
import math
import os
import struct
import threading
import time

from . import NAME, __version__, api
from .store import Chat, Error, Store, epoch

PREVIEW = 160  # characters of a message shown in a chat list or under a reply
RECENT = 900  # seconds counted as recent activity
MAX_FEED = 5000  # events in one feed reply. A viewer further behind simply asks again at once.
IMAGE_BYTES = 25_000_000  # a larger picture is offered for download, not drawn in the page
IMAGE_PIXELS = 25_000_000  # a 24-megapixel photo. Drawn, a picture takes 4 bytes a pixel in the browser.
TEXT_HEAD = 16_384  # bytes read from the start of a file to tell whether it is text, and to preview it
TEXT_LINES = 8  # lines of a text file shown under its message. Kept in memory for each file, so few.
TEXT_WIDTH = 200  # characters of each of those lines
TEXT_BYTES = 5_000_000  # of a text file shown whole in the page. Past that, it shows the start and offers a download.


def _int(p: dict, key: str, default: int, most: int) -> int:
    value = p.get(key)
    return default if value in (None, "") else max(0, min(most, int(value)))


def _chat(store: Store, ref) -> Chat:
    chat = store.chats.get(str(ref or "").strip())
    if not chat:
        raise Error(404, f"There is no chat {ref}.")
    return chat


def _user(store: Store, ref) -> str:
    user = str(ref or "").strip().lower()
    if user not in store.users:
        raise Error(404, f"There is no user {ref}.")
    return user


def _kind(chat: Chat) -> str:
    return "channel" if chat.name else "direct" if len(chat.members) == 2 else "group"


def _short(m: dict) -> dict:
    text = "" if m.get("deleted") else m.get("text", "")
    return {"seq": m["seq"], "from": m["from"], "at": m["at"], "text": text[:PREVIEW], "deleted": bool(m.get("deleted")),
            "files": len(m.get("attachments") or ())}


_tally_lock = threading.Lock()


def _tally(chat: Chat) -> tuple:
    """({user: messages sent}, {user: when they last wrote, in seconds}) for a chat. Worked out when first asked,
    then kept up to date from the messages written since, so agents' sends never pay for it."""
    with _tally_lock:
        done, sent, last = chat.cache.get("tally") or (0, {}, {})
        n = len(chat.times)
        if done < n:
            senders, times = chat.senders, chat.times
            for i in range(done, n):
                u = senders[i]
                sent[u] = sent.get(u, 0) + 1
                last[u] = times[i]
            chat.cache["tally"] = (n, sent, last)
        return sent, last


def _summary(store: Store, chat: Chat) -> dict:
    """A chat for the viewer's lists. Taken under the chat's lock, so its numbers all belong to its line `seq`: a feed
    event with a higher seq is new to the viewer, and one at or below it is already counted here."""
    with chat.lock:
        last = chat.view(chat.last) if chat.last else None
        sent, last_by = _tally(chat)
        return {"id": chat.id, "kind": _kind(chat), "name": chat.name, "description": chat.description,
                "public": chat.public, "archived": chat.archived, "members": list(chat.members),
                "managers": chat.managers, "created_by": chat.meta["created_by"],
                "created_at": chat.meta["created_at"], "seq": chat.seq, "messages": len(chat.messages),
                "last_at": chat.at, "last": _short(last) if last else None, "sent": dict(sent),
                "last_by": dict(last_by)}


def _person(store: Store, user: str) -> dict:
    if user in store.users:
        return store.public(user)
    return {"id": user, "name": user, "fields": {}, "status": "", "removed": True}


def _message(store: Store, chat: Chat, e: dict, parents: dict) -> dict:
    m = chat.view(e)
    _files(store, chat, m)
    m["replies"] = len(chat.replies.get(e["seq"], ()))
    parent = chat.parent.get(e["seq"])
    if parent is not None and parent in parents:
        m["parent"] = _short(chat.view(parents[parent]))
    return m


def _messages(store: Store, chat: Chat, events: list) -> list:
    """Messages and system lines as the viewer shows them. A reply carries the start of the message it answers."""
    have = {e["seq"]: e for e in events}
    wanted = sorted({chat.parent[e["seq"]] for e in events if e["seq"] in chat.parent} - set(have))
    parents = {**have, **{e["seq"]: e for e in chat.read(wanted)}}
    out = []
    for e in events:
        if e["kind"] == "message":
            out.append(_message(store, chat, e, parents))
        else:
            out.append({"id": f"{chat.id}:{e['seq']}", "chat": chat.id, **e})
    return out


# ---------------------------------------------------------------------- pictures and text files

def _files(store: Store, chat: Chat, m: dict):
    """Gives each attachment of a message that the page can show in place what the page needs for it. A picture gets
    its `image`: type, width and height, so the page can make room for it before its bytes arrive. A text file gets its
    `text`: its first lines. Worked out once for each stored file."""
    if not m.get("attachments"):
        return
    known = chat.cache.setdefault("files", {})
    for a in m["attachments"]:
        stored = chat.files.get(a["id"])
        if not stored or a.get("folder"):
            continue
        sha = stored[1]["sha256"]
        if sha not in known:
            path = store.blobs.path(sha)
            image = _image(path) if a["size"] <= IMAGE_BYTES else None
            text = None if image else _text_head(path)
            known[sha] = {"image": image} if image else {"text": text} if text else {}
        a.update(known[sha])


def _text_head(path: str) -> dict | None:
    """The first lines of a file that is text: UTF-8, with no NUL byte in its first TEXT_HEAD bytes. `lines` counts
    them all when those bytes are the whole file, and `more` says whether there is more than the preview. None for
    anything else, and for an empty file. The bytes decide, never the name, so a binary file is never shown as text."""
    try:
        with open(path, "rb") as f:
            head = f.read(TEXT_HEAD)
            whole = not f.read(1)
    except OSError:
        return None
    if not head or b"\0" in head:
        return None
    try:  # unless this is the whole file, a character cut off at the end of what was read is not a fault
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=whole)
    except UnicodeDecodeError:
        return None
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").split("\n")
    if lines[-1] == "" and len(lines) > 1:
        lines.pop()  # the newline that ends the last line starts no new one
    shown = lines[:TEXT_LINES]
    out = {"preview": "\n".join(line[:TEXT_WIDTH] for line in shown),
           "more": not whole or len(lines) > TEXT_LINES or any(len(line) > TEXT_WIDTH for line in shown)}
    if whole:
        out["lines"] = len(lines)
    return out


def _image(path: str) -> dict | None:
    """The type and size in pixels of a PNG, JPEG, GIF, WebP or BMP, read from the start of its file. The bytes
    decide, never the type the sender gave, so only real pictures are drawn. None for anything else."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                kind, (w, h) = "image/png", struct.unpack(">II", head[16:24])
            elif head[:6] in (b"GIF87a", b"GIF89a"):
                kind, (w, h) = "image/gif", struct.unpack("<HH", head[6:10])
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                kind, (w, h) = "image/webp", _webp(head)
            elif head[:2] == b"BM" and struct.unpack("<I", head[14:18])[0] in (12, 40, 52, 56, 108, 124):  # its header's size
                w, h = struct.unpack("<HH" if head[14] == 12 else "<ii", head[18:22 if head[14] == 12 else 26])
                kind, h = "image/bmp", abs(h)  # a negative height means the rows run top down
            elif head[:3] == b"\xff\xd8\xff":
                kind, (w, h) = "image/jpeg", _jpeg(f)
            else:
                return None
    except (OSError, ValueError, IndexError, struct.error):
        return None
    if not (w > 0 and h > 0 and w * h <= IMAGE_PIXELS):
        return None
    return {"type": kind, "width": w, "height": h}


def _webp(head: bytes) -> tuple:
    chunk = head[12:16]
    if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":  # lossy
        w, h = struct.unpack("<HH", head[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and head[20] == 0x2F:  # lossless: 14 bits each, less one
        b = head[21:25]
        return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | (b[1] >> 6))
    if chunk == b"VP8X":  # extended: 24 bits each, less one
        return 1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little")
    raise ValueError("not a WebP picture")


FRAMES = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # JPEG markers that start a frame header


def _jpeg(f) -> tuple:
    """(width, height) of a JPEG as a browser draws it: turned when its EXIF orientation says so. Walks the
    segments from the start, skipping each by its length, until the frame header."""
    f.seek(2)
    turned = False
    while True:
        if f.read(1) != b"\xff":
            raise ValueError("not a JPEG segment")
        marker = f.read(1)
        while marker == b"\xff":  # fill bytes
            marker = f.read(1)
        if not marker or marker[0] in (0xD9, 0xDA):
            raise ValueError("no frame header before the picture's data")
        if marker[0] == 0x01 or 0xD0 <= marker[0] <= 0xD8:  # markers with no length
            continue
        length = struct.unpack(">H", f.read(2))[0]
        if marker[0] in FRAMES:
            h, w = struct.unpack(">xHH", f.read(5))
            return (h, w) if turned else (w, h)
        if marker[0] == 0xE1:
            data = f.read(length - 2)
            if data[:6] == b"Exif\0\0":
                turned = _orientation(data[6:]) in (5, 6, 7, 8)
        else:
            f.seek(length - 2, 1)


def _orientation(tiff: bytes) -> int:
    """The EXIF orientation tag, 1 to 8, from the TIFF block of a JPEG's EXIF segment. 1 when it has none."""
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    try:
        ifd = struct.unpack(order + "I", tiff[4:8])[0]
        for i in range(struct.unpack(order + "H", tiff[ifd:ifd + 2])[0]):
            at = ifd + 2 + 12 * i
            if struct.unpack(order + "H", tiff[at:at + 2])[0] == 0x0112:
                return struct.unpack(order + "H", tiff[at + 8:at + 10])[0]
    except (TypeError, struct.error):
        pass
    return 1


# ---------------------------------------------------------------------- the whole server at once

def view_info(store: Store, p: dict) -> dict:
    return {"name": NAME, "version": __version__, "read_only": store.read_only, "data": os.path.abspath(store.root),
            "feed": store.feed.count, "feed_id": store.feed.id}


def view_snapshot(store: Store, p: dict) -> dict:
    """Every user and chat. `feed` is taken first: whatever happens after it comes through view_feed, and whatever is
    already in this snapshot is recognised there by its chat's seq."""
    position = store.feed.count
    everything = list(store.chats.values())
    chats = [_summary(store, c) for c in everything]
    # How busy each chat was in the 15 minutes before the newest message anywhere: the sidebar draws it as a bar.
    newest = max((c.times[-1] for c in everything if c.times), default=0.0)
    for c, summary in zip(everything, chats):
        summary["recent"] = len(c.times) - bisect.bisect_left(c.times, newest - RECENT)
    users = {u: _person(store, u) for u in sorted(store.users)}
    for c in chats:  # people removed since still appear in the history
        for u in [*c["members"], *c["sent"]]:
            if u not in users:
                users[u] = _person(store, u)
    return {"feed": position, "server": view_info(store, p), "users": list(users.values()), "chats": chats,
            "now": time.time()}


def view_feed(store: Store, p: dict, gone=lambda: False) -> dict:
    """Everything after the feed number `after`, held open like a wait until something happens or `timeout`
    passes. reset means the viewer should take a new snapshot: it fell too far behind, or the server started again
    since (`feed_id` from the snapshot differs) and numbers its feed from the start."""
    after = int(p.get("after") or 0)
    if after > store.feed.count or (p.get("feed_id") and p["feed_id"] != store.feed.id):
        return {"feed": store.feed.count, "events": [], "reset": True}
    timeout = min(float(p.get("timeout") if p.get("timeout") is not None else 25), store.settings["max_wait"])
    deadline = time.monotonic() + max(0.0, timeout)
    cond = store.feed.cond
    with cond:
        while True:
            entries = store.feed.after(after)
            left = deadline - time.monotonic()
            if entries is None or entries or left <= 0:
                break
            if not cond.wait(min(left, 1.0)) and gone():
                return {"feed": after, "events": []}
    if entries is None:
        return {"feed": store.feed.count, "events": [], "reset": True}
    entries = entries[:MAX_FEED]
    return {"feed": entries[-1][0] if entries else after, "events": _events(store, entries)}


def _events(store: Store, entries: list) -> list:
    """Feed entries as the viewer applies them. Edits, deletions and reactions carry the message as it now stands."""
    targets = collections.defaultdict(set)
    for _, chat, e in entries:
        if chat is not None and e["kind"] in ("edit", "delete", "react"):
            targets[chat].add(e["target"])
    changed = {}
    for chat, seqs in targets.items():
        for e in chat.read(sorted(seqs)):
            changed[chat.id, e["seq"]] = e
    replies = collections.defaultdict(list)
    for _, chat, e in entries:
        if chat is not None and e["kind"] == "message":
            replies[chat].append(e)
    shown = {}
    for chat, events in replies.items():
        for m in _messages(store, chat, events):
            shown[chat.id, m["seq"]] = m
    out = []
    for n, chat, e in entries:
        kind = e["kind"]
        if chat is None:
            out.append({"n": n, "kind": "user", "user": _person(store, e["id"])})
        elif kind == "message":
            count = bisect.bisect_left(chat.messages, e["seq"]) + 1  # its place among the chat's messages
            out.append({"n": n, "kind": "message", "chat": chat.id, "seq": e["seq"], "count": count,
                        "message": shown[chat.id, e["seq"]]})
        elif kind in ("edit", "delete", "react"):
            target = changed[chat.id, e["target"]]
            out.append({"n": n, "kind": "change", "chat": chat.id, "seq": e["seq"], "change": kind, "from": e["from"],
                        "message": _messages(store, chat, [target])[0]})
        elif kind == "read":
            out.append({"n": n, "kind": "read", "chat": chat.id, "seq": e["seq"], "user": e["from"], "upto": e["upto"],
                        "at": e["at"]})
        else:
            out.append({"n": n, "kind": "chat", "chat": chat.id, "seq": e["seq"], "event": e,
                        "summary": _summary(store, chat)})
    return out


# ---------------------------------------------------------------------- one chat

def view_messages(store: Store, p: dict) -> dict:
    """A run of a chat's messages, oldest first: the newest `limit`, or those just `before` or `after` a seq, or
    those `around` one. With them, the system lines in the same stretch and every member's read position."""
    chat = _chat(store, p.get("chat"))
    limit = _int(p, "limit", 100, 500) or 1
    seqs = chat.messages
    n = len(seqs)
    if p.get("around") is not None:
        i = bisect.bisect_left(seqs, int(p["around"]))
        lo = max(0, min(i - limit // 2, n - limit))
        hi = min(n, lo + limit)
    elif p.get("before") is not None:
        hi = bisect.bisect_left(seqs, int(p["before"]))
        lo = max(0, hi - limit)
    elif p.get("after") is not None:
        lo = bisect.bisect_right(seqs, int(p["after"]))
        hi = min(n, lo + limit)
    else:
        hi, lo = n, max(0, n - limit)
    wanted = list(seqs[lo:hi])
    lines = chat.system  # the chat's start comes with its first page, and the newest changes with its last
    first = 0 if lo == 0 else bisect.bisect_left(lines, wanted[0])
    last = len(lines) if hi == n else bisect.bisect_right(lines, wanted[-1])
    events = chat.read(sorted([*wanted, *lines[first:last]]))
    return {"chat": chat.id, "messages": _messages(store, chat, events), "first": lo == 0, "last": hi == n,
            "index": lo, "total": n, "positions": _positions(store, chat)}


def _positions(store: Store, chat: Chat) -> dict:
    return {u: store.position(u, chat) for u in chat.members if u in store.state}


def view_thread(store: Store, p: dict) -> dict:
    """The whole thread a message is in, each message with its depth, like `swarmchat read --thread`."""
    chat, tree = store.thread(None, str(p.get("id") or ""))
    depth = dict(tree)
    messages = _messages(store, chat, chat.read([s for s, _ in tree]))
    for m in messages:
        m["depth"] = depth[m["seq"]]
    return {"chat": chat.id, "messages": messages, "positions": _positions(store, chat)}


def view_receipts(store: Store, p: dict) -> dict:
    """Who has read a message and when, and who has not."""
    chat, e = store.message(None, str(p.get("id") or ""))
    read, unread = [], []
    for u in chat.members:
        if u == e["from"]:
            continue
        at = chat.read_at(u, e["seq"])
        if at or (u in store.state and store.position(u, chat) >= e["seq"]):
            read.append({"id": u, "at": at})
        else:
            unread.append(u)
    read.sort(key=lambda r: r["at"] or "")
    return {"id": f"{chat.id}:{e['seq']}", "read": read, "unread": unread}


# ---------------------------------------------------------------------- boards

def _board(chat: Chat) -> list:
    """(seq, time, replies under it, votes, time of its newest reply) for each top-level message. Worked out in one
    pass from the newest message back, since a reply always comes after what it answers, and kept until the chat
    has a new line."""
    seq_now = chat.seq
    cached = chat.cache.get("board")
    if cached and cached[0] == seq_now:
        return cached[1]
    n = len(chat.times)
    messages, times, parent = chat.messages[:n], chat.times[:n], chat.parent
    size, newest = {}, {}
    for seq in reversed(messages):
        up = parent.get(seq)
        if up is not None:
            size[up] = size.get(up, 0) + 1 + size.get(seq, 0)
            newest[up] = max(newest.get(up, up), newest.get(seq, seq))
    posts = []
    for i, seq in enumerate(messages):
        if seq in parent:
            continue
        votes = sum(len(users) for users in chat.changes.get(seq, {}).get("reactions", {}).values())
        last = newest.get(seq, seq)
        posts.append((seq, times[i], size.get(seq, 0), votes, times[bisect.bisect_left(messages, last)]))
    chat.cache["board"] = (seq_now, posts)
    return posts


def view_posts(store: Store, p: dict) -> dict:
    """Top-level messages as posts, from one chat or from every channel and group (the front page), sorted hot, new,
    top or active. With `as`, only chats that agent is in."""
    sort = p.get("sort") or "hot"
    if sort not in ("hot", "new", "top", "active"):
        raise Error(400, "sort is hot, new, top or active.")
    limit, offset = _int(p, "limit", 30, 200) or 1, _int(p, "offset", 0, 10 ** 9)
    if p.get("chat"):
        chats = [_chat(store, p["chat"])]
    else:
        chats = [c for c in list(store.chats.values()) if _kind(c) != "direct"]
    if p.get("as"):
        user = _user(store, p["as"])
        chats = [c for c in chats if user in c.members]
    newest = max((c.times[-1] for c in chats if c.times), default=time.time())

    def hot(post):
        seq, at, replies, votes, _ = post
        return (votes + replies + 1) / ((max(0.0, newest - at) / 3600 + 2) ** 1.5), at, seq

    # Each key ends in the time and the seq, so posts written in the same millisecond keep their order.
    key = {"hot": hot, "new": lambda post: (post[1], post[0]), "top": lambda post: (post[3] + post[2], post[1], post[0]),
           "active": lambda post: (post[4], post[0])}[sort]
    candidates = ((key(post), c.id, post) for c in chats for post in _board(c))
    top = heapq.nlargest(offset + limit, candidates, key=lambda x: x[0])[offset:]
    by_chat = collections.defaultdict(list)
    for _, chat_id, post in top:
        by_chat[chat_id].append(post[0])
    events = {}
    for chat_id, seqs in by_chat.items():
        chat = store.chats[chat_id]
        for m in _messages(store, chat, chat.read(sorted(seqs))):
            events[chat_id, m["seq"]] = m
    posts = []
    for _, chat_id, (seq, at, replies, votes, last_at) in top:
        posts.append({**events[chat_id, seq], "comments": replies, "votes": votes, "active_at": last_at})
    return {"posts": posts, "more": len(top) == limit}


# ---------------------------------------------------------------------- search

def _candidates(chat: Chat, needle: str, too_old=lambda seq: False):
    """(the line's seq, the message's seq, the message or None) for each line whose text may hold `needle`, in lower
    case, newest line first. An edit gives the message it changes. The raw log is read from its end in growing
    pieces, so a search that finds enough in the newest lines reads only those. A hit counts only after a line's
    "text" key, so ids and times never match. too_old(seq) ends it before a piece whose newest line is too old."""
    escaped = json.dumps(needle, ensure_ascii=False)[1:-1]  # as the text is written in the log
    raw = escaped.encode()
    for file_no in range(len(chat.offsets), 0, -1):
        offsets = chat.offsets[file_no - 1]
        first = (file_no - 1) * chat.segment + 1  # the seq of the file's first line
        try:  # a bare descriptor: a search may open a file in each of thousands of chats
            fd = os.open(chat.path(file_no), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            continue
        try:
            hi, size = len(offsets), 64
            while hi > 0:
                if too_old(first + hi - 1):
                    return
                lo = max(0, hi - size)
                size *= 4
                end = offsets[hi] if hi < len(offsets) else os.fstat(fd).st_size
                data = _read_at(fd, offsets[lo], end - offsets[lo])
                if hi == len(offsets):  # a line being written right now is not a line yet
                    data = data[:data.rfind(b"\n") + 1]
                base = offsets[lo]
                lines = []
                if raw.isascii():  # lower() on bytes changes only ASCII, so positions stay those of the file
                    low = data.lower()
                    i = low.find(raw)
                    while i >= 0:
                        line = bisect.bisect_right(offsets, base + i, lo, hi) - 1 - lo
                        start = offsets[lo + line] - base
                        stop = data.find(b"\n", start) + 1
                        key = low.find(b'"text":"', start, stop)
                        if 0 <= key < i:
                            lines.append((line, start, stop))
                            i = low.find(raw, stop)
                        else:
                            i = low.find(raw, i + 1)
                else:
                    for line in range(hi - lo):
                        start = offsets[lo + line] - base
                        stop = data.find(b"\n", start) + 1
                        if escaped in data[start:stop].decode("utf-8", "replace").lower():
                            lines.append((line, start, stop))
                for line, start, stop in reversed(lines):
                    seq = first + lo + line
                    e = json.loads(data[start:stop])
                    if e["kind"] == "message":
                        yield seq, seq, e
                    elif e["kind"] == "edit":
                        target = e["target"] - first - lo  # the message it edits, often in this same piece
                        if 0 <= target < hi - lo:
                            start = offsets[lo + target] - base
                            yield seq, e["target"], json.loads(data[start:data.find(b"\n", start) + 1])
                        else:
                            yield seq, e["target"], None
                hi = lo
        finally:
            os.close(fd)


def _read_at(fd: int, offset: int, size: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)  # Windows has no pread
    parts = []
    while size > 0:
        chunk = os.read(fd, size)
        if not chunk:
            break
        parts.append(chunk)
        size -= len(chunk)
    return b"".join(parts)


def view_search(store: Store, p: dict) -> dict:
    """Messages, newest first, from every chat, or only `chats`, or only an agent's chats with `as`. `text` is found
    in any case, in the text as it now stands. `from`, `mentions`, `files`, `after` and `before` narrow it, as in
    `swarmchat read`. For the next page, pass the reply's `until`.

    Chats are visited newest first, and each from its newest message back, keeping the best `limit` found so far.
    Once nothing older could still make the list, the search stops: a common word costs a few recent files."""
    text = (p.get("text") or "").strip().lower()
    limit = _int(p, "limit", 50, 500) or 1
    chats = [_chat(store, c) for c in p["chats"]] if p.get("chats") else list(store.chats.values())
    if p.get("as"):
        user = _user(store, p["as"])
        chats = [c for c in chats if user in c.members]
    senders = set(p.get("from") or ())
    after = epoch(p["after"]) if p.get("after") else None
    before = epoch(p["before"]) if p.get("before") else None
    until = tuple(p["until"]) if p.get("until") else None
    best, events = [], {}  # best: a heap of (time, chat id, seq), the oldest on top. events: what was read already
    chats = sorted((c for c in chats if c.times), key=lambda c: c.times[-1], reverse=True)

    def floor() -> float:
        return best[0][0] if len(best) >= limit else -math.inf

    def offer(t: float, chat: Chat, seq: int, e: dict | None):
        item = (t, chat.id, seq)
        if until is not None and item >= until:
            return
        if len(best) < limit:
            heapq.heappush(best, item)
        elif item > best[0]:
            events.pop(heapq.heapreplace(best, item)[1:], None)
        else:
            return
        if e is not None:
            events[chat.id, seq] = e

    for chat in chats:
        if chat.times[-1] < floor():
            break  # every chat after this one is older still
        times = chat.times[:]  # a copy, and the messages it covers: an agent may be writing to this chat
        msgs = chat.messages[:len(times)]
        if senders and not senders & set(_tally(chat)[0]):
            continue
        mentioned = set(chat.mentions.get(str(p["mentions"]), ())) if p.get("mentions") else None
        with_files = {seq for seq, _ in chat.files.values()} if p.get("files") else None
        if mentioned == set() or with_files == set():
            continue

        def fits(i: int, seq: int) -> bool:
            at = times[i]
            return not ((after is not None and at < after) or (before is not None and at >= before)
                        or (senders and chat.senders[i] not in senders)
                        or (mentioned is not None and seq not in mentioned)
                        or (with_files is not None and seq not in with_files))

        if text:
            seen = set()

            def too_old(line: int) -> bool:  # every line up to this one is older than all of the best
                i = bisect.bisect_right(msgs, line) - 1
                bound = times[i] if i >= 0 else -math.inf
                return bound < floor() or (after is not None and bound < after)

            for line, seq, e in _candidates(chat, text, too_old):
                if too_old(line):
                    break
                if seq in seen:
                    continue
                seen.add(seq)
                i = bisect.bisect_left(msgs, seq)
                if i >= len(msgs) or msgs[i] != seq or not fits(i, seq):
                    continue
                e = e or chat.read([seq])[0]
                m = chat.view(e)
                if m.get("deleted") or text not in m["text"].lower():
                    continue
                offer(times[i], chat, seq, e)
        else:
            if mentioned is not None or with_files is not None:
                pool = sorted(mentioned if mentioned is not None else with_files, reverse=True)
                places = ((i, seq) for i, seq in ((bisect.bisect_left(msgs, seq), seq) for seq in pool) if i < len(msgs))
            else:
                top = len(msgs)
                if before is not None:
                    top = bisect.bisect_left(times, before)
                if until is not None:
                    top = min(top, bisect.bisect_right(times, until[0]))
                places = ((i, msgs[i]) for i in range(top - 1, -1, -1))
            for i, seq in places:
                if times[i] < floor() or (after is not None and times[i] < after):
                    break
                if fits(i, seq):
                    offer(times[i], chat, seq, None)
    found = sorted(best, reverse=True)
    by_chat = collections.defaultdict(list)
    for _, chat_id, seq in found:
        if (chat_id, seq) not in events:
            by_chat[chat_id].append(seq)
    for chat_id, seqs in by_chat.items():
        for e in store.chats[chat_id].read(sorted(seqs)):
            events[chat_id, e["seq"]] = e
    shown = {}
    grouped = collections.defaultdict(list)
    for _, chat_id, seq in found:
        grouped[chat_id].append(events[chat_id, seq])
    for chat_id, evs in grouped.items():
        for m in _messages(store, store.chats[chat_id], evs):
            shown[chat_id, m["seq"]] = m
    return {"messages": [shown[chat_id, seq] for _, chat_id, seq in found],
            "until": list(found[-1]) if len(found) == limit else None}


# ---------------------------------------------------------------------- over time

def view_activity(store: Store, p: dict) -> dict:
    """Messages over time: `buckets` counts from the first message to the last, across every chat or the chats an
    agent is in (`as`). With `from` and `to` (seconds since 1970), how many messages each chat had in that stretch,
    and from whom."""
    chats = list(store.chats.values())
    if p.get("as"):
        user = _user(store, p["as"])
        chats = [c for c in chats if user in c.members]
    chats = [c for c in chats if c.times]
    if not chats:
        return {"start": None, "end": None, "width": None, "counts": [], "window": {}}
    buckets = _int(p, "buckets", 200, 2000) or 1
    first, last = min(c.times[0] for c in chats), max(c.times[-1] for c in chats)
    width = max((last - first) / buckets, 0.001)
    counts = [0] * buckets
    for c in chats:
        times = c.times
        if len(times) <= 4 * buckets:
            for t in times:
                counts[max(0, min(buckets - 1, int((t - first) / width)))] += 1
        else:  # a long chat: where each bucket ends, found by halving, instead of every message
            lo = 0
            for b in range(buckets):
                hi = len(times) if b == buckets - 1 else bisect.bisect_left(times, first + (b + 1) * width, lo)
                counts[b] += hi - lo
                lo = hi
    out = {"start": first, "end": last, "width": width, "counts": counts}
    if p.get("from") is not None and p.get("to") is not None:
        start, end = float(p["from"]), float(p["to"])
        window = {}
        for c in chats:
            i, j = bisect.bisect_left(c.times, start), bisect.bisect_right(c.times, end)
            if j > i:
                sent = dict(_tally(c)[0]) if i == 0 and j == len(c.times) else dict(collections.Counter(c.senders[i:j]))
                window[c.id] = {"messages": j - i, "sent": sent}
        out["window"] = window
    return out


# ---------------------------------------------------------------------- as one agent

def view_as(store: Store, p: dict) -> dict:
    """What one agent sees: its chats exactly as `swarmchat chats` lists them, its read position in each, and with
    `unread`, what `swarmchat read` would hand it now. Nothing is marked read."""
    user = _user(store, p.get("user"))
    out = {"user": store.public(user), "chats": api.chats(store, user, {"limit": 10 ** 9})["chats"],
           "positions": {c.id: store.position(user, c) for c in store.my_chats(user)}}
    if p.get("unread"):
        r = api.read(store, user, {"peek": True, "limit": _int(p, "limit", 50, 500) or 50})
        for m in r["messages"]:
            if m.get("attachments") and m.get("chat") in store.chats:
                _files(store, store.chats[m["chat"]], m)
        out["unread"], out["unread_left"] = r["messages"], r["unread_left"]
    return out


def _members_at(chat: Chat, at: str) -> set:
    """Who was in a chat at the time `at`, from its created, added and removed lines."""
    members = set()
    for e in chat.read(list(chat.system)):
        if e["at"] > at:
            break
        if e["kind"] == "created":
            members = set(e["members"])
        elif e["kind"] == "added":
            members |= set(e["users"])
        elif e["kind"] == "removed":
            members -= set(e["users"])
    return members


def view_knew(store: Store, p: dict) -> dict:
    """How far the sender of a message had marked messages read in each of its chats when it sent it, from the read
    lines in the logs. It is not everything the sender saw: --peek, threads, previews and searches leave no line."""
    chat, e = store.message(None, str(p.get("id") or ""))
    user, at = e["from"], e["at"]
    t = epoch(at)
    out = []
    for c in list(store.chats.values()):
        if c is not chat and user not in c.members and user not in c.reads and user not in _tally(c)[0]:
            continue
        if c is not chat and user not in _members_at(c, at):
            continue
        n = bisect.bisect_right(c.times, t)  # messages written by then
        if not n and c is not chat:
            continue
        upto, read_at = c.position_at(user, at)
        i = bisect.bisect_right(c.messages, upto)
        unseen = sum(1 for s in c.senders[i:n] if s != user)
        out.append({"chat": c.id, "upto": upto, "read_at": read_at, "messages": n, "read": min(i, n),
                    "last": c.messages[n - 1] if n else 0, "unseen": unseen})
    out.sort(key=lambda r: (r["chat"] != chat.id, -r["unseen"], r["chat"]))
    return {"message": _messages(store, chat, [e])[0], "user": user, "at": at, "chats": out}


def download(store: Store, p: dict) -> tuple:
    """(the attachment, the path of its bytes), from any chat."""
    return store.attachment(None, str(p.get("id") or ""))


def view_text(store: Store, p: dict) -> dict:
    """A text file, for the page to show formatted: its first TEXT_BYTES at most, cut at the end of a line. `cut` says
    whether there is more. Bytes that are not UTF-8 show as the replacement character."""
    a, path = download(store, p)
    if a.get("folder"):
        raise Error(400, f"{a['name']} is a folder. Download it instead.")
    with open(path, "rb") as f:
        data = f.read(TEXT_BYTES + 1)
    cut = len(data) > TEXT_BYTES
    if cut:
        data = data[:TEXT_BYTES]
        data = data[:data.rfind(b"\n") + 1] or data
    text = data.decode("utf-8", "replace").removeprefix("\ufeff").replace("\0", "\ufffd")
    return {"text": text, "size": a["size"], "shown": len(data), "cut": cut}


def view_page(store: Store, p: dict) -> dict:
    """The viewer itself, for the page at /view to put in its own place once the token is accepted."""
    app = page()
    return {"html": app["html"], "js": app["js"]}


METHODS = {f.__name__: f for f in (
    view_info, view_snapshot, view_messages, view_thread, view_receipts, view_posts, view_search, view_activity,
    view_as, view_knew, view_text, view_page)}


# ---------------------------------------------------------------------- the page

PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page")
_page, _gate = {}, {}


def _text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _stamp(names) -> tuple:
    return tuple(os.stat(os.path.join(PAGE_DIR, n)).st_mtime_ns for n in names)


def _hash(script: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


def page() -> dict:
    """The viewer: index.html with app.css written into it ("html"), its script ("js"), and the two as one file
    ("body"). Built again only when a file changes."""
    names = ("index.html", "app.css", "app.js")
    stamp = _stamp(names)
    if _page.get("stamp") != stamp:
        html, css, js = (_text(os.path.join(PAGE_DIR, n)) for n in names)
        html = html.replace("<!--CSS-->", f"<style>{css}</style>")
        _page.update(stamp=stamp, html=html.replace("<!--JS-->", ""), js=js, script=_hash(js),
                     body=html.replace("<!--JS-->", f"<script>{js}</script>").encode())
    return _page


def gate() -> dict:
    """What /view serves: a form for the view token, which fetches the viewer (view_page) only once the token is
    accepted. With it: its gzip, an ETag, and a Content-Security-Policy that lets only its own script and the viewer's
    exact script run, so text an agent wrote can never run as code."""
    app, names = page(), ("gate.html", "gate.js")
    stamp = (app["stamp"], _stamp(names))
    if _gate.get("stamp") != stamp:
        html, js = (_text(os.path.join(PAGE_DIR, n)) for n in names)
        body = html.replace("<!--JS-->", f"<script>{js}</script>").encode()
        csp = (f"default-src 'none'; script-src {_hash(js)} {app['script']}; style-src 'unsafe-inline'; "
               "img-src 'self' data: blob:; connect-src 'self'; worker-src blob:; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'none'")
        etag = f'"{hashlib.sha256(body + csp.encode()).hexdigest()[:20]}"'  # a new viewer script changes the policy
        _gate.update(stamp=stamp, body=body, gzip=gzip.compress(body, 9), etag=etag, csp=csp)
    return _gate
