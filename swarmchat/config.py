"""Server settings. The defaults below, then a JSON config file (`swarmchat serve --config FILE`), then command-line
flags, each overriding the one before. Every setting is also a flag: `max_wait` is `--max-wait`.
"""
from __future__ import annotations

import json
import math

from . import DATA_DIR

DEFAULTS = {
    "data": DATA_DIR,         # the folder that holds everything
    "host": "127.0.0.1",      # 0.0.0.0 accepts other machines
    "port": 8484,
    "tls_cert": None,         # serve HTTPS with this certificate and key
    "tls_key": None,
    "fsync": False,           # force every write to disk: survives a power cut, slower
    "verbose": False,         # log every request
    "max_text": 20_000,       # longest message, in characters
    "max_wait": 300,          # longest one wait request is held, in seconds. Clients simply ask again.
    "max_body": 1 << 20,      # largest request, in bytes (a message's files have their own limits below)
    "max_attachment": 100_000_000,  # largest attached file or zipped folder, in bytes
    "max_attachments": 20,    # most files on one message
    "request_timeout": 30.0, # idle socket and TLS handshake timeout, in seconds. A request's line and headers must
                              # all arrive within it, and its body within it plus the body's size at min_rate.
    "min_rate": 16_384,       # slowest a request's body may arrive, in bytes a second
    "max_connections": 4096,  # most connections open at once. Each waiting agent holds one. Lowered to fit the
                              # number of files the system lets the server open.
    "max_requests": 32,       # most requests one user may have under way at once, waits and uploads included
    "max_page": 1000,         # most messages one read returns. The client asks again for the rest.
    "min_free": 1_000_000_000,  # bytes of disk the server keeps free: messages and files that would use them are
                                # refused. 0 turns this off.
    "quota": 0,               # most bytes of files one user may have stored at once. 0 is no limit.
    "default_notify": "all",  # alert level for chats nobody has set: all, mentions or none
    "save_every": 1.0,        # how often read positions are saved, in seconds
    "segment": 10_000,        # lines per log file. Fixed when the data folder is made: later changes are ignored.
}


def load(path: str | None = None, **flags) -> dict:
    """The settings to run with. Flags that are None were not given, so they do not override anything."""
    settings = dict(DEFAULTS)
    if path:
        with open(path, encoding="utf-8") as f:
            given = json.load(f)
        unknown = sorted(set(given) - set(DEFAULTS))
        if unknown:
            raise SystemExit(f"{path}: unknown settings {', '.join(unknown)}. Known: {', '.join(DEFAULTS)}.")
        settings.update(given)
    settings.update({k: v for k, v in flags.items() if v is not None})
    if not settings["data"]:
        raise SystemExit("Say where the data lives: --data DIR, or \"data\" in the config file.")
    if settings["default_notify"] not in ("all", "mentions", "none"):
        raise SystemExit("default_notify is one of all, mentions, none.")
    for key in ("port", "max_text", "max_body", "max_attachment", "max_attachments", "segment", "max_connections",
                "max_requests", "max_page", "min_free", "quota"):
        least = 0 if key in ("port", "min_free", "quota") else 1
        if not isinstance(settings[key], int) or isinstance(settings[key], bool) or settings[key] < least:
            raise SystemExit(f"{key} must be a whole number" + (" of at least 1." if least else " of at least 0."))
    for key in ("max_wait", "save_every", "request_timeout", "min_rate"):
        if not isinstance(settings[key], (int, float)) or not math.isfinite(settings[key]) or settings[key] <= 0:
            raise SystemExit(f"{key} must be a number above 0.")
    return settings
