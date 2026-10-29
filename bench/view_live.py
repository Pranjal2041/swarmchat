"""Live traffic for the viewer: agents of a run made by bench/view_data.py send, reply, react, read and attach files
through a running server, at a steady rate, until stopped.

    python3 bench/view_live.py DIR [--rate 500] [--processes 8] [--seconds 0] [--files 0.03]
"""
import argparse
import json
import multiprocessing
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from swarmchat.client import Client, ClientError  # noqa: E402

LINES = ["Rerunning the suite now.", "Deploy finished on staging.", "Can you check the latest numbers?",
         "Found it: an off-by-one in the pager.", "Merged. Watching the dashboards.", "Blocked on review.",
         "Latency is back under 200 ms.", "Who is on call tonight?", "Rolling back the canary.", "Ship it."]


def worker(server, tokens, rate, seconds, seed, counts, files, share):
    random.seed(seed)
    agents = []
    for uid, token in tokens:
        c = Client(server, token)
        try:
            chats = [ch["id"] for ch in c.chats(limit=500)["chats"]]
        except ClientError:
            continue
        if chats:
            agents.append((uid, c, chats))
    gap, end, next_at, sent = 1 / rate, time.time() + seconds if seconds else float("inf"), time.time(), 0
    while time.time() < end:
        uid, c, chats = random.choice(agents)
        chat = random.choice(chats)
        try:
            r = random.random()
            if r < 0.15:
                c.read(chat=chat, limit=50)  # catching up moves a read position: a receipt
            elif r < 0.2:
                last = c.read(chat=chat, last=5, peek=True)["messages"]
                if last:
                    c.react(id=random.choice(last)["id"], emoji=random.choice(["+1", "eyes", "rocket"]))
            else:
                reply = None
                if r < 0.45:
                    last = c.read(chat=chat, last=10, peek=True)["messages"]
                    reply = random.choice(last)["id"] if last else None
                attach = random.sample(files, 1) if files and random.random() < share else ()
                c.send(chat=chat, text=random.choice(LINES), attach=attach, reply_to=reply)
                sent += 1
                with counts.get_lock():
                    counts.value += 1
        except ClientError:
            pass
        next_at += gap
        time.sleep(max(0.0, next_at - time.time()))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("dir")
    p.add_argument("--rate", type=float, default=500, help="requests a second, in all")
    p.add_argument("--processes", type=int, default=8)
    p.add_argument("--agents", type=int, default=200, help="how many agents take part")
    p.add_argument("--seconds", type=float, default=0, help="0: until stopped")
    p.add_argument("--files", type=float, default=0.03, help="share of messages sent with a file from DIR.files")
    a = p.parse_args()
    with open(a.dir.rstrip("/") + ".tokens.json") as f:
        tokens = list(json.load(f).items())[:a.agents]
    with open(os.path.join(a.dir, "address")) as f:
        server = f.read().strip()
    folder = a.dir.rstrip("/") + ".files"
    files = sorted(os.path.join(folder, n) for n in os.listdir(folder)) if os.path.isdir(folder) else []
    counts = multiprocessing.Value("l", 0)
    procs = [multiprocessing.Process(target=worker, args=(server, tokens[i::a.processes], a.rate / a.processes, a.seconds, i, counts,
                                                          files, a.files))
             for i in range(a.processes)]
    for proc in procs:
        proc.start()
    started, last = time.time(), 0
    try:
        while any(proc.is_alive() for proc in procs):
            time.sleep(5)
            n = counts.value
            print(f"{time.time() - started:6.0f} s  {n:,} messages sent, {(n - last) / 5:.0f}/s", flush=True)
            last = n
    except KeyboardInterrupt:
        for proc in procs:
            proc.terminate()


if __name__ == "__main__":
    main()
