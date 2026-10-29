"""Files for the demo runs of bench/view_data.py and bench/view_live.py: pictures agents would share (charts,
dashboards, screenshots, flame graphs, diagrams), drawn as real PNGs with the standard library only, plus logs, tables,
a patch, code, notes and a folder of results.

    python3 bench/demo_files.py OUT_DIR [--seed 1]
"""
import argparse
import json
import math
import os
import random
import struct
import time
import zlib

DARK = dict(bg=(17, 20, 27), panel=(26, 30, 39), grid=(40, 46, 58), axis=(78, 86, 102), ink=(150, 158, 175), faint=(60, 67, 80))
LIGHT = dict(bg=(255, 255, 255), panel=(246, 247, 249), grid=(232, 235, 240), axis=(160, 166, 178), ink=(90, 98, 112), faint=(214, 218, 225))
SERIES = [(76, 141, 255), (255, 159, 67), (46, 204, 135), (239, 83, 102), (168, 117, 255), (38, 198, 218)]


class Canvas:
    """RGB pixels in a bytearray. Everything is drawn as filled rows, which Python copies fast."""

    def __init__(self, w, h, bg):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def rect(self, x0, y0, x1, y1, c):
        x0, x1, y0, y1 = max(0, int(x0)), min(self.w, int(x1)), max(0, int(y0)), min(self.h, int(y1))
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(c) * (x1 - x0)
        for y in range(y0, y1):
            i = (y * self.w + x0) * 3
            self.px[i:i + len(row)] = row

    def round_rect(self, x0, y0, x1, y1, r, c):
        r = int(min(r, (x1 - x0) / 2, (y1 - y0) / 2))
        self.rect(x0, y0 + r, x1, y1 - r, c)
        for k in range(r):
            inset = r - math.sqrt(max(0, r * r - (r - k - 0.5) ** 2))
            self.rect(x0 + inset, y0 + k, x1 - inset, y0 + k + 1, c)
            self.rect(x0 + inset, y1 - k - 1, x1 - inset, y1 - k, c)

    def line(self, x0, y0, x1, y1, c, t=2):
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for k in range(n + 1):
            x, y = x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n
            self.rect(x - t / 2, y - t / 2, x + t / 2 + 0.5, y + t / 2 + 0.5, c)

    def disc(self, cx, cy, r, c):
        for dy in range(-r, r + 1):
            dx = math.sqrt(max(0, r * r - dy * dy))
            self.rect(cx - dx, cy + dy, cx + dx + 1, cy + dy + 1, c)

    def png(self) -> bytes:
        stride = self.w * 3
        raw = b"".join(b"\0" + bytes(self.px[y * stride:(y + 1) * stride]) for y in range(self.h))

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def frame(c, th, x0, y0, x1, y1, rows=5, cols=8):
    """A plot area with a grid and axes."""
    for i in range(rows + 1):
        y = y0 + (y1 - y0) * i / rows
        c.rect(x0, y, x1, y + 1, th["grid"])
        c.rect(x0 - 34, y - 3, x0 - 10, y + 3, th["faint"])  # where a tick label would be
    for i in range(cols + 1 if cols else 0):
        x = x0 + (x1 - x0) * i / cols
        c.rect(x, y0, x + 1, y1, th["grid"])
        c.rect(x - 12, y1 + 10, x + 12, y1 + 15, th["faint"])
    c.rect(x0, y1, x1, y1 + 2, th["axis"])
    c.rect(x0 - 1, y0, x0 + 1, y1, th["axis"])


def legend(c, th, x, y, n):
    for i in range(n):
        c.round_rect(x, y + i * 22, x + 14, y + i * 22 + 14, 3, SERIES[i])
        c.rect(x + 22, y + i * 22 + 5, x + 22 + 50 + 13 * (i % 3), y + i * 22 + 10, th["ink"])


def line_chart(rng, w, h, th, shape):
    c = Canvas(w, h, th["bg"])
    x0, y0, x1, y1 = 70, 60, w - 150, h - 50
    c.rect(x0, 22, x0 + 220, 32, th["ink"])  # a title
    frame(c, th, x0, y0, x1, y1)
    n = rng.randint(2, 4)
    for s in range(n):
        points, v = [], rng.uniform(0.3, 0.6)
        for i in range(160):
            t = i / 159
            if shape == "loss":
                v = (0.9 - 0.15 * s) * math.exp(-4 * t) + 0.08 + 0.03 * s + rng.gauss(0, 0.012)
            elif shape == "spike":
                v = 0.25 + 0.1 * s + rng.gauss(0, 0.02) + (0.45 if 0.55 < t < 0.62 else 0)
            else:
                v = min(0.95, max(0.05, v + rng.gauss(0, 0.025)))
            points.append((x0 + (x1 - x0) * t, y1 - (y1 - y0) * min(1, max(0, v))))
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            c.line(ax, ay, bx, by, SERIES[s], 2)
    legend(c, th, x1 + 20, y0, n)
    return c


def bar_chart(rng, w, h, th):
    c = Canvas(w, h, th["bg"])
    x0, y0, x1, y1 = 70, 60, w - 40, h - 50
    c.rect(x0, 22, x0 + 260, 32, th["ink"])
    frame(c, th, x0, y0, x1, y1, cols=0)
    groups, n = rng.randint(4, 8), rng.randint(1, 3)
    slot = (x1 - x0) / groups
    bar = slot * 0.7 / n
    for g in range(groups):
        for s in range(n):
            v = rng.uniform(0.15, 0.95)
            bx = x0 + g * slot + slot * 0.15 + s * bar
            c.round_rect(bx + 1, y1 - (y1 - y0) * v, bx + bar - 1, y1 + 4, 4, SERIES[s])
        c.rect(x0 + g * slot + slot / 2 - 20, y1 + 12, x0 + g * slot + slot / 2 + 20, y1 + 17, th["faint"])
    c.rect(x0 - 1, y1, x1, y1 + 2, th["axis"])
    return c


def heatmap(rng, w, h, th, square=False):
    c = Canvas(w, h, th["bg"])
    cols, rows = (24, 24) if square else (48, 20)
    x0, y0 = 70, 50
    cw, ch = (w - x0 - 90) / cols, (h - y0 - 30) / rows
    lo, hi = th["panel"], (255, 120, 50) if th is DARK else (40, 90, 200)
    for r in range(rows):
        for k in range(cols):
            if square:
                v = 0.9 if r == k else rng.random() ** 3 * 0.6
            else:
                v = min(1, max(0, 0.5 + 0.35 * math.sin(k / 5 + r / 7) + rng.gauss(0, 0.12)))
            c.rect(x0 + k * cw + 1, y0 + r * ch + 1, x0 + (k + 1) * cw, y0 + (r + 1) * ch, mix(lo, hi, v))
    for i in range(100):  # the colour scale
        c.rect(w - 50, y0 + (h - y0 - 30) * (1 - i / 100), w - 30, y0 + (h - y0 - 30) * (1 - (i - 1) / 100), mix(lo, hi, i / 100))
    return c


def scatter(rng, w, h, th):
    c = Canvas(w, h, th["bg"])
    x0, y0, x1, y1 = 70, 50, w - 40, h - 50
    frame(c, th, x0, y0, x1, y1)
    for s in range(rng.randint(3, 5)):
        cx, cy, spread = rng.uniform(0.2, 0.8), rng.uniform(0.2, 0.8), rng.uniform(0.04, 0.1)
        for _ in range(140):
            x, y = rng.gauss(cx, spread), rng.gauss(cy, spread)
            if 0 < x < 1 and 0 < y < 1:
                c.disc(x0 + (x1 - x0) * x, y1 - (y1 - y0) * y, 3, SERIES[s])
    return c


def sparkline(c, rng, x0, y0, x1, y1, color):
    v, last = rng.uniform(0.3, 0.7), None
    for i in range(60):
        v = min(0.95, max(0.05, v + rng.gauss(0, 0.06)))
        p = (x0 + (x1 - x0) * i / 59, y1 - (y1 - y0) * v)
        if last:
            c.line(*last, *p, color, 2)
        last = p


def dashboard(rng, w, h):
    th = DARK
    c = Canvas(w, h, th["bg"])
    c.rect(0, 0, w, 48, th["panel"])
    c.round_rect(16, 14, 36, 34, 5, SERIES[0])
    for i in range(5):
        c.round_rect(60 + i * 110, 18, 150 + i * 110, 30, 6, th["grid"] if i else th["axis"])
    cols, gap = 3, 14
    pw = (w - gap * (cols + 1)) / cols
    y = 62
    for row in range(3):
        ph = 110 if row == 0 else (h - 62 - 110 - 4 * gap) / 2
        for k in range(cols):
            x = gap + k * (pw + gap)
            c.round_rect(x, y, x + pw, y + ph, 8, th["panel"])
            c.rect(x + 14, y + 14, x + 110, y + 22, th["ink"])
            if row == 0:  # big numbers
                c.round_rect(x + 14, y + 42, x + 14 + rng.randint(90, 160), y + 80, 6, SERIES[k])
            elif (row + k) % 2:
                sparkline(c, rng, x + 14, y + 40, x + pw - 14, y + ph - 16, SERIES[(row + k) % len(SERIES)])
            else:
                n = 14
                for b in range(n):
                    v = rng.uniform(0.2, 1)
                    bx = x + 14 + (pw - 28) * b / n
                    c.rect(bx + 2, y + ph - 16 - (ph - 60) * v, bx + (pw - 28) / n - 2, y + ph - 16, SERIES[(row + k + 1) % len(SERIES)])
        y += ph + gap
    return c


def window(rng, w, h, th):
    """A screenshot of an app: title bar, a list on the left, a form or a table on the right."""
    c = Canvas(w, h, th["bg"])
    c.rect(0, 0, w, 38, th["panel"])
    for i, col in enumerate([(255, 95, 87), (254, 188, 46), (40, 200, 64)]):
        c.disc(22 + i * 22, 19, 6, col)
    c.round_rect(w / 2 - 160, 10, w / 2 + 160, 28, 6, th["bg"])
    c.rect(0, 38, 230, h, th["panel"])
    c.rect(230, 38, 231, h, th["grid"])
    for i in range(14):
        y = 60 + i * 34
        if i == 3:
            c.round_rect(10, y - 8, 220, y + 18, 6, mix(th["panel"], SERIES[0], 0.35))
        c.round_rect(20, y, 34, y + 12, 3, th["faint"])
        c.round_rect(44, y + 2, 44 + rng.randint(60, 150), y + 10, 4, th["ink"] if i == 3 else th["axis"])
    x0 = 260
    c.round_rect(x0, 62, x0 + 260, 78, 6, th["ink"])
    for r in range(rng.randint(8, 12)):
        y = 110 + r * 44
        c.rect(x0, y + 30, w - 30, y + 31, th["grid"])
        for k, width in enumerate([140, 220, 90, 120]):
            x = x0 + [0, 200, 480, 640][k]
            if x + width < w - 30:
                color = SERIES[2] if k == 2 and r % 3 else SERIES[3] if k == 2 else th["axis"] if k else th["ink"]
                c.round_rect(x, y + 8, x + width * rng.uniform(0.5, 1), y + 18, 5, color)
    c.round_rect(w - 170, h - 60, w - 30, h - 26, 8, SERIES[0])
    return c


def flamegraph(rng, w, h):
    th = DARK
    c = Canvas(w, h, th["bg"])
    row = 22

    def stack(x0, x1, depth):
        y = h - 20 - (depth + 1) * row
        if y < 20 or x1 - x0 < 4:
            return
        c.round_rect(x0 + 1, y + 1, x1 - 1, y + row - 1, 3, mix((230, 80, 40), (250, 200, 60), rng.random()))
        x = x0
        while x < x1 - 6 and depth < 22:
            width = (x1 - x0) * rng.uniform(0.15, 0.7)
            if x + width > x1:
                width = x1 - x
            if rng.random() < 0.8:
                stack(x, x + width, depth + 1)
            x += width + rng.uniform(0, (x1 - x0) * 0.1)
    stack(10, w - 10, 0)
    return c


def diagram(rng, w, h, th):
    c = Canvas(w, h, th["bg"])
    boxes = []
    cols, rows = rng.randint(3, 5), rng.randint(2, 3)
    for k in range(cols):
        for r in range(rows):
            if rng.random() < 0.8:
                x = 60 + k * (w - 120) / cols
                y = 60 + r * (h - 120) / rows + rng.uniform(-10, 10)
                boxes.append((k, x, y, x + 170, y + 70))
    for k, x0, y0, x1, y1 in boxes:  # arrows to boxes in the next column
        for k2, a0, b0, a1, b1 in boxes:
            if k2 == k + 1 and rng.random() < 0.6:
                c.line(x1, (y0 + y1) / 2, a0, (b0 + b1) / 2, th["axis"], 2)
                c.rect(a0 - 8, (b0 + b1) / 2 - 4, a0, (b0 + b1) / 2 + 4, th["axis"])
    for i, (k, x0, y0, x1, y1) in enumerate(boxes):
        c.round_rect(x0, y0, x1, y1, 10, SERIES[k % len(SERIES)])
        c.round_rect(x0 + 3, y0 + 3, x1 - 3, y1 - 3, 8, th["panel"])
        c.round_rect(x0 + 18, y0 + 22, x0 + 18 + rng.randint(70, 130), y0 + 32, 5, th["ink"])
        c.round_rect(x0 + 18, y0 + 42, x0 + 18 + rng.randint(40, 90), y0 + 49, 4, th["faint"])
    return c


def pictures(seed=1) -> list:
    """[(file name, PNG bytes)]."""
    rng = random.Random(seed)
    stamp = time.strftime("%Y-%m-%d", time.gmtime(1791900000))
    plan = [
        ("latency-p99.png", lambda: line_chart(rng, 1200, 640, DARK, "walk")),
        ("error-rate-after-deploy.png", lambda: line_chart(rng, 1200, 560, DARK, "spike")),
        ("loss-curve.png", lambda: line_chart(rng, 960, 600, LIGHT, "loss")),
        ("throughput.png", lambda: line_chart(rng, 1400, 520, LIGHT, "walk")),
        ("eval-loss-vs-step.png", lambda: line_chart(rng, 800, 500, DARK, "loss")),
        ("queue-depth.png", lambda: line_chart(rng, 1000, 420, DARK, "spike")),
        ("benchmark-results.png", lambda: bar_chart(rng, 1000, 600, LIGHT)),
        ("build-times.png", lambda: bar_chart(rng, 900, 520, DARK)),
        ("cost-by-team.png", lambda: bar_chart(rng, 1100, 560, LIGHT)),
        ("latency-heatmap.png", lambda: heatmap(rng, 1200, 520, DARK)),
        ("confusion-matrix.png", lambda: heatmap(rng, 760, 700, LIGHT, square=True)),
        ("attention-weights.png", lambda: heatmap(rng, 700, 680, DARK, square=True)),
        ("embedding-clusters.png", lambda: scatter(rng, 900, 760, LIGHT)),
        ("tsne.png", lambda: scatter(rng, 800, 800, DARK)),
        (f"Screenshot {stamp} at 14.02.11.png", lambda: dashboard(rng, 1600, 900)),
        (f"Screenshot {stamp} at 09.47.33.png", lambda: dashboard(rng, 1440, 900)),
        ("grafana-incident.png", lambda: dashboard(rng, 1920, 1000)),
        ("settings-page.png", lambda: window(rng, 1280, 800, LIGHT)),
        ("admin-console.png", lambda: window(rng, 1280, 760, DARK)),
        ("staging-ui.png", lambda: window(rng, 1100, 700, LIGHT)),
        ("full-page.png", lambda: window(rng, 900, 2200, LIGHT)),
        ("flamegraph.png", lambda: flamegraph(rng, 1400, 600)),
        ("cpu-profile.png", lambda: flamegraph(rng, 1200, 520)),
        ("architecture.png", lambda: diagram(rng, 1200, 600, DARK)),
        ("pipeline.png", lambda: diagram(rng, 1400, 480, LIGHT)),
        ("data-flow.png", lambda: diagram(rng, 1000, 640, LIGHT)),
        ("sparkline.png", lambda: line_chart(rng, 420, 160, DARK, "walk")),
        ("avatar.png", lambda: Canvas(96, 96, SERIES[rng.randrange(len(SERIES))])),
    ]
    return [(name, make().png()) for name, make in plan]


def documents(seed=1) -> list:
    """[(file name, bytes)]: what agents attach besides pictures."""
    rng = random.Random(seed)
    log = "".join(f"2026-10-13T14:{m:02d}:{s:02d}Z {lvl} {msg}\n" for m, s, lvl, msg in (
        (rng.randint(0, 59), rng.randint(0, 59), rng.choice(["INFO", "INFO", "WARN", "ERROR"]),
         rng.choice(["request served in 41 ms", "retrying fetch (attempt 2)", "cache miss for key user:812",
                     "timeout after 30s talking to search-7", "worker 3 restarted", "queue depth 1204"]))
        for _ in range(400)))
    log += ("2026-10-13T14:59:58Z ERROR request failed\nTraceback (most recent call last):\n"
            '  File "/srv/search/handler.py", line 88, in handle\n    hits = index.query(q, timeout=30)\n'
            '  File "/srv/search/index.py", line 212, in query\n    raise TimeoutError(f"no answer from {shard}")\n'
            "TimeoutError: no answer from search-7\n2026-10-13T14:59:59Z INFO worker 3 restarted\n")
    build = "".join(f"\x1b[32m✓\x1b[0m {step} \x1b[2m({rng.randint(1, 900)} ms)\x1b[0m\n" for step in (
        "lint", "typecheck", "unit tests", "integration tests")) + "\x1b[31m✗ e2e tests\x1b[0m\n  \x1b[1mExpected\x1b[0m 50 pages, got 49\n"
    tests = ("============ test session starts ============\ncollected 812 items\n\n"
             + "".join(f"tests/test_{rng.choice(['pager', 'search', 'auth', 'billing'])}.py {'.' * rng.randint(10, 60)}\n" for _ in range(30))
             + "\nFAILED tests/test_pager.py::test_last_page - AssertionError: 49 != 50\n"
             "======= 1 failed, 811 passed in 41.2s =======\n")
    csv = "run,step,loss,accuracy,latency_ms\n" + "".join(
        f"{r},{s},{math.exp(-s / 3000) + rng.random() * 0.05:.4f},{0.5 + 0.45 * (1 - math.exp(-s / 2000)):.4f},{rng.randint(30, 90)}\n"
        for r in range(3) for s in range(0, 10000, 100))
    config = json.dumps({"model": "ranker-v4", "batch_size": 256, "lr": 3e-4, "warmup": 500, "eval_every": 1000,
                         "features": ["clicks", "dwell", "freshness"], "shards": 16}, indent=2) + "\n"
    patch = ("diff --git a/pager.py b/pager.py\nindex 3f2a1c0..9b8e7d4 100644\n--- a/pager.py\n+++ b/pager.py\n"
             "@@ -38,9 +38,9 @@ from typing import Sequence\n \n def pages(items: Sequence, size: int) -> list:\n"
             '     """Splits items into pages of at most size items."""\n     out = []\n'
             "-    for start in range(0, len(items) - 1, size):\n+    for start in range(0, len(items), size):\n"
             "         out.append(items[start:start + size])\n     return out\n \n"
             "diff --git a/tests/test_pager.py b/tests/test_pager.py\nindex 1d4c2e9..77a0b31 100644\n"
             "--- a/tests/test_pager.py\n+++ b/tests/test_pager.py\n@@ -12,3 +12,8 @@ def test_first_page():\n"
             "     assert pages(list(range(10)), 3)[0] == [0, 1, 2]\n+\n+\n+def test_last_page():\n"
             "+    # the last item used to be dropped when it started a page of its own\n"
             "+    assert pages(list(range(50)), 7)[-1] == [49]\n")
    code = ('"""Retries with backoff for calls to the search shards."""\nimport random\nimport time\n\n'
            "RETRIES = 4  # attempts before giving up\n\n\nclass Backoff:\n"
            "    def __init__(self, base: float = 0.2, cap: float = 5.0):\n        self.base, self.cap = base, cap\n\n"
            "    def wait(self, attempt: int) -> float:\n"
            '        """Full jitter: a random wait up to base * 2 ** attempt, at most cap."""\n'
            "        return random.uniform(0, min(self.cap, self.base * 2 ** attempt))\n\n\n"
            "def call(fn, *args, backoff=None):\n    backoff = backoff or Backoff()\n"
            "    for attempt in range(RETRIES):\n        try:\n            return fn(*args)\n"
            "        except TimeoutError as e:\n            if attempt == RETRIES - 1:\n"
            '                raise RuntimeError(f"gave up after {RETRIES} tries") from e\n'
            "            time.sleep(backoff.wait(attempt))\n    return None\n")
    notes = ("# Postmortem: search latency, 13 October\n\n**Impact:** p99 latency went from 120 ms to *4.1 s* for "
             "40 minutes. No data was lost.\n\n## What happened\n\nThe new index was built without the `freshness` "
             "field, so every query fell back to a full scan. See the [runbook](https://example.com/runbook/search).\n\n"
             "## Timeline\n\n| Time | Event |\n|:-----|:------|\n| 14:02 | Deploy of index v41 |\n"
             "| 14:09 | Alert: p99 above 1 s |\n| 14:42 | Rolled back to v40 |\n\n## Fix\n\n- [x] Rebuilt the index\n"
             "- [x] Rolled back the deploy\n- [ ] Added a check to the deploy\n  - fail when a field is missing\n\n"
             "```python\nassert 'freshness' in index.fields, 'index built without freshness'\n```\n\n"
             "> Lesson: check the schema before switching traffic.\n")
    report = ('<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <title>Nightly report</title>\n'
              "  <style>\n    body { font: 15px/1.5 sans-serif; margin: 2em; }\n    .bad { color: #c00; }\n  </style>\n</head>\n"
              '<body>\n  <!-- made by the nightly job -->\n  <h1>Nightly report</h1>\n'
              '  <p>All green except <span class="bad">the pager tests</span>.</p>\n'
              '  <a href="https://example.com/runs/812">Run 812</a>\n</body>\n</html>\n')
    return [("server.log", log.encode()), ("pytest-output.txt", tests.encode()), ("metrics.csv", csv.encode()),
            ("config.json", config.encode()), ("fix-pager.patch", patch.encode()), ("postmortem.md", notes.encode()),
            ("nightly-report.html", report.encode()), ("retry.py", code.encode()), ("build.log", build.encode()),
            ("checkpoint-step-9000.bin", rng.randbytes(2_400_000))]


def write(out: str, seed=1) -> list:
    """Writes every file into out, and a folder of results, eval-results/. [(path, is a picture)]."""
    os.makedirs(out, exist_ok=True)
    made = []
    for group, files in (("pictures", pictures(seed)), ("documents", documents(seed))):
        for name, data in files:
            with open(os.path.join(out, name), "wb") as f:
                f.write(data)
            made.append((os.path.join(out, name), group == "pictures"))
    folder = os.path.join(out, "eval-results")
    os.makedirs(os.path.join(folder, "plots"), exist_ok=True)
    for name in ("metrics.csv", "config.json"):
        with open(os.path.join(out, name), "rb") as src, open(os.path.join(folder, name), "wb") as dst:
            dst.write(src.read())
    with open(os.path.join(out, "loss-curve.png"), "rb") as src, open(os.path.join(folder, "plots", "loss-curve.png"), "wb") as dst:
        dst.write(src.read())
    made.append((folder, False))
    return made


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    started = time.time()
    made = write(a.out, a.seed)
    print(f"{len(made)} files in {a.out} in {time.time() - started:.1f} s")
