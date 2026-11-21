"""Thu gon (compact) ban COPY cua messages cho luot `new` cua che do session.

Van de: luot `new` replay TOAN BO history vao chat moi va tinh do dai do vao `chars_sent`
(session.commit). Neu ban replay da >= `session_max_chars` thi session vua tao da het han o luot
ke tiep (`reset reason=max_chars`) -> luot sau lai `new`, history chi phinh them, khong bao gio
ve `continue`.

Giai phap: truoc khi dung file/prompt replay, thu gon BAN COPY cua messages xuong
`target_chars` (mac dinh 50% `session_max_chars`, chua dau cho cac luot `continue` sau do):

  1. tool result cu     -> giu ngan sach `session_replay_tool_budget`, phan con lai thay bang
                           stub (giu dong dau, vd "Read file X, lines A-B", de model biet doc lai);
  2. arguments cua tool_call assistant cu -> ngan sach `session_replay_args_budget`; vuot thi chi
                           elide cac gia tri CHUOI lon (content cua write, oldString/newString cua
                           edit...), giu path/key khac, JSON van hop le;
  3. vai luot gan nhat + batch tool result cuoi: giu nguyen, chi cat moi tool result o
                           `session_replay_recent_cap` ky tu (dau + duoi).

KHONG mutate list messages cua client: hash session (session.hash_messages, plan.hashes) tinh tu
messages goc va `commit` dung lai plan.hashes; assistant co tool_calls chi hash (id, name). Ket
qua o day chi dung de RENDER.

Thuan (khong I/O) ngoai log. Loi bat ngo -> tra messages goc (khong lam hong request).
"""
import json

from .config import CONFIG
from .qwen import debug_log, log
from .tools import _stringify_content, messages_to_prompt

_NO_LIMIT = 1 << 40
_MAX_DEBUG_ITEMS = 40


def _num(key, default):
    try:
        return int(float(CONFIG.get(key, default)))
    except (TypeError, ValueError):
        return int(default)


def enabled():
    return bool(CONFIG.get("session_replay_compact", True))


def target_chars():
    """Dich do dai replay. `session_replay_target_chars` > 0 thi dung nguyen gia tri; con lai
    = 50% `session_max_chars` (0 neu session_max_chars <= 0, tuc khong gioi han -> khong thu gon)."""
    t = _num("session_replay_target_chars", 0)
    if t > 0:
        return t
    mc = _num("session_max_chars", 400000)
    try:
        ratio = float(CONFIG.get("session_replay_target_ratio", 0.5))
    except (TypeError, ValueError):
        ratio = 0.5
    if not 0.05 <= ratio <= 1.0:
        ratio = 0.5
    return int(mc * ratio) if mc > 0 else 0


def adaptive():
    """true: ngan sach tool/args tu co theo target (chi cat den khi vua du), false: chi dung ngan
    sach co dinh `session_replay_tool_budget`/`session_replay_args_budget`."""
    return bool(CONFIG.get("session_replay_adaptive", True))


# ------------------------------------------------------------------ helpers ----
def _first_line(text, limit=300):
    for ln in text[:4000].split("\n"):
        ln = ln.strip()
        if ln:
            return ln[:limit]
    return ""


def _stub(text):
    head = _first_line(text)
    marker = (f"[... {len(text)} chars of old tool output elided to save context; "
              "re-run the tool if you need it ...]")
    return (head + "\n" if head else "") + marker


def _cap(text, cap):
    """Cat giua: giu 2/3 dau (co dong dau) + 1/3 cuoi (loi/ket qua thuong nam cuoi)."""
    if len(text) <= cap:
        return text
    head = (cap * 2) // 3
    tail = cap - head
    return (text[:head] + f"\n[... {len(text) - cap} chars elided in the middle ...]\n"
            + (text[-tail:] if tail > 0 else ""))


def _is_tc_assistant(m):
    return isinstance(m, dict) and m.get("role") == "assistant" and bool(m.get("tool_calls"))


def _parse_args(tc):
    """tool_call -> (obj, len(arguments)) hoac None neu khong thu gon duoc (arguments hong thi
    messages_to_prompt cung doi thanh {} nen de nguyen)."""
    fn = tc.get("function") if isinstance(tc, dict) else None
    if not isinstance(fn, dict):
        return None
    a = fn.get("arguments")
    if isinstance(a, str):
        try:
            return json.loads(a), len(a)
        except ValueError:
            return None
    if isinstance(a, (dict, list)):
        return a, len(json.dumps(a, ensure_ascii=False))
    return None


def _elide_strings(obj, min_len, cnt):
    """Ban sao obj, gia tri chuoi >= min_len -> "[elided N chars]". cnt = [so chuoi, so ky tu]."""
    if isinstance(obj, str):
        if len(obj) >= min_len:
            cnt[0] += 1
            cnt[1] += len(obj)
            return f"[elided {len(obj)} chars]"
        return obj
    if isinstance(obj, list):
        return [_elide_strings(x, min_len, cnt) for x in obj]
    if isinstance(obj, dict):
        return {k: _elide_strings(v, min_len, cnt) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------- one attempt ----
def _compact_once(messages, recent_turns, tool_budget, args_budget):
    """-> (messages_moi, stats). messages goc khong bi sua; message khong doi giu nguyen object."""
    n = len(messages)
    tc_idx = [i for i, m in enumerate(messages) if _is_tc_assistant(m)]
    if recent_turns > 0 and tc_idx:
        res_start = args_start = tc_idx[-recent_turns] if len(tc_idx) >= recent_turns else 0
    else:
        # Khong giu luot nao nguyen ven: chi batch tool result sau tin assistant cuoi.
        last_asst = max((i for i, m in enumerate(messages)
                         if isinstance(m, dict) and m.get("role") == "assistant"), default=-1)
        res_start, args_start = last_asst + 1, n
    short_thr = max(0, _num("session_replay_short_result", 2000))
    recent_cap = max(1000, _num("session_replay_recent_cap", 24000))
    min_len = max(50, _num("session_replay_elide_min", 200))

    out = list(messages)
    st = {"recent_turns": recent_turns, "tool_budget": tool_budget, "args_budget": args_budget,
          "tool_stubbed": 0, "tool_old": 0, "tool_saved": 0,
          "args_elided": 0, "args_strings": 0, "args_saved": 0,
          "recent_capped": 0, "recent_saved": 0, "items": []}

    # --- lop 1a: tool result cu (idx < res_start) theo ngan sach
    cand = []
    for i in range(res_start):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "tool":
            cand.append((i, _stringify_content(m.get("content"))))
    st["tool_old"] = len(cand)
    keep, remaining, used_short = set(), tool_budget, 0
    newest_first = sorted(cand, key=lambda x: -x[0])
    short_cap = tool_budget // 2          # dam loi edit ngan khong an het ngan sach cua read lon
    # (1) ket qua ngan uu tien, toi da nua ngan sach; (2) ket qua lon, moi -> cu; (3) phan ngan
    # con lai. Moi buoc deu tru vao cung mot ngan sach (ket qua ngan khong duoc mien).
    for i, t in newest_first:
        L = len(t)
        if L < short_thr and used_short + L <= short_cap and L <= remaining:
            keep.add(i)
            remaining -= L
            used_short += L
    for big in (True, False):
        for i, t in newest_first:
            if i not in keep and (len(t) >= short_thr) == big and len(t) <= remaining:
                keep.add(i)
                remaining -= len(t)
    for i, t in cand:
        if i in keep:
            continue
        stub = _stub(t)
        if len(stub) >= len(t):
            continue                       # qua ngan: stub khong loi hon
        nm = dict(messages[i])
        nm["content"] = stub
        out[i] = nm
        st["tool_stubbed"] += 1
        st["tool_saved"] += len(t) - len(stub)
        st["items"].append(f"msg[{i}] tool id={messages[i].get('tool_call_id', '?')} "
                           f"{len(t)} chars -> stub ({_first_line(t, 80)!r})")

    # --- lop 1b: vung gan day - chi cat tran moi tool result
    for i in range(res_start, n):
        m = messages[i]
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            continue
        t = _stringify_content(m.get("content"))
        if len(t) > recent_cap:
            capped = _cap(t, recent_cap)
            nm = dict(m)
            nm["content"] = capped
            out[i] = nm
            st["recent_capped"] += 1
            st["recent_saved"] += len(t) - len(capped)
            st["items"].append(f"msg[{i}] tool(recent) id={m.get('tool_call_id', '?')} "
                               f"{len(t)} -> {len(capped)} chars (cap)")

    # --- lop 2: arguments cua tool_call assistant cu (idx < args_start)
    remaining = args_budget
    for i in range(args_start - 1, -1, -1):          # moi -> cu
        m = messages[i]
        if not _is_tc_assistant(m):
            continue
        tcs = m["tool_calls"]
        if not isinstance(tcs, list):
            continue
        new_tcs, changed = list(tcs), False
        for j, tc in enumerate(tcs):
            parsed = _parse_args(tc)
            if parsed is None:
                continue
            obj, cost = parsed
            if cost <= remaining:
                remaining -= cost
                continue
            cnt = [0, 0]
            new_obj = _elide_strings(obj, min_len, cnt)
            if not cnt[0]:
                continue
            fn = dict(tc["function"])
            fn["arguments"] = json.dumps(new_obj, ensure_ascii=False)
            ntc = dict(tc)
            ntc["function"] = fn
            new_tcs[j] = ntc
            changed = True
            st["args_elided"] += 1
            st["args_strings"] += cnt[0]
            st["args_saved"] += cnt[1]
            st["items"].append(f"msg[{i}] assistant call {fn.get('name', '?')} "
                               f"id={tc.get('id', '?')} args {cost} chars: "
                               f"{cnt[0]} string(s) / {cnt[1]} chars elided")
        if changed:
            nm = dict(m)
            nm["tool_calls"] = new_tcs
            out[i] = nm
    return out, st


def _changed(st):
    return bool(st["tool_stubbed"] or st["args_elided"] or st["recent_capped"])


# --------------------------------------------------------------------- API ----
def compact_messages(messages, tools=None, tool_choice=None):
    """messages client -> (messages_de_render, stats | None).

    stats None = khong thu gon (tat, replay da <= target, khong co gi de bo, hoac loi) va
    messages tra ve CHINH list vao. Nguoc lai la list moi (message khong doi dung chung object).
    Thu cac muc nghiem dan (giam so luot giu nguyen, roi giam ngan sach) cho toi khi <= target;
    van vuot thi dung ket qua nho nhat va log canh bao (log `[replay]`).
    """
    if not enabled():
        return messages, None
    target = target_chars()
    if target <= 0:
        return messages, None
    try:
        before = len(messages_to_prompt(messages, tools, tool_choice, max_bytes=_NO_LIMIT))
        if before <= target:
            debug_log(f"[replay] compact bo qua: replay {before} chars <= target {target} "
                      f"(headroom={_num('session_max_chars', 400000) - before:.0f} chars toi "
                      "session_max_chars)")
            return messages, None
        recent = max(0, _num("session_replay_recent_turns", 3))
        tb = max(0, _num("session_replay_tool_budget", 40000))
        ab = max(0, _num("session_replay_args_budget", 40000))
        # Thu tu tu LONG -> CHAT: muc adaptive (ngan sach theo target) truoc, roi ngan sach co
        # dinh cu. Dung ngay o muc long nhat dat <= target nen khong cat qua tay.
        schedule = []
        if adaptive():
            for frac in (0.6, 0.4, 0.25):
                lt, la = int(target * frac * 0.7), int(target * frac * 0.3)
                if lt > tb:
                    schedule.append((recent, lt, max(la, ab)))
        schedule.append((recent, tb, ab))
        for p in ((1, tb, ab), (0, tb, ab), (0, tb // 2, ab // 2), (0, 0, 0)):
            if p[0] <= recent and p not in schedule:
                schedule.append(p)
        best, tried = None, []
        for (r, t_b, a_b) in schedule:
            out, st = _compact_once(messages, r, t_b, a_b)
            after = len(messages_to_prompt(out, tools, tool_choice, max_bytes=_NO_LIMIT))
            st["before"], st["after"], st["target"] = before, after, target
            tried.append(f"recent={r}/tool={t_b}/args={a_b}->{after}")
            if best is None or after < best[1]["after"]:
                best = (out, st)
            if after <= target:
                break
        out, st = best
        st["tried"] = tried
        if not _changed(st) or st["after"] >= before:
            log(f"[replay] compact: replay {before} chars > target {target} nhung khong co "
                "tool result/arguments cu de thu gon (phan lon la tin user/assistant text?)")
            return messages, None
        log(f"[replay] compact {before} -> {st['after']} chars (target {target}) "
            f"tool_stubbed={st['tool_stubbed']}/{st['tool_old']} (-{st['tool_saved']}) "
            f"args_elided={st['args_elided']} (-{st['args_saved']}) "
            f"recent_capped={st['recent_capped']} (-{st['recent_saved']}) "
            f"params=recent={st['recent_turns']}/tool={st['tool_budget']}/args={st['args_budget']} "
            f"level={len(tried)}/{len(schedule)} adaptive={adaptive()} "
            f"headroom={_num('session_max_chars', 400000) - st['after']:.0f}"
            + ("" if st["after"] <= target else
               f" CANH BAO: van > target, da thu {len(tried)} muc: {'; '.join(tried)}"))
        for it in st["items"][:_MAX_DEBUG_ITEMS]:       # /debug chi giu 100 dong gan nhat
            debug_log(f"[replay]   {it}")
        if len(st["items"]) > _MAX_DEBUG_ITEMS:
            debug_log(f"[replay]   ... va {len(st['items']) - _MAX_DEBUG_ITEMS} muc nua")
        return out, st
    except Exception as e:                                   # khong de compact lam hong request
        log(f"[replay] compact loi, dung messages goc: {e!r}")
        return messages, None
