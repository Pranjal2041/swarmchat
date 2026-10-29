"""Storage benchmark for swarmchat: per-chat append-only files vs one SQLite database vs one SQLite per chat.

Standard library only. Runs a messaging workload against each design in a temporary directory and prints one table:
bulk load rate, concurrent send rate and latency (with and without fsync), read latencies, edit, search and startup.

    python3 bench/storage_bench.py [--dir /path/on/the/disk/to/test] [--messages 300000] [--chats 200]
"""
import argparse
import json
import os
import random
import shutil
import sqlite3
import statistics
import tempfile
import threading
import time
from array import array

WORDS = ("build failure deploy review ticket rollback incident release patch merge request pipeline flaky test "
         "branch commit cache timeout migration schema index alert pager runbook outage postmortem deadline").split()


def text():
    return " ".join(random.choice(WORDS) for _ in range(random.randint(8, 40)))


# ---------------------------------------------------------------- designs

class Files:
    """One folder per chat with an append-only JSONL log. In memory: each chat's line offsets and its edits."""
    name = "files"

    def __init__(self, root, durable):
        self.root, self.durable, self.chats, self.guard = root, durable, {}, threading.Lock()
        os.makedirs(f"{root}/chats", exist_ok=True)
        for chat in os.listdir(f"{root}/chats"):
            self._load(chat)

    def _load(self, chat):
        path = f"{self.root}/chats/{chat}/log.jsonl"
        offsets, edits, pos = array("Q"), {}, 0
        if os.path.exists(path):
            with open(path, "rb") as f:
                for line in f:
                    if b'"edit"' in line:
                        e = json.loads(line)
                        edits[e["target"]] = e["text"]
                    else:
                        offsets.append(pos)
                    pos += len(line)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.chats[chat] = {"w": open(path, "ab"), "path": path, "offsets": offsets, "edits": edits,
                            "lock": threading.Lock()}

    def _chat(self, chat):
        c = self.chats.get(chat)
        if c is None:
            with self.guard:
                if chat not in self.chats:
                    self._load(chat)
            c = self.chats[chat]
        return c

    def _append(self, c, record):
        line = (json.dumps(record) + "\n").encode()
        pos = c["w"].tell()
        c["w"].write(line)
        c["w"].flush()
        if self.durable:
            os.fsync(c["w"].fileno())
        return pos

    def send(self, chat, sender, body):
        c = self._chat(chat)
        with c["lock"]:
            seq = len(c["offsets"]) + 1
            c["offsets"].append(self._append(c, {"seq": seq, "from": sender, "at": time.time(), "text": body}))
            return seq

    def edit(self, chat, seq, body):
        c = self._chat(chat)
        with c["lock"]:
            self._append(c, {"kind": "edit", "target": seq, "text": body, "at": time.time()})
            c["edits"][seq] = body

    def read(self, chat, after, limit):
        c = self._chat(chat)
        offs = c["offsets"][after:after + limit]
        if not offs:
            return []
        out = []
        with open(c["path"], "rb") as f:
            f.seek(offs[0])
            for _ in offs:
                m = json.loads(f.readline())
                while m.get("kind") == "edit":
                    m = json.loads(f.readline())
                if m["seq"] in c["edits"]:
                    m["text"] = c["edits"][m["seq"]]
                out.append(m)
        return out

    def last_seq(self, chat):
        return len(self._chat(chat)["offsets"])

    def search(self, word):
        hits, needle = 0, word.encode()
        for c in self.chats.values():
            with open(c["path"], "rb") as f:
                for line in f:
                    if needle in line:
                        hits += 1
        return hits

    def close(self):
        for c in self.chats.values():
            c["w"].close()


class SqliteOne:
    """One SQLite database in WAL mode. One writer connection behind a lock, one reader connection per thread."""
    name = "sqlite-one"

    def __init__(self, root, durable):
        self.path, self.durable = f"{root}/swarmchat.db", durable
        self.local, self.lock = threading.local(), threading.Lock()
        self.w = self._connect()
        self.w.executescript("""
            create table if not exists messages (chat text, seq integer, sender text, at real, text text,
                                                 edited_at real, primary key (chat, seq)) without rowid;
        """)
        self.seqs = dict(self.w.execute("select chat, max(seq) from messages group by chat"))

    def _connect(self):
        c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        c.execute("pragma journal_mode=wal")
        c.execute(f"pragma synchronous={'full' if self.durable else 'normal'}")
        return c

    def _r(self):
        if not hasattr(self.local, "c"):
            self.local.c = self._connect()
        return self.local.c

    def send(self, chat, sender, body):
        with self.lock:
            seq = self.seqs.get(chat, 0) + 1
            self.w.execute("insert into messages values (?,?,?,?,?,null)", (chat, seq, sender, time.time(), body))
            self.seqs[chat] = seq
            return seq

    def edit(self, chat, seq, body):
        with self.lock:
            self.w.execute("update messages set text=?, edited_at=? where chat=? and seq=?", (body, time.time(), chat, seq))

    def read(self, chat, after, limit):
        rows = self._r().execute("select seq, sender, at, text from messages where chat=? and seq>? order by seq limit ?",
                                 (chat, after, limit)).fetchall()
        return [{"seq": s, "from": f, "at": a, "text": t} for s, f, a, t in rows]

    def last_seq(self, chat):
        return self.seqs.get(chat, 0)

    def search(self, word):
        return self._r().execute("select count(*) from messages where instr(text, ?) > 0", (word,)).fetchone()[0]

    def close(self):
        self.w.close()


class SqlitePerChat(SqliteOne):
    """One SQLite database per chat, so writes to different chats never wait for each other."""
    name = "sqlite-per-chat"

    def __init__(self, root, durable):
        self.root, self.durable, self.dbs, self.guard = root, durable, {}, threading.Lock()
        os.makedirs(f"{root}/chats", exist_ok=True)
        for name in os.listdir(f"{root}/chats"):
            if name.endswith(".db"):
                self._db(name[:-3])

    def _db(self, chat):
        d = self.dbs.get(chat)
        if d is None:
            with self.guard:
                if chat not in self.dbs:
                    c = sqlite3.connect(f"{self.root}/chats/{chat}.db", check_same_thread=False, isolation_level=None)
                    c.execute("pragma journal_mode=wal")
                    c.execute(f"pragma synchronous={'full' if self.durable else 'normal'}")
                    c.execute("create table if not exists messages (seq integer primary key, sender text, at real, "
                              "text text, edited_at real)")
                    seq = c.execute("select coalesce(max(seq), 0) from messages").fetchone()[0]
                    self.dbs[chat] = {"c": c, "lock": threading.Lock(), "seq": seq}
            d = self.dbs[chat]
        return d

    def send(self, chat, sender, body):
        d = self._db(chat)
        with d["lock"]:
            d["seq"] += 1
            d["c"].execute("insert into messages values (?,?,?,?,null)", (d["seq"], sender, time.time(), body))
            return d["seq"]

    def edit(self, chat, seq, body):
        d = self._db(chat)
        with d["lock"]:
            d["c"].execute("update messages set text=?, edited_at=? where seq=?", (body, time.time(), seq))

    def read(self, chat, after, limit):
        d = self._db(chat)
        with d["lock"]:
            rows = d["c"].execute("select seq, sender, at, text from messages where seq>? order by seq limit ?",
                                  (after, limit)).fetchall()
        return [{"seq": s, "from": f, "at": a, "text": t} for s, f, a, t in rows]

    def last_seq(self, chat):
        return self._db(chat)["seq"]

    def search(self, word):
        hits = 0
        for d in self.dbs.values():
            with d["lock"]:
                hits += d["c"].execute("select count(*) from messages where instr(text, ?) > 0", (word,)).fetchone()[0]
        return hits

    def close(self):
        for d in self.dbs.values():
            d["c"].close()


# ---------------------------------------------------------------- workload

def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def timed(fn, n):
    lat = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        lat.append(time.perf_counter() - t)
    return lat


def concurrent_sends(store, chats, threads, per_thread):
    lat, lock = [], threading.Lock()

    def worker():
        mine = []
        for _ in range(per_thread):
            chat = random.choice(chats)
            t = time.perf_counter()
            store.send(chat, "u%d" % random.randrange(500), text())
            mine.append(time.perf_counter() - t)
        with lock:
            lat.extend(mine)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker) for _ in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return threads * per_thread / (time.perf_counter() - t0), lat


def run(design, base, args):
    root = tempfile.mkdtemp(prefix=f"swarmchat-{design.name}-", dir=base)
    chats = ["c%04d" % i for i in range(args.chats)]
    r = {}
    store = design(root, durable=False)
    t = time.perf_counter()
    for i in range(args.messages):
        store.send(chats[i % len(chats)], "u%d" % (i % 500), text())
    r["bulk load, msgs/s"] = args.messages / (time.perf_counter() - t)

    for threads in (1, 16, 64):
        rate, lat = concurrent_sends(store, chats, threads, max(200, 6000 // threads))
        r[f"send x{threads} threads, msgs/s"] = rate
        r[f"send x{threads}, p99 ms"] = pct(lat, 0.99) * 1000

    rl = timed(lambda: store.read(c := random.choice(chats), max(0, store.last_seq(c) - 20), 20), 2000)
    r["read last 20 of a chat, p50 ms"] = statistics.median(rl) * 1000
    r["read last 20 of a chat, p99 ms"] = pct(rl, 0.99) * 1000

    def unread():  # one agent catching up: 10 of its chats, up to 50 new messages each
        for c in random.sample(chats, 10):
            store.read(c, max(0, store.last_seq(c) - 5), 50)
    ul = timed(unread, 300)
    r["unread across 10 chats, p50 ms"] = statistics.median(ul) * 1000

    el = timed(lambda: store.edit(c := random.choice(chats), random.randint(1, store.last_seq(c)), "edited " + text()), 2000)
    r["edit a message, p50 ms"] = statistics.median(el) * 1000

    t = time.perf_counter()
    store.search("cache timeout")
    r["search all messages, s"] = time.perf_counter() - t
    store.close()

    t = time.perf_counter()
    store = design(root, durable=False)
    r["startup with all messages, s"] = time.perf_counter() - t
    store.close()

    store = design(root, durable=True)
    for threads in (1, 16, 64):
        rate, lat = concurrent_sends(store, chats, threads, max(100, 1500 // threads))
        r[f"durable send x{threads}, msgs/s"] = rate
        r[f"durable send x{threads}, p99 ms"] = pct(lat, 0.99) * 1000
    store.close()

    r["disk used, MB"] = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(root) for f in fs) / 1e6
    shutil.rmtree(root)
    return r


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default=tempfile.gettempdir())
    p.add_argument("--messages", type=int, default=300_000)
    p.add_argument("--chats", type=int, default=200)
    args = p.parse_args()
    random.seed(7)
    results = {}
    for design in (Files, SqliteOne, SqlitePerChat):
        t = time.perf_counter()
        results[design.name] = run(design, args.dir, args)
        print(f"# {design.name} done in {time.perf_counter() - t:.0f} s", flush=True)
    names = list(results)
    print(f"\n{args.messages:,} messages in {args.chats} chats, on {args.dir}, sqlite {sqlite3.sqlite_version}\n")
    print(f"{'':42s}" + "".join(f"{n:>17s}" for n in names))
    for key in results[names[0]]:
        print(f"{key:42s}" + "".join(f"{results[n][key]:>17,.2f}" for n in names))


if __name__ == "__main__":
    main()
