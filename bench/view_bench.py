"""Times every request the viewer makes, on a data folder (see bench/view_data.py for a large one).

    python3 bench/view_bench.py DIR

Loads the folder read-only, then runs each request a few times in this process, including turning its reply into
gzipped JSON as the server does for a browser. Prints the median time and the reply's size.
"""
import gzip
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from swarmchat import view  # noqa: E402
from swarmchat.store import Store  # noqa: E402


def timed(label, fn, runs=5):
    times, out = [], None
    for _ in range(runs):
        t = time.perf_counter()
        out = fn()
        data = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode()
        packed = gzip.compress(data, 4) if len(data) > 2048 else data
        times.append(time.perf_counter() - t)
    print(f"{label:<44} {statistics.median(times) * 1000:8.1f} ms  {len(data) / 1e3:9.1f} KB  {len(packed) / 1e3:8.1f} KB gzipped")
    return out


def main():
    root = sys.argv[1]
    t = time.perf_counter()
    store = Store(root, read_only=True)
    messages = sum(len(c.messages) for c in store.chats.values())
    print(f"loaded {len(store.users)} users, {len(store.chats)} chats, {messages:,} messages in {time.perf_counter() - t:.2f} s")
    busiest = max(store.chats.values(), key=lambda c: len(c.messages))
    agent = max(store.users, key=lambda u: sum(view._tally(c)[0].get(u, 0) for c in store.chats.values()))
    some = busiest.messages[len(busiest.messages) // 2]
    replied = next(s for s in reversed(busiest.messages) if s in busiest.parent)
    message_id = f"{busiest.id}:{replied}"
    print(f"busiest chat {busiest.id}: {len(busiest.messages):,} messages; busiest agent {agent}")
    timed("snapshot (every user and chat)", lambda: view.view_snapshot(store, {}))
    timed("messages: newest 150 of the busiest chat", lambda: view.view_messages(store, {"chat": busiest.id, "limit": 150}))
    timed("messages: 150 around one in the middle", lambda: view.view_messages(store, {"chat": busiest.id, "around": some, "limit": 150}))
    timed("thread", lambda: view.view_thread(store, {"id": message_id}))
    timed("receipts", lambda: view.view_receipts(store, {"id": message_id}))
    timed("posts: front page, hot", lambda: view.view_posts(store, {"sort": "hot", "limit": 40}), runs=3)
    timed("posts: front page, hot, again (cached)", lambda: view.view_posts(store, {"sort": "hot", "limit": 40}), runs=3)
    timed("posts: busiest chat, top", lambda: view.view_posts(store, {"chat": busiest.id, "sort": "top", "limit": 40}))
    timed("search: newest 150 anywhere (all activity)", lambda: view.view_search(store, {"limit": 150}))
    timed("search: a common word", lambda: view.view_search(store, {"text": "deploy", "limit": 100}))
    timed("search: a rare word", lambda: view.view_search(store, {"text": "typo", "limit": 100}))
    timed("search: a word in no message", lambda: view.view_search(store, {"text": "zebrafish", "limit": 100}))
    timed("search: from one agent", lambda: view.view_search(store, {"from": [agent], "limit": 40}))
    timed("search: a word, from one agent", lambda: view.view_search(store, {"text": "the", "from": [agent], "limit": 100}))
    timed("search: mentions of one agent", lambda: view.view_search(store, {"mentions": agent, "limit": 100}))
    timed("activity: 300 buckets", lambda: view.view_activity(store, {"buckets": 300}))
    r = view.view_activity(store, {"buckets": 10})
    mid = (r["start"] + r["end"]) / 2
    timed("activity: a window of a tenth of the run", lambda: view.view_activity(store, {"buckets": 1, "from": mid, "to": mid + (r["end"] - r["start"]) / 10}))
    timed("activity: the whole run as a window", lambda: view.view_activity(store, {"buckets": 1, "from": r["start"], "to": r["end"]}))
    timed("as one agent, with what it would read", lambda: view.view_as(store, {"user": agent, "unread": True, "limit": 200}))
    timed("what an agent knew when it sent a message", lambda: view.view_knew(store, {"id": message_id}))
    span = r["end"] - r["start"]
    for share in (0.01, 0.5, 0.99):
        timed(f"replay: every chat at {share:.0%} of the run", lambda: view.view_at(store, {"at": r["start"] + share * span}))
    timed("replay: one agent's chats at the middle", lambda: view.view_at(store, {"at": mid, "as": agent, "mine": True}))
    timed("replay: the next 2,000 messages", lambda: view.view_replay(store, {"after": mid}))
    timed("replay: the next 20,000 messages", lambda: view.view_replay(store, {"after": mid, "limit": 20000}))
    timed("replay: newest 150 of the busiest chat then", lambda: view.view_messages(store, {"chat": busiest.id, "at": mid, "limit": 150}))
    store.close()


if __name__ == "__main__":
    main()
