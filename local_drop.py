#!/usr/bin/env python3
"""
Local Drop - send photos, videos and any other files from your phone to a folder on this PC.

How to use
  1. Double-click "Start Local Drop.bat" (or run: python local_drop.py).
  2. A page opens on your PC with a QR code. Scan it with your phone.
     Your phone must be on the same Wi-Fi / network as this PC.
  3. On the phone:
       - the big shutter button takes a photo
       - Video records a video
       - Files sends anything already on the phone (documents, PDFs, videos,
         text files, zips...), several at once if you like
     Everything uploads by itself, untouched, at full resolution.
  4. Choose the main save folder on the PC page ("Choose another folder").
     From the phone, tap the folder bar to pick or create folders inside it.
     Both choices are remembered between runs.
  5. Stop it with the Stop button on either page, or by closing the window.

Naming
  Photos and videos taken from the page are named IMG_/VID_ + date and time.
  Files you send keep their own names. If that name is already taken, the new
  one is saved as "name (2).ext". Nothing is ever overwritten.

Options
  --dir PATH      Folder to start with until you choose one on the PC page.
                  Default: your Pictures\\Local Drop folder.
  --port N        Port to listen on. Default: 8000.
  --no-browser    Don't open the PC page at start.

Settings are kept in %APPDATA%\\LocalDrop\\settings.json.
Needs Python 3.8 or newer. No extra packages.

If your phone can't connect: when Windows Firewall asks, allow Python on
Private networks, and make sure this Wi-Fi is set to "Private" in Windows
network settings (Public networks block incoming connections).
"""

import argparse
import datetime
import html
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CHUNK = 1024 * 1024
SPACE_MARGIN = 50 * 1024 * 1024  # always leave this much free on the disk
UPLOAD_IDLE_TIMEOUT = 120        # seconds a phone may go silent mid-upload
BAD_NAME_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
SKIP_IN_COUNTS = {"desktop.ini", "thumbs.db", ".ds_store"}
MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic", "image/heif": ".heif",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "video/3gpp": ".3gp", "video/3gpp2": ".3g2", "video/x-matroska": ".mkv",
}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def sniff_image_ext(head: bytes):
    """Return a file extension if the bytes look like a known image, else None."""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return ".heic"
    return None


def _exif_orientation(tiff: bytes):
    if tiff[:2] == b"II":
        e = "<"
    elif tiff[:2] == b"MM":
        e = ">"
    else:
        return None
    ifd = struct.unpack(e + "I", tiff[4:8])[0]
    count = struct.unpack(e + "H", tiff[ifd:ifd + 2])[0]
    for k in range(count):
        p = ifd + 2 + 12 * k
        if struct.unpack(e + "H", tiff[p:p + 2])[0] == 0x0112:
            return struct.unpack(e + "H", tiff[p + 8:p + 10])[0]
    return None


def _jpeg_size(b: bytes):
    i, orientation, size = 2, 1, None
    while i + 4 <= len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack(">H", b[i + 2:i + 4])[0]
        seg = b[i + 4:i + 2 + seglen]
        if marker == 0xE1 and seg[:6] == b"Exif\0\0":
            orientation = _exif_orientation(seg[6:]) or orientation
        elif marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", seg[1:5])
            size = (w, h)
            break
        elif marker == 0xDA:
            break
        i += 2 + seglen
    if not size:
        return None
    return (size[1], size[0]) if orientation in (5, 6, 7, 8) else size


def _heif_size(b: bytes):
    """HEIC/AVIF: the largest 'ispe' box is the full picture (iPhones store it as tiles)."""
    best, i = None, b.find(b"ispe")
    while i != -1 and i + 16 <= len(b):
        w, h = struct.unpack(">II", b[i + 8:i + 16])
        if best is None or w * h > best[0] * best[1]:
            best = (w, h)
        i = b.find(b"ispe", i + 4)
    if best:
        r = b.find(b"irot")
        if r != -1 and r + 4 < len(b) and (b[r + 4] & 3) in (1, 3):
            best = (best[1], best[0])
    return best


def _webp_size(b: bytes):
    kind = b[12:16]
    if kind == b"VP8 ":
        w, h = struct.unpack("<HH", b[26:30])
        return (w & 0x3FFF, h & 0x3FFF)
    if kind == b"VP8L":
        bits = int.from_bytes(b[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if kind == b"VP8X":
        return (int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1)
    return None


def image_size(path: Path):
    """(width, height) of a saved photo as it's meant to be viewed, read from its header. None if unknown."""
    try:
        with open(path, "rb") as f:
            head = f.read(1024 * 1024)
        if head[:3] == b"\xff\xd8\xff":
            return _jpeg_size(head)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return _webp_size(head)
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[4:8] == b"ftyp":
            return _heif_size(head)
    except (OSError, struct.error, ValueError, IndexError):
        pass
    return None


def ext_from(name: str, ctype: str, default: str) -> str:
    """Pick an extension for a photo/video from its phone-side name or its type."""
    ext = os.path.splitext(name or "")[1].lower()
    if 1 < len(ext) <= 8 and ext[1:].isalnum():
        return ".jpg" if ext == ".jpeg" else ext
    return MIME_EXT.get((ctype or "").split(";")[0].strip().lower(), default)


def clean_filename(raw: str) -> str:
    """Make a phone-side file name safe to save on Windows, keeping it recognisable."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]          # no folders
    name = "".join("_" if (c in BAD_NAME_CHARS or ord(c) < 32) else c for c in name)
    name = name.strip().rstrip(". ")
    if not name.strip("."):
        return "file"
    stem, ext = os.path.splitext(name)
    if len(ext) > 16:                                                   # not a real extension
        stem, ext = name, ""
    if stem.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    stem = stem[:150].rstrip(". ") or "file"
    return stem + ext


def clean_dirname(raw: str) -> str:
    """Make a folder name that came from the phone safe for Windows, keeping it recognisable."""
    name = "".join("_" if (c in BAD_NAME_CHARS or ord(c) < 32) else c for c in (raw or ""))
    name = name.strip().rstrip(". ")[:80].rstrip(". ")
    if not name.strip("."):
        return "_"                                                       # also turns "." and ".." harmless
    if name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def check_folder_name(name: str):
    """Return (clean_name, None) if usable as a Windows folder name, else (None, reason)."""
    name = (name or "").strip()
    if not name:
        return None, "Type a name for the new folder."
    if len(name) > 80:
        return None, "Keep folder names under 80 characters."
    if any(c in BAD_NAME_CHARS or ord(c) < 32 for c in name):
        return None, 'Folder names can\'t contain < > : " / \\ | ? *'
    if name.startswith("."):
        return None, "Folder names can't start with a dot."
    if name.endswith((".", " ")):
        return None, "Folder names can't end with a dot or a space."
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        return None, "Windows reserves that name. Pick another."
    return name, None


def prepare_folder(raw):
    """Turn a typed or picked path into a usable save folder (created if needed).
    Returns (Path, None) or (None, reason)."""
    text = str(raw or "").strip().strip('"').strip()
    if not text:
        return None, "Type the full path of a folder, like D:\\Photos."
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        return None, "Use a full path that starts with a drive, like D:\\Photos."
    try:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".localdrop_write_test"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        return None, f"Local Drop can't save to {path} ({e.strerror or e})."
    return path, None


def count_files(folder: Path):
    try:
        with os.scandir(folder) as it:
            return sum(1 for e in it
                       if e.is_file(follow_symlinks=False)
                       and not e.name.endswith(".part")
                       and e.name.lower() not in SKIP_IN_COUNTS)
    except OSError:
        return None


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def stamp(prefix: str) -> str:
    return datetime.datetime.now().strftime(f"{prefix}_%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# PC helpers
# ---------------------------------------------------------------------------

def lan_addresses():
    """Best guess at this PC's address on the local network, plus any others."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # picks the default network card; sends nothing
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    usable = [ip for ip in found if not ip.startswith(("127.", "169.254."))]
    return usable or ["127.0.0.1"]


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    return Path(base) if base else Path.home() / ".config"


def settings_path() -> Path:
    return app_data_dir() / "LocalDrop" / "settings.json"


# Before it was renamed, Local Drop was called Phone Cam. These let an existing
# setup carry over: the saved folder choice and the old default save folder.
LEGACY_SETTINGS = app_data_dir() / "PhoneCam" / "settings.json"
LEGACY_DEFAULT_DIR = Path.home() / "Pictures" / "PhoneCam"
DEFAULT_DIR = Path.home() / "Pictures" / "Local Drop"


def load_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# Runs in its own small Python process so the Windows folder window never
# fights with the web server's threads.
PICKER_SCRIPT = r"""
import json, sys
try:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    path = filedialog.askdirectory(parent=root, initialdir=sys.argv[1], mustexist=False,
                                   title="Choose where Local Drop saves files")
    root.destroy()
    print(json.dumps({"path": path or ""}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""


def ask_for_folder(start: Path):
    """Show the folder picker on this PC. Returns (picked_path_or_empty, problem_or_None)."""
    try:
        r = subprocess.run([sys.executable, "-c", PICKER_SCRIPT, str(start)],
                           capture_output=True, timeout=900)
        out = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return "", "no-picker"
    if "error" in out:
        return "", "no-picker"
    return out.get("path", ""), None


def open_folder(path: Path) -> bool:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # opens File Explorer
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class NamePicker:
    """Hands out free file names ("name.ext", "name (2).ext", ...), safe across parallel uploads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reserved = set()

    def reserve(self, folder: Path, filename: str) -> Path:
        stem, ext = os.path.splitext(filename)
        with self.lock:
            n = 1
            while True:
                name = filename if n == 1 else f"{stem} ({n}){ext}"
                path = folder / name
                key = str(path).lower()
                if key not in self.reserved and not path.exists():
                    self.reserved.add(key)
                    return path
                n += 1

    def release(self, path: Path):
        with self.lock:
            self.reserved.discard(str(path).lower())


class App:
    def __init__(self, save_dir: Path, default_folder: str, settings_file: Path):
        self.save_dir = save_dir
        self.default_folder = default_folder
        self.settings_file = settings_file
        self.lock = threading.Lock()
        self.picker_lock = threading.Lock()
        self.names = NamePicker()
        self.active_parts = set()   # .part files being written right now
        self.own_ips = set(lan_addresses())
        self.stopping = False
        self.stop_message = "Stopped."

    def persist(self):
        data = {"save_dir": str(self.save_dir), "default_folder": self.default_folder}
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.settings_file)
        except OSError as e:
            print(f"  [{now()}] Couldn't remember this setting for next time: {e}")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionError, socket.timeout)):
            return  # a phone walking away mid-request isn't worth a message
        print(f"  [{now()}] Problem handling a request from {client_address[0]}: {err!r}")


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "LocalDrop/1.4"

    # Set by main()
    app: App = None
    phone_page: bytes = b""
    pc_page: bytes = b""

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; saves are logged by hand

    # -- responses ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict):
        self._send(code, json.dumps(data).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str, **extra):
        self._json(code, {"ok": False, "error": message, **extra})

    def read_json(self, limit: int = 8192):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > limit:
                raise ValueError
            raw = self.rfile.read(length) if length > 0 else b""
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None

    def from_this_pc(self) -> bool:
        ip = self.client_address[0]
        return ip.startswith("127.") or ip == "::1" or ip in self.app.own_ips

    # -- folders -----------------------------------------------------------

    def resolve_folder(self, rel: str, root: Path = None):
        """Map a phone-side path like 'Trips/Rila' to a real folder inside the save folder.
        Returns (path, normalised_rel) or (None, None) if it's missing or outside."""
        root = root or self.app.save_dir
        parts = [p for p in (rel or "").replace("\\", "/").split("/") if p.strip()]
        for p in parts:
            if p in (".", "..") or p.startswith(".") or ":" in p:
                return None, None
        target = root.joinpath(*parts).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None, None
        if not target.is_dir():
            return None, None
        return target, "/".join(parts)

    def check_target(self, folder_rel: str, length: int):
        """Shared by /api/check and /upload.
        Returns (folder, rel, None) when it's fine to save, or (None, None, (code, message))."""
        folder, rel = self.resolve_folder(folder_rel)
        if folder is None:
            return None, None, (404, "The folder you picked isn't on your PC any more. "
                                     "Pick another folder, then tap Try again.")
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            free = None
        if free is not None and length > free - SPACE_MARGIN:
            return None, None, (507, f"Your PC doesn't have room for this. It's {human_size(length)} "
                                     f"and the disk has {human_size(max(free - SPACE_MARGIN, 0))} free.")
        return folder, rel, None

    def list_folders(self, query):
        root = self.app.save_dir
        target, rel = self.resolve_folder(query.get("path", [""])[0], root)
        if target is None:
            return self._error(404, "That folder isn't on your PC any more.")
        folders = []
        try:
            with os.scandir(target) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
            for e in entries:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")):
                    folders.append({"name": e.name, "files": count_files(Path(e.path))})
        except OSError:
            return self._error(500, "Your PC wouldn't let Local Drop open that folder.")
        self._json(200, {"ok": True, "path": rel, "abs": str(target), "root_name": root.name or str(root),
                         "files": count_files(target), "folders": folders})

    def create_folder(self):
        data = self.read_json(4096)
        if data is None:
            return self._error(400, "Local Drop couldn't read that request. Reload the page and try again.")
        parent, parent_rel = self.resolve_folder(str(data.get("parent", "")))
        if parent is None:
            return self._error(404, "The folder you were in isn't on your PC any more.")
        name, problem = check_folder_name(str(data.get("name", "")))
        if problem:
            return self._error(400, problem)

        new = parent / name
        existed = new.is_dir()
        if not existed:
            try:
                new.mkdir()
            except FileExistsError:
                return self._error(409, f"There's already a file called {name} here. Pick another name.")
            except OSError:
                return self._error(500, "Your PC wouldn't let Local Drop make that folder.")
            print(f"  [{now()}] New folder  {new}")
        rel = f"{parent_rel}/{name}" if parent_rel else name
        self._json(200, {"ok": True, "path": rel, "existed": existed})

    def drop_folder(self):
        """Make a fresh folder for a whole folder sent from the phone: "Holiday", or
        "Holiday (2)" if that name is taken, so a second send never mixes into the first."""
        data = self.read_json(4096)
        if data is None:
            return self._error(400, "Local Drop couldn't read that request. Reload the page and try again.")
        parent, parent_rel = self.resolve_folder(str(data.get("parent", "")))
        if parent is None:
            return self._error(404, "The folder you picked isn't on your PC any more. Pick another folder, "
                                    "then send it again.")
        base = clean_dirname(str(data.get("name", ""))).lstrip(".").strip() or "Folder"
        with self.app.lock:
            n = 1
            while True:
                name = base if n == 1 else f"{base} ({n})"
                new = parent / name
                if not new.exists():
                    try:
                        new.mkdir()
                        break
                    except FileExistsError:
                        pass
                    except OSError:
                        return self._error(500, "Your PC wouldn't let Local Drop make that folder.")
                n += 1
        print(f"  [{now()}] Receiving folder  {new}")
        rel = f"{parent_rel}/{name}" if parent_rel else name
        self._json(200, {"ok": True, "path": rel, "name": name})

    # -- settings ----------------------------------------------------------

    def settings_payload(self):
        app = self.app
        root = app.save_dir
        _, rel = self.resolve_folder(app.default_folder, root)
        return {"ok": True, "root": str(root), "root_name": root.name or str(root),
                "default_folder": rel or "", "this_pc": self.from_this_pc()}

    def set_default_folder(self):
        data = self.read_json()
        if data is None:
            return self._error(400, "Local Drop couldn't read that request. Reload the page and try again.")
        _, rel = self.resolve_folder(str(data.get("folder", "")))
        if rel is None:
            return self._error(404, "That folder isn't on your PC any more.")
        with self.app.lock:
            self.app.default_folder = rel
            self.app.persist()
        self._json(200, self.settings_payload())

    def use_root(self, path: Path):
        with self.app.lock:
            self.app.save_dir = path
            self.app.default_folder = ""
            self.app.persist()
        print(f"  [{now()}] Now saving to  {path}")

    def set_root(self):
        if not self.from_this_pc():
            return self._error(403, "Change the save location on the PC that runs Local Drop.")
        data = self.read_json()
        if data is None:
            return self._error(400, "Local Drop couldn't read that request. Reload the page and try again.")
        path, problem = prepare_folder(data.get("path", ""))
        if problem:
            return self._error(400, problem)
        self.use_root(path)
        self._json(200, self.settings_payload())

    def browse_root(self):
        if not self.from_this_pc():
            return self._error(403, "Change the save location on the PC that runs Local Drop.")
        if not self.app.picker_lock.acquire(blocking=False):
            return self._error(409, "The folder window is already open on your PC. Check the taskbar.")
        try:
            picked, problem = ask_for_folder(self.app.save_dir)
        finally:
            self.app.picker_lock.release()
        if problem:
            return self._error(501, "The folder window couldn't open on this PC. Type or paste the path instead.",
                               reason="no-picker")
        if not picked:
            return self._json(200, {**self.settings_payload(), "cancelled": True})
        path, problem = prepare_folder(picked)
        if problem:
            return self._error(400, problem)
        self.use_root(path)
        self._json(200, self.settings_payload())

    def open_root(self):
        if not self.from_this_pc():
            return self._error(403, "Open the folder from the PC that runs Local Drop.")
        if not open_folder(self.app.save_dir):
            return self._error(500, "Local Drop couldn't open the folder on this PC.")
        self._json(200, {"ok": True})

    def stop(self):
        data = self.read_json() or {}
        busy = len(self.app.active_parts)
        if busy and not data.get("force"):
            msg = ("A file is still being sent to this PC." if busy == 1
                   else f"{busy} files are still being sent to this PC.")
            return self._error(409, msg, busy=busy)
        where = "this PC" if self.from_this_pc() else f"the phone at {self.client_address[0]}"
        self.app.stop_message = f"Stopped from the Local Drop page on {where}."
        self.app.stopping = True
        self._json(200, {"ok": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # -- uploads -----------------------------------------------------------

    def precheck(self, query):
        """Cheap check the phone makes before sending, so a big video doesn't
        travel all the way just to be turned away."""
        try:
            size = max(0, int(query.get("size", ["0"])[0]))
        except ValueError:
            size = 0
        _, _, problem = self.check_target(query.get("folder", [""])[0], size)
        if problem:
            return self._error(*problem)
        self._json(200, {"ok": True})

    def upload(self, query):
        q = lambda key: query.get(key, [""])[0]
        kind = q("kind") if q("kind") in ("photo", "video") else "file"
        app = self.app

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            return self._error(411, "Your phone didn't say how big the file is. Reload the page and try again.")
        if length == 0 and kind != "file":
            return self._error(400, f"The {kind} arrived empty. Take it again.")

        folder, rel, problem = self.check_target(q("folder"), length)
        if problem:
            self.close_connection = True
            return self._error(*problem)

        # Files from a whole folder carry their subfolder path, e.g. "Day 1/Morning".
        sub = [clean_dirname(s) for s in q("sub").replace("\\", "/").split("/") if s.strip()]
        if sub:
            top, folder = folder, folder.joinpath(*sub)
            try:
                folder.resolve().relative_to(top)
                folder.mkdir(parents=True, exist_ok=True)
            except (ValueError, OSError):
                self.close_connection = True
                return self._error(500, "Your PC couldn't make a folder for this file. Skip it to carry on.")
            rel = "/".join([rel, *sub]) if rel else "/".join(sub)

        self.connection.settimeout(UPLOAD_IDLE_TIMEOUT)
        first = self.rfile.read(min(length, 64 * 1024)) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if kind == "photo":
            filename = stamp("IMG") + (sniff_image_ext(first) or ext_from(q("name"), ctype, ".jpg"))
        elif kind == "video":
            filename = stamp("VID") + ext_from(q("name"), ctype, ".mp4")
        else:
            filename = clean_filename(q("name"))

        # The bytes are written exactly as the phone sent them: no resizing,
        # no re-compression, metadata kept. Full resolution, always.
        final = app.names.reserve(folder, filename)
        part = final.with_name(final.name + ".part")
        received = 0
        app.active_parts.add(part)
        try:
            with open(part, "wb") as f:
                f.write(first)
                received = len(first)
                while received < length:
                    if app.stopping:
                        raise ConnectionError("Local Drop was stopped")
                    chunk = self.rfile.read(min(CHUNK, length - received))
                    if not chunk:
                        raise ConnectionError("upload cut off")
                    f.write(chunk)
                    received += len(chunk)
            os.replace(part, final)
        except (OSError, ConnectionError) as e:
            try:
                part.unlink()
            except OSError:
                pass
            print(f"  [{now()}] Upload of {final.name} from {self.client_address[0]} failed: {e}")
            if isinstance(e, (ConnectionError, socket.timeout)):
                return  # the phone already hung up
            return self._error(500, "Your PC couldn't write the file. Check the folder still exists "
                                    "and the disk isn't full.")
        finally:
            app.active_parts.discard(part)
            app.names.release(final)

        dims = image_size(final) if sniff_image_ext(first) else None
        where = f"{rel}/" if rel else ""
        px = f", {dims[0]} x {dims[1]} px" if dims else ""
        print(f"  [{now()}] Saved {where}{final.name}  ({human_size(received)}{px})  from {self.client_address[0]}")
        reply = {"ok": True, "name": final.name, "folder": rel, "size": received, "kind": kind}
        if dims:
            reply.update(width=dims[0], height=dims[1])
        self._json(200, reply)

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, self.phone_page, "text/html; charset=utf-8")
        elif url.path == "/api/folders":
            self.list_folders(parse_qs(url.query))
        elif url.path == "/api/check":
            self.precheck(parse_qs(url.query))
        elif url.path == "/api/settings":
            self._json(200, self.settings_payload())
        elif url.path == "/qr":
            self._send(200, self.pc_page, "text/html; charset=utf-8")
        elif url.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/upload":
            return self.upload(parse_qs(urlparse(self.path).query))
        routes = {
            "/api/folders": self.create_folder,
            "/api/drop-folder": self.drop_folder,
            "/api/default": self.set_default_folder,
            "/api/root": self.set_root,
            "/api/root/browse": self.browse_root,
            "/api/root/open": self.open_root,
            "/api/stop": self.stop,
        }
        handler = routes.get(path)
        if handler:
            return handler()
        self._send(404, b"Not found", "text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" media="print" onload="this.media=\'all\'" '
    'href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;700&display=swap">'
)

BASE_CSS = """
:root{
  --prussian:#173A5E;   /* cyanotype blue: page */
  --deep:#0F2A45;       /* darker blue: ink on paper */
  --paper:#EEF3F8;      /* print paper: text, shutter, sheet */
  --wash:#9CBDDD;       /* faded blue: secondary text */
  --ink-soft:#4F6B88;   /* secondary text on paper */
  --rule:rgba(238,243,248,.2);
  --rule-strong:rgba(238,243,248,.55);
  --rule-ink:rgba(15,42,69,.12);
  --alert:#FF9C8F;
  --alert-ink:#B23A2B;
  --font:"Schibsted Grotesk",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--prussian);color:var(--paper);font-family:var(--font);
  line-height:1.45;-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit}
[hidden]{display:none!important}
:focus-visible{outline:3px solid var(--paper);outline-offset:3px;border-radius:6px}
"""

FOLDER_ICON = ('<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2.2h8.8'
               'A1.5 1.5 0 0 1 21 9.7v8.8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z" fill="none" '
               'stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>')

POWER_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5v8" fill="none" stroke="currentColor" '
              'stroke-width="1.9" stroke-linecap="round"/><path d="M7.2 6.6a7.5 7.5 0 1 0 9.6 0" fill="none" '
              'stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>')

PHONE_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#173A5E">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Local Drop">
<title>Local Drop</title>
__FONT__
<style>
__BASE__
body{min-height:100vh;min-height:100dvh;display:flex;flex-direction:column;
  padding:max(18px,env(safe-area-inset-top)) 16px max(18px,env(safe-area-inset-bottom));
  -webkit-tap-highlight-color:transparent;overscroll-behavior:none}
header{display:flex;align-items:center;gap:12px}
h1{margin:0;flex:1;font-size:1.375rem;font-weight:700;letter-spacing:-.015em}
#count{font-size:.9375rem;color:var(--wash);font-variant-numeric:tabular-nums}
.stop{display:grid;place-items:center;width:36px;height:36px;border-radius:50%;padding:0;cursor:pointer;
  border:1.5px solid var(--rule-strong);background:transparent;color:var(--paper)}
.stop svg{width:18px;height:18px}
.ico{width:20px;height:20px;flex:none}

.dest{display:flex;align-items:center;gap:10px;width:100%;margin-top:12px;padding:10px 12px;
  background:rgba(15,42,69,.45);border:1px solid var(--rule);border-radius:10px;cursor:pointer;text-align:left}
.dest .where{flex:1;min-width:0}
.dest .label{display:block;font-size:.75rem;color:var(--wash)}
.dest .path{display:block;min-height:1.45em;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dest .change{font-size:.875rem;font-weight:500;color:var(--deep);background:var(--paper);
  border-radius:999px;padding:.3rem .8rem;flex:none}

main{flex:1;display:flex;flex-direction:column;min-height:0;margin-top:16px}
.print{flex:1;min-height:200px;display:grid;place-items:center;position:relative;overflow:hidden;
  background:var(--paper);padding:10px;border-radius:3px;
  box-shadow:0 1px 0 rgba(255,255,255,.25) inset, 0 14px 30px -18px rgba(5,15,30,.8)}
.print > img,.print > video{position:absolute;inset:10px;width:calc(100% - 20px);height:calc(100% - 20px);
  object-fit:contain;display:block;background:#dfe7ef}
.print > video{background:var(--deep)}
.print.is-empty{background:transparent;box-shadow:none;border:1.5px dashed var(--rule)}
.empty{max-width:24ch;text-align:center;color:var(--wash);font-size:.9375rem;margin:0}
.develop{animation:develop 1.1s cubic-bezier(.2,.7,.2,1) both}
@keyframes develop{
  from{filter:sepia(1) hue-rotate(175deg) saturate(4) brightness(1.35) contrast(.8);opacity:.35}
  to{filter:none;opacity:1}
}
.doc{display:grid;place-items:center;width:100%;height:100%;color:var(--deep);animation:fadein .5s ease both}
.doc-ext{font-size:clamp(3rem,17vw,5.5rem);font-weight:700;letter-spacing:-.045em;line-height:1;
  max-width:100%;overflow-wrap:anywhere;text-align:center}
.doc-ext svg{width:76px;height:76px;display:block}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.caption{margin:.6rem 0 0;font-size:.8125rem;color:var(--wash);min-height:1.2em;
  display:flex;justify-content:space-between;gap:12px}
.caption span:first-child{color:var(--paper);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.caption span:last-child{flex:none}

.strip{display:flex;gap:8px;overflow-x:auto;margin-top:12px;padding-bottom:2px;scrollbar-width:none}
.strip:empty{display:none}
.strip::-webkit-scrollbar{display:none}
.thumb{flex:none;position:relative;width:52px;height:52px;border:3px solid var(--paper);border-radius:2px;
  background:#dfe7ef;overflow:hidden;display:grid;place-items:center}
.thumb img,.thumb video{width:100%;height:100%;object-fit:cover;display:block}
.thumb.is-doc{background:var(--paper);color:var(--deep);font-size:.6875rem;font-weight:700;line-height:1.05;
  text-align:center;padding:2px;overflow-wrap:anywhere}
.thumb.is-video::after{content:"";position:absolute;right:4px;bottom:4px;border-style:solid;
  border-width:5px 0 5px 8px;border-color:transparent transparent transparent var(--paper);
  filter:drop-shadow(0 0 2px rgba(0,0,0,.7))}

.controls{position:relative;display:flex;flex-direction:column;align-items:center;gap:10px;margin-top:20px}
.deck{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;width:100%;max-width:340px}
.side{justify-self:center;position:relative;border:0;background:transparent;padding:0;cursor:pointer;
  border-radius:50%;touch-action:manipulation}
.side-ring{display:grid;place-items:center;width:58px;height:58px;border-radius:50%;
  border:1.5px solid var(--rule-strong);background:rgba(15,42,69,.45);transition:transform .12s ease}
.side:active .side-ring{transform:scale(.92)}
.side-ring svg{width:24px;height:24px}
.side-label{position:absolute;top:calc(100% + 5px);left:50%;transform:translateX(-50%);
  font-size:.8125rem;color:var(--wash);white-space:nowrap}

.shutter{position:relative;width:96px;height:96px;border:0;padding:0;border-radius:50%;
  background:transparent;cursor:pointer;touch-action:manipulation}
.shutter .ring{position:absolute;inset:0;width:100%;height:100%;transform:rotate(-90deg)}
.shutter .track{fill:none;stroke:var(--paper);stroke-width:4;opacity:.9}
.shutter .progress{fill:none;stroke:var(--paper);stroke-width:4;stroke-linecap:round;
  opacity:0;transition:stroke-dashoffset .15s linear}
.shutter .disc{position:absolute;inset:11px;border-radius:50%;background:var(--paper);
  display:grid;place-items:center;transition:transform .12s ease, background-color .2s ease}
.shutter .disc > *{grid-area:1/1}
.shutter:active .disc{transform:scale(.92)}
.shutter .check{width:34px;height:34px;opacity:0;scale:.6;
  transition:opacity .2s ease, scale .25s cubic-bezier(.3,1.6,.5,1)}
.shutter .pct{font-size:1rem;font-weight:700;color:var(--deep);font-variant-numeric:tabular-nums;opacity:0}
.shutter.is-busy .track{opacity:.25}
.shutter.is-busy .progress{opacity:1}
.shutter.is-busy .disc{background:#c9d8e7}
.shutter.is-busy .pct{opacity:1}
.shutter.is-done .check{opacity:1;scale:1}
.shutter.is-error .track{stroke:var(--alert);opacity:1}
.shutter.is-error .check{opacity:0}

.status{margin:12px 0 0;min-height:2.9em;max-width:34ch;text-align:center;font-size:.9375rem}
.status.is-error{color:var(--alert)}
.recover{display:flex;gap:10px}
.retry,.skip{font-weight:500;font-size:.9375rem;border-radius:999px;padding:.55rem 1.2rem;cursor:pointer}
.retry{color:var(--deep);background:var(--paper);border:0}
.skip{color:var(--paper);background:transparent;border:1.5px solid var(--rule-strong)}

.stopped{flex:1;display:flex;flex-direction:column;justify-content:center;align-items:flex-start;
  gap:.5rem;max-width:30ch;margin:0 auto;padding-bottom:10vh}
.stopped h2{margin:0;font-size:1.75rem;line-height:1.15;letter-spacing:-.02em}
.stopped p{margin:0;color:var(--wash)}
body.is-stopped .dest,body.is-stopped main,body.is-stopped .controls,body.is-stopped .stop,
body.is-stopped #count{display:none}

/* Files menu: files, or a whole folder */
.pick-menu{position:absolute;right:max(0px,calc(50% - 170px));bottom:calc(100% + 8px);z-index:5;min-width:240px;
  background:var(--paper);color:var(--deep);border-radius:14px;padding:6px;
  box-shadow:0 18px 40px -14px rgba(5,15,30,.8);animation:pop .16s cubic-bezier(.2,.8,.2,1)}
.pick-menu button{display:flex;align-items:center;gap:12px;width:100%;border:0;background:transparent;
  border-radius:10px;padding:10px 12px;text-align:left;cursor:pointer}
.pick-menu button:hover,.pick-menu button:focus-visible{background:#dde7f1;outline:none}
.pick-menu svg,.pick-menu .ico{width:22px;height:22px;flex:none;color:var(--prussian)}
.pick-menu b{display:block;font-weight:500}
.pick-menu small{display:block;font-size:.8125rem;color:var(--ink-soft)}
@keyframes pop{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* Drag and drop on computers */
.dropzone{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:16px;
  background:rgba(15,42,69,.97);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);pointer-events:none}
.dropzone::before{content:"";position:absolute;inset:16px;border:2px dashed var(--rule-strong);border-radius:20px}
.drop-card{position:relative;text-align:center;max-width:36rem}
.drop-title{margin:0;font-size:clamp(2.25rem,6vw,3.5rem);font-weight:700;letter-spacing:-.03em;line-height:1.05}
.drop-where{margin:.7rem 0 0;font-weight:500;overflow-wrap:anywhere}
.drop-note{margin:.25rem 0 0;color:var(--wash)}

/* Folder sheet */
dialog.sheet{position:fixed;margin:auto auto 0;width:100%;max-width:520px;max-height:86vh;max-height:86dvh;
  border:0;padding:18px 16px max(16px,env(safe-area-inset-bottom));
  background:var(--paper);color:var(--deep);border-radius:16px 16px 0 0;
  display:flex;flex-direction:column;animation:rise .22s cubic-bezier(.2,.8,.2,1)}
dialog.sheet:not([open]){display:none}
dialog.sheet::backdrop{background:rgba(8,20,36,.6)}
@keyframes rise{from{transform:translateY(24px);opacity:0}to{transform:none;opacity:1}}
.sheet :focus-visible{outline-color:var(--prussian)}
.sheet-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
.sheet h2{margin:0;font-size:1.125rem;font-weight:700;letter-spacing:-.01em}
.close{border:0;background:transparent;padding:6px;margin:-6px;border-radius:8px;cursor:pointer;color:var(--ink-soft)}
.close svg{width:22px;height:22px;display:block}
.crumbs{display:flex;flex-wrap:wrap;align-items:center;gap:2px 4px;margin-top:12px;font-size:.9375rem}
.crumbs button{border:0;background:transparent;padding:4px 2px;cursor:pointer;color:var(--prussian);
  text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--rule-ink)}
.crumbs [aria-current]{color:var(--deep);font-weight:700;text-decoration:none;cursor:default}
.crumbs .sep{color:var(--ink-soft)}
.abs{margin:.15rem 0 0;font-size:.75rem;color:var(--ink-soft);overflow-wrap:anywhere}
.folder-list{list-style:none;margin:12px 0 0;padding:0;overflow-y:auto;flex:1 1 auto;min-height:56px;
  border-top:1px solid var(--rule-ink)}
.folder-list li{border-bottom:1px solid var(--rule-ink)}
.folder-list button{display:flex;align-items:center;gap:12px;width:100%;padding:13px 2px;border:0;
  background:transparent;cursor:pointer;text-align:left}
.folder-list .name{flex:1;min-width:0;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.folder-list .meta{font-size:.8125rem;color:var(--ink-soft);font-variant-numeric:tabular-nums}
.folder-list .chev{width:16px;height:16px;color:var(--ink-soft)}
.folder-list .none{padding:16px 2px;color:var(--ink-soft);font-size:.9375rem}
.folder-list[aria-busy="true"]{opacity:.5}
.new-folder{display:flex;gap:8px;margin-top:14px}
.new-folder input{flex:1;min-width:0;font:inherit;font-size:16px;color:var(--deep);background:#fff;
  border:1.5px solid var(--rule-ink);border-radius:10px;padding:.6rem .75rem}
.new-folder input:focus{outline:none;border-color:var(--prussian)}
.new-folder button{border:1.5px solid var(--prussian);background:transparent;color:var(--prussian);
  font-weight:500;border-radius:10px;padding:0 1rem;cursor:pointer}
.sheet-msg{margin:.5rem 0 0;min-height:1.3em;font-size:.875rem;color:var(--ink-soft)}
.sheet-msg.is-error{color:var(--alert-ink)}
.use{margin-top:6px;border:0;border-radius:12px;padding:.85rem 1rem;font-weight:700;
  background:var(--prussian);color:var(--paper);cursor:pointer}
.use:disabled{background:transparent;color:var(--ink-soft);border:1.5px solid var(--rule-ink);cursor:default;font-weight:500}

.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (prefers-reduced-motion:reduce){
  .develop,.doc,dialog.sheet,.pick-menu{animation:none}
  .shutter .check,.shutter .disc,.shutter .progress,.side-ring{transition:none}
}
@media (min-width:600px){
  body{max-width:520px;margin:0 auto}
}
</style>
</head>
<body>
<header>
  <h1>Local Drop</h1>
  <span id="count">0 sent</span>
  <button class="stop" id="stop" type="button" aria-label="Stop Local Drop">__POWER_ICON__</button>
</header>

<button class="dest" id="dest" type="button" aria-haspopup="dialog">
  __FOLDER_ICON__
  <span class="where">
    <span class="label">Saving to</span>
    <span class="path" id="dest-path"></span>
  </span>
  <span class="change">Change</span>
</button>

<main>
  <div class="print is-empty" id="print">
    <p class="empty">Nothing sent yet. The last thing you send shows up here.</p>
  </div>
  <p class="caption" id="caption" aria-hidden="true"></p>
  <div class="strip" id="strip" aria-label="Sent this session"></div>
</main>

<div class="controls">
  <div class="deck">
    <button class="side" id="video-btn" type="button" aria-label="Record a video">
      <span class="side-ring" aria-hidden="true">
        <svg viewBox="0 0 24 24"><rect x="2.5" y="6.5" width="13" height="11" rx="2" fill="none"
          stroke="currentColor" stroke-width="1.7"/><path d="M15.5 10.6l5-2.9v8.6l-5-2.9z" fill="none"
          stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>
      </span>
      <span class="side-label" aria-hidden="true">Video</span>
    </button>

    <button class="shutter" id="shutter" type="button" aria-describedby="status">
      <span class="sr-only">Take a photo</span>
      <svg class="ring" viewBox="0 0 96 96" aria-hidden="true">
        <circle class="track" cx="48" cy="48" r="46"/>
        <circle class="progress" id="progress" cx="48" cy="48" r="46"/>
      </svg>
      <span class="disc" aria-hidden="true">
        <svg class="check" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none"
          stroke="#173A5E" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span class="pct" id="pct"></span>
      </span>
    </button>

    <button class="side" id="files-btn" type="button" aria-label="Send files">
      <span class="side-ring" aria-hidden="true">
        <svg viewBox="0 0 24 24"><path d="M6.5 3.5h7l4.5 4.5v12.5h-11.5z" fill="none" stroke="currentColor"
          stroke-width="1.7" stroke-linejoin="round"/><path d="M13.5 3.5V8H18" fill="none" stroke="currentColor"
          stroke-width="1.7" stroke-linejoin="round"/><path d="M12.25 17.5v-6m-2.6 2.5l2.6-2.6 2.6 2.6"
          fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </span>
      <span class="side-label" aria-hidden="true">Files</span>
    </button>
  </div>

  <div class="pick-menu" id="pick-menu" role="menu" aria-label="Send" hidden>
    <button type="button" role="menuitem" id="pick-files">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 3.5h7l4.5 4.5v12.5h-11.5z" fill="none"
        stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M13.5 3.5V8H18" fill="none"
        stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>
      <span><b>Files</b><small>One or more, any type</small></span>
    </button>
    <button type="button" role="menuitem" id="pick-folder">
      __FOLDER_ICON__
      <span><b>A whole folder</b><small>Keeps its subfolders</small></span>
    </button>
  </div>

  <p class="status" id="status" role="status" aria-live="polite">Take a photo, record a video, or send files.</p>
  <div class="recover" id="recover" hidden>
    <button class="retry" id="retry" type="button">Try again</button>
    <button class="skip" id="skip" type="button">Skip it</button>
  </div>
</div>

<section class="stopped" id="stopped" hidden>
  <h2>Local Drop is stopped</h2>
  <p>Start it again on your PC to send more.</p>
</section>

<dialog class="sheet" id="sheet" aria-labelledby="sheet-title">
  <div class="sheet-head">
    <h2 id="sheet-title">Choose a folder</h2>
    <button class="close" id="sheet-close" type="button" aria-label="Close">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor"
        stroke-width="2" stroke-linecap="round"/></svg>
    </button>
  </div>
  <nav class="crumbs" id="crumbs" aria-label="Folder path"></nav>
  <p class="abs" id="abs"></p>
  <ul class="folder-list" id="folder-list"></ul>
  <form class="new-folder" id="new-folder">
    <label class="sr-only" for="new-name">New folder name</label>
    <input id="new-name" placeholder="New folder name" autocomplete="off" maxlength="80" enterkeyhint="done">
    <button type="submit" id="create">Create</button>
  </form>
  <p class="sheet-msg" id="sheet-msg" role="status" aria-live="polite"></p>
  <button class="use" id="use" type="button">Use this folder</button>
</dialog>

<input id="photo-in" type="file" accept="image/*" capture="environment" hidden>
<input id="video-in" type="file" accept="video/*" capture="environment" hidden>
<input id="files-in" type="file" multiple hidden>
<input id="folder-in" type="file" webkitdirectory multiple hidden>

<div class="dropzone" id="dropzone" hidden>
  <div class="drop-card">
    <p class="drop-title">Drop to send</p>
    <p class="drop-where" id="drop-where"></p>
    <p class="drop-note">Files and whole folders, subfolders included.</p>
  </div>
</div>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const MB = 1048576;
  const inputs = { photo: $('photo-in'), video: $('video-in'), file: $('files-in'), folder: $('folder-in') };
  const shutter = $('shutter'), progress = $('progress'), pctEl = $('pct');
  const statusEl = $('status'), recoverEl = $('recover'), retryBtn = $('retry'), skipBtn = $('skip');
  const printEl = $('print'), captionEl = $('caption'), stripEl = $('strip'), countEl = $('count');
  const sheet = $('sheet'), crumbsEl = $('crumbs'), absEl = $('abs'), listEl = $('folder-list');
  const form = $('new-folder'), nameInput = $('new-name'), createBtn = $('create');
  const sheetMsg = $('sheet-msg'), useBtn = $('use'), destPath = $('dest-path');
  const filesBtn = $('files-btn'), menu = $('pick-menu'), dropzone = $('dropzone');
  const CIRC = 2 * Math.PI * 46;
  const STRIP_MAX = 12;
  const JUNK = /^(\.ds_store|thumbs\.db|desktop\.ini)$/i;   // system clutter inside folders: not sent
  const CAN_PICK_FOLDER = 'webkitdirectory' in document.createElement('input');
  const OFFLINE = "Can't reach your PC. Check that Local Drop is still running and your phone is on the same Wi-Fi.";
  const DOC_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 3.5h7l4.5 4.5v12.5h-11.5z" fill="none" '
    + 'stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M13.5 3.5V8H18" fill="none" '
    + 'stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  const queue = [];
  let busy = false, failed = null, sent = 0, streak = 0, doneTimer = 0;
  let ROOT = '', target = '', browsing = '';

  // ---------- small helpers ----------
  const join = (a, b) => a ? a + '/' + b : b;
  const pretty = rel => [ROOT, ...(rel ? rel.split('/') : [])].join(' / ');
  const size = n => n < 1024 ? n + ' bytes'
    : n < MB ? Math.round(n / 1024) + ' KB'
    : n < 1024 * MB ? (n / MB).toFixed(1) + ' MB'
    : (n / 1024 / MB).toFixed(2) + ' GB';
  const filesText = n => n == null ? '' : n === 1 ? '1 file' : n + ' files';
  const extOf = name => { const m = /\.([^.\/\\]{1,10})$/.exec(name || ''); return m ? m[1].toLowerCase() : ''; };
  const short = (s, max = 30) => s.length <= max ? s : s.slice(0, max - 11) + '…' + s.slice(-10);
  const quote = s => `“${short(s, 26)}”`;
  const isStopped = () => document.body.classList.contains('is-stopped');
  const post = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  async function api(url, opts) {
    let res;
    try { res = await fetch(url, opts); } catch (_) { throw new Error(OFFLINE); }
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || !data.ok) {
      const err = new Error(data.error || `Your PC answered with error ${res.status}.`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ---------- where things are saved (kept on the PC, shared by every phone) ----------
  function setTarget(rel) {
    target = rel;
    destPath.textContent = pretty(rel);
  }
  function applySettings(s) {
    ROOT = s.root_name;
    setTarget(s.default_folder);
  }
  async function loadSettings() {
    try { applySettings(await api('/api/settings')); return true; }
    catch (_) { return false; }
  }
  async function chooseFolder(rel) {
    applySettings(await api('/api/default', post({ folder: rel })));
  }

  // ---------- picking ----------
  shutter.addEventListener('click', () => inputs.photo.click());
  $('video-btn').addEventListener('click', () => inputs.video.click());
  // Files: straight to the picker, or a small menu when this browser can also pick a whole folder.
  function openMenu() {
    menu.hidden = false;
    filesBtn.setAttribute('aria-expanded', 'true');
    $('pick-files').focus();
  }
  function closeMenu(refocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    filesBtn.setAttribute('aria-expanded', 'false');
    if (refocus) filesBtn.focus();
  }
  if (CAN_PICK_FOLDER) {
    filesBtn.setAttribute('aria-haspopup', 'menu');
    filesBtn.setAttribute('aria-expanded', 'false');
  }
  filesBtn.addEventListener('click', () => {
    if (!CAN_PICK_FOLDER) return inputs.file.click();
    menu.hidden ? openMenu() : closeMenu();
  });
  $('pick-files').addEventListener('click', () => { closeMenu(); inputs.file.click(); });
  $('pick-folder').addEventListener('click', () => { closeMenu(); inputs.folder.click(); });
  document.addEventListener('pointerdown', e => {
    if (!menu.hidden && !menu.contains(e.target) && !filesBtn.contains(e.target)) closeMenu();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenu(true); });
  menu.addEventListener('keydown', e => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const items = [...menu.querySelectorAll('[role=menuitem]')];
    const i = items.indexOf(document.activeElement);
    items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
  });

  // Photos/videos taken through the camera option of the Files picker come in with
  // generic names (iPhone: image.jpg, capturedvideo.MOV). Name those like camera shots.
  const kindFor = (kind, file) => {
    if (kind !== 'file' || !/^(image\.(jpe?g|png|heic)|capturedvideo\.mov)$/i.test(file.name)) return kind;
    return (file.type || '').startsWith('video') || /\.mov$/i.test(file.name) ? 'video' : 'photo';
  };

  function kick() {
    if (failed) {
      setStatus(`Something didn't send. Tap Try again or Skip it to carry on. ${queue.length} waiting.`, true);
      return;
    }
    pump();
  }

  for (const kind of ['photo', 'video', 'file']) {
    const input = inputs[kind];
    input.addEventListener('change', () => {
      const files = Array.from(input.files || []);
      input.value = '';
      if (!files.length) return;
      files.forEach(file => queue.push({ file, kind: kindFor(kind, file), folder: target }));
      kick();
    });
  }

  inputs.folder.addEventListener('change', () => {
    const files = Array.from(inputs.folder.files || []);
    inputs.folder.value = '';
    if (!files.length) return;
    const first = (files[0].webkitRelativePath || '').split('/');
    queueFolder(first.length > 1 ? first[0] : 'Folder', files.map(file => {
      const parts = (file.webkitRelativePath || file.name).split('/');
      return { file, dir: parts.slice(1, -1).join('/') };
    }));
  });

  // A whole folder goes into a fresh folder of the same name on the PC, subfolders and all.
  async function queueFolder(name, entries) {
    entries = entries.filter(e => !JUNK.test(e.file.name));
    if (!entries.length) {
      setStatus(`The folder ${quote(name)} has no files in it, so nothing was sent.`);
      return;
    }
    let top;
    try {
      top = await api('/api/drop-folder', post({ parent: target, name }));
    } catch (err) {
      setStatus(err.message, true);
      return;
    }
    const batch = { name: top.name, total: entries.length, done: 0, skipped: 0, sentBytes: 0,
                    bytes: entries.reduce((sum, e) => sum + e.file.size, 0) };
    entries.forEach(e => queue.push({ file: e.file, kind: 'file', folder: top.path, sub: e.dir, batch }));
    kick();
  }

  // ---------- sending ----------
  progress.style.strokeDasharray = CIRC;
  progress.style.strokeDashoffset = CIRC;
  const setProgress = f => {
    progress.style.strokeDashoffset = CIRC * (1 - f);
    pctEl.textContent = Math.floor(f * 100) + '%';
  };
  const setStatus = (text, isError) => {
    statusEl.textContent = text;
    statusEl.classList.toggle('is-error', !!isError);
  };
  const px = data => data.width ? `${data.width} × ${data.height}` : '';   // never split across lines

  function sendingText(item) {
    const b = item.batch;
    if (b) {
      let t = `Sending folder ${quote(b.name)}: ${b.done + 1} of ${b.total}.`;
      if (item.file.size > 25 * MB) t += ' Keep this screen on until it finishes.';
      return t;
    }
    const what = item.kind === 'photo' ? 'the photo' : item.kind === 'video' ? 'the video'
      : short(item.file.name || 'file');
    let t = `Sending ${what}` + (queue.length ? `, ${queue.length} more waiting.` : '.');
    if (item.file.size > 25 * MB) t += ' Keep this screen on until it finishes.';
    return t;
  }
  function savedText(item, data) {
    if (item.kind === 'photo') return data.width ? `Photo saved to your PC at full size, ${px(data)}.` : 'Photo saved to your PC.';
    if (item.kind === 'video') return 'Video saved to your PC.';
    return `Saved ${short(data.name, 34)} to your PC.`;
  }
  function doneText(item, data) {
    const b = item.batch;
    if (b && b.done === b.total) {
      return b.skipped
        ? `Folder ${quote(b.name)} saved to your PC, ${b.total - b.skipped} of ${b.total} files.`
        : `Folder ${quote(b.name)} saved to your PC, ${filesText(b.total)}.`;
    }
    return streak > 1 ? `All ${streak} saved to your PC.` : savedText(item, data);
  }
  // A folder's ring fills once for the whole folder, by bytes, not once per file.
  const overall = (item, loaded, total) => {
    const b = item.batch;
    if (!b) return total ? loaded / total : 0;
    return b.bytes ? (b.sentBytes + loaded) / b.bytes : b.done / b.total;
  };

  async function upload(item) {
    // Ask first, so a big file isn't sent all the way just to be turned away.
    await api('/api/check?' + new URLSearchParams({ folder: item.folder, size: item.file.size }));
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const qs = new URLSearchParams({ folder: item.folder, sub: item.sub || '', kind: item.kind,
                                       name: item.file.name || '' });
      xhr.open('POST', '/upload?' + qs);
      xhr.setRequestHeader('Content-Type', item.file.type || 'application/octet-stream');
      xhr.upload.onprogress = e => { if (e.lengthComputable) setProgress(overall(item, e.loaded, e.total)); };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status === 200 && data.ok) return resolve(data);
        const err = new Error(data.error || `Your PC didn't accept the file (error ${xhr.status}).`);
        err.status = xhr.status;
        reject(err);
      };
      xhr.onerror = () => reject(new Error(OFFLINE + ' Then tap Try again.'));
      xhr.send(item.file);   // the original file, byte for byte: never resized or re-compressed
    });
  }

  function pump() {
    if (busy || failed || !queue.length) return;
    busy = true;
    const item = queue.shift();
    clearTimeout(doneTimer);
    shutter.classList.remove('is-done', 'is-error');
    shutter.classList.add('is-busy');
    setProgress(overall(item, 0, 0));
    setStatus(sendingText(item));

    upload(item).then(data => {
      sent += 1; streak += 1;
      countEl.textContent = sent + ' sent';
      if (item.batch) { item.batch.done += 1; item.batch.sentBytes += item.file.size; }
      show(item, data);
      shutter.classList.add('is-done');
      if (!queue.length) {
        setStatus(doneText(item, data));
        streak = 0;
      }
      doneTimer = setTimeout(() => shutter.classList.remove('is-done'), 1400);
    }).catch(err => {
      failed = item;
      if (err.status === 404) item.folderGone = true;
      shutter.classList.add('is-error');
      recoverEl.hidden = false;
      setStatus(err.message, true);
    }).finally(() => {
      busy = false;
      shutter.classList.remove('is-busy');
      setProgress(0);
      pump();
    });
  }

  function clearFailure() {
    failed = null;
    recoverEl.hidden = true;
    shutter.classList.remove('is-error');
  }

  retryBtn.addEventListener('click', async () => {
    if (!failed) return;
    const item = failed;
    if (item.folderGone) {
      await loadSettings();
      queue.forEach(q => { if (q.folder === item.folder) q.folder = target; });
      item.folder = target;
      item.folderGone = false;
    }
    queue.unshift(item);
    clearFailure();
    pump();
  });

  skipBtn.addEventListener('click', () => {
    if (!failed) return;
    const item = failed, b = item.batch;
    if (b) { b.done += 1; b.skipped += 1; b.sentBytes += item.file.size; }
    clearFailure();
    if (queue.length) return pump();
    setStatus(b && b.done === b.total ? doneText(item) : 'Skipped that one.');
    streak = 0;
  });

  // ---------- showing what was sent ----------
  const mediaKind = (file, name) => {
    const t = file.type || '';
    if (t.startsWith('image/') || /\.(jpe?g|png|gif|webp|avif|heic|heif|bmp)$/i.test(name)) return 'image';
    if (t.startsWith('video/') || /\.(mp4|mov|m4v|webm|3gp|mkv)$/i.test(name)) return 'video';
    return 'doc';
  };

  function docCard(name) {
    const card = document.createElement('div');
    card.className = 'doc';
    const ext = document.createElement('span');
    ext.className = 'doc-ext';
    const e = extOf(name);
    if (e) ext.textContent = '.' + e; else ext.innerHTML = DOC_ICON;
    card.append(ext);
    return card;
  }

  function bigView(kind, url, name) {
    if (kind === 'image') {
      const img = new Image();
      img.className = 'develop';
      img.alt = 'Last thing sent: ' + name;
      img.onerror = () => img.replaceWith(docCard(name));   // e.g. HEIC on Android
      img.src = url;
      return img;
    }
    if (kind === 'video') {
      const v = document.createElement('video');
      v.controls = true; v.muted = true; v.playsInline = true; v.preload = 'metadata';
      v.setAttribute('aria-label', 'Last video sent: ' + name);
      v.addEventListener('error', () => v.replaceWith(docCard(name)));
      v.src = url + '#t=0.1';
      return v;
    }
    return docCard(name);
  }

  function thumbView(kind, url, name) {
    const t = document.createElement('span');
    t.className = 'thumb';
    t.dataset.url = url;
    t.setAttribute('role', 'img');
    t.setAttribute('aria-label', name);
    const asDoc = () => { t.className = 'thumb is-doc'; t.replaceChildren(); t.textContent = extOf(name) || 'file'; };
    if (kind === 'image') {
      const i = new Image(); i.alt = ''; i.onerror = asDoc; i.src = url; t.append(i);
    } else if (kind === 'video') {
      t.classList.add('is-video');
      const v = document.createElement('video');
      v.muted = true; v.playsInline = true; v.preload = 'metadata';
      v.addEventListener('error', asDoc);
      v.src = url + '#t=0.1';
      t.append(v);
    } else {
      asDoc();
    }
    return t;
  }

  function show(item, data) {
    const url = URL.createObjectURL(item.file);
    const kind = mediaKind(item.file, data.name);
    printEl.classList.remove('is-empty');
    printEl.replaceChildren(bigView(kind, url, data.name));

    captionEl.replaceChildren();
    const name = document.createElement('span');
    name.textContent = (data.folder ? data.folder.split('/').pop() + ' / ' : '') + data.name;
    const sz = document.createElement('span');
    sz.textContent = (data.width ? px(data) + ', ' : '') + size(data.size);
    captionEl.append(name, sz);

    stripEl.prepend(thumbView(kind, url, data.name));
    while (stripEl.children.length > STRIP_MAX) {
      const old = stripEl.lastElementChild;
      URL.revokeObjectURL(old.dataset.url);
      old.remove();
    }
  }

  // ---------- stopping the server ----------
  async function stopServer(force) {
    try {
      await api('/api/stop', post({ force }));
    } catch (err) {
      if (err.status === 409 && !force) {
        if (confirm(err.message + " Stop anyway? It won't be saved.")) return stopServer(true);
        return;
      }
      setStatus(err.message, true);
      return;
    }
    queue.length = 0;
    closeMenu();
    if (sheet.open) sheet.close();
    document.body.classList.add('is-stopped');
    $('stopped').hidden = false;
  }
  $('stop').addEventListener('click', () => {
    const mine = busy || queue.length;
    const warn = mine ? " What's still sending from this phone won't be saved." : '';
    if (confirm('Stop Local Drop on your PC?' + warn + ' To send more, start it again on the PC.')) stopServer(!!mine);
  });

  // ---------- folder sheet ----------
  const say = (text, isError) => {
    sheetMsg.textContent = text || '';
    sheetMsg.classList.toggle('is-error', !!isError);
  };

  $('dest').addEventListener('click', () => {
    say('');
    sheet.showModal();
    browse(target);
  });
  $('sheet-close').addEventListener('click', () => sheet.close());
  sheet.addEventListener('click', e => {
    if (e.target !== sheet) return;
    const r = sheet.getBoundingClientRect();
    const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    if (!inside) sheet.close();  // tapped the dimmed area above the sheet
  });

  async function browse(rel) {
    listEl.setAttribute('aria-busy', 'true');
    try {
      const data = await api('/api/folders?path=' + encodeURIComponent(rel));
      browsing = data.path;
      ROOT = data.root_name;
      renderCrumbs();
      const n = filesText(data.files);
      absEl.textContent = (n ? n + ' in here, on your PC at ' : 'On your PC at ') + data.abs;
      renderList(data);
      say('');
    } catch (err) {
      if (err.status === 404 && rel) return browse('');
      say(err.message, true);
    } finally {
      listEl.removeAttribute('aria-busy');
    }
  }

  function renderCrumbs() {
    crumbsEl.replaceChildren();
    const parts = browsing ? browsing.split('/') : [];
    const levels = [{ name: ROOT, rel: '' }];
    parts.forEach((p, i) => levels.push({ name: p, rel: parts.slice(0, i + 1).join('/') }));
    levels.forEach((lvl, i) => {
      if (i) {
        const sep = document.createElement('span');
        sep.className = 'sep'; sep.textContent = '/'; sep.setAttribute('aria-hidden', 'true');
        crumbsEl.append(sep);
      }
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = lvl.name;
      if (i === levels.length - 1) b.setAttribute('aria-current', 'location');
      else b.addEventListener('click', () => browse(lvl.rel));
      crumbsEl.append(b);
    });
  }

  function renderList(data) {
    listEl.replaceChildren();
    if (!data.folders.length) {
      const li = document.createElement('li');
      li.className = 'none';
      li.textContent = 'No folders in here yet. Make one below.';
      listEl.append(li);
    }
    for (const f of data.folders) {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.innerHTML = `__FOLDER_ICON__<span class="name"></span><span class="meta"></span>
        <svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5L10.5 8 6 12.5" fill="none"
        stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
      b.querySelector('.name').textContent = f.name;
      b.querySelector('.meta').textContent = filesText(f.files);
      b.addEventListener('click', () => browse(join(browsing, f.name)));
      li.append(b);
      listEl.append(li);
    }
    const here = browsing === target;
    useBtn.disabled = here;
    useBtn.textContent = here ? 'Already saving here' : 'Use this folder';
  }

  useBtn.addEventListener('click', async () => {
    useBtn.disabled = true;
    try {
      await chooseFolder(browsing);
      sheet.close();
      if (!busy && !failed) setStatus('Now saving to ' + pretty(target) + '.');
    } catch (err) {
      say(err.message, true);
      useBtn.disabled = false;
    }
  });

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const name = nameInput.value.trim();
    if (!name) { say('Type a name for the new folder.', true); nameInput.focus(); return; }
    createBtn.disabled = true;
    try {
      const data = await api('/api/folders', post({ parent: browsing, name }));
      await chooseFolder(data.path);
      nameInput.value = '';
      nameInput.blur();
      sheet.close();
      if (!busy && !failed) {
        setStatus((data.existed ? 'That folder already existed. ' : 'Folder created. ')
          + 'Now saving to ' + pretty(target) + '.');
      }
    } catch (err) {
      say(err.message, true);
    } finally {
      createBtn.disabled = false;
    }
  });

  // ---------- drag and drop (on a computer or tablet) ----------
  let dragDepth = 0;
  const carriesFiles = e => Array.from((e.dataTransfer && e.dataTransfer.types) || []).includes('Files');
  const showDrop = on => {
    if (on) $('drop-where').textContent = 'into ' + pretty(target);
    dropzone.hidden = !on;
  };
  window.addEventListener('dragenter', e => {
    if (!carriesFiles(e) || isStopped()) return;
    e.preventDefault();
    dragDepth += 1;
    showDrop(true);
  });
  window.addEventListener('dragover', e => {
    if (!carriesFiles(e) || isStopped()) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  window.addEventListener('dragleave', e => {
    if (!carriesFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) showDrop(false);
  });
  window.addEventListener('drop', e => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    showDrop(false);
    if (isStopped()) return;
    // Entries must be read during the drop event itself; the folders are walked afterwards.
    const entries = Array.from(e.dataTransfer.items || [])
      .filter(i => i.kind === 'file' && i.webkitGetAsEntry)
      .map(i => i.webkitGetAsEntry())
      .filter(Boolean);
    if (entries.length) takeDrop(entries);
    else Array.from(e.dataTransfer.files || []).forEach(file => queue.push({ file, kind: 'file', folder: target }));
    kick();
  });

  const readAll = reader => new Promise((resolve, reject) => {
    const out = [];
    const next = () => reader.readEntries(batch => {
      if (!batch.length) return resolve(out);
      out.push(...batch);
      next();
    }, reject);
    next();
  });
  const fileOf = entry => new Promise((resolve, reject) => entry.file(resolve, reject));

  async function walk(dir, path, out) {
    let children = [];
    try { children = await readAll(dir.createReader()); } catch (_) { return out; }
    for (const child of children) {
      const childPath = path ? path + '/' + child.name : child.name;
      if (child.isDirectory) await walk(child, childPath, out);
      else { try { out.push({ file: await fileOf(child), dir: path }); } catch (_) {} }
    }
    return out;
  }

  async function takeDrop(entries) {
    for (const entry of entries.filter(en => en.isFile)) {
      try { queue.push({ file: await fileOf(entry), kind: 'file', folder: target }); } catch (_) {}
    }
    kick();
    for (const dir of entries.filter(en => en.isDirectory)) {
      setStatus(`Reading folder ${quote(dir.name)}…`);
      await queueFolder(dir.name, await walk(dir, '', []));
    }
  }

  if (matchMedia('(hover: hover) and (pointer: fine)').matches) {
    const empty = printEl.querySelector('.empty');
    if (empty) empty.textContent = 'Nothing sent yet. Drag files or whole folders here, or use the buttons below.';
  }

  // ---------- start, and catch up when you come back to the page ----------
  loadSettings().then(ok => { if (!ok) setStatus(OFFLINE, true); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !sheet.open && !document.body.classList.contains('is-stopped')) {
      loadSettings();
    }
  });
})();
</script>
</body>
</html>
"""

PC_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local Drop</title>
__FONT__
<style>
__BASE__
body{min-height:100vh;display:grid;place-items:center;padding:40px 16px}
.wrap{display:grid;grid-template-columns:auto minmax(0,31rem);gap:56px;align-items:start;max-width:900px}
.qr{background:var(--paper);padding:20px;border-radius:4px;width:288px;height:288px;
  display:grid;place-items:center;box-shadow:0 24px 50px -28px rgba(5,15,30,.9)}
.qr canvas,.qr img{display:block;width:248px!important;height:248px!important;image-rendering:pixelated}
.qr .noqr{color:var(--deep);text-align:center;font-size:.9375rem;margin:0}
h1{font-size:2.25rem;line-height:1.1;letter-spacing:-.02em;margin:0 0 .75rem;font-weight:700}
.url{display:inline-block;font-size:1.5rem;font-weight:500;color:var(--paper);text-decoration:none;
  border-bottom:2px solid var(--rule);padding-bottom:2px;overflow-wrap:anywhere}
p{margin:.9rem 0 0;color:var(--wash);max-width:46ch}
.alts{margin:.4rem 0 0;padding:0;list-style:none;color:var(--paper)}

.block{margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--rule)}
.block h2{margin:0;font-size:1.0625rem;font-weight:700;letter-spacing:-.005em}
.path{margin:.45rem 0 0;font-size:1.25rem;font-weight:500;color:var(--paper);overflow-wrap:anywhere;max-width:none}
.note{margin:.3rem 0 0}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:1.1rem}
.btn{font-weight:500;font-size:.9375rem;border-radius:999px;padding:.6rem 1.2rem;cursor:pointer;
  border:1.5px solid var(--rule-strong);background:transparent;color:var(--paper)}
.btn.primary{background:var(--paper);border-color:var(--paper);color:var(--deep)}
.btn.danger{border-color:var(--alert);color:var(--alert)}
.btn:disabled{opacity:.55;cursor:default}
details{margin-top:1rem}
summary{cursor:pointer;width:max-content;color:var(--wash);text-decoration:underline;
  text-underline-offset:3px;text-decoration-color:var(--rule)}
.typed{display:flex;gap:8px;margin-top:.7rem;max-width:31rem}
.typed input{flex:1;min-width:0;font:inherit;color:var(--deep);background:var(--paper);border:0;
  border-radius:10px;padding:.6rem .75rem}
.typed input:focus-visible{outline-offset:2px}
.msg{min-height:1.45em;margin:.8rem 0 0;color:var(--paper)}
.msg.is-error{color:var(--alert)}
.stop-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px}
.stop-row p{margin:0}

.done{max-width:34rem}
.done p{margin-top:.75rem}
@media (max-width:760px){.wrap{grid-template-columns:1fr;gap:32px;justify-items:start}}
</style>
</head>
<body>
<div class="wrap" id="wrap">
  <div class="qr" id="qr"><p class="noqr">Type the address on the right into your phone's browser.</p></div>
  <div>
    <h1>Scan this with your phone</h1>
    <a class="url" href="__URL__">__URL__</a>
    <p>Your phone needs to be on the same Wi-Fi as this PC. Add the page to your home screen to open it in one tap next time.</p>
    __ALTS__

    <section class="block" aria-labelledby="where-title">
      <h2 id="where-title">Save location</h2>
      <p class="path" id="root-path"></p>
      <p class="note" id="root-note"></p>
      <div class="actions" id="root-actions">
        <button class="btn primary" id="browse" type="button">Choose another folder</button>
        <button class="btn" id="open" type="button">Open folder</button>
      </div>
      <details id="typed-box">
        <summary>Type a path instead</summary>
        <form class="typed" id="typed">
          <input id="typed-path" placeholder="D:\Photos\From phone" autocomplete="off" spellcheck="false"
                 aria-label="Folder path">
          <button class="btn primary" type="submit">Use this folder</button>
        </form>
      </details>
      <p class="msg" id="root-msg" role="status" aria-live="polite"></p>
    </section>

    <section class="block stop-row">
      <button class="btn danger" id="stop" type="button">Stop Local Drop</button>
      <p>Phones can't send anything until you start it again.</p>
    </section>
  </div>
</div>

<div class="done" id="done" hidden>
  <h1>Local Drop has stopped</h1>
  <p>You can close this tab. To start it again, double-click Start Local Drop.bat.</p>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>
(() => {
  const $ = id => document.getElementById(id);
  const pathEl = $('root-path'), noteEl = $('root-note'), msgEl = $('root-msg');
  const browseBtn = $('browse'), openBtn = $('open'), typedBox = $('typed-box');
  const OFFLINE = "Local Drop isn't answering. If you closed its window, double-click Start Local Drop.bat to start it again.";

  if (window.QRCode) {
    const box = $('qr');
    box.replaceChildren();
    new QRCode(box, { text: __URL_JSON__, width: 248, height: 248,
      colorDark: '#0F2A45', colorLight: '#EEF3F8', correctLevel: QRCode.CorrectLevel.M });
  }

  const say = (text, isError) => { msgEl.textContent = text || ''; msgEl.classList.toggle('is-error', !!isError); };
  const post = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  async function api(url, opts) {
    let res;
    try { res = await fetch(url, opts); } catch (_) { throw new Error(OFFLINE); }
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || !data.ok) {
      const err = new Error(data.error || `Local Drop answered with error ${res.status}.`);
      err.status = res.status; err.reason = data.reason;
      throw err;
    }
    return data;
  }

  function render(s) {
    pathEl.textContent = s.root;
    noteEl.textContent = s.default_folder
      ? `Phones are saving into ${s.default_folder.split('/').join(' \\ ')} inside it. They can pick or create other folders in here.`
      : 'Phones save straight into this folder, and can pick or create folders inside it.';
    const mine = s.this_pc;
    $('root-actions').hidden = !mine;
    typedBox.hidden = !mine;
    if (!mine) say('You can only change this on the PC that runs Local Drop.');
  }

  async function load() {
    try { render(await api('/api/settings')); }
    catch (err) { say(err.message, true); }
  }

  browseBtn.addEventListener('click', async () => {
    browseBtn.disabled = true;
    say('A folder window opened on this PC. If you don\'t see it, check the taskbar.');
    try {
      const s = await api('/api/root/browse', post());
      render(s);
      say(s.cancelled ? '' : 'Saved. Everything from your phone now goes here.');
    } catch (err) {
      if (err.reason === 'no-picker') {
        typedBox.open = true;
        $('typed-path').focus();
      }
      say(err.message, true);
    } finally {
      browseBtn.disabled = false;
    }
  });

  $('typed').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      render(await api('/api/root', post({ path: $('typed-path').value })));
      $('typed-path').value = '';
      typedBox.open = false;
      say('Saved. Everything from your phone now goes here.');
    } catch (err) {
      say(err.message, true);
    }
  });

  openBtn.addEventListener('click', async () => {
    try { await api('/api/root/open', post()); say(''); }
    catch (err) { say(err.message, true); }
  });

  async function stop(force) {
    try {
      await api('/api/stop', post({ force }));
    } catch (err) {
      if (err.status === 409 && !force) {
        if (confirm(err.message + " Stop anyway? It won't be saved.")) return stop(true);
        return;
      }
      say(err.message, true);
      return;
    }
    $('wrap').hidden = true;
    $('done').hidden = false;
  }
  $('stop').addEventListener('click', () => stop(false));

  load();
  window.addEventListener('focus', () => { if ($('done').hidden) load(); });
})();
</script>
</body>
</html>
"""


def build_pages(urls):
    phone = (PHONE_PAGE.replace("__FONT__", FONT_LINK)
             .replace("__BASE__", BASE_CSS)
             .replace("__FOLDER_ICON__", FOLDER_ICON)
             .replace("__POWER_ICON__", POWER_ICON))

    main_url = urls[0]
    alts = ""
    if len(urls) > 1:
        items = "".join(f"<li>{html.escape(u)}</li>" for u in urls[1:])
        alts = f'<p>If that address doesn\'t open, try:</p><ul class="alts">{items}</ul>'
    url_json = json.dumps(main_url).replace("</", "<\\/")
    pc = (PC_PAGE.replace("__FONT__", FONT_LINK)
          .replace("__BASE__", BASE_CSS)
          .replace("__ALTS__", alts)
          .replace("__URL_JSON__", url_json)
          .replace("__URL__", html.escape(main_url)))
    return phone.encode("utf-8"), pc.encode("utf-8")


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Send photos, videos and files from your phone to this PC.")
    ap.add_argument("--dir", help="folder to start with until you choose one on the PC page")
    ap.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the PC page at start")
    args = ap.parse_args()

    sfile = settings_path()
    saved = load_settings(sfile)
    migrated = used_saved = False
    if not saved:
        saved = load_settings(LEGACY_SETTINGS)
        migrated = bool(saved.get("save_dir"))
    save_dir, default_folder = None, ""
    if saved.get("save_dir"):
        save_dir, problem = prepare_folder(saved["save_dir"])
        if problem:
            print(f"\n  The folder you chose last time isn't available: {problem}\n  Using another one for now.")
        else:
            used_saved = True
            default_folder = str(saved.get("default_folder") or "")
            if args.dir:
                print("\n  Using the folder you chose on the Local Drop page. (--dir only applies until you choose one.)")
    if save_dir is None:
        keep_old = LEGACY_DEFAULT_DIR.is_dir() and not DEFAULT_DIR.exists()   # keep using it if it's there
        start = args.dir or str(LEGACY_DEFAULT_DIR if keep_old else DEFAULT_DIR)
        save_dir, problem = prepare_folder(start)
        if problem:
            print(f"\n  {problem}\n  Pick another folder with --dir.\n")
            sys.exit(1)

    try:
        server = Server(("0.0.0.0", args.port), Handler)
    except OSError:
        print(f"\n  Port {args.port} is already in use. Local Drop may already be running in another window."
              f"\n  Close it, or start with a different port, e.g. --port {args.port + 1}\n")
        sys.exit(1)

    app = App(save_dir, default_folder, sfile)
    if migrated and used_saved:
        app.persist()   # copy the old Phone Cam choice over to Local Drop's settings
    urls =[f"http://{ip}:{args.port}" for ip in lan_addresses()]
    Handler.app = app
    Handler.phone_page, Handler.pc_page = build_pages(urls)

    print()
    print("  Local Drop is running.")
    print()
    print(f"  On your phone, open:  {urls[0]}")
    for u in urls[1:]:
        print(f"                   or:  {u}")
    print(f"  Saving to:            {save_dir}")
    print("  (change it on the Local Drop page that just opened on this PC)")
    print()
    print("  Can't connect from the phone? Allow Python through Windows Firewall")
    print("  on Private networks, and set this Wi-Fi to Private in Windows settings.")
    print()
    print("  To stop: use the Stop button on the page, or close this window.")
    print()

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://localhost:{args.port}/qr")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping = True
        deadline = time.time() + 5
        while app.active_parts and time.time() < deadline:   # let unfinished uploads clean up
            time.sleep(0.1)
        for part in list(app.active_parts):
            try:
                part.unlink()
            except OSError:
                pass
        server.server_close()
    print(f"\n  {app.stop_message}\n")


if __name__ == "__main__":
    main()
