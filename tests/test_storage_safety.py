"""I/O recovery, single-writer ownership, and account lifecycle regressions."""
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from test_swarmchat import Base, Server, ROOT
from swarmchat import config
from swarmchat.client import ClientError
from swarmchat.server import make_server, save_read_positions
from swarmchat.store import Chat, Store, Error


class StorageSafety(Base):
    def test_deleted_ids_remain_reserved_and_tokens_invalid_after_restart(self):
        self.alice.send(chat="bob", text="private to the original Bob")
        self.bob.send(chat="alice", text="original authorship")
        self.server.admin.user_remove(id="bob")
        self.server.admin.user_remove(id="carol")  # No conversation history; the tombstone alone must reserve this id.
        for restarted in (False, True):
            if restarted:
                self.restart()
            with self.assertRaises(ClientError) as rejected:
                self.server.admin.user_add(id="bob", name="Replacement")
            self.assertEqual(rejected.exception.status, 400)
            with self.assertRaises(ClientError):
                self.server.admin.user_add(id="carol")
            with self.assertRaises(ClientError) as revoked:
                self.bob.read(all=True)
            self.assertEqual(revoked.exception.status, 401)

    def test_legacy_deleted_ids_in_chat_history_remain_reserved(self):
        self.alice.send(chat="bob", text="legacy private message")
        self.server.admin.user_remove(id="bob")
        os.remove(Path(self.data) / "retired" / "bob.json")  # Simulate the older deletion format.
        self.restart()
        with self.assertRaises(ClientError) as rejected:
            self.server.admin.user_add(id="bob")
        self.assertEqual(rejected.exception.status, 400)

    def test_failed_rotation_and_profile_creation_do_not_mutate_live_users(self):
        old_token = self.bob.token
        with patch("swarmchat.store.write_json", side_effect=OSError(errno.ENOSPC, "injected full disk")):
            with self.assertRaises(ClientError):
                self.server.admin.token_rotate(id="bob")
        self.assertEqual(self.server.client(old_token).whoami()["user"]["id"], "bob")
        with self.assertRaises(ClientError):
            self.server.admin.user_add(id="bad", name="\ud800")
        self.assertNotIn("bad", [u["id"] for u in self.alice.people()["people"]])

    def test_partial_write_rolls_back_before_the_next_send(self):
        message = self.alice.send(chat="bob", text="seed")["message"]
        chat = self.server.store.chats[message["chat"]]
        writer = chat.writer
        original_write = writer.write

        def partial_write(data):
            original_write(data[:10])
            raise OSError(errno.ENOSPC, "injected partial write")

        with patch.object(writer, "write", side_effect=partial_write):
            with self.assertRaises(ClientError):
                self.alice.send(chat="bob", text="partial")
        self.alice.send(chat="bob", text="after recovery")
        self.restart()
        self.assertEqual(self.texts(self.bob.read()), ["seed", "after recovery"])

    def test_failed_rollback_blocks_writes_until_restart(self):
        message = self.alice.send(chat="bob", text="seed")["message"]
        chat = self.server.store.chats[message["chat"]]
        with patch.object(chat.writer, "write", side_effect=OSError(errno.EIO, "write failed")), \
                patch("swarmchat.store.os.ftruncate", side_effect=OSError(errno.EIO, "rollback failed")):
            with self.assertRaises(ClientError):
                self.alice.send(chat="bob", text="failed")
        with self.assertRaises(ClientError) as blocked:
            self.alice.send(chat="bob", text="must restart")
        self.assertEqual(blocked.exception.status, 503)
        self.restart()
        self.alice.send(chat="bob", text="recovered")
        self.assertEqual(self.texts(self.bob.read()), ["seed", "recovered"])

    def test_background_saver_retries_after_transient_failure(self):
        self.alice.send(chat="bob", text="read before error")
        self.bob.read()
        stopped, saved = threading.Event(), threading.Event()
        original = self.server.store.flush
        attempts = []

        def flaky_flush():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(errno.ENOSPC, "transient")
            original()
            saved.set()
            stopped.set()

        self.server.store.settings["save_every"] = 0.01
        with patch.object(self.server.store, "flush", side_effect=flaky_flush), patch("sys.stderr", new=io.StringIO()):
            thread = threading.Thread(target=save_read_positions, args=(self.server.store, stopped), daemon=True)
            thread.start()
            try:
                self.assertTrue(saved.wait(2), "Background persistence did not recover")
            finally:
                stopped.set()
                thread.join(timeout=2)
        self.restart()
        self.assertEqual(self.bob.read()["messages"], [])

    def test_directory_lock_is_released_after_process_is_killed(self):
        with tempfile.TemporaryDirectory(prefix="swarmchat-lock-test-") as root:
            child = subprocess.Popen([sys.executable, "-c",
                                      "import sys, time; from swarmchat.store import Store; "
                                      "s = Store(sys.argv[1]); print('locked', flush=True); time.sleep(30)", root],
                                     cwd=ROOT, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(OSError):
                    Store(root)
            finally:
                child.kill()
                child.wait(timeout=5)
                child.stdout.close()
            reopened = Store(root)
            reopened.close()

    def test_failed_startup_releases_directory_lock(self):
        with tempfile.TemporaryDirectory(prefix="swarmchat-start-test-") as root:
            with self.assertRaises(OSError):
                make_server(config.load(data=root, port=self.server.httpd.server_address[1]))
            reopened = Store(root)
            reopened.close()

    def test_duplicate_log_sequences_are_rejected_without_retaining_lock(self):
        with tempfile.TemporaryDirectory(prefix="swarmchat-corrupt-test-") as root:
            store = Store(root)
            store.add_user("alice")
            store.add_user("bob")
            chat = store.chat("alice", "bob", create=True)
            store.send("alice", chat, "one")
            log = Path(chat.path(1))
            store.close()
            original = log.read_bytes()
            with log.open("ab") as stream:
                stream.write(original.splitlines(keepends=True)[-1])
            with self.assertRaises(Error) as invalid:
                Store(root)
            self.assertIn("expected sequence", str(invalid.exception))
            log.write_bytes(original)
            recovered = Store(root)
            recovered.close()

    def test_a_deletion_cut_off_part_way_is_finished_at_the_next_start(self):
        sent = self.alice.send(chat="bob", text="secret words here")["message"]
        self.alice.edit(id=sent["id"], text="edited secret")
        chat, e = self.server.store.message("alice", sent["id"])

        def crash(chat, writes, fsync):  # the process dies with the new line half written over the old one
            file_no, pos, line = writes[0]
            with open(chat.path(file_no), "r+b") as f:
                f.seek(pos)
                f.write(line.encode()[:12])
            raise RuntimeError("killed")
        with patch.object(Chat, "_overwrite", crash), chat.lock:
            chat.append({"kind": "delete", "from": "alice", "at": "2026-10-24T00:00:00.000Z", "target": e["seq"]}, False)
            self.assertRaises(RuntimeError, chat.redact, e["seq"], False)
        self.assertTrue(os.path.exists(os.path.join(chat.dir, "redact.journal")))
        self.restart()  # a half-written line would stop the start, without the journal
        log = b"".join(path.read_bytes() for path in sorted(Path(chat.dir, "log").iterdir()))
        self.assertNotIn(b"secret", log)
        self.assertFalse(os.path.exists(os.path.join(chat.dir, "redact.journal")))
        self.assertEqual(self.bob.read(chat="alice", all=True)["messages"][0]["deleted"], True)

    def test_rejected_status_does_not_break_everyones_people_directory(self):
        with self.assertRaises(ClientError):
            self.bob.settings_status(text="\ud800")
        try:
            people = self.alice.people()["people"]
        except ClientError as exc:
            self.fail(f"Bob's rejected status broke Alice's directory request: HTTP {exc.status}")
        self.assertEqual(len(people), 3)


    def test_failed_fsync_does_not_hide_next_acknowledged_message_after_restart(self):
        self.alice.send(chat="bob", text="seed")
        self.server.store.fsync = True
        with patch("swarmchat.store.os.fsync", side_effect=OSError(errno.EIO, "injected fsync failure")):
            with self.assertRaises(ClientError) as failure:
                self.alice.send(chat="bob", text="unacknowledged")
        self.assertEqual(failure.exception.status, 500)
        acknowledged = self.alice.send(chat="bob", text="acknowledged after failure")["message"]
        self.assertEqual(self.bob.read(id=acknowledged["id"], peek=True)["messages"][0]["text"],
                         "acknowledged after failure")
        self.restart()
        recovered = self.bob.read(id=acknowledged["id"], peek=True)["messages"][0]["text"]
        self.assertEqual(recovered, "acknowledged after failure")


    def test_failed_state_flush_is_retried_after_io_recovers(self):
        self.alice.send(chat="bob", text="already read")
        self.bob.read()
        with patch("swarmchat.store.write_json", side_effect=OSError(errno.ENOSPC, "injected full disk")):
            with self.assertRaises(OSError):
                self.server.store.flush()
        self.server.store.flush()  # Disk has recovered: dirty state must still be queued.
        self.restart()
        self.assertEqual(self.bob.read()["messages"], [])


    def test_two_servers_cannot_silently_corrupt_shared_directory(self):
        self.alice.send(chat="bob", text="seed")
        try:
            secondary = Server(self.data)
        except (OSError, SystemExit):
            return  # Correct behavior may reject a second writer outright.
        try:
            second_alice = secondary.client(self.alice.token)
            self.alice.send(chat="bob", text="from first server")
            second_alice.send(chat="bob", text="from second server")
        finally:
            secondary.stop()
        self.restart()
        self.assertCountEqual(self.texts(self.bob.read(all=True, peek=True)),
                              ["seed", "from first server", "from second server"])
