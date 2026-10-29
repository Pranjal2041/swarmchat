"""An OS-held, nonblocking lease on a server's data directory.

The lock file stays in place; the operating system releases ownership on exit,
including SIGKILL. Never unlink a live lock file: another process could lock a
different inode at the same path.
"""
import os
import sys


class DirectoryLock:
    def __init__(self, root: str):
        os.makedirs(root, 0o700, exist_ok=True)
        try:  # the folder holds every message and the tokens: its owner alone may look in, whatever was there before
            os.chmod(root, 0o700)
        except OSError as exc:
            print(f"warning: could not make {root} private to its owner: {exc}", file=sys.stderr)
        self.handle = os.fdopen(os.open(os.path.join(root, ".server.lock"), os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                if not os.fstat(self.handle.fileno()).st_size:
                    self.handle.write(b"\0")
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            raise OSError(f"Cannot lock {root}; another server may already own this data directory.") from exc

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None
