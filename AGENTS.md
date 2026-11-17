# AGENTS.md — qwen-web2api

Proxy Qwen Chat (chat.qwen.ai) → API tương thích OpenAI. Chạy **bridge-only**: Python không
bao giờ gọi chat.qwen.ai trực tiếp. Cùng process có 2 server: API công khai ở `CONFIG["port"]`
(default 8082) và bridge nội bộ ở `CONFIG["bridge_port"]` (8083). Extension trong tab
chat.qwen.ai tự bấm UI/bắt SSE; Python chỉ xếp job và stream kết quả về client.

File này mô tả **cấu trúc và trạng thái hiện tại**. Lịch sử fix, tên patch/finding và lý do
từng thay đổi nằm ở `CHANGELOG.md` — đọc khi cần giải thích tại sao code lại như vậy.

## Chạy & kiểm tra

```powershell
python -m qwen_web2api                 # luôn bridge mode
python -m qwen_web2api --port 8081     # override port API
python -m unittest test_qwen_web2api -v
python -m pytest test_qwen_web2api.py -q
node --check extension/bridge.js       # cú pháp content script
```

Cần mở https://chat.qwen.ai (đã đăng nhập), giữ tab mở và load unpacked `extension/`.
Không có tab/worker → request trả 503.

## Đọc file nào khi nào

| File | Vai trò | Đọc khi cần |
| --- | --- | --- |
| `qwen_web2api/server.py` | HTTP OpenAI-compatible: `/v1/chat/completions`, `/v1/responses`, `/v1/models`, auth, CORS, SSE, `_run_turn` (tool retry/nudge), `prepare_large_prompt` | route, SSE response, auth, tool retry, file prompt |
| `qwen_web2api/bridge.py` | Server 8083 cho extension: `/poll`, `/event`, `/status`, `/debug`, `/requeue`, `/file/*`, `/alive`, `/cancel`; queue job, stream event, `known_models()`, `temporary_chat_enabled()`, `job_cancelled()`, `job_alive()`, `cancel_job()` | job queue/timeout/requeue, liveness, file serving, models feed |
| `qwen_web2api/tools.py` | Flatten messages → prompt, chèn `# Tool Use`, parse tool_call, truncate theo byte | prompt render, tool_call parser |
| `qwen_web2api/models.py` | Alias model + `@think=0/1` | tên model |
| `qwen_web2api/config.py` | `DEFAULT_CONFIG` + `CONFIG` (global mutable), load/find config | giá trị mặc định, alias config key |
| `qwen_web2api/qwen.py` | `QwenError` + logger `log()`, `debug_log()`, `is_debug()`, `get_recent_log()` | log/error dùng chung |
| `qwen_web2api/__main__.py` | CLI, `_guard_bind` tự sinh API key khi bind ra ngoài loopback | khởi động, auth |
| `extension/bridge.js` | MV3 content script (MAIN world): poll job, temporary/model/mode picker, gửi prompt, attach file, bắt native `tool_call`, hook fetch/XHR | mọi hành vi trong tab Qwen |
| `test_qwen_web2api.py` | Toàn bộ unit test (1 file, stdlib `unittest`) | regression, mock endpoint |
| `test_thinking_trace.js` | Scratch Node debug parser thinking (không phải test suite) | debug SSE shape upstream |
| `CHANGELOG.md` | Lịch sử patch/finding | lý do thay đổi, triệu chứng cũ |

Luồng chat: `do_POST` → `_handle_chat` / `_handle_responses` → `prepare_large_prompt` →
`messages_to_prompt` → `_run_turn` → `bridge.generate` / `bridge.generate_stream` →
`parse_tool_calls` → JSON hoặc SSE.

## Config chính

Giá trị mặc định và alias nằm trong `qwen_web2api/config.py`. Các key quan sát được:

- Transport: `port`, `bridge_port` (8083), `bridge_worker_ttl` (20s), `bridge_job_timeout` (600s),
  `bridge_poll_wait` (20s). `extension/bridge.js` hardcode `http://127.0.0.1:8083` — đổi port
  phải sửa cả 2 nơi.
- `temporary_chat` (chấp nhận cả spelling `temporary-chat`), `temporary_chat_reload` (default
  `true`), `captcha_wait_seconds` (default 300, `0` = tắt).
- UI: `bridge_select_model` (true), `bridge_select_mode` (true), `bridge_force_request_model`
  (false, thử nghiệm), `bridge_require_model` / `bridge_require_mode` (false),
  `bridge_require_attachment` (true).
- File/prompt: `prompt_max_bytes` (0 = không cắt), `prompt_hard_max_chars` (0),
  `file_prompt_max_chars` (mặc định 130000), `file_prompt_truncate` (false),
  `attachment_prompt` (mặc định `"Please read the text and action"`).
- Liveness/retry: `bridge_cancel_on_disconnect` (true), `bridge_max_requeues` (8),
  `retry_attempts`, `retry_delay_sec`, `tool_retry_on_miss`, `tool_retry_attempts`,
  `tool_reject_bad_arguments` (true).
- Khác: `default_model`, `strict_models`, `thinking`, `max_body_bytes`, `log_requests`,
  `log_file`, `debug` (alias `debug_log`/`verbose_log`).

`debug=true` → `/status` trả `debug: true`, extension log chi tiết + forward event `debug`,
`GET /debug` (8083) trả snapshot config/bridge state/100 dòng `recent_log`.

## Hành vi hiện tại cần lưu ý

### Extension & UI

- Poll loop đọc `/status`. Nếu `temporary_chat` khác URL: có job → `/requeue` rồi navigate;
  không job → navigate luôn; quá 2 retry → warn, chạy tiếp. Idle navigation cap 2 lần/60s
  (`sessionStorage`). `isTemporaryPage(strict)`: chế độ thường chỉ tin URL/cache, không tin DOM
  (nút toggle luôn hiện → reload lặp).
- Temporary mode: `tryNewChat` không tìm nút New Chat. `pageDirty()` = `pageUsed` hoặc
  (nếu không phải do bridge vừa reload — cờ `qwenUIBridgeFreshReload`) heuristic
  `hasChatMessages()`. Dirty → `/requeue` kèm `fresh_reloads` rồi
  `location.assign('/?temporary-chat=true')`, tối đa 1 lần/job. Trang vừa reload được coi là sạch.
- Chế độ thường bấm New Chat mỗi job (`NEW_CHAT_EACH_JOB = true`). `waitUiIdle` bấm Stop nếu
  UI vẫn đang generate (job trước settle sớm do native `tool_call`/`role=function`).
  `findSendButton` bỏ qua nút Stop.
- Model picker: trigger/option được tìm theo id/name đã chuẩn hoá (bỏ `@think=`). Picker
  `.wms-*` có popup phụ qua `.wms-more-entry`. Chọn lỗi → `warn` (mặc định chạy tiếp với model
  UI cũ) hoặc huỷ+retry nếu `bridge_require_model`. `bridge_force_request_model` ghi đè
  `model`/`models` trong body request.
- Mode: dropdown gần ô prompt; click `.qwen-chat-v2-dropdown-menu-select`, item
  `role="menuitem"` / `.qwen-chat-v2-dropdown-menu-item`. none/minimal/low → Fast, medium →
  Auto, high/xhigh/max → Thinking. `/poll` trả `mode`, vòng poll phải copy vào `job`.
- `refreshModels()` đọc `/api/v2/models` trong tab (throttle 10 phút), gửi qua body `/poll`,
  merge vào `/v1/models`.
- Captcha: `waitCaptchaClear` chờ ở 3 chỗ (trước new-chat/reload, sau `waitPageReady`, lúc Send
  không bấm được). Selector hiện là **đoán** (`captchaSignal`: baxia/turnstile/URL
  `_____tmd_____`/text dialog) — cần DOM thật để chỉnh.
- Lỗi UI mới phải `warn`/log rồi chạy tiếp, không throw làm hỏng request.

### SSE & thinking

- Upstream thinking: `phase="thinking_summary"` với `extra.summary_thought` /
  `extra.summary_title` (mảng cumulative lẫn block disjoint `status:"finished"`). `_seenTitles`
  in mỗi step title đậm **một lần**. `on_reasoning_delta` stream trực tiếp qua SSE, kể cả khi
  `tools_active`. Cũng nhận `reasoning_content` / `phase=thinking|think|reasoning`.
- `SEND_TIMEOUT_MS` 90s (huỷ ngay khi response bắt đầu) → watchdog idle
  `STREAM_IDLE_TIMEOUT_MS` 3 phút, reset theo chunk. `reader.cancel()` khi idle/settle để
  không giữ socket.
- Hook cả `window.fetch` và `XMLHttpRequest`. Có handler `error`/`abort`/`timeout`/`loadend`
  + watchdog để giải phóng worker. Navigation watchdog tự bỏ cờ `navigating` sau 20s.
- Delta lạ → warn 1 lần (phase + keys); 20 dòng SSE gần nhất in bằng
  `__qwenUIBridgeDebug()`.

### File prompt & attach

- `prepare_large_prompt` cắt bằng `_render_fit` (`FILE_MAX_CHARS`=130000, giữ đầu + tin mới
  nhất, chèn `[...truncated...]`). `messages_to_prompt(max_bytes=...)` ép trần byte. CRLF được
  chuẩn hoá LF.
- Nhiều tin nhắn và lượt cuối là user (<= 130000): lượt cuối (+ tool definitions /
  response_format) làm `attachment_prompt` điền composer. Toàn bộ prompt nằm trong file:
  composer = config `attachment_prompt` + `file_info.extra_instructions` +
  `file_info.retry_nudge`, ghép bởi `bridge._file_companion`.
- `attachLongPrompt` thứ tự: **paste-file → drop-file → paste-text → file-input**, mỗi bước kiểm
  tra attachment card. Hai cái đầu thành công cao nhất; hai cái cuối dễ tạo request sai.
  `paste-file`/`drop-file` dùng chung `new File([text], fileInfo.filename, ...)` **trong memory**
  (không tạo file đĩa mới) — chỉ khác event dispatch. File tạm trên đĩa là `qwen_prompt_*.txt`
  do Python tạo, serve qua `/file/<filename>`. Không có ngưỡng bỏ paste riêng: A/B ở ~320k ký tự
  cho cả hai tạo card trong ~1–2s; muốn đổi thì sửa mảng `strategies`.
- Bảo vệ inline: composer rỗng sau khi điền > 2000 ký tự → `learnInlineLimit`
  (`qwenUIBridgeInlineLimit`, trần 32000) + retry; hook fetch chặn `content_chars` < 90% prompt.
  Prompt >= `inlineMaxChars` tự attach file. Nếu mọi strategy thất bại và text thô > 125000
  ký tự → ném lỗi (Python retry) thay vì gửi text quá limit; nếu không, composer chỉ còn companion
  prompt và **mất lịch sử**. Job có file dùng `SEND_TIMEOUT_FILE_MS` (240s).
- `file_prompt_truncate=false` → không cắt (mặc định, giữ evidence); `prompt_hard_max_chars` vượt
  → HTTP 413. `bridge_require_attachment` chặn request thiếu attachment trừ khi nội dung text
  >= 95% `file_info.size`.

### Liveness / huỷ / retry

- Trước mỗi thao tác UI extension hỏi `GET /alive?job_id=<jid>` (`jobAlive(force=true)` bỏ cache
  1.5s). `POST /cancel {job_id, reason}` huỷ job; `/requeue` trả 409 khi job chết/quá TTL/quá
  `bridge_max_requeues`; `/event` cho job chết trả `{stale:true, cancelled}` → extension dừng UI
  và bỏ response cũ. `job.cancelled` được set khi stale hoặc quá `job.deadline`.
- `_watch_disconnect` + heartbeat SSE đánh dấu `gone` cho lỗi socket xác định (`_is_disconnect_error`).
  `bridge_cancel_on_disconnect` huỷ job non-stream. `tab_reloaded` → fail nhanh 502 + retry;
  `tab_reloading` chỉ warn. Job orphan (caller ngắt/TTL) bị `/poll` drop.
- Tool drop (undeclared / `body is not valid JSON`) → `_run_turn` nudge retry kèm lý do + tool
  hợp lệ. Tool built-in của Qwen (`QWEN_BUILTIN_TOOLS`, hiện `code_interpreter`) bị extension bỏ
  qua để Qwen tự chạy.

### Tool protocol

Fence ` ```tool_call `, chuẩn `{"name": "...", "arguments": {...}}`. Parser tự sửa shorthand,
single-key dict, trailing comma, single quote; nhận thêm XML tag + JSON và XML
`<function=...><parameter=...>` (không nhận `<function=...>` standalone → nudge retry). Prose đi
cùng tool_call được giữ ở `content`. `tool_reject_bad_arguments=true` (mặc định) từ chối
arguments sai kiểu thay vì biến thành `{}`. Chi tiết lịch sử ở `CHANGELOG.md`.

## Điểm dễ vỡ

- `extension/bridge.js` hardcode bridge port 8083.
- DOM heuristics chưa có test UI thật: model picker, mode dropdown, captcha selector, nút Send.
  Qwen đổi layout → sửa `findModelTrigger` / `selectModel` / `findModeTrigger` / `captchaSignal`.
- `QWEN_BUILTIN_TOOLS` là danh sách cứng; Qwen thêm built-in mà không cập nhật → bị nudge như
  tool lạ.
- Temporary switch có thể `location.assign` làm mất JS đang chạy; job đã `/requeue` vẫn nằm trong
  `_jobs` nhưng request timeout nếu tab không reload.
- Global state cần restore trong test: `bridge._pending`, `_jobs`, `_models`, `_last_seen`,
  `_cancelled`, `_managed_jobs`, `qwen._recent_log`.
- Trên Windows, handler trả lỗi trước khi đọc body có thể reset connection (test 403 gửi body rỗng).

## Quy ước code & test

- Python 3.10+, ưu tiên stdlib; không thêm dependency. Không commit cookie/token/capture request.
- Giữ style + ngôn ngữ comment của file đang sửa (Việt/Anh đều có). Comment mô tả **hành vi hiện
  tại**, không ghi tên script patch tạm hay `finding N`.
- Không đổi shape response/stream OpenAI-compatible nếu không có lý do rõ. `CONFIG` là global
  mutable → test snapshot/restore qua `_Sandbox`. `log()` chỉ ghi khi `CONFIG["log_requests"]`.
- `extension/bridge.js` là content script MV3 duy nhất, MAIN world (override được `window.fetch`),
  không thêm file/build step.
- Một file test: `test_qwen_web2api.py` (unittest, pytest collect được). Không network/browser/
  token; mock `bridge._pending`/`generate`/`known_models`; endpoint bridge dùng HTTP server
  loopback port 0. Không nới assertion chỉ để pass nếu hành vi đang sai.
- `test_thinking_trace.js` là scratch Node, không chạy CI (`node test_thinking_trace.js`).
- Khi thiếu ngữ cảnh: xin đúng file thay vì đoán. Không suy đoán shape SSE upstream — trích từ
  `bridge.js` hoặc capture thật. Nếu buộc làm khi thiếu file, nêu rõ giả định.

## Cập nhật tài liệu

| Thay đổi | Cập nhật |
| --- | --- |
| Thêm/xoá/đổi tên file chính | Bản đồ file (bảng "Đọc file nào khi nào") |
| Thêm/đổi config key hoặc default | Config chính; `config.py` |
| Thêm endpoint bridge | Bản đồ file + Hành vi hiện tại |
| Đổi luồng chat / SSE / attach / liveness | Hành vi hiện tại; Luồng chat |
| Phát hiện điểm dễ vỡ mới | Điểm dễ vỡ |
| Sửa lỗi do nguyên nhân cũ | `CHANGELOG.md` (kèm triệu chứng, lý do) |

Chỉ cập nhật khi **interface hoặc hành vi quan sát được từ bên ngoài** thay đổi — rename biến
nội bộ/refactor không đổi hành vi thì không cần.
