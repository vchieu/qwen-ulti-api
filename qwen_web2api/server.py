"""OpenAI-compatible HTTP server for Qwen Web."""
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import uuid
import tempfile
import os
import glob
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from . import __version__
from .config import CONFIG
from .models import MODELS, resolve_model, ui_mode_for_effort
from .qwen import QwenError, log, debug_log, save_prompt_copy
from . import bridge
from . import session as sess
from .tools import (_TRUNCATION_MARKER, build_response_format_instruction,
                    is_required_tool_choice,
                    looks_like_missed_tool_call, messages_to_prompt,
                    missing_required_params, parse_tool_calls, strip_code_fence,
                    tool_names, tool_parameters, tool_required_params)

ERR_INVALID = "invalid_request_error"
ERR_API = "api_error"
ERR_RATE = "rate_limit_error"
SSE_PING_INTERVAL = 5
# File prompt (paste -> document) phai <= 131072 ky tu, neu khong Qwen KHONG chuyen
# thanh file dinh kem (log that: history file 147719 chars -> mat lich su).
FILE_MAX_CHARS = 130000


def _file_max_chars():
    """Tran ky tu cua file prompt (config file_prompt_max_chars; sai/<=0 -> mac dinh)."""
    try:
        v = int(float(CONFIG.get("file_prompt_max_chars", FILE_MAX_CHARS)))
    except (TypeError, ValueError):
        return FILE_MAX_CHARS
    return v if v > 0 else FILE_MAX_CHARS
MAX_PROMPT_CHARS = 130000  # buffer an toàn dưới giới hạn 131072 ký tự của Qwen Web


def _inline_max_chars():
    """Nguong inline -> file (config inline_prompt_max_chars). <=0/sai = MAX_PROMPT_CHARS
    (hanh vi cu). Khong vuot MAX_PROMPT_CHARS.

    Them: extension bao nguong that su dung duoc (bridge.learned_inline_limit -- learnInlineLimit
    hoc khi Qwen doi paste dai thanh document / Send treo). Dung min(config, learned) de Python
    TACH history -> file + companion that ngay tu dau. Neu de extension tu chuyen thi CA prompt
    (ke ca tin user cuoi) vao 1 file: mat split, mat fingerprint, va dung nham cau companion.
    inline_prompt_use_learned=false -> bo qua gia tri hoc duoc (chi dung config).
    """
    try:
        v = int(float(CONFIG.get("inline_prompt_max_chars", MAX_PROMPT_CHARS)))
    except (TypeError, ValueError):
        v = MAX_PROMPT_CHARS
    if v <= 0:
        v = MAX_PROMPT_CHARS
    thr = min(v, MAX_PROMPT_CHARS)
    if CONFIG.get("inline_prompt_use_learned", True):
        learned = bridge.learned_inline_limit()
        if learned > 0:
            thr = min(thr, learned)
    return thr
_MSG_PREVIEW_CHARS = 200   # Option B: số ký tự preview mỗi message trong log


def _usage(prompt, text, upstream=None):
    if isinstance(upstream, dict) and upstream:
        p = upstream.get("prompt_tokens", upstream.get("input_tokens"))
        c = upstream.get("completion_tokens", upstream.get("output_tokens"))
        if isinstance(p, int) and isinstance(c, int):
            return {"prompt_tokens": p, "completion_tokens": c,
                    "total_tokens": p + c}
    p, c = len(prompt) // 4, len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _map_error(e):
    """giu nguyen 413/499/504 thay vi don ve 503."""
    status = getattr(e, "status", None)
    if status == 429:
        return 429, f"upstream rate limited: {e}", ERR_RATE, "rate_limit_exceeded"
    if status == 413:
        return 413, f"request too large: {e}", ERR_INVALID, "context_length_exceeded"
    if status == 499:
        return 499, f"client cancelled request: {e}", ERR_INVALID, "client_cancelled"
    if status == 504:
        return 504, f"upstream timeout: {e}", ERR_API, "upstream_timeout"
    return 503, f"upstream error: {e}", ERR_API, None


def _normalize_stop(stop):
    if isinstance(stop, str):
        return [stop] if stop else []
    if isinstance(stop, list):
        return [s for s in stop if isinstance(s, str) and s]
    return []


def _apply_stop(text, stops):
    if not text or not stops:
        return text
    cut = None
    for s in stops:
        i = text.find(s)
        if i != -1 and (cut is None or i < cut):
            cut = i
    return text[:cut] if cut is not None else text


def _apply_max_tokens(text, max_tokens):
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        return text, False
    limit = max_tokens * 4
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _cleanup_old_temp_files(max_age_sec=3600):
    """Xóa các file qwen_prompt_* tạm đã tồn tại quá max_age_sec.
    Tránh rác tồn đọng khi bridge.js crash / user đóng tab giữa chừng."""
    try:
        tmpdir = tempfile.gettempdir()
        now = time.time()
        for path in glob.glob(os.path.join(tmpdir, "qwen_prompt_*")):
            try:
                if now - os.path.getmtime(path) > max_age_sec:
                    os.unlink(path)
            except OSError:
                pass
    except Exception as e:
        log(f"[bridge] temp cleanup error: {e}")


def _preview(text, limit=None):
    """Cắt text về limit ký tự, thêm '…' nếu dài hơn."""
    limit = limit or _MSG_PREVIEW_CHARS
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", "")
    return text[:limit] + ("…" if len(text) > limit else "")


def _msg_text(content):
    """Lay text thuan tu content field (str hoac list of parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        return " ".join(p for p in parts if p)
    return str(content)


def _log_messages(cid, messages, tools=None):
    """Option B: log từng message với role + preview content.
    Chỉ ghi khi log_requests bật; preview bị truncate để không làm _recent_log
    overflow chỉ vì 1 request dài."""
    n = len(messages)
    tool_names_str = ""
    if tools:
        names = [((t.get("function") or t).get("name") or "?") for t in tools if isinstance(t, dict)]
        tool_names_str = f"  tools=[{', '.join(names)}]" if names else ""
    log(f"[req] {cid} — {n} message(s){tool_names_str}")
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        # tool_calls: liệt kê tên hàm thay vì dump full JSON
        if role == "assistant" and m.get("tool_calls"):
            tc_names = [c.get("function", {}).get("name", "?")
                        for c in m["tool_calls"] if isinstance(c, dict)]
            log(f"[req] {cid}  [{i}] {role}: <tool_calls: {', '.join(tc_names)}>")
            continue
        # tool result
        if role == "tool":
            content_str = _msg_text(m.get("content"))
            log(f"[req] {cid}  [{i}] tool(id={m.get('tool_call_id', '?')}): "
                f"{_preview(content_str)}")
            continue
        content_str = _msg_text(m.get("content"))
        log(f"[req] {cid}  [{i}] {role}({len(content_str)}c): {_preview(content_str)}")


def _log_turn_result(cid, text, reasoning, calls, usage, elapsed):
    """Option B: log kết quả sau mỗi turn — text preview, reasoning, tool calls, usage, timing."""
    parts = []
    if reasoning:
        parts.append(f"reasoning={len(reasoning)}c")
    if text:
        parts.append(f"text={len(text)}c: {_preview(text)}")
    if calls:
        tc_names = [c.get("function", {}).get("name", "?") for c in calls if isinstance(c, dict)]
        parts.append(f"tool_calls=[{', '.join(tc_names)}]")
    if usage:
        p = usage.get("prompt_tokens", usage.get("input_tokens", "?"))
        c = usage.get("completion_tokens", usage.get("output_tokens", "?"))
        parts.append(f"usage=({p}+{c}tok)")
    parts.append(f"elapsed={elapsed:.1f}s")
    log(f"[res] {cid} — {' | '.join(parts)}")


def _render_fit(messages, tools=None, tool_choice=None, prefix="", limit=None):
    """MAC DINH KHONG CAT.

    Qwen co the chuyen paste thanh file (A/B: van dung o ~320k ky tu); neu that bai extension co
    duong attach khac (paste-text, file-input, drop-file) neu paste that bai.
    Cat o day = mat evidence
    (log loi thuong nam o CUOI message). Chi cat khi
    CONFIG["file_prompt_truncate"] = true.
    """
    limit = limit or _file_max_chars()
    text = messages_to_prompt(messages, tools, tool_choice, max_bytes=1 << 30)
    orig = len(text)
    if not CONFIG.get("file_prompt_truncate"):
        if len(prefix) + orig > limit:
            log(f"[bridge] file prompt {len(prefix) + orig} chars > paste limit "
                f"{limit}: giu nguyen noi dung, extension thu paste-file roi "
                "drop-file, fallback paste-text/file-input (khong cat)")
        return prefix + text
    budget = max(1000, limit - len(prefix))
    if orig > budget:
        max_bytes = int(len(text.encode("utf-8")) * budget / orig * 0.98)
        for _ in range(8):
            text = messages_to_prompt(messages, tools, tool_choice,
                                      max_bytes=max_bytes)
            if len(text) <= budget:
                break
            max_bytes = int(max_bytes * 0.9)
        log(f"[bridge] file prompt {orig} chars > {limit}: truncated to "
            f"{len(text)} chars (file_prompt_truncate=true)")
    return prefix + text


def _roles_summary(messages):
    """[{role:user},...] -> "user:3,assistant:2" (de log layout file prompt)."""
    counts = {}
    for m in messages or []:
        role = str(m.get("role")) if isinstance(m, dict) else "?"
        counts[role] = counts.get(role, 0) + 1
    return ",".join(f"{r}:{n}" for r, n in counts.items()) or "-"


def _fingerprint_prompt_file(file_info, content, layout=""):
    """Dau van tay cua file prompt (bytes/sha256/lines/truncated) -> luu vao file_info de
    extension doi chieu voi file no that su dinh kem, va ghi log de so voi dong
    'completion request' / '[attach-check]'. Khong bao gio lam hong request."""
    try:
        raw = content.encode("utf-8")
        file_info["bytes"] = len(raw)
        file_info["sha256"] = hashlib.sha256(raw).hexdigest()
        file_info["lines"] = content.count("\n") + 1
        file_info["truncated"] = _TRUNCATION_MARKER in content
        log(f"[attach] file {file_info.get('filename')}: chars={len(content)} "
            f"bytes={file_info['bytes']} lines={file_info['lines']} "
            f"sha256={file_info['sha256'][:16]} truncated={file_info['truncated']} "
            f"over_paste_limit={file_info.get('over_paste_limit')} {layout}".rstrip())

        def flat(s):
            return s.replace("\r", " ").replace("\n", " ")
        debug_log(f"[attach] head={flat(content[:120])!r} tail={flat(content[-120:])!r}")
        saved = save_prompt_copy(file_info.get("path"), file_info.get("filename"))
        if saved:
            log(f"[attach] saved copy: {saved}")
    except Exception as e:
        log(f"[attach] fingerprint error: {e}")


def prepare_large_prompt(messages, tools=None, tool_choice=None):
    """Prompt vuot MAX_PROMPT_CHARS -> tach history sang file .txt tam.

      - KHONG cat noi dung (tru khi file_prompt_truncate=true).
      - file_info them paste_limit / over_paste_limit de extension thu paste
        truoc; cac duong khac chi la fallback khi paste that bai.
      - prompt_hard_max_chars > 0 va vuot -> QwenError 413, khong cat im lang.
    """
    full_prompt = messages_to_prompt(messages, tools, tool_choice)
    hard = 0
    try:
        hard = int(float(CONFIG.get("prompt_hard_max_chars", 0) or 0))
    except (TypeError, ValueError):
        hard = 0
    if hard > 0 and len(full_prompt) > hard:
        raise QwenError(f"prompt too large: {len(full_prompt)} chars > "
                        f"prompt_hard_max_chars={hard}", status=413)
    thr = _inline_max_chars()
    debug_log(f"inline check: full_prompt={len(full_prompt)} thr={thr} "
              f"(config={CONFIG.get('inline_prompt_max_chars')}, "
              f"learned={bridge.learned_inline_limit()}, "
              f"use_learned={bool(CONFIG.get('inline_prompt_use_learned', True))}) "
              f"-> {'file' if len(full_prompt) > thr else 'inline'}")
    if len(full_prompt) <= thr:
        return None, messages
    _cleanup_old_temp_files()
    paste_limit = _file_max_chars()
    can_split = False
    companion_prompt = ""
    last_msg = None
    if len(messages) > 1 and isinstance(messages[-1], dict) \
            and messages[-1].get("role") == "user":
        last_msg = messages[-1]
        companion_prompt = messages_to_prompt([last_msg], tools, tool_choice)
        if len(companion_prompt) <= thr:
            can_split = True
    if can_split:
        history = messages[:-1]
        file_content = _render_fit(history, prefix="[Conversation History]\n\n")
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="qwen_prompt_")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(file_content)
        file_info = {
            "path": path,
            "filename": os.path.basename(path),
            "size": len(file_content),
            "attachment_prompt": companion_prompt,
            "paste_limit": paste_limit,
            "over_paste_limit": len(file_content) > paste_limit,
        }
        _fingerprint_prompt_file(
            file_info, file_content,
            f"layout=split history={len(history)} msgs [{_roles_summary(history)}] "
            f"companion={len(companion_prompt)} chars")
        debug_log(f"large prompt split: history -> file ({len(file_content)} chars), "
                  f"companion prompt {len(companion_prompt)} chars, "
                  f"over_paste_limit={file_info['over_paste_limit']}")
        return file_info, [last_msg]
    last_role = messages[-1].get("role") if messages and isinstance(messages[-1], dict) else None
    reason = ("only 1 message" if len(messages) <= 1
              else f"last msg role={last_role!r}, not user"
              if last_role != "user"
              else f"last msg itself > inline threshold ({len(companion_prompt)} chars)")
    log(f"[bridge] large prompt: cannot split ({reason}), writing full prompt to file")
    full_prompt = _render_fit(messages, tools, tool_choice)
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="qwen_prompt_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(full_prompt)
    file_info = {
        "path": path,
        "filename": os.path.basename(path),
        "size": len(full_prompt),
        "paste_limit": paste_limit,
        "over_paste_limit": len(full_prompt) > paste_limit,
    }
    _fingerprint_prompt_file(
        file_info, full_prompt,
        f"layout=full messages={len(messages)} [{_roles_summary(messages)}] reason={reason}")
    short_messages = [{
        "role": "user",
        "content": (
            f"[Attached file: {file_info['filename']}]\n"
            f"Please read and respond to the content in the attached file."
        ),
    }]
    return file_info, short_messages


def _remove_prompt_file(file_info):
    """Xóa file prompt tạm sau khi lượt gọi kết thúc (kể cả khi retry)."""
    path = (file_info or {}).get("path")
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _session_prompt_file(content, prompt):
    """Delta session qua dai de go inline -> ghi file tam (cung co che prepare_large_prompt);
    `prompt` (tin nhan di kem file) la companion o composer."""
    _cleanup_old_temp_files()
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="qwen_prompt_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    paste_limit = _file_max_chars()
    file_info = {
        "path": path,
        "filename": os.path.basename(path),
        "size": len(content),
        "attachment_prompt": prompt,
        "paste_limit": paste_limit,
        "over_paste_limit": len(content) > paste_limit,
    }
    _fingerprint_prompt_file(file_info, content, "layout=session-delta")
    return file_info


def _compose_prompt(plan, messages, tools, tool_choice, rf):
    """Messages client -> (file_info, prompt, messages_da_tach).

    plan None / new: duong cu (prepare_large_prompt + messages_to_prompt); plan new them
    preamble giao thuc session vao CUOI prompt dau tien (file_info co attachment_prompt thi vao
    composer cung prompt; file chua toan bo thi vao extra_instructions). continue/fork: chi
    delta (plan.prompt, hoac file khi qua dai).
    """
    if plan is not None and plan.kind != sess.KIND_NEW:
        prompt = plan.prompt + (rf or "")
        file_info = None
        if plan.file_text is not None:
            file_info = _session_prompt_file(plan.file_text, prompt)
        return file_info, prompt, messages
    file_info, messages = prepare_large_prompt(messages, tools, tool_choice)
    if file_info:
        log(f"[bridge] large prompt detected ({file_info['size']} chars), "
            f"created file: {file_info['filename']}")
    prompt = messages_to_prompt(messages, tools, tool_choice)
    pre = ("\n\n" + sess.STORE.preamble(plan.tools_active)) if plan is not None else ""
    if rf:
        prompt += rf
    prompt += pre
    if file_info and file_info.get("attachment_prompt"):
        file_info["attachment_prompt"] = prompt
    elif file_info:
        # Prompt nam TOAN BO trong file -> composer chi nhan cau companion. Session phai dung
        # cau dung ngu canh (session.job_session), khong dung attachment_prompt cua config.
        if plan is not None:
            cp = str(plan.job_session().get("attach_prompt") or "").strip()
            if cp:
                file_info["companion_prompt"] = cp
        if rf or pre:
            # response_format/preamble phai vao composer (prompt chi la "[Attached file: ...]").
            file_info["extra_instructions"] = ((rf or "") + pre).strip()
    if plan is not None:
        # Luot new = replay day du: tinh vao chars_sent de session_max_chars dung (truoc day
        # luot new chi tinh do dai cau tra loi, vd "chars=0" trong log).
        plan.delta_chars = (file_info["size"] if file_info else 0) + len(prompt)
    return file_info, prompt, messages


def _text_format(text_field):
    """Responses API text.format -> Chat response_format."""
    if not isinstance(text_field, dict):
        return None
    fmt = text_field.get("format")
    if not isinstance(fmt, dict):
        return None
    if fmt.get("type") == "json_object":
        return {"type": "json_object"}
    if fmt.get("type") == "json_schema":
        if isinstance(fmt.get("json_schema"), dict):
            return {"type": "json_schema", "json_schema": fmt["json_schema"]}
        return {"type": "json_schema",
                "json_schema": {"schema": fmt.get("schema", {})}}
    return None


def _norm_resp_tools(tools):
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function", t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({"type": "function", "function": {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }})
    return out or None


def _responses_messages(inp, instructions):
    msgs = []
    if instructions:
        msgs.append({"role": "system", "content": instructions})
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
        return msgs
    for item in inp or []:
        if isinstance(item, str):
            msgs.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "function_call_output":
            out = item.get("output", "")
            if isinstance(out, list):
                out = "\n".join(c.get("text", "") for c in out
                                if isinstance(c, dict)
                                and c.get("type") in ("output_text", "text"))
            msgs.append({"role": "tool",
                         "tool_call_id": item.get("call_id", ""),
                         "content": out})
        elif t == "function_call":
            call = {"id": item.get("call_id") or item.get("id")
                          or f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": item.get("name", ""),
                                 "arguments": item.get("arguments") or "{}"}}
            if msgs and msgs[-1].get("role") == "assistant" \
                    and msgs[-1].get("tool_calls"):
                msgs[-1]["tool_calls"].append(call)
            else:
                msgs.append({"role": "assistant", "content": None,
                             "tool_calls": [call]})
        elif t == "input_text":
            msgs.append({"role": "user", "content": item.get("text", "")})
        elif item.get("role"):
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict)
                    and c.get("type") in ("input_text", "output_text", "text"))
            msgs.append({"role": role, "content": content})
    return msgs


def _is_disconnect_error(exc):
    """loi socket nao nghia la 'client da ngat'.

    ConnectionError (reset/aborted/broken pipe, ke ca WinError 10053/10054) va
    OSError mang errno/winerror cua socket da dong/da ngat.
    """
    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, OSError):
        import errno as _errno
        codes = {getattr(_errno, n) for n in
                 ("ECONNRESET", "ECONNABORTED", "EPIPE", "ENOTCONN",
                  "ESHUTDOWN", "EBADF", "ENOTSOCK") if hasattr(_errno, n)}
        codes |= {10038, 10053, 10054, 10057, 10058}   # WSAE* (Windows)
        return (getattr(exc, "errno", None) in codes
                or getattr(exc, "winerror", None) in codes)
    return False


def _file_for_attempt(file_info, nudge):
    """prompt nam TOAN BO trong file (khong co
    file_info.attachment_prompt) thi composer chi nhan cau mac dinh -> loi nhac
    retry cua _run_turn bi mat. Gan nudge vao ban sao file_info de bridge dua
    vao composer. Job tach history/luot cuoi da dung `prompt` (co nudge) nen
    giu nguyen doi tuong goc.
    """
    if not file_info or not nudge or file_info.get("attachment_prompt"):
        return file_info
    out = dict(file_info)
    out["retry_nudge"] = str(nudge).strip()
    return out


class _Heartbeat:
    """SSE keep-alive while a buffered turn works upstream."""

    def __init__(self, handler, stop, gone):
        self.h, self.stop, self.gone = handler, stop, gone
        self.lock = threading.Lock()

    def ping(self):
        with self.lock:
            if self.stop.is_set():
                return False
            try:
                self.h._sse_write(b": ping\n\n")
                return True
            except Exception as e:
                # moi loi socket 'client ngat'
                # (ke ca ConnectionAbortedError / WinError 10053) deu danh dau gone.
                if _is_disconnect_error(e):
                    self.gone.set()
                return False

    def run(self):
        while not self.stop.wait(SSE_PING_INTERVAL):
            if not self.ping():
                return


class QwenHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log(f"{self.client_address[0]} {fmt % args}")

    # ------------------------------------------------------------ helpers --
    def _route(self):
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        provided = []
        auth = (self.headers.get("Authorization") or "").strip()
        if auth[:7].lower() == "bearer ":
            provided.append(auth[7:].strip())
        for h in ("x-api-key", "x-goog-api-key"):
            v = self.headers.get(h)
            if v:
                provided.append(v.strip())
        for pair in urllib.parse.urlparse(self.path).query.split("&"):
            if pair.startswith("key="):
                provided.append(urllib.parse.unquote(pair[4:]))
        return any(isinstance(k, str) and hmac.compare_digest(v.encode(), k.encode())
                   for v in provided for k in keys)

    def _cors(self):
        origins = CONFIG.get("cors_origins") or []
        if not origins:
            return
        req_origin = self.headers.get("Origin", "")
        origin = "*" if "*" in origins else (
            req_origin if req_origin in origins else "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, x-api-key")

    def _security_ok(self):
        if CONFIG.get("api_keys"):
            return True
        host = self.headers.get("Host", "")
        if host:
            hn = (urllib.parse.urlsplit("//" + host).hostname or "").lower()
            allowed = {"localhost", "127.0.0.1", "::1"}
            bind = (CONFIG.get("host") or "").lower().strip("[]")
            if bind and bind not in ("0.0.0.0", "::", "*"):
                allowed.add(bind)
            if hn and hn not in allowed:
                return False
        origin = self.headers.get("Origin")
        origins = CONFIG.get("cors_origins") or []
        if origin and "*" not in origins and origin not in origins:
            return False
        return True

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_api_error(self, status, message, type_=ERR_API, code=None):
        self.send_json({"error": {"message": message, "type": type_,
                                  "param": None, "code": code}}, status)

    def _sse_write(self, data):
        lock = getattr(self, "_sse_lock", None)
        if lock is None:
            self.wfile.write(data)
            self.wfile.flush()
            return
        with lock:
            self.wfile.write(data)
            self.wfile.flush()

    def _start_sse(self):
        self._sse_lock = threading.Lock()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        stop, gone = threading.Event(), threading.Event()
        hb = _Heartbeat(self, stop, gone)
        threading.Thread(target=hb.run, daemon=True).start()
        return stop.set, gone, hb

    def _sse_chunk(self, cid, model, delta, finish, include_usage=False):
        chunk = {"id": cid, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": model,
                 "choices": [{"index": 0, "delta": delta, "logprobs": None,
                              "finish_reason": finish}]}
        if include_usage:
            chunk["usage"] = None
        self._sse_write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

    def _sse_usage(self, cid, model, prompt, text, upstream):
        chunk = {"id": cid, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": model, "choices": [],
                 "usage": _usage(prompt, text, upstream)}
        self._sse_write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

    def _sse_error(self, status, message, type_=ERR_API, code=None):
        payload = {"error": {"message": message, "type": type_,
                             "param": None, "code": code}}
        self._sse_write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())

    def _sse_done(self):
        self._sse_write(b"data: [DONE]\n\n")

    def _parse(self, body):
        try:
            return json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return None

    def _read_body(self):
        limit = int(CONFIG.get("max_body_bytes", 32 * 1024 * 1024))
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    break
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise ValueError("invalid chunked request body")
                if size == 0:
                    while True:
                        t = self.rfile.readline(65536)
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > limit:
                    raise ValueError("request body too large")
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError("truncated request body")
                chunks.append(data)
                cr = self.rfile.read(1)
                if cr == b"\r":
                    self.rfile.read(1)
            return b"".join(chunks)
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise ValueError("invalid Content-Length header")
        if n < 0:
            raise ValueError("invalid Content-Length header")
        if n > limit:
            raise ValueError("request body too large")
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            raise ValueError("incomplete request body")
        return data

    def _resolve(self, req):
        raw = req.get("model")
        base = raw.split("@think=")[0] if isinstance(raw, str) else raw
        if (CONFIG.get("strict_models") and isinstance(base, str) and base
                and base not in MODELS
                and not any(m["id"] == base for m in bridge.known_models())):
            self.send_api_error(404, f"model '{raw}' not found",
                                ERR_INVALID, "model_not_found")
            return None
        name, upstream, think, err = resolve_model(
            raw or CONFIG["default_model"], CONFIG["default_model"],
            bool(CONFIG.get("thinking")))
        if err:
            self.send_api_error(400, err, ERR_INVALID)
            return None
        effort = req.get("reasoning_effort")
        if not isinstance(effort, str) and isinstance(req.get("reasoning"), dict):
            effort = req["reasoning"].get("effort")
        if isinstance(effort, str):
            e = effort.strip().lower()
            if e in ("minimal", "low", "none"):
                think = False
            elif e in ("medium", "high"):
                think = True
        # Che do UI Thinking/Auto/Fast: none/low -> fast, medium -> auto, high -> thinking;
        # khong co reasoning_effort -> None (giu nguyen UI).
        mode = ui_mode_for_effort(effort)
        if mode is None and isinstance(raw, str) and "@think=" in raw:
            mode = "thinking" if think else "fast"
        self._ui_mode = mode
        echo = raw if isinstance(raw, str) and raw else name
        log(f"[model] {echo} -> upstream={upstream} thinking={think} "
            f"ui_mode={mode or 'default'}")
        return echo, upstream, think

    def _mode_kw(self):
        """kwargs che do UI cho bridge; rong = khong dung toi UI."""
        mode = getattr(self, "_ui_mode", None)
        return {"mode": mode} if mode else {}

    # ---------------------------------------------------------- tool turn --
    def _watch_disconnect(self):
        """Phat hien client ngat ket noi cho request NON-STREAM (khong co heartbeat SSE). Tra ve (event, stop_fn);
        khong co socket that -> (None, None) de unit test khong bi anh huong."""
        sock = getattr(self, "connection", None)
        if sock is None or not CONFIG.get("bridge_cancel_on_disconnect", True):
            return None, None
        gone = threading.Event()
        stop = threading.Event()

        def _run():
            import select
            import socket as _socket
            # loi socket cho thay client da
            # ngat (reset/abort/broken pipe/socket da dong) -> DANH DAU gone roi
            # moi thoat. Truoc day `except Exception: return` thoat im lang nen
            # request cu van chay tiep.
            while not stop.wait(0.5):
                try:
                    r, _w, _x = select.select([sock], [], [], 0)
                    if not r:
                        continue
                    if not sock.recv(1, _socket.MSG_PEEK):
                        gone.set()
                        return
                except (BlockingIOError, InterruptedError, _socket.timeout):
                    continue
                except ValueError:
                    # select/recv tren socket da dong (fileno = -1)
                    gone.set()
                    return
                except Exception as e:
                    if _is_disconnect_error(e):
                        debug_log(f"client disconnect detected: {e!r}")
                        gone.set()
                    return

        threading.Thread(target=_run, daemon=True).start()
        return gone, stop.set

    def _run_turn(self, prompt, upstream, think, tools_active, allowed,
                  schemas, required_params, required_tool, client_gone=None,
                  file_info=None, on_reasoning_delta=None, plan=None):
        """Huy job khi client ngat; tu choi tool_call co arguments hong."""
        stop_watch = None
        if client_gone is None:
            client_gone, stop_watch = self._watch_disconnect()
        extra = dict(self._mode_kw())
        if client_gone is not None:
            extra["cancel_event"] = client_gone
        if plan is not None:
            extra.update(plan.bridge_kwargs())
        attempts = 1
        if required_tool or (tools_active and CONFIG.get("tool_retry_on_miss")):
            attempts = 1 + max(0, int(CONFIG.get("tool_retry_attempts", 1)))
        text = reasoning = ""
        calls, usage, nudge = None, {}, ""
        try:
            for attempt in range(attempts):
                if client_gone is not None and client_gone.is_set():
                    raise QwenError("client disconnected; job cancelled", status=499)
                call_prompt = prompt + nudge if nudge else prompt
                call_file = _file_for_attempt(file_info, nudge)
                kw = extra
                if plan is not None and attempt > 0 and plan.kind != sess.KIND_NEW:
                    # chat Qwen da co delta o lan 1: chi gui loi nhac (hoac fork).
                    call_prompt, call_file, sj = plan.retry_call(prompt, file_info, nudge)
                    kw = dict(extra, session=sj)
                debug_log(f"run_turn attempt {attempt + 1}/{attempts} "
                          f"(tools_active={tools_active}, required_tool={bool(required_tool)})")
                _t0 = time.time()
                if on_reasoning_delta:
                    content_parts, reasoning_parts, usage = [], [], {}
                    for kind, delta in bridge.generate_stream(
                            call_prompt, upstream, think, usage,
                            file_info=call_file, **kw):
                        if client_gone is not None and client_gone.is_set():
                            break
                        if kind == "reasoning":
                            reasoning_parts.append(delta)
                            on_reasoning_delta(delta)
                        else:
                            content_parts.append(delta)
                    text = "".join(content_parts)
                    reasoning = "".join(reasoning_parts)
                else:
                    result = bridge.generate(call_prompt, upstream, think,
                                             file_info=call_file, **kw)
                    text, reasoning, usage = (result["content"], result["reasoning"],
                                              result["usage"])
                if client_gone is not None and client_gone.is_set():
                    raise QwenError("client disconnected; job cancelled", status=499)
                debug_log(f"run_turn elapsed={time.time() - _t0:.1f}s "
                          f"text={len(text)}c reasoning={len(reasoning)}c")
                calls, dropped = None, []
                if tools_active and text:
                    import re as _re
                    _fences = _re.findall(r"```tool_call\b.*?```", text, _re.DOTALL)
                    if _fences:
                        debug_log(f"raw tool fence(s) ({len(_fences)}):\n" +
                                  "\n---\n".join(f[:400] for f in _fences))
                    text, calls, dropped = parse_tool_calls(text, allowed, schemas)
                bad_args = [d for d in dropped
                            if str(d).startswith("unusable arguments")]
                if calls and not bad_args:
                    debug_log(f"parsed {len(calls)} tool call(s): "
                              f"{[c['function']['name'] for c in calls]}")
                    missing = missing_required_params(calls, required_params)
                    if missing and attempt + 1 < attempts:
                        nudge = ("\n\nIMPORTANT: Your previous tool call was missing "
                                 "required parameter(s): " + ", ".join(missing)
                                 + ". Emit the tool_call again with every required "
                                 "parameter.")
                        continue
                    if missing:
                        raise QwenError("model emitted invalid tool call; missing: "
                                        + ", ".join(missing))
                    if text:
                        text = text.strip()
                    return text, reasoning, calls, usage
                if bad_args:
                    # KHONG tra call voi arguments {} gia tao.
                    if attempt + 1 < attempts:
                        debug_log(f"unusable tool arguments, nudge retry: {bad_args}")
                        nudge = ("\n\nIMPORTANT: Your previous tool_call had unusable "
                                 "arguments (" + "; ".join(bad_args) + "). Emit the "
                                 'tool_call again as valid JSON: {"name": "<tool_name>", '
                                 '"arguments": {<parameters>}} with every parameter '
                                 "inside the arguments object.")
                        continue
                    raise QwenError("model emitted tool_call with unusable arguments: "
                                    + "; ".join(bad_args))
                if dropped and tools_active:
                    # block bị drop (undeclared tool, JSON hỏng)
                    # vẫn để lại raw fence trong text nên looks_like_missed_tool_call
                    # trả False; phải nudge theo đúng lý do drop.
                    if attempt + 1 < attempts:
                        names = ", ".join(sorted(allowed)) if allowed else "(none)"
                        debug_log(f"dropped tool_call block(s), nudge retry: {dropped}")
                        nudge = ("\n\nIMPORTANT: Your previous tool_call block was "
                                 "rejected (" + "; ".join(str(d) for d in dropped)
                                 + "). Use ONLY these tools: " + names
                                 + '. Emit the call again as valid JSON: '
                                 '{"name": "<tool_name>", "arguments": {<parameters>}}.')
                        continue
                if required_tool:
                    nudge = "\n\nIMPORTANT: Respond with a tool_call block ONLY."
                    continue
                if attempt + 1 >= attempts:
                    break
                if tools_active and (looks_like_missed_tool_call(text)
                                     or not text.strip()):
                    debug_log("detected missed tool call; sending nudge retry")
                    nudge = ("\n\nIMPORTANT: Do not describe a tool call -- make it. "
                             "If answering requires reading or changing something, "
                             "call the tool now; otherwise give your final answer.")
                    continue
                break
            if required_tool and not calls:
                raise QwenError("model did not emit a usable required tool call; "
                                "please retry")
            return text, reasoning, calls, usage
        finally:
            if stop_watch:
                stop_watch()

    # ------------------------------------------------------------- routes --
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        try:
            path = self._route()
            if path not in ("/", "/health") and not self._authorized():
                return self.send_api_error(401, "invalid api key",
                                           ERR_INVALID, "invalid_api_key")
            if path in ("/", "/health"):
                self.send_json({"status": "ok", "version": __version__})
            elif path in ("/v1/models", "/models"):
                seen, data = set(), []
                for n, c in MODELS.items():
                    seen.add(n)
                    data.append({"id": n, "object": "model",
                                 "created": 1700000000, "owned_by": "qwen",
                                 "description": c.get("desc", "")})
                for m in bridge.known_models():
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        data.append({"id": m["id"], "object": "model",
                                     "created": 1700000000, "owned_by": "qwen",
                                     "description": m.get("desc") or m.get("name", "")})
                self.send_json({"object": "list", "data": data})
            elif path.startswith("/v1/models/") or path.startswith("/models/"):
                mid = path.rsplit("/", 1)[-1]
                if mid in MODELS or any(m["id"] == mid for m in bridge.known_models()):
                    self.send_json({"id": mid, "object": "model",
                                     "created": 1700000000, "owned_by": "qwen"})
                else:
                    self.send_api_error(404, f"model '{mid}' not found",
                                        ERR_INVALID, "model_not_found")
            else:
                self.send_api_error(404, f"not found: {path}", ERR_INVALID)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")
            try:
                self.send_api_error(500, str(e))
            except Exception:
                pass

    def do_POST(self):
        try:
            path = self._route()
            if not self._authorized():
                return self.send_api_error(401, "invalid api key",
                                           ERR_INVALID, "invalid_api_key")
            if not self._security_ok():
                return self.send_api_error(403, "forbidden host/origin", ERR_INVALID)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send_api_error(415, "Content-Type must be application/json",
                                           ERR_INVALID)
            body = self._read_body()
            if path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_chat(body)
            elif path in ("/v1/responses", "/responses"):
                self._handle_responses(body)
            else:
                self.send_api_error(404, f"not found: {path}", ERR_INVALID)
        except QwenError as e:
            # prepare_large_prompt co the raise 413
            self.send_api_error(*_map_error(e))
        except ValueError as e:
            self.send_api_error(413 if "too large" in str(e) else 400,
                                str(e), ERR_INVALID)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_api_error(500, str(e))
            except Exception:
                pass

    # ---------------------------------------------------- chat completions --
    def _handle_chat(self, body):
        with sess.turn_guard():
            return self._handle_chat_locked(body)

    def _session_plan(self, api, messages, tools, tool_choice, tools_active):
        """Plan cua che do session (None khi tat). Loi bat ngo -> khong dung session."""
        if not sess.enabled():
            return None
        try:
            return sess.STORE.plan(messages, tools, tool_choice, tab=bridge.tab_state(),
                                   api=api, tools_active=tools_active)
        except Exception as e:
            log(f"[session] plan error ({e}); chay khong session")
            sess.STORE.reset("plan_error")
            return None

    def _handle_chat_locked(self, body):
        req = self._parse(body)
        if not isinstance(req, dict):
            return self.send_api_error(400, "invalid JSON body", ERR_INVALID)
        resolved = self._resolve(req)
        if not resolved:
            return
        echo, upstream, think = resolved
        messages = req.get("messages", [])
        if not isinstance(messages, list):
            return self.send_api_error(400, "messages must be a list", ERR_INVALID)

        n = req.get("n", 1)
        if isinstance(n, int) and n > 1:
            return self.send_api_error(400, "n>1 is not supported",
                                       ERR_INVALID, "unsupported_value")
        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        tools_active = bool(tools) and tool_choice != "none"
        all_messages = messages
        plan = self._session_plan("chat", all_messages, tools, tool_choice, tools_active)

        # Detect prompt lớn và tạo file tạm nếu cần (prepare_large_prompt tự check ngưỡng)
        rf = build_response_format_instruction(req.get("response_format"))
        file_info, prompt, messages = _compose_prompt(plan, messages, tools, tool_choice, rf)
        if not prompt.strip():
            _remove_prompt_file(file_info)
            return self.send_api_error(400, "empty prompt", ERR_INVALID)
        stream = bool(req.get("stream"))
        debug_log(f"chat request: model={echo}, messages={len(messages)}, stream={stream}, tools={len(tools) if tools else 0}")
        stops = _normalize_stop(req.get("stop"))
        max_tokens = req.get("max_tokens") or req.get("max_completion_tokens")
        include_usage = bool((req.get("stream_options") or {}).get("include_usage"))
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        _log_messages(cid, req.get("messages", []), req.get("tools"))  # Option B: log messages gốc (trước khi split)
        required_tool = is_required_tool_choice(tool_choice)
        allowed = tool_names(tools) or None
        schemas = tool_parameters(tools) or None
        required_params = tool_required_params(tools) or None

        if stream and not rf and not tools_active:
            return self._stream_chat(cid, echo, prompt, upstream, think,
                                     stops, max_tokens, include_usage,
                                     file_info=file_info, plan=plan)

        stop_ping = gone = None
        streamed_reasoning = []
        if stream:
            stop_ping, gone, _hb = self._start_sse()
            self._sse_chunk(cid, echo, {"role": "assistant", "content": ""},
                            None, include_usage)

        def on_reasoning_delta(delta):
            if stream and delta:
                streamed_reasoning.append(delta)
                self._sse_chunk(cid, echo, {"reasoning_content": delta},
                                None, include_usage)

        _t_start = time.time()
        try:
            text, reasoning, calls, usage = self._run_turn(
                prompt, upstream, think, tools_active, allowed, schemas,
                required_params, required_tool, gone, file_info=file_info,
                on_reasoning_delta=on_reasoning_delta if stream else None, plan=plan)
        except Exception as e:
            if plan is not None:
                sess.STORE.fail(plan, str(e))
            if stream:
                try:
                    self._sse_error(*_map_error(e))
                    self._sse_done()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if stop_ping:
                        stop_ping()
                return
            return self.send_api_error(*_map_error(e))
        finally:
            if stop_ping:
                stop_ping()
            _remove_prompt_file(file_info)

        _log_turn_result(cid, text, reasoning, calls, usage, time.time() - _t_start)
        if rf and text:
            text = strip_code_fence(text)
        text = _apply_stop(text, stops)
        text, truncated = _apply_max_tokens(text, max_tokens)
        if not text and not calls and not reasoning:
            if plan is not None:
                sess.STORE.fail(plan, "empty response")
            if stream:
                try:
                    self._sse_error(503, "empty response from upstream", ERR_API)
                    self._sse_done()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self.send_api_error(503, "empty response from upstream", ERR_API)
        if plan is not None:
            sess.STORE.commit(plan, text, calls)
        msg = {"role": "assistant", "content": text or None, "refusal": None}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if calls:
            msg["tool_calls"] = calls
        finish = "tool_calls" if calls else ("length" if truncated else "stop")
        if stream:
            try:
                streamed_len = sum(len(x) for x in streamed_reasoning)
                if reasoning and len(reasoning) > streamed_len:
                    extra = reasoning[streamed_len:]
                    if extra:
                        self._sse_chunk(cid, echo, {"reasoning_content": extra},
                                        None, include_usage)
                elif reasoning and not streamed_reasoning:
                    self._sse_chunk(cid, echo, {"reasoning_content": reasoning},
                                    None, include_usage)
                if text:
                    self._sse_chunk(cid, echo, {"content": text}, None, include_usage)
                for i, tc in enumerate(calls or []):
                    self._sse_chunk(cid, echo,
                                    {"tool_calls": [{"index": i, **tc}]},
                                    None, include_usage)
                self._sse_chunk(cid, echo, {}, finish, include_usage)
                if include_usage:
                    self._sse_usage(cid, echo, prompt, text, usage)
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_json({
            "id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": echo,
            "choices": [{"index": 0, "message": msg, "logprobs": None,
                         "finish_reason": finish}],
            "usage": _usage(prompt, text, usage),
        })

    def _stream_chat(self, cid, model, prompt, upstream, think,
                     stops, max_tokens, include_usage, file_info=None, plan=None):
        stop_ping, gone, _hb = self._start_sse()
        self._sse_chunk(cid, model, {"role": "assistant", "content": ""},
                        None, include_usage)
        full, finish, buf = "", "stop", ""
        hold = max((len(s) for s in stops), default=0)
        sess_kw = plan.bridge_kwargs() if plan is not None else {}
        gen = bridge.generate_stream(prompt, upstream, think, file_info=file_info,
                                     cancel_event=gone, **self._mode_kw(), **sess_kw)
        _t_start = time.time()
        full_reasoning = []
        try:
            for kind, delta in gen:
                if kind == "reasoning":
                    if delta:
                        full_reasoning.append(delta)
                        self._sse_chunk(cid, model,
                                        {"reasoning_content": delta},
                                        None, include_usage)
                    continue
                if not delta:
                    continue
                buf += delta
                hit = None
                for s in stops:
                    i = buf.find(s)
                    if i != -1 and (hit is None or i < hit):
                        hit = i
                if hit is not None:
                    piece = buf[:hit]
                    if piece:
                        full += piece
                        self._sse_chunk(cid, model, {"content": piece},
                                        None, include_usage)
                    buf = ""
                    break
                if max_tokens and (len(full) + len(buf)) > max_tokens * 4:
                    allowed = max(0, max_tokens * 4 - len(full))
                    piece = buf[:allowed]
                    if piece:
                        full += piece
                        self._sse_chunk(cid, model, {"content": piece},
                                        None, include_usage)
                    buf = ""
                    finish = "length"
                    break
                if len(buf) > hold:
                    piece = buf[:len(buf) - hold] if hold else buf
                    buf = buf[-hold:] if hold else ""
                    if piece:
                        full += piece
                        self._sse_chunk(cid, model, {"content": piece},
                                        None, include_usage)
            else:
                if buf:
                    full += buf
                    self._sse_chunk(cid, model, {"content": buf},
                                    None, include_usage)
        except ConnectionError:   # gom ca ConnectionAbortedError (Windows)
            gone.set()
            if plan is not None:
                sess.STORE.fail(plan, "client disconnected")
            return
        except Exception as e:
            if plan is not None:
                sess.STORE.fail(plan, str(e))
            log(f"stream error [{model}, prompt_len={len(prompt)}]: {e}")
            try:
                self._sse_error(*_map_error(e))
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        finally:
            stop_ping()
            _remove_prompt_file(file_info)
            close = getattr(gen, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        if plan is not None:
            sess.STORE.commit(plan, full, None)
        self._sse_chunk(cid, model, {}, finish, include_usage)
        if include_usage:
            self._sse_usage(cid, model, prompt, full, None)
        self._sse_done()
        _log_turn_result(cid, full, "".join(full_reasoning), None, {}, time.time() - _t_start)

    # ---------------------------------------------------------- responses --
    def _handle_responses(self, body):
        with sess.turn_guard():
            return self._handle_responses_locked(body)

    def _handle_responses_locked(self, body):
        req = self._parse(body)
        if not isinstance(req, dict):
            return self.send_api_error(400, "invalid JSON body", ERR_INVALID)
        resolved = self._resolve(req)
        if not resolved:
            return
        echo, upstream, think = resolved
        tools = _norm_resp_tools(req.get("tools"))
        messages = _responses_messages(req.get("input", []), req.get("instructions"))
        tool_choice = req.get("tool_choice") or "auto"

        tools_active = bool(tools) and tool_choice != "none"
        plan = self._session_plan("responses", messages, tools, tool_choice, tools_active)

        # Detect prompt lớn và tạo file tạm nếu cần (Responses API)
        rf = req.get("response_format") or _text_format(req.get("text"))
        rfi = build_response_format_instruction(rf)
        file_info, prompt, messages = _compose_prompt(plan, messages, tools, tool_choice, rfi)
        if not prompt.strip():
            _remove_prompt_file(file_info)
            return self.send_api_error(400, "empty input", ERR_INVALID)
        required_tool = is_required_tool_choice(tool_choice)
        allowed = tool_names(tools) or None
        schemas = tool_parameters(tools) or None
        required_params = tool_required_params(tools) or None
        stream = bool(req.get("stream"))
        debug_log(f"responses request: model={echo}, input_items={len(req.get('input', []))}, stream={stream}, tools={len(tools) if tools else 0}")
        rid = f"resp_{uuid.uuid4().hex[:16]}"
        _log_messages(rid, messages, tools)  # Option B: log messages (sau _responses_messages normalize)
        created = int(time.time())
        seq = 0

        def emit(evt, **fields):
            nonlocal seq
            seq += 1
            obj = {"type": evt, "sequence_number": seq, **fields}
            self._sse_write(
                f"event: {evt}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())

        def resp_obj(status, output, usage):
            return {"id": rid, "object": "response", "created_at": created,
                    "status": status, "model": echo, "output": output,
                    "error": None,
                    "instructions": req.get("instructions")
                    if isinstance(req.get("instructions"), str) else None,
                    "tools": req.get("tools")
                    if isinstance(req.get("tools"), list) else [],
                    "tool_choice": tool_choice,
                    "metadata": req.get("metadata")
                    if isinstance(req.get("metadata"), dict) else None,
                    "parallel_tool_calls": True,
                    "temperature": req.get("temperature", 1),
                    "top_p": req.get("top_p", 1), "usage": usage}

        stop_ping = gone = None
        if stream:
            stop_ping, gone, _hb = self._start_sse()
            emit("response.created", response=resp_obj("in_progress", [], None))
            emit("response.in_progress", response=resp_obj("in_progress", [], None))

        def on_resp_reasoning(delta):
            if stream and delta:
                emit("response.reasoning_text.delta", delta=delta)

        _t_start = time.time()
        try:
            text, _reasoning, calls, usage = self._run_turn(
                prompt, upstream, think, tools_active, allowed, schemas,
                required_params, required_tool, gone, file_info=file_info,
                on_reasoning_delta=on_resp_reasoning if stream else None, plan=plan)
        except Exception as e:
            if plan is not None:
                sess.STORE.fail(plan, str(e))
            if stream:
                try:
                    failed = resp_obj("failed", [], None)
                    failed["error"] = {"message": str(e)}
                    emit("response.failed", response=failed)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if stop_ping:
                        stop_ping()
                return
            return self.send_api_error(*_map_error(e))
        finally:
            if stop_ping:
                stop_ping()
            _remove_prompt_file(file_info)

        _log_turn_result(rid, text, _reasoning, calls, usage, time.time() - _t_start)
        if rfi and text:
            text = strip_code_fence(text)
        text, truncated = _apply_max_tokens(text, req.get("max_output_tokens"))
        if not text and not calls:
            if plan is not None:
                sess.STORE.fail(plan, "empty response")
            if stream:
                failed = resp_obj("failed", [], None)
                failed["error"] = {"message": "empty response from upstream"}
                try:
                    emit("response.failed", response=failed)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self.send_api_error(503, "empty response from upstream", ERR_API)

        if plan is not None:
            sess.STORE.commit(plan, text, calls)
        output = []
        for tc in calls or []:
            output.append({"type": "function_call", "id": tc["id"],
                           "call_id": tc["id"], "name": tc["function"]["name"],
                           "arguments": tc["function"]["arguments"],
                           "status": "completed"})
        if text or not calls:
            output.append({"type": "message",
                           "id": f"msg_{uuid.uuid4().hex[:12]}",
                           "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text",
                                        "text": text or "", "annotations": []}]})
        u = _usage(prompt, text, usage)
        resp_usage = {"input_tokens": u["prompt_tokens"],
                      "output_tokens": u["completion_tokens"],
                      "total_tokens": u["total_tokens"]}
        status = "incomplete" if truncated else "completed"
        if not stream:
            return self.send_json(resp_obj(status, output, resp_usage))
        try:
            for oi, item in enumerate(output):
                if item["type"] == "function_call":
                    pending = {**item, "arguments": "", "status": "in_progress"}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    emit("response.function_call_arguments.delta",
                         item_id=item["id"], output_index=oi,
                         delta=item["arguments"])
                    emit("response.function_call_arguments.done",
                         item_id=item["id"], output_index=oi,
                         arguments=item["arguments"])
                    emit("response.output_item.done", output_index=oi, item=item)
                else:
                    pending = {**item, "status": "in_progress", "content": []}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    for ci, part in enumerate(item["content"]):
                        f = {"item_id": item["id"], "output_index": oi,
                             "content_index": ci}
                        emit("response.content_part.added", **f,
                             part={"type": "output_text", "text": "",
                                   "annotations": []})
                        emit("response.output_text.delta", **f, delta=part["text"])
                        emit("response.output_text.done", **f, text=part["text"])
                        emit("response.content_part.done", **f, part=part)
                    emit("response.output_item.done", output_index=oi, item=item)
            emit("response.incomplete" if truncated else "response.completed",
                 response=resp_obj(status, output, resp_usage))
        except (BrokenPipeError, ConnectionResetError):
            pass


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # allow_reuse_address = False -> tat SO_REUSEADDR mac dinh, thay bang
    # SO_EXCLUSIVEADDRUSE (Windows) de bind that phai khi port da duoc dung boi
    # process khac. TIME_WAIT cua cung process van duoc cho rebind (Windows tu xu li).
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()
