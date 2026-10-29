"""Stress test: many agents sending and waiting at once against a real server process, killed and restarted midway.

    python3 bench/stress.py [--agents 100] [--seconds 60] [--rate 1] [--crash-at 30] [--processes 8]

Each agent belongs to a few DMs and groups. It sends to one of them at `rate` messages a second on average, and
waits for new messages the rest of the time. At `crash-at` seconds the server gets SIGKILL and is started again.
At the end each agent drains what it has not received, and the checks run against the server's own log files:

- every send the server acknowledged is in a log exactly once
- every agent received every message from others in its chats
- how many deliveries were repeats (a crash can lose up to a second of read positions)
"""
import argparse
import collections
import glob
import json
import multiprocessing
import os
import random
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


def start_server(data, port):
    proc = subprocess.Popen([sys.executable, "-m", "swarmchat", "serve", "--data", data, "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=open(os.path.join(os.path.dirname(data), "server.err"), "a"),
                            env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))})
    t = time.time()
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1).read()
            return proc, time.time() - t
        except OSError:
            time.sleep(0.02)


def agent(name, url, token, chats, end, rate, out):
    cl, rnd = Client(url, token), random.Random(name)
    sent, unsure, got, delays, ops, errors = [], [], [], [], 0, collections.Counter()
    n, next_send = 0, time.time() + rnd.expovariate(rate)
    while time.time() < end:
        try:
            if time.time() >= next_send:
                n += 1
                text = f"{name}#{n} {time.time():.6f}"
                try:
                    cl.send(chat=rnd.choice(chats), text=text)
                    sent.append(text)
                except ClientError as e:
                    if e.status:
                        raise
                    unsure.append(text)  # the server may or may not have stored it before it died
                    time.sleep(0.2)
                next_send += rnd.expovariate(rate)
            else:
                r = cl.wait(timeout=min(2.0, max(0.01, next_send - time.time())))
                now = time.time()
                for m in r["messages"]:
                    got.append(m["text"])
                    delays.append(now - float(m["text"].split()[1]))
            ops += 1
        except ClientError as e:
            errors[e.status] += 1
            time.sleep(0.2)
    try:
        cl.close()  # confirms the last messages it received, as a real program does when it ends
    except ClientError:
        pass
    out[name] = {"sent": sent, "unsure": unsure, "got": got, "delays": delays, "ops": ops, "errors": dict(errors)}


def drain(url, token):
    cl, got = Client(url, token), []
    while True:
        r = cl.read(limit=500)
        got += [m["text"] for m in r["messages"]]
        if not r["unread_left"]:
            return got


def worker(agents, url, end, rate, path):
    out, threads = {}, []
    for name, token, chats in agents:
        t = threading.Thread(target=agent, args=(name, url, token, chats, end, rate, out))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    for name, token, _ in agents:
        out[name]["got"] += drain(url, token)
    with open(path, "w") as f:
        json.dump(out, f)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agents", type=int, default=100)
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--rate", type=float, default=1.0, help="messages a second per agent, on average")
    p.add_argument("--crash-at", type=float, default=30, help="seconds in; 0 for no crash")
    p.add_argument("--processes", type=int, default=8)
    p.add_argument("--port", type=int, default=18600)
    p.add_argument("--dir", default=tempfile.gettempdir())
    args = p.parse_args()
    random.seed(1)
    work = tempfile.mkdtemp(prefix="swarmchat-stress-", dir=args.dir)
    data, url = os.path.join(work, "data"), f"http://127.0.0.1:{args.port}"
    server, _ = start_server(data, args.port)
    with open(os.path.join(data, "admin.token")) as f:
        admin = Client(url, f.read().strip())

    names = [f"a{i:03d}" for i in range(args.agents)]
    tokens = {n: admin.user_add(id=n, name=f"Agent {n}")["token"] for n in names}
    clients = {n: Client(url, tokens[n]) for n in names}
    chats = collections.defaultdict(list)  # where each agent sends: people for direct chats, #name for channels
    for n in names:  # two direct chats each, with random others, made by their first message
        for other in random.sample([o for o in names if o != n], 2):
            chats[n].append(other)
            chats[other].append(n)
    for g in range(args.agents // 3):  # channels of 3 to 12
        members = random.sample(names, random.randint(3, 12))
        clients[members[0]].channel_create(name=f"group {g}", members=",".join(members[1:]))
        for m in members:
            chats[m].append(f"#group {g}")

    end = time.time() + args.seconds
    agents = [(n, tokens[n], sorted(set(chats[n]))) for n in names]
    paths, procs = [], []
    ctx = multiprocessing.get_context("fork")
    for k in range(args.processes):
        path = os.path.join(work, f"result-{k}.json")
        proc = ctx.Process(target=worker, args=(agents[k::args.processes], url, end, args.rate, path))
        proc.start()
        paths.append(path)
        procs.append(proc)
    restart = None
    if args.crash_at:
        time.sleep(args.crash_at)
        server.send_signal(signal.SIGKILL)
        server.wait()
        server, restart = start_server(data, args.port)
    for proc in procs:
        proc.join()
    server.send_signal(signal.SIGTERM)
    server.wait()

    results = {}
    for path in paths:
        with open(path) as f:
            results.update(json.load(f))
    stored, members = collections.Counter(), {}
    for meta_path in glob.glob(os.path.join(data, "chats", "*", "meta.json")):
        with open(meta_path) as f:
            meta = json.load(f)
        members[meta["id"]] = set(meta["members"])
    by_chat = collections.defaultdict(list)
    for log in glob.glob(os.path.join(data, "chats", "*", "log", "*.jsonl")):
        chat = log.split(os.sep)[-3]
        with open(log) as f:
            for line in f:
                e = json.loads(line)
                if e["kind"] == "message":
                    stored[e["text"]] += 1
                    by_chat[chat].append(e)

    sent = [t for r in results.values() for t in r["sent"]]
    unsure = [t for r in results.values() for t in r["unsure"]]
    missing_acked = [t for t in sent if stored[t] == 0]
    duplicated = [t for t, k in stored.items() if k > 1]
    missing_delivery, repeats, expected_total = 0, 0, 0
    for n, r in results.items():
        expected = {e["text"] for chat, events in by_chat.items() if n in members[chat] for e in events if e["from"] != n}
        got = collections.Counter(r["got"])
        expected_total += len(expected)
        missing_delivery += len(expected - set(got))
        repeats += sum(k - 1 for k in got.values())
    delays = sorted(d for r in results.values() for d in r["delays"])
    ops = sum(r["ops"] for r in results.values())
    errors = collections.Counter()
    for r in results.values():
        errors.update(r["errors"])

    print(f"{args.agents} agents in {len({c for c in members})} chats, {args.seconds:.0f} s, {args.rate} msg/s each, "
          f"{args.processes} client processes")
    print(f"server killed at {args.crash_at:.0f} s, back up in {restart:.2f} s" if restart else "no crash")
    print(f"sends acknowledged: {len(sent)} ({len(sent) / args.seconds:.0f} a second), unsure during the crash: "
          f"{len(unsure)}, of which stored: {sum(1 for t in unsure if stored[t])}")
    print(f"client operations: {ops} ({ops / args.seconds:.0f} a second), errors by status: {dict(errors)}")
    print(f"CHECK acknowledged sends missing from the logs: {len(missing_acked)}")
    print(f"CHECK messages stored twice: {len(duplicated)}")
    print(f"CHECK deliveries owed: {expected_total}, never received: {missing_delivery}, received twice: {repeats}")
    if delays:
        print(f"send to receipt by a waiting agent: p50 {statistics.median(delays) * 1000:.0f} ms, "
              f"p99 {delays[int(len(delays) * 0.99)] * 1000:.0f} ms, max {delays[-1] * 1000:.0f} ms")
    print(f"data left in {work}")


if __name__ == "__main__":
    main()
