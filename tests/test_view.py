"""The viewer's requests (`swarmchat view`): they see every chat, follow everything live, and change nothing."""
import collections
import gzip
import hashlib
import http.client
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

from test_swarmchat import ROOT, Base, Server
from swarmchat import config, view
from swarmchat.client import Client, ClientError
from swarmchat.server import make_server
from swarmchat.store import epoch


def folder_state(root):
    """Every file under root with its size and modification time, to show that nothing changed."""
    state = {}
    for path, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(path, name)
            st = os.stat(full)
            state[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns)
    return state


class ViewBase(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.data, "view.token")) as f:
            self.view = self.server.client(f.read().strip())

    def ids(self, messages):
        return [m["id"] for m in messages]


class Access(ViewBase):
    def test_the_view_token_only_looks_and_agents_cannot_look_at_everything(self):
        self.alice.send(chat="bob", text="private to bob")
        self.assertEqual(len(self.view.view_snapshot()["chats"]), 1)
        self.assertEqual(len(self.server.admin.view_snapshot()["chats"]), 1)  # the admin token also works
        with self.assertRaises(ClientError) as e:
            self.carol.view_snapshot()
        self.assertEqual(e.exception.status, 401)
        for call in (lambda: self.view.send(chat="bob", text="x"), lambda: self.view.user_add(id="eve"),
                     lambda: self.view.chats()):
            with self.assertRaises(ClientError) as e:
                call()
            self.assertEqual(e.exception.status, 401)
        mode = os.stat(os.path.join(self.data, "view.token")).st_mode & 0o777
        if os.name != "nt":
            self.assertEqual(mode, 0o600)

    def test_looking_moves_no_read_position_and_writes_nothing(self):
        sent = self.alice.send(chat="bob", text="hello @bob")["message"]
        self.alice.channel_create(name="team", members="bob,carol")
        reply = self.bob.send(chat="#team", text="hi all", reply_to=None)["message"]
        self.carol.send(chat="#team", text="answer", reply_to=reply["id"])
        self.server.store.flush()
        before = folder_state(self.data)
        v = self.view
        v.view_snapshot()
        v.view_messages(chat=sent["chat"])
        v.view_thread(id=reply["id"])
        v.view_receipts(id=sent["id"])
        v.view_posts()
        v.view_search(text="hello")
        v.view_activity(buckets=10)
        v.view_as(user="bob", unread=True)
        v.view_knew(id=reply["id"])
        v.view_feed(after=0, timeout=0.01)
        self.server.store.flush()
        self.assertEqual(folder_state(self.data), before)
        self.assertEqual(self.bob.chats()["chats"][-1]["unread"], 1)  # still unread for bob


class Snapshot(ViewBase):
    def test_every_user_and_chat_with_counts(self):
        self.alice.send(chat="bob", text="one")
        self.bob.send(chat="alice", text="two")
        self.alice.send(chat="bob,carol", text="three")
        self.alice.channel_create(name="news", members="bob", public=True)
        snap = self.view.view_snapshot()
        self.assertEqual([u["id"] for u in snap["users"]], ["alice", "bob", "carol"])
        kinds = sorted(c["kind"] for c in snap["chats"])
        self.assertEqual(kinds, ["channel", "direct", "group"])
        direct = next(c for c in snap["chats"] if c["kind"] == "direct")
        self.assertEqual((direct["messages"], direct["sent"], direct["last"]["text"]), (2, {"alice": 1, "bob": 1}, "two"))
        self.assertTrue(next(c for c in snap["chats"] if c["kind"] == "channel")["public"])
        self.assertGreater(snap["feed"], 0)

    def test_removed_people_still_have_names(self):
        self.alice.send(chat="carol", text="bye")
        self.server.admin.user_remove(id="carol")
        users = {u["id"]: u for u in self.view.view_snapshot()["users"]}
        self.assertTrue(users["carol"]["removed"])


class Messages(ViewBase):
    def test_windows_of_a_long_chat(self):
        ids = [self.alice.send(chat="bob", text=f"m{i}")["message"]["seq"] for i in range(30)]
        chat = self.alice.chats()["chats"][0]["id"]
        newest = self.view.view_messages(chat=chat, limit=10)
        self.assertEqual([m["text"] for m in newest["messages"] if m["kind"] == "message"], [f"m{i}" for i in range(20, 30)])
        self.assertTrue(newest["last"])
        self.assertFalse(newest["first"])
        older = self.view.view_messages(chat=chat, limit=10, before=ids[20])
        self.assertEqual([m["text"] for m in older["messages"] if m["kind"] == "message"], [f"m{i}" for i in range(10, 20)])
        first = self.view.view_messages(chat=chat, limit=10, before=ids[10])
        self.assertTrue(first["first"])
        self.assertEqual(first["messages"][0]["kind"], "created")  # the chat's start comes with its first page
        around = self.view.view_messages(chat=chat, limit=5, around=ids[15])
        self.assertEqual([m["text"] for m in around["messages"]], ["m13", "m14", "m15", "m16", "m17"])
        after = self.view.view_messages(chat=chat, limit=5, after=ids[27])
        self.assertEqual([m["text"] for m in after["messages"]], ["m28", "m29"])

    def test_replies_carry_what_they_answer_and_read_positions_come_along(self):
        first = self.alice.send(chat="bob,carol", text="question for everyone")["message"]
        filler = [self.alice.send(chat="bob,carol", text=f"filler {i}")["message"] for i in range(5)]
        self.bob.read()
        self.bob.send(chat="alice,carol", text="an answer", reply_to=first["id"])
        r = self.view.view_messages(chat=first["chat"], limit=1)
        reply = r["messages"][-1]
        self.assertEqual(reply["parent"]["text"], "question for everyone")
        self.assertEqual(reply["reply_to_from"], "alice")
        self.assertEqual(r["positions"]["bob"], filler[-1]["seq"])
        self.assertEqual(r["positions"]["carol"], 0)
        whole = self.view.view_messages(chat=first["chat"])
        self.assertEqual(whole["messages"][1]["replies"], 1)

    def test_thread_and_receipts(self):
        root = self.alice.send(chat="bob,carol", text="root")["message"]
        self.bob.read()
        self.carol.read()
        a = self.bob.send(chat="alice,carol", text="a", reply_to=root["id"])["message"]
        self.carol.send(chat="alice,bob", text="b", reply_to=a["id"])
        t = self.view.view_thread(id=a["id"])
        self.assertEqual([(m["text"], m["depth"]) for m in t["messages"]], [("root", 0), ("a", 1), ("b", 2)])
        r = self.view.view_receipts(id=root["id"])
        self.assertEqual(sorted(x["id"] for x in r["read"]), ["bob", "carol"])  # each replied after reading
        self.assertTrue(all(x["at"] for x in r["read"]))
        # carol answered a without reading it: --reply-to needs only its id
        self.assertEqual(self.view.view_receipts(id=a["id"])["unread"], ["alice", "carol"])
        self.assertEqual(r["unread"], [])


class Boards(ViewBase):
    def test_posts_sort_by_votes_comments_and_time(self):
        self.alice.channel_create(name="board", members="bob,carol", public=True)
        quiet = self.alice.send(chat="#board", text="quiet post")["message"]
        busy = self.bob.send(chat="#board", text="busy post\nwith a body")["message"]
        voted = self.carol.send(chat="#board", text="voted post")["message"]
        for user in (self.alice, self.bob):
            user.react(id=voted["id"], emoji="+1")
        r1 = self.alice.send(chat="#board", text="reply", reply_to=busy["id"])["message"]
        self.carol.send(chat="#board", text="reply to reply", reply_to=r1["id"])
        self.alice.send(chat="bob", text="a direct chat is no board")
        new = self.view.view_posts(sort="new")["posts"]
        self.assertEqual([p["text"].split("\n")[0] for p in new], ["voted post", "busy post", "quiet post"])
        top = self.view.view_posts(sort="top")["posts"]
        self.assertEqual([(p["votes"], p["comments"]) for p in top], [(2, 0), (0, 2), (0, 0)])
        active = self.view.view_posts(sort="active")["posts"]
        self.assertEqual(active[0]["id"], busy["id"])
        self.assertEqual(self.view.view_posts(sort="new", limit=1, offset=1)["posts"][0]["id"], busy["id"])
        self.assertEqual(len(self.view.view_posts(chat=quiet["chat"])["posts"]), 3)
        self.assertEqual(self.view.view_posts(**{"as": "carol"})["posts"][0]["chat"], quiet["chat"])


class Search(ViewBase):
    def setUp(self):
        super().setUp()
        self.alice.channel_create(name="ops", members="bob,carol")
        self.m = []
        for user, chat, text in ((self.alice, "#ops", "Deploy the API now"), (self.bob, "#ops", "deploying, @carol watch it"),
                                 (self.carol, "bob", "private: deploy went fine"), (self.bob, "#ops", 'she said "deploy" twice')):
            self.m.append(user.send(chat=chat, text=text)["message"])
            time.sleep(0.005)  # times are kept to the millisecond

    def texts(self, **params):
        return [m["text"] for m in self.view.view_search(**params)["messages"]]

    def test_text_anywhere_newest_first_in_any_case(self):
        self.assertEqual(self.texts(text="DEPLOY"), ['she said "deploy" twice', "private: deploy went fine",
                                                     "deploying, @carol watch it", "Deploy the API now"])
        self.assertEqual(self.texts(text='"deploy"'), ['she said "deploy" twice'])
        self.assertEqual(self.texts(text="alice"), [])  # in every line's sender, but in no text
        self.assertEqual(self.texts(text="2026"), [])  # in every line's time

    def test_filters(self):
        self.assertEqual(self.texts(text="deploy", **{"from": ["bob"]}), ['she said "deploy" twice', "deploying, @carol watch it"])
        self.assertEqual(self.texts(mentions="carol"), ["deploying, @carol watch it"])
        self.assertEqual(self.texts(text="deploy", chats=[self.m[2]["chat"]]), ["private: deploy went fine"])
        self.assertEqual(self.texts(text="deploy", **{"as": "alice"}), ['she said "deploy" twice',
                                                                       "deploying, @carol watch it", "Deploy the API now"])
        self.assertEqual(len(self.texts()), 4)  # no text: the newest messages everywhere
        self.assertEqual(self.texts(after=self.m[3]["at"]), ['she said "deploy" twice'])
        self.assertEqual(self.texts(before=self.m[1]["at"]), ["Deploy the API now"])

    def test_edits_deletes_and_pages(self):
        self.alice.edit(id=self.m[0]["id"], text="Ship the API now")
        self.bob.delete(id=self.m[1]["id"])
        self.assertEqual(self.texts(text="deploy"), ['she said "deploy" twice', "private: deploy went fine"])
        self.assertEqual(self.texts(text="ship"), ["Ship the API now"])
        page = self.view.view_search(text="deploy", limit=1)
        self.assertEqual([m["text"] for m in page["messages"]], ['she said "deploy" twice'])
        rest = self.view.view_search(text="deploy", limit=10, until=page["until"])
        self.assertEqual([m["text"] for m in rest["messages"]], ["private: deploy went fine"])
        self.assertIsNone(rest["until"])

    def test_words_found_in_long_logs_and_unicode(self):
        data = tempfile.mkdtemp(prefix="swarmchat-test-")
        server = Server(data, segment=5)  # a new log file every 5 lines
        try:
            alice = server.user("alice", "Alice")
            server.user("bob", "Bob")
            alice.send(chat="bob", text="Schöne Grüße aus München")
            for i in range(40):
                alice.send(chat="bob", text=f"note {i}" + (" needle" if i % 10 == 3 else ""))
            with open(os.path.join(data, "view.token")) as f:
                self.view = server.client(f.read().strip())
            self.check_words()
        finally:
            server.stop()
            shutil.rmtree(data)

    def check_words(self):
        self.assertEqual(self.texts(text="NEEDLE"), [f"note {i} needle" for i in (33, 23, 13, 3)])
        self.assertEqual(self.texts(text="NEEDLE", limit=2), ["note 33 needle", "note 23 needle"])
        self.assertEqual(self.texts(text="GRÜSSE"), [])
        self.assertEqual(self.texts(text="GRÜẞE".lower()), ["Schöne Grüße aus München"])
        self.assertEqual(self.texts(text="MÜNCHEN"), ["Schöne Grüße aus München"])


class Live(ViewBase):
    def test_the_feed_carries_everything_in_order(self):
        start = self.view.view_snapshot()["feed"]
        got = []

        def follow():  # as the page does: ask again until the message has come
            position = start
            while not any(e["kind"] == "message" for e in got):
                r = self.view.view_feed(after=position, timeout=10)
                got.extend(r["events"])
                position = r["feed"]
            got.append(position)

        t = threading.Thread(target=follow)
        t.start()
        time.sleep(0.2)
        sent = self.alice.send(chat="bob", text="live one")["message"]
        t.join(5)
        position = got.pop()
        self.assertEqual([e["kind"] for e in got], ["chat", "message"])  # the direct chat is made, then written to
        self.assertEqual(got[0]["event"]["kind"], "created")
        self.assertEqual((got[1]["message"]["text"], got[1]["count"]), ("live one", 1))
        self.bob.read()
        self.alice.react(id=sent["id"], emoji="+1")
        self.alice.edit(id=sent["id"], text="live one, edited")
        self.server.admin.user_add(id="dora", name="Dora")
        self.alice.channel_create(name="new", members="dora")
        r = self.view.view_feed(after=position, timeout=0.01)
        kinds = [e["kind"] for e in r["events"]]
        self.assertEqual(kinds[:4], ["read", "change", "change", "user"])
        self.assertIn("chat", kinds)
        self.assertEqual(r["events"][2]["message"]["text"], "live one, edited")
        self.assertEqual(r["events"][2]["message"]["reactions"], {"+1": ["alice"]})
        self.assertEqual(r["events"][0]["user"], "bob")
        self.assertEqual(self.view.view_feed(after=r["feed"], timeout=0.01)["events"], [])

    def test_a_restarted_server_tells_the_viewer_to_start_again(self):
        snap = self.view.view_snapshot()
        self.alice.send(chat="bob", text="before")
        self.assertFalse(self.view.view_feed(after=snap["feed"], feed_id=snap["server"]["feed_id"], timeout=0.01).get("reset"))
        self.restart()
        with open(os.path.join(self.data, "view.token")) as f:
            view = self.server.client(f.read().strip())
        self.assertTrue(view.view_feed(after=1, feed_id=snap["server"]["feed_id"], timeout=0.01)["reset"])
        self.assertTrue(view.view_feed(after=snap["feed"] + 5, timeout=0.01)["reset"])  # ahead of the new count

    def test_a_viewer_too_far_behind_starts_again(self):
        self.server.store.feed.entries = __import__("collections").deque(maxlen=3)
        for i in range(5):
            self.alice.send(chat="bob", text=str(i))
        self.assertTrue(self.view.view_feed(after=0, timeout=0.01)["reset"])
        latest = self.server.store.feed.count
        self.assertEqual(len(self.view.view_feed(after=latest - 3, timeout=0.01)["events"]), 3)


class Perspective(ViewBase):
    def test_as_an_agent_and_what_it_knew(self):
        hello = self.alice.send(chat="bob", text="hello bob")["message"]
        self.alice.send(chat="bob,carol", text="group news")
        self.carol.send(chat="bob", text="from carol, unread")
        self.bob.read(chat="alice")  # bob reads alice's direct chat only
        answer = self.bob.send(chat="alice", text="hi alice", reply_to=hello["id"])["message"]
        seen = self.view.view_as(user="bob", unread=True)
        unread = {c["title"]: c["unread"] for c in seen["chats"]}
        self.assertEqual(unread, {"Alice Ames": 0, "Alice Ames, Carol Cruz": 1, "Carol Cruz": 1})
        self.assertEqual([m["text"] for m in seen["unread"]], ["group news", "from carol, unread"])
        self.assertEqual(seen["positions"][hello["chat"]], hello["seq"])
        knew = self.view.view_knew(id=answer["id"])
        rows = {r["chat"]: r for r in knew["chats"]}
        self.assertEqual(knew["chats"][0]["chat"], hello["chat"])
        self.assertEqual(rows[hello["chat"]]["unseen"], 0)
        self.assertEqual(sorted(r["unseen"] for r in knew["chats"]), [0, 1, 1])
        self.assertEqual(self.bob.chats(unread=True)["chats"][0]["unread"], 1)  # still unread after all that

    def test_what_it_knew_counts_only_chats_it_was_in(self):
        self.alice.channel_create(name="later", members="carol")
        self.alice.send(chat="#later", text="before bob joined")
        first = self.bob.send(chat="alice", text="first")["message"]
        time.sleep(0.01)  # times are kept to the millisecond
        self.alice.channel_add(chat="#later", users="bob")
        knew = self.view.view_knew(id=first["id"])
        self.assertEqual([r["chat"] for r in knew["chats"]], [first["chat"]])


class Activity(ViewBase):
    def test_counts_over_time_and_in_a_window(self):
        for i in range(12):
            (self.alice if i % 3 else self.bob).send(chat="alice" if i % 3 == 0 else "bob", text=str(i))
            time.sleep(0.005)
        r = self.view.view_activity(buckets=4)
        self.assertEqual(sum(r["counts"]), 12)
        whole = self.view.view_activity(buckets=4, **{"from": r["start"], "to": r["end"]})["window"]
        chat = next(iter(whole))
        self.assertEqual(whole[chat], {"messages": 12, "sent": {"alice": 8, "bob": 4}})
        part = self.view.view_activity(buckets=4, **{"from": r["start"] + 0.002, "to": r["end"]})["window"]
        self.assertEqual(part[chat]["messages"], 11)


class Replay(ViewBase):
    def sent(self, who, chat, text):
        m = who.send(chat=chat, text=text)["message"]
        time.sleep(0.003)  # times are kept to the millisecond
        return m

    def test_every_chat_as_it_stood_at_a_moment(self):
        one = self.sent(self.alice, "bob", "one")
        self.sent(self.bob, "alice", "two")
        self.alice.channel_create(name="news", members="bob")
        time.sleep(0.003)
        news = self.sent(self.alice, "#news", "news")
        direct, channel = one["chat"], news["chat"]
        then = self.view.view_at(at=epoch(one["at"]))["chats"]
        self.assertEqual((then[direct]["messages"], then[direct]["sent"]), (1, {"alice": 1}))
        self.assertEqual(then[direct]["last"], {"seq": one["seq"], "from": "alice", "at": one["at"]})
        self.assertEqual((then[channel]["born"], then[channel]["messages"]), (False, 0))
        self.assertNotIn("last", then[channel])
        now = self.view.view_at(at=epoch(news["at"]))["chats"]
        self.assertEqual((now[direct]["messages"], now[direct]["sent"], now[direct]["recent"]), (2, {"alice": 1, "bob": 1}, 2))
        self.assertEqual((now[channel]["born"], now[channel]["messages"]), (True, 1))

    def test_who_wrote_how_many_by_any_moment(self):
        msgs = [self.sent(self.alice, "bob", str(i)) if i % 3 else self.sent(self.bob, "alice", str(i)) for i in range(10)]
        for k, m in enumerate(msgs, 1):  # early moments count from the start, late ones back from the end
            state = self.view.view_at(at=epoch(m["at"]))["chats"][m["chat"]]
            self.assertEqual((state["messages"], state["sent"]), (k, dict(collections.Counter(x["from"] for x in msgs[:k]))))

    def test_what_an_agent_had_read_then(self):
        first = self.sent(self.alice, "bob", "hello @bob")
        second = self.sent(self.alice, "bob", "are you there")
        self.bob.read(chat="alice")
        time.sleep(0.003)
        third = self.sent(self.alice, "bob", "thanks")
        other = self.sent(self.carol, "alice", "not for bob")
        then = self.view.view_at(at=epoch(second["at"]), **{"as": "bob", "mine": True})["chats"]
        self.assertEqual(list(then), [first["chat"]])  # only bob's chats
        self.assertEqual(then[first["chat"]]["as"], {"upto": 0, "unread": 2, "mentioned": 1})
        later = self.view.view_at(at=epoch(third["at"]), **{"as": "bob"})["chats"]
        self.assertEqual(later[first["chat"]]["as"], {"upto": second["seq"], "unread": 1, "mentioned": 0})
        self.assertNotIn("as", later[other["chat"]])

    def test_what_was_written_after_a_moment_page_by_page(self):
        pairs = [(self.alice, "bob"), (self.bob, "carol"), (self.carol, "alice,bob")]
        msgs = [self.sent(*pairs[i % 3], str(i)) for i in range(7)]
        want = [[epoch(m["at"]), m["chat"], m["seq"], m["from"]] for m in msgs]
        got, after, pages = [], 0, 0
        while True:
            r = self.view.view_replay(after=after, limit=3)
            got += r["messages"]
            pages += 1
            if not r["more"]:
                break
            after = r["messages"][-1][:3]
        self.assertEqual((got, pages), (want, 3))
        self.assertEqual(self.view.view_replay(after=epoch(msgs[1]["at"]))["messages"], want[2:])
        self.assertEqual(self.view.view_replay(after=0, **{"as": "carol"})["messages"], [w for w in want if w[1] != msgs[0]["chat"]])

    def test_a_chat_as_it_was_at_a_moment(self):
        msgs = [self.sent(self.alice, "bob", f"m{i}") for i in range(12)]
        r = self.view.view_messages(chat=msgs[0]["chat"], at=epoch(msgs[7]["at"]), limit=5)
        self.assertEqual([m["text"] for m in r["messages"] if m["kind"] == "message"], ["m3", "m4", "m5", "m6", "m7"])
        before = self.view.view_messages(chat=msgs[0]["chat"], at=epoch(msgs[0]["at"]) - 1)
        self.assertEqual([m for m in before["messages"] if m["kind"] == "message"], [])


def png(w, h) -> bytes:
    """A real PNG of w by h grey pixels."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = (b"\0" + b"\x80" * w) * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg(w, h, orientation=None, frame=b"\xc0") -> bytes:
    """The start of a JPEG: an optional EXIF segment with an orientation, then a frame header."""
    out = b"\xff\xd8"
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\1\1\0\0\1\0\1\0\0"
    if orientation:
        tiff = b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", 1) + struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0)
        payload = b"Exif\0\0" + tiff + struct.pack("<I", 0)
        out += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    out += b"\xff" + frame + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\1\x11\0"
    return out + b"\xff\xd9"


class Pictures(ViewBase):
    """The page draws attached pictures, so each one comes with its type and size, read from its own bytes."""

    def test_pictures_are_known_by_their_bytes_whatever_their_name(self):
        tmp = tempfile.mkdtemp(prefix="swarmchat-pictures-")
        self.addCleanup(shutil.rmtree, tmp)
        riff = lambda chunk, body: b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + chunk + struct.pack("<I", len(body)) + body
        files = {
            "chart.png": png(30, 20),
            "anim.gif": b"GIF89a" + struct.pack("<HH", 31, 21) + bytes(20),
            "lossy.webp": riff(b"VP8 ", b"\0\0\0\x9d\x01\x2a" + struct.pack("<HH", 40, 30) + bytes(10)),
            "lossless.webp": riff(b"VP8L", b"\x2f" + struct.pack("<I", (41 - 1) | (31 - 1) << 14) + bytes(10)),
            "extended.webp": riff(b"VP8X", bytes(4) + (1999).to_bytes(3, "little") + (999).to_bytes(3, "little") + bytes(10)),
            "old.bmp": b"BM" + bytes(12) + struct.pack("<Iii", 40, 50, -60) + bytes(30),
            "older.bmp": b"BM" + bytes(12) + struct.pack("<IHH", 12, 8, 9) + bytes(30),
            "BMW notes.txt": b"BMW notes: the M3 needs new tyres, the X5 a service, nothing else.",
            "plain.jpg": jpeg(200, 100, frame=b"\xc2"),
            "turned.jpg": jpeg(200, 100, orientation=6),
            "upright.jpg": jpeg(200, 100, orientation=1),
            "renamed": png(5, 7),  # no extension: the bytes still say PNG
            "fake.png": b"just some text, not a picture",
            "cut.jpg": jpeg(200, 100)[:12],
            "huge.png": png(1, 1)[:16] + struct.pack(">II", 20000, 20000) + bytes(20),
            "large.png": png(1, 1)[:16] + struct.pack(">II", 6000, 5000) + bytes(20),  # 30 megapixels
        }
        paths = []
        for name, data in files.items():
            paths.append(os.path.join(tmp, name))
            with open(paths[-1], "wb") as f:
                f.write(data)
        os.makedirs(os.path.join(tmp, "shots"))
        with open(os.path.join(tmp, "shots", "a.png"), "wb") as f:
            f.write(png(3, 3))
        start = self.server.store.feed.count
        sent = self.alice.send(chat="bob", text="pictures", attach=[*paths, os.path.join(tmp, "shots")])["message"]

        def shapes(m):
            return {a["name"]: (a["image"]["type"], a["image"]["width"], a["image"]["height"]) if "image" in a else None
                    for a in m["attachments"]}
        expected = {
            "chart.png": ("image/png", 30, 20), "anim.gif": ("image/gif", 31, 21), "lossy.webp": ("image/webp", 40, 30),
            "lossless.webp": ("image/webp", 41, 31), "extended.webp": ("image/webp", 2000, 1000),
            "old.bmp": ("image/bmp", 50, 60), "older.bmp": ("image/bmp", 8, 9), "BMW notes.txt": None, "plain.jpg": ("image/jpeg", 200, 100),
            "turned.jpg": ("image/jpeg", 100, 200),  # turned a quarter, as a browser draws it
            "upright.jpg": ("image/jpeg", 200, 100), "renamed": ("image/png", 5, 7),
            "fake.png": None, "cut.jpg": None, "huge.png": None, "large.png": None, "shots": None,
        }
        window = self.view.view_messages(chat=sent["chat"])["messages"]
        self.assertEqual(shapes(next(m for m in window if m["kind"] == "message")), expected)
        event = next(e for e in self.view.view_feed(after=start, timeout=0.01)["events"] if e["kind"] == "message")
        self.assertEqual(shapes(event["message"]), expected)
        self.assertEqual(shapes(self.view.view_search(text="pictures")["messages"][0]), expected)
        self.assertEqual(shapes(self.view.view_as(user="bob", unread=True)["unread"][0]), expected)
        self.assertNotIn("image", self.bob.read(chat="alice", peek=True)["messages"][0]["attachments"][0])  # the viewer's only
        self.alice.delete(id=sent["id"])
        self.assertNotIn("attachments", self.view.view_messages(chat=sent["chat"])["messages"][-1])


class Texts(ViewBase):
    """The page shows a text file's first lines under its message, and the whole of it in a viewer."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp(prefix="swarmchat-texts-")
        self.addCleanup(shutil.rmtree, tmp)
        files = {
            "notes.md": b"# Title\n\nSome *text*.\n",
            "run.log": "".join(f"2026-10-13T14:00:00Z INFO line {i}\n" for i in range(2000)).encode(),
            "windows.txt": b"one\r\ntwo\r\n",
            "marked.py": "\ufeffprint('hi')\n".encode(),
            "wide.txt": b"x" * 1000 + b"\n",
            "cut.txt": ("a" + "\u00e9" * 20000).encode(),  # the bytes read end inside a character
            "latin1.txt": "caf\u00e9 cr\u00e8me".encode("latin-1"),  # not UTF-8
            "zeros.txt": b"text\0with a NUL",
            "empty.txt": b"",
            "chart.png": png(3, 2),
        }
        paths = []
        for name, data in files.items():
            paths.append(os.path.join(tmp, name))
            with open(paths[-1], "wb") as f:
                f.write(data)
        os.makedirs(os.path.join(tmp, "folder"))
        with open(os.path.join(tmp, "folder", "a.txt"), "w") as f:
            f.write("inside\n")
        self.sent = self.alice.send(chat="bob", text="files", attach=[*paths, os.path.join(tmp, "folder")])["message"]
        self.ids = {a["name"]: a["id"] for a in self.sent["attachments"]}

    def test_text_is_known_by_its_bytes_and_comes_with_its_first_lines(self):
        m = next(m for m in self.view.view_messages(chat=self.sent["chat"])["messages"] if m["kind"] == "message")
        texts = {a["name"]: a.get("text") for a in m["attachments"]}
        self.assertEqual(texts["notes.md"], {"preview": "# Title\n\nSome *text*.", "more": False, "lines": 3})
        self.assertEqual(texts["run.log"]["preview"].split("\n"), [f"2026-10-13T14:00:00Z INFO line {i}" for i in range(8)])
        self.assertTrue(texts["run.log"]["more"])
        self.assertNotIn("lines", texts["run.log"])  # only its start was read
        self.assertEqual(texts["windows.txt"], {"preview": "one\ntwo", "more": False, "lines": 2})
        self.assertEqual(texts["marked.py"], {"preview": "print('hi')", "more": False, "lines": 1})
        self.assertEqual(texts["wide.txt"], {"preview": "x" * 200, "more": True, "lines": 1})
        self.assertTrue(texts["cut.txt"]["preview"].startswith("a\u00e9\u00e9") and texts["cut.txt"]["more"])
        for name in ("latin1.txt", "zeros.txt", "empty.txt", "chart.png", "folder"):
            self.assertIsNone(texts[name], name)
        self.assertIn("image", next(a for a in m["attachments"] if a["name"] == "chart.png"))
        self.assertEqual(self.view.view_search(text="files")["messages"][0]["attachments"][0]["text"], texts["notes.md"])
        self.assertNotIn("text", self.bob.read(chat="alice", peek=True)["messages"][0]["attachments"][0])  # the viewer's only

    def test_the_viewer_gets_the_whole_text_or_its_start_cut_at_a_line(self):
        r = self.view.view_text(id=self.ids["notes.md"])
        self.assertEqual(r, {"text": "# Title\n\nSome *text*.\n", "size": 22, "shown": 22, "cut": False})
        self.assertEqual(self.view.view_text(id=self.ids["marked.py"])["text"], "print('hi')\n")
        self.assertEqual(self.view.view_text(id=self.ids["latin1.txt"])["text"], "caf\ufffd cr\ufffdme")
        self.assertEqual(self.view.view_text(id=self.ids["zeros.txt"])["text"], "text\ufffdwith a NUL")
        with mock.patch.object(view, "TEXT_BYTES", 100):
            r = self.view.view_text(id=self.ids["run.log"])
        self.assertTrue(r["cut"])
        self.assertEqual(r["text"], "".join(f"2026-10-13T14:00:00Z INFO line {i}\n" for i in range(3)))
        self.assertEqual(r["shown"], len(r["text"]))
        with self.assertRaises(ClientError) as e:
            self.view.view_text(id=self.ids["folder"])
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ClientError) as e:
            self.view.view_text(id="nope")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ClientError) as e:
            self.bob.view_text(id=self.ids["notes.md"])  # an agent cannot look through the viewer
        self.assertEqual(e.exception.status, 401)


class Page(ViewBase):
    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.httpd.server_address[1], timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            r = conn.getresponse()
            return r, r.read()
        finally:
            conn.close()

    def test_the_page_is_one_file_with_a_strict_policy(self):
        r, body = self.get("/view")
        self.assertEqual(r.status, 200)
        html = body.decode()
        self.assertNotIn("<!--JS-->", html)
        policy = r.getheader("Content-Security-Policy")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        digest = __import__("base64").b64encode(hashlib.sha256(script.encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        packed, data = self.get("/view", {"Accept-Encoding": "gzip"})
        self.assertEqual(packed.getheader("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(data), body)
        again, _ = self.get("/view", {"If-None-Match": r.getheader("ETag")})
        self.assertEqual(again.status, 304)
        home, _ = self.get("/")
        self.assertEqual((home.status, home.getheader("Location")), (302, "/view"))

    def test_a_missing_page_is_reported_plainly(self):
        with mock.patch.object(view, "PAGE_DIR", os.path.join(self.data, "nowhere")), mock.patch.dict(view._page, clear=True):
            r, body = self.get("/view")
        self.assertEqual(r.status, 500)
        self.assertIn("missing from this install", json.loads(body)["error"])

    def test_large_replies_are_compressed_for_browsers_only(self):
        for i in range(30):
            self.alice.send(chat="bob", text=f"message {i} " * 10)
        body = json.dumps({"chat": self.alice.chats()["chats"][0]["id"]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.httpd.server_address[1], timeout=10)
        conn.request("POST", "/v1/view_messages", body, {"Authorization": f"Bearer {self.view.token}",
                                                         "Accept-Encoding": "gzip, br"})
        r = conn.getresponse()
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        self.assertEqual(len(json.loads(gzip.decompress(r.read()))["messages"]), 31)
        conn.close()
        self.assertEqual(len(self.view.view_messages(chat=json.loads(body)["chat"])["messages"]), 31)


class ReadOnly(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="swarmchat-view-")
        server = Server(self.data)
        alice = server.user("alice", "Alice")
        server.user("bob", "Bob")
        alice.send(chat="bob", text="kept on disk")
        with open(os.path.join(self.data, "chats", alice.chats()["chats"][0]["id"], "log", "000001.jsonl"), "ab") as f:
            f.write(b'{"seq": 99, "half a line')  # a crash in the middle of a write
        server.stop()

    def tearDown(self):
        shutil.rmtree(self.data)

    def test_a_folder_is_shown_without_a_single_change(self):
        before = folder_state(self.data)
        httpd, store = make_server(config.load(data=self.data, port=0), read_only=True)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            view = Client(httpd.url, store.view_token)
            snap = view.view_snapshot()
            self.assertTrue(snap["server"]["read_only"])
            chat = snap["chats"][0]["id"]
            self.assertEqual(view.view_messages(chat=chat)["messages"][-1]["text"], "kept on disk")
            with open(os.path.join(self.data, "admin.token")) as f:
                admin = Client(httpd.url, f.read().strip())
            with self.assertRaises(ClientError) as e:
                admin.user_add(id="eve")
            self.assertEqual(e.exception.status, 403)
            with self.assertRaises(ClientError):
                admin.view_snapshot()  # only this process's own token opens it
            view.close()
            admin.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            store.close()
        self.assertEqual(folder_state(self.data), before)

    def test_the_command_uses_a_running_server_or_else_the_folder(self):
        env = {**os.environ, "PYTHONPATH": ROOT, "SWARMCHAT_CONFIG_DIR": self.data + "/config"}
        proc = subprocess.Popen([sys.executable, "-m", "swarmchat", "view", "--data", self.data, "--no-open"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            proc.stdout.readline()
            url = proc.stdout.readline().split("Viewer: ", 1)[1].strip()
            base, token = url.split("/view#token=")
            view = Client(base, token)
            self.assertEqual(view.view_snapshot()["server"]["read_only"], True)
            view.close()
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()
        server = Server(self.data)
        try:
            r = subprocess.run([sys.executable, "-m", "swarmchat", "view", "--data", self.data, "--no-open"], env=env,
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(self.data, "view.token")) as f:
                self.assertEqual(r.stdout.strip(), f"Viewer: {server.url}/view#token={f.read().strip()}")
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
