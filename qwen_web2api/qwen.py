"""Shared primitives for qwen-web2api.

Upstream calls now always run inside the chat.qwen.ai tab through the browser
bridge (see bridge.py + extension/bridge.js); Python never talks to the Qwen
API directly. This module only keeps the error type and the logger shared by
the rest of the package.
"""
import collections
import json
import os
import platform
import shutil
import sys
import threading
import time

from .config import CONFIG

_recent_log = collections.deque(maxlen=100)
_log_lock = threading.Lock()


def get_recent_log():
    with _log_lock:
        return list(_recent_log)


class QwenError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def is_debug():
    """Kiem tra xem co bat log debug khong (chap nhan ca 'debug' va 'debug_log')."""
    return bool(CONFIG.get("debug") or CONFIG.get("debug_log") or CONFIG.get("verbose_log"))


def debug_log(msg):
    """Ghi log debug chi khi debug duoc bat."""
    if not is_debug():
        return
    log(f"[debug] {msg}")


# --- Log theo phien chay (config log_session_file / log_dir / log_save_prompt_files) ---
_SESSION_ID = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
_session_file = None
_session_path = None


def session_log_path():
    return _session_path


def _log_dir():
    return str(CONFIG.get("log_dir") or "logs")


def _write_session(line):
    with _log_lock:
        if _session_file is not None:
            try:
                _session_file.write(line + "\n")
            except (OSError, ValueError):
                pass


def init_session_log():
    """log_session_file=true -> mo <log_dir>/server-<phien>.log (1 file / lan bat server).
    Tra ve duong dan tuyet doi, hoac None khi tat / khong mo duoc. Goi lai thi tra file cu."""
    global _session_file, _session_path
    if not CONFIG.get("log_session_file"):
        return None
    with _log_lock:
        if _session_file is not None:
            return _session_path
        try:
            d = _log_dir()
            os.makedirs(d, exist_ok=True)
            path = os.path.abspath(os.path.join(d, f"server-{_SESSION_ID}.log"))
            _session_file = open(path, "a", encoding="utf-8", buffering=1)
            _session_path = path
        except OSError as e:
            sys.stderr.write(f"[qwen-web2api] khong mo duoc session log: {e}\n")
            return None
    cfg = {k: v for k, v in CONFIG.items() if k != "api_keys"}
    cfg["api_keys"] = f"<{len(CONFIG.get('api_keys') or [])} key>"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_session(f"=== qwen-web2api session {_SESSION_ID} started {now} ===")
    _write_session(f"python {sys.version.split()[0]} | {platform.platform()} | cwd={os.getcwd()}")
    _write_session("config " + json.dumps(cfg, ensure_ascii=False, sort_keys=True, default=str))
    return _session_path


def close_session_log():
    global _session_file, _session_path
    with _log_lock:
        f, _session_file, _session_path = _session_file, None, None
    if f is not None:
        try:
            f.close()
        except OSError:
            pass


def save_prompt_copy(path, name=None):
    """log_save_prompt_files=true -> chep file prompt vao <log_dir>/prompts-<phien>/.
    Tra ve duong dan ban sao hoac None."""
    if not CONFIG.get("log_save_prompt_files") or not path:
        return None
    try:
        d = os.path.join(_log_dir(), "prompts-" + _SESSION_ID)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, name or os.path.basename(path))
        shutil.copyfile(path, dst)
        return os.path.abspath(dst)
    except OSError:
        return None


def log(msg):
    if not CONFIG.get("log_requests"):
        return
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        _recent_log.append(line)
        if _session_file is not None:
            try:
                _session_file.write(line + "\n")
            except (OSError, ValueError):
                pass
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    path = CONFIG.get("log_file")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
