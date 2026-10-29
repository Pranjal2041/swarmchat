"""The real swarmchat command, used the way a person uses it by hand: separate processes, Ctrl-C on a wait, two
terminals waiting at once, a login switched while a wait runs, a mistyped address. The server runs as its own process,
and every test also checks that its log stays free of tracebacks.
"""
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.name == "nt"
# How a person stops a command: Ctrl-C, a normal kill, a hard kill. Windows has only Ctrl-Break and a hard kill, and a
# process can receive Ctrl-Break only in its own process group.
STOPS = ("ctrl-break", "hard kill") if WINDOWS else ("ctrl-c", "kill", "hard kill")
GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}


def stop(process, how):
    if how == "hard kill":
        process.kill()
    elif how == "ctrl-break":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGINT if how == "ctrl-c" else signal.SIGTERM)


class ByHand(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="swarmchat-hand-")
        self.data = os.path.join(self.dir, "data")
        self.log = open(os.path.join(self.dir, "server.log"), "w+")
        self.server = subprocess.Popen([sys.executable, "-m", "swarmchat", "serve", "--data", self.data, "--port", "0"],
                                       env=self.env(), stdout=self.log, stderr=subprocess.STDOUT, **GROUP)
        self.url = ""
        for _ in range(1500):  # up to 30 s: a busy CI machine can be slow to start Python
            try:
                with open(os.path.join(self.data, "address")) as f:
                    self.url = f.read().strip()
            except FileNotFoundError:
                pass
            if self.url:
                break
            time.sleep(0.02)
        if not self.url:
            self.log.seek(0)
            self.fail(f"the server did not start: {self.log.read()}")
        self.tokens = {}
        for user, name in (("alice", "Alice Ames"), ("bob", "Bob Burns")):
            r = self.swarmchat(None, "admin", "--data", self.data, "add", user, "--name", name, "--json")
            self.tokens[user] = json.loads(r.stdout)["token"]

    def tearDown(self):
        stop(self.server, "ctrl-break" if WINDOWS else "kill")
        self.server.wait(10)
        self.log.seek(0)
        log = self.log.read()
        self.log.close()
        shutil.rmtree(self.dir)
        self.assertNotIn("Traceback", log, log)

    def env(self, user=None, config=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("SWARMCHAT_")}
        e.update(PYTHONPATH=ROOT, SWARMCHAT_CONFIG_DIR=os.path.join(self.dir, f"config-{config or user}"))
        if user:
            e.update(SWARMCHAT_SERVER=self.url, SWARMCHAT_TOKEN=self.tokens[user])
        return e

    def swarmchat(self, user, *args, config=None):
        return subprocess.run([sys.executable, "-m", "swarmchat", *args], env=self.env(user, config),
                              capture_output=True, text=True, timeout=30)

    def start(self, user, *args, config=None):
        """A command left running, as in another terminal."""
        return subprocess.Popen([sys.executable, "-m", "swarmchat", *args], env=self.env(user, config),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **GROUP)

    def test_a_stopped_wait_does_not_use_up_the_next_message(self):
        for how in STOPS:
            with self.subTest(how=how):
                waiting = self.start("bob", "wait")
                time.sleep(3)  # the server is now holding bob's wait
                stop(waiting, how)
                waiting.communicate(timeout=10)
                self.swarmchat("alice", "send", "bob", f"sent after {how}")
                time.sleep(0.3)
                self.assertIn(f"sent after {how}", self.swarmchat("bob", "read").stdout)

    def test_a_hung_up_wait_marks_nothing_read_on_the_server(self):
        # A client of the HTTP API that does not peek: the server itself must notice it has gone.
        host, port = self.url.split("//")[1].rsplit(":", 1)
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.request("POST", "/v1/wait", json.dumps({"timeout": 60}),
                     {"Authorization": f"Bearer {self.tokens['bob']}", "Content-Type": "application/json"})
        time.sleep(0.5)
        conn.close()
        self.swarmchat("alice", "send", "bob", "while nobody listened")
        time.sleep(1.5)
        self.assertIn("while nobody listened", self.swarmchat("bob", "read").stdout)

    def test_two_terminals_waiting_both_see_it(self):
        # --peek: neither marks it read, so both see it however their start-up times fall.
        first = self.start("bob", "wait", "--peek", "--timeout", "30")
        second = self.start("bob", "wait", "--peek", "--timeout", "30")
        time.sleep(1)
        self.swarmchat("alice", "send", "bob", "hello both")
        for waiting in (first, second):
            out, err = waiting.communicate(timeout=40)
            self.assertIn("hello both", out, err)
        self.assertIn("hello both", self.swarmchat("bob", "read").stdout)  # still unread until now
        self.assertIn("No new messages", self.swarmchat("bob", "read").stdout)

    def test_switching_login_while_a_wait_runs(self):
        shared = "terminal"
        login = self.swarmchat(None, "login", self.url, self.tokens["bob"], config=shared)
        self.assertEqual(login.returncode, 0, login.stderr)
        waiting = self.start(None, "wait", "--timeout", "30", config=shared)  # runs as bob
        time.sleep(3)
        self.assertIn("Alice Ames", self.swarmchat(None, "login", self.url, self.tokens["alice"], config=shared).stdout)
        self.swarmchat(None, "send", "bob", "to bob, from alice", config=shared)
        out, err = waiting.communicate(timeout=15)
        self.assertIn("to bob, from alice", out, err)

    def test_a_mistyped_address_keeps_the_saved_login(self):
        shared = "terminal"
        self.swarmchat(None, "login", self.url, self.tokens["bob"], config=shared)
        bad = self.swarmchat(None, "login", "http://127.0.0.1:9", self.tokens["bob"], config=shared)  # nothing listens
        self.assertEqual(bad.returncode, 5, bad.stderr)
        self.assertIn("Is the server running?", bad.stderr)
        self.assertIn("Bob Burns", self.swarmchat(None, "whoami", config=shared).stdout)

    def test_mistakes_get_plain_errors(self):
        wrong = subprocess.run([sys.executable, "-m", "swarmchat", "chats"], capture_output=True, text=True, timeout=30,
                               env={**self.env("bob"), "SWARMCHAT_TOKEN": "wrong"})
        self.assertEqual((wrong.returncode, "Unknown token" in wrong.stderr), (3, True))
        nobody = self.swarmchat("bob", "send", "nobody", "hi")
        self.assertEqual((nobody.returncode, "No channel or person" in nobody.stderr), (2, True))
        self.assertEqual(self.swarmchat("bob", "send", "alice", " ").returncode, 1)
        self.assertEqual(self.swarmchat("bob", "channel").returncode, 2)  # no action: usage, not a crash
        self.assertEqual(self.swarmchat("bob", "wait", "--timeout", "0.2").returncode, 4)

    def test_attachments_by_hand(self):
        work = os.path.join(self.dir, "work")
        os.makedirs(os.path.join(work, "project", "empty"))
        with open(os.path.join(work, "photo.png"), "wb") as f:
            f.write(os.urandom(2_000_000))
        with open(os.path.join(work, "project", "run.sh"), "w") as f:
            f.write("#!/bin/sh\necho hi\n")
        sent = self.swarmchat("alice", "send", "bob", "look", "--attach", os.path.join(work, "photo.png"),
                        "--attach", os.path.join(work, "project"))
        self.assertIn("with 2 attachments", sent.stdout, sent.stderr)
        lines = self.swarmchat("bob", "read", "--json").stdout.splitlines()
        self.assertNotIn("sha256", "".join(lines))
        ids = [a["id"] for a in json.loads(lines[0])["attachments"]]
        inbox = os.path.join(self.dir, "inbox")
        for att in ids:
            self.assertIn("Saved", self.swarmchat("bob", "download", att, "--to", inbox).stdout)
        with open(os.path.join(work, "photo.png"), "rb") as a, open(os.path.join(inbox, "photo.png"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertTrue(os.path.isdir(os.path.join(inbox, "project", "empty")))
        again = self.swarmchat("bob", "download", ids[0], "--to", inbox)
        self.assertEqual((again.returncode, "already exists" in again.stderr), (1, True))
        self.assertIn("[file] photo.png", self.swarmchat("bob", "read", "--last", "1", "alice").stdout)

    def test_an_upload_killed_halfway_leaves_nothing(self):
        big = os.path.join(self.dir, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(90_000_000))
        uploads = os.path.join(self.data, "uploads")
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                sending = self.start("alice", "send", "bob", "big one", "--attach", big)
                deadline = time.monotonic() + 10
                while not os.listdir(uploads) and time.monotonic() < deadline:
                    time.sleep(0.001)  # until the server is part way through receiving it
                self.assertTrue(os.listdir(uploads), "the upload never started")
                sending.kill()
                sending.communicate(timeout=10)
                for _ in range(100):
                    if not os.listdir(uploads):
                        break
                    time.sleep(0.05)
                self.assertEqual(os.listdir(uploads), [])
                self.assertEqual(self.swarmchat("bob", "read", "--json").stdout, "")  # no message, not even half of one
                blobs = [n for _, _, names in os.walk(os.path.join(self.data, "blobs")) for n in names]
                self.assertEqual(blobs, [])

    def test_a_killed_download_leaves_no_half_file(self):
        big = os.path.join(self.dir, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(90_000_000))
        sent = self.swarmchat("alice", "send", "bob", "--attach", big, "--json")
        att = json.loads(sent.stdout)["attachments"][0]["id"]
        inbox = os.path.join(self.dir, "inbox")
        os.makedirs(inbox)
        half = lambda: [n for n in os.listdir(inbox) if n.endswith(".part")]
        for how in STOPS:
            with self.subTest(how=how):
                getting = self.start("bob", "download", att, "--to", inbox)
                deadline = time.monotonic() + 10
                while not half() and time.monotonic() < deadline:
                    time.sleep(0.001)  # until it is part way through writing the file
                self.assertTrue(half(), "the download never started")
                stop(getting, how)
                getting.communicate(timeout=10)
                if how != "hard kill":  # a normal stop cleans up on the way out
                    self.assertEqual(half(), [])
                    self.assertFalse(os.path.exists(os.path.join(inbox, "big.bin")))
        self.assertEqual(len(half()), 1)  # what the hard kill left behind
        old = time.time() - 600
        os.utime(os.path.join(inbox, half()[0]), (old, old))  # some minutes later
        self.assertEqual(self.swarmchat("bob", "download", att, "--to", inbox).returncode, 0)
        self.assertEqual(half(), [])  # cleared by the next download into that folder
        with open(big, "rb") as a, open(os.path.join(inbox, "big.bin"), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_flags_work_anywhere_in_a_command(self):
        admin = open(os.path.join(self.data, "admin.token")).read().strip()
        for args in (["admin", "--admin-token", admin, "--server", self.url, "add", "zed1"],  # as the README shows
                     ["admin", "add", "zed2", "--admin-token", admin, "--server", self.url],
                     ["admin", "add", "zed3", "--data", self.data],
                     ["--server", self.url, "admin", "--data", self.data, "add", "zed4"]):
            with self.subTest(args=args):
                r = self.swarmchat(None, *args)
                self.assertEqual(r.returncode, 0, r.stderr)
        for args in (["--json", "channel", "create", "ops1"], ["channel", "--json", "create", "ops2"],
                     ["channel", "create", "ops3", "--json"],
                     ["--server", self.url, "--token", self.tokens["bob"], "chats"]):
            with self.subTest(args=args):
                r = self.swarmchat("bob", *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                if "--json" in args:
                    self.assertIsInstance(json.loads(r.stdout.splitlines()[0]), dict)

    def test_the_message_may_come_after_flags(self):
        # As an agent wrote it. Before Python 3.13, argparse dropped the text after a flag.
        first = json.loads(self.swarmchat("alice", "send", "bob", "first", "--json").stdout)
        note = os.path.join(self.dir, "note.txt")
        with open(note, "w") as f:
            f.write("hi")
        for flags in (["--reply-to", first["id"]], ["--attach", note], ["--json", "--attach", note]):
            with self.subTest(flags=flags):
                r = self.swarmchat("alice", "send", "bob", *flags, f"after {flags[-2]}", "--json")
                self.assertEqual(r.returncode, 0, r.stderr)
                sent = json.loads(r.stdout)
                self.assertEqual(sent["text"], f"after {flags[-2]}")
                self.assertEqual(sent.get("reply_to"), first["id"] if "--reply-to" in flags else None)
                self.assertEqual(len(sent.get("attachments", [])), 1 if "--attach" in flags else 0)

    def test_reading_a_thread(self):
        def send(user, to, text, *more):
            return json.loads(self.swarmchat(user, "send", to, text, *more, "--json").stdout)["id"]
        post = send("alice", "bob", "Q3 numbers?")
        comment = send("bob", "alice", "July is in", "--reply-to", post)
        answer = send("alice", "bob", "thanks", "--reply-to", comment)
        r = self.swarmchat("bob", "read", "--thread", comment)
        self.assertEqual(r.returncode, 0, r.stderr)
        shown = [line for line in r.stdout.splitlines() if " " in line and line.split()[0] in (post, comment, answer)]
        self.assertEqual([line[:line.index(line.split()[0])] for line in shown], ["", "  ", "    "], r.stdout)  # indents
        self.assertNotIn("reply to", r.stdout)  # the indent says it
        lines = self.swarmchat("bob", "read", "--thread", answer, "--json").stdout.splitlines()
        self.assertEqual([(json.loads(line)["id"], json.loads(line)["depth"]) for line in lines],
                         [(post, 0), (comment, 1), (answer, 2)])

    def test_top_posts_and_replies_to_me(self):
        first = json.loads(self.swarmchat("alice", "send", "bob", "first post", "--json").stdout)["id"]
        second = json.loads(self.swarmchat("alice", "send", "bob", "second post", "--json").stdout)["id"]
        self.swarmchat("bob", "react", second, "+1")
        top = self.swarmchat("bob", "read", "alice", "--all", "--sort", "-len((m.reactions or {}).get('+1', []))",
                       "--limit", "1")
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertIn(": second post", top.stdout)
        self.assertNotIn(": first post", top.stdout)
        self.swarmchat("bob", "send", "alice", "noted", "--reply-to", first)
        self.assertIn(f"(reply to alice's {first})", self.swarmchat("alice", "read").stdout)

    def test_a_public_board(self):
        made = self.swarmchat("alice", "channel", "create", "board", "--public")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertIn("#board  [public]", made.stdout)
        self.assertIn("#board  1 member, 0 messages, last ", self.swarmchat("bob", "channel", "list").stdout)
        refused = self.swarmchat("bob", "send", "#board", "hi")
        self.assertEqual((refused.returncode, "you can join it" in refused.stderr), (3, True))
        self.assertEqual(self.swarmchat("bob", "channel", "join", "board").returncode, 0)
        self.assertIn("(joined)", self.swarmchat("bob", "channel", "list").stdout)
        self.assertEqual(self.swarmchat("bob", "send", "#board", "hi").returncode, 0)
        self.assertEqual(self.swarmchat("bob", "channel", "visibility", "board", "private").returncode, 0)
        self.assertEqual(self.swarmchat("alice", "channel", "list").stdout.strip(), "No public channels.")
        self.assertNotIn("[public]", self.swarmchat("alice", "chats").stdout)

    def test_every_command_explains_itself(self):
        for command in ("whoami", "people", "chats", "read", "send", "edit", "delete", "react", "info", "channel",
                        "channel create", "channel list", "channel join", "channel visibility", "channel add",
                        "channel remove", "channel rename", "channel leave", "notifications", "wait", "settings",
                        "settings notify", "settings status", "settings show", "login", "serve", "admin", "admin add",
                        "admin remove", "admin rotate", "admin policy-set", "admin policy-show", "download"):
            with self.subTest(command=command):
                r = self.swarmchat("bob", *command.split(), "--help")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("usage:", r.stdout)

    def test_json_is_one_object_per_line(self):
        self.swarmchat("alice", "send", "bob", "one")
        self.swarmchat("alice", "channel", "create", "ops", "--members", "bob")
        self.swarmchat("alice", "send", "#ops", "two")
        for command in (["chats"], ["read", "--all", "--peek"], ["people"], ["notifications", "--all"], ["whoami"],
                        ["info", "#ops"], ["settings", "show"]):
            with self.subTest(command=command):
                r = self.swarmchat("bob", *command, "--json")
                self.assertEqual(r.returncode, 0, r.stderr)
                lines = r.stdout.splitlines()
                self.assertTrue(lines)
                for line in lines:
                    self.assertIsInstance(json.loads(line), dict)


if __name__ == "__main__":
    unittest.main()
