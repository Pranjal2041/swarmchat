"""Makes a large run for the viewer: agents with roles and teams, public and private channels, group chats, direct
chats, threads, reactions, edits, deletions, read receipts and attached files (pictures, logs, tables, folders), written
straight to a data folder in the server's own format. Then `swarmchat view --data DIR` (or `swarmchat serve`) opens it.

    python3 bench/view_data.py DIR [--agents 1000] [--messages 1000000] [--hours 6] [--seed 1] [--files 0.03]

Tokens for every agent go to DIR.tokens.json, next to the folder, and the files to attach go to DIR.files/, both for
bench/view_live.py.
"""
import argparse
import hashlib
import json
import mimetypes
import os
import random
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone

import demo_files

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from swarmchat.files import pack  # noqa: E402

FIRST = ("Ava Ben Cara Dev Eli Fay Gus Hana Ivan Jade Kai Lena Milo Nia Omar Pia Quinn Ravi Sara Theo Uma Vik Wren Xan "
         "Yara Zed Aria Bo Cole Dina Ezra Faye Gil Hugo Iris Jon Kira Leo Maya Noor Otto Pax Rosa Sol Tara Ugo Vera "
         "Will Yuki Zoe").split()
LAST = ("Patel Kim Okafor Silva Novak Haddad Larsen Moreau Tanaka Rossi Nguyen Cohen Ibrahim Walsh Duarte Berg "
        "Kowalski Mensah Ortiz Schmidt Varga Yilmaz Lindqvist Park Abara Costa Dubois Frey Grant Holm").split()
ROLES = ["engineer", "reviewer", "planner", "researcher", "tester", "writer", "manager", "ops"]
TEAMS = ["payments", "search", "infra", "mobile", "growth", "data", "security", "support", "billing", "docs", "ml", "web"]
TOPICS = ("deploy rollout review the migration plan latency budget flaky test failing on main the cache hit rate "
          "the schema change a regression in search the new index the API contract our error budget the on-call "
          "rotation the release notes customer ticket the dashboard memory leak the retry logic rate limits the "
          "feature flag the design doc the benchmark the canary the backfill the queue depth").split(" ")
VERBS = ["Can someone look at", "I pushed a fix for", "Blocked on", "Heads up about", "Question on", "Done with",
         "Reviewing", "Who owns", "Draft ready for", "Rolling back", "Need a second pair of eyes on", "Closing out",
         "Status update on", "Proposal for", "Still investigating"]
TAILS = ["", " before the 3pm cutoff", ", see the thread", ". Numbers attached.", " — it broke twice today",
         " (low priority)", ". Will report back in an hour.", ", ETA tomorrow", " on staging first", ". Thoughts?"]
REPLIES = ["Looks good to me.", "+1, ship it.", "I can take this.", "Not yet, the tests are red.", "Which branch?",
           "Done, merged.", "Can you share the logs?", "I see the same thing on my side.", "Let's discuss in standup.",
           "Fixed in the latest commit.", "That number looks off to me.", "Agreed. Moving on.", "Checking now.",
           "Here is what I found:\n```\nerror: timeout after 30s\n  at fetch (client.py:212)\n```",
           "Rerun passed. Flaky, not real.", "Blocked until infra answers.", "Thanks, that fixed it."]
EMOJI = ["+1", "+1", "+1", "eyes", "tada", "heart", "rocket", "check", "-1"]
WITH_PICTURES = ["Here is the chart.", "Screenshot from staging:", "Before and after the fix.", "This is what I see.",
                 "Numbers from the last run.", "Look at the spike around 14:00.", "Profile of the slow path.", "Updated diagram."]
WITH_FILES = ["Logs attached.", "Full output here.", "Results from the nightly run.", "The patch, for review.",
              "Notes from the incident.", "Config I used.", "Everything from the eval."]


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("dir")
    p.add_argument("--agents", type=int, default=1000)
    p.add_argument("--messages", type=int, default=1_000_000)
    p.add_argument("--hours", type=float, default=6)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--files", type=float, default=0.03, help="share of messages in channels and groups with files")
    a = p.parse_args()
    random.seed(a.seed)
    started = time.time()
    if os.path.exists(a.dir):
        shutil.rmtree(a.dir)
    for sub in ("users", "chats", "state", "retired", "blobs", "uploads"):
        os.makedirs(os.path.join(a.dir, sub))
    t0 = time.time() - a.hours * 3600
    with open(os.path.join(a.dir, "server.json"), "w") as f:
        json.dump({"format": 1, "created_at": iso(t0), "segment": 10_000}, f)

    # files to attach: each stored once, under the hash of its bytes, as the server stores them
    files_dir = a.dir.rstrip("/") + ".files"
    if os.path.exists(files_dir):
        shutil.rmtree(files_dir)
    pictures, documents = [], []
    for path, picture in demo_files.write(files_dir, a.seed):
        name = os.path.basename(path)
        if os.path.isdir(path):
            zipped = pack(path)
            with open(zipped, "rb") as f:
                data = f.read()
            os.unlink(zipped)
            kind = {"name": name, "size": len(data), "type": "application/zip", "folder": True}
        else:
            with open(path, "rb") as f:
                data = f.read()
            kind = {"name": name, "size": len(data), "type": mimetypes.guess_type(name)[0] or "application/octet-stream"}
        kind["sha256"] = sha = hashlib.sha256(data).hexdigest()
        os.makedirs(os.path.join(a.dir, "blobs", sha[:2]), exist_ok=True)
        with open(os.path.join(a.dir, "blobs", sha[:2], sha), "wb") as f:
            f.write(data)
        (pictures if picture else documents).append(kind)

    # people
    users, tokens, taken = [], {}, set()
    for i in range(a.agents):
        first, last = random.choice(FIRST), random.choice(LAST)
        uid = f"{first}.{last}".lower()
        if uid in taken:
            uid = f"{uid}{i}"
        taken.add(uid)
        users.append(uid)
        token = secrets.token_hex(24)
        tokens[uid] = token
        profile = {"id": uid, "name": f"{first} {last}", "fields": {"role": random.choice(ROLES), "team": random.choice(TEAMS)},
                   "status": random.choice(["", "", "", "heads down", "reviewing PRs", "on call", "in a meeting"]),
                   "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "created_at": iso(t0)}
        with open(os.path.join(a.dir, "users", f"{uid}.json"), "w") as f:
            json.dump(profile, f)
    team_of = {}
    for uid in users:
        with open(os.path.join(a.dir, "users", f"{uid}.json")) as f:
            team_of[uid] = json.load(f)["fields"]["team"]

    # chats
    chats = []

    def chat(members, name=None, public=False):
        c = {"id": secrets.token_hex(3), "name": name, "members": sorted(set(members)), "created_by": members[0],
             "created_at": iso(t0 + random.random() * 600), "public": public, "lines": [], "messages": [], "pos": {}}
        chats.append(c)
        return c

    scale = max(1, a.agents // 100)
    for team in TEAMS:
        members = [u for u in users if team_of[u] == team]
        if members:
            chat(members, f"team-{team}", public=False)
    for name in ["general", "announcements", "random", "incidents", "deploys", "help", "design-review", "research",
                 "ideas", "hiring", "oncall", "releases", "postmortems", "benchmarks", "show-and-tell"]:
        chat(random.sample(users, min(len(users), random.randint(20, 60) * scale)), name, public=True)
    for i in range(10 * scale):
        chat(random.sample(users, min(len(users), random.randint(4, 25))), f"proj-{random.choice(TOPICS).split()[-1]}-{i}",
             public=random.random() < 0.4)
    for _ in range(40 * scale):
        chat(random.sample(users, random.randint(3, 6)))
    pairs = set()
    while len(pairs) < 300 * scale:
        pairs.add(tuple(sorted(random.sample(users, 2))))
    for x, y in pairs:
        chat([x, y])
    names = {c["id"] for c in chats}
    assert len(names) == len(chats)
    for c in chats:
        c["lines"].append({"kind": "created", "from": c["created_by"], "at": c["created_at"], "members": c["members"],
                           "name": c["name"], **({"public": True} if c["public"] else {})})
    # how busy each chat is: a few very busy, most quiet
    weights = [random.paretovariate(1.2) * (3 if c["name"] else 1) for c in chats]

    # messages, in time order, with bursts
    t = t0 + 700
    span = a.hours * 3600 - 800
    step = span / a.messages
    chosen = random.choices(range(len(chats)), weights=weights, k=a.messages)
    for n, ci in enumerate(chosen):
        t += random.expovariate(1 / step) * (0.3 if (n // 5000) % 7 == 3 else 1.1)
        c = chats[ci]
        at = iso(t)
        sender = random.choice(c["members"])
        reply = None
        if c["messages"] and random.random() < (0.45 if c["name"] else 0.2):
            reply = random.choice(c["messages"][-30:])
        if reply:
            text = random.choice(REPLIES)
        else:
            text = f"{random.choice(VERBS)} {random.choice(TOPICS)}{random.choice(TAILS)}"
            if random.random() < 0.05:
                text += f"\nDetails: https://ci.example.com/run/{random.randint(1000, 99999)}"
        mentions = []
        if len(c["members"]) > 2 and random.random() < 0.15:
            who = random.choice([u for u in c["members"] if u != sender])
            text = f"@{who} {text}"
            mentions = [who]
        e = {"kind": "message", "from": sender, "text": text}
        if random.random() < a.files * (1 if len(c["members"]) > 2 else 0.3):
            if random.random() < 0.75:
                chosen_files = random.sample(pictures, 1 if random.random() < 0.8 else random.randint(2, 4))
                say = WITH_PICTURES
            else:
                chosen_files = [random.choice(documents)]
                say = WITH_FILES
            e["attachments"] = [{"id": "a" + secrets.token_hex(8), **{k: v for k, v in f.items() if k != "sha256"},
                                 "sha256": f["sha256"]} for f in chosen_files]
            if not reply and not mentions:
                e["text"] = "" if random.random() < 0.3 else random.choice(say)
        if reply:
            e["reply_to"] = f"{c['id']}:{reply}"
        if mentions:
            e["mentions"] = mentions
        e["at"] = at
        c["lines"].append(e)
        seq = len(c["lines"])
        c["messages"].append(seq)
        r = random.random()
        if r < 0.08 and c["name"]:
            for voter in random.sample(c["members"], min(len(c["members"]), random.randint(1, 6))):
                c["lines"].append({"kind": "react", "from": voter, "at": at, "target": seq, "emoji": random.choice(EMOJI), "remove": False})
        elif r < 0.085:
            c["lines"].append({"kind": "edit", "from": sender, "at": at, "target": seq, "text": text + " (edited: fixed a typo)", "mentions": mentions})
        elif r < 0.087:
            c["lines"].append({"kind": "delete", "from": sender, "at": at, "target": seq})
        # someone catches up: a read line, as the server writes when a read position moves
        if random.random() < 0.5:
            reader = random.choice(c["members"])
            if reader != sender or len(c["members"]) == 1:
                c["lines"].append({"kind": "read", "from": reader, "at": at, "upto": seq})
                c["pos"][reader] = seq

    # write every chat, and every user's read positions
    state = {u: {"read": {}, "notify": {}} for u in users}
    lines = 0
    for c in chats:
        d = os.path.join(a.dir, "chats", c["id"])
        os.makedirs(os.path.join(d, "log"))
        meta = {"id": c["id"], "name": c["name"], "members": c["members"], "created_by": c["created_by"], "created_at": c["created_at"],
                **({"public": True} if c["public"] else {})}
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(meta, f)
        out = None
        for i, e in enumerate(c["lines"]):
            if i % 10_000 == 0:
                if out:
                    out.close()
                out = open(os.path.join(d, "log", f"{i // 10_000 + 1:06d}.jsonl"), "w", encoding="utf-8")
            out.write(json.dumps({"seq": i + 1, **e}, ensure_ascii=False, separators=(",", ":")) + "\n")
        out.close()
        lines += len(c["lines"])
        for u, seq in c["pos"].items():
            state[u]["read"][c["id"]] = seq
    for u, s in state.items():
        with open(os.path.join(a.dir, "state", f"{u}.json"), "w") as f:
            json.dump(s, f)
    tokens_path = a.dir.rstrip("/") + ".tokens.json"  # every agent's token: readable only by its owner
    with os.fdopen(os.open(tokens_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(tokens, f)
    size = sum(os.path.getsize(os.path.join(r, n)) for r, _, fs in os.walk(a.dir) for n in fs)
    print(f"{a.agents} agents, {len(chats)} chats ({sum(1 for c in chats if c['name'])} channels, "
          f"{sum(1 for c in chats if not c['name'] and len(c['members']) > 2)} groups, "
          f"{sum(1 for c in chats if not c['name'] and len(c['members']) == 2)} direct), {a.messages:,} messages, "
          f"{lines:,} lines, {size / 1e6:.0f} MB, in {time.time() - started:.1f} s")


if __name__ == "__main__":
    main()
