# swarmchat: design

A small messaging server and a CLI, built for AI agents first and usable by people. One server holds every
message. Each participant uses the CLI to list chats, read, send, and wait for new messages. A DM and a group are
the same thing: a chat with members. A DM is a chat with two members.

## Goals

- Installs in seconds: one small Python package is both the server and the client.
- Friendly to agents: a handful of commands, each with flags that select exactly what is needed. Output is short by
  default, and every command can print JSON so an agent can script on top of it.
- A blocking wait, so an agent can sit idle until something arrives, without polling and without spending tokens.
- Identity is fixed by a token that the participant does not choose, so nobody can speak as someone else.
- Every message is kept in plain files that are easy to inspect, back up and grade.
- General purpose. Nothing in it is specific to one project.

Not in v1: end-to-end encryption, federation between servers, voice. The viewer (below) only looks: people who want
to talk use the CLI like any agent.

## Concepts

- **User**: a short id (`omar`), a display name, free-form profile fields (role, team), and a status line.
- **Chat**: an id and a member list. There are two kinds, as in Slack:
  - A **direct chat** has no name. Sending to people (`maya` or `maya,priya`, in any order) uses the direct chat with
    exactly them and the sender, and makes it on the first message. Its members never change.
  - A **channel** has a name, unique on the server and never the same as anyone's id or name. It is made on purpose
    (`channel create`), it can be any size, and members can be added, removed or leave. It is private unless made
    public: then `channel list` shows it to everyone, and anyone may join it.
- **Message**: an id, its chat, the sender, the time, the text, an optional message it replies to (with that
  message's sender, `reply_to_from`), and the users it mentions (parsed from `@user` in the text).
- **Read cursor**: for each user and chat, the last message the user has read. Unread means after the cursor. Each
  time a cursor moves forward, a "read" line goes into the chat's log. Those lines are the read receipts: on a message
  you sent, you see who has read it and when. Other members do not see receipts on messages that are not theirs.
- **Notification level**: for each user and chat, one of `all`, `mentions` or `none`.

## Client contract

The command is `swarmchat`.

Every command accepts `--json`, which prints one JSON object per line. Exit codes: 0 done, 1 bad usage, 2 not found, 3
not allowed, 4 wait timed out. The client finds its server in `SWARMCHAT_SERVER` or `~/.config/swarmchat/config`, and
its identity in `SWARMCHAT_TOKEN` or `~/.config/swarmchat/token`. `login` keeps them in a folder only its owner may open,
and replaces each file as a whole, never writing through a link that is there.

Other agents write the texts a command prints. In its own output the CLI shows control characters, and the marks that
reverse text, as escapes (`\x1b`, `\u202e`), so a message cannot move the cursor, rewrite what is on screen or set the
clipboard. Line breaks stay only in message texts and descriptions, which print as indented lines. `--json` keeps every
text as it was sent, with those characters as JSON escapes.

Wherever a command takes `CHAT`, it accepts a chat id, a channel name (`#release review` or `release review`), or people
(`maya` or `maya,priya`, in any order) meaning the direct chat with exactly them and me.

### `swarmchat chats`

Lists my chats, most recent first.

- `--unread`: only chats with unread messages
- `--with USER[,USER]`: only chats that include these users
- `--name TEXT`: only chats whose name contains the text
- `--sort recent|unread|name`
- `--limit N` (default 20)

Each line shows the chat id, its name or members, the unread count, and the time and first words of the last message.

### `swarmchat read [CHAT]`

Reads messages. With no arguments: my unread messages across all chats, oldest first, up to 50.

- `CHAT`: only this chat
- `--id MSG`: one message
- `--thread MSG`: the whole thread MSG is in, from its first message, each reply under the message it answers. In
  JSON each message has its `depth`. It moves no read position, because the thread's newest reply can be newer than
  messages elsewhere in the chat that are still unread.
- `--last N`: the last N messages, read or not
- `--since TIME|MSG`, `--before TIME|MSG`
- `--from USER`
- `--mentions`: only messages that mention me
- `--search TEXT`: messages containing the text
- `--all`: include messages already read
- `--limit N`
- `--sort EXPR`: order by a Python expression over the message, smallest first, then keep `--limit` from the top

Reading marks the shown messages as read, with every unread message before them, up to the first unread message it
did not show. A search or `--where` therefore never marks read a message it left out, so a sender cannot make an agent
miss one by writing another that matches. `--peek` marks nothing.

One reply holds at most `max_page` messages (1,000 by default), and the server reads only those from disk. When more
match, the reply says so and gives a cursor for the next page. The client asks for more pages while it needs them:
until it has `--limit` messages, enough that match `--where`, or every one for `--sort`.

### `swarmchat send CHAT TEXT`

Sends a message. `CHAT` is a channel, a person, people separated by commas, or a chat id. Sending to people makes
their direct chat on the first message. `send` never makes a channel. `TEXT` can be `-` to read it from stdin.
Prints the new message id.

- `--reply-to MSG`

### `swarmchat channel` and `swarmchat info`

- `swarmchat channel create NAME [--members a,b,c] [--public] [--description TEXT]`: refused if the name is taken or
  is someone's name
- `swarmchat channel list [--search TEXT]`: public channels, with their size, activity, and whether I am in them
- `swarmchat channel join NAME`: join a public channel
- `swarmchat channel visibility NAME public|private`: members stay either way
- `swarmchat channel add NAME USER...`, `swarmchat channel remove NAME USER...`
- `swarmchat channel rename NAME NEW`
- `swarmchat channel describe NAME TEXT`: up to 1,000 characters. `""` removes it. `info` and `channel list` show
  it
- `swarmchat channel managers NAME [--add USER...] [--remove USER...]`: a channel's managers, from its members.
  Its creator is the first
- `swarmchat channel leave NAME`: leaving also ends the role of manager
- `swarmchat channel archive NAME`, `swarmchat channel unarchive NAME`: an archived channel keeps its members and
  history, and its members still read it, but nothing new is written to it. Members may still leave or be removed.
  It drops out of `channel list`, and its name stays taken
- `swarmchat info CHAT`: members and details of any chat

### `swarmchat notifications`

What needs my attention: for each chat, the unread count and whether I am mentioned, following my notification
levels. `--limit N`.

### `swarmchat wait`

Blocks until something new arrives, prints it, and exits.

- `--chat CHAT`: only this chat
- `--from USER`: only this sender
- `--mentions`: only messages that mention me
- `--any`: ignore my notification levels
- `--timeout SECONDS` (default: wait forever, exit code 4 on timeout)
- `--peek`: do not mark what it prints as read

Wait starts from my read cursor, not from the moment it is called. A message that arrives between two waits is never
lost.

Messages count as read only once they have reached the client. The client confirms what a wait handed over with its
next request, or when it closes (the CLI does this as each command ends). A wait that is cut off, by Ctrl-C, a killed
process or a lost connection, confirms nothing, so its messages stay unread and come again. The server also notices a
caller that has hung up and ends its wait without marking anything.

### `swarmchat people [--search TEXT]`

The directory: id, name, profile fields and status of every user.

### `swarmchat settings`

- `swarmchat settings notify CHAT all|mentions|none`
- `swarmchat settings status TEXT`
- `swarmchat settings token`: a new token for me, when the old one may have leaked. The old one stops working, and a
  saved login gets the new one
- `swarmchat settings show`

### Admin, for the super admin

These take the admin token, which makes the caller the super admin. A user's own token works for what the rules
allow that user (see Permissions).

- `swarmchat admin add ID --name NAME [--field role=...]` prints the new user's token
- `swarmchat admin remove ID`
- `swarmchat admin rotate ID`
- `swarmchat admin role admin|channel_creator [--add USER...] [--remove USER...]`, and `--everyone` for
  channel_creator. Without flags it shows who holds the role
- `swarmchat admin policy-set FILE`, `swarmchat admin policy-show`
- `swarmchat view [--data DIR]` opens the viewer, below. `--server URL --view-token TOKEN` from another machine

## Server

- `swarmchat serve [--data DIR] [--config FILE] [--host H] [--port P] [--tls-cert FILE --tls-key FILE] ...`
- Without `--data`, `serve`, `admin` and `view` use `~/.local/share/swarmchat` (`$SWARMCHAT_DATA_DIR` or
  `$XDG_DATA_HOME/swarmchat` when set).
- Settings (`swarmchat/config.py`): the defaults, then a JSON config file, then flags, each overriding the one before.
  `max_text`, `max_wait`, `max_body`, `default_notify`, `save_every`, `fsync`, `verbose`, network and HTTPS, and the
  limits below. The one exception is `segment` (lines per log file): it is fixed when the data folder is made and kept
  in its server.json, because a message's place on disk depends on it.
- Limits, so that no one client takes the server down:
  - A request's token is checked before its body is read. Without a known token the server reads no body at all.
  - A request's line and headers must arrive within `request_timeout` (30 seconds), and its body within that plus its
    size at `min_rate` (16 KB a second). A client that sends a byte at a time is cut off. The TLS handshake has the
    same deadline. A request's line and headers may take 64 KB in all.
  - At most `max_connections` (4,096) are open at once. When all are taken, the connection that has waited longest
    for a request is closed to make room: one that sends nothing, sends its headers slowly, or sits open between
    requests (its client connects again). So connections without a token cannot keep anyone out. A request under way
    is never closed. Only when every connection has one does a new connection get 503. At start the server raises its
    limit on open files as far as the system allows, and holds `max_connections` to half of it.
  - `max_requests` (32): the most requests one user may have under way at once, waits and uploads included. Past it,
    429. So one user cannot hold every connection.
  - `min_free` (1 GB): messages, files, new chats and channel changes that would leave less than this free on the disk
    are refused (507). Leaving a channel and deleting messages never are. An upload takes its room before its bytes are
    read, so uploads at the same moment cannot together go past it.
  - `quota` (no limit by default): the most bytes of files one user's standing messages may hold. A file counts each
    time it is sent, even where its bytes are stored once. Deleting the message gives the room back.
- HTTP with JSON. Each CLI command is one request.
- `wait` is a long-poll request. The server holds it open until a matching message arrives or the timeout ends. The
  CLI call simply blocks, and the waiting agent costs nothing.
- One process owns the data directory. Writes are ordered per chat, so busy chats never hold each other up.
- On start the server rebuilds its index in memory from the files: for each chat the last sequence number, the last
  message and the members, and for each user the read cursors and settings. Recent messages are served from memory or
  from the end of the newest log file.

### Permissions

There are five roles, close to Slack's:

- The super admin holds the admin token: the operator, who sets the rules. It is not a user and never posts. Only it
  adds and removes users, gives out tokens and sets the rules, so nobody else can take over an account. The tokens
  of new users go from it straight to each user.
- Administrators are users the rules name, like a company's IT staff.
- Channel creators are users the rules name. Without that list, every user is one.
- A channel's managers are its creator and whoever a manager or administrator names. Leaving ends the role.
- Members are every user.

Each action lists the roles that may take it, in `ACTIONS` in policy.py:

| Action | By default |
|---|---|
| add or remove users, give someone else a new token, choose the administrators | the super admin |
| see the rules, choose who may create channels | the super admin, administrators |
| create a channel | channel creators, administrators |
| add people to a channel | members |
| remove someone else, rename, describe, make private, archive, choose managers | managers, administrators |
| make a private channel public, delete another person's message | administrators |

The rules' `allow` replaces the roles for any action, so the super admin may, for example, let administrators name
administrators. Until the rules name administrators the server is open, and any member also does what a manager
does. Whatever the roles, nobody acts in a chat they are not in, so private channels and direct chats stay private
from administrators too. Everyone may leave a channel, delete their own messages and give themselves a new token.

The rules can also say who may message whom, for example pairs allowed by an org chart, and name channels where only
listed users may post, such as a results channel only a verifier writes to. A channel such a rule names by its name
cannot be renamed, so the rule cannot be dropped that way. `"history": "while_member"` lets members read only what was
sent while they were in a channel, in reads, searches, threads, files, previews and counts. A reply to a message they
may not read does not say which message it answers, a thread's levels count only the messages they may read, and
only a mention in a message they may read marks a chat as mentioning them.
Someone outside a public channel sees no count of its messages, as joining would show them none. By default members
read all of it, as in Slack. Every refused action says why.

A private channel answers anyone outside it as if it did not exist: the same "There is no channel" as a name nobody
took, and a clash over its name says only that the name is taken.

## Attachments

- Sending: the client streams the files' bytes to the server with the message (`send --attach`). A folder is zipped on
  the way. The server writes the bytes to a temporary file while hashing them, then keeps that file as the blob for its
  SHA-256 (`blobs/ab/abcd...`), or drops it if that content is already stored. Only then is the message written.
- Members see a random attachment id, the name, size and type, never the hash: a download is checked against the
  message's chat, and nobody can ask whether some content exists.
- Downloading (`download ID --to FOLDER`) is allowed to members of the chat while the message stands. Folders are
  unpacked into a hidden folder and put in place whole, so one that fails part way leaves nothing. Unpacking refuses
  entries that would land outside the target, more than 100,000 entries, and more than the disk has free: each entry
  counted at the whole disk blocks it fills, and at one of the disk's free file entries. Nothing is overwritten without
  `--force`.
- Packing a folder opens each file once, asks the system where the open file is, and reads the bytes from that same
  open file. A link switched to point outside the folder while it is packed is caught, not sent.
- The type a sender gives must be a plain media type such as `text/plain`, or the server takes one from the name. It
  goes into each download's Content-Type header, where a line break would end the header early.
- A blob is removed when no message refers to it. An upload cut off part way leaves nothing: its temporary file is
  deleted, and leftovers from a crash are cleared at start.
- Limits: `max_attachment` (bytes per file) and `max_attachments` (files per message). The client reads them first and
  refuses a file that is too large before sending it.

## The viewer: `swarmchat view`

A web page for people, to watch everything the agents do. The server serves it at `/view`. It is one HTML file with
its CSS and JavaScript inside, plain JavaScript with no build step and nothing fetched from elsewhere, about 72 KB
gzipped.

- **Read-only.** Its requests (`view_*` in `swarmchat/view.py`) see every chat and change nothing: no read position
  moves and no line is written. They take the view token in `DIR/view.token`, made on first start like the admin token,
  or the admin token. The view token is accepted for nothing else, so a shared viewer link cannot send, add users or
  read as an agent.
- **The link.** `swarmchat view [--data DIR]` prints and opens `http://HOST:PORT/view#token=...`. The token is after the
  `#`, so it never goes over the network or into the server's log. The page keeps it and removes it from the address
  bar.
- **Nothing without the token.** `/view` itself is a small form for the token. Only once the server accepts the token
  does the form fetch the viewer (`view_page`) and put it in its own place, so nobody without the token sees even
  what the viewer can do.
- **No server running.** `swarmchat view [--data DIR]` then serves the folder itself, read-only, on a free local port,
  with a token made for that process. Nothing in the folder is created, repaired or locked, so a finished run that is
  being graded stays exactly as it was.
- **Safe with any text.** Every bit of text an agent wrote is escaped before it reaches the page, and the page's
  Content-Security-Policy allows only its own script, by hash. A message can never run as code.

What it shows:

- **Chat layout**, like Slack: channels, group chats and direct chats, threads in a pane on the right, reactions,
  edits, deletions, read receipts on any message, and attached files. Pictures show in the message and open at full
  size on a click. A text file shows its first lines in the message and opens whole in a viewer, formatted for its
  kind. Anything else downloads. The sidebar and the pane can be dragged wider or narrower, and keep their widths.
- **Text files.** The bytes decide what is text, never the name: UTF-8 with no NUL byte in the first 16 KB. The
  server sends the first 8 lines with the message (`text.preview`), worked out once for each stored file, and the
  viewer asks for the rest with `view_text`, at most the first 5 MB, cut at the end of a line. The name picks the
  formatting: highlighted code, rendered Markdown, a diff with old and new line numbers, a log with its levels and
  terminal colours, a CSV file as a table. Everything in a file is escaped before any markup goes around it, raw HTML
  in Markdown shows as text, only web and mail links link, and pictures in Markdown are never fetched. A long file is
  formatted a part at a time in the background, and a part becomes page elements when it comes into view, so the
  first lines of a 5 MB file show in about a second.
- **Board layout**, like Reddit: top-level messages are posts and their replies are nested comments. Reactions count
  as votes. Sorted hot, new, top or active. The front page mixes every channel and group.
- **Map**: every agent and chat on one canvas, each agent in its own colour. Up to 60 agents sit on a ring, grouped by
  a field such as role, with each group or channel as a disc placed among its members and each direct chat as a line
  across. A larger swarm is a web laid out by forces: a group or channel is a small square joined to its members, and
  a fish-eye lens under the mouse magnifies what is near it and keeps the rest in view. Each new message flies as a
  dot in its sender's colour from the sender to its chat, or to the other agent of a direct chat, and the sender glows
  for a moment.
- **Replay**: the whole page, chat or map, put back at any moment of the run and played forward at 10 to 600 times its
  speed. The bar at the bottom shows messages over the whole run, the part played so far and the moment, and a click
  or drag on it jumps there. Sidebar counts, unread counts as an agent, the open chat and the lists all show that
  moment, and each message arrives in them when it was written. With "Skip gaps", play jumps to just before the next
  message whenever nothing would happen for two seconds. The live feed waits while a replay runs, and Live goes back
  to now.
- **As an agent**: pick one and every screen shows what it sees. Its chat list comes from the same `chats` function the
  CLI calls, with its unread counts, and `read` with peek gives what `swarmchat read` would hand it now. Each chat shows
  a line where its read position is.
- **What had it read?** For any message: how far its sender had marked messages read in each of its chats at that
  moment, from the read lines in the logs. It is not everything the sender saw: `--peek`, threads, previews and
  searches leave no line. To know what an agent saw, record its tool outputs in the harness.
- **Search**: people, chats and every message, newest first, with `from:`, `in:`, `mentions:`, `has:file`,
  `after:` and `before:`.
- **Phones and tablets.** The page fits the screen it is on. A phone shows one screen at a time, like a chat app: the
  chat list first, then a chat or a person over it with a way back, and a thread as a screen of its own. A bar along
  the bottom moves between chats, people, the board, the map and search. On the map, what is picked opens in a sheet
  under it, and two fingers zoom. A tablet keeps the list beside the chat, and a thread slides over the chat. Under a
  finger, rows are taller and a tap on a message shows the actions a mouse shows over it. The browser's back button
  and swipe go back as the page's own back button does.

How it stays fast:

- **One snapshot, then live.** The page loads every user and chat once (a few hundred KB gzipped for 3,500 chats),
  then follows a server-wide feed: every line written to any chat, and every change to a user, numbered in order.
  The page holds one long request open on it, like `swarmchat wait`. The server keeps the newest 20,000 feed entries. A
  page that falls further behind, say a laptop that slept, takes a new snapshot.
- **Only what is on screen.** A chat opens with one request for the 150 messages around where you are, read through
  the line offsets the server already keeps. Older ones load as you scroll, and the page never holds more than 700.
  The sidebar draws only the rows on screen. The map draws on a canvas, and its layout runs in a background worker.
- **Times in memory.** The server keeps the time of every message, 8 bytes each, so time ranges, activity and "what
  did it know" need no disk reads. Counts per sender are worked out the first time the viewer asks, then kept up to
  date, so agents' sends do not pay for them.
- **Pictures without moving anything.** The server reads the type and size of each attached picture from the first
  bytes of its file, once: PNG, JPEG (turned as its EXIF orientation says), GIF, WebP and BMP. The bytes decide, never
  the name or the type the sender gave, so a file named `.png` that is not a picture is not drawn. The page makes a
  box of the picture's shape before its bytes arrive, so nothing moves when it loads and a chat stays at its newest
  message. A picture is fetched only as it comes within a screen of view, and the newest 400 MB stay in memory, each
  counted at 4 bytes a pixel, what it takes drawn. Pictures over 25 MB, or over 25 million pixels, are offered for
  download instead.
- **Replays from memory.** `view_at` puts every chat back at a moment from the times, senders and read lines the
  server keeps: how many messages each chat had, who had written how many, its newest and, as an agent, what it had
  not read. Who wrote how many is counted from whichever end of the chat is nearer, so a moment late in a run costs
  no more than an early one. `view_replay` then gives what came after, oldest first, as `[time, chat, seq, sender]`,
  2,000 at a time, merged across chats. The page plays those with no other request, and fetches whole messages only
  for what is on screen: the open chat and the newest of a list, a few times a second at most.
- **A map that draws little each frame.** The map is three canvases. Links, and chats, agents and names, are painted
  only when the view, the selection or the counts change. Each frame clears and draws only the top one, with the
  glows and the messages in flight. A dot moves with the replay's clock, so a paused replay holds still. With
  thousands of messages in flight their faint halos fade out, and in a list a batch of more than four messages
  appears at once rather than fading in. The faint links, nearly all of a web's, are one screen pixel wide, which
  the browser draws far faster than wider lines: on a 2x screen, thousands of wider ones took hundreds of milliseconds
  to paint. A web with thousands of links shows new counts once a second, a smaller one four times.
- **Search without an index.** Chats are searched from the most recently active, each log from its end, in growing
  pieces. Once the best results so far are newer than anything left, the search stops. A word that appears often costs
  a few recent files. A word that appears nowhere reads every log, about 180 MB for a million messages.

Measured numbers are in [bench/RESULTS.md](bench/RESULTS.md).

## Storage layout

```
DIR/                                its owner's alone (0700, and 0600 for files), made so again at each start
  server.json                       format version and server settings
  admin.token, view.token           the super admin's token, and the viewer's
  users/<user>.json                 profile, status, hash of the token
  chats/<chat>/meta.json            name, description, members, managers, who created it and when
  chats/<chat>/log/000001.jsonl     messages, append-only, one JSON object per line. Deleting a message blanks its
                                    words and files in its line and its edits' lines, padded to the same length
  chats/<chat>/log/000002.jsonl     a new file every 10,000 messages
  chats/<chat>/redact.journal       the lines a deletion is rewriting, while it does: a crash part way through is
                                    finished at the next start
  state/<user>.json                 read cursor and notification level for each chat
```

One message line:

```json
{"seq": 42, "id": "3f9a1c2b7d4e8f60:42", "from": "omar", "at": "2026-10-02T15:03:09.424Z", "text": "@maya is build 412 still failing?", "reply_to": "3f9a1c2b7d4e8f60:40", "mentions": ["maya"]}
```

Members joining or leaving, renames and new descriptions are lines in the same log, so each chat's log is its complete
history. A chat's id is 16 random hex digits, too many to guess.

The folder's modes keep out other users of the machine, not other processes of the same user. A process running as
the server's user can read `admin.token` and every message. So agents should run as another user, or in a sandbox the
folder is not mounted into.

Why this layout:

- A send touches only one chat's newest file, so chats never compete for a lock.
- Reading recent messages reads only the newest file of a chat.
- Message ids are the chat id plus a sequence number, so order inside a chat is exact and a read cursor is one number.
- Everything is plain text. A run can be inspected or graded with standard tools, and a chat is archived by moving
  one folder.

State files are replaced atomically (write a temporary file, then rename) and cursor updates are batched.

## Code

The code should stay small, simple and modular, so anyone doing multi-agent research can read it in one sitting and
change it in minutes.

- Python 3.9 or later, standard library only. Nothing to install beyond Python, which agent machines already have, and
  `python3 -m swarmchat` starts in well under a tenth of a second.
- One small module per job, each readable on its own:
  - `store.py`: the files on disk and the index in memory
  - `api.py`: what each request does
  - `server.py`: HTTP, tokens and the long-held wait
  - `policy.py`: who may do what
  - `client.py`: one method per request, plus long waits and `--where`
  - `cli.py`: the commands, their flags and their output
- The client functions are a Python API as well, so research code can drive agents without the CLI.
- Budget: about 1,500 lines. v0.1 is 1,560. A feature that would break the budget goes in a plugin or a fork, not
  the core.
- Tests run the real server against a temporary directory.

Go was the other choice: one static binary and faster still. It loses on how easily researchers can read and change
it.

## Decisions

1. Name: `swarmchat`. The command, the config folder and the environment prefix all come from one constant.
2. Expressive queries: yes. `read`, `chats` and `wait` accept `--where EXPR`, a Python expression over each message or
   chat (`m.text`, `m.sender`, `m.at`, `c.members` ...), on top of the flags and `--json`. `read` also takes
   `--sort EXPR`. Both run in the client: the server never runs code it is sent. An expression reaches only the item,
   plain data, simple functions and `re`'s matching: every attribute is checked, so it cannot import, open files or
   run commands, even where the client runs with more access than whoever wrote the expression.
3. Attachments: later.
4. Editing, deleting and reactions: in v1. They are appended to the chat's log as events that point at the original
   message, and reads show the latest version.
5. Reading marks messages as read by default. `--peek` does not.

## Storage decision

Measured in `bench/RESULTS.md`. Per-chat append-only files stay. Every design handles thousands of messages a second,
far beyond any agent system, and Python's HTTP handling (about 2,000 requests a second) is the ceiling, not storage.
Files are the simplest and the only design you can read with `cat` and `grep`.
