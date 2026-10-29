"""HTTP layer benchmark: Python's ThreadingHTTPServer serving small JSON requests while many clients hold long waits.

    python3 bench/http_bench.py [--waiters 1000] [--clients 16] [--requests 20000]
"""
import argparse
import http.client
import json
import os
import resource
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WAKE = threading.Event()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # reply headers and body go out without waiting for the client's delayed ACK

    def log_message(self, *args):
        pass

    def _reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/wait":
            WAKE.wait(120)
            self._reply({"messages": []})
        else:
            self._reply({"id": "c0001:42"})


def call(conn, path, body):
    data = json.dumps(body).encode()
    conn.request("POST", path, data, {"Content-Type": "application/json", "Content-Length": str(len(data))})
    return conn.getresponse().read()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--waiters", type=int, default=1000)
    p.add_argument("--clients", type=int, default=16)
    p.add_argument("--requests", type=int, default=20000)
    args = p.parse_args()
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard if hard != resource.RLIM_INFINITY and hard < 65536 else 65536, hard))
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 4096  # the listen backlog: must be set before the socket listens, so on the class

    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    waiters, failed = [], []
    for _ in range(args.waiters):
        def hold():
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
                call(c, "/wait", {"timeout": 120})
            except OSError as error:
                failed.append(error)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        waiters.append(t)
        time.sleep(0.001)
    time.sleep(2)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / 1e6 if os.uname().sysname == "Darwin" else rss / 1e3

    lat, lock = [], threading.Lock()

    def client(n):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        mine = []
        for _ in range(n):
            t = time.perf_counter()
            call(c, "/send", {"chat": "c0001", "text": "hello " * 20})
            mine.append(time.perf_counter() - t)
        with lock:
            lat.extend(mine)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=client, args=(args.requests // args.clients,)) for _ in range(args.clients)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rate = len(lat) / (time.perf_counter() - t0)
    lat.sort()
    print(f"{args.waiters} agents holding a wait, memory of the whole test process (server and all clients) {rss_mb:,.0f} MB")
    print(f"{args.clients} clients sending: {rate:,.0f} requests/s, p50 {statistics.median(lat) * 1000:.2f} ms, "
          f"p99 {lat[int(len(lat) * 0.99)] * 1000:.2f} ms")
    t = time.perf_counter()
    WAKE.set()
    [w.join(30) for w in waiters]
    import collections
    print(f"waiters that failed to connect or were dropped: {len(failed)}",
          dict(collections.Counter(type(e).__name__ for e in failed)))
    print(f"waking all {args.waiters} waiters took {time.perf_counter() - t:.2f} s")


if __name__ == "__main__":
    main()
