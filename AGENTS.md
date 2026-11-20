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
python -m unittest test_session -v     # chế độ session (extension giả bằng _FakeTab)
python -m pytest test_qwen_web2api.py -q
node --check extension/bridge.js       # cú pháp content script
node --test test_bridge.js              # test logic extension (Node >= 18, không cần browser)
```

Cần mở https://chat.qwen.ai (đã đăng nhập), giữ tab mở và load unpacked `extension/`.
Không có tab/worker → request trả 503.

## Đọc file nào khi nào

| File | Vai trò | Đọc khi cần |
| --- | --- | --- |
| `qwen_web2api/server.py` | HTTP OpenAI-compatible: `/v1/chat/completions`, `/v1/responses`, `/v1/models`, auth, CORS, SSE, `_run_turn` (tool retry/nudge), `prepare_large_prompt` | route, SSE response, auth, tool retry, file prompt |
| `qwen_web2api/bridge.py` | Server 8083 cho extension: `/poll`, `/event`, `/status`, `/debug`, `/requeue`, `/file/*`, `/alive`, `/cancel`; queue job, stream event, `known_models()`, `temporary_chat_enabled()`, `job_cancelled()`, `job_alive()`, `cancel_job()` | job queue/timeout/requeue, liveness, file serving, models feed |
| `qwen_web2api/session.py` | Chế độ session (`bridge_session_mode`): `SessionStore.plan/commit/fail`, hash chuỗi history, render delta, prompt (`DEFAULT_PROMPTS`). Thuần, không I/O | gửi tiếp vào chat Qwen thay vì replay |
| `qwen_web2api/tools.py` | Flatten messages → prompt, chèn `# Tool Use`, parse tool_call, truncate theo byte | prompt render, tool_call parser |
| `qwen_web2api/models.py` | `DEFAULT_MODELS`; `MODELS` đọc live từ config `models`; `@think=0/1` | tên model |
| `qwen_web2api/config.py` | `DEFAULT_CONFIG` + `CONFIG` (global mutable), load/find config | giá trị mặc định, alias config key |
| `qwen_web2api/qwen.py` | `QwenError` + logger `log()`, `debug_log()`, `is_debug()`, `get_recent_log()`, `init_session_log()`, `save_prompt_copy()` | log/error dùng chung, log theo phiên |
| `qwen_web2api/__main__.py` | CLI, `_guard_bind` tự sinh API key khi bind ra ngoài loopback | khởi động, auth |
| `extension/bridge.js` | MV3 content script (MAIN world): poll job, temporary/model/mode picker, gửi prompt, attach file, bắt native `tool_call`, hook fetch/XHR | mọi hành vi trong tab Qwen |
| `test_qwen_web2api.py` | Toàn bộ unit test Python (stdlib `unittest`); `TestBridgeJs` gọi `test_bridge.js` | regression, mock endpoint |
| `test_session.py` | Test `session.py` + nối vào server/bridge (e2e bằng `_FakeTab` thay extension) | session/fork/reset |
| `test_bridge.js` | Test logic `extension/bridge.js` bằng `node:test` (cắt nguyên văn thân hàm + stub DOM) | huỷ giữa chừng khi attach, liveness, đối chiếu file |
| `test_thinking_trace.js` | Scratch Node debug parser thinking (không phải test suite) | debug SSE shape upstream |
| `CHANGELOG.md` | Lịch sử patch/finding | lý do thay đổi, triệu chứng cũ |

Luồng chat: `do_POST` → `_handle_chat` / `_handle_responses` (bọc `sess.turn_guard()`) →
`_session_plan` → `_compose_prompt` (`prepare_large_prompt` + `messages_to_prompt`, hoặc delta
của session) → `_run_turn` → `bridge.generate` / `bridge.generate_stream` → `parse_tool_calls` →
JSON hoặc SSE → `sess.STORE.commit`.

## Config chính

Giá trị mặc định và alias nằm trong `qwen_web2api/config.py`. Các key quan sát được:

- Transport: `port`, `bridge_port` (8083), `bridge_worker_ttl` (20s), `bridge_job_timeout` (600s),
  `bridge_poll_wait` (20s). `extension/bridge.js` hardcode `http://127.0.0.1:8083` — đổi port
  phải sửa cả 2 nơi.
- `temporary_chat` (chấp nhận cả spelling `temporary-chat`), `temporary_chat_reload` (default
  `true`), `captcha_wait_seconds` (default 300, `0` = tắt).
- UI: `bridge_select_model` (true), `bridge_select_mode` (**false**: không bấm dropdown mode),
  `bridge_force_request_model` (false, thử nghiệm), `bridge_force_request_mode` (**true**) +
  `bridge_mode_feature_config` (mặc định = 3 mode lấy từ request thật, xem "Mode Auto/Thinking/Fast"),
  `bridge_require_model` / `bridge_require_mode` (false; `require_mode` chỉ có nghĩa khi
  `bridge_select_mode=true`), `bridge_require_attachment` (true),
  `bridge_attachment_block` (`["name","size","status","count","content"]`; `stale` opt-in; `[]` = chỉ cảnh báo).
- File/prompt: `prompt_max_bytes` (0 = không cắt), `prompt_hard_max_chars` (0), `inline_prompt_max_chars` (30000; 0 = trần cũ 130000),
  `file_prompt_max_chars` (mặc định 130000), `file_prompt_truncate` (false),
  `attachment_prompt` (mặc định `"Please read the text and action"`).
- Session (xem "Chế độ session"): `bridge_session_mode` (**false**), `session_max_chars` (400000),
  `session_max_turns` (60), `session_idle_ttl` (1800s), `session_file_threshold` (30000),
  `session_reminder_every` (8), `session_strict_assistant` (false), `session_hash_ignore`
  (regex bỏ khỏi nội dung trước khi hash; mặc định `<environment_details>…`), `session_prompts`
  (ghi đè từng prompt trong `session.DEFAULT_PROMPTS`), `session_fork` (**false**, thử nghiệm),
  `session_fork_user_action` (`"edit"`), `session_nudge_mode` (`append`|`fork`).
- Liveness/retry: `bridge_cancel_on_disconnect` (true), `bridge_max_requeues` (8),
  `retry_attempts`, `retry_delay_sec`, `tool_retry_on_miss`, `tool_retry_attempts`,
  `tool_reject_bad_arguments` (true).
- Khác: `default_model`, `strict_models`, `thinking`, `max_body_bytes`, `log_requests`,
  `log_file`, `debug` (alias `debug_log`/`verbose_log`).
- Danh sách sửa trong `config.json` (restart server để áp dụng): `models` (null = dùng
  `DEFAULT_MODELS` trong `models.py`; có giá trị hợp lệ → thay hẳn; dạng `["id"]` /
  `[{"id","upstream","desc"}]` / `{"id": {...}}`), `qwen_builtin_tools` (mặc định
  `["code_interpreter"]`; Python gửi cho extension qua `/status.builtin_tools`).
- Log theo phiên: `log_session_file` (mỗi lần bật server 1 file `<log_dir>/server-<ngày-giờ>-<pid>.log`,
  không giới hạn 100 dòng như `/debug`), `log_dir` (mặc định `logs`), `log_save_prompt_files`
  (chép file prompt vào `<log_dir>/prompts-<phiên>/`). Chỉ ghi khi `log_requests=true`.
  `attachment_usage_warn_ratio` (0.05, 0 = tắt warn): xem "Chẩn đoán attachment".

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
- Mode (chỉ khi `bridge_select_mode=true`, không phải đường mặc định): dropdown gần ô prompt; click
  `.qwen-chat-v2-dropdown-menu-select`, item `role="menuitem"` / `.qwen-chat-v2-dropdown-menu-item`.
  none/minimal/low → Fast, medium → Auto, high/xhigh/max → Thinking. `/poll` trả `mode`, vòng poll
  phải copy vào `job`. Mặc định mode đi qua body request, xem "Mode Auto/Thinking/Fast".
- `refreshModels()` đọc `/api/v2/models` trong tab (throttle 10 phút), gửi qua body `/poll`,
  merge vào `/v1/models`.
- Captcha: `waitCaptchaClear` chờ ở 3 chỗ (trước new-chat/reload, sau `waitPageReady`, lúc Send
  không bấm được). Selector hiện là **đoán** (`captchaSignal`: baxia/turnstile/URL
  `_____tmd_____`/text dialog) — cần DOM thật để chỉnh.
- Lỗi UI mới phải `warn`/log rồi chạy tiếp, không throw làm hỏng request.
- **Mode Auto/Thinking/Fast qua body request** (mặc định, không chạm dropdown, áp cho cả 3 mode):
  `reasoning_effort` (none/minimal/low → fast, medium → auto, high/xhigh/max → thinking) →
  `job.mode` → hook `fetch` (`rewriteRequestMode`) ghi đè `feature_config` (từng message, và top-level
  nếu có) của body completion theo `bridge_mode_feature_config[mode]`. Không gửi `reasoning_effort` →
  không ghi đè, body mang mode UI hiện tại. Mặc định (`DEFAULT_MODE_FEATURE_CONFIG` trong `config.py`)
  lấy từ 3 request completion thật của Qwen web (chỉ `messages[0].feature_config` khác nhau, không có
  `feature_config` top-level):

  | mode | thinking_enabled | auto_thinking | thinking_mode | thinking_format |
  | --- | --- | --- | --- | --- |
  | auto | true | true | `"Auto"` | `"summary"` |
  | thinking | true | false | `"Thinking"` | `"summary"` |
  | fast | false | false | `"Fast"` | `null` = xoá key (request Fast thật không có key này) |

  `output_schema="phase"`, `research_mode="normal"`, `auto_search=true` giống nhau ở cả 3 mode nên
  không ghi đè. Giá trị `null` = xoá key khỏi `feature_config`. `config.json` ghi đè **cả dict**
  (không merge theo mode): mode không khai báo / body không có `feature_config` → warn 1 lần/job,
  không ghi đè. Chỉ hook `fetch` (như force model), chưa hook XHR. Qwen đổi key → lấy lại từ dòng
  debug `completion request: ... mode=<mode> ... feature_config=<đầy đủ> top={...}`. Test:
  `TestForceModeConfig` (Python) + `rewriteRequestMode: ...` trong `test_bridge.js` (đối chiếu từng
  cặp trạng thái UI → mode với feature_config thật).

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

- Ngưỡng inline→file: `inline_prompt_max_chars` (mặc định 30000, trần cứng `MAX_PROMPT_CHARS`=130000) — prompt replay lớn (vd 47k sau undo) tách history sang file, không inline. Extension: `sendPrompt` có stall-detector cho job new/legacy prompt ≥ 20000 (composer rỗng / nút Send biến mất quá 8s không request → `learnInlineLimit` + throw sớm, retry đi đường attach file; không áp dụng continue/fork).
`prepare_large_prompt` cắt bằng `_render_fit` (`FILE_MAX_CHARS`=130000, giữ đầu + tin mới
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

#### Chẩn đoán attachment (cảnh báo; metadata lệch thì chặn theo `bridge_attachment_block`)

Khi `bridge_require_attachment=true`, request **có** attachment nhưng metadata lệch file proxy đã
tạo (tên / size / status đang upload / số file, hoặc file lấy từ `/file/` lệch bytes/sha256) bị
chặn + Python retry, thay vì chỉ cảnh báo. Guard chạy cả đường fetch lẫn XHR
(`strictProblemForBody`). Status lạ (`status_unknown`) và shape lạ chỉ cảnh báo vì key thật của Qwen
chưa xác nhận: dòng `file shape` (1 lần/job) in entry file thật để đối chiếu `describeFileEntry`.
Strategy `paste-text` bỏ qua so tên (Qwen tự đặt tên document). Hệ quả: nếu Qwen đổi tên/tính size
khác, bỏ kind tương ứng khỏi `bridge_attachment_block`.

Một job có file để lại các dòng log sau (grep `job <8 ký tự đầu>` để gom theo job):

1. Python `[attach] file ...`: chars/bytes/lines/sha256/truncated/layout; `file_info` mang thêm
   `bytes`, `sha256`, `lines`, `truncated` (debug thêm `head`/`tail`).
2. Extension `file prompt khop file Python` hoặc `FILE PROMPT LECH`: bytes + sha256 của nội dung
   extension lấy từ `/file/` so với file Python đã ghi. `composer da co attachment TRUOC...` =
   file cũ còn sót; `attachment cards sau '<strategy>'` = card DOM sau khi attach.
3. Extension `completion request: ... files=[tên|size|status|type] expect=<tên>/<bytes>B/sha256:..`:
   `FILE DINH KEM BAT THUONG` khi sai số lượng file, khác tên, khác kích thước, trạng thái chưa
   xong, hoặc job không có file mà request vẫn mang attachment. Cách đọc key file (`name`,
   `size`, `status`...) là đoán theo nhiều tên key — nếu Qwen dùng key khác thì `files=[?|?B|?]`;
   xem `file0={...}`.
4. Python `[attach-check]` khi job xong: `usage_in` (input_tokens) / chars = `tokens_per_char`,
   + 160 ký tự đầu câu trả lời. `attach-check WARN` khi tỉ lệ < `attachment_usage_warn_ratio` hoặc
   câu trả lời giống "không thấy file". Chưa xác nhận Qwen có tính token của file vào `usage`
   hay không → đối chiếu vài job thật trước khi tin warn tỉ lệ.

### Liveness / huỷ / retry

- Trước mỗi thao tác UI extension hỏi `GET /alive?job_id=<jid>` (`jobAlive(force=true)` bỏ cache
  1.5s). `POST /cancel {job_id, reason}` huỷ job; `/requeue` trả 409 khi job chết/quá TTL/quá
  `bridge_max_requeues`; `/event` cho job chết trả `{stale:true, cancelled}` → extension dừng UI
  và bỏ response cũ. `job.cancelled` được set khi stale hoặc quá `job.deadline`.
- `_watch_disconnect` + heartbeat SSE đánh dấu `gone` cho lỗi socket xác định (`_is_disconnect_error`).
  `bridge_cancel_on_disconnect` huỷ job non-stream. `tab_reloaded` → fail nhanh 502 + retry;
  `tab_reloading` chỉ warn. Job orphan (caller ngắt/TTL) bị `/poll` drop.
- Caller huỷ khi Qwen đang generate (client ngắt/abort): job `cancelled` + đã `attached` →
  `runJobInner` (finally) gọi `stopGenerationOnCancel` bấm nút Stop trên Qwen (tối đa 3 lần, đợi
  nút hiện tới ~1.5s). Ping 5s hỏi `/alive` cho job đang generate để không phải chờ delta kế tiếp
  (Qwen "nghĩ" lâu thì không có delta). Trước đây chỉ nhả reader → Qwen chạy tiếp đến hết và chỉ
  được dọn khi job sau `waitUiIdle` bấm Stop. Session mode: lượt bị huy đã gửi request nên session
  reset (`STORE.fail`), lượt sau replay đầy đủ.
- Tool drop (undeclared / `body is not valid JSON`) → `_run_turn` nudge retry kèm lý do + tool
  hợp lệ. Tool built-in của Qwen (config `qwen_builtin_tools`, mặc định `code_interpreter`) bị extension bỏ
  qua để Qwen tự chạy.

### Chế độ session (`bridge_session_mode`)

Mục tiêu: coi chat Qwen đang mở là history của client; mỗi lượt chỉ gửi phần **mới** (delta)
thay vì replay toàn bộ. Qwen dựng context phía server theo chuỗi `parent_id`, body completion
chỉ mang **1** message mới.

- `session.py` (thuần): `STORE.plan(messages, tools, tool_choice, tab, api)` → `Plan` kind
  `new` (replay đầy đủ bằng đường cũ + preamble giao thức session) | `continue` (delta) |
  `fork` (delta ở nhánh anh em). Khớp = `messages` của client bắt đầu bằng đúng chuỗi hash đã
  "gộp" vào chat (kể cả tin assistant vừa trả). `canon_message`: system/user/tool = role + nội
  dung (CRLF→LF, strip, bỏ `session_hash_ignore`); assistant có tool_calls = (id, name);
  assistant text thuần = chỉ đếm (Cline hay strip), `session_strict_assistant` so cả nội dung.
  `ctx_hash` = tool definitions.
- Reset (`new`, log `[session] reset ... reason=`): hash lệch (`history_diverged@i/n`), client
  retry/undo khi fork tắt, đổi tool, `api_changed`, quá `session_max_*`/`session_idle_ttl`, tab
  reload (`nonce` khác), tab sang chat khác, delta chứa tin assistant, request lạ xen giữa
  (title-gen... chạy ở chính tab này nên đằng nào chat cũng mất).
- Giao thức Python↔extension: job mang `session` (`mode`, `nonce`, `chat_id`, `tools_active`,
  `attach_prompt`, fork: `fork_parent_id`, `user_action`) + `tools_active` (delta không còn
  `# Tool Use` nên không đoán từ prompt được). Extension báo `session` event ngay khi request
  completion đi (kèm `nonce`, `chat_id`, `parent_id` đọc từ body thật) → Python coi là **đã gửi**:
  job continue/fork **không retry** sau đó (gửi lại = nhân đôi delta); lỗi → `STORE.fail` reset,
  client retry sẽ replay đầy đủ. `error` mang `session_lost:true` khi `verifySession` thất bại
  (`sessionProblem`: nonce, URL `/c/<id>`; temporary chat luôn `/c/local`).
  Job continue/fork **bỏ** `tryNewChat`/`ensureMode`/`waitPageReady` (không reload/đổi chat).
  `/poll` và ping mang `tab:{nonce,path,chat_id,busy}` → `bridge.tab_state()` (TTL
  `bridge_worker_ttl`); không có `nonce` (extension cũ) → không bao giờ tạo session.
- Delta: tool result bọc `=== RESULT tool=<name> id=<id> ===` … `=== END RESULT <id> ===`
  (thiếu result → `MISSING`) + dòng "tiếp tục, đừng lặp lại call"; tin user gửi nguyên văn
  (mỗi `session_reminder_every` lượt thêm 1 dòng nhắc format). Delta > `session_file_threshold`
  → file đính kèm với prompt riêng (`tool_results_file`/`message_file`); nếu extension tự chuyển
  inline → file thì dùng `attach_prompt` (`attach_results`/`attach_fallback`), **không** dùng
  `attachment_prompt` ("compressed conversation history" sai ngữ cảnh).
- Nudge trong session (`_run_turn` lần 2+): `append` chỉ gửi `[Format error] …` vào chat (không
  kèm file); `fork` (cần `session_fork`) gửi lại delta ở nhánh anh em. Chat Qwen chứa cả reply hỏng
  + nudge nhưng hash client chỉ có reply cuối → vẫn khớp.
- Fork (`session_fork`, thử nghiệm): extension ghi đè `parent_id` ở 4 chỗ (top-level
  `parent_id`/`parentId`, `messages[].parent_id`/`parentId`) + `user_action` (hook fetch, như
  `rewriteRequestMode`; XHR không hỗ trợ → lỗi). Dùng cho client retry cùng request, nudge,
  client undo/edit (rewind về checkpoint `(n, parent_id)` có prefix khớp dài nhất).
  Capture thật: sửa tin = completion thường với `user_action:"edit"` và `parent_id` của tin gốc;
  context server theo chuỗi `parent_id` nên nhánh cũ bị bỏ (test ALPHA/BRAVO/CHARLIE).

### Tool protocol

Fence ` ```tool_call `, chuẩn `{"name": "...", "arguments": {...}}`. Parser tự sửa shorthand,
single-key dict, trailing comma, single quote; nhận thêm XML tag + JSON và XML
`<function=...><parameter=...>` (không nhận `<function=...>` standalone → nudge retry). Prose đi
cùng tool_call được giữ ở `content`. `tool_reject_bad_arguments=true` (mặc định) từ chối
arguments sai kiểu thay vì biến thành `{}`. Chi tiết lịch sử ở `CHANGELOG.md`.

## Điểm dễ vỡ

- Session: `parent_id` của lượt sau một fork do **app** tự tính (từ id assistant trong SSE) — chưa
  chứng minh trên máy thật; `user_action:"edit"` có thật sự cần không cũng chưa rõ. Context
  của một chat dài có giới hạn thật chưa đo (`session_max_chars` là số đoán). Tin
  `Tool X does not exist` / reply sai format nằm lại trong chat (không tránh được). Harness đổi
  system/env mỗi lượt → `history_diverged@0` liên tục (xem log `[session] reset`; thêm regex vào
  `session_hash_ignore`). Selector/URL `/c/<id>` của app là capture, đổi → `session_lost`.
- `extension/bridge.js` hardcode bridge port 8083.
- DOM heuristics chưa có test UI thật: model picker, mode dropdown, captcha selector, nút Send.
  Qwen đổi layout → sửa `findModelTrigger` / `selectModel` / `findModeTrigger` / `captchaSignal`.
- `bridge_mode_feature_config` mặc định là tên key `feature_config` của Qwen web lúc capture.
  Qwen đổi tên/giá trị key → mode không còn đổi đúng (request vẫn chạy, nhưng với mode UI
  hiện tại); `bridge_require_mode` không bắt được lỗi này khi `bridge_select_mode=false`.
- Built-in tool của Qwen lấy từ config `qwen_builtin_tools`; Qwen thêm built-in mà không cập
  nhật config → bị nudge như tool lạ. Extension chỉ nhận danh sách mới sau khi Python restart.
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
- Test Python: `test_qwen_web2api.py` (unittest, pytest collect được). Test extension: `test_bridge.js` (Node `node:test`, không dependency): `bridge.js` là IIFE nên test cắt thân hàm theo thụt lề 2 ký tự (`  function ten(` … dòng `  }`) và dựng lại bằng `new Function` với stub; hàm mới cần test thì thêm vào `loadFns([...], ctx)`, thiếu stub → ReferenceError. Không thay test UI thật (selector DOM). Không network/browser/
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
