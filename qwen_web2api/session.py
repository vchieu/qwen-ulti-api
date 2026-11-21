"""Che do session: tan dung chat Qwen hien hanh lam history cua client.

API OpenAI la stateless nen proxy phai tu doi chieu: sau moi luot thanh cong no luu hash cua
cac message da "gop" vao chat Qwen (ke ca tin assistant vua tra ve). Luot sau, neu messages
cua client bat dau bang dung chuoi hash do thi chi can gui phan moi (delta) vao chat dang mo,
Qwen server tu giu history theo chuoi parent_id. Khong khop (client compaction, undo, doi
tool, tab reload...) thi RESET: mo chat moi + gui lai day du bang duong cu.

File nay THUAN (khong I/O, khong browser): server.py/bridge.py chi goi plan/commit/fail.
Moi tinh huong "khong chac" deu rơi ve reset (an toan, ton 1 lan replay) chu khong doan.
"""
import contextlib
import hashlib
import json
import re
import threading
import time
import uuid

from .config import CONFIG
from .qwen import QwenError, log, debug_log
from .tools import _choice_instruction, _stringify_content, _fn_of

KIND_NEW = "new"
KIND_CONTINUE = "continue"
KIND_FORK = "fork"
_MAX_CHECKPOINTS = 200
# Hash dac biet o CUOI session.hashes: client huy giua chung (interrupt) sau khi request da vao
# chat Qwen -> chat co delta + 1 ban tra loi dang do ma client khong biet. Khop 0 hoac 1 tin
# assistant o luot sau (xem _plan) thay vi reset.
INTERRUPTED = "interrupted-reply"
# Cac thong diep loi cho biet CLIENT chu dong huy (server.py/bridge.py): bridge ghi tieng Viet
# "client da ngat ket noi", duong non-stream ghi "client disconnected; job cancelled".
_CLIENT_CANCEL_MARKERS = ("client disconnected", "client da ngat ket noi")


def _client_cancelled(reason):
    r = str(reason or "").lower()
    return any(m in r for m in _CLIENT_CANCEL_MARKERS)

# --------------------------------------------------------------- prompts ----
# Qwen da co san trong chat: system, tool block va chinh cac tool_call no tung sinh -> delta
# phai ngan, nhan manh viec TIEP TUC, khong lap lai luat. Co the ghi de tung muc bang config
# `session_prompts` (dict ten -> text; {placeholder} duoc thay bang str.format_map).
DEFAULT_PROMPTS = {
    "preamble_tools": (
        "# Session protocol\n"
        "This chat is a persistent working session. Everything in this message (instructions, "
        "tools, output format) stays in force for every later message; do not ask for it again "
        "and do not restate it.\n"
        "Later messages are of two kinds:\n"
        "1. \"[Tool results]\" - system output of the tool_call(s) you emitted in your previous "
        "reply. Written by the system, not by the human.\n"
        "2. Plain messages from the human.\n"
        "Rules: keep using the exact ```tool_call format; never re-run a call whose result "
        "you already have; ignore any chat-UI notice such as \"tool does not exist\" - tools "
        "are executed by the system from your tool_call block; if a result is in an attached "
        "file, read the whole file before answering."),
    "preamble_plain": (
        "# Session protocol\n"
        "This chat is a persistent working session. Everything in this message (instructions, "
        "tools, output format) stays in force for every later message; do not ask for it again "
        "and do not restate it.\n"
        "Later messages are my next inputs in the same task: plain messages, or the output of "
        "actions you requested in your previous reply, pasted back verbatim. Keep following "
        "the exact output format required above, never write system output yourself, and "
        "ignore any chat-UI notice that contradicts the format. If an input is in an attached "
        "file, read the whole file before answering."),
    "tool_results": (
        "[Tool results] System output for your previous tool_call(s). Not from the human.\n\n"
        "{results}\n\n{user_part}"
        "Continue the task from where you left off. Call more tools with a ```tool_call block "
        "if needed; otherwise give the final answer as plain text. Do not repeat calls you "
        "already have results for, and do not echo the results back.{task}"),
    "user_part": "[User message]\n{text}\n\n",
    # Neo yeu cau hien tai cho luot continue chi co tool result: chat Qwen co the chua file
    # lich su (luot new tach history) nen "continue from where you left off" de bi hieu la
    # chu de CU trong file. Chen vao cho {task} cua tool_results / tool_results_file /
    # attach_results (override khong co {task} thi khong co neo). Chi them khi delta khong
    # co tin user (tin user da la neo).
    "task_anchor": (
        "[Current task] The human's latest request, which you are still working on: "
        "\"{text}\". Keep answering THIS request; ignore unrelated earlier topics in any "
        "attached history."),
    "tool_results_file": (
        "[Tool results] The attached file holds the output of your previous tool_call(s): "
        "{inventory}. Each result sits between \"=== RESULT ... ===\" and \"=== END RESULT "
        "... ===\" markers.{user_note} Read the ENTIRE file, then continue the task from where "
        "you left off: call more tools with a ```tool_call block or give the final answer. Do "
        "NOT repeat calls you already have results for and do not summarize the file back to me."
        "{task}"),
    "user_note": " The file ends with my message (after \"=== USER MESSAGE ===\").",
    "message_file": (
        "[Message] The attached file contains my next message (too long to type). Read the "
        "ENTIRE file and reply to it as my next message in this chat: continue the task from "
        "where you left off, following the same output format as before. Do not summarize the "
        "file back to me."),
    # Extension tu chuyen delta inline -> file (Qwen doi paste dai thanh document): composer chi
    # nhan cau nay, nen phai dung ngu canh (tool result hay tin user).
    "attach_fallback": (
        "[Message] The attached file contains my next message (too long to type). Read the "
        "ENTIRE file and reply to it as my next message in this chat, following the same "
        "output format as before. Do not summarize the file back to me."),
    # Luot NEW (replay day du) ma toan bo prompt phai nam trong file: composer CHI nhan cau
    # nay. Khac attach_fallback (delta = tin tiep theo trong chat DA co history): file chua CA
    # hoi thoai ke ca system/tool definitions, va tin CUOI cung la tin can tra loi. Khong duoc
    # de roi xuong attachment_prompt cua config ("compressed conversation history") -> model
    # se tom tat lich su thay vi tra loi tin user cuoi.
    "attach_full": (
        "[Conversation file] The attached file contains the FULL conversation for this new "
        "chat: system instructions, any tool definitions, every earlier message and tool "
        "result, and it ENDS with the newest input. Read the ENTIRE file, then respond to "
        "that LAST input exactly as the instructions and output format inside the file "
        "require. Do not summarize the file back to me, and do not repeat work that is "
        "already completed in it."),
    "attach_results": (
        "[Tool results] The attached file holds the system output of your previous tool_call(s) "
        "(not written by the human); each result sits between \"=== RESULT ... ===\" and "
        "\"=== END RESULT ... ===\" markers. Read the ENTIRE file, then continue the task from "
        "where you left off: call more tools with a ```tool_call block or give the final "
        "answer. Do NOT repeat calls you already have results for and do not summarize the "
        "file back to me.{task}"),
    "reminder_tools": ("\n\n[Reminder] Keep the exact ```tool_call format. Each turn: "
                       "tool_call block(s) only, or a plain-text final answer."),
    "reminder_plain": ("\n\n[Reminder] Keep following the output format required in the "
                       "first message of this chat."),
    "nudge": "[Format error]\n{nudge}",
}


def prompts():
    """DEFAULT_PROMPTS + config `session_prompts` (chi nhan key hop le, gia tri str)."""
    out = dict(DEFAULT_PROMPTS)
    over = CONFIG.get("session_prompts")
    if isinstance(over, dict):
        for k, v in over.items():
            if k in out and isinstance(v, str) and v.strip():
                out[k] = v
    return out


def _fmt(tpl, **kw):
    try:
        return tpl.format_map(_SafeDict(kw))
    except (ValueError, IndexError):
        return tpl


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# ---------------------------------------------------------------- config ----
def enabled():
    return bool(CONFIG.get("bridge_session_mode", False))


def _num(key, default):
    try:
        v = float(CONFIG.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return v


def fork_enabled():
    return bool(CONFIG.get("session_fork", False))


def nudge_mode():
    return "fork" if str(CONFIG.get("session_nudge_mode", "append")).lower() == "fork" \
        else "append"


# ---------------------------------------------------------------- hashing ----
def _norm(s):
    return str(s or "").replace("\r\n", "\n").replace("\r", "\n").strip()


_ignore_cache = {"key": None, "rx": []}


def _ignore_patterns():
    """config session_hash_ignore -> list regex da compile (regex sai bi bo qua, cache theo key)."""
    raw = CONFIG.get("session_hash_ignore")
    if not isinstance(raw, (list, tuple)):
        raw = []
    key = tuple(str(x) for x in raw)
    if _ignore_cache["key"] != key:
        rx = []
        for pat in key:
            try:
                rx.append(re.compile(pat, re.DOTALL))
            except re.error:
                log(f"[session] session_hash_ignore: bo qua regex sai {pat!r}")
        _ignore_cache["key"], _ignore_cache["rx"] = key, rx
    return _ignore_cache["rx"]


def _hash_text(s):
    """Noi dung dung de hash: bo phan dong (session_hash_ignore) roi chuan hoa CRLF/strip."""
    s = str(s or "")
    for rx in _ignore_patterns():
        s = rx.sub("", s)
    return _norm(s)


def _h(*parts):
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _role(m):
    r = m.get("role", "user") if isinstance(m, dict) else "user"
    return "system" if r == "developer" else r


def canon_message(m, strict_assistant=False):
    """Hash on dinh cua 1 message.

    system/user/tool: role + noi dung (da chuan hoa CRLF, strip). assistant co tool_calls: chi
    (id, name) -- id do proxy tu sinh va client tra nguyen. assistant text thuan: chi dem
    (client hay strip whitespace); strict_assistant=true thi them noi dung.
    """
    if not isinstance(m, dict):
        return _h("?", str(m))
    role = _role(m)
    content = _hash_text(_stringify_content(m.get("content")))
    if role == "assistant":
        tcs = m.get("tool_calls") or []
        if tcs:
            ids = []
            for tc in tcs:
                fn = (tc or {}).get("function") or {} if isinstance(tc, dict) else {}
                ids.append([str((tc or {}).get("id") or "") if isinstance(tc, dict) else "",
                            str(fn.get("name") or "")])
            return _h("a", "tc", ids)
        return _h("a", "t", content if strict_assistant else "")
    if role == "tool":
        return _h("tool", str(m.get("tool_call_id") or ""), content)
    return _h(role, content)


def hash_messages(messages, strict_assistant=False):
    return [canon_message(m, strict_assistant) for m in messages or []]


def label_message(m):
    """Nhan ngan de CHAN DOAN khi history lech (vd `user(33c) 'dung python...'`); khong dung
    de so khop."""
    if not isinstance(m, dict):
        return "?"
    role = _role(m)
    if role == "assistant" and m.get("tool_calls"):
        names = [str(((tc or {}).get("function") or {}).get("name") or "?")
                 for tc in m["tool_calls"] if isinstance(tc, dict)]
        return "assistant<" + ",".join(names) + ">"
    text = _stringify_content(m.get("content"))
    if role == "tool":
        return f"tool({len(text)}c)"
    flat = " ".join(text.split())[:30]
    return f"{role}({len(text)}c) {flat!r}"


def ctx_hash(tools):
    """Dau van tay tool definitions. Doi tool => tool block trong chat cu het hieu luc."""
    defs = []
    for t in tools or []:
        fn = _fn_of(t) if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            defs.append([fn["name"], fn.get("description", ""), fn.get("parameters", {})])
    return _h("tools", defs)


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


# ----------------------------------------------------------- rendering ----
def _size_label(n):
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{round(n / 1024)} KB"
    return f"{n} B"


def _id_to_name(messages):
    out = {}
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {} if isinstance(tc, dict) else {}
                if isinstance(tc, dict) and tc.get("id") and fn.get("name"):
                    out[tc["id"]] = fn["name"]
    return out


def render_delta(messages, base_n, tools_active, tool_choice=None, turn_no=1):
    """messages[base_n:] -> dict(inline, results, user_text, inventory, n_tool, n_user).

    results = cac khoi `=== RESULT ... ===`; user_text = tin user (nguyen van, noi nhieu tin
    bang dong trong). `inline` la text day du de dien thang vao composer.
    """
    delta = list(messages[base_n:])
    names = _id_to_name(messages)
    blocks, inventory, user_parts = [], [], []
    seen_ids = set()
    for m in delta:
        role = _role(m)
        content = _stringify_content(m.get("content"))
        if role == "tool":
            tcid = str(m.get("tool_call_id") or "")
            name = m.get("name") or names.get(tcid) or "tool"
            seen_ids.add(tcid)
            blocks.append(f"=== RESULT tool={name} id={tcid} ===\n{content}\n"
                          f"=== END RESULT {tcid} ===")
            inventory.append(f"{name}#{tcid} ({_size_label(len(content.encode('utf-8')))})")
        elif role == "system":
            user_parts.append(f"[System instruction]: {content}")
        else:
            user_parts.append(content)
    # Call cua luot assistant ngay truoc delta ma client khong tra ket qua -> danh dau MISSING
    # de model khong doan mo.
    if base_n > 0 and blocks:
        prev = messages[base_n - 1]
        if isinstance(prev, dict) and prev.get("role") == "assistant":
            for tc in prev.get("tool_calls") or []:
                tcid = str((tc or {}).get("id") or "") if isinstance(tc, dict) else ""
                if tcid and tcid not in seen_ids:
                    fn = (tc or {}).get("function") or {}
                    blocks.append(f"=== RESULT tool={fn.get('name') or 'tool'} id={tcid} "
                                  f"MISSING ===")
    results = "\n\n".join(blocks)
    user_text = "\n\n".join(p for p in user_parts if p is not None)
    return {"results": results, "user_text": user_text, "inventory": inventory,
            "n_tool": len(blocks), "n_user": len(user_parts)}


def _task_anchor(messages, base_n, parts):
    """Neo yeu cau hien tai (tin user thuc cuoi cung truoc delta) cho luot continue chi co
    tool result, da kem dau ngat "\\n\\n" de gan vao {task}. Rong khi tat
    (session_task_anchor=false), khi delta da co tin user, hoac khong co tin user nao.
    Noi dung da bo phan dong (session_hash_ignore) nhu luc hash, de <environment_details>... khong chiem het gioi han."""
    if not parts.get("results") or parts.get("user_text"):
        return ""
    if not bool(CONFIG.get("session_task_anchor", True)):
        return ""
    limit = int(_num("session_task_anchor_chars", 500))
    if limit <= 0:
        return ""
    for m in reversed(list(messages[:base_n])):
        if isinstance(m, dict) and _role(m) == "user":
            text = " ".join(_hash_text(_stringify_content(m.get("content"))).split())
            if not text:
                continue
            if len(text) > limit:
                text = text[:limit].rstrip() + " ..."
            return "\n\n" + _fmt(prompts()["task_anchor"], text=text)
    return ""


def _delta_text(parts, tools_active, tool_choice, turn_no):
    """Dung prompt inline (hoac noi dung file khi file_mode) cho delta da render."""
    P = prompts()
    every = int(_num("session_reminder_every", 8))
    results, user_text = parts["results"], parts["user_text"]
    if results:
        user_part = _fmt(P["user_part"], text=user_text) if user_text else ""
        text = _fmt(P["tool_results"], results=results, user_part=user_part,
                    task=parts.get("anchor", ""))
    else:
        text = user_text
        if every > 0 and turn_no % every == 0:
            text += P["reminder_tools"] if tools_active else P["reminder_plain"]
    if tools_active and tool_choice is not None:
        text += _choice_instruction(tool_choice)
    return text


# ------------------------------------------------------------- the plan ----
class Plan:
    """Ket qua plan_turn: new (replay day du, duong cu) | continue | fork (delta)."""

    def __init__(self, kind, reason, hashes, ctx, api="chat", tools_active=False):
        self.kind = kind
        self.reason = reason
        self.hashes = hashes            # hash cua MOI message client vua gui
        self.ctx = ctx
        self.api = api
        self.tools_active = bool(tools_active)
        self.session = None             # Session dang noi (continue/fork)
        self.base_n = 0                 # so message da co trong chat truoc delta
        self.parent_id = None           # fork: nut cha de ghi de
        self.prompt = None              # inline / composer text (continue/fork)
        self.file_text = None           # != None -> delta di bang file
        self.delta_chars = 0
        self.labels = []                # label_message cua MOI message client (chan doan)
        self.attach_prompt = ""         # composer text neu extension chuyen inline -> file
        self.summary = ""
        self.out = {}                   # bridge.generate_stream ghi chat_id/nonce/sent/...
        self.sid = None

    # -- thong tin gui cho bridge/extension
    def job_session(self, mode=None):
        s = {"mode": mode or self.kind, "tools_active": self.tools_active}
        if self.session is not None:
            s.update({"sid": self.session.sid, "chat_id": self.session.chat_id,
                      "nonce": self.session.nonce})
        if (mode or self.kind) == KIND_FORK and self.parent_id:
            s["fork_parent_id"] = self.parent_id
            s["user_action"] = str(CONFIG.get("session_fork_user_action", "edit") or "")
        if self.kind in (KIND_CONTINUE, KIND_FORK):
            s["attach_prompt"] = self.attach_prompt or prompts()["attach_fallback"]
        elif self.kind == KIND_NEW:
            # Luot new = replay day du. Extension co the tu chuyen inline -> file (nguong da
            # hoc); luc do composer CHI nhan cau nay -> phai noi ro file chua toan bo hoi thoai
            # va tin CUOI trong file la tin can tra loi. Thieu key nay thi extension roi xuong
            # attachment_prompt cua config (compaction) -> model tom tat lich su.
            s["attach_prompt"] = self.attach_prompt or prompts()["attach_full"]
        return s

    def bridge_kwargs(self):
        return {"session": self.job_session(), "session_out": self.out,
                "tools_active": self.tools_active}

    def retry_call(self, prompt, file_info, nudge):
        """Lan thu >= 2 cua plan continue/fork -> (prompt, file_info, session_dict).
        (plan new giu hanh vi cu cua _run_turn: gui lai ca prompt trong chat moi.)

        continue/fork: delta da nam trong chat (lan 1 da gui) nen chi gui rieng loi nhac, khong
        kem file. nudge_mode=fork (va biet parent) thi gui lai delta o nhanh anh em de reply
        hong khong con tren duong dan.
        """
        nudge = str(nudge or "").strip()
        if nudge_mode() == "fork" and fork_enabled() and self.out.get("parent_id"):
            s = self.job_session(KIND_FORK)
            s["fork_parent_id"] = self.out["parent_id"]
            s["user_action"] = str(CONFIG.get("session_fork_user_action", "edit") or "")
            return prompt + ("\n\n" + nudge if nudge else ""), file_info, s
        s = self.job_session(KIND_CONTINUE)
        return _fmt(prompts()["nudge"], nudge=nudge), None, s


class Session:
    def __init__(self, chat_id, nonce, hashes, ctx, api="chat"):
        self.sid = uuid.uuid4().hex[:8]
        self.chat_id = chat_id
        self.nonce = nonce
        self.hashes = list(hashes)
        self.ctx = ctx
        self.api = api
        self.turns = 0
        self.chars_sent = 0
        self.created = self.last_used = time.time()
        self.checkpoints = []           # [{"n": so msg truoc delta, "parent": nut cha}]
        self.labels = []                # nhan chan doan song song voi hashes

    def describe(self):
        return (f"sid={self.sid} chat={str(self.chat_id or '-')[:8]} "
                f"consumed={len(self.hashes)} turns={self.turns} chars={self.chars_sent}")


class SessionStore:
    """Chi 1 session hieu luc cung luc (1 tab = 1 chat). turn() tuan tu hoa moi request."""

    def __init__(self):
        self._lock = threading.RLock()
        self._turn = threading.Lock()
        self.current = None
        self.last_reset = ""

    def get(self):
        with self._lock:
            return self.current

    def reset(self, reason):
        with self._lock:
            if self.current is not None:
                log(f"[session] reset {self.current.describe()} reason={reason}")
            self.current = None
            self.last_reset = reason

    @contextlib.contextmanager
    def turn(self, timeout=None):
        """Mot luot plan->chay->commit tai mot thoi diem: neu khong, 2 request xen ke se do
        delta cua nhau vao cung chat."""
        if timeout is None:
            timeout = max(1.0, _num("bridge_job_timeout", 600)) + 30
        if not self._turn.acquire(timeout=timeout):
            raise QwenError("session busy: request truoc chua xong", status=503)
        try:
            yield
        finally:
            self._turn.release()

    # ------------------------------------------------------------ planning --
    def plan(self, messages, tools=None, tool_choice=None, tab=None, api="chat",
             tools_active=None, now=None):
        labels = [label_message(m) for m in messages or []]
        p = self._plan(messages, tools, tool_choice, tab, api, tools_active, now, labels)
        p.labels = labels
        return p

    def _diff_note(self, cur, labels, lcp):
        """Cho biet message nao lech: client[i] vs session[i] + checkpoint (neu bat fork)."""
        def at(seq, i):
            return seq[i] if 0 <= i < len(seq) else "-"
        note = f" client[{lcp}]={at(labels, lcp)} session[{lcp}]={at(cur.labels, lcp)}"
        if fork_enabled():
            note += " fork:khong co checkpoint hop le, ckpts=" + str(
                [c.get("n") for c in cur.checkpoints][-12:])
        return note

    def _plan(self, messages, tools, tool_choice, tab, api, tools_active, now, labels):
        now = time.time() if now is None else now
        strict = bool(CONFIG.get("session_strict_assistant", False))
        hashes = hash_messages(messages, strict)
        ch = ctx_hash(tools)
        if tools_active is None:
            tools_active = bool(tools) and tool_choice != "none"
        with self._lock:
            cur = self.current

            def new(reason):
                self.reset(reason) if cur is not None else None
                p = Plan(KIND_NEW, reason, hashes, ch, api, tools_active)
                debug_log(f"[session] plan new reason={reason} messages={len(messages)}")
                return p

            if cur is None:
                return new("no_session")
            if not messages:
                return new("empty_messages")
            if now - cur.last_used > _num("session_idle_ttl", 1800) > 0:
                return new("idle_ttl")
            mt = int(_num("session_max_turns", 60))
            if mt > 0 and cur.turns >= mt:
                return new("max_turns")
            mc = int(_num("session_max_chars", 400000))
            if mc > 0 and cur.chars_sent >= mc:
                return new("max_chars")
            if not isinstance(tab, dict) or not tab.get("nonce"):
                return new("no_tab_info")
            if tab.get("nonce") != cur.nonce:
                return new("tab_reloaded")
            if cur.chat_id and tab.get("chat_id") and tab.get("chat_id") != cur.chat_id:
                return new("tab_chat_changed")
            if cur.ctx != ch:
                return new("tools_changed")
            if cur.api != api:
                return new("api_changed")
            if cur.hashes and cur.hashes[-1] == INTERRUPTED:
                # Luot truoc bi client huy sau khi da gui: chat Qwen co ...+delta+ban tra loi do.
                # Client thuong bo ban do (hai tin user lien tiep) hoac giu lai nhu 1 tin
                # assistant van ban -> gui tiep phan con lai vao chat nay, khong reset.
                m = len(cur.hashes) - 1
                lcp_i = common_prefix(hashes, cur.hashes[:m])
                if lcp_i == m and len(messages) > m:
                    base = m
                    nxt = messages[m]
                    if isinstance(nxt, dict) and _role(nxt) == "assistant" \
                            and not nxt.get("tool_calls"):
                        base = m + 1
                    if len(messages) > base:
                        return self._continue_plan(cur, messages, hashes, ch, tools,
                                                   tool_choice, api, tools_active, base,
                                                   KIND_CONTINUE, None, "after_interrupt")
                return new(f"history_diverged_after_interrupt@{lcp_i}/{m} msgs={len(messages)}"
                           + self._diff_note(cur, labels, lcp_i))
            n = len(cur.hashes)
            lcp = common_prefix(hashes, cur.hashes)
            if lcp == n and len(hashes) > n:
                return self._continue_plan(cur, messages, hashes, ch, tools, tool_choice,
                                           api, tools_active, n, KIND_CONTINUE, None,
                                           "history_match")
            if fork_enabled():
                for ck in reversed(cur.checkpoints):
                    base = ck.get("n", 0)
                    if base < 1 or not ck.get("parent") or base > lcp:
                        continue
                    delta = messages[base:]
                    if not delta or any(_role(m) == "assistant" for m in delta) \
                            or _role(delta[-1]) not in ("user", "tool"):
                        continue
                    why = ("retry_same_history" if len(hashes) == n - 1 and lcp == n - 1
                           else f"history_rewound@{lcp}/{n}")
                    p = self._continue_plan(cur, messages, hashes, ch, tools, tool_choice,
                                            api, tools_active, base, KIND_FORK,
                                            ck["parent"], why)
                    return p
            note = self._diff_note(cur, labels, lcp)
            if lcp == len(hashes):
                return new(f"retry_or_rewind len={len(hashes)}/{n}{note}")
            return new(f"history_diverged@{lcp}/{n} msgs={len(messages)}{note}")

    def _continue_plan(self, cur, messages, hashes, ch, tools, tool_choice, api,
                       tools_active, base_n, kind, parent, reason):
        delta = messages[base_n:]
        tail = _role(delta[-1]) if delta else ""
        if not delta or tail not in ("user", "tool") \
                or any(_role(m) == "assistant" for m in delta):
            self.reset(f"bad_delta tail={tail or '-'}")
            return Plan(KIND_NEW, "bad_delta", hashes, ch, api, tools_active)
        p = Plan(kind, reason, hashes, ch, api, tools_active)
        p.session, p.sid, p.base_n, p.parent_id = cur, cur.sid, base_n, parent
        parts = render_delta(messages, base_n, tools_active, tool_choice)
        parts["anchor"] = _task_anchor(messages, base_n, parts)
        turn_no = cur.turns + 1
        inline = _delta_text(parts, tools_active, tool_choice, turn_no)
        thr = int(_num("session_file_threshold", 30000))
        P = prompts()
        p.attach_prompt = (_fmt(P["attach_results"], task=parts["anchor"])
                           if parts["n_tool"] else P["attach_fallback"])
        if tools_active and tool_choice is not None:
            p.attach_prompt += _choice_instruction(tool_choice)
        if thr > 0 and len(inline) > thr:
            body = parts["results"]
            if parts["user_text"]:
                body += ("\n\n=== USER MESSAGE ===\n" if body else "") + parts["user_text"]
            p.file_text = body
            if parts["results"]:
                note = _fmt(P["user_note"]) if parts["user_text"] else ""
                p.prompt = _fmt(P["tool_results_file"],
                                inventory=", ".join(parts["inventory"]) or "(none)",
                                user_note=note, task=parts["anchor"])
            else:
                p.prompt = P["message_file"]
            if tools_active and tool_choice is not None:
                p.prompt += _choice_instruction(tool_choice)
            p.delta_chars = len(body)
        else:
            p.prompt = inline
            p.delta_chars = len(inline)
        p.summary = (f"{kind} consumed={base_n} delta={len(delta)}"
                     f"(tool x{parts['n_tool']}, user x{parts['n_user']}) "
                     f"chars={p.delta_chars} {'file' if p.file_text is not None else 'inline'}"
                     + (f" parent={str(parent)[:8]}" if parent else ""))
        log(f"[session] {cur.describe()} {p.summary} why={reason}")
        return p

    def preamble(self, tools_active):
        P = prompts()
        return P["preamble_tools"] if tools_active else P["preamble_plain"]

    # ------------------------------------------------------------- commit --
    def commit(self, plan, text, calls, out=None):
        """Luot thanh cong: ghi nhan chat Qwen da chua (messages client + assistant vua tra)."""
        out = out if out is not None else plan.out
        strict = bool(CONFIG.get("session_strict_assistant", False))
        asst = {"role": "assistant", "content": text}
        if calls:
            asst["tool_calls"] = calls
        new_hashes = list(plan.hashes)
        new_labels = list(plan.labels)
        if plan.api == "responses" and calls and text:
            # Responses API: function_call va message la 2 item rieng -> client gui lai 2 msg.
            new_hashes.append(canon_message({"role": "assistant", "content": None,
                                             "tool_calls": calls}, strict))
            new_hashes.append(canon_message({"role": "assistant", "content": text}, strict))
        else:
            new_hashes.append(canon_message(asst, strict))
        new_labels += [label_message(asst)] * (len(new_hashes) - len(new_labels))
        with self._lock:
            cur = self.current
            if plan.kind == KIND_NEW:
                nonce = out.get("nonce")
                if not nonce:
                    log("[session] khong tao session: extension khong bao nonce (can "
                        "extension moi hon)")
                    self.current = None
                    return None
                s = Session(out.get("chat_id"), nonce, new_hashes, plan.ctx, plan.api)
                s.turns = 1
                s.labels = new_labels
                s.chars_sent = plan.delta_chars + len(text or "")
                s.checkpoints = [{"n": 0, "parent": None}]
                self.current = s
                log(f"[session] new {s.describe()} nonce={str(nonce)[:8]}")
                return s
            if cur is None or cur is not plan.session:
                log("[session] bo qua commit: session da bi thay/reset trong luc chay")
                return None
            if out.get("nonce") and out.get("nonce") != cur.nonce:
                self.reset("tab_reloaded_during_turn")
                return None
            if cur.chat_id and out.get("chat_id") and out.get("chat_id") != cur.chat_id:
                self.reset("chat_id_changed_during_turn")
                return None
            if out.get("dirty"):
                self.reset("dirty")
                return None
            ck = {"n": plan.base_n, "parent": out.get("parent_id") or plan.parent_id}
            cur.checkpoints = [c for c in cur.checkpoints if c["n"] < plan.base_n] + [ck]
            cur.checkpoints = cur.checkpoints[-_MAX_CHECKPOINTS:]
            cur.hashes = new_hashes
            cur.labels = new_labels
            cur.turns += 1
            cur.chars_sent += plan.delta_chars + len(text or "")
            cur.last_used = time.time()
            debug_log(f"[session] commit {cur.describe()}")
            return cur

    def fail(self, plan, reason):
        """Luot loi. Da gui (sent) hoac extension bao session_lost -> chat da/khong ro trang thai
        => reset (client retry se tu replay day du). Chua gui gi thi giu session."""
        if plan is None:
            return
        out = plan.out or {}
        if out.get("sent") and not out.get("session_lost") and not out.get("dirty") \
                and _client_cancelled(reason) and out.get("nonce"):
            # Client huy giua chung (vd bam interrupt): extension da bam Stop, chat giu delta +
            # ban tra loi do -> danh dau INTERRUPTED de luot sau gui tiep o chat nay.
            if self._mark_interrupted(plan, out):
                return
        if plan.kind == KIND_NEW:
            return
        if out.get("sent") or out.get("session_lost") or out.get("dirty"):
            self.reset(f"turn_failed: {reason}")

    def _mark_interrupted(self, plan, out):
        nonce = out.get("nonce")
        hashes = list(plan.hashes) + [INTERRUPTED]
        labels = list(plan.labels) + ["assistant(interrupted)"]
        with self._lock:
            cur = self.current
            if plan.kind == KIND_NEW:
                s = Session(out.get("chat_id"), nonce, hashes, plan.ctx, plan.api)
                s.turns = 1
                s.chars_sent = plan.delta_chars
                s.checkpoints = [{"n": 0, "parent": None}]
                s.labels = labels
                self.current = s
                log(f"[session] interrupted (turn dau): giu {s.describe()}, luot sau gui tiep")
                return True
            if cur is None or cur is not plan.session or nonce != cur.nonce \
                    or (cur.chat_id and out.get("chat_id")
                        and out.get("chat_id") != cur.chat_id):
                return False
            ck = {"n": plan.base_n, "parent": out.get("parent_id") or plan.parent_id}
            cur.checkpoints = [c for c in cur.checkpoints if c["n"] < plan.base_n] + [ck]
            cur.checkpoints = cur.checkpoints[-_MAX_CHECKPOINTS:]
            cur.hashes, cur.labels = hashes, labels
            cur.turns += 1
            cur.chars_sent += plan.delta_chars
            cur.last_used = time.time()
            log(f"[session] interrupted: giu {cur.describe()}, luot sau gui tiep o chat nay")
            return True


STORE = SessionStore()


@contextlib.contextmanager
def turn_guard():
    """Tuan tu hoa request khi bat session mode (khong bat thi khong lam gi)."""
    if not enabled():
        yield
        return
    with STORE.turn():
        yield