"""End-to-end tests: a real server on a free port with a temporary data folder, and real clients.

    python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from swarmchat import config  # noqa: E402
from swarmchat.client import Client, ClientError, matcher, sorter  # noqa: E402
from swarmchat.server import make_server  # noqa: E402


class Server:
    def __init__(self, data, **settings):
        self.data = data
        self.httpd, self.store = make_server(config.load(data=data, port=0, **settings))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        with open(os.path.join(data, "admin.token")) as f:
            self.admin = Client(self.url, f.read().strip())
        self.clients = [self.admin]

    def client(self, token):
        self.clients.append(Client(self.url, token))
        return self.clients[-1]

    def user(self, user_id, name):
        return self.client(self.admin.user_add(id=user_id, name=name)["token"])

    def stop(self):
        for client in self.clients:
            client.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="swarmchat-test-")
        self.server = Server(self.data)
        self.alice = self.server.user("alice", "Alice Ames")
        self.bob = self.server.user("bob", "Bob Burns")
        self.carol = self.server.user("carol", "Carol Cruz")

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.data)

    def restart(self):
        tokens = {name: getattr(self, name).token for name in ("alice", "bob", "carol")}
        self.server.stop()
        self.server = Server(self.data)
        for name, token in tokens.items():
            setattr(self, name, self.server.client(token))

    def texts(self, result):
        return [m["text"] for m in result["messages"]]


class Chats(Base):
    def test_a_dm_is_a_chat_of_two_and_is_reused(self):
        first = self.alice.send(chat="bob", text="hi bob")
        second = self.alice.send(chat="Bob Burns", text="are you there")
        self.assertEqual(first["chat"]["id"], second["chat"]["id"])
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 2)
        self.assertEqual(self.texts(self.bob.read()), ["hi bob", "are you there"])
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 0)
        self.assertEqual(self.bob.read()["messages"], [])

    def test_direct_chats_and_channels(self):
        trio = self.alice.send(chat="bob,carol", text="all three")["chat"]
        self.assertEqual(trio["kind"], "direct")
        self.assertEqual(self.carol.send(chat="bob,alice", text="any order")["chat"]["id"], trio["id"])
        self.assertNotEqual(trio["id"], self.alice.send(chat="bob", text="just us")["chat"]["id"])
        channel = self.alice.channel_create(name="Release review", members="bob,carol")["chat"]
        self.assertEqual((channel["kind"], channel["title"]), ("channel", "#Release review"))
        self.carol.send(chat="#release review", text="by name")
        self.carol.send(chat="Release Review", text="without the #")
        self.assertEqual(self.texts(self.alice.read(chat=channel["id"])), ["by name", "without the #"])
        self.assertEqual(self.alice.send(chat="bob,carol", text="still direct")["chat"]["id"], trio["id"])
        self.bob.channel_leave(chat="#release review")
        self.assertEqual([m["id"] for m in self.alice.chat_info(chat=channel["id"])["chat"]["members"]], ["alice", "carol"])
        with self.assertRaises(ClientError) as e:
            self.bob.send(chat="#release review", text="let me in")
        self.assertEqual(e.exception.status, 404)  # a private channel is not there for those outside it

    def test_names_mean_one_thing(self):
        self.alice.channel_create(name="ops")
        for call in (lambda: self.bob.channel_create(name="OPS"),            # channel names are unique
                     lambda: self.bob.channel_create(name="Carol Cruz"),     # and never someone's name
                     lambda: self.alice.channel_add(chat=self.alice.send(chat="bob", text="x")["chat"]["id"],
                                                    users="carol"),          # direct chats keep their members
                     lambda: self.alice.channel_rename(chat="bob", name="pair")):
            with self.assertRaises(ClientError) as e:
                call()
            self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ClientError) as e:
            self.bob.channel_create(name="#ops")
        self.assertEqual(str(e.exception), "The name #ops is taken. Pick another name.")  # no id, for a private one
        with self.assertRaises(ClientError) as e:
            self.alice.channel_create(name="#ops")
        self.assertIn("already a channel #ops", str(e.exception))  # its members learn which one

    def test_people_with_the_same_name(self):
        self.server.user("jsmith", "John Smith")
        self.server.user("jsmith2", "John Smith")
        chat = self.alice.channel_create(name="smiths", members="jsmith,jsmith2,bob")["chat"]["id"]
        self.assertIn("John Smith (jsmith2)", [m["name"] for m in self.alice.chat_info(chat=chat)["chat"]["members"]])
        for text in ("@John Smith please check", "@John please check"):
            with self.assertRaises(ClientError) as e:
                self.alice.send(chat=chat, text=text)
            self.assertIn("@jsmith or @jsmith2", str(e.exception))
        self.assertEqual(self.alice.send(chat=chat, text="@jsmith2 please")["message"]["mentions"], ["jsmith2"])
        self.assertEqual(self.alice.send(chat=chat, text="@Bob and @bob")["message"]["mentions"], ["bob"])
        with self.assertRaises(ClientError) as e:
            self.alice.send(chat="John Smith", text="which one?")
        self.assertIn("jsmith, jsmith2", str(e.exception))

    def test_first_messages_both_ways_make_one_chat(self):
        pairs = [(self.server.user(f"x{i}", f"X {i}"), self.server.user(f"y{i}", f"Y {i}")) for i in range(20)]
        threads = [threading.Thread(target=c.send, kwargs={"chat": other, "text": "hi"})
                   for i, (x, y) in enumerate(pairs) for c, other in ((x, f"y{i}"), (y, f"x{i}"))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual([len(x.chats()["chats"]) for x, _ in pairs], [1] * 20)

    def test_unknown_people_and_tokens(self):
        with self.assertRaises(ClientError) as e:
            self.alice.send(chat="bobb", text="x")
        self.assertIn("Did you mean", str(e.exception))
        with self.assertRaises(ClientError) as e:
            self.server.client("wrong").chats()
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(ClientError) as e:
            self.alice.user_add(id="mallory")
        self.assertEqual(e.exception.status, 403)  # a known user, who is not the super admin

    def test_mentions_and_notification_levels(self):
        chat = self.alice.send(chat="bob,carol", text="hello both")["chat"]["id"]
        self.bob.settings_notify(chat=chat, level="mentions")
        self.assertEqual(self.bob.notifications()["chats"], [])
        self.alice.send(chat=chat, text="@Bob can you check?")
        self.assertTrue(self.bob.notifications()["chats"][0]["mentioned"])
        self.assertEqual(self.texts(self.bob.read(mentions=True)), ["@Bob can you check?"])
        m = self.carol.send(chat=chat, text="@all done")["message"]
        self.assertEqual(sorted(m["mentions"]), ["alice", "bob"])


class Messages(Base):
    def test_edit_delete_react_survive_a_restart(self):
        m = self.alice.send(chat="bob", text="frist")["message"]
        self.alice.edit(id=m["id"], text="first")
        self.bob.react(id=m["id"], emoji="+1")
        with self.assertRaises(ClientError) as e:
            self.bob.edit(id=m["id"], text="mine now")
        self.assertEqual(e.exception.status, 403)
        gone = self.alice.send(chat="bob", text="oops")["message"]
        self.alice.delete(id=gone["id"])
        self.bob.read()
        self.restart()
        history = self.bob.read(chat="alice", last=10)["messages"]
        self.assertEqual(history[0]["text"], "first")
        self.assertEqual(history[0]["reactions"], {"+1": ["bob"]})
        self.assertTrue(history[1]["deleted"])
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 0)  # read positions survive too

    def test_history_search_since_from_where(self):
        for i in range(30):  # odd notes come from alice, even ones from carol
            (self.alice if i % 2 else self.carol).send(chat="bob", text=f"note {i}")
        self.assertEqual(self.texts(self.bob.read(chat="alice", last=3)), ["note 25", "note 27", "note 29"])
        self.assertEqual(len(self.bob.read(search="NOTE 1", peek=True)["messages"]), 11)
        self.assertEqual(self.texts(self.bob.read(**{"from": "carol"}, last=2)), ["note 26", "note 28"])
        r = self.bob.read(where="int(m.text.split()[1]) % 10 == 0", all=True, peek=True)
        self.assertEqual(self.texts(r), ["note 0", "note 10", "note 20"])
        since = self.bob.read(chat="alice", last=1)["messages"][0]["id"]
        self.alice.send(chat="bob", text="later")
        self.assertEqual(self.texts(self.bob.read(chat="alice", since=since)), ["later"])

    def test_unread_limit_and_reading_in_pages(self):
        for i in range(60):
            self.alice.send(chat="bob", text=str(i))
        page = self.bob.read()
        self.assertEqual(len(page["messages"]), 50)
        self.assertEqual(page["unread_left"], 10)
        self.assertEqual(self.texts(self.bob.read()), [str(i) for i in range(50, 60)])

    def tree(self, result):
        return [(m["depth"], m["text"]) for m in result["messages"]]

    def test_a_thread_is_its_first_message_and_every_reply_below_it(self):
        board = self.alice.channel_create(name="board", members="bob,carol")["chat"]["id"]
        post = self.alice.send(chat=board, text="post")["message"]["id"]
        other = self.bob.send(chat=board, text="another post")["message"]["id"]
        first = self.bob.send(chat=board, text="comment 1", reply_to=post)["message"]["id"]
        self.carol.send(chat=board, text="on the other post", reply_to=other)
        second = self.carol.send(chat=board, text="comment 2", reply_to=f" {post} ")["message"]["id"]
        answer = self.alice.send(chat=board, text="answer to 1", reply_to=first)["message"]["id"]
        self.bob.send(chat=board, text="answer to the answer", reply_to=answer)
        tree = [(0, "post"), (1, "comment 1"), (2, "answer to 1"), (3, "answer to the answer"), (1, "comment 2")]
        for start in (post, first, answer, second):  # from any message in it
            self.assertEqual(self.tree(self.carol.read(thread=start)), tree)
        self.assertEqual(self.tree(self.carol.read(thread=other)), [(0, "another post"), (1, "on the other post")])
        self.assertEqual(self.carol.read(thread=second)["messages"][4]["reply_to"], post)  # stored as the plain id
        self.assertEqual(self.tree(self.carol.read(thread=post, where="m['from'] == 'bob'")),
                         [(1, "comment 1"), (3, "answer to the answer")])
        self.assertEqual(self.tree(self.carol.read(thread=post, limit=2)), tree[:2])

    def test_a_thread_keeps_its_shape_through_deletes_and_restarts_and_marks_nothing_read(self):
        sent = self.alice.send(chat="bob,carol", text="post")["message"]
        chat, post = sent["chat"], sent["id"]
        gone = self.bob.send(chat=chat, text="comment", reply_to=post)["message"]["id"]
        self.carol.send(chat=chat, text="answer", reply_to=gone)
        self.bob.delete(id=gone)
        self.restart()
        unread = self.carol.chats()["chats"][0]["unread"]
        r = self.carol.read(thread=post)
        self.assertEqual(self.tree(r), [(0, "post"), (1, ""), (2, "answer")])
        self.assertTrue(r["messages"][1]["deleted"])
        self.assertEqual(self.carol.chats()["chats"][0]["unread"], unread)  # the thread view moved no read position
        self.assertEqual(self.bob.read()["messages"][0]["text"], "post")
        with self.assertRaises(ClientError) as e:  # only members see a thread
            self.server.user("dora", "Dora Diaz").read(thread=post)
        self.assertEqual(e.exception.status, 404)

    def test_replies_say_whose_message_they_answer(self):
        board = self.alice.channel_create(name="board", members="bob,carol")["chat"]["id"]
        post = self.alice.send(chat=board, text="post")["message"]["id"]
        reply = self.bob.send(chat=board, text="reply", reply_to=post)["message"]
        self.assertEqual(reply["reply_to_from"], "alice")
        self.assertEqual(self.alice.send(chat=board, text="answer", reply_to=reply["id"])["message"]["reply_to_from"],
                         "bob")
        self.alice.read()
        self.carol.send(chat=board, text="unrelated")
        self.carol.send(chat=board, text="to bob", reply_to=reply["id"])
        self.carol.send(chat=board, text="to alice", reply_to=post)
        self.assertEqual(self.texts(self.alice.wait(timeout=2, where="m.reply_to_from == 'alice'")), ["to alice"])
        self.restart()  # worked out from the log, so nothing to store
        self.assertEqual([m.get("reply_to_from") for m in self.carol.read(chat=board, all=True)["messages"]],
                         [None, "alice", "bob", None, "bob", "alice"])

    def test_sort_orders_the_chosen_messages_then_limits(self):
        board = self.alice.channel_create(name="board", members="bob,carol")["chat"]["id"]
        ids = [self.alice.send(chat=board, text=f"post {i}")["message"]["id"] for i in range(5)]
        for voter, post in ((self.bob, 3), (self.carol, 3), (self.bob, 1), (self.carol, 4)):
            voter.react(id=ids[post], emoji="+1")
        votes = "-len((m.reactions or {}).get('+1', []))"
        top = self.bob.read(chat=board, all=True, sort=votes, limit=3, peek=True)
        self.assertEqual(self.texts(top), ["post 3", "post 1", "post 4"])  # a tie keeps the order in time
        self.assertEqual(self.texts(self.bob.read(chat=board, last=3, sort=votes, peek=True)),
                         ["post 3", "post 4", "post 2"])  # the last 3, ordered
        self.assertEqual(self.texts(self.bob.read(chat=board, last=3, where="m.text != 'post 4'", sort=votes,
                                                  peek=True)), ["post 3", "post 1", "post 2"])
        self.assertEqual(len(self.bob.read(chat=board, all=True, sort="m.text", peek=True)["messages"]), 5)
        self.assertEqual(self.texts(self.bob.read(chat=board, sort=votes))[:2], ["post 3", "post 1"])  # unread ones
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 0)
        for bad, words in (("m.reactions", "cannot order"), ("m.text +", "not a Python expression")):
            with self.assertRaises(ClientError) as e:
                self.bob.read(chat=board, all=True, sort=bad)
            self.assertIn(words, str(e.exception))

    def test_own_messages_are_never_unread(self):
        self.alice.send(chat="bob", text="mine")
        self.assertEqual(self.alice.chats()["chats"][0]["unread"], 0)
        self.assertEqual(self.alice.read()["messages"], [])


class Receipts(Base):
    def mine(self, client, chat):
        return client.read(chat=chat, last=1, peek=True)["messages"][0]

    def test_direct_chat_receipt(self):
        self.alice.send(chat="bob", text="did you get this?")
        m = self.mine(self.alice, "bob")
        self.assertEqual((m["read_by"], m["unread_by"]), ([], ["bob"]))
        self.bob.read()
        m = self.mine(self.alice, "bob")
        self.assertEqual([r["id"] for r in m["read_by"]], ["bob"])
        self.assertTrue(m["read_by"][0]["at"])
        self.assertNotIn("read_by", self.mine(self.bob, "alice"))  # only the sender sees receipts

    def test_channel_receipts_and_restart(self):
        self.server.user("dan", "Dan Dorr")
        chat = self.alice.channel_create(name="team", members="bob,carol,dan")["chat"]["id"]
        self.alice.send(chat=chat, text="please read")
        before = self.alice.chats()["chats"][0]["last_at"]
        self.bob.read()
        self.carol.wait(timeout=1)
        self.assertEqual(self.alice.chats()["chats"][0]["last_at"], before)  # reading is not chat activity
        self.restart()
        m = self.mine(self.alice, chat)
        self.assertEqual(sorted(r["id"] for r in m["read_by"]), ["bob", "carol"])
        self.assertEqual(m["unread_by"], ["dan"])

    def test_peeking_leaves_no_receipt(self):
        self.alice.send(chat="bob", text="x")
        self.bob.read(peek=True)
        self.assertEqual(self.mine(self.alice, "bob")["unread_by"], ["bob"])


class Waiting(Base):
    def test_wait_blocks_until_a_message_arrives(self):
        threading.Timer(0.3, lambda: self.alice.send(chat="bob", text="ping")).start()
        t = time.monotonic()
        r = self.bob.wait(timeout=10)
        self.assertEqual(self.texts(r), ["ping"])
        self.assertLess(time.monotonic() - t, 2)
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 0)

    def test_wait_times_out_empty(self):
        t = time.monotonic()
        self.assertEqual(self.bob.wait(timeout=0.5)["messages"], [])
        self.assertGreaterEqual(time.monotonic() - t, 0.5)

    def test_nothing_is_lost_between_waits(self):
        self.alice.send(chat="bob", text="one")
        self.alice.send(chat="bob", text="two")
        self.assertEqual(self.texts(self.bob.wait(timeout=1)), ["one", "two"])
        self.alice.send(chat="bob", text="three")  # arrives while bob is not waiting
        self.assertEqual(self.texts(self.bob.wait(timeout=1)), ["three"])

    def test_wait_filters(self):
        chat = self.alice.send(chat="bob,carol", text="start")["chat"]["id"]
        self.bob.read()
        self.bob.settings_notify(chat=chat, level="none")
        self.alice.send(chat=chat, text="muted")
        self.assertEqual(self.bob.wait(timeout=0.3)["messages"], [])
        self.assertEqual(self.texts(self.bob.wait(timeout=0.3, chat=chat)), ["muted"])
        threading.Timer(0.2, lambda: [self.carol.send(chat="bob", text="noise"),
                                      self.alice.send(chat="bob", text="the build")]).start()
        r = self.bob.wait(timeout=5, where="'build' in m.text")
        self.assertEqual(self.texts(r), ["the build"])

    def test_unconfirmed_messages_stay_unread_when_the_server_cannot_be_reached(self):
        self.alice.send(chat="bob", text="confirm me")
        self.assertEqual(self.texts(self.bob.wait(timeout=1)), ["confirm me"])
        self.bob._drop()
        self.bob.port = 9  # the server cannot be reached now: nothing listens there
        self.bob.close()  # cannot confirm: must neither raise nor loop
        self.assertTrue(self.bob.ack)
        again = self.server.client(self.bob.token)
        self.assertEqual(self.texts(again.read()), ["confirm me"])  # it comes again, nothing is lost

    def test_many_waiters_wake(self):
        users = [self.server.user(f"u{i}", f"User {i}") for i in range(30)]
        results = []
        threads = [threading.Thread(target=lambda u=u: results.append(u.wait(timeout=10))) for u in users]
        [t.start() for t in threads]
        time.sleep(0.3)
        self.alice.channel_create(name="all hands", members=",".join(f"u{i}" for i in range(30)))
        self.alice.send(chat="#all hands", text="go")
        [t.join(5) for t in threads]
        self.assertEqual([self.texts(r) for r in results], [["go"]] * 30)


class Storage(Base):
    def test_log_files_rotate_and_reload(self):
        data = tempfile.mkdtemp(prefix="swarmchat-test-")
        server = Server(data, segment=7)
        try:
            a, b = server.user("a", "A"), server.user("b", "B")
            chat = a.send(chat="b", text="0")["chat"]["id"]  # line 1 is the chat's creation, line 2 this message
            for i in range(1, 20):
                a.send(chat="b", text=str(i))
            logs = sorted(os.listdir(os.path.join(data, "chats", chat, "log")))
            self.assertEqual(logs, ["000001.jsonl", "000002.jsonl", "000003.jsonl"])
            server.stop()
            server = Server(data)  # the folder keeps its own segment size, whatever is asked now
            self.assertEqual(server.store.segment, 7)
            b = server.client(b.token)
            self.assertEqual([m["text"] for m in b.read(chat="a", last=3)["messages"]], ["17", "18", "19"])
            server.client(a.token).send(chat="b", text="20")
            self.assertEqual([m["text"] for m in b.read(chat="a", last=2)["messages"]], ["19", "20"])
        finally:
            server.stop()
            shutil.rmtree(data)

    def test_a_half_written_line_is_dropped(self):
        chat = self.alice.send(chat="bob", text="kept")["chat"]["id"]
        self.server.stop()
        with open(os.path.join(self.data, "chats", chat, "log", "000001.jsonl"), "ab") as f:
            f.write(b'{"seq":3,"kind":"mess')
        self.server = Server(self.data)
        self.alice = self.server.client(self.alice.token)
        self.bob = self.server.client(self.bob.token)
        self.alice.send(chat="bob", text="after")
        self.assertEqual(self.texts(self.bob.read()), ["kept", "after"])

    def test_policy_contacts(self):
        self.server.admin.policy_set(rules={"contacts": {"alice": ["bob"], "bob": ["carol"]}})
        self.alice.send(chat="bob", text="fine")
        self.bob.send(chat="alice", text="contacts work both ways")
        self.bob.channel_create(name="team", members="alice,carol")  # bob may reach both
        self.alice.send(chat="#team", text="anyone in a channel may post in it")
        self.bob.channel_create(name="pair", members="alice")
        for call in (lambda: self.alice.send(chat="carol", text="not a contact"),
                     lambda: self.carol.send(chat="alice,bob", text="alice is not carol's contact"),
                     lambda: self.alice.channel_create(name="mine", members="carol"),
                     lambda: self.alice.channel_add(chat="#pair", users="carol")):
            with self.assertRaises(ClientError) as e:
                call()
            self.assertEqual(e.exception.status, 403)

    def test_policy_posters(self):
        records = self.alice.channel_create(name="records", members="bob,carol")["chat"]["id"]
        self.alice.channel_create(name="talk", members="bob")
        self.server.admin.policy_set(rules={"posters": {"#Records": ["alice"]}})
        first = self.alice.send(chat="#records", text="new best: 42")["message"]["id"]
        self.bob.send(chat="#talk", text="other channels are open")
        self.bob.react(id=first, emoji="+1")
        for call in (lambda: self.bob.send(chat="#records", text="me too"),
                     lambda: self.carol.send(chat=records, text="by id"),
                     lambda: self.bob.send(chat="#records", text="a reply", reply_to=first)):
            with self.assertRaises(ClientError) as e:
                call()
            self.assertEqual(e.exception.status, 403)
            self.assertIn("Only alice may post in #records", str(e.exception))
        self.alice.edit(id=first, text="new best: 43")
        self.assertEqual(self.texts(self.carol.read(chat="#records")), ["new best: 43"])
        self.server.admin.policy_set(rules={"posters": {records: ["bob"]}})  # by id
        self.bob.send(chat="#records", text="bob now")
        with self.assertRaises(ClientError):
            self.alice.send(chat="#records", text="alice not any more")


class MorePaths(Base):
    def test_channel_members_and_names_change(self):
        chat = self.alice.channel_create(name="ops", members="bob")["chat"]["id"]
        self.alice.channel_add(chat="#ops", users="carol")
        self.carol.send(chat="#ops", text="joined")
        self.alice.channel_rename(chat=chat, name="operations")
        self.assertEqual(self.bob.chats(name="operations")["chats"][0]["id"], chat)
        self.alice.channel_remove(chat=chat, users="bob")
        with self.assertRaises(ClientError):
            self.bob.send(chat=chat, text="still here?")
        self.assertEqual(self.texts(self.carol.read(chat="operations", last=5)), ["joined"])

    def test_tokens_rotate_and_users_go(self):
        old = self.bob.token
        self.bob = self.server.client(self.server.admin.token_rotate(id="bob")["token"])
        with self.assertRaises(ClientError) as e:
            self.server.client(old).chats()
        self.assertEqual(e.exception.status, 401)
        direct = self.alice.send(chat="bob,carol", text="hi")["chat"]["id"]
        channel = self.alice.channel_create(name="team", members="bob,carol")["chat"]["id"]
        self.server.admin.user_remove(id="bob")
        self.assertEqual([m["id"] for m in self.alice.chat_info(chat=channel)["chat"]["members"]], ["alice", "carol"])
        self.assertEqual(len(self.alice.chat_info(chat=direct)["chat"]["members"]), 3)  # kept as a record
        self.assertEqual([u["id"] for u in self.alice.people()["people"]], ["alice", "carol"])

    def test_status_search_filters_and_one_message(self):
        self.bob.settings_status(text="out this week")
        self.assertEqual([u["id"] for u in self.alice.people(search="out this")["people"]], ["bob"])
        dm = self.alice.send(chat="bob", text="dm")["message"]
        self.alice.send(chat="carol", text="other")
        self.assertEqual([c["title"] for c in self.alice.chats(**{"with": "bob"})["chats"]], ["Bob Burns"])
        self.assertEqual(self.alice.chats(sort="name")["chats"][0]["title"], "Bob Burns")
        self.assertEqual(self.bob.read(id=dm["id"])["messages"][0]["text"], "dm")
        self.assertEqual(len(self.bob.notifications(all=True)["chats"]), 1)

    def test_bad_requests_are_refused_clearly(self):
        m = self.alice.send(chat="bob", text="hi")["message"]
        other = self.alice.send(chat="carol", text="hi")["message"]
        for call, status in ((lambda: self.alice.send(chat="bob", text=" "), 400),
                             (lambda: self.alice.send(chat="bob", text="x" * 20001), 400),
                             (lambda: self.alice.send(chat="bob", text="r", reply_to=other["id"]), 400),
                             (lambda: self.carol.read(id=m["id"]), 404),
                             (lambda: self.alice.react(id=m["id"], emoji=""), 400),
                             (lambda: self.alice.settings_notify(chat="bob", level="loud"), 400),
                             (lambda: self.alice.channel_create(name=""), 400),
                             (lambda: self.alice.nonsense(), 404)):
            with self.assertRaises(ClientError) as e:
                call()
            self.assertEqual(e.exception.status, status)
        self.alice.delete(id=m["id"])
        with self.assertRaises(ClientError):
            self.alice.edit(id=m["id"], text="back")

    def test_unicode_and_reactions_come_and_go(self):
        m = self.alice.send(chat="bob", text="naïve café ☕\nsecond line")["message"]
        self.bob.react(id=m["id"], emoji="☕")
        self.bob.react(id=m["id"], emoji="☕", remove=True)
        got = self.bob.read()["messages"][0]
        self.assertEqual(got["text"], "naïve café ☕\nsecond line")
        self.assertNotIn("reactions", got)
        self.assertEqual(len(self.bob.read(search="CAFÉ", all=True)["messages"]), 1)

    def test_wait_by_sender_and_by_mention(self):
        chat = self.alice.send(chat="bob,carol", text="start")["chat"]["id"]
        self.bob.read()
        self.carol.send(chat=chat, text="from carol")
        self.alice.send(chat=chat, text="from alice")
        self.assertEqual(self.texts(self.bob.wait(timeout=1, **{"from": "alice"})), ["from alice"])
        self.alice.send(chat=chat, text="plain")
        self.alice.send(chat=chat, text="@bob look")
        self.assertEqual(self.texts(self.bob.wait(timeout=1, mentions=True)), ["@bob look"])

    def test_durable_mode_and_a_normal_stop_keep_read_positions(self):
        data = tempfile.mkdtemp(prefix="swarmchat-test-")
        port = 18700 + os.getpid() % 200
        env = {**os.environ, "PYTHONPATH": ROOT}
        cmd = [sys.executable, "-m", "swarmchat", "serve", "--data", data, "--port", str(port), "--fsync"]
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        server = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, **group)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with open(os.path.join(data, "admin.token")) as f:
                        admin = Client(url, f.read().strip())
                        admin.policy_show()
                        break
                except (OSError, ClientError):
                    time.sleep(0.05)
            a = Client(url, admin.user_add(id="a")["token"])
            b = Client(url, admin.user_add(id="b")["token"])
            a.send(chat="b", text="one")
            self.assertEqual(self.texts(b.read()), ["one"])
            # A normal stop (SIGTERM, or Ctrl-Break on Windows): read positions are written before it exits.
            server.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            server.wait(10)
            server = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, **group)
            for _ in range(100):
                try:
                    self.assertEqual(b.read()["messages"], [])
                    break
                except ClientError:
                    time.sleep(0.05)
            for c in (a, b, admin):
                c.close()
        finally:
            server.kill()
            server.wait()
            shutil.rmtree(data)


class PublicChannels(Base):
    def listed(self, client, **params):
        return [(c["name"], c["members"], c["joined"]) for c in client.channel_list(**params)["channels"]]

    def refused(self, call, status, words):
        with self.assertRaises(ClientError) as e:
            call()
        self.assertEqual(e.exception.status, status)
        self.assertIn(words, str(e.exception))

    def test_a_channel_is_private_unless_made_public(self):
        self.alice.channel_create(name="plans", members="bob")
        self.assertEqual(self.listed(self.carol), [])
        self.refused(lambda: self.carol.channel_join(chat="plans"), 404, "There is no channel plans")
        self.refused(lambda: self.carol.read(chat="#plans"), 404, "There is no channel #plans")
        board = self.alice.channel_create(name="board", public=True)["chat"]
        self.assertTrue(board["public"])
        self.assertEqual(self.listed(self.carol), [("board", 1, False)])
        self.refused(lambda: self.carol.send(chat="#board", text="hi"), 403, "It is public, so you can join it")
        self.assertEqual(self.carol.channel_join(chat="#board")["chat"]["name"], "board")
        self.carol.channel_join(chat=board["id"])  # by id too, and twice is fine
        self.carol.send(chat="#board", text="first post")
        self.assertEqual(self.texts(self.alice.read(chat="#board")), ["first post"])
        self.assertEqual(self.listed(self.carol), [("board", 2, True)])
        self.assertEqual(self.listed(self.bob, search="BOA"), [("board", 2, False)])
        self.assertEqual(self.listed(self.bob, search="plans"), [])

    def test_members_change_visibility_and_members_stay(self):
        self.alice.channel_create(name="board", public=True)
        self.carol.channel_join(chat="board")
        self.refused(lambda: self.bob.channel_visibility(chat="#board", visibility="private"), 403, "not in #board")
        self.carol.channel_visibility(chat="#board", visibility="private")
        self.assertEqual(self.listed(self.bob), [])
        self.refused(lambda: self.bob.channel_join(chat="board"), 404, "There is no channel board")
        self.carol.send(chat="#board", text="still here")  # members stay
        self.alice.channel_create(name="plans")
        self.alice.channel_visibility(chat="#plans", visibility="public")
        self.restart()  # visibility is kept
        self.assertEqual(self.listed(self.bob), [("plans", 1, False)])
        self.bob.channel_join(chat="plans")
        self.alice.send(chat="bob", text="a direct chat")
        self.refused(lambda: self.alice.channel_visibility(chat="bob", visibility="public"), 400, "Only a channel")
        self.refused(lambda: self.alice.channel_visibility(chat="#plans", visibility="open"), 400, "public or private")
        self.refused(lambda: self.bob.channel_join(chat="alice"), 404, "no channel")
        kinds = [json.loads(line).get("public") for line in open(os.path.join(
            self.data, "chats", self.alice.chats(name="plans")["chats"][0]["id"], "log", "000001.jsonl"))
            if json.loads(line)["kind"] == "visibility"]
        self.assertEqual(kinds, [True])  # the change is in the chat's history

    def test_a_channel_has_a_description_its_members_change(self):
        address = "Release 2.4, shipping Friday"
        chat = self.alice.channel_create(name="release-2-4", members="bob", public=True, description=f"  {address} ")
        self.assertEqual(chat["chat"]["description"], address)
        self.assertEqual(self.carol.channel_list()["channels"][0]["description"], address)
        self.assertEqual(self.alice.channel_create(name="plain")["chat"]["description"], "")
        self.refused(lambda: self.carol.channel_describe(chat="#release-2-4", description="mine"), 403, "not in")
        self.assertEqual(self.bob.channel_describe(chat="#release-2-4", description="Freeze moved to Monday")["chat"]
                         ["description"], "Freeze moved to Monday")
        self.bob.channel_describe(chat="#release-2-4", description="Freeze moved to Monday")  # the same is no change
        self.refused(lambda: self.bob.channel_describe(chat="#release-2-4", description="x" * 1001), 400, "1,000")
        self.refused(lambda: self.bob.channel_describe(chat="#release-2-4", description=["a"]), 400, "is text")
        self.refused(lambda: self.alice.channel_create(name="big", description="x" * 1001), 400, "1,000")
        self.alice.send(chat="bob", text="a direct chat")
        self.refused(lambda: self.alice.channel_describe(chat="bob", description="us"), 400, "Only a channel")
        self.restart()  # the description is kept
        self.assertEqual(self.bob.chat_info(chat="#release-2-4")["chat"]["description"], "Freeze moved to Monday")
        snapshot = self.server.admin.view_snapshot()["chats"]
        self.assertEqual(next(c["description"] for c in snapshot if c["id"] == chat["chat"]["id"]),
                         "Freeze moved to Monday")
        self.alice.channel_describe(chat="#release-2-4", description="")
        self.assertEqual(self.bob.chats(name="release-2-4")["chats"][0]["description"], "")
        with open(os.path.join(self.data, "chats", chat["chat"]["id"], "log", "000001.jsonl")) as f:
            log = [json.loads(line) for line in f]
        self.assertEqual(log[0]["description"], address)  # the chat's history has every description
        self.assertEqual([(e["from"], e["description"]) for e in log if e["kind"] == "described"],
                         [("bob", "Freeze moved to Monday"), ("alice", "")])

    def test_anyone_may_join_a_public_channel_under_contact_rules(self):
        self.server.admin.policy_set(rules={"contacts": {"alice": ["bob"]}})
        self.alice.channel_create(name="board", members="bob", public=True)
        self.alice.channel_create(name="plans", members="bob")
        self.carol.channel_join(chat="board")  # making it public was the check
        self.carol.send(chat="#board", text="hello all")
        self.refused(lambda: self.carol.channel_join(chat="plans"), 404, "There is no channel plans")
        self.refused(lambda: self.carol.send(chat="alice", text="hi"), 403, "may not contact")


class Administrators(Base):
    def refused(self, call, status, words):
        with self.assertRaises(ClientError) as e:
            call()
        self.assertEqual(e.exception.status, status)
        self.assertIn(words, str(e.exception))

    def log(self, chat):
        with open(os.path.join(self.data, "chats", chat, "log", "000001.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_managers_and_administrators_remove_others(self):
        self.alice.channel_create(name="ops", members="bob,carol")
        self.bob.channel_remove(chat="#ops", users="carol")  # an open server: any member may
        self.alice.channel_add(chat="#ops", users="carol")
        self.server.admin.policy_set(rules={"admins": ["Carol"]})
        self.refused(lambda: self.bob.channel_remove(chat="#ops", users="carol"), 403,
                     "Only a channel manager or an administrator may remove someone else from a channel.")
        self.refused(lambda: self.bob.channel_remove(chat="#ops", users="bob,carol"), 403, "Only a channel manager")
        self.bob.channel_leave(chat="#ops")  # anyone may still leave
        self.bob.channel_create(name="bobs", members="carol")
        self.bob.channel_remove(chat="#bobs", users="bob")
        self.alice.channel_add(chat="#ops", users="bob")  # any member adds people
        self.carol.channel_remove(chat="#ops", users="bob")  # an administrator in the channel
        self.alice.channel_add(chat="#ops", users="bob")
        self.alice.channel_remove(chat="#ops", users="bob")  # its creator manages it
        self.assertEqual(sorted(m["id"] for m in self.alice.chat_info(chat="#ops")["chat"]["members"]),
                         ["alice", "carol"])

    def test_a_channels_managers_manage_it(self):
        chat = self.alice.channel_create(name="ops", members="bob,carol")["chat"]
        self.assertEqual(chat["managers"], ["alice"])
        self.server.admin.policy_set(rules={"admins": []})  # roles, and no administrators
        for call, what in ((lambda: self.bob.channel_rename(chat="#ops", name="ops2"), "rename a channel"),
                           (lambda: self.bob.channel_describe(chat="#ops", description="Mine"),
                            "change a channel's description"),
                           (lambda: self.bob.channel_visibility(chat="#ops", visibility="private"),
                            "make a channel private"),
                           (lambda: self.bob.channel_archive(chat="#ops"), "archive or unarchive a channel"),
                           (lambda: self.bob.channel_managers(chat="#ops", add="bob"), "choose a channel's managers")):
            self.refused(call, 403, f"Only a channel manager or an administrator may {what}.")
        self.assertEqual(self.bob.channel_managers(chat="#ops")["chat"]["managers"], ["alice"])  # members may look
        self.refused(lambda: self.alice.channel_visibility(chat="#ops", visibility="public"), 403,
                     "Only an administrator may make a private channel public.")  # its manager may not
        self.server.admin.policy_set(rules={"admins": ["carol"]})
        self.assertTrue(self.carol.channel_visibility(chat="#ops", visibility="public")["chat"]["public"])
        self.assertFalse(self.alice.channel_visibility(chat="#ops", visibility="private")["chat"]["public"])
        self.server.admin.policy_set(rules={"admins": [], "allow": {"make_public": ["manager"]}})
        self.alice.channel_visibility(chat="#ops", visibility="public")
        self.alice.channel_visibility(chat="#ops", visibility="private")
        self.server.admin.policy_set(rules={"admins": []})
        self.server.user("dave", "Dave Diaz")
        self.refused(lambda: self.alice.channel_managers(chat="#ops", add="dave"), 400, "dave is not in #ops")
        self.assertEqual(self.alice.channel_managers(chat="#ops", add="bob")["chat"]["managers"], ["alice", "bob"])
        self.bob.channel_rename(chat="#ops", name="ops2")
        self.bob.channel_describe(chat="#ops2", description="Operations")
        self.assertEqual(self.bob.channel_managers(chat="#ops2", remove="alice")["chat"]["managers"], ["bob"])
        self.refused(lambda: self.alice.channel_rename(chat="#ops2", name="ops"), 403, "Only a channel manager")
        self.restart()  # managers are kept
        self.assertEqual(self.carol.chat_info(chat="#ops2")["chat"]["managers"], ["bob"])
        self.bob.channel_leave(chat="#ops2")  # leaving ends the role
        self.assertEqual(self.alice.chat_info(chat="#ops2")["chat"]["managers"], [])
        self.server.admin.policy_set(rules={"admins": ["carol"]})
        self.carol.channel_managers(chat="#ops2", add="alice")  # an administrator in the channel names one
        self.alice.channel_rename(chat="#ops2", name="ops")
        self.assertEqual([(e["from"], e.get("added"), e.get("removed")) for e in self.log(chat["id"])
                          if e["kind"] == "managers"],
                         [("alice", ["bob"], None), ("bob", None, ["alice"]), ("carol", ["alice"], None)])
        self.alice.send(chat="bob", text="direct")
        self.refused(lambda: self.alice.channel_managers(chat="bob", add="bob"), 400, "Only a channel has managers")
        # A channel from before managers: its creator manages it.
        old = self.alice.channel_create(name="old", members="bob")["chat"]["id"]
        meta_path = os.path.join(self.data, "chats", old, "meta.json")
        with open(meta_path) as f:
            meta = json.load(f)
        del meta["managers"]
        with open(meta_path, "w") as f:
            json.dump(meta, f)
        self.restart()
        self.assertEqual(self.bob.chat_info(chat="#old")["chat"]["managers"], ["alice"])

    def test_the_super_admin_lets_managers_delete_messages(self):
        self.alice.channel_create(name="ops", members="bob")
        first = self.bob.send(chat="#ops", text="spam")["message"]["id"]
        self.refused(lambda: self.alice.delete(id=first), 403, "Only an administrator may delete another person's")
        self.server.admin.policy_set(rules={"admins": [], "allow": {"delete_message": ["manager", "admin"]}})
        self.assertTrue(self.alice.delete(id=first)["message"]["deleted"])
        self.server.admin.policy_set(rules={"admins": [], "allow": {"delete_message": []}})
        second = self.bob.send(chat="#ops", text="more spam")["message"]["id"]
        self.refused(lambda: self.alice.delete(id=second), 403, "No one may delete another person's message.")
        self.bob.delete(id=second)  # the sender still may

    def test_only_the_super_admin_makes_accounts_tokens_and_rules(self):
        self.server.admin.policy_set(rules={"admins": ["alice"]})
        for call, what in ((lambda: self.alice.user_add(id="dora"), "add users"),
                           (lambda: self.alice.user_remove(id="bob"), "remove users"),
                           (lambda: self.alice.token_rotate(id="bob"), "give someone else a new token"),
                           (lambda: self.alice.role_change(role="admin", add="bob"), "choose the administrators")):
            self.refused(call, 403, f"Only the super admin may {what}.")
        self.refused(lambda: self.alice.policy_set(rules={"admins": ["alice", "bob"]}), 403,
                     "Only the super admin may set the rules.")
        self.bob.whoami()  # bob's token still works: an administrator cannot take over an account
        self.assertEqual(self.alice.policy_show()["rules"], {"admins": ["alice"]})  # administrators may look
        self.assertEqual(self.alice.role_change(role="admin")["users"], ["alice"])
        self.refused(lambda: self.bob.policy_show(), 403, "Only the super admin or an administrator may see the rules.")
        self.refused(lambda: self.server.client("nonsense").policy_show(), 401, "the admin token")
        # The super admin may hand any action to other roles, here naming administrators.
        self.server.admin.policy_set(rules={"admins": ["alice"], "allow": {"edit_admins": ["superadmin", "admin"]}})
        self.assertEqual(self.alice.role_change(role="admin", add="bob")["users"], ["alice", "bob"])
        self.bob.role_change(role="admin", remove="alice")
        self.assertEqual(self.server.admin.role_change(role="admin")["users"], ["bob"])
        self.refused(lambda: self.alice.policy_show(), 403, "Only the super admin or an administrator")
        for rules, words in (({"allow": {"fly": ["admin"]}}, "There is no action 'fly'"),
                             ({"allow": {"add_user": ["king"]}}, "is a list of roles"),
                             ({"allow": {"add_user": "admin"}}, "is a list of roles"),
                             ({"allow": ["add_user"]}, "maps actions"),
                             ({"admins": "alice"}, "list of user ids"),
                             ({"channel_creators": [1]}, "list of user ids")):
            self.refused(lambda: self.server.admin.policy_set(rules=rules), 400, words)
        self.assertIsNone(self.server.admin.role_change(role="channel_creator")["users"])  # everyone
        self.server.admin.policy_set(rules={})
        self.assertEqual(self.server.admin.role_change(role="admin")["users"], [])
        self.refused(lambda: self.server.admin.role_change(role="king"), 400, "admin or channel_creator")
        self.refused(lambda: self.server.admin.role_change(role="admin", everyone=True), 400, "Only channel_creator")
        self.refused(lambda: self.server.admin.role_change(role="admin", add="nobody"), 404, "No one called")

    def test_who_may_create_channels(self):
        self.bob.channel_create(name="anyone")  # without rules, everyone may
        self.server.admin.policy_set(rules={"admins": ["alice"]})
        self.bob.channel_create(name="still-anyone")  # naming administrators leaves it to everyone
        self.assertEqual(self.alice.role_change(role="channel_creator", add="carol")["users"], ["carol"])
        self.refused(lambda: self.bob.channel_create(name="bobs"), 403,
                     "Only someone chosen to create channels or an administrator may create a channel.")
        self.carol.channel_create(name="carols")
        self.alice.channel_create(name="alices")  # administrators always may
        self.bob.send(chat="carol", text="a direct chat needs no permission")
        self.refused(lambda: self.bob.role_change(role="channel_creator", add="bob"), 403,
                     "Only the super admin or an administrator may choose who may create channels.")
        self.assertIsNone(self.alice.role_change(role="channel_creator", everyone=True)["users"])
        self.bob.channel_create(name="bobs")
        self.assertEqual(self.server.admin.policy_show()["rules"], {"admins": ["alice"]})

    def test_anyone_gives_themselves_a_new_token(self):
        new = self.bob.settings_token()["token"]
        self.refused(self.bob.whoami, 401, "Unknown token")
        self.assertEqual(self.server.client(new).whoami()["user"]["id"], "bob")

    def test_an_administrator_deletes_anyones_message(self):
        chat = self.alice.channel_create(name="ops", members="bob,carol")["chat"]["id"]
        first = self.bob.send(chat="#ops", text="a secret")["message"]["id"]
        self.refused(lambda: self.alice.delete(id=first), 403, "Only an administrator may delete another person's")
        self.server.admin.policy_set(rules={"admins": ["alice"]})
        self.refused(lambda: self.carol.delete(id=first), 403, "Only an administrator may delete another person's")
        self.refused(lambda: self.alice.edit(id=first, text="mine now"), 403, "Only the sender can edit")
        self.assertTrue(self.alice.delete(id=first)["message"]["deleted"])
        shown = self.carol.read(chat="#ops", all=True)["messages"][0]
        self.assertEqual((shown["deleted"], shown["text"], shown["from"]), (True, "", "bob"))
        self.assertEqual([(e["from"], e["target"]) for e in self.log(chat) if e["kind"] == "delete"], [("alice", 2)])
        self.bob.send(chat="alice", text="direct")
        theirs = self.alice.send(chat="bob,carol", text="group")["message"]["id"]
        self.carol.delete(id=self.carol.send(chat="#ops", text="own")["message"]["id"])  # the sender still may
        self.alice.delete(id=self.bob.read(chat="alice", all=True)["messages"][0]["id"])  # in any chat she is in
        self.refused(lambda: self.bob.delete(id=theirs), 403, "Only an administrator may delete another person's")

    def test_archiving_closes_a_channel_and_keeps_it(self):
        chat = self.alice.channel_create(name="ops", members="bob", public=True)["chat"]["id"]
        first = self.bob.send(chat="#ops", text="before")["message"]["id"]
        self.server.admin.policy_set(rules={"admins": ["alice"]})
        self.refused(lambda: self.bob.channel_archive(chat="#ops"), 403, "Only a channel manager or an administrator")
        self.assertTrue(self.alice.channel_archive(chat="#ops")["chat"]["archived"])
        self.alice.channel_archive(chat="#ops")  # twice is fine
        for call in (lambda: self.bob.send(chat="#ops", text="after"),
                     lambda: self.bob.send(chat=chat, text="reply", reply_to=first),
                     lambda: self.bob.edit(id=first, text="changed"),
                     lambda: self.bob.react(id=first, emoji="+1"),
                     lambda: self.alice.delete(id=first),
                     lambda: self.alice.channel_add(chat="#ops", users="carol"),
                     lambda: self.carol.channel_join(chat="#ops"),
                     lambda: self.alice.channel_rename(chat="#ops", name="ops2"),
                     lambda: self.alice.channel_describe(chat="#ops", description="closed"),
                     lambda: self.alice.channel_visibility(chat="#ops", visibility="private")):
            self.refused(call, 403, "#ops is archived")
        self.refused(lambda: self.carol.channel_create(name="ops"), 400, "It is archived. Unarchive it")
        self.assertEqual(self.carol.channel_list()["channels"], [])
        self.assertEqual(self.texts(self.bob.read(chat="#ops", all=True)), ["before"])  # members still read
        self.assertTrue(self.bob.chats()["chats"][0]["archived"])
        snapshot = self.server.admin.view_snapshot()["chats"]
        self.assertTrue(next(c for c in snapshot if c["id"] == chat)["archived"])
        self.restart()
        self.assertTrue(self.bob.chat_info(chat="#ops")["chat"]["archived"])
        self.refused(lambda: self.bob.send(chat="#ops", text="after a restart"), 403, "#ops is archived")
        self.refused(lambda: self.bob.channel_unarchive(chat="#ops"), 403, "Only a channel manager")
        self.assertFalse(self.alice.channel_unarchive(chat="#ops")["chat"]["archived"])
        self.bob.send(chat="#ops", text="open again")
        self.assertEqual([e["kind"] for e in self.log(chat) if e["kind"] in ("archived", "unarchived")],
                         ["archived", "unarchived"])
        self.alice.send(chat="bob", text="a direct chat")
        self.refused(lambda: self.alice.channel_archive(chat="bob"), 400, "Only a channel")

    def test_members_leave_an_archived_channel(self):
        self.alice.channel_create(name="ops", members="bob,carol")
        self.bob.channel_archive(chat="#ops")  # without the rule, any member may archive
        self.bob.channel_leave(chat="#ops")
        self.alice.channel_remove(chat="#ops", users="carol")
        self.server.admin.user_remove(id="alice")  # removing an account takes it out of archived channels too
        self.assertEqual(self.server.store.channels["ops"].members, [])


class Hardening(Base):
    """What an audit of hidden-information boundaries asked for."""

    def refused(self, call, status, words):
        with self.assertRaises(ClientError) as e:
            call()
        self.assertEqual(e.exception.status, status)
        self.assertIn(words, str(e.exception))

    def log_bytes(self, chat):
        folder, out = os.path.join(self.data, "chats", chat, "log"), b""
        for name in sorted(os.listdir(folder)):
            with open(os.path.join(folder, name), "rb") as f:
                out += f.read()
        return out

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_data_folder_is_its_owners_alone(self):
        chat = self.alice.channel_create(name="ops", members="bob")["chat"]["id"]
        self.alice.send(chat="#ops", text="hello")
        mode = lambda *parts: os.stat(os.path.join(self.data, *parts)).st_mode & 0o777  # noqa: E731
        self.assertEqual([mode(), mode("chats"), mode("chats", chat), mode("chats", chat, "log"), mode("users")],
                         [0o700] * 5)
        self.assertEqual(mode("chats", chat, "log", "000001.jsonl"), 0o600)
        os.chmod(self.data, 0o755)  # a folder from before: made private on the next start
        self.restart()
        self.assertEqual(mode(), 0o700)

    def test_deleting_a_message_erases_its_words_on_disk(self):
        chat = self.alice.channel_create(name="ops", members="bob")["chat"]["id"]
        keep = self.alice.send(chat="#ops", text="first")["message"]["id"]
        gone = self.bob.send(chat="#ops", text="secret-words-123", reply_to=keep)["message"]["id"]
        self.bob.edit(id=gone, text="edited-secret-456")
        self.alice.send(chat="#ops", text="after", reply_to=gone)
        self.bob.delete(id=gone)
        self.assertNotIn(b"secret", self.log_bytes(chat))
        self.restart()  # every line still reads where the index says
        self.assertEqual(self.texts(self.alice.read(chat="#ops", all=True)), ["first", "", "after"])
        self.assertEqual(len(self.alice.read(thread=keep)["messages"]), 3)
        # A message deleted before deletion erased the words: they go on the next start.
        old = self.bob.send(chat="#ops", text="old-secret-789")["message"]["id"]
        tokens = {name: getattr(self, name).token for name in ("alice", "bob", "carol")}
        self.server.stop()
        seq = len(self.log_bytes(chat).splitlines()) + 1
        with open(os.path.join(self.data, "chats", chat, "log", "000001.jsonl"), "ab") as f:
            f.write(json.dumps({"seq": seq, "kind": "delete", "from": "bob", "at": "2026-10-24T00:00:00.000Z",
                                "target": int(old.split(":")[1])}).encode() + b"\n")
        self.server = Server(self.data)
        for name, token in tokens.items():
            setattr(self, name, self.server.client(token))
        self.assertNotIn(b"old-secret", self.log_bytes(chat))
        self.assertEqual(self.texts(self.alice.read(chat="#ops", all=True))[-1], "")

    def test_a_private_channel_does_not_answer_outsiders(self):
        chat = self.alice.channel_create(name="merger-talks", members="bob")["chat"]["id"]
        self.assertRegex(chat, r"^[0-9a-f]{16}$")
        for call in (lambda: self.carol.channel_join(chat=chat), lambda: self.carol.read(chat=chat),
                     lambda: self.carol.send(chat=chat, text="hi"), lambda: self.carol.read(chat="#merger-talks"),
                     lambda: self.carol.channel_join(chat="merger-talks")):
            with self.assertRaises(ClientError) as e:
                call()
            self.assertEqual(e.exception.status, 404)
            self.assertNotIn("merger" if "merger" not in str(call.__code__.co_consts) else chat, str(e.exception))
        self.refused(lambda: self.carol.channel_create(name="Merger-Talks"), 400, "The name #Merger-Talks is taken.")
        self.refused(lambda: self.carol.channel_create(name="Merger-Talks"), 400, "Pick another name.")
        with self.assertRaises(ClientError) as e:
            self.carol.channel_create(name="merger-talks")
        self.assertNotIn(chat, str(e.exception))
        first = self.alice.send(chat=chat, text="numbers")["message"]["id"]
        self.assertEqual(self.bob.read(id=first)["messages"][0]["text"], "numbers")  # longer ids work everywhere
        self.bob.send(chat=chat, text="reply", reply_to=first)
        self.refused(lambda: self.carol.read(id=first), 404, "No message")

    def test_filters_reach_only_plain_data(self):
        m = {"id": "x:1", "text": "build 412 failing in CI", "from": "maya", "reactions": {"+1": ["a"]}}
        self.assertTrue(matcher("any(w in m.text for w in ['failing'])")(m))  # m reaches generator expressions too
        self.assertTrue(matcher("re.search(r'build \\d+', m.text) and m.sender == 'maya'")(m))
        self.assertEqual([x["id"] for x in sorter("-len((m.reactions or {}).get('+1', []))")([m])], ["x:1"])
        for expr in ("open('/etc/passwd').read()", "__import__('os')", "m.__class__", "(x for x in []).gi_frame",
                     "'{0.__class__}'.format(m)", "getattr(m, 'text')", "type(m)", "re.compile('a')"):
            with self.assertRaises(ClientError, msg=expr):
                matcher(expr)(m)

    def test_a_posting_rule_by_name_keeps_the_channel_name(self):
        chat = self.alice.channel_create(name="records", members="bob")["chat"]["id"]
        self.server.admin.policy_set(rules={"posters": {"#records": ["alice"]}})
        self.refused(lambda: self.bob.send(chat="#records", text="forged"), 403, "Only alice may post")
        self.refused(lambda: self.bob.channel_rename(chat="#records", name="open"), 403, "keeps its name")
        self.alice.channel_rename(chat="#records", name="Records")  # the same name, so the rule holds
        self.refused(lambda: self.bob.send(chat="#Records", text="forged"), 403, "Only alice may post")
        self.server.admin.policy_set(rules={"posters": {chat: ["alice"]}})  # by id, it may be renamed
        self.bob.channel_rename(chat="#Records", name="results")
        self.refused(lambda: self.bob.send(chat="#results", text="forged"), 403, "Only alice may post")

    def test_a_search_or_filter_marks_read_nothing_it_left_out(self):
        self.alice.send(chat="bob", text="important instruction")
        self.alice.send(chat="bob", text="needle")
        self.alice.send(chat="bob", text="after")
        self.assertEqual(self.texts(self.bob.read(search="needle")), ["needle"])
        self.assertEqual(self.texts(self.bob.read(where="m.text == 'after'")), ["after"])
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 3)  # the first was never shown, so nothing moved
        self.assertEqual(self.texts(self.bob.read(limit=1)), ["important instruction"])
        self.assertEqual(self.texts(self.bob.read(search="after")), ["after"])
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 2)  # needle comes first, and was not shown this time
        self.assertEqual(self.texts(self.bob.read()), ["needle", "after"])
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 0)

    def test_members_may_read_only_what_was_sent_while_they_were_in(self):
        self.refused(lambda: self.server.admin.policy_set(rules={"history": "some"}), 400, "while_member")
        self.server.admin.policy_set(rules={"history": "while_member"})
        chat = self.alice.channel_create(name="ops", members="bob")["chat"]["id"]
        before = self.alice.send(chat="#ops", text="before carol")["message"]["id"]
        self.alice.channel_add(chat="#ops", users="carol")
        reply = self.bob.send(chat="#ops", text="with carol", reply_to=before)["message"]["id"]
        self.alice.channel_remove(chat="#ops", users="carol")
        work = tempfile.mkdtemp(prefix="swarmchat-history-")
        try:
            with open(os.path.join(work, "plan.txt"), "w") as f:
                f.write("plan")
            out = self.alice.send(chat="#ops", text="carol is out", attach=[os.path.join(work, "plan.txt")])["message"]
            self.alice.channel_add(chat="#ops", users="carol")
            summary = self.carol.chats(name="ops")["chats"][0]
            self.assertEqual((summary["last"]["text"], summary["unread"]), ("with carol", 1))
            self.assertEqual(self.carol.chat_info(chat="#ops")["chat"]["messages"], 1)
            self.assertEqual(self.texts(self.carol.read(chat="#ops")), ["with carol"])
            seen = self.carol.read(chat="#ops", all=True)["messages"][0]
            self.assertEqual(("reply_to" in seen, "reply_to_from" in seen), (False, False))  # it answers a hidden one
            self.alice.send(chat="#ops", text="carol is back")
            self.assertEqual(self.texts(self.carol.read(chat="#ops", all=True)), ["with carol", "carol is back"])
            self.assertEqual(self.texts(self.carol.read(search="carol is out")), [])
            self.assertEqual(self.texts(self.carol.read(thread=reply)), ["with carol"])  # without its start
            self.assertEqual(self.carol.read(thread=reply)["messages"][0]["depth"], 0)  # nor its level
            for call in (lambda: self.carol.read(id=out["id"]), lambda: self.carol.read(id=before),
                         lambda: self.carol.react(id=out["id"], emoji="+1"),
                         lambda: self.carol.send(chat="#ops", text="re", reply_to=out["id"]),
                         lambda: self.carol.download(out["attachments"][0]["id"], to=os.path.join(work, "in"))):
                self.refused(call, 404, "No ")
            self.alice.send(chat="#ops", text="ping")
            self.assertEqual(self.texts(self.carol.wait(timeout=1)), ["ping"])
            self.assertEqual(len(self.bob.read(chat="#ops", all=True)["messages"]), 5)  # bob was in all along
            self.alice.channel_add(chat="#ops", users="carol")  # an edit adding a mention to a hidden message
            self.alice.edit(id=before, text="before carol, @carol")
            self.assertFalse(self.carol.chats(name="ops")["chats"][0]["mentioned"])
            self.alice.channel_create(name="town", public=True)
            self.alice.send(chat="#town", text="before anyone joined")
            counts = lambda who: {c["name"]: c["messages"] for c in who.channel_list()["channels"]}  # noqa: E731
            self.assertEqual(counts(self.carol), {"town": None})  # joining would show none of them
            self.carol.channel_join(chat="#town")
            self.alice.send(chat="#town", text="after")
            self.assertEqual((counts(self.carol), counts(self.alice)), ({"town": 1}, {"town": 2}))
            self.server.admin.policy_set(rules={})  # by default, members read everything, as in Slack
            self.assertEqual(len(self.carol.read(chat="#ops", all=True)["messages"]), 5)
        finally:
            shutil.rmtree(work)


class Pages(unittest.TestCase):
    """A read gives at most max_page messages. The client asks for the next pages while it needs them."""

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="swarmchat-test-")
        self.server = Server(self.data, max_page=5)
        self.a, self.b = self.server.user("a", "A"), self.server.user("b", "B")
        self.ids = [self.a.send(chat="b", text=f"m{i:02}")["message"]["id"] for i in range(12)]

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.data)

    def texts(self, result):
        return [m["text"] for m in result["messages"]]

    def test_one_reply_holds_a_page_and_a_cursor_to_the_next(self):
        first = self.b.call("read", {"peek": True, "limit": 100})
        self.assertEqual((self.texts(first), first["more"]), ([f"m{i:02}" for i in range(5)], True))
        second = self.b.call("read", {"peek": True, "limit": 100, "cursor": first["cursor"]})
        self.assertEqual(self.texts(second), [f"m{i:02}" for i in range(5, 10)])
        back = self.b.call("read", {"all": True, "peek": True, "limit": 100})
        self.assertEqual(self.texts(back), [f"m{i:02}" for i in range(7, 12)])  # history goes back from the newest
        back = self.b.call("read", {"all": True, "peek": True, "limit": 100, "cursor": back["cursor"]})
        self.assertEqual(self.texts(back), [f"m{i:02}" for i in range(2, 7)])
        self.assertEqual(self.b.call("read", {"peek": True})["unread_left"], 12)

    def test_the_client_puts_pages_together(self):
        every = [f"m{i:02}" for i in range(12)]
        self.assertEqual(self.texts(self.b.read(peek=True, limit=12)), every)
        history = lambda **select: self.texts(self.b.read(peek=True, **select))  # noqa: E731
        self.assertEqual(history(chat="a", all=True, limit=100), every)
        self.assertEqual(history(last=7), every[5:])
        self.assertEqual(history(all=True, where="m.text.endswith('1')"), ["m01", "m11"])
        self.assertEqual(history(all=True, sort="m.text", limit=3), every[:3])
        self.assertEqual(history(all=True, sort="-int(m.text[1:])", limit=2), ["m11", "m10"])
        self.assertEqual(history(chat="a", since=self.ids[2], limit=100), every[3:])
        self.assertEqual(history(chat="a", before=self.ids[9], search="m0", limit=100), every[:9])
        self.assertEqual(self.texts(self.b.read(peek=True, where="m.text > 'm06'")), every[7:])  # every unread one
        top = self.ids[0]
        for i in range(8):
            top = self.a.send(chat="b", text=f"reply {i}", reply_to=top)["message"]["id"]
        thread = self.b.read(thread=self.ids[0])["messages"]
        self.assertEqual(([m["text"] for m in thread], [m["depth"] for m in thread]),
                         (["m00"] + [f"reply {i}" for i in range(8)], list(range(9))))
        r = self.b.read()  # no limit asked for: one page, and the rest stay unread
        self.assertEqual((len(r["messages"]), r["unread_left"]), (5, 15))


class Settings(unittest.TestCase):
    def test_file_then_flags(self):
        data = tempfile.mkdtemp(prefix="swarmchat-test-")
        path = os.path.join(data, "settings.json")
        with open(path, "w") as f:
            json.dump({"data": data, "port": 9000, "max_text": 50, "default_notify": "mentions"}, f)
        s = config.load(path, port=9100, max_wait=None)
        self.assertEqual((s["port"], s["max_text"], s["max_wait"], s["default_notify"]), (9100, 50, 300, "mentions"))
        with open(path, "w") as f:
            json.dump({"data": data, "max_txt": 5}, f)
        with self.assertRaises(SystemExit) as e:
            config.load(path)
        self.assertIn("unknown settings max_txt", str(e.exception))
        for bad in ({"max_wait": 0}, {"default_notify": "loud"}, {"segment": "big"}):
            with self.assertRaises(SystemExit):
                config.load(None, data=data, **bad)
        shutil.rmtree(data)

    def test_limits_apply(self):
        data = tempfile.mkdtemp(prefix="swarmchat-test-")
        server = Server(data, max_text=10, max_wait=0.3, default_notify="mentions")
        try:
            a, b = server.user("a", "A"), server.user("b", "B")
            with self.assertRaises(ClientError) as e:
                a.send(chat="b", text="this is too long")
            self.assertIn("longer than 10", str(e.exception))
            a.send(chat="b", text="hello")
            t = time.monotonic()
            self.assertEqual(b.call("wait", {"timeout": 60})["messages"], [])  # muted to mentions by default
            self.assertLess(time.monotonic() - t, 2)  # and held for at most max_wait
            a.send(chat="b", text="@b here")
            self.assertEqual([m["text"] for m in b.wait(timeout=2)["messages"]], ["@b here"])
        finally:
            server.stop()
            shutil.rmtree(data)


class CommandLine(Base):
    def swarmchat(self, client, *args, stdin=None):
        env = {**os.environ, "SWARMCHAT_SERVER": self.server.url, "SWARMCHAT_TOKEN": client.token, "PYTHONPATH": ROOT,
               "SWARMCHAT_CONFIG_DIR": self.data + "/config", "SWARMCHAT_DATA_DIR": self.data + "/no-default"}
        return subprocess.run([sys.executable, "-m", "swarmchat", *args], env=env, input=stdin, capture_output=True,
                              text=True, timeout=30)

    def test_what_agents_write_cannot_drive_the_terminal(self):
        text = "hi\x1b[31m red\r\u202eevil\x9b]52;c;aGk=\x07 end"
        self.alice.settings_status(text="busy\x1b[2J\nbob: approved")
        self.alice.channel_create(name="ops\x1b[8m", members="bob")
        self.alice.send(chat="#ops\x1b[8m", text=text)
        for args in (("read", "--peek"), ("people",), ("chats",), ("info", "#ops\x1b[8m"), ("read", "--peek", "--json")):
            with self.subTest(args=args):
                r = self.swarmchat(self.bob, *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                for raw in ("\x1b", "\r", "\u202e", "\x9b", "\x07"):
                    self.assertNotIn(raw, r.stdout)
        out = self.swarmchat(self.bob, "read", "--peek").stdout
        self.assertIn("hi\\x1b[31m red\\x0d\\u202eevil", out)
        self.assertIn("busy\\x1b[2J\\nbob: approved", self.swarmchat(self.bob, "people").stdout)  # one line
        sent = json.loads(self.swarmchat(self.bob, "read", "--peek", "--json").stdout)
        self.assertEqual(sent["text"], text)  # JSON escapes them, and keeps the text as it was

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_a_saved_login_is_for_its_owner_alone(self):
        folder = os.path.join(self.data, "config")
        os.makedirs(folder)
        os.chmod(folder, 0o755)
        with open(os.path.join(folder, "token"), "w") as f:
            f.write("old")
        os.chmod(os.path.join(folder, "token"), 0o644)  # copied in from somewhere, readable by all
        elsewhere = os.path.join(self.data, "elsewhere.json")
        with open(elsewhere, "w") as f:
            f.write('{"server": "http://127.0.0.1:9"}')
        os.symlink(elsewhere, os.path.join(folder, "config.json"))
        r = self.swarmchat(self.bob, "login", self.server.url, self.bob.token)
        self.assertEqual(r.returncode, 0, r.stderr)
        mode = lambda path: os.stat(path).st_mode & 0o777  # noqa: E731
        self.assertEqual((mode(folder), mode(os.path.join(folder, "token"))), (0o700, 0o600))
        self.assertFalse(os.path.islink(os.path.join(folder, "config.json")))
        with open(elsewhere) as f:
            self.assertEqual(f.read(), '{"server": "http://127.0.0.1:9"}')  # written over, never through
        with open(os.path.join(folder, "token")) as f:
            self.assertEqual(f.read().strip(), self.bob.token)

    def test_admin_finds_the_server_from_its_folder(self):
        env = {**os.environ, "PYTHONPATH": ROOT, "SWARMCHAT_SERVER": "http://127.0.0.1:9",
               "SWARMCHAT_CONFIG_DIR": self.data + "/config"}
        cmd = [sys.executable, "-m", "swarmchat", "admin", "--data", self.data, "add", "dora", "--name", "Dora"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Added dora", r.stdout)

    def test_admin_and_view_find_the_default_folder(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SWARMCHAT_")}
        env.update(PYTHONPATH=ROOT, SWARMCHAT_CONFIG_DIR=self.data + "/config", SWARMCHAT_DATA_DIR=self.data)
        run = lambda *args: subprocess.run([sys.executable, "-m", "swarmchat", *args], env=env,  # noqa: E731
                                           capture_output=True, text=True, timeout=30)
        r = run("admin", "add", "dora", "--name", "Dora")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Added dora", r.stdout)
        r = run("view", "--no-open")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Viewer: {self.server.url.rstrip('/')}/view#token=", r.stdout)
        env["SWARMCHAT_DATA_DIR"] = self.data + "/missing"
        r = run("view", "--no-open")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No server folder at", r.stderr)

    def test_an_administrator_runs_role_and_channel_commands(self):
        self.server.admin.policy_set(rules={"admins": ["alice"]})
        r = self.swarmchat(self.alice, "admin", "add", "dora")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("Only the super admin may add users.", r.stderr)
        r = self.swarmchat(self.alice, "admin", "role", "channel_creator", "--add", "carol")
        self.assertEqual((r.returncode, r.stdout), (0, "channel_creator: carol\n"), r.stderr)
        self.assertEqual(self.swarmchat(self.alice, "admin", "role", "admin").stdout, "admin: alice\n")
        r = self.swarmchat(self.alice, "admin", "role", "channel_creator", "--everyone")
        self.assertEqual(r.stdout, "channel_creator: everyone\n", r.stderr)
        self.alice.channel_create(name="ops", members="bob")
        r = self.swarmchat(self.bob, "channel", "archive", "ops")
        self.assertEqual(r.returncode, 3, r.stderr)
        r = self.swarmchat(self.alice, "channel", "archive", "ops")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("#ops  [archived]", r.stdout)
        self.assertIn("[archived]", self.swarmchat(self.bob, "chats").stdout)
        self.assertEqual(self.swarmchat(self.alice, "channel", "unarchive", "ops").returncode, 0)
        r = self.swarmchat(self.alice, "channel", "managers", "ops", "--add", "bob")
        self.assertEqual(r.stdout, "Managers of #ops: alice, bob\n", r.stderr)
        self.assertIn("\nManagers: alice, bob\n", self.swarmchat(self.bob, "info", "#ops").stdout)

    def test_a_new_token_replaces_the_saved_login(self):
        r = self.swarmchat(self.bob, "login", self.server.url, self.bob.token)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.swarmchat(self.bob, "settings", "token")
        self.assertEqual(r.stdout, "Your old token no longer works. The new one is saved in your login.\n", r.stderr)
        env = {**os.environ, "PYTHONPATH": ROOT, "SWARMCHAT_CONFIG_DIR": self.data + "/config"}
        env.pop("SWARMCHAT_TOKEN", None)
        env.pop("SWARMCHAT_SERVER", None)
        r = subprocess.run([sys.executable, "-m", "swarmchat", "whoami"], env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bob", r.stdout)
        self.assertEqual(self.swarmchat(self.bob, "whoami").returncode, 3)  # the old token, from the environment

    def test_channel_descriptions(self):
        r = self.swarmchat(self.alice, "channel", "create", "release-2-4", "--public", "--description",
                           "Release 2.4, shipping Friday")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Release 2.4, shipping Friday", self.swarmchat(self.bob, "channel", "list").stdout)
        r = self.swarmchat(self.alice, "channel", "describe", "release-2-4", "Freeze moved to Monday")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("\nFreeze moved to Monday\n", self.swarmchat(self.alice, "info", "#release-2-4").stdout)
        self.assertEqual(self.swarmchat(self.alice, "channel", "describe", "release-2-4", "").returncode, 0)
        self.assertNotIn("Freeze", self.swarmchat(self.alice, "info", "#release-2-4").stdout)

    def test_the_viewer_and_the_help_say_little_without_a_token(self):
        import urllib.request
        from swarmchat import view
        with urllib.request.urlopen(self.server.url + "/view") as r:
            page, policy = r.read().decode(), r.headers["Content-Security-Policy"]
        for word in ('id="app"', "What had", "Board", "agent"):
            self.assertNotIn(word, page)  # a form for the token, and nothing of the viewer
        self.assertIn(view.page()["script"], policy)  # the viewer's own script may run, once fetched
        with self.assertRaises(ClientError) as e:
            self.alice.view_page()
        self.assertEqual(e.exception.status, 401)
        self.assertIn('id="app"', self.server.admin.view_page()["html"])
        self.assertNotIn("agent", self.swarmchat(self.alice, "--help").stdout.lower())

    def test_no_server_is_reported_plainly(self):
        with self.assertRaises(ClientError) as e:
            Client("http://127.0.0.1:9", "x").whoami()
        self.assertIn("Is the server running?", str(e.exception))
        self.assertNotIn("may have been applied", str(e.exception))

    def test_no_token_looks_like_a_flag(self):
        # One in 64 tokens used to start with '-', and then `swarmchat login URL TOKEN` and --token TOKEN refused it.
        tokens = [self.server.admin.user_add(id=f"t{i}")["token"] for i in range(200)]
        tokens += [self.server.admin.token_rotate(id="t0")["token"] for _ in range(50)]
        with open(os.path.join(self.data, "admin.token")) as f:
            tokens.append(f.read().strip())
        self.assertEqual([t for t in tokens if t.startswith("-")], [])
        r = self.swarmchat(self.bob, "login", self.server.url, tokens[-2])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_send_read_and_exit_codes(self):
        r = self.swarmchat(self.alice, "send", "bob,carol", "-", stdin="line one\nline two")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.swarmchat(self.bob, "read").stdout
        self.assertIn("Alice Ames: line one\n    line two", out)
        self.assertEqual(self.swarmchat(self.bob, "send", "nobody", "x").returncode, 2)
        self.assertEqual(self.swarmchat(self.bob, "wait", "--timeout", "0.2").returncode, 4)
        lines = self.swarmchat(self.carol, "read", "--json").stdout.splitlines()
        self.assertEqual(json.loads(lines[0])["text"], "line one\nline two")


if __name__ == "__main__":
    unittest.main()
