"""Prompt building + tool-call parsing (OpenAI tools -> Qwen Web text protocol).

Trimmed, self-contained version of the gemini-web2api tool layer:
flatten messages -> one prompt with a pinned '# Tool Use' block, then parse
```tool_call fences out of the answer with JSONDecoder.raw_decode.
"""
import ast
import json
import re
import uuid

from .config import CONFIG
from .qwen import log

_TRUNCATION_MARKER = "[...truncated...]"


def _stringify_content(content) -> str:
    """Flatten content, chuan hoa CRLF/CR -> LF (client Windows lam phong prompt)."""
    return _stringify_raw(content).replace("\r\n", "\n").replace("\r", "\n")


def _stringify_raw(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "input_text", "output_text"):
                    out.append(c.get("text", ""))
                elif isinstance(c.get("text"), str):
                    out.append(c["text"])
            elif isinstance(c, str):
                out.append(c)
        return "\n".join(p for p in out if p)
    return str(content)


def _fn_of(t):
    return t.get("function", t) if t.get("type") == "function" else t


def tool_names(tools) -> set:
    names = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = _fn_of(t)
        if isinstance(fn, dict) and fn.get("name"):
            names.add(fn["name"])
    return names


def tool_parameters(tools) -> dict:
    schemas = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = _fn_of(t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters") or {}
        props = params.get("properties") if isinstance(params, dict) else None
        schemas[fn["name"]] = set(props) if isinstance(props, dict) else set()
    return schemas


def tool_required_params(tools) -> dict:
    required = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = _fn_of(t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters") or {}
        names = params.get("required") if isinstance(params, dict) else None
        if isinstance(names, list):
            required[fn["name"]] = {n for n in names if isinstance(n, str)}
    return required


def missing_required_params(tool_calls, required) -> list:
    missing = []
    if not required:
        return missing
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        params = required.get(fn.get("name"))
        if not params:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}", strict=False)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(args, dict):
            continue
        missing.extend(f"{fn['name']}.{p}" for p in sorted(params) if p not in args)
    return missing


def is_required_tool_choice(tool_choice) -> bool:
    if tool_choice == "required":
        return True
    if isinstance(tool_choice, dict):
        return bool(tool_choice.get("function") or tool_choice.get("name"))
    return False


def _choice_instruction(tool_choice) -> str:
    if tool_choice == "none":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if tool_choice == "required":
        return ("\n\nIMPORTANT: You MUST call at least one tool. "
                "Do not respond with text only.")
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        name = fn.get("name") if isinstance(fn, dict) else tool_choice.get("name")
        if name:
            return (f'\n\nIMPORTANT: You MUST call the tool "{name}". '
                    "Do not call other tools.")
    return ""


_TOOL_REMINDER = ("\n\n[Reminder: you have working tools. To know what a file "
                  "contains or what a command produced, call the tool -- do "
                  "not answer from memory and do not claim to have read or "
                  "run anything you have not.]")


def _tool_use_block(tool_defs, constraint) -> str:
    tools_json = json.dumps(tool_defs, ensure_ascii=False)
    return (
        "# Tool Use\n\n"
        "The tools below are connected to the user's machine and execute for "
        "real the moment you call them.\n\n"
        "Call format (use this exact format):\n"
        "```tool_call\n"
        '{"name": "<tool_name>", "arguments": {<parameters>}}\n'
        "```\n\n"
        "Rules:\n"
        "- Each turn do exactly ONE of: (A) you need to read or change "
        "something -> output ONLY tool_call block(s); (B) you already have "
        "everything -> answer as plain text with NO block. Never both, and "
        "never any prose after the block.\n"
        "- If the conversation's own instructions require every reply to use "
        "a tool (for example an error such as \"You did not use a tool in "
        "your previous response\"), do NOT answer in plain text: deliver the "
        "final answer through the matching completion tool (for example "
        "attempt_completion) as a tool_call block.\n"
        '- Every parameter belongs inside the "arguments" object, keyed '
        'exactly like the tool\'s "parameters" properties -- never beside '
        '"name".\n'
        "- Emit several blocks in one turn when the calls are independent.\n"
        "- Before reviewing or editing code, read the actual files with a "
        "read/search tool first. Listing file names is not reading.\n"
        "- Never write [Tool result for ...] yourself and never invent a "
        "result: emit the block and stop.\n"
        "- Never claim the tools are unavailable or that you lack "
        "permission: they are connected and will run.\n\n"
        f"Available tools:\n{tools_json}{constraint}"
    )


def _join_parts(parts, max_bytes, pinned_head=None) -> str:
    """Join parts; on overflow keep the pinned tool block + newest messages."""
    items = [p for p in parts if p]
    pinned = (pinned_head or "").strip()

    def nb(s):
        return len(s.encode("utf-8"))

    full = "\n\n".join(([pinned] if pinned else []) + items)
    if nb(full) <= max_bytes:
        return full
    log(f"Prompt truncated to {max_bytes} bytes")
    budget = max_bytes - nb(_TRUNCATION_MARKER) - 4
    if pinned:
        cap = max_bytes * 2 // 5
        if nb(pinned) > cap:
            pinned = pinned.encode("utf-8")[:cap].decode("utf-8", "ignore")
        budget -= nb(pinned) + 2
    if budget <= 0:
        return full.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    tail_idx, used = set(), 0
    for pos in range(len(items) - 1, -1, -1):
        cost = nb(items[pos]) + (2 if tail_idx else 0)
        if used + cost > budget:
            break
        tail_idx.add(pos)
        used += cost
    tail = [items[i] for i in sorted(tail_idx)]
    if not tail and items:
        tail = [items[-1].encode("utf-8")[:max(1, budget)].decode("utf-8", "ignore")]
    remaining = budget - used
    head = []
    for pos, part in enumerate(items):
        if pos in tail_idx:
            break
        cost = nb(part) + (2 if head else 0)
        if cost > remaining:
            break
        head.append(part)
        remaining -= cost
    blocks = ([pinned] if pinned else []) + head
    if blocks:
        return "\n\n".join(blocks + [_TRUNCATION_MARKER] + tail)
    return "\n\n".join([_TRUNCATION_MARKER] + tail)


def messages_to_prompt(messages, tools=None, tool_choice=None,
                       max_bytes=None) -> str:
    # prompt_max_bytes <= 0 / thieu = khong gioi han
    max_bytes = int(max_bytes or CONFIG.get("prompt_max_bytes", 0) or 0)
    if max_bytes <= 0:
        max_bytes = 1 << 40
    parts, tool_block = [], None
    if tools and tool_choice != "none":
        defs = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = _fn_of(t)
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            defs.append({"name": fn["name"],
                         "description": fn.get("description", ""),
                         "parameters": fn.get("parameters", {})})
        if defs:
            tool_block = _tool_use_block(defs, _choice_instruction(tool_choice))

    id_to_name = {}
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                if isinstance(tc, dict) and tc.get("id") and fn.get("name"):
                    id_to_name[tc["id"]] = fn["name"]

    combined = set()
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        if role == "developer":
            role = "system"
        content = _stringify_content(m.get("content"))
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if m.get("tool_calls"):
                blocks = []
                for tc in m["tool_calls"]:
                    fn = (tc or {}).get("function") or {}
                    args = fn.get("arguments") or "{}"
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    try:
                        json.loads(args)
                    except (ValueError, json.JSONDecodeError):
                        args = "{}"
                    blocks.append(
                        f'```tool_call\n{{"id": {json.dumps(tc.get("id", ""))}, '
                        f'"name": {json.dumps(fn.get("name", ""))}, '
                        f'"arguments": {args}}}\n```')
                text = f"[Assistant]: {content}\n" + "\n".join(blocks)
                for j in range(i + 1, len(messages)):
                    nm = messages[j]
                    if not isinstance(nm, dict) or nm.get("role") != "tool":
                        break
                    tcid = nm.get("tool_call_id", "")
                    combined.add(tcid)
                    name = nm.get("name") or id_to_name.get(tcid) or "tool"
                    text += (f"\n[Tool result for {name} (id={tcid})]: "
                             + _stringify_content(nm.get("content")))
                parts.append(text)
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            tcid = m.get("tool_call_id", "")
            if tcid in combined:
                continue
            name = m.get("name") or id_to_name.get(tcid) or "tool"
            parts.append(f"[Tool result for {name} (id={tcid})]: {content}")
        else:
            parts.append(content)

    if tool_block:
        parts.append(_TOOL_REMINDER)
    return _join_parts(parts, max_bytes, pinned_head=tool_block)


# ------------------------------------------------------------- parsing ----
_FENCE_OPEN = re.compile(
    r"```(tool_call|function_call|tool_code|json)(?![A-Za-z0-9_])[ \t]*\n?")
_FENCE_CLOSE = re.compile(r"\s*```")
# XML-style <tool_call>...</tool_call> tag Qwen đôi khi sinh ra
_XML_TAG_OPEN = re.compile(r"<tool_call>[ \t]*\n?", re.IGNORECASE)
_XML_TAG_CLOSE = re.compile(r"\s*</tool_call>", re.IGNORECASE)
_ARGUMENT_KEYS = ("arguments", "args", "input", "parameters", "params", "action_input")
_META_KEYS = {"name", "id", "type", "description", "tool", "tool_name", "action", "function"}
_NOTHING = object()

# XML function/parameter format:
#   <function=tool_name>  hoặc  <function name="tool_name">
#   <parameter=key>value</parameter>
# Qwen-Agent và một số checkpoint Qwen 2.5/3 tự sinh ra dạng này.
_XML_FUNC_OPEN = re.compile(
    r"<function[= ](?:name=)?[\"']?(?P<name>[\w.:-]+)[\"']?>[ \t]*\n?",
    re.IGNORECASE,
)
_XML_FUNC_CLOSE = re.compile(r"\s*</function>", re.IGNORECASE)
_XML_PARAM = re.compile(
    r"<parameter[= ](?:name=)?[\"']?(?P<key>[\w.-]+)[\"']?>(?P<val>.*?)(?:</parameter>|(?=<parameter\b)|(?=</function>)|(?=</tool_call>)|$)",
    re.DOTALL | re.IGNORECASE,
)


def _parse_xml_function_block(block_text: str) -> list:
    """Parse dạng XML function/parameter bên trong <tool_call>:

      <function=tool_name>
        <parameter=key>value</parameter>
      </function>

    Hỗ trợ nhiều <function> trong cùng một block và cả cú pháp
    <function name="tool_name"> (khoảng trắng thay cho dấu =).
    Trả về list dict {"name": ..., "arguments": {...}}.
    Trả về [] nếu không tìm thấy bất kỳ <function> nào.
    """
    results = []
    for m_fn in _XML_FUNC_OPEN.finditer(block_text):
        fn_name = m_fn.group("name").strip()
        if not fn_name:
            continue
        # Tìm phần thân: từ sau <function=...> đến </function> hoặc hết block
        body_start = m_fn.end()
        close_m = _XML_FUNC_CLOSE.search(block_text, body_start)
        body = block_text[body_start: close_m.start() if close_m else len(block_text)]
        params: dict = {}
        for m_p in _XML_PARAM.finditer(body):
            key = m_p.group("key").strip()
            val = (m_p.group("val") or "").strip()
            # Cố gắng parse literal JSON (số, bool, list, dict) giữ nguyên;
            # nếu không parse được thì giữ kiểu str.
            try:
                parsed_val = json.loads(val)
                if isinstance(parsed_val, (dict, list, int, float, bool)):
                    params[key] = parsed_val
                    continue
            except (ValueError, json.JSONDecodeError):
                pass
            params[key] = val
        results.append({"name": fn_name, "arguments": params})
    return results


def _parse_lenient_json(raw: str):
    """Cố gắng parse JSON từ chuỗi thô bên trong fence/tag tool_call.

    Sửa chữa các lỗi cú pháp phổ biến của model:
      1. Thiếu key "name": {"grep", "arguments": ...} -> {"name": "grep", ...}
      2. Trailing comma: {"a": 1,} -> {"a": 1}
      3. Python-like literals (single quotes, booleans, None)
      4. Text bao quanh JSON bên trong fence: Cắt từ '{' đầu đến '}' cuối
    """
    raw = (raw or "").strip()
    if not raw:
        return _NOTHING

    try:
        return json.loads(raw, strict=False)
    except ValueError:
        pass

    fb = raw.find("{")
    lb = raw.rfind("}")
    candidate = raw[fb:lb + 1] if (fb != -1 and lb > fb) else raw

    try:
        return json.loads(candidate, strict=False)
    except ValueError:
        pass

    # Sửa lỗi model thiếu key "name": {"grep", "arguments": ...} hoặc {"grep", ...}
    repaired = re.sub(
        r'^\s*\{\s*(["\']?)([a-zA-Z0-9_.-]+)\1\s*,\s*',
        r'{"name": "\2", ',
        candidate,
        count=1,
    )
    # Xoá trailing comma: ,} hoặc ,]
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)

    try:
        return json.loads(repaired, strict=False)
    except ValueError:
        pass

    # Thử ast.literal_eval (hỗ trợ single quotes, Python dict literal)
    try:
        py_str = re.sub(r'\btrue\b', 'True', repaired)
        py_str = re.sub(r'\bfalse\b', 'False', py_str)
        py_str = re.sub(r'\bnull\b', 'None', py_str)
        val = ast.literal_eval(py_str)
        if isinstance(val, dict):
            return val
    except Exception:
        pass

    return _NOTHING


def _iter_blocks(text):
    """Yield (start, end, data, kind); raw_decode so ``` inside strings is safe.

    Hỗ trợ ba dạng:
      1. ```tool_call / ```function_call / ```tool_code / ```json  ... ```
      2. <tool_call>{"name":...}</tool_call>  (XML tag bọc JSON)
      3. <tool_call><function=name><parameter=key>val</parameter></function></tool_call>
         (XML function/parameter — Qwen-Agent style)

    Mỗi block có thể chứa NHIỀU JSON object hoặc NHIỀU <function> liên tiếp:
    parser gom hết tất cả rồi yield từng item riêng lẻ, giữ nguyên
    span [block_start, block_end] chung cho tất cả item trong block.
    """
    dec = json.JSONDecoder(strict=False)

    # --- Gom tất cả match điểm mở (fence + xml) rồi xử lý theo thứ tự ---
    class _Match:
        __slots__ = ("start", "end_open", "kind", "is_xml")

        def __init__(self, start, end_open, kind, is_xml):
            self.start = start
            self.end_open = end_open
            self.kind = kind
            self.is_xml = is_xml

    candidates = []
    for m in _FENCE_OPEN.finditer(text):
        candidates.append(_Match(m.start(), m.end(), m.group(1), False))
    for m in _XML_TAG_OPEN.finditer(text):
        candidates.append(_Match(m.start(), m.end(), "tool_call", True))
    candidates.sort(key=lambda c: c.start)

    consumed_up_to = 0  # ngoài loop dùng để bỏ qua overlap

    for cand in candidates:
        if cand.start < consumed_up_to:
            continue  # nằm trong block đã xử lý trước

        # --- Với XML: thử JSON trước (trên full text), XML function/parameter sau ---
        if cand.is_xml:
            # Tìm close tag để biết biên block; raw_block chỉ dùng cho XML-function path.
            close_m = _XML_TAG_CLOSE.search(text, cand.end_open)
            block_end = close_m.end() if close_m else len(text)
            raw_block = text[cand.end_open:(close_m.start() if close_m else len(text))]

            # --- Nhánh A: thử decode JSON trực tiếp trên full text (không slice) ---
            # Dùng raw_decode trên `text` thay vì `raw_block` để ``` và </tool_call>
            # nằm trong chuỗi JSON không làm sai biên (giống nhánh fence).
            j = cand.end_open
            while j < len(text) and text[j] in " \t\r\n":
                j += 1

            any_decoded = False
            if j < len(text) and text[j] == "{":
                # Chỉ thử JSON khi ký tự đầu trông như JSON object
                _j = j
                while _j < len(text):
                    while _j < len(text) and text[_j] in " \t\r\n":
                        _j += 1
                    if _j >= len(text):
                        break
                    try:
                        data, rel_end = dec.raw_decode(text, _j)
                    except ValueError:
                        # Phần còn lại không decode được → thử lenient trên phần đó
                        # (chỉ khi đã lấy được ít nhất 1 object trước đó)
                        if any_decoded:
                            end_of_block = close_m.start() if close_m else len(text)
                            remainder = text[_j:end_of_block].strip()
                            if remainder:
                                data = _parse_lenient_json(remainder)
                                yield cand.start, block_end, data, cand.kind
                        break
                    # Kiểm tra close tag ngay sau JSON object
                    close_after = _XML_TAG_CLOSE.match(text, rel_end)
                    if close_after:
                        # Tìm thấy </tool_call> thật ngay sau JSON → đây là biên thực
                        this_block_end = close_after.end()
                        block_end = this_block_end
                        yield cand.start, block_end, data, cand.kind
                        any_decoded = True
                        break
                    # Chưa gặp close tag → còn JSON tiếp theo, nhưng kiểm tra không
                    # vượt quá block đã biết
                    end_of_block = close_m.start() if close_m else len(text)
                    if rel_end > end_of_block:
                        # rel_end đã vượt biên block → chấp nhận data, dừng
                        yield cand.start, block_end, data, cand.kind
                        any_decoded = True
                        break
                    yield cand.start, block_end, data, cand.kind
                    any_decoded = True
                    _j = rel_end

            # --- Nhánh B: XML function/parameter (Qwen-Agent style) ---
            # Chỉ thử khi JSON không decode được — tránh false-positive khi
            # <function=...> xuất hiện bên trong chuỗi JSON.
            if not any_decoded and _XML_FUNC_OPEN.search(raw_block):
                xml_calls = _parse_xml_function_block(raw_block)
                if xml_calls:
                    for item in xml_calls:
                        yield cand.start, block_end, item, cand.kind
                    consumed_up_to = block_end
                    continue
                # Không tìm thấy <function> hợp lệ → fall through sang lenient JSON

            # --- Nhánh C: fallback lenient JSON (khi cả hai nhánh trên thất bại) ---
            if not any_decoded:
                data = _parse_lenient_json(raw_block)
                yield cand.start, block_end, data, cand.kind

            consumed_up_to = block_end

        else:
            # --- Fence block: dùng raw_decode trên toàn `text` để ``` trong
            # chuỗi JSON không bị hiểu nhầm là thẻ đóng. ---
            j = cand.end_open
            while j < len(text) and text[j] in " \t\r\n":
                j += 1

            any_decoded = False
            block_end = None  # xác định sau khi gặp close fence thật
            # Buffer các object chưa yield (chưa biết block_end xác thực)
            pending_items = []

            while j <= len(text):
                while j < len(text) and text[j] in " \t\r\n":
                    j += 1
                if j >= len(text):
                    break

                # Nếu đã biết block_end và j vượt qua → dừng
                if block_end is not None and j >= block_end:
                    break

                try:
                    data, rel_end = dec.raw_decode(text, j)
                except ValueError:
                    # JSON decode thất bại — thử lenient cho phần còn lại nếu
                    # đã lấy được ít nhất 1 object (Bug C fix).
                    if any_decoded:
                        # Tìm close fence để xác định biên
                        peek = _FENCE_CLOSE.search(text, j)
                        end_of_fence = peek.start() if peek else len(text)
                        remainder = text[j:end_of_fence].strip()
                        this_block_end = peek.end() if peek else len(text)
                        block_end = block_end or this_block_end
                        if remainder:
                            ldata = _parse_lenient_json(remainder)
                            pending_items.append((cand.start, ldata, cand.kind))
                        # Flush pending với block_end đã biết
                        for _ps, _pd, _pk in pending_items:
                            yield _ps, block_end, _pd, _pk
                        pending_items = []
                    break

                # Kiểm tra close fence NGAY SAU JSON (dùng match, không search —
                # search sẽ trúng ``` bên trong chuỗi JSON tiếp theo → Bug B).
                close_m = _FENCE_CLOSE.match(text, rel_end)
                if close_m:
                    this_block_end = close_m.end()
                    block_end = this_block_end
                    # Flush tất cả object pending trước, rồi yield object này
                    for _ps, _pd, _pk in pending_items:
                        yield _ps, block_end, _pd, _pk
                    pending_items = []
                    yield cand.start, block_end, data, cand.kind
                    any_decoded = True
                    j = this_block_end
                    break
                else:
                    # Chưa gặp close fence ngay sau → còn JSON tiếp theo.
                    # Giữ trong pending, block_end sẽ cập nhật khi gặp close thật.
                    pending_items.append((cand.start, data, cand.kind))
                    any_decoded = True
                    j = rel_end

            # Nếu còn pending mà chưa flush (unclosed fence / EOF)
            if pending_items:
                block_end = block_end or len(text)
                for _ps, _pd, _pk in pending_items:
                    yield _ps, block_end, _pd, _pk
                pending_items = []

            if not any_decoded:
                # JSON decode thất bại hoàn toàn → thử parse linh hoạt
                close_idx = text.find("```", cand.end_open)
                close_start = close_idx if close_idx != -1 else len(text)
                close_end = (close_idx + 3) if close_idx != -1 else len(text)
                raw_fallback = text[cand.end_open:close_start]
                data = _parse_lenient_json(raw_fallback)
                yield cand.start, close_end, data, cand.kind
                block_end = close_end

            consumed_up_to = block_end or len(text)




def _normalize_name(name, allowed):
    if not isinstance(name, str) or not name:
        return None
    stripped = name
    while True:
        low = stripped.lower()
        if low.startswith("functions.") or low.startswith("default_api."):
            stripped = stripped.split(".", 1)[1]
            if not stripped:
                return None
        else:
            break
    if allowed is None:
        return stripped or None
    if stripped in allowed:
        return stripped
    cands = {a for a in allowed if a.lower() == stripped.lower()}
    if not cands and "." in stripped:
        seg = stripped.split(".")[-1].lower()
        cands = {a for a in allowed if a.split(".")[-1].lower() == seg}
    return next(iter(cands)) if len(cands) == 1 else None


def _wrapper_type_error(data, props=None):
    """Kiem tra KIEU cua khoa arguments/args/...
    TRUOC khi _extract_args xu ly dang viet tat. Truoc day `arguments: []` o tool
    chi co tham so tuy chon bi doi thanh {"arguments": []} roi tra cho client.
    Tra ve mo ta loi, "" neu hop le. Khoa trung ten tham so that (key in props)
    khong tinh la wrapper.
    """
    for key in _ARGUMENT_KEYS:
        if key not in data:
            continue
        if props is not None and key in props:
            continue
        v = data[key]
        if v is None or isinstance(v, dict):
            continue
        if isinstance(v, str):
            continue   # _args_json tu kiem tra chuoi co phai JSON object khong
        return f"arguments must be an object, got {type(v).__name__}"
    return ""


def _extract_args(data, props=None):
    # Wrapper la chuoi rong/khoang trang (vd `"arguments": ""`): xu ly TRUOC nhanh viet tat,
    # khong de no thanh tham so {"arguments": ""}. Khoa trung ten tham so that thi bo qua.
    empty_wrappers = set()
    for key in _ARGUMENT_KEYS:
        if key in data:
            v = data[key]
            if isinstance(v, dict):
                if (props is not None and key in props
                        and (set(data) - _META_KEYS - {key})):
                    continue
                return v
            if isinstance(v, str):
                if props is not None and key in props:
                    continue
                if v.strip():
                    return v
                empty_wrappers.add(key)
    if props is None:
        rest = {k: v for k, v in data.items() if k not in _META_KEYS}
    else:
        rest = {k: v for k, v in data.items()
                if k not in _META_KEYS or k in props}
    rest = {k: v for k, v in rest.items() if k not in empty_wrappers}
    if empty_wrappers and not rest:
        # Chi co wrapper rong: tra "" de _args_json bao "empty string"
        # (tool co schema -> tu choi + retry; tool khong tham so -> {}).
        return ""
    # arguments: null = khong co tham so; khong de lot {"arguments": null}
    rest = {k: v for k, v in rest.items() if v is not None}
    if len(rest) == 1:
        k, v = next(iter(rest.items()))
        if k in ("parameters", "params") and isinstance(v, dict):
            return v
    return rest or {}


def _args_json(args):
    if isinstance(args, dict):
        return json.dumps(args, ensure_ascii=False), None
    if isinstance(args, str):
        t = args.strip()
        if not t:
            return "{}", "empty string"
        try:
            p = json.loads(t, strict=False)
            if isinstance(p, dict):
                return json.dumps(p, ensure_ascii=False), None
        except ValueError:
            pass
        return "{}", "not a JSON object"
    if args is None:
        return "{}", "missing"
    return "{}", f"unsupported type {type(args).__name__}"


def parse_tool_calls(text, allowed_names=None, tool_schemas=None):
    """Returns (clean_text, tool_calls, dropped_reasons)."""
    calls, dropped, clean_parts, last_end = [], [], [], 0
    # prev_block_start: theo dõi để tránh chèn prose giữa các item trong cùng block
    prev_block_start = -1
    for start, end, data, kind in _iter_blocks(text or ""):
        if kind == "json":
            if (not isinstance(data, dict) or not allowed_names
                    or data.get("name") not in allowed_names):
                continue
            has_wrapper = any(k in data for k in _ARGUMENT_KEYS)
            keys = set(data) - _META_KEYS
            props = (tool_schemas or {}).get(data.get("name"))
            if not has_wrapper and not (keys and props and keys <= props):
                continue
        else:
            line_start = text.rfind("\n", 0, start) + 1
            # Bỏ qua các thẻ XML đóng như </tool_call> trước block tiếp theo
            # (không phải prose thực sự, không được coi là mid-sentence)
            prefix = re.sub(r"</?\w[\w:.-]*(?:\s[^>]*)?>", "", text[line_start:start]).strip()
            if prefix:
                continue  # quoted mid-sentence: explanation, not a call
        parsed, reason = None, ""
        if isinstance(data, dict):
            name = None
            if isinstance(data.get("function"), dict):
                fn = data["function"]
                name = fn.get("name")
                data = fn
            elif "action" in data and ("action_input" in data or "arguments" in data):
                name = data.get("action")
                args_val = data.get("action_input", data.get("arguments"))
                data = {"name": name, "arguments": args_val}
            else:
                name = (data.get("name") or data.get("tool")
                        or data.get("tool_name")
                        or (data.get("function") if isinstance(data.get("function"), str) else None))

            # Trường hợp dict chỉ có 1 key là tên tool: {"grep": {"pattern": ...}}
            if not name and len(data) == 1:
                cand = next(iter(data.keys()))
                if cand not in _META_KEYS and cand not in _ARGUMENT_KEYS:
                    norm = _normalize_name(cand, allowed_names)
                    if norm or (allowed_names is None and re.match(r"^[a-zA-Z0-9_.-]+$", cand)):
                        name = norm or cand
                        inner = data[cand]
                        data = inner if isinstance(inner, dict) else {"arguments": inner}

            resolved = _normalize_name(name, allowed_names) if name else None
            if not name:
                reason = "no tool name"
            elif allowed_names is not None and resolved is None:
                reason = f"undeclared tool '{name}'"
            else:
                name = resolved or name
                props = (tool_schemas or {}).get(name)
                bad_type = _wrapper_type_error(data, props)
                if bad_type and CONFIG.get("tool_reject_bad_arguments", True):
                    # sai kieu -> tu choi + retry
                    reason = f"unusable arguments ({bad_type})"
                    args, degraded = "{}", None
                else:
                    args, degraded = _args_json(_extract_args(data, props))
                if degraded:
                    # tu choi call co
                    # arguments hong thay vi bien thanh {} roi tra cho client.
                    harmless = degraded in ("missing", "empty string") and not props
                    if CONFIG.get("tool_reject_bad_arguments", True) and not harmless:
                        reason = f"unusable arguments ({degraded})"
                    else:
                        log(f"tool_call '{name}': arguments unusable ({degraded})")
                if not reason:
                    parsed = {"id": f"call_{uuid.uuid4().hex[:8]}",
                              "type": "function",
                              "function": {"name": name, "arguments": args}}
        else:
            reason = "body is not valid JSON"
        if parsed is None:
            dropped.append(reason)
            log(f"Dropping tool_call block: {reason}: {text[start:end][:200]}")
            continue
        # Chỉ cắt prose trước block lần đầu gặp block này;
        # các item tiếp theo trong cùng block (cùng start) không chèn thêm text.
        if start != prev_block_start:
            clean_parts.append(text[last_end:start])
            prev_block_start = start
        last_end = end
        calls.append(parsed)
    clean_parts.append(text[last_end:])
    clean = "".join(clean_parts)
    return clean.strip(), calls, dropped


def looks_like_tool_call(text) -> bool:
    for _s, _e, data, kind in _iter_blocks(text or ""):
        if isinstance(data, dict):
            name = (data.get("name") or data.get("tool") or data.get("tool_name")
                    or (data.get("function") if isinstance(data.get("function"), str) else None))
            if not name and isinstance(data.get("function"), dict):
                name = data["function"].get("name")
            if not name and len(data) == 1:
                cand = next(iter(data.keys()))
                if cand not in _META_KEYS and cand not in _ARGUMENT_KEYS:
                    name = cand
            if name:
                if kind != "json" or any(k in data for k in _ARGUMENT_KEYS):
                    return True
    return False


_MISS_RE = re.compile(
    r"\b(?:let me|i'?ll|i will|let's)\s+"
    r"(?:read|check|look|see|inspect|examine|write|create|update|edit|run|fix|implement)\b"
    r"|\b(?:de minh|minh se|toi se)\s+"
    r"(?:doc|kiem tra|xem|viet|tao|sua|chay)\b", re.IGNORECASE)


def looks_like_missed_tool_call(text) -> bool:
    if not text or len(text) >= 1200:
        return False
    tl = text.lower()
    if ("```tool_call" in text or "```function_call" in text
            or "<tool_call>" in tl):
        return False
    # <function=...> standalone (không có <tool_call>) vẫn cần nudge vì
    # _iter_blocks không parse được dạng thiếu wrapper → retry có ích.
    # Chỉ suppress khi <function=...> đi kèm <tool_call> (đã handled trên).
    return bool(_MISS_RE.search(text))


def build_response_format_instruction(response_format) -> str:
    if not isinstance(response_format, dict):
        return ""
    t = response_format.get("type")
    if t == "json_object":
        return ("\n\nIMPORTANT: Respond with a single valid JSON object only. "
                "Do not include prose, explanations or Markdown code fences.")
    if t == "json_schema":
        schema = response_format.get("json_schema", {})
        if isinstance(schema, dict) and "schema" in schema:
            schema = schema["schema"]
        return ("\n\nIMPORTANT: Respond with a single valid JSON value that "
                "strictly conforms to the following JSON Schema. Do not "
                "include prose or Markdown code fences.\nJSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False))
    return ""


def strip_code_fence(text) -> str:
    if not text:
        return text
    stripped = text.strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*[ \t]*\n?(.*?)\n?```$", stripped, re.DOTALL)
    return m.group(1).strip() if m else text