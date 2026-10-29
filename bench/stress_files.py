"""Load test for attachments: many agents sending, downloading and deleting files at once against a real server.

    python3 bench/stress_files.py [--agents 16] [--seconds 60] [--dir /path/on/the/disk/to/test]

Each agent, again and again: sends a message with 1 to 3 files (10 KB to 8 MB, some shared by everyone so the same
bytes are sent many times), downloads a file it may see and checks it byte for byte, sends a plain message, or deletes
one of its own messages. Afterwards, from the server's own folder:

- every download matched the file that was sent
- the server holds exactly the distinct contents still referred to by a message, once each
- no upload is left half-written, and the server log has no traceback
"""
import argparse
import collections
import hashlib
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from swarmchat.client import Client, ClientError  # noqa: E402


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agents", type=int, default=16)
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--port", type=int, default=18950)
    p.add_argument("--dir", default=tempfile.gettempdir())
    args = p.parse_args()
    random.seed(3)
    work = tempfile.mkdtemp(prefix="swarmchat-files-stress-", dir=args.dir)
    data, url = os.path.join(work, "data"), f"http://127.0.0.1:{args.port}"
    log = open(os.path.join(work, "server.log"), "w+")
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server = subprocess.Popen([sys.executable, "-m", "swarmchat", "serve", "--data", data, "--port", str(args.port)],
                              stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONPATH": src})
    for _ in range(300):
        try:
            urllib.request.urlopen(f"{url}/v1/health", timeout=1).read()
            break
        except OSError:
            time.sleep(0.05)
    with open(os.path.join(data, "admin.token")) as f:
        admin = Client(url, f.read().strip())

    pool = os.path.join(work, "pool")  # the files agents send: a few shared by all, plus each agent's own
    os.makedirs(pool)
    sizes = [10_000, 200_000, 1_000_000, 3_000_000, 8_000_000]
    shared = []
    for i, n in enumerate(sizes):
        shared.append(os.path.join(pool, f"shared{i}.bin"))
        with open(shared[-1], "wb") as f:
            f.write(os.urandom(n))
    names = [f"a{i:02d}" for i in range(args.agents)]
    tokens = {n: admin.user_add(id=n, name=f"Agent {n}")["token"] for n in names}
    admin.close()
    channels = []
    for g in range(max(1, args.agents // 4)):
        members = random.sample(names, min(len(names), 6))
        Client(url, tokens[members[0]]).channel_create(name=f"files {g}", members=",".join(members[1:]))
        channels.append((f"#files {g}", set(members)))

    lock = threading.Lock()
    sent = {}  # attachment id -> {"sha", "size", "members", "message", "sender", "deleted"}
    stats = collections.Counter()
    up_seconds, down_seconds = [], []
    problems = []
    end = time.time() + args.seconds

    def agent(name):
        rnd = random.Random(name)
        cl = Client(url, tokens[name])
        own = []
        for i in range(3):
            own.append(os.path.join(pool, f"{name}-{i}.bin"))
            with open(own[-1], "wb") as f:
                f.write(os.urandom(rnd.choice(sizes)))
        mine = []  # (message id, attachment ids) I sent
        inbox = os.path.join(work, f"in-{name}")
        while time.time() < end:
            action = rnd.random()
            try:
                if action < 0.4:
                    where, members = rnd.choice(channels + [(o, {name, o}) for o in rnd.sample(names, 2) if o != name])
                    if name not in members:
                        continue
                    files = rnd.sample(shared + own, rnd.randint(1, 3))
                    t = time.perf_counter()
                    m = cl.send(chat=where, text="files", attach=files)["message"]
                    up_seconds.append(time.perf_counter() - t)
                    with lock:
                        for a, path in zip(m["attachments"], files):
                            sent[a["id"]] = {"sha": sha(path), "size": a["size"], "members": set(members),
                                             "message": m["id"], "sender": name, "deleted": False}
                            stats["uploads"] += 1
                            stats["bytes up"] += a["size"]
                    mine.append((m["id"], [a["id"] for a in m["attachments"]]))
                elif action < 0.8:
                    with lock:
                        mine_to_see = [i for i, a in sent.items() if name in a["members"]]
                    if not mine_to_see:
                        continue
                    att = rnd.choice(mine_to_see)
                    t = time.perf_counter()
                    try:
                        path = cl.download(att, to=inbox, force=True)
                    except ClientError as e:
                        with lock:
                            if e.status == 404 and sent[att]["deleted"]:
                                stats["refused after delete"] += 1
                                continue
                        raise
                    down_seconds.append(time.perf_counter() - t)
                    with lock:
                        expected = sent[att]
                    got = sha(path)
                    os.unlink(path)
                    with lock:
                        stats["downloads"] += 1
                        stats["bytes down"] += expected["size"]
                        if got != expected["sha"]:
                            problems.append(f"{name} downloaded {att} with different bytes")
                elif action < 0.9:
                    cl.send(chat=rnd.choice([n for n in names if n != name]), text="plain text")
                    stats["texts"] += 1
                elif mine:
                    message, atts = mine.pop(rnd.randrange(len(mine)))
                    with lock:  # mark first: a download racing the delete may be refused
                        for a in atts:
                            sent[a]["deleted"] = True
                    cl.delete(id=message)
                    stats["deletes"] += 1
            except ClientError as e:
                with lock:
                    problems.append(f"{name}: {e.status} {e}")
        cl.close()

    t0 = time.time()
    threads = [threading.Thread(target=agent, args=(n,)) for n in names]
    [t.start() for t in threads]
    [t.join() for t in threads]
    took = time.time() - t0
    server.send_signal(signal.SIGTERM)
    server.wait(30)

    live = {a["sha"] for a in sent.values() if not a["deleted"]}
    on_disk = {name for _, _, files in os.walk(os.path.join(data, "blobs")) for name in files}
    leftovers = os.listdir(os.path.join(data, "uploads"))
    log.seek(0)
    tracebacks = log.read().count("Traceback")
    print(f"{args.agents} agents, {took:.0f} s")
    print(f"uploads {stats['uploads']} ({stats['bytes up'] / 1e6:,.0f} MB, {stats['bytes up'] / 1e6 / took:,.1f} MB/s), "
          f"downloads {stats['downloads']} ({stats['bytes down'] / 1e6:,.0f} MB, "
          f"{stats['bytes down'] / 1e6 / took:,.1f} MB/s), plain messages {stats['texts']}, deletes {stats['deletes']}, "
          f"downloads refused after their message was deleted {stats['refused after delete']}")
    if up_seconds:
        print(f"send with files: p50 {statistics.median(up_seconds) * 1000:.0f} ms, "
              f"p99 {sorted(up_seconds)[int(len(up_seconds) * 0.99)] * 1000:.0f} ms")
    if down_seconds:
        print(f"download: p50 {statistics.median(down_seconds) * 1000:.0f} ms, "
              f"p99 {sorted(down_seconds)[int(len(down_seconds) * 0.99)] * 1000:.0f} ms")
    print(f"CHECK problems (wrong bytes or unexpected errors): {len(problems)} {problems[:5]}")
    print(f"CHECK stored contents: {len(on_disk)}, still referred to: {len(live)}, equal: {on_disk == live}")
    print(f"CHECK half-written uploads left: {len(leftovers)}")
    print(f"CHECK tracebacks in the server log: {tracebacks}")
    shutil.rmtree(work)


if __name__ == "__main__":
    main()
