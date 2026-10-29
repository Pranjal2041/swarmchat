"""What each request does. Every function takes the store, the caller's user id and the request's parameters, and
returns a JSON-ready dict. server.py routes POST /v1/<name> to METHODS[name], or to ADMIN[name] for the operator and
the administrators.
"""
from __future__ import annotations

import contextlib
import heapq
import itertools
import math
import mimetypes
import os
import secrets
import socket
import time

from .files import mime, safe_name
from .store import MESSAGE_ID, Chat, Error, Store, epoch

MAX_WAIT_MESSAGES = 100


def _list(value) -> list:
    if not value:
        return []
    return [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if v.strip()]


def _int(params: dict, key: str, default: int) -> int:
    value = params.get(key)
    return default if value in (None, "") else max(0, int(value))


def _title(store: Store, me: str, chat: Chat) -> str:
    return f"#{chat.name}" if chat.name else ", ".join(store.display(u) for u in chat.members if u != me)


def _message(store: Store, chat: Chat, e: dict, me: str | None = None) -> dict:
    """A message for `me` to see. On my own messages: who has read them and when (read receipts)."""
    m = chat.view(e)
    m["from_name"] = store.display(m["from"])
    if me and e["seq"] in chat.parent and not store.sees(me, chat, chat.parent[e["seq"]]):
        m.pop("reply_to", None)  # it answers a message I may not read: which one, and whose, stay hidden
        m.pop("reply_to_from", None)
    if me and e["from"] == me:
        m["read_by"], m["unread_by"] = [], []
        for u in chat.members:
            if u == me:
                continue
            at = chat.read_at(u, e["seq"])
            if at or store.state.get(u, {}).get("read", {}).get(chat.id, 0) >= e["seq"]:
                m["read_by"].append({"id": u, "name": store.display(u), "at": at})
            else:
                m["unread_by"].append(u)
    return m


def _named(store: Store, m: dict) -> dict:
    return {**m, "from_name": store.display(m["from"])}


def _summary(store: Store, me: str, chat: Chat) -> dict:
    position, mentions, last = store.position(me, chat), chat.mentions.get(me), store.last(me, chat)
    return {"id": chat.id, "kind": "channel" if chat.name else "direct", "name": chat.name,
            "description": chat.description, "public": chat.public, "archived": chat.archived,
            "managers": chat.managers, "title": _title(store, me, chat),
            "members": [{"id": u, "name": store.display(u)} for u in chat.members],
            "unread": store.unread(me, chat), "mentioned": _mentioned(store, me, chat, mentions or (), position),
            "notify": store.level(me, chat), "last_at": chat.at,
            "last": _message(store, chat, last, me) if last else None}


def _mentioned(store: Store, me: str, chat: Chat, mentions, position: int) -> bool:
    """Whether a message after my read position that I may read mentions me. An edit can add a mention to a message
    from before I joined: under while_member that one does not count."""
    for seq in reversed(mentions):
        if seq <= position:
            return False
        if store.sees(me, chat, seq):
            return True
    return False


def _chat_titles(store: Store, me: str, messages: list) -> dict:
    return {m["chat"]: {"title": _title(store, me, store.chats[m["chat"]]), "name": store.chats[m["chat"]].name}
            for m in messages}


def _mark(store: Store, me: str, messages: list):
    """Marks read everything up to the newest of these messages in each chat: what a wait hands over."""
    newest = {}
    for m in messages:
        newest[m["chat"]] = max(newest.get(m["chat"], 0), m["seq"])
    for chat_id, seq in newest.items():
        store.mark_read(me, store.chats[chat_id], seq)


def _mark_shown(store: Store, me: str, shown: dict):
    """Marks read what a read showed ({chat: seqs}), up to the first unread message it did not show in each chat. A
    search or filter leaves the messages it skipped unread, so a sender cannot make me miss one by writing another that
    matches."""
    for chat_id, seqs in shown.items():
        chat = store.chats.get(chat_id)
        if chat and me in chat.members:
            upto = store.shown_upto(me, chat, {int(s) for s in seqs})
            if upto:
                store.mark_read(me, chat, upto)


def _time(store: Store, me: str, value: str | None):
    """Retain a message's sequence number when it is used as a history bound."""
    if value and MESSAGE_ID.fullmatch(value):
        chat, message = store.message(me, value)
        return message["at"], chat.id, message["seq"]
    return value


def _compare_bound(chat: Chat, event: dict, bound) -> int:
    if isinstance(bound, tuple):
        left, right = (event["seq"], bound[2]) if chat.id == bound[1] else ((event["at"], chat.id, event["seq"]), bound)
    else:
        left, right = event["at"], bound
    return (left > right) - (left < right)


def _ordered(messages: list) -> list:
    # Choose among chat heads, preserving sequence order even in older logs whose
    # timestamps disagree with commit order. A cursor must never jump over a head.
    grouped = {}
    for message in messages:
        grouped.setdefault(message["chat"], []).append(message)
    return list(heapq.merge(*grouped.values(), key=lambda m: (m["at"], m["chat"], m["seq"])))


# ---------------------------------------------------------------------- requests for every user

def whoami(store: Store, me: str, p: dict) -> dict:
    return {"user": store.public(me)}


def people(store: Store, me: str, p: dict) -> dict:
    found = [store.public(u) for u in sorted(store.users)]
    q = (p.get("search") or "").lower()
    if q:
        found = [u for u in found if q in " ".join([u["id"], u["name"], u["status"], *map(str, u["fields"].values())]).lower()]
    return {"people": found}


def chats(store: Store, me: str, p: dict) -> dict:
    found = [_summary(store, me, c) for c in store.my_chats(me)]
    if p.get("unread"):
        found = [c for c in found if c["unread"]]
    if p.get("with"):
        wanted = {store.user(u) for u in _list(p["with"])}
        found = [c for c in found if wanted <= {m["id"] for m in c["members"]}]
    if p.get("name"):
        q = p["name"].lower().lstrip("#")
        found = [c for c in found if q in c["title"].lower()]
    order = p.get("sort") or "recent"
    keys = {"recent": lambda c: c["last_at"], "unread": lambda c: (c["unread"], c["last_at"]), "name": lambda c: c["title"].lower()}
    if order not in keys:
        raise Error(400, f"sort is one of {', '.join(keys)}.")
    found.sort(key=keys[order], reverse=order != "name")
    return {"chats": found[:_int(p, "limit", 20)]}


def _instant(bound) -> float | None:
    """A time bound in seconds, when it is written as messages' times are, else None: then no message is skipped on its
    time alone, and each is compared as it is read."""
    at = bound[0] if isinstance(bound, tuple) else bound
    try:
        return epoch(at) if isinstance(at, str) and len(at) == 24 and at.endswith("Z") else None
    except ValueError:
        return None


def _pick(store: Store, me: str, chat: Chat, places, n: int, since, before, search: str) -> list:
    """Up to n of the messages at these places in the chat, in their order, keeping those after `since`, before
    `before` and containing `search`. Reads only what it keeps or must look at, a batch at a time."""
    if not (since or before or search):
        return [_message(store, chat, e, me) for e in chat.read([chat.messages[i] for i in itertools.islice(places, n)])]
    low, high = (_instant(b) if b and not (isinstance(b, tuple) and b[1] == chat.id) else None for b in (since, before))
    times = chat.times
    places = (i for i in places if (low is None or times[i] >= low) and (high is None or times[i] <= high))
    found = []
    while len(found) < n:
        batch = [chat.messages[i] for i in itertools.islice(places, 256)]
        if not batch:
            break
        for e in chat.read(batch):
            if (since and _compare_bound(chat, e, since) <= 0) or (before and _compare_bound(chat, e, before) >= 0):
                continue
            m = _message(store, chat, e, me)
            if not search or search in m["text"].lower():
                found.append(m)
    return found[:n]


def read(store: Store, me: str, p: dict) -> dict:
    """Unread messages by default. --last, --all, --since, --before or --search read history instead. --thread reads a
    whole thread, each message with its depth in it, and moves no read position: the thread's newest reply may be newer
    than messages elsewhere in the chat that are still unread.

    One reply holds at most max_page messages, and only those are read from disk. When more match, `more` is true, and
    the same request with the reply's `cursor` gives the next page: later messages for unread ones and threads,
    earlier ones for history."""
    fallback, more, most = False, False, store.settings["max_page"]
    cursor = p.get("cursor") or {}
    if not isinstance(cursor, dict):
        raise Error(400, "cursor is the one a previous reply gave.")
    if p.get("thread"):
        chat, tree = store.thread(me, p["thread"])
        skip, limit = _int(cursor, "skip", 0), min(_int(p, "limit", 0) or most, most)
        page, more = tree[skip:skip + limit], skip + limit < len(tree)
        depth = dict(page)
        targets, keep, cursor = [chat], "first", {"skip": skip + len(page)}
        messages = [{**_message(store, chat, e, me), "depth": depth[e["seq"]]} for e in chat.read([s for s, _ in page])]
    elif p.get("id"):
        chat, e = store.message(me, p["id"])
        targets, messages, keep = [chat], [_message(store, chat, e, me)], "first"
    else:
        targets = [store.chat(me, p["chat"])] if p.get("chat") else store.my_chats(me)
        senders = {store.user(u) for u in _list(p.get("from"))}
        since, before = _time(store, me, p.get("since")), _time(store, me, p.get("before"))
        search = (p.get("search") or "").lower()
        history = bool(p.get("last") or p.get("all") or since or before or search)
        keep = "last" if history else "first"
        limit = min(_int(p, "limit", 0) or _int(p, "last", 0) or (20 if history else 50), most)
        messages, cursor = [], {c: int(seq) for c, seq in cursor.items()}
        for chat in targets:
            # History goes back from the newest message, or from where the previous page stopped. Unread messages go on
            # from my read position, or from where the previous page stopped, and leave out my own.
            if history:
                after, upto = 0, cursor.get(chat.id)
            else:
                after, upto = max(store.position(me, chat), cursor.get(chat.id, 0)), None
            if isinstance(since, tuple) and since[1] == chat.id:  # a message of this chat bounds it by seq
                after = max(after, since[2])
            if isinstance(before, tuple) and before[1] == chat.id:
                upto = before[2] if upto is None else min(upto, before[2])
            mine, sent = set(chat.mentions.get(me, ())) if p.get("mentions") else None, chat.senders
            places = (i for i in store.indexes(me, chat, after, upto, newest=history)
                      if (history or sent[i] != me) and (not senders or sent[i] in senders)
                      and (mine is None or chat.messages[i] in mine))
            found = _pick(store, me, chat, places, limit + 1, since, before, search)
            more = more or len(found) > limit
            messages += found[:limit][::-1] if history else found[:limit]  # each chat oldest first, for _ordered
        if not history and p.get("chat") and not messages and not senders and not p.get("mentions") and not cursor:
            fallback, keep = True, "last"  # nothing unread in this chat: show its latest messages instead
            chat = targets[0]
            seqs = [chat.messages[i] for i in itertools.islice(store.indexes(me, chat, newest=True), 20)][::-1]
            messages = [_message(store, chat, e, me) for e in chat.read(seqs)]
        messages = _ordered(messages)
        more = more or len(messages) > limit
        messages = messages[:limit] if keep == "first" else messages[-limit:]
        for m in messages:  # where the next page starts in each chat
            c = m["chat"]
            cursor[c] = max(cursor.get(c, 0), m["seq"]) if keep == "first" else min(cursor.get(c, m["seq"]), m["seq"])
    if not p.get("peek") and not p.get("thread"):
        shown = {}
        for m in messages:
            shown.setdefault(m["chat"], []).append(m["seq"])
        _mark_shown(store, me, shown)
    left = sum(store.unread(me, c) for c in targets)
    return {"messages": messages, "chats": _chat_titles(store, me, messages), "unread_left": left,
            "fallback": fallback, "keep": keep, "more": more and not fallback, "cursor": cursor if more else None}


def confirm(store: Store, me: str, positions: dict):
    """Marks read up to these positions ({chat: seq}), in the chats I am still in."""
    for chat_id, seq in (positions or {}).items():
        chat = store.chats.get(chat_id)
        if chat and me in chat.members:
            store.mark_read(me, chat, int(seq))


def mark_read(store: Store, me: str, p: dict) -> dict:
    """Marks read up to `positions` ({chat: seq}), and what a read showed (`shown`, {chat: [seq, ...]}): see
    _mark_shown."""
    confirm(store, me, p.get("positions"))
    if not isinstance(p.get("shown") or {}, dict):
        raise Error(400, "shown is {chat: [message seqs]}.")
    _mark_shown(store, me, p.get("shown") or {})
    targets = [store.chat(me, p["chat"])] if p.get("chat") else store.my_chats(me)
    return {"ok": True, "unread_left": sum(store.unread(me, c) for c in targets)}


def send(store: Store, me: str, p: dict) -> dict:
    if not p.get("chat"):
        raise Error(400, "Say where to send: a #channel, a person, people separated by commas, or a chat id.")
    chat = store.chat(me, p["chat"], create=True)
    message = _named(store, store.send(me, chat, p.get("text") or "", p.get("reply_to")))
    return {"message": message, "chat": _summary(store, me, chat)}


def send_files(store: Store, me: str, header: dict, read, body_size: int) -> dict:
    """A message with attachments. `header` is like send's parameters plus files: [{name, size, type, folder}].
    read(n) then gives the files' bytes one after another, body_size bytes in all. Everything is checked before the
    bytes are read, and nothing is kept unless the whole message is written."""
    s, files = store.settings, header.get("files")
    if not isinstance(files, list) or not files:
        raise Error(400, "Name at least one file.")
    if len(files) > s["max_attachments"]:
        raise Error(413, f"At most {s['max_attachments']} files on one message.")
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("size"), int) or isinstance(f["size"], bool) or f["size"] < 0:
            raise Error(400, "Each file needs a name and a whole-number size.")
        if f["size"] > s["max_attachment"]:
            raise Error(413, f"{safe_name(f.get('name'))} is larger than {s['max_attachment']} bytes.")
    if sum(f["size"] for f in files) != body_size:
        raise Error(400, "The file sizes do not add up to the request's length.")
    text = header.get("text") or ""
    if len(text) > s["max_text"]:
        raise Error(400, f"The message is longer than {s['max_text']} characters.")
    if not header.get("chat"):
        raise Error(400, "Say where to send: a #channel, a person, people separated by commas, or a chat id.")
    chat = store.chat(me, header["chat"], create=True)
    store._member(me, chat)
    store._open(chat)
    refusal = store.policy.send(me, chat.members, chat.name, chat.id)
    if refusal:
        raise Error(403, refusal)
    with store.reserve(me, body_size):  # room on the disk and in my quota, taken before a byte is read
        received, attachments = [], []
        try:
            for f in files:
                received.append(store.blobs.receive(read, f["size"]))
            for f, (tmp, sha) in zip(files, received):
                store.blobs.keep(tmp, sha)
                name = safe_name(f.get("name"))
                # The type goes into the Content-Type header of every download: only a well-formed one is kept.
                kind = mime(f.get("type")) or mimetypes.guess_type(name)[0] or "application/octet-stream"
                attachments.append({"id": "a" + secrets.token_hex(8), "name": name, "size": f["size"], "type": kind,
                                    "sha256": sha, **({"folder": True} if f.get("folder") else {})})
        except BaseException as e:
            for a in attachments:
                store.blobs.release(a["sha256"])
            for tmp, _ in received[len(attachments):]:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
            if isinstance(e, EOFError):
                raise Error(400, str(e)) from None
            if isinstance(e, OSError) and not isinstance(e, (ConnectionError, TimeoutError, socket.timeout)):  # disk full
                raise Error(507, f"The server could not store the files: {e.strerror or e}") from None
            raise
        message = _named(store, store.send(me, chat, text, header.get("reply_to"), attachments))
    return {"message": message, "chat": _summary(store, me, chat)}


def download(store: Store, me: str, p: dict) -> tuple:
    """(the attachment, the path of its bytes): only for members of its chat, while its message stands."""
    return store.attachment(me, p.get("id") or "")


def edit(store: Store, me: str, p: dict) -> dict:
    return {"message": _named(store, store.change(me, p.get("id") or "", "edit", text=p.get("text") or ""))}


def delete(store: Store, me: str, p: dict) -> dict:
    return {"message": _named(store, store.change(me, p.get("id") or "", "delete"))}


def react(store: Store, me: str, p: dict) -> dict:
    emoji = (p.get("emoji") or "").strip()
    if not emoji or len(emoji) > 40:
        raise Error(400, "Give an emoji or a short word, up to 40 characters.")
    return {"message": _named(store, store.change(me, p.get("id") or "", "react", emoji=emoji,
                                                  remove=bool(p.get("remove"))))}


def channel_create(store: Store, me: str, p: dict) -> dict:
    chat = store.create_channel(me, p.get("name") or "", _list(p.get("members")), bool(p.get("public")),
                                p.get("description") or "")
    return {"chat": _summary(store, me, chat)}


def channel_list(store: Store, me: str, p: dict) -> dict:
    """Public channels, which anyone may join, most recently active first. Archived ones take no one, so they are
    left out. The count of messages is of those the caller may read: where members read only what was sent while they
    were in, someone outside learns no count, as joining would show them none of those messages."""
    q = (p.get("search") or "").strip().lstrip("#").lower()
    found = sorted((c for c in list(store.chats.values()) if c.public and not c.archived and q in c.name.lower()),
                   key=lambda c: c.at, reverse=True)
    return {"channels": [{"id": c.id, "name": c.name, "title": f"#{c.name}", "description": c.description,
                          "members": len(c.members), "last_at": c.at,
                          "messages": store.count(me, c) if me in c.members or not store.limited(c) else None,
                          "joined": me in c.members}
                         for c in found[:_int(p, "limit", 50)]]}


def channel_join(store: Store, me: str, p: dict) -> dict:
    return {"chat": _summary(store, me, store.join(me, p.get("chat") or ""))}


def channel_visibility(store: Store, me: str, p: dict) -> dict:
    chat, visibility = store.chat(me, p.get("chat") or ""), p.get("visibility")
    if visibility not in ("public", "private"):
        raise Error(400, "visibility is public or private.")
    store.set_public(me, chat, visibility == "public")
    return {"chat": _summary(store, me, chat)}


def chat_info(store: Store, me: str, p: dict) -> dict:
    chat = store.chat(me, p.get("chat") or "")
    info = _summary(store, me, chat)
    info["members"] = [{**store.public(u), "name": store.display(u)} for u in chat.members]
    info.update(created_by=chat.meta["created_by"], created_at=chat.meta["created_at"], messages=store.count(me, chat))
    return {"chat": info}


def channel_add(store: Store, me: str, p: dict) -> dict:
    chat = store.chat(me, p.get("chat") or "")
    store.change_members(me, chat, add=_list(p.get("users")))
    return {"chat": _summary(store, me, chat)}


def channel_remove(store: Store, me: str, p: dict) -> dict:
    chat = store.chat(me, p.get("chat") or "")
    store.change_members(me, chat, remove=_list(p.get("users")))
    return {"chat": _summary(store, me, chat) if me in chat.members else {"id": chat.id}}


def channel_leave(store: Store, me: str, p: dict) -> dict:
    return channel_remove(store, me, {"chat": p.get("chat"), "users": [me]})


def channel_rename(store: Store, me: str, p: dict) -> dict:
    chat = store.chat(me, p.get("chat") or "")
    store.rename(me, chat, p.get("name") or "")
    return {"chat": _summary(store, me, chat)}


def channel_describe(store: Store, me: str, p: dict) -> dict:
    chat = store.chat(me, p.get("chat") or "")
    store.describe(me, chat, p.get("description") or "")
    return {"chat": _summary(store, me, chat)}


def channel_managers(store: Store, me: str, p: dict) -> dict:
    """A channel's managers, after naming the ones in `add` and ending the role of the ones in `remove`."""
    chat = store.chat(me, p.get("chat") or "")
    add, remove = _list(p.get("add")), _list(p.get("remove"))
    if add or remove:
        store.set_managers(me, chat, add, remove)
    return {"chat": _summary(store, me, chat)}


def channel_archive(store: Store, me: str, p: dict) -> dict:
    chat = store.chat(me, p.get("chat") or "")
    store.archive(me, chat, True)
    return {"chat": _summary(store, me, chat)}


def channel_unarchive(store: Store, me: str, p: dict) -> dict:
    chat = store.chat(me, p.get("chat") or "")
    store.archive(me, chat, False)
    return {"chat": _summary(store, me, chat)}


def notifications(store: Store, me: str, p: dict) -> dict:
    """Chats that need me, following my notification level for each: all, mentions or none."""
    found = []
    for chat in store.my_chats(me):
        s = _summary(store, me, chat)
        if p.get("all") or (s["notify"] == "all" and s["unread"]) or (s["notify"] == "mentions" and s["mentioned"]):
            found.append(s)
    found.sort(key=lambda c: c["last_at"], reverse=True)
    return {"chats": found[:_int(p, "limit", 20)], "unread": sum(c["unread"] for c in found),
            "mentions": sum(c["mentioned"] for c in found)}


def _pending(store: Store, me: str, p: dict, only: Chat | None, senders: set, after: dict):
    """Messages a wait would return now, and how far it looked in each chat."""
    found, positions = [], {}
    for chat in [only] if only else store.my_chats(me):
        level = "all" if (only or p.get("any")) else store.level(me, chat)
        start = int(after.get(chat.id, store.position(me, chat)))
        newest = next((i for i in store.indexes(me, chat, start, newest=True) if chat.senders[i] != me), None)
        positions[chat.id] = start if newest is None else chat.messages[newest]
        if level == "none":
            continue
        mine = set(chat.mentions.get(me, ())) if p.get("mentions") or level == "mentions" else None
        remaining = MAX_WAIT_MESSAGES - len(found)
        seqs = [chat.messages[i] for i in itertools.islice(
            (i for i in store.indexes(me, chat, start) if chat.senders[i] != me
             and (not senders or chat.senders[i] in senders) and (mine is None or chat.messages[i] in mine)),
            remaining + 1)]
        if len(seqs) > remaining:
            seqs = seqs[:remaining]
            positions[chat.id] = seqs[-1]  # Remaining eligible messages have not reached the client yet.
        found += [_message(store, chat, e, me) for e in chat.read(seqs)]
        if len(found) >= MAX_WAIT_MESSAGES:
            break
    found.sort(key=lambda m: (m["at"], m["chat"], m["seq"]))
    return found, positions


def wait(store: Store, me: str, p: dict, gone=lambda: False) -> dict:
    """Holds the request until a matching message arrives or the timeout ends. Starts from my read positions (or from
    `after`, the positions a previous wait returned), so a message that arrives between two waits is never missed.
    `gone` tells whether the caller has hung up (a killed command): then the wait ends and marks nothing read."""
    hold = store.settings["max_wait"]  # the client asks again for longer waits
    timeout = float(hold if p.get("timeout") is None else p["timeout"])
    if not math.isfinite(timeout) or timeout < 0:
        raise Error(400, "timeout must be a finite number of seconds at least 0.")
    deadline = time.monotonic() + min(timeout, hold)
    only = store.chat(me, p["chat"]) if p.get("chat") else None
    senders = {store.user(u) for u in _list(p.get("from"))}
    after = p.get("after") or {}
    cond = store.signal(me)
    with cond:
        while True:
            # Access is checked again on every pass: removal from a channel wakes this wait.
            if me not in store.users:
                raise Error(401, "This user no longer exists.")
            if only:
                store._member(me, only)
            found, positions = _pending(store, me, p, only, senders, after)
            # Removal changes members before any later message is written, so checking after reading drops
            # everything that arrived once access had ended. Other chats keep going meanwhile.
            found = [m for m in found if me in store.chats[m["chat"]].members]
            left = deadline - time.monotonic()
            if found or left <= 0:
                break
            # Sleep until signalled, looking every second whether the caller is still there.
            while not cond.wait(min(left, 1.0)):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                if gone():
                    return {"messages": [], "positions": {}, "chats": {}}
    # Marked after letting go of my signal: senders wake me through it, and must not wait on my disk writes. Never
    # for a caller that has gone, or the messages would count as read without anyone having seen them.
    if found and not p.get("peek") and not gone():
        _mark(store, me, found)
    return {"messages": found, "positions": positions, "chats": _chat_titles(store, me, found)}


def settings_notify(store: Store, me: str, p: dict) -> dict:
    store.set_level(me, store.chat(me, p.get("chat") or ""), p.get("level") or "")
    return settings_show(store, me, p)


def settings_status(store: Store, me: str, p: dict) -> dict:
    store.set_status(me, (p.get("text") or "").strip())
    return settings_show(store, me, p)


def settings_token(store: Store, me: str, p: dict) -> dict:
    """A new token for me. The old one stops working at once."""
    return {"token": store.rotate_token(me)}


def settings_show(store: Store, me: str, p: dict) -> dict:
    return {"user": store.public(me), "notify": dict(store.state[me]["notify"])}


METHODS = {f.__name__: f for f in (
    whoami, people, chats, read, mark_read, send, edit, delete, react, chat_info, channel_create, channel_list,
    channel_join, channel_visibility, channel_add, channel_remove, channel_leave, channel_rename, channel_describe,
    channel_managers, channel_archive, channel_unarchive, notifications, wait, settings_notify, settings_status,
    settings_token, settings_show)}


# ---------------------------------------------------------------------- requests for the super admin, and whoever
# the rules allow. `me` is the user who asks, or None for the super admin's admin token.

def user_add(store: Store, me, p: dict) -> dict:
    store.require(me, "add_user")
    token = store.add_user(p.get("id") or "", p.get("name") or "", p.get("fields") or {})
    return {"user": store.public(p["id"].strip().lower()), "token": token}


def user_remove(store: Store, me, p: dict) -> dict:
    store.require(me, "remove_user")
    store.remove_user(p.get("id") or "")
    return {"ok": True}


def token_rotate(store: Store, me, p: dict) -> dict:
    store.require(me, "rotate_token")
    return {"token": store.rotate_token(p.get("id") or "")}


def policy_set(store: Store, me, p: dict) -> dict:
    if me is not None:  # the rules decide every other permission, so they stay with the super admin
        raise Error(403, "Only the super admin may set the rules.")
    if not isinstance(p.get("rules") or {}, dict):
        raise Error(400, "The rules are a JSON object.")
    store.set_policy(p.get("rules") or {})
    return {"rules": store.policy.rules}


def policy_show(store: Store, me, p: dict) -> dict:
    store.require(me, "show_rules")
    return {"rules": store.policy.rules}


ROLE_LISTS = {"admin": ("admins", "edit_admins"), "channel_creator": ("channel_creators", "edit_channel_creators")}


def role_change(store: Store, me, p: dict) -> dict:
    """Shows who holds a role, or changes it: admin, or channel_creator, where `everyone` makes every user one."""
    role, add, remove, everyone = p.get("role"), _list(p.get("add")), _list(p.get("remove")), bool(p.get("everyone"))
    if role not in ROLE_LISTS:
        raise Error(400, "The role is admin or channel_creator.")
    if everyone and (role != "channel_creator" or add or remove):
        raise Error(400, "Only channel_creator may be everyone, and then without names to add or remove.")
    key, action = ROLE_LISTS[role]
    store.require(me, action if add or remove or everyone else "show_rules")
    users = store.change_role(key, add, remove, everyone) if add or remove or everyone else store.policy.rules.get(key)
    return {"role": role, "users": [] if users is None and role == "admin" else users}  # None: every user


ADMIN = {f.__name__: f for f in (user_add, user_remove, token_rotate, policy_set, policy_show, role_change)}
