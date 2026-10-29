"""Attachments: files and folders sent with a message, stored once by hash, downloaded by members only."""
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from test_swarmchat import Base, Server
from swarmchat import config, files
from swarmchat.client import Client, ClientError
from swarmchat.server import make_server


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree(root):
    """Every file and folder under root, with file bytes: equal trees compare equal."""
    out = {}
    for path, dirs, files in os.walk(root):
        rel = os.path.relpath(path, root)
        out[rel] = None
        for name in files:
            with open(os.path.join(path, name), "rb") as f:
                out[os.path.join(rel, name)] = f.read()
    return out


class Attachments(Base):
    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="swarmchat-files-")
        self.sent = os.path.join(self.work, "sent")
        os.makedirs(os.path.join(self.sent, "project", "src"))
        os.makedirs(os.path.join(self.sent, "project", "empty"))
        self.files = {"photo.png": os.urandom(300_000), "video.mp4": os.urandom(3_500_000),
                      "run.sh": b"#!/bin/sh\necho hello\n"}
        for name, data in self.files.items():
            with open(os.path.join(self.sent, name), "wb") as f:
                f.write(data)
        with open(os.path.join(self.sent, "project", "src", "data.bin"), "wb") as f:
            f.write(os.urandom(70_000))
        with open(os.path.join(self.sent, "project", "notes.txt"), "w") as f:
            f.write("nested\n")

    def tearDown(self):
        shutil.rmtree(self.work)
        super().tearDown()

    def path(self, *parts):
        return os.path.join(self.sent, *parts)

    def blobs(self):
        return sorted(name for _, _, names in os.walk(os.path.join(self.data, "blobs")) for name in names)

    def uploads(self):
        return os.listdir(os.path.join(self.data, "uploads"))

    def test_round_trip_in_every_kind_of_chat(self):
        channel = self.alice.channel_create(name="team", members="bob,carol")["chat"]["id"]
        attach = [self.path(n) for n in self.files] + [self.path("project")]
        for where in ("bob", "bob,carol", channel):
            with self.subTest(where=where):
                m = self.alice.send(chat=where, text="files", attach=attach)["message"]
                self.assertEqual([a["name"] for a in m["attachments"]], [*self.files, "project"])
                got = os.path.join(self.work, "got-" + where.replace(",", "-"))
                for a in m["attachments"]:
                    self.bob.download(a["id"], to=got)
                for name, data in self.files.items():
                    with open(os.path.join(got, name), "rb") as f:
                        self.assertEqual(f.read(), data)
                self.assertEqual(tree(os.path.join(got, "project")), tree(self.path("project")))
        self.assertEqual(len(self.blobs()), 4)  # the same four contents, stored once each

    def test_hashes_never_reach_clients(self):
        self.alice.send(chat="bob", attach=[self.path("run.sh")])
        for result in (self.bob.read(all=True, peek=True), self.alice.chats()):
            self.assertNotIn("sha256", json.dumps(result))

    def test_history_and_downloads_survive_a_restart(self):
        m = self.alice.send(chat="bob", text="before restart", attach=[self.path("photo.png")])["message"]
        self.restart()
        history = self.bob.read(chat="alice", last=1)["messages"][0]
        self.assertEqual(history["attachments"][0]["id"], m["attachments"][0]["id"])
        target = self.bob.download(m["attachments"][0]["id"], to=self.work)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), self.files["photo.png"])

    def test_only_members_can_download(self):
        channel = self.alice.channel_create(name="team", members="bob")["chat"]["id"]
        att = self.alice.send(chat=channel, attach=[self.path("run.sh")])["message"]["attachments"][0]["id"]
        with self.assertRaises(ClientError) as e:
            self.carol.download(att, to=self.work)
        self.assertEqual(e.exception.status, 404)
        self.alice.channel_add(chat=channel, users="carol")  # history comes with joining, files included
        self.carol.download(att, to=os.path.join(self.work, "carol"))
        self.alice.channel_remove(chat=channel, users="bob")
        with self.assertRaises(ClientError) as e:
            self.bob.download(att, to=os.path.join(self.work, "bob"))
        self.assertEqual(e.exception.status, 404)

    def test_deleting_messages_frees_storage_once_nothing_refers_to_it(self):
        first = self.alice.send(chat="bob", attach=[self.path("photo.png")])["message"]
        second = self.alice.send(chat="carol", attach=[self.path("photo.png")])["message"]
        self.assertEqual(len(self.blobs()), 1)
        self.alice.delete(id=first["id"])
        self.assertEqual(len(self.blobs()), 1)  # carol's message still has it
        with self.assertRaises(ClientError):
            self.bob.download(first["attachments"][0]["id"], to=self.work)
        self.assertNotIn("attachments", self.bob.read(chat="alice", last=1)["messages"][0])
        self.alice.delete(id=second["id"])
        self.assertEqual(self.blobs(), [])
        self.restart()
        self.assertEqual(self.blobs(), [])

    def test_limits(self):
        self.server.stop()
        self.server = Server(self.data, max_attachment=1000, max_attachments=2)
        alice = self.server.client(self.alice.token)
        with self.assertRaises(ClientError) as e:
            alice.send(chat="bob", attach=[self.path("photo.png")])
        self.assertEqual(e.exception.status, 413)
        with self.assertRaises(ClientError) as e:
            alice.send(chat="bob", attach=[self.path("run.sh")] * 3)
        self.assertEqual(e.exception.status, 413)
        alice.send(chat="bob", attach=[self.path("run.sh")])
        self.assertEqual((len(self.blobs()), self.uploads()), (1, []))

    def test_a_refused_upload_gets_a_clear_answer(self):
        # The server refuses before reading the bytes (a private channel the sender is not in, which answers as if it
        # did not exist), yet the sender gets the reason, not a reset.
        self.bob.channel_create(name="private")
        with self.assertRaises(ClientError) as e:
            self.raw_send(json.dumps({"chat": "#private", "files": [{"name": "a.bin", "size": 400_000}]}),
                          os.urandom(400_000))
        self.assertEqual(e.exception.status, 404)
        self.assertEqual((self.blobs(), self.uploads()), ([], []))

    def test_an_interrupted_upload_leaves_nothing(self):
        host, port = self.server.httpd.server_address[:2]
        header = (json.dumps({"chat": "bob", "files": [{"name": "big.bin", "size": 5_000_000}]}) + "\n").encode()
        with socket.create_connection((host, port)) as sock:
            sock.sendall(b"POST /v1/send_files HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + self.alice.token.encode()
                         + b"\r\nContent-Length: " + str(len(header) + 5_000_000).encode() + b"\r\n\r\n" + header
                         + os.urandom(2_000_000))
            time.sleep(0.3)
        time.sleep(0.5)
        self.assertEqual((self.blobs(), self.uploads()), ([], []))
        self.assertEqual(self.bob.read()["messages"], [])

    def test_names_cannot_escape(self):
        m = self.raw_send(json.dumps({"chat": "bob", "files": [{"name": "../../etc/evil.sh", "size": 4}]}), b"evil")
        self.assertEqual(m["attachments"][0]["name"], "evil.sh")
        target = self.bob.download(m["attachments"][0]["id"], to=os.path.join(self.work, "inbox"))
        self.assertEqual(target, os.path.join(self.work, "inbox", "evil.sh"))

    def test_a_folder_with_unsafe_paths_is_not_unpacked(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ok.txt", "fine")
            z.writestr("../escaped.txt", "outside")
        data = buf.getvalue()
        m = self.raw_send(json.dumps({"chat": "bob", "files": [{"name": "trap", "size": len(data), "folder": True}]}),
                          data)
        with self.assertRaises(ClientError) as e:
            self.bob.download(m["attachments"][0]["id"], to=os.path.join(self.work, "inbox"))
        self.assertIn("unsafe path", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.work, "escaped.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "inbox", "trap")))

    def test_nothing_is_overwritten_without_force(self):
        att = self.alice.send(chat="bob", attach=[self.path("run.sh")])["message"]["attachments"][0]["id"]
        target = os.path.join(self.work, "run.sh")
        with open(target, "w") as f:
            f.write("mine")
        with self.assertRaises(ClientError):
            self.bob.download(att, to=self.work)
        with open(target) as f:
            self.assertEqual(f.read(), "mine")
        self.bob.download(att, to=self.work, force=True)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), self.files["run.sh"])

    def test_leftovers_from_a_crash_are_cleared_at_start(self):
        self.alice.send(chat="bob", attach=[self.path("run.sh")])
        self.server.stop()
        os.makedirs(os.path.join(self.data, "blobs", "ff"), exist_ok=True)
        open(os.path.join(self.data, "blobs", "ff", "ff" * 32), "w").close()
        open(os.path.join(self.data, "uploads", "half.part"), "w").close()
        self.server = Server(self.data)
        self.assertEqual((len(self.blobs()), self.uploads()), (1, []))

    def test_bad_uploads_are_refused(self):
        for header, body in ((json.dumps({"chat": "bob", "files": [{"name": "a", "size": 10}]}), b"short"),
                             (json.dumps({"chat": "bob", "files": []}), b""),
                             ("not json", b"")):
            with self.subTest(header=header[:30]):
                with self.assertRaises(ClientError) as e:
                    self.raw_send(header, body)
                self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ClientError) as e:
            self.alice.send(chat="bob")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ClientError) as e:
            self.alice.send(chat="bob", attach=[os.path.join(self.work, "missing.txt")])
        self.assertEqual(e.exception.status, 400)

    def test_many_senders_at_once_keep_storage_exact(self):
        import concurrent.futures
        users = [self.server.user(f"u{i}", f"User {i}") for i in range(12)]
        shared = self.path("photo.png")
        own = []
        for i in range(12):
            own.append(os.path.join(self.work, f"own{i}.bin"))
            with open(own[-1], "wb") as f:
                f.write(os.urandom(50_000))

        def go(i):
            u = users[i]
            keep = u.send(chat="bob", attach=[shared, own[i]])["message"]
            gone = u.send(chat="carol", attach=[shared])["message"]
            u.delete(id=gone["id"])
            return keep["attachments"]

        with concurrent.futures.ThreadPoolExecutor(12) as pool:
            kept = list(pool.map(go, range(12)))
        self.assertEqual(len(self.blobs()), 13)  # the shared photo once, plus each sender's own file
        for i, atts in enumerate(kept):
            target = self.bob.download(atts[1]["id"], to=os.path.join(self.work, f"bob{i}"))
            with open(target, "rb") as a, open(own[i], "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.restart()  # counts rebuilt from the logs agree
        self.assertEqual(len(self.blobs()), 13)

    def test_a_large_file_round_trips(self):
        big = os.path.join(self.work, "big.bin")
        with open(big, "wb") as f:
            for _ in range(90):
                f.write(os.urandom(1_000_000))
        att = self.alice.send(chat="bob", attach=[big])["message"]["attachments"][0]
        self.assertEqual(att["size"], 90_000_000)
        self.assertEqual(sha(self.bob.download(att["id"], to=os.path.join(self.work, "in"))), sha(big))

    def test_disk_full_mid_upload_gives_a_clear_error_and_leaves_nothing(self):
        real_open = os.fdopen

        def full_disk(fd, mode="r", *args, **kwargs):
            f = real_open(fd, mode, *args, **kwargs)
            if "w" in mode and "b" in mode:
                written = [0]
                write = f.write

                def limited(data):
                    written[0] += len(data)
                    if written[0] > 100_000:
                        raise OSError(28, "No space left on device")
                    return write(data)
                f.write = limited
            return f

        with patch("swarmchat.files.os.fdopen", side_effect=full_disk):
            with self.assertRaises(ClientError) as e:
                self.alice.send(chat="bob", text="won't fit", attach=[self.path("photo.png")])
        self.assertEqual(e.exception.status, 507)
        self.assertIn("No space left", str(e.exception))
        self.assertEqual((self.blobs(), self.uploads(), self.bob.read()["messages"]), ([], [], []))
        self.alice.send(chat="bob", attach=[self.path("photo.png")])  # and it works again once there is room

    def test_a_disk_floor_and_a_quota_hold_uploads_back_before_they_are_read(self):
        self.alice.channel_create(name="before")
        self.server.store.settings.update(min_free=1 << 62)  # more than any disk has
        with self.assertRaises(ClientError) as e:
            self.alice.send(chat="bob", text="hello")
        self.assertEqual(e.exception.status, 507)
        with self.assertRaises(ClientError) as e:
            self.raw_send(json.dumps({"chat": "bob", "files": [{"name": "a.bin", "size": 400_000}]}), os.urandom(400_000))
        self.assertEqual(e.exception.status, 507)
        self.assertEqual((self.blobs(), self.uploads()), ([], []))
        for call in (lambda: self.alice.channel_create(name="ops"), lambda: self.alice.channel_add(chat="#before", users="bob"),
                     lambda: self.alice.channel_rename(chat="#before", name="after"),
                     lambda: self.alice.channel_describe(chat="#before", description="what it is for"),
                     lambda: self.alice.send(chat="carol", text="a first message")):
            with self.assertRaises(ClientError) as e:
                call()
            self.assertEqual(e.exception.status, 507)
        self.alice.channel_leave(chat="#before")  # leaving is never refused
        self.server.store.settings.update(min_free=1, quota=500_000)
        first = self.alice.send(chat="bob", attach=[self.path("photo.png")])["message"]  # 300,000 bytes
        with self.assertRaises(ClientError) as e:
            self.alice.send(chat="bob", attach=[self.path("photo.png")])
        self.assertEqual(e.exception.status, 413)
        self.assertIn("quota of 500,000 bytes", str(e.exception))
        self.bob.send(chat="alice", attach=[self.path("photo.png")])  # a quota is each user's own
        self.restart()  # counted again from the logs
        self.server.store.settings.update(min_free=1, quota=500_000)
        self.assertRaises(ClientError, self.alice.send, chat="bob", attach=[self.path("photo.png")])
        self.alice.delete(id=first["id"])
        self.alice.send(chat="bob", attach=[self.path("photo.png")])

    def test_links_inside_a_folder(self):
        folder = os.path.join(self.work, "linked")
        os.makedirs(os.path.join(folder, "real"))
        with open(os.path.join(folder, "real", "a.txt"), "w") as f:
            f.write("a")
        os.symlink(os.path.join(folder, "real", "a.txt"), os.path.join(folder, "file-link.txt"))
        os.symlink(os.path.join(folder, "real"), os.path.join(folder, "folder-link"))
        os.symlink(folder, os.path.join(folder, "real", "loop"))  # back up to the top: must not go on forever
        os.symlink(os.path.join(folder, "nowhere"), os.path.join(folder, "broken"))
        att = self.alice.send(chat="bob", attach=[folder])["message"]["attachments"][0]
        got = os.path.join(self.bob.download(att["id"], to=os.path.join(self.work, "in")))
        for rel in ("real/a.txt", "file-link.txt", "folder-link/a.txt"):  # links arrive as what they point to
            with open(os.path.join(got, rel)) as f:
                self.assertEqual(f.read(), "a")
        self.assertFalse(os.path.exists(os.path.join(got, "broken")))
        self.assertFalse(os.path.islink(os.path.join(got, "file-link.txt")))

    def test_a_link_out_of_the_folder_stops_the_send(self):
        folder, secret = os.path.join(self.work, "deliverable"), os.path.join(self.work, "answers.txt")
        os.makedirs(folder)
        with open(secret, "w") as f:
            f.write("the answer key")
        with open(os.path.join(folder, "report.txt"), "w") as f:
            f.write("my report")
        os.symlink(secret, os.path.join(folder, "notes.txt"))
        with self.assertRaises(ClientError) as e:
            self.alice.send(chat="bob", attach=[folder])
        self.assertEqual(e.exception.status, 400)
        self.assertIn("outside the folder", str(e.exception))
        os.makedirs(os.path.join(folder, "more"))
        os.unlink(os.path.join(folder, "notes.txt"))
        os.symlink(self.work, os.path.join(folder, "more", "up"))  # a folder outside it, too
        self.assertRaises(ClientError, self.alice.send, chat="bob", attach=[folder])
        self.assertEqual((self.blobs(), self.uploads(), self.bob.read()["messages"]), ([], [], []))

    @unittest.skipIf(os.name == "nt", "switching a link needs no extra rights on POSIX only")
    def test_a_link_switched_out_of_the_folder_while_it_is_packed_is_caught(self):
        folder, secret = os.path.join(self.work, "deliverable"), os.path.join(self.work, "answers.txt")
        os.makedirs(folder)
        for path, text in ((secret, "the answer key"), (os.path.join(folder, "report.txt"), "my report")):
            with open(path, "w") as f:
                f.write(text)
        link = os.path.join(folder, "notes.txt")
        os.symlink(os.path.join(folder, "report.txt"), link)  # inside the folder, as any check by name would find
        real_open = os.open

        def switching(path, flags, *args, **kwargs):  # the link changes after the folder was listed, before it is read
            if path == link and os.readlink(link) != secret:
                os.unlink(link)
                os.symlink(secret, link)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(files.os, "open", switching), self.assertRaises(ValueError) as e:
            files.pack(folder)
        self.assertIn("outside the folder", str(e.exception))

    def test_a_folder_too_large_for_the_disk_or_with_too_many_entries_is_not_unpacked(self):
        att = self.alice.send(chat="bob", attach=[self.path("project")])["message"]["attachments"][0]
        inbox = os.path.join(self.work, "in")
        for limit, value, words in (("MAX_ENTRIES", 2, "entries, more than 2"), ("SPARE", 1 << 62, "free")):
            with self.subTest(limit=limit), patch.object(files, limit, value), self.assertRaises(ClientError) as e:
                self.bob.download(att["id"], to=inbox)
            self.assertIn(words, str(e.exception))
            self.assertEqual(os.listdir(inbox), [])
        def broken(zip_path, dest):  # the disk fills part way through
            with open(os.path.join(dest, "first.txt"), "w") as f:
                f.write("part of it")
            raise OSError(28, "No space left on device")
        with patch("swarmchat.client.unpack", broken), self.assertRaises(ClientError) as e:
            self.bob.download(att["id"], to=inbox)
        self.assertIn("No space left", str(e.exception))
        self.assertEqual(os.listdir(inbox), [])  # nothing half unpacked is left
        self.assertEqual(tree(self.bob.download(att["id"], to=inbox)), tree(self.path("project")))

    def test_a_file_type_cannot_carry_headers(self):
        m = self.raw_send(json.dumps({"chat": "bob", "files": [
            {"name": "a.txt", "size": 5, "type": "text/plain\r\nContent-Length: 0\r\n\r\nX"}]}), b"hello")
        self.assertEqual(m["attachments"][0]["type"], "text/plain")  # from the name instead
        att = m["attachments"][0]["id"]
        chat = self.server.store.files[att]
        chat.files[att][1]["type"] = "text/plain\r\nX-Injected: 1"  # one stored before types were checked
        conn = http.client.HTTPConnection(*self.server.httpd.server_address[:2], timeout=10)
        conn.request("POST", "/v1/download", json.dumps({"id": att}), {"Authorization": f"Bearer {self.bob.token}"})
        response = conn.getresponse()
        self.assertEqual((response.status, response.getheader("Content-Type"), response.getheader("X-Injected"),
                          response.read()), (200, "application/octet-stream", None, b"hello"))
        conn.close()

    def test_the_same_folder_always_zips_to_the_same_bytes(self):
        first = files.pack(self.path("project"))
        time.sleep(2.1)  # past a zip timestamp step
        second = files.pack(self.path("project"))
        try:
            self.assertEqual(sha(first), sha(second))
        finally:
            os.unlink(first)
            os.unlink(second)

    def test_two_folders_linking_to_each_other_still_finish(self):
        top = os.path.join(self.work, "pair")
        os.makedirs(os.path.join(top, "a"))
        os.makedirs(os.path.join(top, "b"))
        os.symlink(os.path.join(top, "b"), os.path.join(top, "a", "to-b"))
        os.symlink(os.path.join(top, "a"), os.path.join(top, "b", "to-a"))
        with open(os.path.join(top, "a", "x.txt"), "w") as f:
            f.write("x")
        packed = files.pack(top)
        try:
            with zipfile.ZipFile(packed) as z:
                names = z.namelist()
        finally:
            os.unlink(packed)
        self.assertIn("a/x.txt", names)
        self.assertIn("b/to-a/x.txt", names)
        self.assertLess(len(names), 20)

    def test_names_in_any_language(self):
        folder = os.path.join(self.work, "dossier été")
        os.makedirs(os.path.join(folder, "東京"))
        with open(os.path.join(folder, "東京", "résumé.txt"), "w", encoding="utf-8") as f:
            f.write("bonjour")
        single = os.path.join(self.work, "naïve café ☕.txt")
        with open(single, "w", encoding="utf-8") as f:
            f.write("x")
        m = self.alice.send(chat="bob", attach=[single, folder])["message"]
        self.assertEqual([a["name"] for a in m["attachments"]], ["naïve café ☕.txt", "dossier été"])
        inbox = os.path.join(self.work, "in")
        for a in m["attachments"]:
            self.bob.download(a["id"], to=inbox)
        self.assertTrue(os.path.exists(os.path.join(inbox, "naïve café ☕.txt")))
        with open(os.path.join(inbox, "dossier été", "東京", "résumé.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "bonjour")

    @unittest.skipUnless(shutil.which("openssl"), "openssl is needed for a test certificate")
    def test_over_https_with_a_trusted_self_signed_certificate(self):
        cert, key = os.path.join(self.work, "cert.pem"), os.path.join(self.work, "key.pem")
        made = subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                               "-out", cert, "-days", "1", "-subj", "/CN=localhost",
                               "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], capture_output=True, timeout=30)
        if made.returncode:
            self.skipTest(f"this openssl cannot make the certificate: {made.stderr[-200:]}")
        data = os.path.join(self.work, "tls-data")
        httpd, store = make_server(config.load(data=data, port=0, tls_cert=cert, tls_key=key))
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            url = f"https://127.0.0.1:{httpd.server_address[1]}"
            with patch.dict(os.environ, {"SWARMCHAT_CA": cert}):
                with open(os.path.join(data, "admin.token")) as f:
                    admin = Client(url, f.read().strip())
                a = Client(url, admin.user_add(id="a")["token"])
                b = Client(url, admin.user_add(id="b")["token"])
                att = a.send(chat="b", attach=[self.path("video.mp4"), self.path("project")])["message"]["attachments"]
                inbox = os.path.join(self.work, "tls-in")
                for x in att:
                    b.download(x["id"], to=inbox)
                self.assertEqual(sha(os.path.join(inbox, "video.mp4")), sha(self.path("video.mp4")))
                self.assertEqual(tree(os.path.join(inbox, "project")), tree(self.path("project")))
                for c in (admin, a, b):
                    c.close()
            with self.assertRaises(ClientError):  # without trusting the certificate, the client refuses
                Client(url, "x").whoami()
        finally:
            httpd.shutdown()
            httpd.server_close()
            store.close()

    def raw_send(self, header: str, body: bytes) -> dict:
        """An upload written by hand, to send what the client never would."""
        host, port = self.server.httpd.server_address[:2]
        line = (header + "\n").encode()
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("POST", "/v1/send_files", line + body, {"Authorization": f"Bearer {self.alice.token}",
                                                             "Content-Length": str(len(line) + len(body))})
        response = conn.getresponse()
        result = json.loads(response.read() or b"{}")
        conn.close()
        if response.status != 200:
            raise ClientError(response.status, result.get("error", ""))
        return result["message"]


if __name__ == "__main__":
    unittest.main()
