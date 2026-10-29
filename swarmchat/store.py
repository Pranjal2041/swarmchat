"""Storage: plain files on disk, and the index in memory that requests are answered from.

    DIR/                             readable only by its owner, like everything in it
      server.json                    format version
      admin.token                    the super admin's token, readable only by its owner
      view.token                     a token that may only look: it opens `swarmchat view`, readable only by its owner
      address                        where the server last started listening, for `swarmchat admin --data DIR`
      policy.json                    optional rules, see policy.py
      users/<user>.json              profile, status and the hash of the user's token
      retired/<user>.json            reserved ids of removed accounts (no token or profile)
      blobs/<ab>/<sha256>            attachment contents, each stored once (see files.py)
      uploads/                       uploads in progress
      chats/<chat>/meta.json         name, description, members, managers, public or not, archived or not, who
                                     created it and when
      chats/<chat>/log/000001.jsonl  every event in the chat, one JSON object per line, append-only. Deleting a
                                     message blanks its words in its line, and in its edits' lines
      chats/<chat>/redact.journal    the lines a deletion is rewriting, while it does: a crash part way through is
                                     finished at the next start
      state/<user>.json              read position and notification level for each chat

Each line of a chat log has a sequence number (seq), one higher than the line before. The kinds of line:
- "message"
- "edit", "delete" and "react", which point at a message's seq and are applied on top of it when it is read
- "created", "added", "removed", "renamed", "described", "managers", "visibility", "archived" and "unarchived",
  which keep the chat's history
- "read", written when someone's read position moves forward: the read receipts
A message's id is "<chat>:<seq>". A new log file starts every `segment` lines (a setting fixed when the folder is
made, kept in server.json), so a seq alone says which file and line hold an event.
"""
from __future__ import annotations

import bisect
import collections
import difflib
import hashlib
import contextlib
import itertools
import json
import os
import re
import secrets
import shutil
import threading
import time
from array import array
from datetime import datetime, timezone

from .config import DEFAULTS
from .files import Blobs
from .locking import DirectoryLock
from .policy import Policy

FORMAT = 1
LEVELS = ("all", "mentions", "none")
FULL = "The server's disk is nearly full, so it takes no new messages or files for now. Ask its operator to free space."
USER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
MESSAGE_ID = re.compile(r"([0-9a-f]{6,32}):(\d+)")  # chat ids were 6 hex digits, now 16


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_MINUTES = {}  # "2026-10-02T15:03" -> that minute in seconds since 1970


def epoch(at: str) -> float:
    """Seconds since 1970 from a time as now() writes it, 2026-10-02T15:03:09.424Z. It runs for every message at
    start, so that form is taken apart by hand, with each minute worked out once: a third of the time of parsing it."""
    if len(at) == 24 and at[-1] == "Z" and at[16] == ":":
        minute = _MINUTES.get(at[:16])
        if minute is None:
            minute = _MINUTES[at[:16]] = datetime.fromisoformat(at[:16] + ":00+00:00").timestamp()
        return minute + float(at[17:23])
    moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(size: int = 24) -> str:
    """A secret of `size` random bytes, in hex: it never starts with '-', so a command line never takes it for a flag."""
    return secrets.token_hex(size)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: str, data, mode: int | None = None, durable: bool = False):
    """Replaces a small file atomically: a reader sees the old version or the new one, never half of one. durable
    forces it to disk before returning, so it survives a power cut too."""
    encoded = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    tmp = f"{path}.{threading.get_ident()}.tmp"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o600), "wb") as f:
            f.write(encoded)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if durable and os.name != "nt":  # the new name, too, must reach the disk
            fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Error(Exception):
    """A request the store refuses. status is the HTTP status: 400 bad request, 403 not allowed, 404 not found."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Chat:
    """One chat: its metadata, its log, and what is kept in memory to answer requests without reading the log."""

    def __init__(self, root: str, meta: dict, segment: int):
        self.meta, self.segment = meta, segment
        self.dir = os.path.join(root, "chats", meta["id"])
        self.lock = threading.Lock()  # held while appending
        self.seq = 0  # seq of the last event
        self.at = meta["created_at"]  # time of the last message or membership change
        self.offsets = []  # one array per log file: the byte offset of each line
        self.messages = array("I")  # seqs of message events, increasing
        self.senders = []  # the sender of each entry in self.messages
        self.times = array("d")  # the time of each entry in self.messages, in seconds since 1970. Indexed last, so
        # its length is how many messages a reader without the lock can count on.
        self.system = array("I")  # seqs of the lines that made the chat or changed its members, name, description
        # or visibility
        self.mentions = {}  # user -> array of seqs of messages that mention them
        self.changes = {}  # message seq -> {"text", "edited_at", "deleted", "reactions"}
        self.edits = {}  # message seq -> seqs of the edits to it
        self.spans = {}  # user -> [[seq of the line that let them in, seq of the line that took them out, or None]]
        self.reads = {}  # user -> (array of the seqs they read up to, the time of each), increasing
        self.files = {}  # attachment id -> (message seq, the attachment as stored), for messages not deleted
        self.parent = {}  # seq of a reply -> seq of the message it answers
        self.replies = {}  # seq of a message -> seqs of the replies to it, in order
        self.last = None  # the newest message event, as stored
        self.writer, self.writer_file = None, 0
        self.failed = False  # A failed rollback requires reopening/recovering this chat.
        self.listener = None  # called with (chat, event) after each new line: the server-wide feed
        self.cache = {}  # what the viewer works out from the indexes above, kept with the chat (see view.py)

    id = property(lambda self: self.meta["id"])
    name = property(lambda self: self.meta.get("name"))
    description = property(lambda self: self.meta.get("description") or "")  # what a channel is for, or ""
    public = property(lambda self: bool(self.meta.get("public")))  # a channel anyone may find and join
    archived = property(lambda self: bool(self.meta.get("archived")))  # a channel kept to read, where nothing changes
    members = property(lambda self: self.meta["members"])

    @property
    def managers(self) -> list:
        """A channel's managers: its creator, and whoever a manager or administrator names. Leaving ends the role."""
        if not self.name:
            return []
        return [u for u in self.meta.get("managers", [self.meta["created_by"]]) if u in self.members]

    def path(self, file_no: int) -> str:
        return os.path.join(self.dir, "log", f"{file_no:06d}.jsonl")

    def load(self, read_only: bool = False):
        if not read_only:
            os.makedirs(os.path.join(self.dir, "log"), 0o700, exist_ok=True)
        journal = os.path.join(self.dir, "redact.journal")
        writes = read_json(journal)
        if writes is not None:  # a deletion's rewrite that a crash cut off: its lines may be half old, half new
            if read_only:
                raise SystemExit(f"Chat {self.id} has a deletion that a crash cut off. Start the server on this folder "
                                 f"once to finish it.")
            self._overwrite(writes, True)
            os.remove(journal)
        file_no = 1
        while os.path.exists(self.path(file_no)):
            offsets, pos = array("Q"), 0
            with open(self.path(file_no), "rb") as f:
                for line in f:
                    if not line.endswith(b"\n"):  # half a line, left by a crash in the middle of a write
                        break
                    offsets.append(pos)
                    pos += len(line)
                    event = json.loads(line)
                    if event["seq"] != self.seq + 1:
                        raise Error(500, f"Corrupt log {self.path(file_no)}: expected sequence {self.seq + 1}.")
                    self.index(event)
            if os.path.getsize(self.path(file_no)) != pos and not read_only:
                os.truncate(self.path(file_no), pos)
            self.offsets.append(offsets)
            file_no += 1

    def index(self, e: dict):
        self.seq = e["seq"]
        kind = e["kind"]
        if kind == "message":
            self.messages.append(e["seq"])
            self.senders.append(e["from"])
            self.times.append(epoch(e["at"]))
            for user in e.get("mentions", ()):
                self.mentions.setdefault(user, array("I")).append(e["seq"])
            for a in e.get("attachments", ()):
                self.files[a["id"]] = (e["seq"], a)
            answered = "reply_to" in e and MESSAGE_ID.fullmatch(e["reply_to"].strip())
            if answered:
                self.parent[e["seq"]] = int(answered[2])
                self.replies.setdefault(int(answered[2]), []).append(e["seq"])
            self.last, self.at = e, e["at"]
        elif kind in ("edit", "delete", "react"):
            change = self.changes.setdefault(e["target"], {})
            if kind == "edit":
                change.update(text=e["text"], edited_at=e["at"])
                self.edits.setdefault(e["target"], []).append(e["seq"])
                if "mentions" in e:
                    change["mentions"] = e["mentions"]
                    self.set_mentions(e["target"], e["mentions"])
            elif kind == "delete":
                change["deleted"] = e["at"]
                for att_id in [i for i, (seq, _) in self.files.items() if seq == e["target"]]:
                    del self.files[att_id]
                change["mentions"] = []
                self.set_mentions(e["target"], [])
            else:
                users = change.setdefault("reactions", {}).setdefault(e["emoji"], [])
                if e.get("remove") and e["from"] in users:
                    users.remove(e["from"])
                elif not e.get("remove") and e["from"] not in users:
                    users.append(e["from"])
        elif kind == "read":
            upto, times = self.reads.setdefault(e["from"], (array("I"), []))
            upto.append(e["upto"])
            times.append(e["at"])
        else:
            self.at = e["at"]
            self.system.append(e["seq"])
            if kind in ("created", "added"):
                for user in e.get("members") or e.get("users") or ():
                    self.spans.setdefault(user, []).append([e["seq"], None])
            elif kind == "removed":
                for user in e["users"]:
                    if self.spans.get(user) and self.spans[user][-1][1] is None:
                        self.spans[user][-1][1] = e["seq"]

    def set_mentions(self, seq: int, users: list):
        for user in set(self.mentions) | set(users):
            seqs = self.mentions.setdefault(user, array("I"))
            i = bisect.bisect_left(seqs, seq)
            present = i < len(seqs) and seqs[i] == seq
            if user in users and not present:
                seqs.insert(i, seq)
            elif user not in users and present:
                del seqs[i]

    def append(self, event: dict, fsync: bool) -> dict:
        """Writes one event at the end of the log, then indexes it. The caller holds self.lock."""
        if self.failed:
            raise Error(503, "This chat needs recovery after a failed log write. Restart the server.")
        event = {"seq": self.seq + 1, **event}
        encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        file_no = (event["seq"] - 1) // self.segment + 1
        if self.writer_file != file_no:
            if self.writer:
                self.writer.close()
            fd = os.open(self.path(file_no), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
            self.writer, self.writer_file = os.fdopen(fd, "ab", buffering=0), file_no
        while len(self.offsets) < file_no:
            self.offsets.append(array("Q"))
        pos = self.writer.tell()
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = self.writer.write(remaining)
                if not written:
                    raise OSError("The log write made no progress.")
                remaining = remaining[written:]
            self.writer.flush()
        except OSError:
            try:
                os.ftruncate(self.writer.fileno(), pos)
                self.writer.seek(pos)
            except OSError:
                self.failed = True
            raise
        # Once the complete line exists, reserve its sequence even if fsync fails.
        # An unacknowledged message may survive, but its id must never be reused.
        self.offsets[file_no - 1].append(pos)
        self.index(event)
        if fsync:
            os.fsync(self.writer.fileno())
        if self.listener:
            self.listener(self, event)
        return event

    def read(self, seqs) -> list:
        """The events with these seqs, as stored, in the order given. Opens each log file once."""
        found, files = {}, {}
        try:
            for seq in sorted(set(seqs)):
                file_no, line_no = divmod(seq - 1, self.segment)
                if file_no not in files:
                    files[file_no] = open(self.path(file_no + 1), "rb")
                for attempt in range(5):
                    files[file_no].seek(self.offsets[file_no][line_no])
                    try:
                        found[seq] = json.loads(files[file_no].readline())
                        break
                    except ValueError:  # read while redact() rewrote it: the next read sees the whole new line
                        if attempt == 4:
                            raise
                        time.sleep(0.001)
        finally:
            for f in files.values():
                f.close()
        return [found[seq] for seq in seqs]

    def redact(self, seq: int, fsync: bool):
        """Rewrites a deleted message's line, and the lines of its edits, without their text, mentions and files.
        Each line keeps its length, padded with spaces, so every offset stays right. The new lines go to a journal
        first, so a crash in the middle of the rewrite is finished at the next start instead of leaving a line that is
        half old and half new. The journal holds only what remains, never the words. The caller holds self.lock, or is
        loading the chat."""
        writes = []
        for target in (seq, *self.edits.get(seq, ())):
            file_no, line_no = divmod(target - 1, self.segment)
            pos = self.offsets[file_no][line_no]
            with open(self.path(file_no + 1), "rb") as f:
                f.seek(pos)
                line = f.readline()
            e = json.loads(line)
            kept = {k: e[k] for k in ("seq", "kind", "from", "at", "target", "reply_to") if k in e}
            if e["kind"] == "edit":
                kept["text"] = ""  # an edit always has text
            if kept == e:
                continue  # already redacted
            encoded = json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode()
            writes.append([file_no + 1, pos, (encoded + b" " * (len(line) - 1 - len(encoded))).decode()])
        if writes:
            journal = os.path.join(self.dir, "redact.journal")
            write_json(journal, writes, durable=fsync)
            self._overwrite(writes, fsync)
            os.remove(journal)

    def _overwrite(self, writes: list, fsync: bool):
        """Writes each [log file number, offset, new line without its newline] in place."""
        for file_no, pos, line in writes:
            with open(self.path(file_no), "r+b") as f:
                f.seek(pos)
                f.write(line.encode())
                f.flush()
                if fsync:
                    os.fsync(f.fileno())

    def view(self, e: dict) -> dict:
        """A message as members see it: with its id, and with later edits, deletion and reactions applied."""
        m = {"id": f"{self.id}:{e['seq']}", "chat": self.id, **e}
        change = self.changes.get(e["seq"], {})
        if "text" in change:
            m["text"], m["edited_at"] = change["text"], change["edited_at"]
        if "mentions" in change:
            m["mentions"] = change["mentions"]
        if m.get("attachments"):  # members see id, name, size and type: never the hash
            m["attachments"] = [{k: v for k, v in a.items() if k != "sha256"} for a in m["attachments"]]
        if e["seq"] in self.parent:  # whose message it answers: --where "m.reply_to_from == 'maya'" finds replies to maya
            m["reply_to_from"] = self.sender(self.parent[e["seq"]])
        if "deleted" in change:
            m["text"], m["deleted"] = "", True
            m.pop("attachments", None)
        reactions = {emoji: users for emoji, users in change.get("reactions", {}).items() if users}
        if reactions:
            m["reactions"] = reactions
        return m

    def present(self, user: str, seq: int) -> bool:
        """Whether `user` was a member when the line at seq was written."""
        return any(start < seq and (end is None or seq < end) for start, end in self.spans.get(user, ()))

    def read_at(self, user: str, seq: int) -> str | None:
        """When `user` read the message at seq, from the read lines in the log, or None."""
        upto, times = self.reads.get(user, ((), ()))
        i = bisect.bisect_left(upto, seq)
        return times[i] if i < len(upto) else None

    def sender(self, seq: int) -> str:
        """Who sent the message at seq."""
        return self.senders[bisect.bisect_left(self.messages, seq)]

    def is_message(self, seq: int) -> bool:
        i = bisect.bisect_left(self.messages, seq)
        return i < len(self.messages) and self.messages[i] == seq

    def position_at(self, user: str, at: str) -> tuple:
        """(how far `user` had read, when that read happened) at the time `at`, from the read lines. (0, None) if
        they had read nothing here by then."""
        upto, times = self.reads.get(user, ((), ()))
        i = bisect.bisect_right(times, at)
        return (upto[i - 1], times[i - 1]) if i else (0, None)

    def close(self):
        if self.writer:
            self.writer.close()
            self.writer, self.writer_file = None, 0


class Feed:
    """Every new line in any chat, and every change to a user, numbered in order: what `swarmchat view` follows live.
    Only the newest `size` entries are kept. A viewer that falls further behind than that starts again from a
    snapshot."""

    def __init__(self, size: int = 20_000):
        self.entries = collections.deque(maxlen=size)  # (number, chat or None, event)
        self.count = 0  # the number of the newest entry
        self.id = secrets.token_hex(4)  # numbers start again with each start of the server: this tells them apart
        self.cond = threading.Condition()

    def add(self, chat: Chat | None, event: dict):
        with self.cond:
            self.count += 1
            self.entries.append((self.count, chat, event))
            self.cond.notify_all()

    def after(self, number: int) -> list | None:
        """The entries after `number`, oldest first, or None when some of them are no longer kept."""
        with self.cond:
            first = self.count - len(self.entries) + 1
            if number >= self.count:
                return []
            if number + 1 < first:
                return None
            return list(itertools.islice(self.entries, number + 1 - first, None))


class Store:
    """Every user, chat, read position and setting, loaded from DIR at start and kept in step with it.

    With read_only, the folder is only looked at, for `swarmchat view` when no server is running on it: nothing in it is
    created, repaired or locked. The server then takes only the viewer's requests, with a view token made for this
    process alone (view_token)."""

    def __init__(self, root: str, settings: dict | None = None, read_only: bool = False):
        self.root, self.settings, self.read_only = root, {**DEFAULTS, **(settings or {})}, read_only
        self.fsync = self.settings["fsync"]
        self.lock = threading.RLock()  # users, chat membership, read positions and settings
        self.chats, self._closed = {}, False
        self.feed = Feed()
        self.counting = threading.Lock()  # the counts below
        self.stored = collections.Counter()  # user -> bytes of the files on their messages that stand
        self.pending = collections.Counter()  # user -> bytes of their uploads under way
        self.reserved = 0  # bytes of every upload under way
        self.free, self.free_at = 0, -1.0  # bytes free on the disk, and when that was asked
        self.requests = collections.Counter()  # user -> their requests under way
        self._directory_lock = None if read_only else DirectoryLock(root)
        try:
            self._load()
        except BaseException:
            for chat in self.chats.values():
                chat.close()
            if self._directory_lock:
                self._directory_lock.close()
            raise

    def _load(self):
        if self.read_only and not os.path.exists(self.file("server.json")):
            raise SystemExit(f"{self.root} is not a data folder: it has no server.json.")
        for sub in ("users", "chats", "state", "retired"):
            if not self.read_only:
                os.makedirs(self.file(sub), 0o700, exist_ok=True)
        info = read_json(self.file("server.json"))
        if info is None:
            info = {"format": FORMAT, "created_at": now(), "segment": self.settings["segment"]}
            write_json(self.file("server.json"), info)
        elif info.get("format") != FORMAT:
            raise SystemExit(f"{self.root} holds data format {info.get('format')}, this version reads format {FORMAT}.")
        self.settings["segment"] = self.segment = info.get("segment", 10_000)  # the folder's own, whatever was asked
        if self.read_only:
            self.view_token = new_token(32)
            self.admin, self.viewer = None, digest(self.view_token)
        else:
            self.view_token = None
            self.admin, self.viewer = (digest(self._token(name)) for name in ("admin.token", "view.token"))
        self.policy = Policy(read_json(self.file("policy.json"), {}))
        self.users, self.tokens = {}, {}
        self.retired = {read_json(self.file("retired", name))["id"]
                        for name in self._list("retired") if name.endswith(".json")}
        for name in sorted(self._list("users")):
            if name.endswith(".json"):
                user = read_json(self.file("users", name))
                if user["id"] in self.retired:
                    continue
                self.users[user["id"]], self.tokens[user["token_sha256"]] = user, user["id"]
        self.state = {u: read_json(self.file("state", f"{u}.json"), {"read": {}, "notify": {}}) for u in self.users}
        self.names = collections.Counter(u["name"].lower() for u in self.users.values())
        # A chat with a name is a channel. Without one it is a direct chat, found by its exact set of members.
        self.chats, self.direct, self.channels = {}, {}, {}
        self.blobs, self.files = Blobs(self.root, self.read_only), {}  # files: attachment id -> its chat
        for chat_id in sorted(self._list("chats")):
            meta = read_json(self.file("chats", chat_id, "meta.json"))
            if meta:
                chat = self.chats[chat_id] = Chat(self.root, meta, self.segment)
                chat.load(self.read_only)
                if not self.read_only:  # deleted before deletion erased the words on disk
                    for seq, change in chat.changes.items():
                        if "deleted" in change:
                            chat.redact(seq, False)
                chat.listener = self.feed.add
                self._index_chat(chat)
                # Older versions deleted profiles outright; preserve identities still present in history.
                self.retired.update(u for u in [*chat.members, *chat.senders, meta["created_by"]] if u not in self.users)
                for att_id, (seq, a) in chat.files.items():
                    self.files[att_id] = chat
                    self.blobs.refs[a["sha256"]] += 1
                    self.stored[chat.sender(seq)] += a["size"]
        if not self.read_only:
            self.blobs.prune()
        self.dirty, self.signals = set(), {}

    def file(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def _list(self, sub: str) -> list:
        return os.listdir(self.file(sub)) if os.path.isdir(self.file(sub)) else []

    def _token(self, name: str) -> str:
        """The token kept in the file `name`, made on first start, readable only by the folder's owner."""
        if not os.path.exists(self.file(name)):
            with os.fdopen(os.open(self.file(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write(new_token(32) + "\n")
        with open(self.file(name)) as f:
            return f.read().strip()

    # ------------------------------------------------------------------ users

    def me(self, token: str) -> str | None:
        return self.tokens.get(digest(token)) if token else None

    def is_admin(self, token: str) -> bool:
        return bool(token) and self.admin is not None and secrets.compare_digest(digest(token), self.admin)

    def can_view(self, token: str) -> bool:
        """The view token, or the admin token: either may look at everything through `swarmchat view`."""
        return bool(token) and (secrets.compare_digest(digest(token), self.viewer) or self.is_admin(token))

    def name(self, user: str) -> str:
        return self.users.get(user, {}).get("name", user)

    def display(self, user: str) -> str:
        """The name to show: the display name, with the id added when someone else has the same name."""
        name = self.name(user)
        return f"{name} ({user})" if self.names[name.lower()] > 1 else name

    def public(self, user: str) -> dict:
        u = self.users.get(user, {"id": user, "name": user})
        return {"id": u["id"], "name": u["name"], "fields": u.get("fields", {}), "status": u.get("status", "")}

    def user(self, ref: str) -> str:
        """A user id from an id or a display name, in any case."""
        ref = ref.strip().lstrip("@")
        if ref.lower() in self.users:
            return ref.lower()
        hits = [u for u, d in self.users.items() if d["name"].lower() == ref.lower()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise Error(400, f"Several people are named {ref}: {', '.join(hits)}. Use the id.")
        close = difflib.get_close_matches(ref, [d["name"] for d in self.users.values()] + list(self.users), n=3)
        raise Error(404, f"No one called '{ref}'." + (f" Did you mean {', '.join(close)}?" if close else ""))

    def add_user(self, user: str, name: str = "", fields: dict | None = None) -> str:
        user = user.strip().lower()
        if not USER_ID.fullmatch(user):
            raise Error(400, "A user id is 1 to 40 of a-z, 0-9, '.', '_' and '-', starting with a letter or digit.")
        token = new_token()
        if not isinstance(name, str) or (fields is not None and not isinstance(fields, dict)):
            raise Error(400, "name must be text and fields must be an object.")
        with self.lock:
            if user in self.users:
                raise Error(400, f"{user} already exists.")
            if user in self.retired:
                raise Error(400, f"{user} is a retired user id. Choose a new id; rotate a token to keep an existing identity.")
            profile = {"id": user, "name": name or user, "fields": fields or {}, "status": "",
                                "token_sha256": digest(token), "created_at": now()}
            write_json(self.file("users", f"{user}.json"), profile, 0o600)
            self.users[user] = profile
            self.tokens[digest(token)] = user
            self.state[user] = {"read": {}, "notify": {}}
            self.names[self.users[user]["name"].lower()] += 1
        self.feed.add(None, {"kind": "user", "id": user})
        return token

    def save_user(self, user: str):
        write_json(self.file("users", f"{user}.json"), self.users[user], 0o600)

    def rotate_token(self, user: str) -> str:
        user, token = self.user(user), new_token()
        with self.lock:
            profile = {**self.users[user], "token_sha256": digest(token)}
            write_json(self.file("users", f"{user}.json"), profile, 0o600)
            self.tokens.pop(self.users[user]["token_sha256"], None)
            self.users[user] = profile
            self.tokens[digest(token)] = user
        return token

    def remove_user(self, user: str):
        user = self.user(user)
        for chat in self.my_chats(user):
            if chat.name:  # leaves every channel. Direct chats keep their members, as a record.
                self.change_members(user, chat, remove=[user])
        with self.lock:
            write_json(self.file("retired", f"{user}.json"), {"id": user, "deleted_at": now()}, 0o600)
            self.retired.add(user)
            gone = self.users.pop(user)
            self.tokens.pop(gone["token_sha256"], None)
            self.names[gone["name"].lower()] -= 1
            self.state.pop(user, None)
            for path in (self.file("users", f"{user}.json"), self.file("state", f"{user}.json")):
                if os.path.exists(path):
                    os.remove(path)
        self.feed.add(None, {"kind": "user", "id": user})

    def set_status(self, user: str, text: str):
        with self.lock:
            profile = {**self.users[user], "status": text}
            write_json(self.file("users", f"{user}.json"), profile, 0o600)
            self.users[user] = profile
        self.feed.add(None, {"kind": "user", "id": user})

    def set_policy(self, rules: dict):
        policy = Policy(rules)
        with self.lock:
            write_json(self.file("policy.json"), rules)
            self.policy = policy

    def change_role(self, key: str, add=(), remove=(), everyone: bool = False) -> list | None:
        """Puts users on the rules' list `key`, admins or channel_creators, or takes them off. `everyone` drops the
        list of channel creators, so every user is one again."""
        add, remove = {self.user(u) for u in add}, {str(u).strip().lower() for u in remove}
        with self.lock:
            rules = dict(self.policy.rules)
            if everyone:
                rules.pop(key, None)
            else:
                rules[key] = sorted(({u.strip().lower() for u in rules.get(key) or ()} | add) - remove)
            self.set_policy(rules)
            return rules.get(key)

    # ------------------------------------------------------------------ chats

    def my_chats(self, user: str) -> list:
        return [chat for chat in list(self.chats.values()) if user in chat.members]

    def _index_chat(self, chat: Chat):
        if chat.name:
            self.channels.setdefault(chat.name.lower(), chat)
        else:
            self.direct.setdefault(frozenset(chat.members), chat)

    def _member(self, me: str, channel: Chat) -> Chat:
        if me not in self.users:
            raise Error(401, "This user no longer exists.")
        if me not in channel.members:
            how = "It is public, so you can join it." if channel.public else "Ask one of its members to add you."
            raise Error(403, f"You are not in #{channel.name}. {how}")
        return channel

    def ranges(self, me: str | None, chat: Chat, after: int = 0, before: int | None = None) -> list:
        """[(first, end)] index ranges in chat.messages of the messages after seq `after`, and before seq `before` if
        given, that `me` may read. me=None is the operator's viewer, which reads everything."""
        n = len(chat.times)  # what a reader without the chat's lock can count on
        lo = bisect.bisect_right(chat.messages, after, 0, n)
        hi = n if before is None else bisect.bisect_left(chat.messages, before, 0, n)
        if me is None or not self.limited(chat):
            return [(lo, hi)] if lo < hi else []
        found = []
        for start, end in list(chat.spans.get(me, ())):  # see Chat.present
            a = max(lo, bisect.bisect_right(chat.messages, start, 0, n))
            b = hi if end is None else min(hi, bisect.bisect_left(chat.messages, end, 0, n))
            if a < b:
                found.append((a, b))
        return found

    def indexes(self, me: str | None, chat: Chat, after: int = 0, before: int | None = None, newest: bool = False):
        """The places in chat.messages (and chat.senders, chat.times) of the messages in ranges(), one at a time, oldest
        first or newest first. A caller that wants a few messages of a long chat looks at only those."""
        ranges = self.ranges(me, chat, after, before)
        for lo, hi in reversed(ranges) if newest else ranges:
            yield from range(hi - 1, lo - 1, -1) if newest else range(lo, hi)

    def count(self, me: str | None, chat: Chat) -> int:
        """How many of a chat's messages `me` may read."""
        return sum(hi - lo for lo, hi in self.ranges(me, chat))

    def visible(self, me: str, chat: Chat, pairs: list) -> list:
        """The (seq, sender) pairs of messages `me` may read: all of them, unless the rules' history is while_member,
        which keeps a channel's messages from before someone joined, or from while they were out, from them."""
        if not self.limited(chat):
            return pairs
        return [pair for pair in pairs if chat.present(me, pair[0])]

    def limited(self, chat: Chat) -> bool:
        """Whether the rules keep some of this chat's messages from some of its members."""
        return bool(chat.name) and self.policy.history == "while_member"

    def sees(self, me: str | None, chat: Chat, seq: int) -> bool:
        return me is None or bool(self.visible(me, chat, [(seq, None)]))

    def unread(self, me: str, chat: Chat) -> int:
        """How many messages after my read position, from others, I may read. Counted without listing them."""
        return sum(hi - lo - chat.senders[lo:hi].count(me) for lo, hi in self.ranges(me, chat, self.position(me, chat)))

    def last(self, me: str, chat: Chat) -> dict | None:
        """The newest message I may read in a chat, as stored."""
        if chat.last is None or self.sees(me, chat, chat.last["seq"]):
            return chat.last
        i = next(self.indexes(me, chat, newest=True), None)
        return None if i is None else chat.read([chat.messages[i]])[0]

    def require(self, me: str | None, action: str, chat: Chat | None = None):
        """Refuses unless the rules let `me` (None for the super admin) take `action`, in `chat` for a chat's action."""
        refusal = self.policy.check(action, self.policy.roles(me, chat.managers if chat is not None else None))
        if refusal:
            raise Error(403, refusal)

    def _open(self, chat: Chat):
        """Refuses any change to an archived channel. Its members still read it, and may leave or be removed."""
        if chat.archived:
            raise Error(403, f"#{chat.name} is archived, so nothing in it can change. Unarchive it first.")

    def chat(self, me: str, ref: str, create: bool = False) -> Chat:
        """A chat from its id, a channel's name (with or without #), or people: one person, or several separated by
        commas, meaning the direct chat with exactly them. With create, a missing direct chat is made."""
        ref = ref.strip()
        chat = self.chats.get(ref)
        if chat and me in chat.members:
            return chat
        channel = self.channels.get(ref.lstrip("#").strip().lower())
        if channel and not channel.public and me not in channel.members:
            channel = None  # a private channel answers only to its members: to anyone else it does not exist
        if ref.startswith("#"):
            if not channel:
                raise Error(404, f"There is no channel {ref}.")
            return self._member(me, channel)
        try:
            members = frozenset([me, *(self.user(p) for p in ref.split(",") if p.strip())])
        except Error as e:
            if channel:
                return self._member(me, channel)
            raise Error(404, f"No channel or person '{ref}'. {e}") from None
        if channel:
            raise Error(400, f"'{ref}' is both a channel and a person. Write #{channel.name} for the channel.")
        if len(members) < 2:
            raise Error(400, "Name at least one other person.")
        chat = self.direct.get(members)
        if chat:
            return chat
        if not create:
            raise Error(404, f"You have no chat with exactly {', '.join(sorted(members - {me}))} yet.")
        self.room()
        refusal = self.policy.create(me, sorted(members))
        if refusal:
            raise Error(403, refusal)
        with self.lock:  # two people may write to each other for the first time at the same moment
            return self.direct.get(members) or self._new_chat(me, sorted(members), None)

    def create_channel(self, me: str, name: str, others=(), public: bool = False, description: str = "") -> Chat:
        """A channel is private unless made public: then anyone may find it and join."""
        name, description = self._channel_name(name), self._description(description)
        self.require(me, "create_channel")
        self.room()
        members = sorted({me, *(self.user(o) for o in others)})
        refusal = self.policy.create(me, members)
        if refusal:
            raise Error(403, refusal)
        with self.lock:
            self._name_free(me, name)
            return self._new_chat(me, members, name, public, description)

    def _channel_name(self, name: str) -> str:
        name = (name or "").strip().lstrip("#").strip()
        if not name or len(name) > 80 or "," in name:
            raise Error(400, "A channel name is 1 to 80 characters, without commas.")
        return name

    def _description(self, text: str) -> str:
        text = (text or "").strip() if isinstance(text, str) or not text else None
        if text is None or len(text) > 1000:
            raise Error(400, "A channel description is text, up to 1,000 characters.")
        return text

    def _name_free(self, me: str, name: str, chat: Chat | None = None):
        """Channel names are unique on the server, and never someone's id or name, so a name means one thing. A
        private channel's members learn which channel took a name. Anyone else learns only that it is taken."""
        taken = self.channels.get(name.lower())
        if taken and taken is not chat:
            if not taken.public and me not in taken.members:
                raise Error(400, f"The name #{name} is taken. Pick another name.")
            how = "It is archived. Unarchive it" if taken.archived else "Join it" if taken.public \
                else "Ask one of its members to add you"
            raise Error(400, f"There is already a channel #{taken.name} ({taken.id}). {how}, or pick another name.")
        if name.lower() in self.users or self.names[name.lower()] > 0:
            raise Error(400, f"'{name}' is someone's name. Pick another name for the channel.")

    def _new_chat(self, me: str, members: list, name: str | None, public: bool = False, description: str = "") -> Chat:
        """Makes a chat on disk and in the index. The caller holds self.lock and has checked everything."""
        chat_id = secrets.token_hex(8)  # too many to guess, so an id does not find a chat
        while chat_id in self.chats:
            chat_id = secrets.token_hex(8)
        meta = {"id": chat_id, "name": name, "members": members, "created_by": me, "created_at": now(),
                **({"managers": [me]} if name else {})}
        shown = {**({"public": True} if public else {}), **({"description": description} if description else {})}
        meta.update(shown)
        chat = Chat(self.root, meta, self.segment)
        chat.listener = self.feed.add
        os.makedirs(chat.dir, 0o700)
        os.mkdir(os.path.join(chat.dir, "log"), 0o700)
        write_json(os.path.join(chat.dir, "meta.json"), meta)
        with chat.lock:
            chat.append({"kind": "created", "from": me, "at": meta["created_at"], "members": members, "name": name,
                         **shown}, self.fsync)
        self.chats[chat_id] = chat
        self._index_chat(chat)
        return chat

    def change_members(self, me: str, chat: Chat, add=(), remove=()):
        if not chat.name:
            raise Error(400, "The members of a direct chat are fixed. Make a channel for a group whose members change.")
        add = [u for u in dict.fromkeys(map(self.user, add)) if u not in chat.members]
        remove = [u for u in dict.fromkeys(map(self.user, remove)) if u in chat.members]
        if set(add) - {me}:  # joining a public channel is not adding
            self.require(me, "add_members", chat)
        if set(remove) - {me}:  # anyone may leave
            self.require(me, "remove_members", chat)
        refusal = add and self.policy.add(me, chat.members, add)
        if refusal:
            raise Error(403, refusal)
        if not add and not remove:
            return
        if add:  # leaving is never refused: it is how room is made
            self.room()
        with self.lock, chat.lock:  # members change under the chat's lock, so a send sees them before or after
            if add:
                self._open(chat)
            if remove:
                chat.meta["managers"] = [u for u in chat.managers if u not in remove]
            chat.meta["members"] = sorted((set(chat.members) | set(add)) - set(remove))
            write_json(os.path.join(chat.dir, "meta.json"), chat.meta)
            if add:
                chat.append({"kind": "added", "from": me, "at": now(), "users": add}, self.fsync)
            if remove:
                chat.append({"kind": "removed", "from": me, "at": now(), "users": remove}, self.fsync)
        self.wake([*chat.members, *remove], None)

    def join(self, me: str, ref: str) -> Chat:
        """Joins a public channel, by its name or id. A private one takes being added by a member."""
        ref = ref.strip()
        chat = self.chats.get(ref) or self.channels.get(ref.lstrip("#").strip().lower())
        if not chat or not chat.name or (not chat.public and me not in chat.members):
            raise Error(404, f"There is no channel {ref}.")  # a private channel's name and id stay with its members
        with self.lock:  # so the channel cannot turn private between the check and the join
            if me in chat.members:
                return chat
            if not chat.public:  # made private since the check above
                raise Error(404, f"There is no channel {ref}.")
            refusal = self.policy.join(me, chat.members)
            if refusal:
                raise Error(403, refusal)
            self.change_members(me, chat, add=[me])
        return chat

    def set_public(self, me: str, chat: Chat, public: bool):
        """Makes a channel public (anyone may find and join it) or private (only its members may add people). Members
        stay either way."""
        if not chat.name:
            raise Error(400, "Only a channel can be public. A direct chat holds exactly its members.")
        self.require(me, "make_public" if public else "make_private", chat)
        self.room()
        with self.lock:
            self._open(chat)
            if chat.public == public:
                return
            chat.meta["public"] = public
            write_json(os.path.join(chat.dir, "meta.json"), chat.meta)
        with chat.lock:
            chat.append({"kind": "visibility", "from": me, "at": now(), "public": public}, self.fsync)

    def rename(self, me: str, chat: Chat, name: str):
        if not chat.name:
            raise Error(400, "Only a channel has a name. Make a channel to name a group.")
        name = self._channel_name(name)
        self.require(me, "rename_channel", chat)
        self.room()
        if self.policy.posters and chat.name.lower() in self.policy.posters and name.lower() != chat.name.lower():
            raise Error(403, f"The rules name #{chat.name} for who may post in it, so it keeps its name: a new one "
                             f"would drop the rule. Naming it by its id, {chat.id}, in the rules allows renaming.")
        with self.lock:
            self._open(chat)
            self._name_free(me, name, chat)
            del self.channels[chat.name.lower()]
            chat.meta["name"] = name
            self.channels[name.lower()] = chat
            write_json(os.path.join(chat.dir, "meta.json"), chat.meta)
        with chat.lock:
            chat.append({"kind": "renamed", "from": me, "at": now(), "name": name}, self.fsync)

    def describe(self, me: str, chat: Chat, description: str):
        """Sets what a channel is for. An empty description removes it."""
        if not chat.name:
            raise Error(400, "Only a channel has a description. A direct chat is described by its members.")
        description = self._description(description)
        self.require(me, "describe_channel", chat)
        self.room()
        with self.lock:
            self._open(chat)
            if chat.description == description:
                return
            chat.meta["description"] = description
            write_json(os.path.join(chat.dir, "meta.json"), chat.meta)
        with chat.lock:
            chat.append({"kind": "described", "from": me, "at": now(), "description": description}, self.fsync)

    def set_managers(self, me: str, chat: Chat, add=(), remove=()):
        """Names a channel's managers, from its members, or ends their role."""
        if not chat.name:
            raise Error(400, "Only a channel has managers. A direct chat has only its members.")
        self.require(me, "set_managers", chat)
        self.room()
        add, remove = list(dict.fromkeys(map(self.user, add))), list(dict.fromkeys(map(self.user, remove)))
        with self.lock, chat.lock:
            self._open(chat)
            strangers = [u for u in add if u not in chat.members]
            if strangers:
                raise Error(400, f"{', '.join(strangers)} {'is' if len(strangers) == 1 else 'are'} not in "
                                 f"#{chat.name}. Add them first.")
            add = [u for u in add if u not in chat.managers]
            remove = [u for u in remove if u in chat.managers]
            if not add and not remove:
                return
            chat.meta["managers"] = sorted((set(chat.managers) | set(add)) - set(remove))
            write_json(os.path.join(chat.dir, "meta.json"), chat.meta)
            chat.append({"kind": "managers", "from": me, "at": now(), **({"added": add} if add else {}),
                         **({"removed": remove} if remove else {})}, self.fsync)

    def archive(self, me: str, chat: Chat, archived: bool):
        """Archives a channel, or unarchives it. An archived channel keeps its members and history, and its members
        still read it, but nothing new is written to it: no message, edit, deletion, reaction, new member, new name,
        new description or change of visibility. Members may still leave or be removed."""
        if not chat.name:
            raise Error(400, "Only a channel can be archived. A direct chat stays as it is.")
        self.require(me, "archive_channel", chat)
        self.room()
        with self.lock, chat.lock:  # under the chat's lock, so a send sees the channel archived before or after
            if chat.archived == archived:
                return
            chat.meta["archived"] = archived
            write_json(os.path.join(chat.dir, "meta.json"), chat.meta)
            chat.append({"kind": "archived" if archived else "unarchived", "from": me, "at": now()}, self.fsync)

    # ------------------------------------------------------------------ messages

    def mentions(self, chat: Chat, text: str, sender: str) -> list:
        """Members the text mentions: @id, @Full Name, @First when no one else in the chat has that first name, or
        @all. A mention that could mean two members is refused, so the sender writes the id instead."""
        work, others = text.lower(), [u for u in chat.members if u != sender]
        if re.search(r"@(all|everyone|here|channel)\b", work):
            return others
        found = set()
        for u in sorted(others, key=len, reverse=True):
            pattern = rf"@{re.escape(u)}(?![\w-])"
            if re.search(pattern, work):
                found.add(u)
                work = re.sub(pattern, " ", work)
        for part in ("full", "first"):
            groups = collections.defaultdict(list)
            for u in others:
                words = self.name(u).lower().split()
                if words:
                    groups[" ".join(words) if part == "full" else words[0]].append(u)
            for name in sorted(groups, key=len, reverse=True):
                pattern = rf"@{re.escape(name)}(?![\w-])"
                if re.search(pattern, work):
                    if len(groups[name]) > 1:
                        ids = " or ".join(f"@{u}" for u in groups[name])
                        raise Error(400, f"@{name} could mean {ids}. Write {ids} instead.")
                    found.add(groups[name][0])
                    work = re.sub(pattern, " ", work)
        return sorted(found)

    def send(self, me: str, chat: Chat, text: str, reply_to: str | None = None, attachments=()) -> dict:
        """Writes a message. Attachments, if any, are already stored (Blobs.keep), and are released again if the
        message cannot be written."""
        try:
            return self._send(me, chat, text, reply_to, list(attachments))
        except BaseException:
            for a in attachments:
                self.blobs.release(a["sha256"])
            raise

    def _send(self, me: str, chat: Chat, text: str, reply_to: str | None, attachments: list) -> dict:
        if not attachments and (not text or not text.strip()):
            raise Error(400, "The message is empty.")
        if len(text) > self.settings["max_text"]:
            raise Error(400, f"The message is longer than {self.settings['max_text']} characters.")
        if not attachments:  # files took their room before they were read: see reserve()
            self.room()
        event = {"kind": "message", "from": me, "text": text}
        if attachments:
            event["attachments"] = attachments
        if reply_to:
            target, e = self.message(me, reply_to)
            if target is not chat:
                raise Error(400, "A reply must be in the same chat as the message it answers.")
            event["reply_to"] = f"{chat.id}:{e['seq']}"
        # Membership changes take the chat's lock too, so these checks still hold when the line is written. Only this
        # chat waits: sends to other chats go on in parallel.
        with chat.lock:
            self._member(me, chat)
            self._open(chat)
            refusal = self.policy.send(me, chat.members, chat.name, chat.id)
            if refusal:
                raise Error(403, refusal)
            mentioned = self.mentions(chat, text, me)
            if mentioned:
                event["mentions"] = mentioned
            # Timestamps cannot go backwards within a chat, including after a clock adjustment.
            event["at"] = max(now(), chat.last["at"] if chat.last else "")
            e = chat.append(event, self.fsync)
            for a in attachments:
                self.files[a["id"]] = chat
        if attachments:
            with self.counting:
                self.stored[me] += sum(a["size"] for a in attachments)
        self.wake(chat.members, me)
        return chat.view(e)

    def attachment(self, me: str | None, attachment_id: str) -> tuple:
        """(the attachment as stored, the path of its bytes), if it is in one of my chats and its message stands.
        me=None is the operator's viewer, which sees every chat."""
        chat = self.files.get(attachment_id)
        if not chat or (me is not None and me not in chat.members) or attachment_id not in chat.files or \
                not self.sees(me, chat, chat.files[attachment_id][0]):
            raise Error(404, f"No attachment {attachment_id} in your chats.")
        a = chat.files[attachment_id][1]
        return a, self.blobs.path(a["sha256"])

    def message(self, me: str | None, message_id: str):
        """(chat, the stored message event) for a message id the caller can see. me=None sees every chat."""
        match = MESSAGE_ID.fullmatch(message_id.strip())
        chat = match and self.chats.get(match[1])
        if not chat or (me is not None and me not in chat.members) or not chat.is_message(int(match[2])) or \
                not self.sees(me, chat, int(match[2])):
            raise Error(404, f"No message {message_id} in your chats.")
        return chat, chat.read([int(match[2])])[0]

    def thread(self, me: str | None, message_id: str) -> tuple:
        """(chat, [(seq, depth), ...]): the whole thread a message is in. Its first message comes first, at depth 0, and
        each reply follows the message it answers, one level deeper. Replies to the same message keep their order."""
        chat, e = self.message(me, message_id)
        top = e["seq"]
        while top in chat.parent:
            top = chat.parent[top]
        # A message I may not read is left out, and so is its level: depth counts only the messages above it I may read.
        found, todo = [], [(top, 0)]
        while todo:
            seq, depth = todo.pop()
            shown = self.sees(me, chat, seq)
            if shown:
                found.append((seq, depth))
            todo += [(reply, depth + 1 if shown else depth) for reply in reversed(chat.replies.get(seq, ()))]
        return chat, found

    def change(self, me: str, message_id: str, kind: str, **fields) -> dict:
        """An edit, delete or reaction on a message. Only the sender may edit. The sender may delete, and so may whoever
        the rules allow to delete another person's message: administrators, by default."""
        chat, e = self.message(me, message_id)
        if kind == "edit" and e["from"] != me:
            raise Error(403, "Only the sender can edit a message.")
        if kind == "edit":
            text = fields.get("text") or ""
            if not isinstance(text, str) or not text.strip():
                raise Error(400, "The new text is empty. To remove the message, delete it.")
            if len(text) > self.settings["max_text"]:
                raise Error(400, f"The message is longer than {self.settings['max_text']} characters.")
        if kind != "delete":  # a deletion frees room
            self.room()
        with chat.lock:  # as in send: membership cannot change underneath, and other chats are not held up
            self._member(me, chat)
            self._open(chat)
            if kind == "edit":
                refusal = self.policy.send(me, chat.members, chat.name, chat.id)
            elif kind == "react":
                refusal = self.policy.react(me, chat.members, chat.name)
            else:
                refusal = None if e["from"] == me else \
                    self.policy.check("delete_message", self.policy.roles(me, chat.managers))
            if refusal:
                raise Error(403, refusal)
            if kind == "edit":
                fields["mentions"] = self.mentions(chat, fields["text"], me)
            if "deleted" in chat.changes.get(e["seq"], {}):
                raise Error(400, f"{message_id} was deleted.")
            chat.append({"kind": kind, "from": me, "at": now(), "target": e["seq"], **fields}, self.fsync)
            if kind == "delete":  # its words leave the disk too, not only the screen
                chat.redact(e["seq"], self.fsync)
        if kind == "delete":  # the message's files go with it, once nothing else refers to their bytes
            for a in e.get("attachments", ()):
                self.files.pop(a["id"], None)
                self.blobs.release(a["sha256"])
                with self.counting:
                    self.stored[e["from"]] -= a["size"]
        self.wake(chat.members, me)
        return chat.view(e)

    # ------------------------------------------------------------------ room on disk, and how much one user holds

    def room(self):
        """Refuses a message when the disk has less than min_free bytes free, beyond what uploads under way will take.
        The disk is asked at most twice a second: asking takes 50 to 150 microseconds, more than a send's other work."""
        floor = self.settings["min_free"]
        if not floor:
            return
        if time.monotonic() - self.free_at > 0.5:
            self.free, self.free_at = shutil.disk_usage(self.root).free, time.monotonic()
        if self.free - self.reserved < floor:
            raise Error(507, FULL)

    @contextlib.contextmanager
    def reserve(self, me: str, size: int):
        """Holds `size` bytes for an upload of `me`'s while it is read and stored, so that uploads at the same moment
        cannot together go below min_free on the disk or past a user's quota of stored files."""
        floor, quota = self.settings["min_free"], self.settings["quota"]
        with self.counting:
            if quota and self.stored[me] + self.pending[me] + size > quota:
                raise Error(413, f"These files would take you past your quota of {quota:,} bytes of stored files: "
                                 f"{self.stored[me]:,} are stored. Delete messages with files no one needs.")
            if floor:
                self.free, self.free_at = shutil.disk_usage(self.root).free, time.monotonic()
                if self.free - self.reserved - size < floor:
                    raise Error(507, FULL)
            self.reserved += size
            self.pending[me] += size
        try:
            yield
        finally:
            with self.counting:
                self.reserved -= size
                self.pending[me] -= size

    @contextlib.contextmanager
    def request(self, me: str):
        """Counts a request of `me`'s while it is under way: a wait, an upload, anything. At most max_requests at once,
        so no one user can hold every connection the server has."""
        with self.counting:
            if self.requests[me] >= self.settings["max_requests"]:
                raise Error(429, f"You have {self.requests[me]} requests under way, the most at once. Wait for one to "
                                 f"end.")
            self.requests[me] += 1
        try:
            yield
        finally:
            with self.counting:
                self.requests[me] -= 1

    # ------------------------------------------------------------------ read positions, settings, waking

    def position(self, user: str, chat: Chat) -> int:
        return self.state[user]["read"].get(chat.id, 0)

    def mark_read(self, user: str, chat: Chat, seq: int):
        """Moves a read position forward, and writes a read line in the chat's log as the receipt."""
        with self.lock:
            read = self.state[user]["read"]
            seq = min(seq, chat.seq)
            if seq <= read.get(chat.id, 0):
                return
            with chat.lock:  # still under self.lock, so one user's read lines stay in increasing order
                chat.append({"kind": "read", "from": user, "at": now(), "upto": seq}, self.fsync)
            read[chat.id] = seq
            self.dirty.add(user)

    def shown_upto(self, user: str, chat: Chat, shown: set) -> int:
        """How far `user`'s read position may move once the messages `shown` reached them: past each unread message up
        to the first one not shown, or 0 when that is the first. A message a search or filter left out stays unread."""
        upto = 0
        for i in self.indexes(user, chat, self.position(user, chat)):
            if chat.senders[i] == user:
                continue
            if chat.messages[i] not in shown:
                break
            upto = chat.messages[i]
        return upto

    def level(self, user: str, chat: Chat) -> str:
        return self.state[user]["notify"].get(chat.id, self.settings["default_notify"])

    def set_level(self, user: str, chat: Chat, level: str):
        if level not in LEVELS:
            raise Error(400, f"The level is one of {', '.join(LEVELS)}.")
        with self.lock:
            self.state[user]["notify"][chat.id] = level
            self.dirty.add(user)
        self.flush()
        self.wake([user], None)

    def flush(self):
        """Writes the read positions and settings that changed. The server calls this every second."""
        with self.lock:
            if self._closed:
                return
            for user in list(self.dirty):
                if user in self.state:
                    write_json(self.file("state", f"{user}.json"), self.state[user])
                self.dirty.discard(user)

    def signal(self, user: str) -> threading.Condition:
        with self.lock:
            return self.signals.setdefault(user, threading.Condition())

    def wake(self, members, sender: str | None):
        for user in members:
            cond = self.signals.get(user)
            if cond is not None and user != sender:
                with cond:
                    cond.notify_all()

    def close(self):
        with self.lock:
            if self._closed:
                return
            try:
                self.flush()
            finally:
                try:
                    for chat in self.chats.values():
                        with chat.lock:
                            chat.close()
                finally:
                    self._closed = True
                    if self._directory_lock:
                        self._directory_lock.close()
