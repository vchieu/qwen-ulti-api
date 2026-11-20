"""Browser bridge: chay upstream call ben trong tab chat.qwen.ai that.

Ly do: endpoint completions duoc bao ve boi baxia. Header bx-ua do JS cua
trang ky cho TUNG request, x5sec het han nhanh -- Python khong tai tao duoc.
Chay fetch() trong trang dung session that + header anti-bot that.

  * OpenAI API van o CONFIG["port"] (8082) -- server.py import truc tiep
    generate/generate_stream tu module nay.
  * Module nay chay HTTP server thu hai o CONFIG["bridge_port"] (8083).
  * /requeue tra job ve hang doi khi extension phai navigate doi che do
    (temporary chat); /poll nhan them danh sach model de /v1/models merge.
"""
import collections
import json
import queue
import re
import threading
import time
import uuid
import tempfile
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .config import CONFIG
from . import qwen

_ALLOWED_ORIGIN = "https://chat.qwen.ai"
_lock = threading.Lock()
_pending = queue.Queue()
_jobs = {}
_models = []
_last_seen = 0.0
# Trang thai tab chat.qwen.ai (extension gui trong /poll va ping): nonce moi lan load trang,
# path, chat_id tren URL. session.py doi chieu nonce de biet tab co bi reload khong.
_tab = {}
_tab_ts = 0.0
# Reference to shared circular buffer in qwen.py
_recent_log = qwen._recent_log


def _log(msg):
    qwen.log("[bridge] " + msg)


def _debug_log(msg):
    if qwen.is_debug():
        _log("[debug] " + msg)


# Truoc day client ngat ket noi chi xoa _jobs[jid]; extension van dang giu
# job va /requeue (khong kiem tra gi) co the hoi sinh job da chet -> prompt
# cu tu duoc go lai vao UI. Registry nay cho huy job xuyen suot.
_cancelled = collections.OrderedDict()      # job_id -> ts bi huy
_managed_jobs = collections.OrderedDict()   # job_id -> ts queue (job Python tao)
_CANCEL_TTL = 900.0
_CANCEL_MAX = 4096


def _as_num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _prune_job_registry(now=None):
    now = time.time() if now is None else now
    with _lock:
        for reg in (_cancelled, _managed_jobs):
            for jid in list(reg):
                if now - reg[jid] > _CANCEL_TTL:
                    reg.pop(jid, None)
            while len(reg) > _CANCEL_MAX:
                reg.popitem(last=False)


def job_cancelled(jid):
    with _lock:
        return jid in _cancelled


def job_alive(jid):
    """Job con song = caller con cho (_jobs) VA chua bi huy."""
    with _lock:
        return jid in _jobs and jid not in _cancelled


def cancel_job(jid, reason="client disconnected"):
    """Huy job: danh dau + tha caller + de extension biet qua /alive, /event."""
    if not isinstance(jid, str) or not jid.strip():
        return False
    jid = jid.strip()
    with _lock:
        _cancelled[jid] = time.time()
        q = _jobs.pop(jid, None)
    if q is not None:
        try:
            q.put_nowait({"kind": "error", "cancelled": True,
                          "message": "job cancelled: " + str(reason)})
        except Exception:
            pass
    _prune_job_registry()
    _log(f"cancelled job {jid} ({reason})")
    return True


def _native_fence(name, args, call_id=None):
    obj = {"name": name, "arguments": args}
    if call_id:
        obj["id"] = str(call_id)
    return "\n```tool_call\n" + json.dumps(obj, ensure_ascii=False) + "\n```\n"


def _split_json_objects(s):
    """Tach nhieu JSON object lien tiep trong 1 chuoi arguments.
    Tra ve [] neu khong tach duoc >= 2 object (de tools.py tu choi call)."""
    dec = json.JSONDecoder(strict=False)
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n or s[i] != "{":
            break
        try:
            obj, end = dec.raw_decode(s, i)
        except ValueError:
            break
        if not isinstance(obj, dict):
            break
        out.append(obj)
        i = end
    return out if len(out) > 1 else []

def worker_alive():
    return (time.time() - _last_seen) < float(CONFIG.get("bridge_worker_ttl", 20))


def temporary_chat_enabled():
    """Doc ca hai kieu key; `temporary_chat` duoc uu tien khi bat."""
    if CONFIG.get("temporary_chat"):
        return True
    return bool(CONFIG.get("temporary-chat"))


def known_models():
    """Ban sao danh sach model do extension bao ve (id + name)."""
    with _lock:
        return [dict(m) for m in _models]


def _remember_models(items):
    """Cap nhat danh sach model tu /poll; bo qua entry sai, giu toi da 200."""
    global _models
    clean = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        mid = mid.strip()
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            name = mid
        clean.append({"id": mid, "name": name.strip()})
        if len(clean) >= 200:
            break
    if clean:
        with _lock:
            _models = clean


def _remember_tab(tab):
    """Luu trang thai tab tu extension. Chi giu nonce/path/chat_id (str) va busy (bool); khong
    co nonce -> bo qua (extension cu)."""
    global _tab, _tab_ts
    if not isinstance(tab, dict):
        return
    clean = {}
    for k in ("nonce", "path", "chat_id"):
        v = tab.get(k)
        if isinstance(v, str) and v:
            clean[k] = v[:300]
    if isinstance(tab.get("busy"), bool):
        clean["busy"] = tab["busy"]
    if not clean.get("nonce"):
        return
    with _lock:
        _tab, _tab_ts = clean, time.time()


def tab_state():
    """Ban sao trang thai tab moi nhat, hoac None khi chua co / qua bridge_worker_ttl."""
    with _lock:
        if not _tab or (time.time() - _tab_ts) > _as_num(
                CONFIG.get("bridge_worker_ttl"), 20):
            return None
        return dict(_tab)


def _captcha_wait():
    """Giay cho giai captcha (config captcha_wait_seconds; sai/am -> mac dinh/0)."""
    try:
        return max(0, int(float(CONFIG.get("captcha_wait_seconds", 300))))
    except (TypeError, ValueError):
        return 300


def _model_ids():
    """Id upstream hop le (models.py) -> extension dung de ghi de model request."""
    from .models import MODELS
    return sorted({str(c.get("upstream", n)) for n, c in MODELS.items()})


_DEFAULT_BUILTIN_TOOLS = ("code_interpreter",)


def builtin_tools():
    """Ten tool built-in cua Qwen (config qwen_builtin_tools): extension de Qwen UI tu
    chay, khong tra ve client. Chuan hoa lowercase + bo trung; sai kieu -> mac dinh."""
    raw = CONFIG.get("qwen_builtin_tools")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return list(_DEFAULT_BUILTIN_TOOLS)
    out = []
    for x in raw:
        if isinstance(x, str):
            n = x.strip().lower()
            if n and n not in out:
                out.append(n)
    return out


_UI_MODES = ("auto", "thinking", "fast")


def mode_feature_config():
    """config bridge_mode_feature_config -> {mode: {key: value}} chi giu mode hop le
    (auto/thinking/fast, khong phan biet hoa thuong) co gia tri la dict khong rong."""
    raw = CONFIG.get("bridge_mode_feature_config")
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            name = k.strip().lower() if isinstance(k, str) else ""
            if name in _UI_MODES and isinstance(v, dict) and v:
                out[name] = v
    return out


_ATTACH_BLOCK_KINDS = ("name", "size", "status", "count", "content", "stale")


def _attachment_block():
    """config bridge_attachment_block -> list kind hop le (kind la bi bo qua)."""
    raw = CONFIG.get("bridge_attachment_block")
    if not isinstance(raw, (list, tuple)):
        return []
    kinds = (str(x).strip().lower() for x in raw)
    return [k for k in kinds if k in _ATTACH_BLOCK_KINDS]


def status():
    with _lock:
        n = len(_jobs)
        n_models = len(_models)
    return {"worker": worker_alive(), "pending": _pending.qsize(), "jobs": n,
            "temporary_chat": temporary_chat_enabled(),
            "select_model": bool(CONFIG.get("bridge_select_model", True)),
            "select_mode": bool(CONFIG.get("bridge_select_mode", True)),
            "force_model": bool(CONFIG.get("bridge_force_request_model", False)),
            "force_mode": bool(CONFIG.get("bridge_force_request_mode", False)),
            "mode_feature_config": mode_feature_config(),
            # extension phai doc 2 co nay.
            "require_model": bool(CONFIG.get("bridge_require_model", False)),
            "require_mode": bool(CONFIG.get("bridge_require_mode", False)),
            # chan request thieu attachment.
            "require_attachment": bool(CONFIG.get("bridge_require_attachment", True)),
            "attachment_block": _attachment_block(),
            "model_ids": _model_ids(),
            # tool built-in cua Qwen (config qwen_builtin_tools).
            "builtin_tools": builtin_tools(),
            "temporary_reload": bool(CONFIG.get("temporary_chat_reload", True)),
            "captcha_wait": _captcha_wait(),
            "attachment_prompt": str(CONFIG.get("attachment_prompt", "Please read the text and action")),
            "debug": qwen.is_debug(),
            "models": n_models,
            "last_seen_ago": round(time.time() - _last_seen, 1)
            if _last_seen else None}


class _Tracker:
    """Gom delta; tu nhan dien incremental vs cumulative."""

    def __init__(self):
        self.total = {"content": "", "reasoning": ""}
        self.last = {"content": None, "reasoning": None}
        self.cumulative = None

    def feed(self, kind, text):
        prev = self.last[kind]
        if self.cumulative is None and prev is not None:
            self.cumulative = bool(text.startswith(prev) and len(text) > len(prev))
        if self.cumulative:
            base = self.total[kind]
            if text.startswith(base):
                delta = text[len(base):]
                self.total[kind] = text
            else:
                delta = text
                self.total[kind] = base + text
        else:
            delta = text
            self.total[kind] += text
        self.last[kind] = text
        return delta


def _file_companion(file_info):
    """Cau gui kem file khi prompt nam TOAN BO
    trong file. Ngoai cau mac dinh con co response_format (extra_instructions)
    va loi nhac sua loi cua lan retry (retry_nudge) -- truoc day chi co cau
    mac dinh nen 2 phan nay khong bao gio toi Qwen.
    """
    parts = [str(CONFIG.get("attachment_prompt", "Please read the text and action"))]
    for key in ("extra_instructions", "retry_nudge"):
        extra = str((file_info or {}).get(key) or "").strip()
        if extra:
            parts.append(extra)
    return "\n\n".join(parts)


# Heuristic: cau tra loi cua Qwen nghe nhu "khong thay / khong doc duoc file". Chi dung
# de canh bao (co the bao nham khi noi dung that co cau tuong tu).
_NO_FILE_RX = re.compile(
    r"(?:(?:\bno\b|\bnot\b|n't|\bcannot\b|\bunable\b|\bmissing\b)[^.\n]{0,60}?"
    r"\b(?:see|find|access|read|open|receive|view|locate|detect|attached)\b[^.\n]{0,40}?"
    r"\b(?:file|attachment|attached|document)s?\b"
    r"|\b(?:file|attachment|document)s?\b[^.\n]{0,40}?\b(?:empty|missing|blank)\b"
    r"|\b(?:file|attachment|document)s?\b[^.\n]{0,30}?"
    r"\bnot (?:attached|visible|provided|included)\b"
    r"|không\s+(?:thấy|tìm thấy|nhận được|đọc được|truy cập được|có)"
    r"[^.\n]{0,40}?(?:file|tệp|tài liệu|đính kèm)"
    r"|(?:没有|未|无法|看不到|没看到|读取不到)[^。\n]{0,10}?(?:文件|附件|文档))",
    re.IGNORECASE)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def attach_report(file_info, usage, content, warn_ratio=0.05):
    """Job co file da xong -> (dong_tom_tat, [canh_bao]). Thuan tuy, khong log.

    Tin hieu Qwen co doc file hay khong (chi la heuristic, khong chung minh duoc):
      - usage.input_tokens so voi so ky tu file (ty le qua thap -> chua doc file,
        hoac usage cua Qwen khong tinh token cua file: doi chieu vai job dau);
      - cau tra loi dau tien co dang "khong thay file".
    """
    fi = file_info if isinstance(file_info, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    chars = fi.get("size") if isinstance(fi.get("size"), int) else 0
    tin = next((_num(usage.get(k)) for k in ("input_tokens", "prompt_tokens")
                if _num(usage.get(k)) is not None), None)
    tout = next((_num(usage.get(k)) for k in ("output_tokens", "completion_tokens")
                 if _num(usage.get(k)) is not None), None)
    ratio = (tin / chars) if (tin is not None and chars) else None
    text = content or ""
    head = " ".join(text[:160].split())
    line = (f"[attach-check] file={fi.get('filename') or '?'} chars={chars or '?'} "
            f"bytes={fi.get('bytes', '?')} sha256={str(fi.get('sha256') or '?')[:12]} "
            f"usage_in={tin} usage_out={tout} "
            f"tokens_per_char={'%.3f' % ratio if ratio is not None else '?'} "
            f"usage_keys={sorted(usage)} resp_chars={len(text)} resp_head={head!r}")
    warns = []
    if ratio is not None and warn_ratio and warn_ratio > 0 and chars >= 2000 \
            and ratio < warn_ratio:
        warns.append(
            f"usage.input_tokens={tin:g} qua thap so voi file {chars} ky tu "
            f"(ty le {ratio:.3f} < {warn_ratio}): Qwen co the CHUA doc file, hoac usage "
            "khong tinh token cua file -- doi chieu voi vai job dau roi chinh "
            "attachment_usage_warn_ratio")
    m = _NO_FILE_RX.search(text[:600])
    if m:
        warns.append(f"cau tra loi giong Qwen KHONG thay/doc duoc file: {m.group(0)!r}")
    return line, warns


def _log_attach_report(jid, attempt, file_info, usage, content):
    try:
        line, warns = attach_report(
            file_info, usage, content,
            _as_num(CONFIG.get("attachment_usage_warn_ratio"), 0.05))
        _log(f"[job {jid[:8]}] attempt={attempt + 1} {line}")
        for w in warns:
            _log(f"[job {jid[:8]}] attach-check WARN: {w}")
    except Exception as e:   # chan doan khong duoc lam hong request
        _log(f"[job {jid[:8]}] attach-check error: {e}")


def generate_stream(prompt, model, think, usage_out=None, file_info=None, mode=None,
                    cancel_event=None, session=None, session_out=None, tools_active=None):
    """Yield (kind, text) -- interface server.py goi cho luong SSE.

      cancel_event (threading.Event): client ngat ket noi thi
      khong queue job moi, khong retry, huy job dang chay (ke ca GeneratorExit).
      khong co file dinh kem -> attachment_prompt = "" (khong
      duoc phep co cau fallback de thay the prompt that cua user).
      warn model_mismatch/mode_mismatch + config strict -> loi.
      arguments hong giu nguyen de tools.py tu choi; chuoi
      nhieu JSON lien tiep thi tach thanh nhieu fence.

      session (dict, session.Plan.job_session) -> job mang them `session` + `tools_active`;
      session_out (dict) nhan nonce/chat_id/parent_id/sent/session_lost/dirty tu extension.
      Job session continue/fork KHONG duoc retry sau khi request da gui (delta da nam trong
      chat Qwen, gui lai se nhan doi). tools_active (khong None) thay cho viec doan tu
      "# Tool Use" trong prompt (delta khong con tool block).
    """
    attempts = max(1, int(CONFIG.get("retry_attempts", 2)))
    native_ok = (bool(tools_active) if tools_active is not None
                 else "# Tool Use" in (prompt or ""))
    in_chat = isinstance(session, dict) and session.get("mode") in ("continue", "fork")
    timeout = max(0.05, float(CONFIG.get("bridge_job_timeout", 300)))
    last_err = None

    def _client_gone():
        return cancel_event is not None and cancel_event.is_set()

    for attempt in range(attempts):
        if _client_gone():
            raise qwen.QwenError(
                "browser bridge: client da ngat ket noi, job bi huy", status=499)
        if not worker_alive():
            raise qwen.QwenError(
                "browser bridge: chua co tab chat.qwen.ai ket noi. Mo "
                "https://chat.qwen.ai (da dang nhap), giu tab mo va bat "
                "extension. Kiem tra: curl http://127.0.0.1:"
                f"{CONFIG.get('bridge_port', 8083)}/status", status=503)
        jid = uuid.uuid4().hex
        q = queue.Queue()
        now = time.time()
        with _lock:
            _jobs[jid] = q
            _managed_jobs[jid] = now
        if file_info and file_info.get("attachment_prompt"):
            # Luot cuoi cua user tach rieng: dung prompt (tin nhan cuoi + tool/rf).
            att_prompt = str(prompt or file_info["attachment_prompt"])
        elif file_info:
            # Fallback: khong dung prompt vi no chua "[Attached file: ...]".
            att_prompt = _file_companion(file_info)
        else:
            att_prompt = ""
        _debug_log(f"queueing job {jid} (model={model}, "
                   f"prompt_len={len(prompt or '')}, file={bool(file_info)}, "
                   f"attempt={attempt + 1}/{attempts}"
                   + (f", filename={file_info.get('filename')}"
                      if isinstance(file_info, dict) else "") + ")")
        job = {"job_id": jid, "model": model, "prompt": prompt,
               "think": bool(think), "ts": now,
               "deadline": now + timeout, "attempt": attempt,
               "mode": mode if mode in ("auto", "thinking", "fast") else None,
               "file_info": file_info,
               "attachment_prompt": att_prompt,
               "managed": True}
        if session is not None:
            job["session"] = session
            job["tools_active"] = bool(tools_active)
        _pending.put(job)
        job_sent = job_lost = False
        tracker = _Tracker()
        usage_seen = {}
        emitted = False
        seen_native = set()
        deadline = time.monotonic() + timeout
        try:
            while True:
                if _client_gone():
                    cancel_job(jid, "client disconnected")
                    raise qwen.QwenError(
                        "browser bridge: client da ngat ket noi, job bi huy",
                        status=499)
                try:
                    ev = q.get(timeout=min(1.0, max(0.05, deadline - time.monotonic())))
                except queue.Empty:
                    if _client_gone():
                        continue
                    if time.monotonic() >= deadline:
                        cancel_job(jid, "job timeout")
                        _log(f"[orphan] job {jid} timed out after {timeout}s "
                             "without response from browser tab")
                        raise qwen.QwenError(
                            "browser bridge: tab khong tra ket qua (timeout)",
                            status=504)
                    continue
                kind = ev.get("kind")
                # bridge_job_timeout la thoi gian IM LANG,
                # khong phai tong thoi gian job (stream dai khong bi giet).
                deadline = time.monotonic() + timeout
                if ev.get("cancelled"):
                    raise qwen.QwenError("browser bridge: job da bi huy", status=499)
                if kind == "delta":
                    text = ev.get("text") or ""
                    k = "reasoning" if ev.get("phase") == "reasoning" else "content"
                    if text:
                        d = tracker.feed(k, text)
                        if d:
                            emitted = True
                            yield k, d
                elif kind == "tool_call":
                    if not native_ok:
                        continue
                    name = ev.get("name") or ""
                    if not name:
                        continue
                    key = ev.get("id") or ev.get("call_id") or ev.get("index")
                    if key is not None:
                        # args trong key: 1 native call bi tach thanh nhieu
                        # JSON dung chung id nhung arguments khac.
                        key = (str(name), str(key), str(ev.get("arguments")))
                        if key in seen_native:
                            continue
                        seen_native.add(key)
                    args = ev.get("arguments")
                    if args is None:
                        args = {}
                    if isinstance(args, str):
                        s = args.strip()
                        if not s:
                            args = {}
                        else:
                            try:
                                args = json.loads(s, strict=False)
                            except ValueError:
                                parts = _split_json_objects(s)
                                if len(parts) > 1:
                                    _debug_log(f"native tool_call '{name}': tach "
                                               f"{len(parts)} arguments bi gop")
                                    for p in parts:
                                        d = tracker.feed(
                                            "content",
                                            _native_fence(name, p, ev.get("id")))
                                        if d:
                                            emitted = True
                                            yield "content", d
                                    continue
                                args = s
                    d = tracker.feed("content", _native_fence(name, args, ev.get("id")))
                    if d:
                        emitted = True
                        yield "content", d
                elif kind == "session":
                    # Extension da gui request completion (chat_id/parent_id doc tu body that).
                    job_sent = True
                    if session_out is not None:
                        for k in ("nonce", "chat_id", "parent_id"):
                            if isinstance(ev.get(k), str):
                                session_out[k] = ev[k]
                        session_out["sent"] = True
                    _debug_log(f"[job {jid[:8]}] session event: mode={ev.get('mode')} "
                               f"chat={str(ev.get('chat_id') or '-')[:8]} "
                               f"parent={str(ev.get('parent_id') or '-')[:8]}")
                elif kind == "usage":
                    if isinstance(ev.get("usage"), dict):
                        usage_seen.update(ev["usage"])
                    if usage_out is not None and isinstance(ev.get("usage"), dict):
                        usage_out.update(ev["usage"])
                elif kind in ("debug", "log"):
                    _debug_log(f"[job {jid[:8]}] extension: " + str(ev.get("message") or ""))
                elif kind == "warn":
                    _log(f"[job {jid[:8]}] extension warn: " + str(ev.get("message") or ""))
                    if ev.get("tab_reloaded"):
                        raise qwen.QwenError(
                            "browser bridge: tab reload giua chung job (buoc: "
                            + str(ev.get("stage") or "?") + ")", status=502)
                    if ev.get("model_mismatch") and CONFIG.get("bridge_require_model"):
                        raise qwen.QwenError(
                            "browser bridge: khong chon duoc model yeu cau ("
                            + str(ev.get("message") or model) + ")", status=502)
                    if ev.get("mode_mismatch") and CONFIG.get("bridge_require_mode"):
                        raise qwen.QwenError(
                            "browser bridge: khong chon duoc che do UI yeu cau ("
                            + str(ev.get("message") or mode) + ")", status=502)
                elif kind == "error":
                    if ev.get("session_lost"):
                        job_lost = True
                        if session_out is not None:
                            session_out["session_lost"] = True
                    if ev.get("dirty") and session_out is not None:
                        session_out["dirty"] = True
                    raise qwen.QwenError(
                        "browser bridge: " + str(ev.get("message") or "unknown"),
                        status=502)
                elif kind == "done":
                    if not tracker.total["content"] and not tracker.total["reasoning"]:
                        raise qwen.QwenError(
                            "browser bridge: upstream khong tra ve text nao",
                            status=502)
                    _debug_log(f"job {jid} completed successfully "
                               f"({len(tracker.total['content'])} content, "
                               f"{len(tracker.total['reasoning'])} reasoning)")
                    if file_info:
                        _log_attach_report(jid, attempt, file_info, usage_seen,
                                           tracker.total["content"])
                    return
        except GeneratorExit:
            # Client dong stream: phai danh dau huy, neu khong extension
            # /requeue la job chet song lai.
            cancel_job(jid, "caller closed stream")
            raise
        except Exception as e:
            last_err = e if isinstance(e, qwen.QwenError) else qwen.QwenError(str(e))
            if getattr(last_err, "status", None) == 499 or _client_gone():
                cancel_job(jid, "client disconnected")
                _debug_log(f"job {jid} cancelled, khong retry")
                raise last_err
            if emitted:
                _debug_log(f"job {jid} raising after partial stream: {last_err}")
                raise last_err
            if in_chat and (job_sent or job_lost):
                _log(f"job {jid}: session {session.get('mode')} "
                     f"{'mat session' if job_lost else 'da gui request'} -> khong retry: "
                     f"{last_err}")
                raise last_err
            if attempt < attempts - 1:
                _log(f"retry {attempt + 1}/{attempts}: {last_err}")
                delay = max(0.0, _as_num(CONFIG.get("retry_delay_sec"), 2))
                if cancel_event is not None:
                    cancel_event.wait(delay)
                else:
                    time.sleep(delay)
        finally:
            with _lock:
                _jobs.pop(jid, None)
    raise last_err


def generate(prompt, model, think, file_info=None, mode=None, cancel_event=None,
             session=None, session_out=None, tools_active=None):
    """Nhan cancel_event de huy job; join bang "". Gop het delta thanh content."""
    t0 = time.monotonic()
    usage, content, reasoning = {}, [], []
    extra = {}
    if mode:
        extra["mode"] = mode
    if cancel_event is not None:
        extra["cancel_event"] = cancel_event
    if session is not None:
        extra.update(session=session, session_out=session_out, tools_active=tools_active)
    for kind, text in generate_stream(prompt, model, think, usage,
                                      file_info=file_info, **extra):
        (reasoning if kind == "reasoning" else content).append(text)
    out = {"content": "".join(content), "reasoning": "".join(reasoning),
           "usage": usage}
    qwen.log(f"[timing] bridge generate: {time.monotonic() - t0:.1f}s, "
             f"{len(out['content'])} chars (+{len(out['reasoning'])} reasoning)")
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _origin_ok(self):
        origin = self.headers.get("Origin", "")
        return origin == _ALLOWED_ORIGIN or origin.startswith("chrome-extension://")

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin == _ALLOWED_ORIGIN or origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        # Serve file prompt tạm được tạo bởi server.py khi prompt quá dài
        if path.startswith("/file/"):
            filename = os.path.basename(path[len("/file/"):])
            if not filename.startswith("qwen_prompt_"):
                return self._json({"error": "invalid file"}, 400)

            tmpdir = os.path.abspath(tempfile.gettempdir())
            filepath = os.path.join(tmpdir, filename)

            # Chỉ serve khi file nằm trực tiếp trong tmpdir
            if (os.path.exists(filepath)
                    and os.path.dirname(os.path.abspath(filepath)) == tmpdir):
                try:
                    with open(filepath, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(content)
                    # Khong xoa tai day: server co the retry cung file_info
                    # (tool retry), va server.py tu don file sau khi luot goi xong.
                    return
                except Exception as e:
                    _log(f"file serve error: {e}")
            self._json({"error": "not found"}, 404)
            return

        # Extension hoi job con song TRUOC moi thao tac UI. GET /alive?job_id=<jid>
        if path == "/alive":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            jid = ""
            for pair in qs.split("&"):
                if pair.startswith("job_id="):
                    jid = pair[7:]
            return self._json({"job_id": jid, "alive": job_alive(jid),
                               "cancelled": job_cancelled(jid)})
        if path == "/status":
            return self._json(status())
        if path == "/debug":
            if not qwen.is_debug():
                return self._json({"error": "debug mode not enabled; set \"debug\": true in config"}, 403)
            with _lock:
                n_jobs = len(_jobs)
                n_models = len(_models)
            safe_keys = ("debug", "log_requests", "temporary_chat", "temporary-chat",
                         "bridge_select_model", "temporary_chat_reload", "captcha_wait_seconds", "default_model",
                         "bridge_select_mode", "bridge_force_request_model", "prompt_max_bytes",
                         "file_prompt_max_chars", "inline_prompt_max_chars",
                         "retry_attempts",
                         "bridge_job_timeout", "bridge_poll_wait", "bridge_worker_ttl",
                         "attachment_prompt", "log_file", "bridge_port", "port",
                         "models", "qwen_builtin_tools", "log_session_file", "log_dir",
                         "log_save_prompt_files", "attachment_usage_warn_ratio",
                         "bridge_session_mode", "session_max_chars", "session_max_turns",
                         "session_idle_ttl", "session_file_threshold", "session_fork")
            cfg_snapshot = {k: CONFIG[k] for k in safe_keys if k in CONFIG}
            return self._json({
                "config": cfg_snapshot,
                "bridge": {
                    "worker_alive": worker_alive(),
                    "last_seen_ago": round(time.time() - _last_seen, 1) if _last_seen else None,
                    "pending_jobs": _pending.qsize(),
                    "active_jobs": n_jobs,
                    "known_models_count": n_models,
                    "temporary_chat": temporary_chat_enabled(),
                },
                "recent_log": qwen.get_recent_log(),
            })
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        global _last_seen
        path = self.path.split("?")[0].rstrip("/")
        if not self._origin_ok():
            return self._json({"error": "forbidden origin"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 8 * 1024 * 1024:
            return self._json({"error": "body too large"}, 413)
        raw = self.rfile.read(n) if n else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "invalid JSON"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "invalid JSON object"}, 400)
        _last_seen = time.time()
        if isinstance(data.get("models"), list):
            _remember_models(data["models"])
        if isinstance(data.get("tab"), dict):
            _remember_tab(data["tab"])
        if path == "/cancel":
            jid = data.get("job_id")
            if not isinstance(jid, str) or not jid.strip():
                return self._json({"error": "invalid job"}, 400)
            cancel_job(jid, data.get("reason") or "extension cancel")
            return self._json({"ok": True, "cancelled": True})
        if path == "/requeue":
            job = data.get("job")
            if not isinstance(job, dict) or not isinstance(job.get("job_id"), str) \
                    or not job["job_id"].strip():
                return self._json({"error": "invalid job"}, 400)
            job = dict(job)
            jid = job["job_id"].strip()
            job["job_id"] = jid
            if job_cancelled(jid):
                _log(f"refuse requeue: job {jid} da bi huy")
                return self._json({"ok": False, "cancelled": True,
                                   "error": "job cancelled"}, 409)
            with _lock:
                known = jid in _managed_jobs
                active = jid in _jobs
            if (known or job.get("managed")) and not active:
                _log(f"refuse requeue: job {jid} khong con caller (orphan)")
                return self._json({"ok": False, "cancelled": True,
                                   "error": "job no longer active"}, 409)
            now = time.time()
            job.setdefault("first_ts", now)
            rq = _as_num(job.get("requeues"), 0) + 1
            max_rq = _as_num(CONFIG.get("bridge_max_requeues"), 8)
            ttl = _as_num(CONFIG.get("bridge_job_timeout"), 300)
            age = now - _as_num(job.get("first_ts"), now)
            if max_rq > 0 and rq > max_rq:
                cancel_job(jid, "too many requeues")
                return self._json({"ok": False, "cancelled": True,
                                   "error": "too many requeues"}, 409)
            if ttl > 0 and age > ttl:
                cancel_job(jid, "requeue ttl exceeded")
                return self._json({"ok": False, "cancelled": True,
                                   "error": "job expired"}, 409)
            job["requeues"] = int(rq)
            job["ts"] = now
            if known:
                job["managed"] = True
            _pending.put(job)
            _log(f"requeued job {jid} (#{int(rq)})")
            return self._json({"ok": True})
        if path == "/poll":
            worker = data.get("worker", "unknown")
            deadline = time.time() + float(CONFIG.get("bridge_poll_wait", 20))
            ttl = float(CONFIG.get("bridge_job_timeout", 300))
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return self._json({"job": None})
                try:
                    job = _pending.get(timeout=remaining)
                except queue.Empty:
                    return self._json({"job": None})
                jid = job.get("job_id")
                age = time.time() - job.get("ts", 0)
                if age > ttl:
                    _log(f"[orphan] dropping expired job {jid} "
                         f"(queued {age:.1f}s > ttl {ttl}s)")
                    continue
                with _lock:
                    is_active = jid in _jobs
                    is_cancelled = jid in _cancelled
                if is_cancelled:
                    _log(f"[orphan] dropping cancelled job {jid}")
                    continue
                if job.get("managed") and not is_active:
                    _log(f"[orphan] dropping orphan job {jid} "
                         "(caller disconnected or cancelled)")
                    continue
                _debug_log(f"worker '{worker}' polled job {jid} "
                           f"(model={job.get('model')}, age={age:.1f}s)")
                return self._json({"job": job})
        if path == "/event":
            jid = data.get("job_id")
            kind = data.get("kind")
            with _lock:
                q = _jobs.get(jid)
                dead = jid in _cancelled
            if q is not None and not dead:
                q.put(data)
                return self._json({"ok": True})
            if kind not in ("ping",):
                msg = data.get("message") or ""
                if jid in (None, "", "none"):
                    _log(f"extension warn: {msg}")
                elif data.get("tab_reloaded") or data.get("tab_reloading"):
                    _log(f"extension warn: {msg}")
                elif kind == "warn":
                    _log(f"extension warn (orphan job {jid}): {msg}")
                else:
                    _debug_log(f"event '{kind}' for unknown/orphan job {jid}: {msg}")
            # stale=true -> extension phai dung UI va bo response cu.
            return self._json({"ok": True, "stale": True, "cancelled": bool(dead)})
        self._json({"error": "not found"}, 404)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server():
    port = int(CONFIG.get("bridge_port", 8083))
    srv = _Server(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _log(f"listening on http://127.0.0.1:{port}")
    return srv