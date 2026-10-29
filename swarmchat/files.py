"""Attachments.

Every file is stored once, named by the SHA-256 of its bytes: DIR/blobs/3f/3fa9.... Messages refer to their files by
a random attachment id, never by the hash. A download is allowed only to members of the message's chat, and nobody
can ask the server whether some file exists. A blob is removed when no message refers to it any more.

A folder travels as one zip and is unpacked on download. Unpacking refuses any entry that would land outside the
target folder, more than MAX_ENTRIES entries, and more than the disk can take while keeping SPARE bytes free: each entry
counted at the whole disk blocks it fills, and at one of the disk's free file entries (inodes).
"""
from __future__ import annotations

import collections
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import zipfile

CHUNK = 1 << 20
MAX_ENTRIES = 100_000  # files and folders in one folder attachment
SPARE = 64 << 20  # bytes of disk an unpacked folder or a download must leave free
MIME = re.compile(r"[A-Za-z0-9][\w!#$&^.+-]*/[A-Za-z0-9][\w!#$&^.+-]*", re.ASCII)


def mime(kind) -> str | None:
    """A media type such as text/plain, or None for anything else: it goes into a reply's Content-Type header, where
    a line break or other control character would end the header early."""
    return kind if isinstance(kind, str) and len(kind) <= 100 and MIME.fullmatch(kind) else None


def safe_name(name: str) -> str:
    """A file name that cannot point anywhere else: no folders, no '..', no control characters."""
    name = os.path.basename(str(name).replace("\\", "/")).strip()
    name = "".join(c for c in name if c.isprintable())[:200]
    return name if name not in ("", ".", "..") else "file"


class Blobs:
    """The files on disk, and how many messages refer to each."""

    def __init__(self, root: str, read_only: bool = False):
        self.dir, self.tmp = os.path.join(root, "blobs"), os.path.join(root, "uploads")
        if not read_only:
            os.makedirs(self.dir, 0o700, exist_ok=True)
            os.makedirs(self.tmp, 0o700, exist_ok=True)
            for name in os.listdir(self.tmp):  # uploads cut off by a crash
                os.unlink(os.path.join(self.tmp, name))
        self.refs = collections.Counter()
        self.lock = threading.Lock()

    def path(self, sha: str) -> str:
        return os.path.join(self.dir, sha[:2], sha)

    def receive(self, read, size: int) -> tuple:
        """Reads exactly `size` bytes with read(n) into a temporary file, hashing as it goes. (temp path, sha256).
        If the bytes stop early, raises EOFError and leaves nothing behind."""
        fd, tmp = tempfile.mkstemp(dir=self.tmp, suffix=".part")
        digest, left = hashlib.sha256(), size
        try:
            with os.fdopen(fd, "wb") as f:
                while left:
                    chunk = read(min(CHUNK, left))
                    if not chunk:
                        raise EOFError("the upload ended before all its bytes arrived")
                    f.write(chunk)
                    digest.update(chunk)
                    left -= len(chunk)
        except BaseException:
            os.unlink(tmp)
            raise
        return tmp, digest.hexdigest()

    def keep(self, tmp: str, sha: str):
        """Makes a received file the blob for its hash, or drops it if that blob is already stored, and counts one
        more reference. Counting under the same lock as release() means a blob cannot vanish between the two."""
        with self.lock:
            dest = self.path(sha)
            if os.path.exists(dest):
                os.unlink(tmp)
            else:
                os.makedirs(os.path.dirname(dest), 0o700, exist_ok=True)
                os.replace(tmp, dest)
            self.refs[sha] += 1

    def release(self, sha: str):
        """One reference fewer. The last one removes the file."""
        with self.lock:
            self.refs[sha] -= 1
            if self.refs[sha] <= 0:
                del self.refs[sha]
                try:
                    os.unlink(self.path(sha))
                except FileNotFoundError:
                    pass

    def prune(self):
        """At start: removes blobs no message refers to, left by a crash between storing a file and its message."""
        for sub in os.listdir(self.dir):
            for sha in os.listdir(os.path.join(self.dir, sub)):
                if not self.refs[sha]:
                    os.unlink(os.path.join(self.dir, sub, sha))


def opened_path(fd: int) -> str | None:
    """Where the file open on `fd` is, from the system: what was opened, whatever its path has become since. None
    where the system cannot say."""
    try:
        if sys.platform.startswith("linux"):
            return os.readlink(f"/proc/self/fd/{fd}")
        if sys.platform == "darwin":
            import fcntl
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            final = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            final.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            n = final(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
            path = buffer.value if 0 < n < len(buffer) else None
            return path[4:] if path and path.startswith("\\\\?\\") and path[5:7] == ":\\" else path
    except (OSError, ValueError, AttributeError, ImportError):
        pass
    except Exception:  # noqa: BLE001 - ctypes.ArgumentError: the system could not say
        pass
    return None


def pack(folder: str) -> str:
    """Zips a folder, with relative paths and empty folders, into a temporary file. The caller removes it.
    A link to something inside the folder is sent as what it points to. A link to anything outside it is refused,
    so a folder cannot carry files from elsewhere on the sender's disk. A link back to a folder above it on the same
    path is skipped, so a loop of links cannot go on forever. Entries carry the files' own times, so the same folder
    always zips to the same bytes and is stored once however often it is sent.

    Each file is opened once, then checked where the system says the opened file is, and its bytes come from that same
    open file. A link switched to a file outside the folder between a check and a read cannot slip one in."""
    base = where = os.path.realpath(folder)
    try:  # the folder as the system names open files, which may differ from realpath in case
        fd = os.open(base, os.O_RDONLY)
        try:
            where = opened_path(fd) or base
        finally:
            os.close(fd)
    except OSError:  # Windows cannot open a folder this way: it compares without case anyway
        pass

    def inside(path: str, real: str | None = None) -> str:
        real, root = (real, where) if real else (os.path.realpath(path), base)
        top = os.path.normcase(root.rstrip(os.sep) + os.sep)
        if os.path.normcase(real) != os.path.normcase(root) and not os.path.normcase(real).startswith(top):
            raise ValueError(f"{path} links to {real}, outside the folder. Copy it in, or remove the link")
        return real

    def add(z: zipfile.ZipFile, full: str):
        try:  # not blocking, so a file switched to a pipe cannot hang the send
            fd = os.open(full, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:  # a broken link, or gone since the folder was listed: nothing to send
            return
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return
            where = opened_path(fd)
            if where is None:  # no way to ask the system: the open file must be the one the checked path names
                real = inside(full)
                same = os.stat(real)
                if (same.st_dev, same.st_ino) != (st.st_dev, st.st_ino):
                    raise ValueError(f"{full} changed while the folder was being sent")
            else:
                inside(full, where)
            info = zipfile.ZipInfo(os.path.relpath(full, folder).replace(os.sep, "/"), time.localtime(st.st_mtime)[:6])
            info.external_attr, info.file_size = (st.st_mode & 0xFFFF) << 16, st.st_size  # as ZipFile.write would
            info.compress_type = zipfile.ZIP_DEFLATED
            with z.open(info, "w") as out:
                shutil.copyfileobj(f, out, 1024 * 8)

    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    above = {}  # folder as walked -> real paths of it and every folder above it
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(folder, followlinks=True):
                chain = above[root] = above.get(os.path.dirname(root), frozenset()) | {os.path.realpath(root)}
                dirs[:] = sorted(d for d in dirs if inside(os.path.join(root, d)) not in chain)
                rel = os.path.relpath(root, folder)
                if rel != "." and not dirs and not files:
                    stamp = time.localtime(max(os.path.getmtime(root), 315532800))[:6]  # zip times start in 1980
                    z.writestr(zipfile.ZipInfo(rel.replace(os.sep, "/") + "/", stamp), "")
                for name in sorted(files):
                    add(z, os.path.join(root, name))
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def unpack(zip_path: str, dest: str):
    """Unzips into dest, refusing absolute paths, '..' and anything else that would land outside it, and a folder of
    more entries or bytes than the limits above. The sizes it declares are the most each entry can write: zipfile
    stops there."""
    base = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError(f"the folder holds {len(infos):,} entries, more than {MAX_ENTRIES:,}")
        block, inodes = 4096, None
        if hasattr(os, "statvfs"):
            disk = os.statvfs(base)
            block, inodes = disk.f_frsize or disk.f_bsize or 4096, disk.f_favail if disk.f_files else None
        # A one-byte file still fills a whole block, and its entry in its folder takes room too: a block each.
        total = sum(-(-info.file_size // block) * block + block for info in infos)
        free = shutil.disk_usage(base).free
        if total > free - SPARE:
            raise ValueError(f"the folder takes {total:,} bytes on disk, and this disk has {free:,} free")
        if inodes is not None and len(infos) > inodes - 1000:
            raise ValueError(f"the folder holds {len(infos):,} entries, and this disk has room for {inodes:,} more")
        for info in infos:
            parts = info.filename.replace("\\", "/").split("/")
            target = os.path.realpath(os.path.join(base, *parts))
            if info.filename.startswith(("/", "\\")) or ".." in parts or ":" in parts[0] or \
                    (target != base and not target.startswith(base + os.sep)):
                raise ValueError(f"the folder holds an unsafe path: {info.filename}")
        z.extractall(base)
