# CHANGELOG — lịch sử thay đổi

File này ghi **lịch sử**: vấn đề từng gặp, nguyên nhân và hướng sửa. Để nắm cấu trúc/hành vi
hiện tại thì đọc `AGENTS.md`.

> Các tên như `qwen_fix_patchN`, `qwen_findings_patchN`, `finding N`, `N1/N2` là nhãn của
> các script tạm dùng lúc áp patch (ví dụ `qwen_findings_patch2.py`). Chúng **không phải file
> nguồn** và đã bị dọn khỏi comment trong source. Giữ lại ở đây chỉ để đối chiếu lịch sử.

## Nền tảng

- Bỏ hoàn toàn đường Python gọi trực tiếp `chat.qwen.ai` (`curl_cffi`/request, token) —
  chuyển sang **bridge-only**: extension trong tab đã đăng nhập làm mọi thao tác UI, Python
  chỉ xếp job qua server 8083 và stream kết quả về client.
- Tách logger dùng chung (`QwenError`, `log`, `debug_log`, `is_debug`, `get_recent_log`) vào
  `qwen_web2api/qwen.py`; `server.py` và `bridge.py` không còn transport/token riêng.
- Thêm `/v1/responses` cạnh `/v1/chat/completions` và `/v1/models`. `/v1/models` merge danh
  sách thật từ `/api/v2/models` do extension gửi qua body `/poll` (`bridge.known_models()`).

## Temporary chat & model picker (`qwen_fix_patch3`, `qwen_fix_patch6`)

- `temporary_chat`: mọi job chạy ở `/?temporary-chat=true`. Chấp nhận cả spelling
  `temporary-chat` (hyphen) trong config file.
- `temporary_chat_reload`: temporary chat không có nút New Chat. Khi trang đã có tin nhắn,
  extension `/requeue` job rồi reload `/?temporary-chat=true` để tránh lẫn context. Config
  `false` → chỉ `warn`. Idle navigation được cap 2 lần/60s qua `sessionStorage`.
- `isTemporaryPage(strict)`: chế độ thường chỉ tin URL/cache (nút toggle "temporary chat"
  luôn hiện trong DOM → false positive gây `location.assign("/")` lặp). Chế độ temporary
  dùng heuristic lỏng vì app có thể tự xoá param.
- Picker `.wms-*` có popup chính (3 model) + "Expand more models" mở
  `.wms-popup--secondary`. `pickWmsModel` khớp tên ở `.wms-list__name-text`, hover+click
  `.wms-more-entry` nếu không thấy. `closeModelPopup` được gọi khi chọn lỗi vì popup sót
  có thể chặn nút Send. Id model trong popup phụ hiện vẫn suy ra từ nhãn UI (chưa đối chiếu
  `/api/v2/models`).
- `findModelTrigger` không còn chọn nhầm nút profile ("QwenUser… Free"); `selectModel`
  đọc text nhìn thấy (`triggerLabel`) thay vì chỉ aria-label.
- Cache `pageLoadedAsTemporary` không reset mù quáng: reset khi URL mất param có thể gây
  reload lặp nếu Qwen tự xoá param.

## Chế độ Thinking/Auto/Fast (`qwen_fix_patch7`)

- `bridge_select_mode`: chọn mode theo `reasoning_effort` (none/minimal/low → Fast,
  medium → Auto, high/xhigh/max → Thinking). `model@think=1/0` được dùng khi không có effort.
- Từng có bug: `/poll` trả `mode` nhưng vòng poll extension không copy vào `job`, nên
  `selectThinkingMode` im lặng bỏ qua (log không có dòng mode nào). Giá trị được copy.
- Nút mở dropdown thật là `.qwen-chat-v2-dropdown-menu-select` (sâu hơn trong
  `.qwen-thinking-selector`); wrapper `.qwen-thinking-selector` từng bị heuristic chọn nhầm
  vì class chứa chuỗi `select`. Menu item được poll tới ~2.5s mới báo lỗi.
- `bridge_force_request_model` (thử nghiệm): ghi đè `model`/`models` trong body completion.

## File prompt & attach (`qwen_fix_patch2`, `qwen_fix_patch4`, `qwen_fix_patch5`, `finding 5`)

- `prepare_large_prompt` cắt history/file đầy đủ bằng `_render_fit` (`FILE_MAX_CHARS`=130000,
  giữ đầu + tin mới nhất, chèn `[...truncated...]`); `messages_to_prompt(max_bytes=...)`
  ép trần byte. CRLF được chuẩn hoá về LF (Windows text mode từng ghi `\r\r\n` làm phình file).
- Khi có history và lượt cuối là user (<= 130000 ký tự), lượt cuối đó (kèm tool definitions /
  response_format nếu có) được render làm `attachment_prompt` điền vào composer;
  `attachment_prompt` cấu hình là fallback. Khi toàn bộ prompt nằm trong file
  (không có `file_info.attachment_prompt`), composer = config `attachment_prompt` +
  `file_info.extra_instructions` + `file_info.retry_nudge`, ghép bởi `bridge._file_companion`.
- Qwen đổi text dài thành document card làm composer rỗng → request chỉ mang
  `attachment_prompt` (`content_chars=119 attachments=[]`) và model trả "không thấy tài liệu".
  `submitPrompt` gửi debug `inline prompt: len/composerAfter/attachmentNodes/sendBtn`; composer
  rỗng sau khi điền > 2000 ký tự → `learnInlineLimit` (`qwenUIBridgeInlineLimit`, trần 32000)
  + ném lỗi để retry. Hook `fetch` chặn request có `content_chars` < 90% prompt
  (`checkInlineIntegrity`). Prompt >= `inlineMaxChars` tự đi đường attach file.
- Giả định cũ "Qwen chỉ chuyển paste thành file ở <= 131072 ký tự" bị A/B phủ định: ở file
  ~320k ký tự, `paste-file` và `drop-file` đều tạo attachment card trong ~1–2s, cả hai đều
  cho `files=1` với `status=uploaded`. Đoạn 5–13s tới `completion request` là Qwen tự upload
  lên OSS, không phải chi phí của strategy. Vì vậy **bỏ hẳn nhánh `PASTE_FILE_MAX`**; nếu attach
  lỗi sau này thì chỉ đổi thứ tự mảng `strategies`.
- Thứ tự cuối cùng: `paste-file → drop-file → paste-text → file-input`. Hai cái đầu có tỉ lệ
  thành công cao nhất; `paste-text`/`file-input` để cuối vì dễ tạo request sai.
  `paste-file`/`drop-file` dùng chung một `File` **trong memory** (`makeTextFile`) — chỉ khác
  event: `ClipboardEvent("paste")` với `clipboardData.files` vs `DragEvent`
  `dragenter/dragover/drop` vào `composer.closest("form") || composer`. File tạm thật trên đĩa
  là `qwen_prompt_*.txt` do Python tạo, bridge serve qua `/file/<filename>`.
- `file_prompt_truncate=false` (mặc định) → KHÔNG cắt file prompt khi vượt trần paste, tránh
  mất evidence ở cuối log; `true` → cắt theo cơ chế cũ.
- `prompt_hard_max_chars` (mặc định `0` = không giới hạn): vượt → HTTP 413, không cắt im lặng.
- Job có file dùng `SEND_TIMEOUT_FILE_MS` (240s). `waitPageReady` chờ model picker + 1s ở job
  đầu tiên sau mỗi lần load trang.

## Chẩn đoán (`qwen_fix_patch5`, `qwen_fix_patch6`, `bridge_fix_patch`)

- Hook cả `window.fetch` lẫn `XMLHttpRequest` để bắt SSE khi app đổi transport.
  `inspectCompletionRequest` đọc body completion của chính app (`content_chars`, `files`,
  `feature_config`, `file0={...}`) — bằng chứng chính xác file có tới Qwen hay không; heuristic
  DOM `findActiveAttachmentElements` có thể báo sai nên chỉ warn 1 lần/job.
- Log `completion request:` có `model=` (model thật Qwen nhận), `want=`, `vis=`,
  `feature_config=`; model request khác model job → warn `MODEL KHAC YEU CAU`.
- Debug event `temporary evidence: {url,dom,text,cached}` để thấy cache temporary lệch
  URL/DOM. Poll rỗng không ghi log nữa (mỗi 20s một dòng làm đầy 100 dòng `recent_log`).
- `reloadFreshTemporary` kiểm tra `/requeue` trả `{ok:true}` — hàm `post()` không check HTTP
  status nên từng reload dù requeue lỗi, làm mất job.
- `waitCaptchaClear` (`captchaSignal`: selector captcha/baxia/turnstile, URL `_____tmd_____`,
  text trong dialog) chờ người dùng giải ở 3 chỗ: trước new-chat/reload, sau `waitPageReady`,
  lúc Send không bấm được. **Selector vẫn là đoán**, cần DOM thật để chỉnh. `pageDiag` thêm
  trạng thái UI vào warn Send/timeout.

## Liveness, huỷ và strict (`qwen_findings_patch1`, `qwen_findings_patch2`, finding 1–9)

- Trước mỗi thao tác UI, extension gọi `GET /alive?job_id=<jid>` → `{alive, cancelled}`.
  `jobAlive(job, force)` / `ensureAlive(job, stage, force)`: `force=true` bỏ qua cache 1.5s.
  `submitPrompt`, `sendPrompt`, `attachLongPrompt` hỏi proxy thật ngay trước thao tác sau `sleep`.
- `POST /cancel {job_id, reason}` huỷ job xuyên suốt. `/requeue` trả 409
  `{ok:false, cancelled:true}` khi job đã huỷ / hết caller / vượt `bridge_max_requeues` / quá TTL.
  `/event` cho job chết trả `{stale:true, cancelled}` → extension dừng UI và bỏ response cũ.
- `_watch_disconnect` và heartbeat SSE đánh dấu `gone` khi gặp lỗi socket xác định mất kết nối
  (`ConnectionError`, `ValueError` socket đóng, `OSError` errno reset/abort/pipe/notsock; helper
  `_is_disconnect_error`). Trước đây watcher thoát im lặng nên job cũ vẫn chạy. `bridge_cancel_on_disconnect`
  huỷ job non-stream khi client ngắt.
- `waitCaptchaClear` kiểm tra `jobAlive(job, true)` mỗi vòng 1s; job huỷ → `settle` + thoát ngay,
  không giữ worker tới `captcha_wait_seconds`.
- `bridge_require_model` / `bridge_require_mode`: chọn UI thất bại → huỷ job + retry thay vì gửi
  với model/mode cũ. `strictRequestProblem` chặn request có `model != job.model`;
  `selectThinkingMode` thất bại khi không đọc được nhãn nút sau khi chọn.
- `bridge_require_attachment`: hook fetch chặn request thiếu attachment trừ khi
  `content_chars >= 95% file_info.size`.
- `tool_reject_bad_arguments`: arguments sai kiểu (list/số/bool) bị từ chối với lý do
  `unusable arguments (arguments must be an object, got list)` trước đường tắt
  `{"arguments": ...}`; `false` giữ hành vi cũ.
- Extension tách native call theo `id`/`index` hoặc ranh giới JSON hoàn chỉnh — trước đây gộp
  2 call cùng tên thành 1 chuỗi hỏng.
- XHR chỉ có handler `load` → job treo vĩnh viễn, `active` không giảm, worker không nhận job mới.
  Thêm `error`/`abort`/`timeout`/`loadend` + watchdog timer. Navigation watchdog: cờ
  `navigating=true` không có timeout → poll `continue` mãi; giờ tự bỏ sau 20s. Reader leak:
  `reader.cancel()` ngay trong idle timer và trong `finally`.
- `tab_reloaded`: extension báo qua `/event` (kèm `beforeunload` + `navigator.sendBeacon`);
  Python `generate_stream` fail nhanh (502 → retry). `tab_reloading` chỉ là cảnh báo.
- Orphan job: `/poll` drop job hết TTL hoặc có `managed=True` nhưng caller đã ngắt.

## Tool built-in & retry khi drop (`qwen_fix_patch8`)

- Native `function_call` của tool built-in Qwen (`code_interpreter`, chạy trong sandbox Qwen)
  không bị chặn; extension bỏ qua để Qwen UI tự chạy rồi stream tiếp, gửi debug
  `de Qwen tu chay built-in tool: ...` một lần. Danh sách cứng `QWEN_BUILTIN_TOOLS` trong
  `extension/bridge.js` cần cập nhật khi Qwen thêm built-in.
- Ở `_run_turn`, `parse_tool_calls` trả `dropped` (undeclared tool, `body is not valid JSON`)
  → nudge retry kèm lý do + danh sách tool hợp lệ. Trước đây raw fence còn trong text làm
  `looks_like_missed_tool_call` trả `False` nên block bị trả nguyên thành text.

## Tool_call parser

- Fence ` ```tool_call ` với chuẩn OpenAI `{"name": "...", "arguments": {...}}`; tự sửa shorthand
  `{"<tool_name>", "arguments": ...}`, single-key dict, trailing comma, single quote.
- XML tag + JSON: `<tool_call>{"name":...}</tool_call>` (nhiều object/tag). Parser dùng `raw_decode`
  trên **full text** (không slice) để `</tool_call>` trong chuỗi không làm sai biên.
- XML function/parameter (Qwen-Agent):
  `<tool_call><function=name><parameter=k>v</parameter></function></tool_call>`; tự detect kiểu
  số/bool/list/dict JSON. `<function=...>` standalone không được parse →
  `looks_like_missed_tool_call` trả `True` để nudge retry.
- Khi response có cả prose và tool_call, prose được giữ ở `content`.

## Config hoá danh sách + chẩn đoán attachment + log theo phiên

- `models` và `qwen_builtin_tools` chuyển ra `config.json`. `models.MODELS` giờ là mapping đọc
  live từ `CONFIG` (config được load sau khi import nên dict tĩnh bị cũ); `DEFAULT_MODELS` là
  mặc định khi key trống/sai. `QWEN_BUILTIN_TOOLS` hardcode trong `extension/bridge.js` thành
  `qwenBuiltinTools` (mặc định `code_interpreter`), cập nhật từ `/status.builtin_tools`.
- Lý do: `bridge_require_attachment` không có ca test thật nên không biết khi nào xảy ra. Log cũ
  chỉ cho biết request có `files` hay không, không trả lời được: đính nhầm file? file thiếu nội
  dung? Qwen có đọc không? File tạm bị xoá sau mỗi lượt nên không đối chiếu được sau đó.
  Thêm: dấu vân tay file (`bytes`/`sha256`/`lines`), extension đối chiếu nội dung lấy từ
  `/file/`, so tên/kích thước/trạng thái file trong body completion với file đã tạo, cảnh báo
  attachment sót khi job không có file, `[attach-check]` (usage.input_tokens / ký tự file + đầu
  câu trả lời, bắt câu kiểu "không thấy file"). Tất cả chỉ cảnh báo, không chặn.
- `log_session_file`: mỗi lần bật server một file log riêng (`/debug` chỉ giữ 100 dòng).
  `log_save_prompt_files` giữ bản sao file prompt để so với dòng `[attach]`. Các dòng
  `extension:` / `extension warn:` mang tiền tố `[job <8 ký tự>]`.

## Findings: huỷ lúc attach + arguments chuỗi rỗng

- `attachLongPrompt`: job bị huỷ trong lúc chờ strategy attach cuối vẫn `clearComposer` rồi điền lại text thô (~100000 ký tự). Giờ kiểm tra `jobAlive(job, true)` trước `clearComposer` và trước nhánh fallback điền text.
- `_extract_args`: `arguments: ""` ở tool có tham số optional rơi vào nhánh viết tắt thành `{"arguments": ""}` rồi trả thẳng cho client, không retry. Giờ chuỗi rỗng được xử lý trước nhánh viết tắt (wrapper không thành tham số): tool có schema bị từ chối `unusable arguments (empty string)` + nudge retry, tool không có tham số cho `{}`; `tool_reject_bad_arguments=false` giữ hành vi cũ (`{}`).

## Test tự động cho extension

- `test_bridge.js` (Node `node:test`, không dependency) + `TestBridgeJs` trong test Python. Test cắt nguyên văn thân hàm từ `extension/bridge.js` và chạy với stub DOM/mạng/sleep. Khoá luồng điều khiển: job huỷ giữa chừng khi attach (finding clearComposer/điền lại text), thứ tự strategy, fallback text thô, ném lỗi khi > 125000, `jobAlive`, đối chiếu file (`verifyPromptFile`, `checkRequestFiles`). Hình dạng file trong request là giả định, sửa test khi có capture thật.

## Ghi đè mode trong request (`bridge_force_request_mode`)

- Giống `bridge_force_request_model` nhưng cho Thinking/Auto/Fast: `rewriteRequestMode` ghi đè `feature_config` theo `bridge_mode_feature_config[mode]` (người dùng khai báo từ capture thật, không có default vì chưa xác nhận tên key). Lý do: dropdown UI treo khi tab không active, trong khi sửa body không cần chạm UI.
- Dòng `completion request:` giờ in `mode=`, `feature_config` đầy đủ (≤1500 ký tự, kèm `top_feature_config` nếu có) và `top={scalar top-level}` để lấy mẫu từng mode.

## Mode Auto/Thinking/Fast qua body request (mặc định)

- Có 3 request completion thật của Qwen web (một cho mỗi mode). Chỉ `messages[0].feature_config` khác
  nhau, không có `feature_config` top-level. Auto: `thinking_enabled=true, auto_thinking=true,
  thinking_mode="Auto", thinking_format="summary"`; Thinking: `thinking_enabled=true,
  auto_thinking=false, thinking_mode="Thinking", thinking_format="summary"`; Fast:
  `thinking_enabled=false, auto_thinking=false, thinking_mode="Fast"` và **không có** `thinking_format`.
  `output_schema="phase"`, `research_mode="normal"`, `auto_search=true` giống nhau ở cả 3.
- Mapping đó thành default (`DEFAULT_MODE_FEATURE_CONFIG`). `bridge_force_request_mode` mặc định `true`,
  `bridge_select_mode` mặc định `false`: chọn mode chỉ cần `reasoning_effort` (low/none → Fast, medium →
  Auto, high → Thinking), proxy ghi thẳng vào body, không bấm dropdown nữa. Áp cho cả 3 mode, không riêng
  Thinking. Lý do: dropdown treo khi tab không active, sửa body thì không phụ thuộc UI. Không gửi
  `reasoning_effort` → body giữ mode UI hiện tại.
- Giá trị `null` trong `bridge_mode_feature_config` = xoá key. Lý do: UI đang ở Thinking/Auto mà job cần Fast,
  nếu chỉ ghi đè key có sẵn thì body vẫn mang `thinking_format:"summary"`, khác request Fast thật.
- `bridge_require_mode` chỉ kiểm tra lúc bấm dropdown, nên không còn tác dụng khi `bridge_select_mode=false`.
  Muốn quay về dropdown: `bridge_select_mode=true`, `bridge_force_request_mode=false`.

## Attachment guard: chặn metadata lệch, thêm XHR (`qwen_findings_patch_attach`)

- Vấn đề: `checkRequestFiles` / `verifyPromptFile` chỉ cảnh báo, request vẫn gửi dù sai tên, sai
  size, file còn uploading hay nội dung lấy từ bridge lệch sha256; `strictRequestProblem` chỉ chặn
  khi attachments=0. Đường XHR không có guard nào. Key file của Qwen vẫn là đoán.
- Sửa: `classifyRequestFiles` trả `{kind,msg}`; `strictProblemForBody` (sync) chặn kind nằm trong
  `bridge_attachment_block` (mặc định name/size/status/count/content); `verifyPromptFile` trả
  boolean và `attachLongPrompt` ném lỗi khi lệch (Python retry); XHR gọi cùng guard. `status`
  chỉ chặn nhóm pending/uploading, status lạ chỉ cảnh báo; `paste-text` bỏ qua so tên.
- Thêm dòng debug `file shape` + cảnh báo `FILE SHAPE/DOM` để đối chiếu key/card thật.
- Còn lại (không sửa được từ client): chưa chứng minh Qwen đọc đủ nội dung; chỉ có heuristic
  `attach-check` (usage input_tokens / chars). XHR chưa có `rewriteRequestModel/Mode`.

## Chế độ session: gửi tiếp vào chat hiện hành (`bridge_session_mode`)

- Vấn đề: mỗi request OpenAI mang cả history nên proxy mở chat mới và replay toàn bộ (tốn
  token/thời gian, file prompt lớn). Capture thật cho thấy Qwen dựng context phía server theo
  chuỗi `parent_id`; body completion chỉ có 1 message mới (turn 2: `parent_id` = id assistant
  trước, lặp ở 4 chỗ; temporary chat dùng cùng shape, URL luôn `/c/local`). Sửa tin = completion
  thường với `user_action:"edit"` + `parent_id` của tin gốc, nhánh cũ bị bỏ khỏi context.
- Sửa: `qwen_web2api/session.py` (plan/commit/fail theo hash chuỗi history, prompt delta cho
  tool result/tin user) nối vào `server.py` (`_session_plan`, `_compose_prompt`, `_run_turn(plan)`,
  commit sau mỗi lượt) và `bridge.py` (`session`/`tools_active` trong job, event `session`,
  `session_lost`, `tab_state()`; job continue/fork không retry sau khi đã gửi). Extension:
  `PAGE_NONCE`, `tabInfo` trong `/poll` + ping, `sessionProblem`, bỏ new-chat/reload cho job
  trong chat, `announceSession`, `rewriteRequestSession` (fork). Mặc định tắt.
- `tools_active` truyền tường minh: trước đây suy từ `"# Tool Use"` trong prompt (bridge.py +
  extension `toolsActive`) — delta không có tool block nên native `tool_call` bị nuốt.
- Hash bỏ `session_hash_ignore` (mặc định `<environment_details>`) để phần động của Cline không
  làm lệch history; `attach_prompt` riêng cho delta chuyển file (không dùng `attachment_prompt`).
- Còn lại: request lạ xen giữa (title-gen) vẫn reset session vì chạy chính trong tab này; tiếp tục
  chat cũ bằng `/c/<chat_id>` chưa làm; fork chưa kiểm chứng trên máy thật.
- Client interrupt giữa chừng (vd bấm interrupt rồi gõ "tiếp đi") trước đây reset session → reload
  trang + replay (log `reset ... turn_failed: client disconnected`, rồi `plan new reason=no_session`).
  Giờ `fail()` giữ session với hash đặc biệt `INTERRUPTED` ở cuối: luot sau (hai tin user liền
  nhau, hoặc client giữ lại bản assistant dở) chỉ gửi phần mới (`why=after_interrupt`). `history_diverged`
  / `retry_or_rewind` ghi thêm `client[i]=... session[i]=...` để biết tin nào lệch.
- Lượt `new` (replay đầy đủ) giờ tính độ dài prompt đã gửi vào `chars_sent` (trước đây chỉ tính
  câu trả lời → log `chars=0`, `session_max_chars` hụt đúng bằng prompt đầu).

Session patch 3: hydrate wait thật + chẩn đoán Send + ngưỡng inline→file (normal mode)
Vấn đề (log server-20261120-150311-6156.log): undo lượt chat khi `session_fork=false` →
`history_diverged@11/17` → plan `new` → replay 47043 ký tự inline. Composer nhận đủ text
(`composerAfter=47043 sendBtn=true`) nhưng 60s sau `composer=0 sendBtn=false stopBtn=false`,
KHÔNG có dòng `completion request:` / `session event:` ⇒ app không phát request completions
(attempt 3 cùng tab cùng hook lại bắt được ⇒ không phải hook hụt). Mỗi attempt cháy 60s vòng
Send + 90s `SEND_TIMEOUT_MS`, ×3 ≈ 213s.
Manh mối quyết định: `temporary evidence dom=false` ở ĐÚNG 2 job fail; 8 job thành công đều
`dom=true`. `waitPageReady` có `selectModelEnabled === false || findModelTrigger(...)` → khi
`bridge_select_model=false` vòng lặp break NGAY, không kiểm tra gì, chỉ `sleep(1000)`; log
`modelPicker=true after ~1000ms` KHÔNG chứng minh trang đã hydrate. Nghi ngờ: composer nhận
`insertText` rồi bị re-render xoá mất / handler Send chưa gắn.
Sửa: (1) `waitPageReady` đợi composer + temporary DOM (bounded `TEMP_DOM_WAIT_MS`=3s, tổng
trần `PAGE_READY_MAX_MS`=8s) bất kể `select_model`, warn khi vẫn chưa hydrate. (2) `sendDiag`
+ `job._sendTrace`: clicks/enters, `composerLens` theo thời gian, `lenAtClick`, `emptyAt`,
attachment card, `chatMsgs` (bubble), `tempDom` — gắn vào warn 60s, lỗi send-timeout 90s và
lỗi stall; phân biệt "text bị xoá trước click" vs "click rồi app dọn composer".
(3) `inline_prompt_max_chars` (default 30000, 0 = trần cũ 130000, không vượt `MAX_PROMPT_CHARS`)
dùng cho cả quyết định inline lẫn điều kiện tách companion; stall-detector 8s cho job
new/legacy prompt ≥ 20000 → `learnInlineLimit` + throw sớm, retry đi đường attach file.
Không áp dụng continue/fork (retry có thể nhân đôi delta). Không đụng logic fork.
Đường file đã ổn định 2/2 lần trong chính log này (34919 & 46176 chars, paste-file ~2s,
`status=uploaded`, usage_in 25179/28470). Test: `TestInlinePromptThreshold` + 11 test Node.

Interrupt ở client không dừng generate trên Qwen
Vấn đề: ngắt request ở client (OpenCode) khi model đang chạy → Python huỷ job (`/alive` false,
event `stale`) nhưng extension chỉ nhả reader (`pumpFetch`), không bấm Stop → Qwen chạy tiếp đến hết;
trên chat session thành tin assistant thừa, chỉ được dọn khi job kế `waitUiIdle` bấm Stop.
Sửa: `stopGenerationOnCancel(job)` trong `finally` của `runJobInner` (chỉ job `cancelled` và đã
`attached`; tối đa 3 lần bấm, đợi nút Stop hiện); ping 5s gọi `jobAlive(current, true)` cho job
đang generate để huỷ không phải chờ delta kế tiếp. Test Node: 5 test `stopGenerationOnCancel`.
Kèm: test `sendPromptEnv` của session_patch3 thiếu `sendDiag` trong `loadFns` → 2 test Node fail
(`ReferenceError: sendDiag is not defined`); đã thêm `sendDiag`.

## Session: companion của lượt `new` + ngưỡng inline học được chảy về Python (F1/F2)

**F1 — lượt `new` dùng nhầm câu companion của config.** `Plan.job_session()` chỉ gắn
`attach_prompt` cho `continue`/`fork`. Khi extension tự chuyển inline → file (ngưỡng đã học),
`job.attachment_prompt` rỗng (Python không tạo file) → rơi xuống `attachment_prompt` của config
("I have attached a file containing our **compressed conversation history**… CONTINUE the work
from where it left off"). Log `server-20261120-200441-816.log`: 3 job `new` đều
`content_chars=228 companion_len=228`; tin user cuối là "Thanks" nhưng model trả
"Tôi đã đọc toàn bộ file lịch sử hội thoại. Tóm tắt những gì đã làm: …" (usage_in=43731 ⇒ file
**có** tới Qwen, `status=uploaded`, `parse_status=success` — không phải lỗi mất file).
Sửa: thêm `DEFAULT_PROMPTS["attach_full"]` (file chứa TOÀN BỘ hội thoại, kể cả system/tool
definitions, và **tin cuối là tin cần trả lời**) + `job_session()` trả nó cho `KIND_NEW`.
Đường Python whole-file (không split được) cũng dính cùng lỗi: `_compose_prompt` giờ đặt
`file_info["companion_prompt"]` cho job session và `_file_companion` ưu tiên nó hơn config.

**F2 — ngưỡng inline "đã học" chỉ nằm trong tab.** `learnInlineLimit` ghi `sessionStorage` +
console, không báo Python; `server._inline_max_chars()` chỉ đọc config. Với
`inline_prompt_max_chars=0` Python nghĩ ngưỡng = 130000 nên **không** split history/last-message,
không `[attach]` fingerprint, không prompt copy, không `[attach-check]`; extension nhận cả
prompt (kể cả tin user cuối) vào 1 file. `file_info` extension tự dựng cũng thiếu `bytes` nên
`classifyRequestFiles` bỏ qua so size (`Number(exp.bytes)||0`) → guard `size` vô hiệu
(log `expect=…txt/?B/sha256:?`).
Sửa: (1) extension gửi `inline_limit` trong ping 5s + body `/poll`, **và gửi ngay** khi học
(`/event` `kind:"ping"` — không sinh log orphan); `bridge._remember_inline_limit` chỉ nhận
1000..1000000 và **chỉ giảm**; `/status.inline_limit` trả lại để tab mới adopt;
`_inline_max_chars() = min(config, learned)`, tắt bằng `inline_prompt_use_learned=false`.
(2) Khối inline→file tách thành `maybeInlineToFile(job)` (test được), `file_info` mang thêm
`bytes`/`sha256` từ `fingerprintText()`; debug in rõ `companion=python|session|config`.

**Giới hạn đã biết:** `prompt`/`file_info` được tính **một lần** trong `_compose_prompt` trước
`_run_turn`, nên các attempt retry *cùng lượt* vẫn dùng prompt chưa split (extension tự chuyển).
Ngưỡng mới có tác dụng từ **request kế tiếp**. Trong log này 2/3 lượt replay lớn (95994 và
117870 ký tự) sẽ được Python split với companion thật.

Test: `TestNewPlanAttachPrompt` + `TestNewPlanAttachPromptHttp` (test_session.py),
`TestLearnedInlineLimit` + `TestInlineLimitReporting` (test_qwen_web2api.py),
6 test Node (`adoptInlineLimit`, `learnInlineLimit`, `maybeInlineToFile` ×3, guard size).

Đo được hydrate + A/B được `waitPageReady` (không đổi hành vi mặc định)

Vấn đề: log `page ready: ... after Xms` gộp cả vòng đợi và `sleep(1000)` cố định nên không
trả lời được "vòng đợi có thực sự phải chờ không". Log `server-20261120-231423-14420.log` có
2 lần gọi: job `dc010a1f` (tab mở sẵn) `after 1015ms` ⇒ loop ~15ms; job `4233de7a` (ngay sau
tab reload 23:19:20) `after 2916ms` ⇒ loop ~1916ms, thoát do điều kiện đúng chứ không phải
timeout (`1916 < TEMP_DOM_WAIT_MS`). Tức hydrate trễ thật ~1.9s, nhưng chỉ 1 mẫu, và cả log
chỉ có 2 lần gọi vì `waitPageReady` bị chặn bởi `pageUsed` + chỉ chạy ở nhánh new/legacy.

Thêm (mặc định giữ nguyên hành vi):
- `hydrationProbe()`: mỗi lần load trang lấy mẫu composer / model picker / temporary DOM /
  React props (`__reactProps$…` trên node composer) mỗi 200ms trong 12s, gửi 1 event
  `kind:"probe", job_id:"none"` kèm object `probe`. Read-only, không phụ thuộc job → đo được
  phân bố hydrate kể cả khi không có request nào tới sớm.
- `waitPageReady` log tách `loop=Xms/N rounds sleep=Yms total=Zms`.
- `page_ready_mode` = `full` (mặc định) | `sleep_only` (hành vi cũ trước session_patch3) |
  `off`; qua `/status`, giá trị lạ → `full`. Dùng để A/B: tắt `bridge_session_mode` +
  `temporary_chat` + `temporary_chat_reload` ⇒ mỗi request = reload + job new = một ca hydrate.
- Gate đổi từ `pageUsed` sang `pageSent` (set khi request completion thật sự đi, cả fetch lẫn
  XHR). Trước đây job đầu fail vì chưa hydrate thì attempt 2/3 trên cùng trang không được đợi
  nữa — đúng lúc cần nhất lại tắt. `pageDirty()`/`tryNewChat` vẫn dùng `pageUsed` như cũ.
- `/event` orphan: `kind:"probe"` ghi nhãn `extension probe:` (trước mọi kind ≠ ping đều là
  `extension warn:`); `ping` vẫn im lặng.

Test: `TestPageReadyMode` (Python) + 4 test `waitPageReady` mới (3 chế độ, gate pageSent,
log tách) + 3 test `hydrationProbe`; `pageReadyEnv` cập nhật theo ctx mới.


## Job dài bị extension giết giữa chừng + job mất khi tab reload lúc long-poll
Vấn đề 1: `bridge.generate_stream` gửi `deadline = ts + bridge_job_timeout` (mốc tuyệt đối
kể từ lúc XẾP job), trong khi vòng lặp Python lại reset deadline theo MỖI event
(`bridge_job_timeout` = thời gian IM LẶNG). Extension `jobAlive` coi `job.deadline` là hạn
tuyệt đối → job Thinking/agent chạy quá 600s bị `job.cancelled = true` + `settle(...)`
(ping 5s gọi `jobAlive(j, true)`), client nhận 502 sau khi đã có một phần nội dung nên
Python không retry (`emitted=True`).
Sửa: job mang thêm `idle_timeout` (giây). Extension có `jobIdleLimitMs`/`touchJob`/`jobStale`:
có `idle_timeout` thì hạn là *im lặng* tính từ `_lastActivity` (gia hạn lúc tạo job, mỗi
`send()` về proxy, mỗi `handleData`, mỗi `saveJobState`, mỗi vòng `sendPrompt` và
`waitCaptchaClear`); `deadline` tuyệt đối chỉ còn là fallback cho Python cũ.
Vấn đề 2: `/poll` giữ kết nối tới `bridge_poll_wait` (20s). Tab reload/crash trong lúc block
→ job tiếp theo bị thread cũ dequeue, `self._json` ghi vào socket chết (BrokenPipeError) →
job biến mất, caller chờ hết `bridge_job_timeout` mới 504. Chỉ try/except là KHÔNG đủ: gói
TCP đầu tiên sau FIN vẫn có thể ghi "thành công".
Sửa (3 lớp): (a) thread `/poll` chia lát 1s (`_pending.get(timeout=min(remaining, 1.0))`) và
thoát sớm khi socket chết, không dequeue job của request kế tiếp; (b)
`_Handler._poll_client_gone()` (select + `recv(1, MSG_PEEK)`, best-effort) kiểm tra socket
TRƯỚC khi giao, chết → trả job lại `_pending`; (c) bọc `_json` trong try/except, lỗi ghi → trả
job lại `_pending`. Giữ nguyên `ts` để TTL không bị reset. Log
`[orphan] poll client gone: requeued job …` / `[orphan] poll write failed …: requeued job …`.
Test: `TestJobIdleTimeout`, `TestPollDeliverySafety`, `TestPollEarlyExit`
(test_qwen_web2api.py) + 12 test Node (`aliveEnvIdle` và 6 test khoá các điểm gọi `touchJob`);
`sendPromptEnv` có stub `touchJob` đếm `_touches`.
Chưa sửa (cần capture thật): SSE đứt không `[DONE]` vẫn `settle(null)` → câu trả lời cụt;
`_stringify_raw` bỏ part `image_url` im lặng.


## Server thứ hai cướp port của server đang chạy (Windows)
Triệu chứng: script A/B bật server mới trong khi server cũ vẫn sống mà không báo lỗi gì; request
rơi vào process ngẫu nhiên, server cũ "chết lạ". Trên Windows `SO_REUSEADDR=1`
(`allow_reuse_address = True` mặc định của `HTTPServer`) cho phép bind đè lên port ĐANG có người
nghe — khác Linux, nơi nó chỉ nới điều kiện TIME_WAIT.
Sửa: `bridge._Server` (8083) và `server.ThreadedServer` (API) đặt `allow_reuse_address = False`
và override `server_bind()` set `SO_EXCLUSIVEADDRUSE=1` TRƯỚC khi bind, chỉ khi hằng số tồn tại
(`hasattr`) nên Linux/macOS giữ hành vi cũ. Server thứ hai trên cùng port giờ fail ngay
`OSError [WinError 10048]`; tắt server cũ rồi bind lại đúng port đó vẫn thành công (restart sau
crash).
Test: `TestSocketExclusiveAddr` (bind trùng port ở cả hai server + restart sau `server_close`).
