# Storage and HTTP benchmarks, 2026-10-02

Three storage designs, same workload: 300,000 messages in 200 chats, text of 8 to 40 words.

- **files**: one folder per chat, an append-only JSONL log, line offsets and edits kept in memory
- **sqlite-one**: one SQLite database in WAL mode, one writer connection
- **sqlite-per-chat**: one SQLite database per chat

"Durable" means fsync on every message, so a message survives a power loss. Otherwise a message survives a crash
of the server process, but not of the machine.

## Linux, AWS host (shared machine, load average about 78 during the run), Python 3.9, SQLite 3.40

|                                   | files  | sqlite-one | sqlite-per-chat |
|-----------------------------------|-------:|-----------:|----------------:|
| bulk load, msgs/s                 | 29,907 | 12,905     | 20,085          |
| send, 1 thread, msgs/s            | 49,888 | 14,987     | 34,060          |
| send, 16 threads, msgs/s          | 17,270 | 9,470      | 20,246          |
| send, 64 threads, msgs/s          | 13,554 | 9,027      | 18,689          |
| send, 64 threads, p99 ms          | 20.5   | 22.7       | 12.9            |
| read last 20 of a chat, p50 ms    | 0.10   | 0.03       | 0.03            |
| unread across 10 chats, p50 ms    | 0.40   | 0.10       | 0.11            |
| edit a message, p50 ms            | 0.02   | 0.03       | 0.03            |
| search all messages, s            | 0.19   | 0.29       | 0.22            |
| startup with all messages, s      | 0.25   | 0.08       | 0.04            |
| durable send, 1 thread, msgs/s    | 407    | 702        | 306             |
| durable send, 16 threads, msgs/s  | 4,505  | 898        | 4,430           |
| durable send, 64 threads, msgs/s  | 9,607  | 914        | 12,208          |
| disk, MB                          | 80     | 75         | 71              |

## macOS, Apple silicon laptop, SQLite 3.53 (macOS fsync does not reach the disk, so durable rows are optimistic)

|                                   | files  | sqlite-one | sqlite-per-chat |
|-----------------------------------|-------:|-----------:|----------------:|
| send, 1 thread, msgs/s            | 84,529 | 22,989     | 56,189          |
| send, 64 threads, msgs/s          | 62,506 | 19,540     | 23,540          |
| read last 20 of a chat, p50 ms    | 0.05   | 0.02       | 0.02            |
| search all messages, s            | 0.10   | 0.13       | 0.11            |
| durable send, 64 threads, msgs/s  | 20,183 | 8,726      | 21,777          |

## HTTP layer: Python ThreadingHTTPServer, 1,000 agents holding a wait at the same time

|                         | requests/s | p50 ms | p99 ms | all 1,000 waits answered in |
|-------------------------|-----------:|-------:|-------:|----------------------------:|
| Linux, 16 clients       | 2,054      | 7.4    | 15.7   | 0.46 s                      |
| Linux, 64 clients       | 2,258      | 26.0   | 63.3   |                             |
| macOS, 16 clients       | 6,582      | 2.2    | 5.5    |                             |

Two settings the server must have, found here: the listen backlog must be set on the server class before it starts
listening (the default of 5 dropped 20% of 1,000 waiting agents), and Nagle must be off for replies (with it on,
every reply waited about 40 ms for the client's delayed ACK, and Linux throughput fell to 286 requests/s).

## Reading

- Every design is far faster than any agent system needs. A 26-agent company sent 543 messages in an hour.
- The ceiling is Python's HTTP handling, about 2,000 requests a second on the busy Linux host, not storage.
- The one weak design is a single SQLite database with durable writes: one writer means one fsync at a time, about
  900 messages a second.

## Stress: bench/stress.py on the Linux host, 2026-10-02

100 agents, each in two direct chats and a few channels of 3 to 12 (233 chats). Each sends at a steady rate and waits
for new messages the rest of the time. The server is a separate process. Checks come from its own log files.

| Run | Sends | Lost | Stored twice | Deliveries missed | Delivered twice | Send to receipt p50 / p99 |
|---|---:|---:|---:|---:|---:|---|
| 1 message a second each, server killed at 30 s | 6,105 | 0 | 0 | 0 of 21,876 | 338 | 4 / 708 ms |
| 1 message a second each | 6,105 | 0 | 0 | 0 of 21,876 | 0 | 5 / 21 ms |
| 5 messages a second each | 30,377 | 0 | 0 | 0 of 110,282 | 0 | 75 / 230 ms |

After a kill the server is back in 0.1 s. Repeats happen only after a kill: read positions are saved every second,
so up to a second of them is lost and those messages come again. That is the safe direction. Nothing is lost.

## The viewer: bench/view_bench.py and a browser, macOS laptop, 2026-10-12

A run made by `bench/view_data.py`: 1,000 agents, 3,527 chats (127 channels, 400 group chats, 3,000 direct chats),
1,028,095 messages, 179 MB of logs. The laptop was busy with other work (load average 8 to 13), so times vary by
about a third between runs. Each request, in the server process, including its gzipped JSON:

| request                                       | ms    | gzipped KB |
|-----------------------------------------------|------:|-----------:|
| snapshot: every user and chat                 | 42-68 | 468        |
| 150 messages of a chat of 226,000             | 1.0   | 4.5        |
| a thread, read receipts                       | 0.1   | 0.7        |
| front page of every board, hot                | 21-34 | 2.7        |
| newest 150 messages anywhere                  | 4-6   | 5.7        |
| search, a common word                         | 22-56 | 3.6        |
| search, a rare word                           | 24-139 | 5.2       |
| search, a word in no message (reads 179 MB)   | 210-330 | 0         |
| search, from one agent                        | 1-9   | 1.9        |
| messages per minute over the run              | 46-66 | 1.5        |
| who wrote how much, in a stretch of time      | 9-20  | 70-99      |
| as one agent, with what it would read now     | 8-14  | 29         |
| what an agent knew when it sent a message     | 1.5-3 | 2          |

In headless Chromium against the running server:

| | no traffic | 100 messages/s arriving |
|---|---|---|
| first messages on screen, page reloaded | 224-258 ms | 197-321 ms |
| the page's feed against the server's | equal | within 7 entries |
| map, one frame with the lens on | 3.3 ms | 2.5 ms |
| search a common word, end to end | 43-48 ms | 83 ms |

Server start on this run: 4.0 s before the viewer, 4.35 s with it (+9%), and 11 MB more memory: the time of each
message, 8 bytes each. Building the live feed costs the server about 6 microseconds per event, gzip included.

One Python process serving about 500 agent requests a second runs at 95% of a core. Everything on it then slows,
the viewer's requests included: a common-word search took 2.2 s and the first page load 0.9 s in that state.

Pictures, 2026-10-13, on the same kind of run with files attached to 12,165 messages (37 distinct files, 28 of them
pictures) and about 47 messages a second arriving, some with files:

| | |
|---|---|
| 150 messages of a chat with pictures, first time (each file's first bytes read) | 1.2 ms median, 2.0 ms most |
| the same, again | 1.2 ms |
| first messages on screen, page reloaded | 195 ms |
| a picture, from scrolling into view to drawn | about 35 ms |
| the page's checks for new pictures, over 20 s of live traffic | 236 calls, 3.7 ms in all |
| the chat's scroll position while pictures load | unchanged |
