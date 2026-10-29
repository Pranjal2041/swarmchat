"""The command line. Each command is a client call and a printer. `--json` prints one JSON object per line instead.

Exit codes: 0 done, 1 bad request, 2 not found, 3 not allowed, 4 wait timed out, 5 server unreachable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import signal
import socket
import sys
import threading
from datetime import datetime, timedelta, timezone

from . import DATA_DIR, ENV, NAME, __version__
from .client import Client, ClientError, read_saved_token, save_login

EXIT = {0: 5, 400: 1, 401: 3, 403: 3, 404: 2, 413: 1}
# What makes a terminal do something rather than show something: control characters (escape sequences, carriage
# returns, OSC 52 clipboard writes), C1 controls, and the marks that reverse the direction of text. Shown escaped.
UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
UNSAFE_JSON = re.compile("[\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")  # json.dumps escapes the rest


# ---------------------------------------------------------------------- printing

def clean(text: str, lines: bool = True) -> str:
    """Text from the server, safe to print: what UNSAFE matches is shown as \\x1b or \\u202e, and so are line
    breaks unless `lines`. Agents write these texts, and another agent's terminal must not act on them."""
    text = UNSAFE.sub(lambda m: f"\\x{ord(m[0]):02x}" if ord(m[0]) < 0x100 else f"\\u{ord(m[0]):04x}", text)
    return text if lines else text.replace("\n", "\\n")


def tidy(value, key: str | None = None):
    """A reply made safe to print: every string cleaned, with line breaks kept only in message texts and
    descriptions, which print as lines of their own."""
    if isinstance(value, str):
        return clean(value, key in ("text", "description"))
    if isinstance(value, dict):
        return {clean(k, False) if isinstance(k, str) else k: tidy(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [tidy(v, key) for v in value]
    return value


class Shown:
    """A client whose replies are tidied for printing. The --json output and --where filters see them as sent."""

    def __init__(self, client: Client):
        self.client = client

    def __getattr__(self, name: str):
        value = getattr(self.client, name)
        return (lambda *args, **kwargs: tidy(value(*args, **kwargs))) if callable(value) else value


def say(*parts, lines: bool = True, **kwargs):
    """print(), with every part cleaned: the last guard, for whatever a printer shows."""
    print(*(clean(str(part), lines) for part in parts), **kwargs)


def json_line(item) -> str:
    """One JSON object on one line. json.dumps escapes control characters, but not C1 controls or the marks that
    reverse text: those are escaped too, which leaves the same JSON values."""
    return UNSAFE_JSON.sub(lambda m: f"\\u{ord(m[0]):04x}", json.dumps(item, ensure_ascii=False))


def local(at: str) -> str:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


def when(value: str | None) -> str | None:
    """A time from the command line, sent as UTC: 30m, 2h or 3d ago, a local date or time, or a message id."""
    if not value:
        return value
    ago = re.fullmatch(r"(\d+)([smhd])", value)
    if ago:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[ago[2]]
        moment = datetime.now(timezone.utc) - timedelta(**{unit: int(ago[1])})
    else:
        try:
            moment = datetime.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
        except ValueError:
            return value  # a message id
        if moment.tzinfo is None:
            moment = moment.astimezone()
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def receipt(read_by: list, unread_by: list) -> str:
    """Read receipts on my own message: who has read it."""
    total = len(read_by) + len(unread_by)
    if not read_by:
        return "not read yet"
    if not unread_by:
        return "read" if total == 1 else "read by all"
    names = ", ".join(r["name"] for r in read_by[:3]) + (f" and {len(read_by) - 3} more" if len(read_by) > 3 else "")
    return f"read by {names}, {len(read_by)} of {total}"


def message_line(m: dict) -> str:
    text = "(deleted)" if m.get("deleted") else m.get("text", "")
    answered = f"{m['reply_to_from']}'s {m['reply_to']}" if m.get("reply_to_from") else m.get("reply_to")
    notes = [f"reply to {answered}"] if m.get("reply_to") and "depth" not in m else []  # a thread indents instead
    if m.get("edited_at"):
        notes.append("edited")
    if "read_by" in m:
        notes.append(receipt(m["read_by"], m["unread_by"]))
    reactions = "  ".join(f"{emoji} {', '.join(users)}" for emoji, users in (m.get("reactions") or {}).items())
    head = f"{m['id']}  {local(m['at'])}  {m.get('from_name', m['from'])}: "
    lines = text.split("\n")
    out = head + lines[0] + "".join(f"\n    {line}" for line in lines[1:])
    if notes:
        out += f"  ({', '.join(notes)})"
    for f in m.get("attachments") or []:
        out += f"\n    [{'folder' if f.get('folder') else 'file'}] {f['name']}  {size(f['size'])}  {f['id']}"
    return out + (f"\n    [{reactions}]" if reactions else "")


def size(n: float) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def print_messages(result: dict):
    titles, current = result.get("chats", {}), None
    for m in result["messages"]:
        if m["chat"] != current:
            current = m["chat"]
            say(f"== {titles.get(current, {}).get('title', current)} [{current}]")
        indent = "  " * m.get("depth", 0)  # in a thread, a reply sits under the message it answers
        say("\n".join(indent + line for line in message_line(m).split("\n")))


def chat_line(c: dict) -> str:
    unread = f"{c['unread']} unread" + (", mentions you" if c.get("mentioned") else "") if c["unread"] else "read"
    last = c.get("last")
    preview = f"  {local(last['at'])} {last['from_name']}: {last['text'][:70]}".replace("\n", " ") if last else ""
    muted = "" if c.get("notify", "all") == "all" else f"  [notify {c['notify']}]"
    public = "  [public]" if c.get("public") else ""
    archived = "  [archived]" if c.get("archived") else ""
    return f"{c['id']}  {c['title']}{public}{archived}  ({unread}){muted}{preview}"


def emit(args, items: list, printer):
    if args.json:
        for item in items:
            print(json_line(item))
    else:
        printer(items)


# ---------------------------------------------------------------------- commands

def cmd_whoami(a, cl):
    u = cl.whoami()["user"]
    emit(a, [u], lambda items: say(f"{u['id']}  {u['name']}  server {cl.server}"))


def cmd_people(a, cl):
    def show(people):
        for u in people:
            fields = "  ".join(f"{k}={v}" for k, v in u["fields"].items())
            say(f"{u['id']}  {u['name']}" + (f"  {fields}" if fields else "") + (f"  ({u['status']})" if u["status"] else ""))
    emit(a, cl.people(search=a.search)["people"], show)


def cmd_chats(a, cl):
    def show(chats):
        for c in chats:
            say(chat_line(c))
        if not chats:
            say("No chats.")
    emit(a, cl.chats(where=a.where, limit=a.limit, unread=a.unread, name=a.name, sort=a.sort,
                     **{"with": a.with_})["chats"], show)


def cmd_read(a, cl):
    r = cl.read(where=a.where, sort=a.sort, peek=a.peek, limit=a.limit, chat=a.chat, id=a.id, thread=a.thread,
                last=a.last, all=a.all, since=when(a.since), before=when(a.before), search=a.search,
                mentions=a.mentions, **{"from": a.from_})
    if a.json:
        return emit(a, r["messages"], None)
    if r.get("fallback"):
        say("(nothing unread here, showing the latest messages)")
    print_messages(r)
    if not r["messages"] and not r.get("fallback"):
        say("No new messages." if not (a.last or a.all or a.since or a.before or a.search) else "No messages match.")
    if r["unread_left"]:
        say(f"{r['unread_left']} more unread. Run `{NAME} read` again to see them.")


def cmd_send(a, cl):
    text = sys.stdin.read() if a.text == "-" else a.text
    r = cl.send(chat=a.chat, text=text, attach=a.attach or (), reply_to=a.reply_to)
    files = r["message"].get("attachments") or []
    emit(a, [r["message"]], lambda _: say(f"Sent {r['message']['id']} to {r['chat']['title']} [{r['chat']['id']}]"
                                            + (f" with {len(files)} attachment{'s' * (len(files) != 1)}: "
                                               + ", ".join(f"{f['name']} {f['id']}" for f in files) if files else "")
                                            + "."))


def cmd_download(a, cl):
    path = cl.download(a.attachment, to=a.to, force=a.force)
    emit(a, [{"id": a.attachment, "path": path}], lambda _: say(f"Saved {path}"))


def cmd_edit(a, cl):
    emit(a, [cl.edit(id=a.message, text=a.text)["message"]], lambda ms: say(message_line(ms[0])))


def cmd_delete(a, cl):
    emit(a, [cl.delete(id=a.message)["message"]], lambda ms: say(f"Deleted {ms[0]['id']}."))


def cmd_react(a, cl):
    emit(a, [cl.react(id=a.message, emoji=a.emoji, remove=a.remove)["message"]], lambda ms: say(message_line(ms[0])))


def cmd_info(a, cl):
    c = cl.chat_info(chat=a.chat)["chat"]
    members = "\n".join(f"  {u['id']}  {u['name']}" + (f"  ({u['status']})" if u["status"] else "") for u in c["members"])
    about = f"{c['description']}\n" if c.get("description") else ""
    about += f"Managers: {', '.join(c['managers'])}\n" if c.get("managers") else ""
    emit(a, [c], lambda _: say(f"{chat_line(c)}\n{about}{c['messages']} message{'s' * (c['messages'] != 1)}. Created "
                                 f"by {c['created_by']} on {local(c['created_at'])}. Members:\n{members}"))


def cmd_channel(a, cl):
    if a.action == "list":
        def show(channels):
            for c in channels:
                count = "" if c["messages"] is None else f"{c['messages']} message{'s' * (c['messages'] != 1)}, "
                about = c.get("description", "").replace("\n", "\n    ")
                say(f"{c['id']}  {c['title']}  {c['members']} member{'s' * (c['members'] != 1)}, {count}"
                    f"last {local(c['last_at'])}" + ("  (joined)" if c["joined"] else "") + (f"\n    {about}" if about else ""))
            if not channels:
                say("No public channels.")
        return emit(a, cl.channel_list(search=a.search, limit=a.limit)["channels"], show)
    if a.action == "create":
        r = cl.channel_create(name=a.name, members=a.members, public=a.public, description=a.description)
    elif a.action == "join":
        r = cl.channel_join(chat=a.channel)
    elif a.action == "visibility":
        r = cl.channel_visibility(chat=a.channel, visibility=a.visibility)
    elif a.action in ("add", "remove"):
        r = getattr(cl, f"channel_{a.action}")(chat=a.channel, users=a.users)
    elif a.action == "rename":
        r = cl.channel_rename(chat=a.channel, name=a.name)
    elif a.action == "describe":
        r = cl.channel_describe(chat=a.channel, description=a.description)
    elif a.action == "managers":
        r = cl.channel_managers(chat=a.channel, add=a.add, remove=a.remove)
        return emit(a, [r["chat"]], lambda _: say(f"Managers of {r['chat']['title']}: "
                                                      f"{', '.join(r['chat']['managers']) or 'none'}"))
    elif a.action in ("archive", "unarchive"):
        r = getattr(cl, f"channel_{a.action}")(chat=a.channel)
    else:
        r = cl.channel_leave(chat=a.channel)
        return emit(a, [r["chat"]], lambda _: say(f"You left {a.channel}."))
    emit(a, [r["chat"]], lambda chats: say(chat_line(chats[0])))


def cmd_notifications(a, cl):
    r = cl.notifications(all=a.all, limit=a.limit)
    if a.json:
        return emit(a, r["chats"], None)
    if not r["chats"]:
        return say("Nothing new.")
    for c in r["chats"]:
        say(chat_line(c))
    mentions = {0: "", 1: ", 1 chat mentions you"}.get(r["mentions"], f", {r['mentions']} chats mention you")
    say(f"{r['unread']} unread{mentions}.")


def cmd_wait(a, cl):
    r = cl.wait(timeout=a.timeout, where=a.where, peek=a.peek, chat=a.chat, mentions=a.mentions, any=a.any,
                **{"from": a.from_})
    if not r["messages"]:
        if not a.json:
            say("No new messages before the timeout.")
        return 4
    if a.json:
        return emit(a, r["messages"], None)
    print_messages(r)


def cmd_settings(a, cl):
    if a.action == "notify":
        r = cl.settings_notify(chat=a.chat, level=a.level)
    elif a.action == "status":
        r = cl.settings_status(text=a.text)
    elif a.action == "token":
        r = cl.settings_token()
        saved = cl.token == read_saved_token()
        if saved:
            save_login(cl.server, r["token"])
        return emit(a, [r], lambda _: say("Your old token no longer works. " + (
            "The new one is saved in your login." if saved else f"Your new token: {r['token']}")))
    else:
        r = cl.settings_show()
    emit(a, [r], lambda _: say(f"{r['user']['id']}  {r['user']['name']}  status: {r['user']['status'] or '-'}\n"
                                 + ("notify: " + ", ".join(f"{k}={v}" for k, v in r["notify"].items())
                                    if r["notify"] else "notify: all for every chat")))


def cmd_login(a, cl):
    check = Client(a.server_url, a.token_value)
    cmd_whoami(a, check if a.json else Shown(check))  # saved only once the server answers to this token
    save_login(a.server_url, a.token_value)


def cmd_serve(a, cl):
    from . import config
    from .server import serve
    serve(config.load(a.config, **{k: getattr(a, k) for k in config.DEFAULTS}))


def cmd_view(a, cl):
    """Opens the viewer in a browser: on the server running on a data folder, or on --server with a view token.
    When nothing is running on the folder, it shows the folder itself, read-only, until Ctrl-C."""
    token = a.view_token or os.environ.get(f"{ENV}_VIEW_TOKEN") or a.admin_token or os.environ.get(f"{ENV}_ADMIN_TOKEN")
    server = a.server
    data = a.data or (DATA_DIR if not token and not server else None)  # with nothing given, the default folder
    if data:
        if not a.data and not os.path.isdir(data):
            raise ClientError(400, f"No server folder at {data}. Give the server's folder with --data DIR, or a view "
                                   f"token with --view-token or ${ENV}_VIEW_TOKEN. The server writes it to DIR/view.token.")
        if not token and os.path.exists(os.path.join(data, "view.token")):
            with open(os.path.join(data, "view.token")) as f:
                token = f.read().strip()
        if not server and os.path.exists(os.path.join(data, "address")):
            with open(os.path.join(data, "address")) as f:
                server = f.read().strip()
    elif not token:
        raise ClientError(400, f"Give the server's folder with --data DIR, or a view token with --view-token or "
                               f"${ENV}_VIEW_TOKEN. The server writes it to DIR/view.token.")
    live = None
    if token and (server or not data):
        check = Client(server, token)
        try:
            check.view_info()
            live = check.server
        except ClientError as e:
            if not data:
                raise
            if e.status not in (0, 401, 404):
                raise
        finally:
            check.close()
    if live:
        return show_viewer(a, f"{live.rstrip('/')}/view#token={token}")
    from . import config
    from .server import make_server
    httpd, store = make_server(config.load(data=data, host=a.host, port=a.port if a.port is not None else 0),
                               read_only=True)
    try:
        say(f"No server is running on {data}, so it is shown as it is on disk, read-only. Ctrl-C ends it.")
        show_viewer(a, f"{httpd.url}/view#token={store.view_token}")
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


def show_viewer(a, url: str):
    say(f"Viewer: {url}", flush=True)
    # Only where a browser can open: on a server without a screen, Python would start a text browser in the terminal.
    if not a.no_open and (sys.platform in ("darwin", "win32") or os.environ.get("DISPLAY") or
                          os.environ.get("WAYLAND_DISPLAY")):
        import webbrowser
        webbrowser.open(url)


def cmd_admin(a, cl):
    """With the admin token, as the super admin, or with your own token (--token, then the login) for what the
    rules allow you: see policy.py."""
    token = a.admin_token or a.token or os.environ.get(f"{ENV}_ADMIN_TOKEN")
    server = a.server
    data = a.data
    # With no token or server given at all, the default folder of a server on this machine, if there is one.
    if not (data or token or server or os.environ.get(f"{ENV}_TOKEN") or os.environ.get(f"{ENV}_SERVER")) and \
            os.path.exists(os.path.join(DATA_DIR, "admin.token")):
        data = DATA_DIR
    if data:  # the server's own folder: its admin token, and the address it last started on
        if not token:
            with open(os.path.join(data, "admin.token")) as f:
                token = f.read().strip()
        if not server and os.path.exists(os.path.join(data, "address")):
            with open(os.path.join(data, "address")) as f:
                server = f.read().strip()
    admin = Client(server, token) if a.json else Shown(Client(server, token))
    if a.action == "add":
        fields = dict(f.split("=", 1) for f in a.field or [])
        r = admin.user_add(id=a.id, name=a.name, fields=fields)
        return emit(a, [r], lambda _: say(f"Added {r['user']['id']} ({r['user']['name']}). Token: {r['token']}"))
    if a.action == "remove":
        admin.user_remove(id=a.id)
        return emit(a, [{"removed": a.id}], lambda _: say(f"Removed {a.id}."))
    if a.action == "rotate":
        r = admin.token_rotate(id=a.id)
        return emit(a, [r], lambda _: say(f"New token for {a.id}: {r['token']}"))
    if a.action == "role":
        r = admin.role_change(role=a.role, add=a.add, remove=a.remove, everyone=a.everyone)
        return emit(a, [r], lambda _: say(f"{r['role']}: " + ("everyone" if r["users"] is None
                                                                  else ", ".join(r["users"]) or "no one")))
    if a.action == "policy-set":
        with open(a.file) as f:
            r = admin.policy_set(rules=json.load(f))
    else:
        r = admin.policy_show()
    emit(a, [r], lambda _: say(json.dumps(r["rules"], indent=1)))


# ---------------------------------------------------------------------- arguments

class CommandParser(argparse.ArgumentParser):
    """A command's parser, which takes its words and flags in any order. Before Python 3.13 argparse dropped an
    optional word written after a flag: `swarmchat send bob --reply-to ID "text"` lost the text."""
    mixing = False

    def parse_known_args(self, args=None, namespace=None):
        if self._subparsers is not None or self.mixing:
            return super().parse_known_args(args, namespace)
        self.mixing = True  # before 3.13, parse_known_intermixed_args() calls back into this method
        try:
            return self.parse_known_intermixed_args(args, namespace)
        finally:
            self.mixing = False


def parser() -> argparse.ArgumentParser:
    # Flags that work anywhere in a command: `swarmchat --server URL chats` and `swarmchat chats --server URL` alike.
    # Without a default (SUPPRESS), a later parser never overwrites a value an earlier one already read. main() fills
    # the rest.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON object per line")
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help=f"server URL (default: ${ENV}_SERVER, then the login config)")
    common.add_argument("--token", default=argparse.SUPPRESS,
                        help=f"your token (default: ${ENV}_TOKEN, then the login config)")
    admin_common = argparse.ArgumentParser(add_help=False, parents=[common])
    admin_common.add_argument("--admin-token", default=argparse.SUPPRESS,
                              help=f"default: ${ENV}_ADMIN_TOKEN, or DATA/admin.token with --data, or the default "
                                   f"folder's ({DATA_DIR}). With none of them, your own token, for what the rules "
                                   "allow you")
    admin_common.add_argument("--data", default=argparse.SUPPRESS,
                              help="the server's folder: its admin token and address are read from there")
    p = argparse.ArgumentParser(prog=NAME, parents=[common], description="Chats: direct chats with people, and named "
                                                                         "channels. List, read, send, wait.")
    p.add_argument("--version", action="version", version=f"{NAME} {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command", parser_class=CommandParser)

    def command(name, func, help, parents=(common,)):
        c = sub.add_parser(name, parents=list(parents), help=help, description=help)
        c.set_defaults(func=func)
        return c

    def where(c, what="message `m`"):
        c.add_argument("--where", metavar="EXPR", help=f"keep only items where this Python expression over the {what} "
                                                       "is true, e.g. \"'build' in m.text and m.sender == 'maya'\"")

    command("whoami", cmd_whoami, "show who you are")
    c = command("people", cmd_people, "list everyone you can message")
    c.add_argument("--search", help="only people whose id, name, status or profile contains this")

    c = command("chats", cmd_chats, "list your chats, most recent first")
    c.add_argument("--unread", action="store_true", help="only chats with unread messages")
    c.add_argument("--with", dest="with_", metavar="PEOPLE", help="only chats that include these people (comma-separated)")
    c.add_argument("--name", help="only chats whose title contains this")
    c.add_argument("--sort", choices=["recent", "unread", "name"], default="recent")
    c.add_argument("--limit", type=int, default=20)
    where(c, "chat `c` (c.title, c.unread, c.members ...)")

    c = command("read", cmd_read, "read messages, your unread ones by default, oldest first, and mark them read")
    c.add_argument("chat", nargs="?", help="a chat id, a chat name, or people's ids (comma-separated)")
    c.add_argument("--id", metavar="MESSAGE", help="one message, by id")
    c.add_argument("--thread", metavar="MESSAGE", help="the whole thread this message is in: its first message and "
                                                       "every reply below it, indented. Marks nothing read")
    c.add_argument("--last", type=int, metavar="N", help="the last N messages, read or not")
    c.add_argument("--all", action="store_true", help="read history, not only unread messages")
    c.add_argument("--since", metavar="WHEN", help="after a time (30m, 2h, 3d, 2026-10-02 15:00) or a message id")
    c.add_argument("--before", metavar="WHEN", help="before a time or a message id")
    c.add_argument("--from", dest="from_", metavar="PEOPLE", help="only from these people (comma-separated)")
    c.add_argument("--mentions", action="store_true", help="only messages that mention you")
    c.add_argument("--search", metavar="TEXT", help="only messages containing this text, any case")
    c.add_argument("--limit", type=int, metavar="N", help="at most N messages (default 50 unread, 20 history)")
    c.add_argument("--peek", action="store_true", help="do not mark anything read")
    where(c)
    c.add_argument("--sort", metavar="EXPR", help="order by this Python expression over the message `m`, smallest "
                                                  "first, then keep --limit from the top. Negate a number for largest "
                                                  "first: \"-len((m.reactions or {}).get('+1', []))\" puts the most "
                                                  "voted first")

    c = command("send", cmd_send, "send a message to a channel, or to people (their direct chat is made on first use)")
    c.add_argument("chat", help="a channel (#release-review, quoted in a shell), a person (maya), people (maya,priya) "
                                "or a chat id")
    c.add_argument("text", nargs="?", default="", help="the message, or - to read it from stdin (optional with --attach)")
    c.add_argument("--attach", action="append", metavar="PATH", help="a file or folder to send with it (repeat for more)")
    c.add_argument("--reply-to", metavar="MESSAGE")

    c = command("download", cmd_download, "save an attachment (a folder is unpacked)")
    c.add_argument("attachment", help="the attachment id shown under its message")
    c.add_argument("--to", default=".", metavar="FOLDER", help="where to save it (default: here)")
    c.add_argument("--force", action="store_true", help="replace a file or folder of the same name")

    c = command("edit", cmd_edit, "replace the text of a message you sent")
    c.add_argument("message")
    c.add_argument("text")
    c = command("delete", cmd_delete, "delete a message you sent, or anyone's if you are an administrator")
    c.add_argument("message")
    c = command("react", cmd_react, "add a reaction to a message")
    c.add_argument("message")
    c.add_argument("emoji")
    c.add_argument("--remove", action="store_true", help="take your reaction back")

    c = command("info", cmd_info, "members and details of a chat")
    c.add_argument("chat")

    c = command("channel", cmd_channel, "create, find and join channels, change their members, managers, name, "
                                        "description or visibility, and archive them")
    actions = c.add_subparsers(dest="action", metavar="action", required=True)
    a = actions.add_parser("create", parents=[common], help="create a channel, private unless --public")
    a.add_argument("name")
    a.add_argument("--members", help="people's ids to add, comma-separated")
    a.add_argument("--public", action="store_true", help="anyone may find it (channel list) and join it")
    a.add_argument("--description", metavar="TEXT", help="what the channel is for, up to 1,000 characters")
    a = actions.add_parser("list", parents=[common], help="public channels, which anyone may join")
    a.add_argument("--search", metavar="TEXT", help="only channels whose name contains this")
    a.add_argument("--limit", type=int, default=50)
    a = actions.add_parser("join", parents=[common], help="join a public channel")
    a.add_argument("channel")
    a = actions.add_parser("visibility", parents=[common], help="make a channel public or private (members stay)")
    a.add_argument("channel")
    a.add_argument("visibility", choices=["public", "private"])
    for action in ("add", "remove"):
        a = actions.add_parser(action, parents=[common], help=f"{action} people")
        a.add_argument("channel")
        a.add_argument("users", nargs="+")
    a = actions.add_parser("rename", parents=[common], help="rename a channel")
    a.add_argument("channel")
    a.add_argument("name")
    a = actions.add_parser("describe", parents=[common], help="change a channel's description")
    a.add_argument("channel")
    a.add_argument("description", help='the new description, or "" to remove it')
    a = actions.add_parser("managers", parents=[common], help="show a channel's managers, or name and remove them")
    a.add_argument("channel")
    a.add_argument("--add", nargs="+", metavar="USER", help="members to make managers")
    a.add_argument("--remove", nargs="+", metavar="USER", help="managers to make ordinary members")
    a = actions.add_parser("leave", parents=[common], help="leave a channel")
    a.add_argument("channel")
    a = actions.add_parser("archive", parents=[common], help="archive a channel: kept to read, closed to changes")
    a.add_argument("channel")
    a = actions.add_parser("unarchive", parents=[common], help="open an archived channel again")
    a.add_argument("channel")

    c = command("notifications", cmd_notifications, "chats that need you, following your notification settings")
    c.add_argument("--all", action="store_true", help="every chat, even ones with nothing new")
    c.add_argument("--limit", type=int, default=20)

    c = command("wait", cmd_wait, "block until a new message arrives, print it and mark it read")
    c.add_argument("--chat", help="only this chat")
    c.add_argument("--from", dest="from_", metavar="PEOPLE", help="only from these people")
    c.add_argument("--mentions", action="store_true", help="only messages that mention you")
    c.add_argument("--any", action="store_true", help="ignore your notification settings")
    c.add_argument("--timeout", type=float, metavar="SECONDS", help="give up after this long (exit code 4)")
    c.add_argument("--peek", action="store_true", help="do not mark what it prints as read")
    where(c)

    c = command("settings", cmd_settings, "notification levels and your status")
    actions = c.add_subparsers(dest="action", metavar="action", required=True)
    a = actions.add_parser("notify", parents=[common], help="all, mentions or none for a chat")
    a.add_argument("chat")
    a.add_argument("level", choices=["all", "mentions", "none"])
    a = actions.add_parser("status", parents=[common], help="set your status line")
    a.add_argument("text")
    actions.add_parser("token", parents=[common], help="give yourself a new token, when the old one may have leaked")
    actions.add_parser("show", parents=[common], help="show your settings")

    c = command("login", cmd_login, "save the server address and your token for later commands")
    c.add_argument("server_url")
    c.add_argument("token_value")

    c = command("serve", cmd_serve, "run the server")
    c.add_argument("--config", metavar="FILE", help="a JSON file of settings. Flags override it.")
    c.add_argument("--data", help=f"the folder that holds everything (default {DATA_DIR})")
    c.add_argument("--host", help="default 127.0.0.1. Use 0.0.0.0 to accept other machines.")
    c.add_argument("--port", type=int, help="default 8484")
    c.add_argument("--tls-cert", help="serve HTTPS with this certificate")
    c.add_argument("--tls-key")
    c.add_argument("--fsync", action="store_const", const=True, help="force each write to disk: survives a power cut, slower")
    c.add_argument("--verbose", action="store_const", const=True, help="log every request")
    c.add_argument("--max-text", type=int, metavar="CHARS", help="longest message (default 20000)")
    c.add_argument("--max-wait", type=float, metavar="SECONDS", help="longest one wait request is held (default 300)")
    c.add_argument("--max-body", type=int, metavar="BYTES", help="largest request (default 1048576)")
    c.add_argument("--max-attachment", type=int, metavar="BYTES", help="largest attached file or folder (default 100000000)")
    c.add_argument("--max-attachments", type=int, metavar="N", help="most files on one message (default 20)")
    c.add_argument("--request-timeout", type=float, metavar="SECONDS",
                   help="idle socket and TLS handshake timeout, and the time a request's headers have (default 30)")
    c.add_argument("--min-rate", type=float, metavar="BYTES", help="slowest a request's body may arrive, in bytes a "
                                                                  "second (default 16384)")
    c.add_argument("--max-connections", type=int, metavar="N", help="most connections open at once (default 4096)")
    c.add_argument("--max-requests", type=int, metavar="N", help="most requests one user may have under way at once, "
                                                                  "waits and uploads included (default 32)")
    c.add_argument("--max-page", type=int, metavar="N", help="most messages one read returns (default 1000)")
    c.add_argument("--min-free", type=int, metavar="BYTES", help="disk the server keeps free, refusing messages and "
                                                                "files that would use it (default 1000000000, 0 off)")
    c.add_argument("--quota", type=int, metavar="BYTES", help="most bytes of files one user may have stored (default 0, "
                                                             "no limit)")
    c.add_argument("--default-notify", choices=["all", "mentions", "none"], help="alert level for chats nobody set (default all)")
    c.add_argument("--save-every", type=float, metavar="SECONDS", help="how often read positions are saved (default 1)")
    c.add_argument("--segment", type=int, metavar="LINES", help="lines per log file (default 10000), fixed once data exists")

    c = command("view", cmd_view, "watch every chat in a browser: live, as any user, as chats, boards or a map",
                parents=())
    c.add_argument("--data", default=argparse.SUPPRESS,
                   help=f"the server's folder (default {DATA_DIR}): the viewer's token and the server's address are "
                        "read from there")
    c.add_argument("--server", default=argparse.SUPPRESS,
                   help="the server's URL, from another machine (with --view-token)")
    c.add_argument("--view-token", default=None, help=f"default: ${ENV}_VIEW_TOKEN, or DATA/view.token with --data")
    c.add_argument("--admin-token", default=argparse.SUPPRESS, help="the admin token also opens the viewer")
    c.add_argument("--host", default="127.0.0.1", help="with no server running: where to serve the folder")
    c.add_argument("--port", type=int, default=None, help="with no server running: the port (default: any free one)")
    c.add_argument("--no-open", action="store_true", help="print the link only, do not open a browser")

    c = command("admin", cmd_admin, "manage users, roles and rules (the admin token, or your own for what the rules "
                                    "allow you)", parents=(admin_common,))
    actions = c.add_subparsers(dest="action", metavar="action", required=True)
    a = actions.add_parser("add", parents=[admin_common], help="add a user and print their token")
    a.add_argument("id")
    a.add_argument("--name", default="")
    a.add_argument("--field", action="append", metavar="KEY=VALUE", help="a profile field, e.g. role=release-manager")
    for action, help in (("remove", "remove a user"), ("rotate", "give a user a new token")):
        a = actions.add_parser(action, parents=[admin_common], help=help)
        a.add_argument("id")
    a = actions.add_parser("role", parents=[admin_common], help="show who holds a role, or change it")
    a.add_argument("role", choices=["admin", "channel_creator"])
    a.add_argument("--add", nargs="+", metavar="USER")
    a.add_argument("--remove", nargs="+", metavar="USER")
    a.add_argument("--everyone", action="store_true", help="channel_creator only: every user may create channels")
    a = actions.add_parser("policy-set", parents=[admin_common], help="replace the rules with a JSON file")
    a.add_argument("file")
    actions.add_parser("policy-show", parents=[admin_common], help="show the rules")
    return p


def run_interruptibly(func, args, client) -> int:
    """Runs a command in a worker thread while this one stays free to take Ctrl-C. On Windows a blocking network read
    is not interrupted by Ctrl-C or Ctrl-Break, so a `swarmchat wait` could not be stopped at the keyboard.

    This thread sleeps on a socket that wakes it at once when the worker ends (it writes ENDED) or a signal arrives
    (Python writes the signal's number, see signal.set_wakeup_fd). On a stop, the connection is shut down under the
    worker: its read fails at once and it cleans up (a half-written download is removed) before the command ends."""
    ENDED = b"\xff"  # no signal number is that high
    outcome = {}
    wake, ring = socket.socketpair()
    ring.setblocking(False)

    def work():
        try:
            outcome["code"] = func(args, client) or 0
        except BaseException as e:  # noqa: BLE001 - handed back to the main thread below
            outcome["error"] = e
        finally:
            try:
                ring.send(ENDED)
            except OSError:  # the main thread gave up waiting, and the command is ending without this one
                pass

    def ended(timeout=None) -> bool:
        while select.select([wake], [], [], timeout)[0]:
            if ENDED in wake.recv(512):
                return True
        return False

    previous = signal.set_wakeup_fd(ring.fileno())
    try:
        threading.Thread(target=work, daemon=True).start()
        try:
            ended()
        except KeyboardInterrupt:
            if client:
                client.interrupt()
            ended(5)
            raise
    finally:
        signal.set_wakeup_fd(previous)
        wake.close()
        ring.close()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["code"]

def main(argv=None) -> int:
    # A normal stop (kill, SIGTERM) ends like Ctrl-C, so half-written downloads are cleaned up on the way out.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if hasattr(signal, "SIGBREAK"):  # Ctrl-Break, the normal stop on Windows
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    p = parser()
    args = p.parse_args(argv)
    for key, value in (("json", False), ("server", None), ("token", None), ("admin_token", None), ("data", None)):
        if not hasattr(args, key):
            setattr(args, key, value)
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    client = None
    try:
        if args.command in ("serve", "view"):
            return args.func(args, None) or 0  # a server handles its own signals, which needs the main thread
        client = None if args.command in ("admin", "login") else Client(args.server, args.token)
        code = run_interruptibly(args.func, args, client if args.json or client is None else Shown(client))
        if client:
            client.close()  # confirms what a wait received, so it counts as read
        return code
    except ClientError as e:
        say(f"error: {e}", file=sys.stderr, lines=False)
        return EXIT.get(e.status, 1)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        say(f"error: {e}", file=sys.stderr, lines=False)
        return 1
    except KeyboardInterrupt:
        return 130
