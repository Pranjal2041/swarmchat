"""Delivery, cursor, mention, and membership regressions over real HTTP."""
import concurrent.futures
import threading
import time
from unittest.mock import patch

from test_swarmchat import Base
from swarmchat import api
from swarmchat.client import ClientError


class MessageCorrectness(Base):
    def test_legacy_out_of_order_timestamps_do_not_skip_unread_messages(self):
        first = self.alice.channel_create(name="legacy", members="bob")["chat"]["id"]
        second = self.carol.channel_create(name="other", members="bob")["chat"]["id"]
        for chat_id, sender, text, at in ((first, "alice", "late time, early seq", "2026-10-03T18:00:03.000Z"),
                                          (first, "alice", "early time, late seq", "2026-10-03T18:00:01.000Z"),
                                          (second, "carol", "other chat", "2026-10-03T18:00:02.000Z")):
            chat = self.server.store.chats[chat_id]
            with chat.lock:
                chat.append({"kind": "message", "from": sender, "text": text, "at": at}, False)
        self.restart()
        received = self.texts(self.bob.read(limit=2)) + self.texts(self.bob.read(limit=2))
        self.assertCountEqual(received, ["late time, early seq", "early time, late seq", "other chat"])

    def test_edited_mentions_and_removals_survive_restart(self):
        first = self.alice.send(chat="bob", text="plain")["message"]
        second = self.alice.send(chat="bob", text="@bob remove me")["message"]
        self.alice.edit(id=first["id"], text="@bob added")
        self.alice.edit(id=second["id"], text="plain now")
        self.restart()
        self.assertEqual(self.texts(self.bob.read(mentions=True, peek=True)), ["@bob added"])
        self.alice.delete(id=first["id"])
        self.assertEqual(self.bob.read(mentions=True, peek=True)["messages"], [])

    def test_filtered_unread_counts_keep_the_requested_scope(self):
        message = self.alice.send(chat="bob", text="first")["message"]
        self.alice.send(chat="bob", text="second")
        self.carol.send(chat="bob", text="other chat")
        self.assertEqual(self.bob.read(id=message["id"], where="True")["unread_left"], 1)
        self.assertEqual(self.bob.read(chat="alice", where="True")["unread_left"], 0)
        self.assertEqual(self.bob.read(where="True")["unread_left"], 0)

    def test_removed_member_cannot_receive_from_already_open_wait(self):
        chat = self.alice.channel_create(name="private", members="bob")["chat"]["id"]
        entered_wait = threading.Event()
        original_pending = api._pending

        def observed_pending(*args, **kwargs):
            result = original_pending(*args, **kwargs)
            entered_wait.set()
            return result

        with patch.object(api, "_pending", side_effect=observed_pending):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(self.bob.wait, chat=chat, timeout=0.4, peek=True)
                self.assertTrue(entered_wait.wait(2), "Wait never reached the server")
                self.alice.channel_remove(chat=chat, users="bob")
                self.alice.send(chat=chat, text="secret sent AFTER Bob was removed")
                try:
                    result = waiting.result(timeout=3)
                except ClientError as exc:
                    self.assertIn(exc.status, (401, 403, 404))
                else:
                    self.assertEqual(result["messages"], [], "Removed member received a new private message")


    def test_filtered_wait_finds_match_after_first_hundred_messages(self):
        for number in range(100):
            self.alice.send(chat="bob", text=f"noise {number}")
        self.alice.send(chat="bob", text="urgent")
        result = self.bob.wait(timeout=0.3, where="m.text == 'urgent'")
        self.assertEqual(self.texts(result), ["urgent"])


    def test_since_message_id_includes_later_message_in_same_millisecond(self):
        with patch("swarmchat.store.now", return_value="2026-10-03T18:00:00.000Z"):
            first = self.alice.send(chat="bob", text="first")["message"]
            self.alice.send(chat="bob", text="second")
        result = self.bob.read(chat="alice", since=first["id"], peek=True)
        self.assertEqual(self.texts(result), ["second"])


    def test_edit_obeys_configured_message_length_limit(self):
        message = self.alice.send(chat="bob", text="short")["message"]
        with self.assertRaises(ClientError) as caught:
            self.alice.edit(id=message["id"], text="x" * 20001)
        self.assertEqual(caught.exception.status, 400)


    def test_edit_reindexes_mentions(self):
        message = self.alice.send(chat="bob", text="please check")["message"]
        self.alice.edit(id=message["id"], text="@bob please check")
        result = self.bob.read(mentions=True, peek=True)
        self.assertEqual(self.texts(result), ["@bob please check"])


    def test_filtered_read_reports_remaining_unread_after_marking(self):
        self.alice.send(chat="bob", text="one")
        result = self.bob.read(where="True")
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(self.bob.chats()["chats"][0]["unread"], 0)
        self.assertEqual(result["unread_left"], 0)


    def test_concurrent_send_order_cannot_skip_unread_messages(self):
        chat = self.alice.channel_create(name="team", members="bob,carol")["chat"]["id"]
        anchor = self.alice.send(chat=chat, text="anchor")["message"]["id"]
        self.bob.read()
        paused, release = threading.Event(), threading.Event()
        original_message = self.server.store.message

        def pause_before_commit(me, message_id):  # the reply lookup runs before a send takes the chat's lock
            if me == "alice":
                paused.set()
                if not release.wait(3):
                    raise RuntimeError("test scheduling timeout")
            return original_message(me, message_id)

        with patch.object(self.server.store, "message", side_effect=pause_before_commit):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.alice.send, chat=chat, text="started first, committed last", reply_to=anchor)
                try:
                    self.assertTrue(paused.wait(2))
                    time.sleep(0.01)  # Distinct real timestamps; the clock is not mocked.
                    self.carol.send(chat="bob", text="another chat")
                    time.sleep(0.01)
                    self.carol.send(chat=chat, text="committed first")
                finally:
                    release.set()
                first.result(timeout=3)
        self.assertEqual(sum(c["unread"] for c in self.bob.chats()["chats"]), 3)
        delivered = self.texts(self.bob.read(limit=2)) + self.texts(self.bob.read(limit=2))
        self.assertCountEqual(delivered, ["started first, committed last", "another chat", "committed first"])


    def test_contact_policy_also_rejects_new_text_via_edit(self):
        message = self.alice.send(chat="bob", text="before restriction")["message"]
        self.server.admin.policy_set(rules={"contacts": {}})
        with self.assertRaises(ClientError) as denied:
            self.alice.send(chat="bob", text="new communication")
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(ClientError) as edited:
            self.alice.edit(id=message["id"], text="new communication")
        self.assertEqual(edited.exception.status, 403)


    def test_adding_member_to_existing_channel_wakes_wait_for_history(self):
        chat = self.alice.channel_create(name="history")["chat"]["id"]
        self.alice.send(chat=chat, text="waiting for Bob")
        entered = threading.Event()
        original_pending = api._pending

        def observe_pending(*args, **kwargs):
            result = original_pending(*args, **kwargs)
            entered.set()
            return result

        with patch.object(api, "_pending", side_effect=observe_pending):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(self.bob.wait, timeout=0.8, peek=True)
                self.assertTrue(entered.wait(2))
                self.alice.channel_add(chat=chat, users="bob")
                try:
                    result = waiting.result(timeout=0.3)
                except concurrent.futures.TimeoutError:
                    result = waiting.result(timeout=2)
                    self.assertEqual(self.texts(result), ["waiting for Bob"])
                    self.fail("Adding Bob did not wake his wait; it returned only at timeout")
                self.assertEqual(self.texts(result), ["waiting for Bob"])
