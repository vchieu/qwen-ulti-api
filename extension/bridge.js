/* Qwen Web2API UI bridge.
   Khong tu fetch completions nua (payload do app tu gui).
   - Chon temporary chat / model trong UI theo job (best-effort, warn neu loi).
   - Tu dien composer, bam send (hoac Enter).
   - Hook fetch/XHR de bat SSE tu chinh request cua app.
   - Day delta ve proxy local.
   Giu tab chat.qwen.ai mo, dang nhap. GET /api/v2/models chi de do danh
   sach model cho picker; temporary mode co the navigate mot lan. */
(() => {
  if (window.__qwenUIBridge) return;
  window.__qwenUIBridge = true;

  const BRIDGE = "http://127.0.0.1:8083";
  const BASE = location.origin;
  const MAX_JOBS = 1;
  const NEW_CHAT_EACH_JOB = true;   // thu bam New Chat truoc moi job
  const SEND_TIMEOUT_MS = 90000;    // Timeout de dien UI va cho completion response bat dau
  const STREAM_IDLE_TIMEOUT_MS = 180000; // 3 phut khong co bat ky chunk nao moi coi la stream treo
  const COMPOSER_WAIT_MS = 15000;
  const MODEL_REFRESH_MS = 10 * 60 * 1000;
  const MODE_CHECK_MS = 5000;
  const MODE_NAV_KEY = "qwenUIBridgeModeNav";
  const MODE_NAV_WINDOW_MS = 60000;
  const MODE_NAV_MAX = 2;
  const ATTACHMENT_COMPANION_PROMPT = "Please read the text and action";
  // tool built-in cua Qwen UI (vd code_interpreter chay trong
  // sandbox cua Qwen). Native call cua cac tool nay KHONG bi chan; de Qwen tu
  // chay roi stream tiep ket qua. Tool khac van gom tra Python nhu cu.
  // Mac dinh; Python gui danh sach that qua /status.builtin_tools (config qwen_builtin_tools).
  let qwenBuiltinTools = new Set(["code_interpreter"]);
  const ACTIVE_JOB_KEY = "qwenUIBridgeActiveJob";
  // Nonce moi lan load trang (content script chay lai khi tab reload/navigate): Python doi chieu
  // de biet chat cua session con nguyen ven khong (che do session, bridge_session_mode).
  const PAGE_NONCE = (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function")
    ? crypto.randomUUID()
    : "n" + Date.now().toString(36) + Math.random().toString(36).slice(2);

  let pageLoadedAsTemporary = /[?&]temporary-chat=true/.test(location.search)
    || /[?&]temporary-chat=true/.test(location.href);
  let active = 0;
  let navigating = false;
  let current = null;
  let knownModels = [];
  let lastRawDeltas = [];
  let lastModelRefresh = 0;
  let lastModeCheck = 0;
  let selectModelEnabled = true;
  let selectModeEnabled = true;    // config bridge_select_mode (qua /status)  /* */
  let forceRequestMode = false;    // config bridge_force_request_mode (qua /status)
  let modeFeatureConfig = {};      // config bridge_mode_feature_config: {mode: {key: value}}
  let forceRequestModel = false;   // config bridge_force_request_model (qua /status)
  let uiModelIds = [];             // id model hop le (models.py) de ghi de request
  let attachmentPrompt = ATTACHMENT_COMPANION_PROMPT;
  const FRESH_RELOAD_KEY = "qwenUIBridgeFreshReload";
  const FRESH_RELOAD_WINDOW_MS = 60000;
  let captchaWaitMs = 300000;   // config captcha_wait_seconds (qua /status); 0 = tat
  const CAPTCHA_SELECTORS = [
    '#baxia-dialog-content', '.baxia-dialog', '[class*="baxia" i]', '#nocaptcha',
    '.nc-container', '[class*="captcha" i]', '[id*="captcha" i]',
    'iframe[src*="captcha" i]', 'iframe[src*="baxia" i]', 'iframe[src*="geetest" i]',
    'iframe[src*="recaptcha" i]', 'iframe[src*="hcaptcha" i]',
    'iframe[src*="challenges.cloudflare.com"]', '.cf-turnstile', '#px-captcha',
  ].join(", ");
  const CAPTCHA_TEXT_RX = /verify (that )?you are (a )?human|are you a robot|slide to verify|drag the slider|unusual traffic|captcha|xác minh|kéo thanh trượt|trượt để/i;

  // Tra ve mo ta ngan neu trang dang hien captcha, "" neu khong. Khong bao gio throw.
  function captchaSignal() {
    try {
      if (/_____tmd_____|\/punish(\/|$)/.test(location.pathname + location.search)) {
        return "url:" + location.pathname;
      }
      for (const el of document.querySelectorAll(CAPTCHA_SELECTORS)) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 40 || r.height < 24) continue;   // iframe/badge an
        return el.tagName.toLowerCase() + "#" + (el.id || "") + "." + String(el.className || "").slice(0, 50)
          + " " + String(el.outerHTML || "").replace(/\s+/g, " ").slice(0, 160);
      }
      // Chi xet text trong dialog/modal (khong xet ca trang: noi dung chat co the nhac "captcha").
      for (const el of document.querySelectorAll('[role="dialog"], [class*="modal" i]')) {
        if (!visible(el)) continue;
        const t = String(el.innerText || "").replace(/\s+/g, " ").slice(0, 300);
        if (CAPTCHA_TEXT_RX.test(t)) return "dialog: " + t.slice(0, 100);
      }
    } catch (e) { /* best-effort */ }
    return "";
  }

  // Cho nguoi dung giai captcha (KHONG reload). Tra ve true neu da biet mat captcha.
  async function waitCaptchaClear(job, where) {
    if (!(captchaWaitMs > 0)) return false;
    const sig = captchaSignal();
    if (!sig) return false;
    if (job && job._clearSendTimer) job._clearSendTimer();   // timeout 90s khong duoc giet job luc dang cho
    if (job) send(job.id, { kind: "warn",
      message: "phat hien captcha (" + where + "): " + sig + " -> cho ban giai toi da "
        + Math.round(captchaWaitMs / 1000) + "s, khong reload" });
    const t0 = Date.now();
    let clearSince = 0;
    while (Date.now() - t0 < captchaWaitMs) {
      touchJob(job);

      await sleep(1000);
      /* job huy/het han luc dang cho captcha ->
         thoat NGAY (khong giu worker toi het captcha_wait_seconds). */
      if (job && !(await jobAlive(job, true))) {
        if (!job.settled) settle(job, new Error("job cancelled (cho captcha)"));
        return false;
      }
      if (captchaSignal()) { clearSince = 0; continue; }
      if (!clearSince) clearSince = Date.now();
      if (Date.now() - clearSince >= 1500) break;   // mat on dinh 1.5s => da giai xong
    }
    const solved = !captchaSignal();
    if (job) send(job.id, { kind: solved ? "debug" : "warn",
      message: solved ? "captcha da het sau " + Math.round((Date.now() - t0) / 1000) + "s; tiep tuc"
        : "het thoi gian cho captcha (" + Math.round(captchaWaitMs / 1000) + "s); tiep tuc" });
    if (job && job._armSendTimer) job._armSendTimer();
    return solved;
  }

  // Trang thai UI de chan doan khi Send khong bam duoc / timeout. Khong bao gio throw.
  function pageDiag(composer) {
    try {
      const bits = [];
      if (composer) {
        bits.push("composer=" + composerText(composer).trim().length);
        bits.push("sendBtn=" + !!findSendButton(composer));
      }
      bits.push("stopBtn=" + !!findStopButton());
      const ov = Array.from(document.querySelectorAll(
        '.wms-popup, [role="listbox"], [role="dialog"], [class*="modal" i]')).filter(visible);
      bits.push("overlays=[" + ov.slice(0, 4).map((e) =>
        String(e.className || e.getAttribute("role") || e.tagName).slice(0, 40) + ":"
        + String(e.innerText || "").replace(/\s+/g, " ").slice(0, 50)).join(" ; ") + "]");
      const ifr = Array.from(document.querySelectorAll("iframe")).filter(visible)
        .map((f) => String(f.src || "").slice(0, 60));
      if (ifr.length) bits.push("iframes=[" + ifr.slice(0, 3).join(", ") + "]");
      const cs = captchaSignal();
      if (cs) bits.push("captcha=" + cs.slice(0, 80));
      if (composer) {
        const scope = composer.closest("form") || document;
        const btns = Array.from(scope.querySelectorAll('button, [role="button"]'))
          .filter((b) => visible(b) && b.querySelector("svg")).slice(-3)
          .map((b) => (b.disabled ? "disabled" : "enabled")
            + (b.getAttribute("aria-disabled") === "true" ? "/aria-disabled" : "")
            + ":" + String(b.className || "").slice(0, 30));
        bits.push("btns=[" + btns.join(", ") + "]");
      }
      return bits.join(" ");
    } catch (e) {
      return "diag-error:" + ((e && e.message) || e);
    }
  }

  function hoverEl(el) {
    for (const t of ["pointerover", "mouseover", "pointerenter", "mouseenter", "mousemove"]) {
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
    }
  }

  function visibleModelNames() {
    return Array.from(document.querySelectorAll(".wms-list__name-text"))
      .filter(visible).map((e) => String(e.textContent || "").trim());
  }

  // Picker moi cua Qwen (`.wms-*`): popup chinh + muc "Expand more models" (popup phu).
  async function pickWmsModel(targets) {
    const norms = (targets || []).map(normLabel).filter(Boolean);
    const scan = () => {
      for (const it of document.querySelectorAll(".wms-list__item")) {
        if (!visible(it)) continue;
        const nm = it.querySelector(".wms-list__name-text");
        const n = normLabel(nm ? nm.textContent : "");
        if (n && norms.includes(n)) return it;
      }
      return null;
    };
    let hit = scan();
    if (!hit) {
      const more = Array.from(document.querySelectorAll(".wms-more-entry")).find(visible);
      if (!more) return false;
      const host = more.closest(".ant-dropdown-trigger") || more;
      hoverEl(host);
      await sleep(500);
      hit = scan();
      if (!hit) {
        more.click();
        await sleep(600);
        hit = scan();
      }
    }
    if (!hit) return false;
    hit.click();
    return true;
  }

  async function closeModelPopup(trigger) {
    const isOpen = () => Array.from(document.querySelectorAll(".wms-popup")).some(visible);
    if (!isOpen()) return;
    pressEscape();
    document.dispatchEvent(new KeyboardEvent("keydown",
      { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true }));
    await sleep(300);
    if (isOpen() && trigger) {
      trigger.click();
      await sleep(300);
    }
    if (isOpen()) debugLog("popup model van mo sau khi dong");
  }
  const INLINE_LIMIT_KEY = "qwenUIBridgeInlineLimit";
  const INLINE_LIMIT_CAP = 32000;   // 32775 ky tu tung chay inline OK -> nguong an toan ban dau sau loi
  const INLINE_STALL_MIN_CHARS = 20000;  // prompt inline >= nguong nay moi xet stall sau Send
  const INLINE_STALL_GRACE_MS = 8000;    // composer rong / nut Send bien mat qua lau -> fallback file
  const TEMP_DOM_WAIT_MS = 3000;         // doi them temporary DOM (bounded) khi trang vua reload
  const PAGE_READY_MAX_MS = 8000;
  const HYDRATION_PROBE_MS = 12000;      // cua so do hydrate moi lan load trang
  const HYDRATION_PROBE_STEP_MS = 200;   // buoc lay mau cua hydrationProbe
  let inlineMaxChars = Infinity;    // hoc duoc: prompt inline >= nguong nay di duong attach file
  try {
    const savedLimit = Number(sessionStorage.getItem(INLINE_LIMIT_KEY));
    if (savedLimit > 0) inlineMaxChars = savedLimit;
  } catch (e) { /* best-effort */ }

  // Nguong inline chi GIAM. Python bao lai gia tri da biet qua /status.inline_limit de tab
  // moi / phien moi cung dung nguong an toan nay (sessionStorage cua tab cu khong con).
  function adoptInlineLimit(v) {
    const n = Number(v);
    if (!(n > 0) || !(n < inlineMaxChars)) return false;
    inlineMaxChars = n;
    try { sessionStorage.setItem(INLINE_LIMIT_KEY, String(n)); } catch (e) { /* best-effort */ }
    return true;
  }
  function learnInlineLimit(len) {
    const n = Math.max(1000, Math.min(Number(len) - 1, INLINE_LIMIT_CAP));
    if (!adoptInlineLimit(n)) return;
    console.log("[qwen-ui-bridge] inline prompt limit learned:", n);
    // Bao Python NGAY (ping co inline_limit): Python dung min(config, learned) de tach
    // history -> file + companion that cho cac request sau. Khong cho poll/ping ke tiep vi
    // job retry co the duoc tra ve truoc do (long-poll dang treo).
    try { post("/event", { kind: "ping", inline_limit: n, tab: tabInfo() }).catch(() => {}); }
    catch (e) { /* best-effort */ }
  }
  // Prompt inline >= nguong da hoc -> di duong attach file. Tra ve true neu da chuyen.
  // file_info mang THEM bytes/sha256 (fingerprintText): thieu bytes thi classifyRequestFiles
  // bo qua so size -> guard 'size' cua bridge_attachment_block im lang vo hieu.
  // Companion uu tien: prompt Python da tach -> session.attach_prompt (dung ngu canh tung
  // loai luot) -> attachment_prompt cua config (chi la fallback cuoi, kieu compaction).
  async function maybeInlineToFile(job) {
    if (!job || job.file_info) return false;
    const text = String(job.prompt || "");
    if (!(text.length >= inlineMaxChars)) return false;
    const fp = await fingerprintText(text);
    job.file_info = {
      filename: "qwen_prompt_inline_" + job.id + ".txt",
      size: text.length,
      bytes: fp.bytes,
      sha256: fp.sha256,
      _inlineText: text,
    };
    const sessAp = job.session && job.session.attach_prompt;
    const src = job.attachment_prompt ? "python" : (sessAp ? "session" : "config");
    job.attachment_prompt = job.attachment_prompt || sessAp
      || attachmentPrompt || ATTACHMENT_COMPANION_PROMPT;
    send(job.id, { kind: "debug",
      message: "prompt " + text.length + " ky tu >= nguong inline da hoc (" + inlineMaxChars
        + "): dung duong attach file (bytes=" + fp.bytes + ", companion=" + src
        + "/" + String(job.attachment_prompt).length + " ky tu)" });
    return true;
  }

  // Job new/legacy prompt inline lon: retry an toan (chat moi) -> duoc phep stall-fallback.
  // continue/fork: delta co the da nam trong chat -> KHONG fallback (nhan doi delta).
  function inlineStallWatch(job) {
    return !!(job && !job.file_info
      && !(job.session && (job.session.mode === "continue" || job.session.mode === "fork"))
      && String(job.prompt || "").length >= INLINE_STALL_MIN_CHARS);
  }
  // Dem bubble chat dang hien (heuristic, co the dem trung node long nhau). Muc dich: phan biet
  // "chua gui gi" (n=0) vs "da co tin user trong chat" (n>0) khi Send that bai.
  function chatMessageStats() {
    try {
      const nodes = Array.from(document.querySelectorAll(
        '[class*="message" i], [class*="bubble" i], [data-message-id], [class*="chat-item" i]'
      )).filter((el) => visible(el) && (el.innerText || el.textContent || "").trim().length > 10);
      const last = nodes.length
        ? String(nodes[nodes.length - 1].innerText || "").replace(/\s+/g, " ").trim().slice(0, 60)
        : "";
      return { n: nodes.length, last: last };
    } catch (e) {
      return { n: -1, last: "" };
    }
  }
  // Diag day du tai thoi diem Send that bai: pageDiag + lich su composer/click + card + bubble
  // + temporary DOM. Khong bao gio throw (chan doan khong duoc lam hong job).
  function sendDiag(job, composer) {
    const bits = [];
    try {
      const c = composer || (job && job._composerEl) || null;
      bits.push(pageDiag(c));
      const t = job && job._sendTrace;
      if (t) {
        bits.push("clicks=" + t.clicks + " enters=" + t.enters
          + " firstClick=" + (t.firstClickAt >= 0 ? "+" + t.firstClickAt + "ms" : "-")
          + " emptyAt=" + (t.emptyAt >= 0 ? "+" + t.emptyAt + "ms" : "-"));
        if ((t.samples || []).length) bits.push("composerLens=[" + t.samples.join(",") + "]");
        if ((t.atClick || []).length) bits.push("lenAtClick=[" + t.atClick.join(",") + "]");
      }
      if (c) bits.push("cards=" + findActiveAttachmentElements(c).length);
      const ms = chatMessageStats();
      bits.push("chatMsgs=" + ms.n + (ms.last ? " last=" + JSON.stringify(ms.last) : ""));
      bits.push("tempDom=" + !!temporaryEvidence().dom);
    } catch (e) {
      bits.push("sendDiag-error:" + ((e && e.message) || e));
    }
    return bits.join(" ");
  }

  // Kiem tra body request completion cua app TRUOC khi gui: prompt inline ma noi
  // dung bi mat (Qwen doi text dai thanh document nhung khong dinh kem) -> ly do.
  function checkInlineIntegrity(job, init) {
    try {
      if (!job || job.file_info) return "";
      const body = init && init.body;
      if (typeof body !== "string" || !body) return "";
      const req = JSON.parse(body);
      const msgs = req && Array.isArray(req.messages) ? req.messages : [];
      const chars = msgs.reduce((n, m) => n + partsText(m && m.content).length, 0);
      const want = String(job.prompt || "").length;
      if (want > 2000 && chars < want * 0.9) {
        return "noi dung prompt bi mat truoc khi gui: prompt=" + want
          + " ky tu nhung request chi co " + chars;
      }
    } catch (e) { /* loi chan doan khong duoc chan app */ }
    return "";
  }

  // Lan dau tren moi lan load trang (nhat la ngay sau reload): DOM chua hydrate nen
  // model picker/composer chua san sang -> doi truoc khi chon model va dien prompt.
  // Che do doi hydrate = config page_ready_mode ("full" | "sleep_only" | "off").
  // Gate theo pageSent (request completion DA di) thay vi pageUsed (chi moi tim thay
  // composer): job dau fail VI chua hydrate thi attempt 2/3 tren cung trang van duoc doi.
  // Log tach loop/sleep/rounds de do chi phi that cua viec doi (A/B voi sleep_only/off).
  async function waitPageReady(job) {
    if (pageSent) return;
    if (pageReadyMode === "off") {
      if (job) send(job.id, { kind: "debug", message: "page ready: mode=off (khong doi hydrate)" });
      return;
    }
    const t0 = Date.now();
    const targets = modelTargets(job && job.model);
    const wantTemp = isTemporaryPage(false);
    let picker = false, composer = false, tempDom = false, rounds = 0;
    do {
      rounds++;
      picker = !!findModelTrigger(targets);
      composer = findComposers().length > 0;
      tempDom = !wantTemp || !!temporaryEvidence().dom;
      if (pageReadyMode !== "full") break;        // sleep_only: hanh vi cu, lay mau 1 lan
      if (composer && (picker || selectModelEnabled === false)
          && (tempDom || Date.now() - t0 >= TEMP_DOM_WAIT_MS)) break;
      await sleep(300);
    } while (Date.now() - t0 < PAGE_READY_MAX_MS);
    const loopMs = Date.now() - t0;
    const atLoop = { picker: picker, composer: composer, tempDom: tempDom };
    const tSleep = Date.now();
    await sleep(1000);   // de app gan xong handler cho composer/paste
    const sleepMs = Date.now() - tSleep;
    // Lay mau LAI sau sleep. Log cu in gia tri cua vong loop cuoi (truoc sleep 1s) nen
    // warn "CHUA hydrate" co the la false positive: log 00:18:41 in tempDom=false nhung
    // evidence.dom=true doc ngay sau do -> DOM da xong TRONG luc sleep.
    picker = !!findModelTrigger(targets);
    composer = findComposers().length > 0;
    tempDom = !wantTemp || !!temporaryEvidence().dom;
    if (job) send(job.id, { kind: "debug",
      message: "page ready: mode=" + pageReadyMode + " modelPicker=" + picker
        + " composer=" + composer + " tempDom=" + tempDom
        + " (atLoop picker=" + atLoop.picker + " composer=" + atLoop.composer
        + " tempDomAtLoop=" + atLoop.tempDom + ")"
        + " loop=" + loopMs + "ms/" + rounds + " rounds sleep=" + sleepMs
        + "ms total=" + (Date.now() - t0) + "ms" });
    if (job && pageReadyMode === "full" && (!composer || !tempDom)) {
      fwd(job, "warn", "trang co the CHUA hydrate xong khi dien prompt (composer=" + composer
        + " tempDom=" + tempDom + " wantTemp=" + wantTemp
        + " evidence=" + JSON.stringify(temporaryEvidence()) + ")");
    }
  }

  // Do DUONG CONG hydrate that cua trang, KHONG phu thuoc job: chi doc DOM, khong sua gi.
  // Chay 1 lan moi load trang, gui 1 event tong ket (kind="probe", job_id="none").
  // -1 = khong thay trong cua so do. reactProps = React da gan props vao composer chua
  // (tin hieu hydrate that, tot hon "co phan tu temporary trong DOM").
  async function hydrationProbe(ms) {
    const total = Number(ms) > 0 ? Number(ms) : HYDRATION_PROBE_MS;
    const t0 = Date.now();
    const at = { composer: -1, picker: -1, tempDom: -1, reactProps: -1 };
    for (;;) {
      const now = Date.now() - t0;
      try {
        const el = findComposers()[0];
        if (at.composer < 0 && el) at.composer = now;
        if (at.picker < 0 && findModelTrigger([])) at.picker = now;
        if (at.tempDom < 0 && temporaryEvidence().dom) at.tempDom = now;
        if (at.reactProps < 0 && el && Object.keys(el).some(
          (k) => k.indexOf("__reactProps$") === 0)) at.reactProps = now;
      } catch (e) { /* best-effort: probe khong duoc lam hong gi */ }
      if (now >= total) break;
      await sleep(HYDRATION_PROBE_STEP_MS);
    }
    try {
      await post("/event", { job_id: "none", kind: "probe", probe: at,
        message: "hydration probe (ms ke tu luc content script chay): " + JSON.stringify(at)
          + " window=" + total + "ms path=" + location.pathname });
    } catch (e) { /* best-effort */ }
  }

  let temporaryReloadEnabled = true;  // config temporary_chat_reload (qua /status)
  let pageReadyMode = "full";         // config page_ready_mode: full | sleep_only | off
  let pageUsed = false;               // trang nay da gui >= 1 prompt => co tin nhan cu
  let pageSent = false;               // request completion DA di tren trang nay
                                      // (gate waitPageReady; khac pageUsed = moi thay composer)
  let freshByBridge = false;          // trang vua duoc bridge chu dong reload => sach
  try {
    const rawTs = sessionStorage.getItem(FRESH_RELOAD_KEY);
    if (rawTs) {
      sessionStorage.removeItem(FRESH_RELOAD_KEY);
      freshByBridge = Date.now() - Number(rawTs) < FRESH_RELOAD_WINDOW_MS;
    }
  } catch (e) { /* best-effort */ }
  let debugEnabled = false;
  let lastModelRefreshWarnTs = 0;  // throttle warn về Python: 1 lần/60s

  const log = (...a) => console.log("[qwen-ui-bridge]", ...a);
  const debugLog = (...a) => {
    if (debugEnabled) console.log("[qwen-ui-bridge:debug]", ...a);
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function post(path, body) {
    const r = await fetch(BRIDGE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json();
  }

  async function send(jobId, ev) {
    if (current && current.id === jobId) touchJob(current);
    try {
      const r = await post("/event", Object.assign({ job_id: jobId }, ev));
      /* proxy bao job da chet -> dung UI ngay.
         Day la duong huy nhanh nhat: moi event cua job cu deu tra stale=true. */
      if (r && r.stale && current && current.id === jobId && !current.settled) {
        current.cancelled = true;
        settle(current, new Error("proxy bao job " + jobId + " da het hieu luc (stale)"));
      }
    } catch (e) { /* proxy chet */ }
  }

  /* liveness + strict model/mode ------------ */
  let requireModel = false;   // config bridge_require_model (qua /status)
  let requireMode = false;    // config bridge_require_mode  (qua /status)
  let requireAttachment = true;   // config bridge_require_attachment (qua /status)  /* */
  // config bridge_attachment_block (qua /status): kind metadata file lech thi CHAN thay vi canh bao.
  // name|size|status|count (request), content (file lay tu bridge lech file Python), stale (opt-in).
  let attachmentBlock = new Set(["name", "size", "status", "count", "content"]);
  let xhrBodyWarned = false;  // XHR body khong phai string: chi canh bao 1 lan
  let navSince = 0;           // N1: watchdog cho co `navigating`
  const ALIVE_CACHE_MS = 1500;

  // bridge_job_timeout cua Python la thoi gian IM LANG (reset moi event), KHONG phai tong
  // thoi gian job. Truoc day extension coi job.deadline (epoch ms ke tu luc XEP job) la han
  // tuyet doi -> job Thinking/agent chay > bridge_job_timeout bi settle giua chung du dang
  // stream (ping 5s goi jobAlive(j, true)). Gio: co idle_timeout (Python moi) -> han tinh tu
  // _lastActivity, gia han bang touchJob; deadline tuyet doi chi con la fallback Python cu.
  function jobIdleLimitMs(job) {
    const s = Number(job && job.idle_timeout);
    return s > 0 ? s * 1000 : 0;
  }
  function touchJob(job) {
    if (job) job._lastActivity = Date.now();
  }
  function jobStale(job) {
    const idle = jobIdleLimitMs(job);
    if (idle > 0) {
      const last = Number(job._lastActivity) || 0;
      return !!(last && Date.now() - last > idle);
    }
    return !!(job.deadline && Date.now() > job.deadline);
  }

  function jobDead(job) {
    return !job || job.settled || job.cancelled;
  }

  // Con song = chua settle/cancel, chua qua deadline, va proxy con cho caller.
  // force=true bo qua cache 1.5s -> hoi proxy that
  // ngay truoc thao tac UI (sau sleep, job co the vua bi huy).
  async function jobAlive(job, force) {
    if (jobDead(job)) return false;
    if (jobStale(job)) { job.cancelled = true; return false; }
    const now = Date.now();
    if (!force && job._aliveAt && now - job._aliveAt < ALIVE_CACHE_MS) return job._alive !== false;
    job._aliveAt = now;
    try {
      const r = await getJSON("/alive?job_id=" + encodeURIComponent(job.id));
      job._alive = !!(r && r.alive);
      if (!job._alive) job.cancelled = true;
    } catch (e) {
      // Khong noi duoc bridge (hoac chua co /alive): khong ket luan job chet.
      return !jobDead(job);
    }
    return job._alive && !jobDead(job);
  }

  async function ensureAlive(job, stage, force) {
    if (await jobAlive(job, force)) return true;
    if (job && !job.settled) {
      job.cancelled = true;
      settle(job, new Error("job het hieu luc o buoc '" + stage + "' (caller da ngat/huy)"));
    }
    throw new Error("job cancelled at " + stage);
  }

  function saveJobState(job, stage, details) {
    touchJob(job);
    if (!job) return;
    job._stage = stage;
    try {
      sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify({
        id: job.id,
        stage: stage,
        has_file: !!job.file_info,
        filename: job.file_info?.filename,
        details: details || "",
        ts: Date.now()
      }));
    } catch (e) {}
  }

  function clearJobState() {
    try {
      sessionStorage.removeItem(ACTIVE_JOB_KEY);
    } catch (e) {}
  }

  // Kiem tra xem tab co vua bi reload giua chung khi dang xu ly job khong
  try {
    const lastStateRaw = sessionStorage.getItem(ACTIVE_JOB_KEY);
    if (lastStateRaw) {
      sessionStorage.removeItem(ACTIVE_JOB_KEY);
      const last = JSON.parse(lastStateRaw);
      if (last && last.id && (Date.now() - (last.ts || 0) < 300000)) {
        const fileMsg = last.has_file
          ? ` (pasted file ${last.filename || 'prompt'} da bi mat sau reload)`
          : "";
        const reloadMsg = `CANH BAO: Browser/Tab da tu reload khi dang xu ly job ${last.id} o buoc '${last.stage}'${fileMsg}`;
        console.warn("[qwen-ui-bridge] RELOAD DETECTED:", reloadMsg);
        post("/event", {
          job_id: last.id,
          kind: "warn",
          message: reloadMsg,
          tab_reloaded: true,
          stage: last.stage
        }).catch(() => {});
      }
    }
  } catch (e) {}

  if (typeof window.addEventListener === "function") {
    window.addEventListener("beforeunload", () => {
      if (current && !current.settled) {
        const fileMsg = current.file_info
          ? ` (pasted file ${current.file_info.filename || 'prompt'} se bi mat!)`
          : "";
        const msg = `Tab dang reload/unload khi job ${current.id} chua hoan thanh (buoc: ${current._stage || 'processing'})${fileMsg}`;
        console.warn("[qwen-ui-bridge]", msg);
        try {
          const payload = JSON.stringify({
            job_id: current.id,
            kind: "warn",
            message: msg,
            tab_reloading: true
          });
          if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
            navigator.sendBeacon(BRIDGE + "/event", new Blob([payload], { type: "application/json" }));
          }
        } catch (e) {}
      }
    });
  }

  function settle(job, err) {
    if (!job || job.settled) return;
    job.settled = true;
    clearJobState();
    if (err) {
      const msg = String((err && err.message) || err);
      log("job error:", msg);
      const lost = !!(err && err.sessionLost);
      send(job.id, lost ? { kind: "error", message: msg, session_lost: true }
        : { kind: "error", message: msg }).finally(() => job.reject(err));
    } else {
      send(job.id, { kind: "done" }).finally(() => job.resolve());
    }
  }

  // ----------------------------------------- models / temporary-chat mode --
  function textOf(el) {
    if (!el) return "";
    return String(el.getAttribute("aria-label") || el.getAttribute("title")
      || el.innerText || el.textContent || "").trim();
  }

  // Text nhin thay cua element (khac textOf: textOf uu tien aria-label).
  function visibleText(el) {
    if (!el) return "";
    return String(el.innerText || el.textContent || "").trim();
  }

  // Nut model picker co the mang aria-label co dinh ("Select Model") trong khi
  // text hien thi la ten model dang chon -> doc text nhin thay truoc.
  function triggerLabel(el) {
    return visibleText(el) || textOf(el);
  }

  function normLabel(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/\.0\b/g, "")
      .replace(/[^a-z0-9]+/g, "");
  }

  // Qwen doi shape theo phase: string, mang part ({type,text}) hay object
  // {content:[...]}. Gom ve 1 string de khong lam mat thinking.
  function partsText(v) {
    if (typeof v === "string") return v;
    if (Array.isArray(v)) return v.map(partsText).join("");
    if (v && typeof v === "object") {
      if (typeof v.text === "string") return v.text;
      if (v.content !== undefined) return partsText(v.content);
    }
    return "";
  }

  // Gom content cua summary (string | mang string | mang part | {content}) thanh
  // 1 string, moi phan cach nhau bang `sep`.
  function joinParts(v, sep) {
    const raw = (v && typeof v === "object" && !Array.isArray(v)
      && v.content !== undefined) ? v.content : v;
    const list = Array.isArray(raw) ? raw : (raw == null ? [] : [raw]);
    return list.map((p) => partsText(p).trim()).filter(Boolean).join(sep);
  }

  function labelMatches(targets, text) {
    const n = normLabel(text);
    if (!n) return false;
    return (targets || []).some((t) => {
      const tn = normLabel(t);
      return tn && (n === tn || n.includes(tn));
    });
  }

  function pressEscape() {
    const opts = { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true };
    const el = document.activeElement || document.body;
    if (!el) return;
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
  }

  async function getJSON(path) {
    const r = await fetch(BRIDGE + path);
    if (!r.ok) throw new Error("bridge " + path + " -> " + r.status);
    return r.json();
  }

  async function readStatus() {
    const st = await getJSON("/status");
    if (st && typeof st.select_model === "boolean") selectModelEnabled = st.select_model;
    if (st && typeof st.select_mode === "boolean") selectModeEnabled = st.select_mode;
    if (st && typeof st.force_model === "boolean") forceRequestModel = st.force_model;
    if (st && typeof st.force_mode === "boolean") forceRequestMode = st.force_mode;
    if (st && st.mode_feature_config && typeof st.mode_feature_config === "object") {
      modeFeatureConfig = st.mode_feature_config;
    }
    if (st && typeof st.require_model === "boolean") requireModel = st.require_model;
    if (st && typeof st.require_mode === "boolean") requireMode = st.require_mode;
    if (st && typeof st.require_attachment === "boolean") requireAttachment = st.require_attachment;
    if (st && Array.isArray(st.attachment_block)) {
      attachmentBlock = new Set(st.attachment_block
        .filter((x) => typeof x === "string")
        .map((x) => x.trim().toLowerCase()).filter(Boolean));
    }
    if (st && Array.isArray(st.model_ids)) {
      uiModelIds = st.model_ids.filter((x) => typeof x === "string");
    }
    if (st && Array.isArray(st.builtin_tools)) {
      const names = st.builtin_tools
        .filter((x) => typeof x === "string")
        .map((x) => x.trim().toLowerCase())
        .filter(Boolean);
      if (names.join(",") !== Array.from(qwenBuiltinTools).join(",")) {
        debugLog("builtin tools cua Qwen:", names);
      }
      qwenBuiltinTools = new Set(names);
    }
    if (st && typeof st.attachment_prompt === "string" && st.attachment_prompt.trim()) {
      attachmentPrompt = st.attachment_prompt.trim();
    }
    if (st && typeof st.debug === "boolean") {
      debugEnabled = st.debug;
    }
    if (st && typeof st.page_ready_mode === "string") pageReadyMode = st.page_ready_mode;
    if (st) adoptInlineLimit(st.inline_limit);
    if (st && typeof st.temporary_reload === "boolean") {
      temporaryReloadEnabled = st.temporary_reload;
    }
    if (st && typeof st.captcha_wait === "number") {
      captchaWaitMs = Math.max(0, st.captcha_wait) * 1000;
    }
    return st || {};
  }

  // Throttled: lay danh sach model tu chinh API cua trang (chi de do picker).
  async function refreshModels(force) {
    const now = Date.now();
    if (!force && now - lastModelRefresh < MODEL_REFRESH_MS) return knownModels;
    try {
      const r = await origFetch(BASE + "/api/v2/models", {
        headers: { accept: "application/json" },
        credentials: "same-origin",
      });
      if (!r.ok) throw new Error("models -> " + r.status);
      const data = await r.json();
      const src = data && data.data;
      const list = Array.isArray(src)
        ? src
        : (src && (src.data || src.models || src.list));
      const out = [];
      for (const m of Array.isArray(list) ? list : []) {
        if (!m || typeof m.id !== "string" || !m.id.trim()) continue;
        const id = m.id.trim();
        const name = (typeof m.name === "string" && m.name.trim()) || id;
        out.push({ id: id, name: name });
      }
      if (out.length) {
        knownModels = out;
        lastModelRefresh = now;
      }
    } catch (e) {
      log("model refresh skipped:", (e && e.message) || e);
      lastModelRefresh = Date.now() - MODEL_REFRESH_MS + 60000;  // thu lai sau 1 phut, khong moi vong poll
      // Forward warn về Python nếu có job đang chạy, rate-limited 60s
      const now = Date.now();
      if (now - lastModelRefreshWarnTs > 60000) {
        lastModelRefreshWarnTs = now;
        const msg = "model refresh fail: " + ((e && e.message) || String(e));
        post("/event", { job_id: "none", kind: "warn", message: msg }).catch(() => {});
      }
    }
    return knownModels;
  }

  function modelTargets(model) {
    if (!model) return [];
    const raw = String(model).split("@think=")[0].trim();
    if (!raw) return [];
    const targetNorm = normLabel(raw);
    const labels = [raw];
    for (const m of knownModels) {
      if (!m || !m.id) continue;
      if (m.id === raw || normLabel(m.id) === targetNorm) {
        labels.push(m.id);
        if (m.name) labels.push(m.name);
      }
    }
    const out = [];
    for (const l of labels) {
      const t = String(l || "").trim();
      if (!t) continue;
      if (!out.includes(t)) out.push(t);
      const n = normLabel(t);
      if (n && !out.includes(n)) out.push(n);
    }
    return out;
  }

  function findModelTrigger(targets) {
    const nodes = Array.from(document.querySelectorAll(
      'button, [role="button"], [role="combobox"], [aria-haspopup], [class*="model" i]'
    ));
    const norms = (targets || []).map(normLabel).filter(Boolean);
    const known = [];
    for (const m of knownModels) {
      if (!m) continue;
      for (const v of [m.id, m.name]) {
        const n = normLabel(v);
        if (n) known.push(n);
      }
    }
    let best = null, bestScore = 0;
    for (const el of nodes) {
      if (!visible(el)) continue;
      const label = textOf(el);
      if (!label) continue;
      const low = label.toLowerCase();
      if (/^(send|stop|gửi|dừng)/i.test(low)) continue;
      const n = normLabel(label);
      if (!n) continue;
      let score = 0;
      if (norms.includes(n)) score += 5;
      else if (norms.some((t) => t && n.includes(t)
        && Math.abs(n.length - t.length) <= 3)) score += 3;
      else if (known.includes(n)) score += 4;          // dang hien 1 model khac trong danh sach
      // "Qwen3.8-Max" hop le; "QwenUser6364 Free" (nut profile) thi khong.
      else if (/^(qwen|qwq)\d/.test(n) && n.length <= 40) score += 4;
      const hint = ((el.getAttribute("aria-label") || "") + " "
        + (el.getAttribute("title") || "")).toLowerCase();
      if (/model|mô hình/.test(hint)) score += 2;
      if (el.closest("form")) score -= 2;
      if (el.matches('button, [role="button"], [role="combobox"], [aria-haspopup]')) score += 1;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return bestScore >= 3 ? best : null;
  }

  // Forward ve Python log (debugLog chi in console cua tab -> khong thay trong /debug).
  function fwd(job, kind, message) {
    try { if (job) send(job.id, { kind: kind, message: message }); } catch (e) { /* best-effort */ }
  }

  function visState() {
    try {
      return document.visibilityState + (document.hasFocus() ? "/focus" : "/nofocus");
    } catch (e) { return "?"; }
  }

  // Tab nen: React render cham -> cho nhan trigger doi toi ms thay vi mot lan sleep co dinh.
  async function waitLabelMatches(targets, trigger, ms) {
    const t0 = Date.now();
    for (;;) {
      if (labelMatches(targets, triggerLabel(trigger))) return true;
      if (Date.now() - t0 >= ms) return false;
      await sleep(200);
    }
  }

  function modelUiSummary(job) {
    try {
      const t = findModelTrigger(modelTargets(job.model));
      return "model UI='" + (t ? triggerLabel(t) : "?") + "' job.model=" + job.model
        + " vis=" + visState();
    } catch (e) { return "model UI=? (loi doc nhan)"; }
  }

  // Ghi de truong model/models trong body completion bang job.model (opt-in).
  function rewriteRequestModel(job, init) {
    try {
      if (!job || !init || typeof init.body !== "string" || !init.body) return null;
      const want = String(job.model || "").split("@think=")[0].trim();
      if (!want) return null;
      const valid = uiModelIds.includes(want) || knownModels.some((m) => m && m.id === want);
      if (!valid) {
        fwd(job, "warn", "force model: '" + want + "' khong nam trong danh sach hop le -> khong ghi de");
        return null;
      }
      const req = JSON.parse(init.body);
      if (!req || typeof req !== "object") return null;
      let changed = false;
      const was = typeof req.model === "string" ? req.model : "";
      if (typeof req.model === "string" && req.model !== want) { req.model = want; changed = true; }
      for (const m of Array.isArray(req.messages) ? req.messages : []) {
        if (!m || typeof m !== "object") continue;
        if (typeof m.model === "string" && m.model !== want) { m.model = want; changed = true; }
        if (Array.isArray(m.models) && m.models.length
            && !(m.models.length === 1 && m.models[0] === want)) {
          m.models = [want];
          changed = true;
        }
      }
      if (!changed) return null;
      fwd(job, "debug", "force model: ghi de request model '" + was + "' -> '" + want + "'");
      return Object.assign({}, init, { body: JSON.stringify(req) });
    } catch (e) { return null; }
  }

  // (config bridge_force_request_mode): ghi de feature_config trong body completion theo che do
  // cua job (reasoning_effort -> auto/thinking/fast), giong rewriteRequestModel. Key/gia tri lay
  // tu bridge_mode_feature_config[mode] (mac dinh = request that cua Qwen, xem config.py) --
  // code khong doan ten key. Gia tri null = XOA key khoi feature_config (Fast khong co
  // thinking_format; chi ghi de key co san thi chuyen tu Thinking sang Fast se de sot key cu).
  function rewriteRequestMode(job, init) {
    try {
      if (!job || !init || typeof init.body !== "string" || !init.body) return null;
      const mode = String(job.mode || "").toLowerCase();
      if (!mode) return null;
      const warnOnce = (msg) => {
        if (job._modeWarned) return;
        job._modeWarned = true;
        fwd(job, "warn", "force mode: " + msg);
      };
      const patch = modeFeatureConfig && modeFeatureConfig[mode];
      if (!patch || typeof patch !== "object" || !Object.keys(patch).length) {
        warnOnce("chua khai bao bridge_mode_feature_config['" + mode + "'] -> khong ghi de");
        return null;
      }
      const req = JSON.parse(init.body);
      if (!req || typeof req !== "object") return null;
      const targets = [];
      if (req.feature_config && typeof req.feature_config === "object") {
        targets.push(["top", req.feature_config]);
      }
      for (const m of Array.isArray(req.messages) ? req.messages : []) {
        if (m && typeof m === "object" && m.feature_config && typeof m.feature_config === "object") {
          targets.push(["msg", m.feature_config]);
        }
      }
      if (!targets.length) {
        warnOnce("body khong co feature_config (keys=" + Object.keys(req).join(",") + ") -> khong ghi de");
        return null;
      }
      const diffs = [];
      for (const [where, fc] of targets) {
        for (const k of Object.keys(patch)) {
          if (patch[k] === null) {
            if (Object.prototype.hasOwnProperty.call(fc, k)) {
              diffs.push(where + "." + k + ": " + JSON.stringify(fc[k]) + " -> (xoa)");
              delete fc[k];
            }
          } else if (JSON.stringify(fc[k]) !== JSON.stringify(patch[k])) {
            diffs.push(where + "." + k + ": " + JSON.stringify(fc[k]) + " -> " + JSON.stringify(patch[k]));
            fc[k] = patch[k];
          }
        }
      }
      if (!diffs.length) {
        debugLog("force mode '" + mode + "': feature_config da dung, khong can ghi de");
        return null;
      }
      fwd(job, "debug", "force mode '" + mode + "': " + diffs.join("; "));
      return Object.assign({}, init, { body: JSON.stringify(req) });
    } catch (e) { return null; }
  }

  // ---- Che do session: dung tiep chat dang mo thay vi mo chat moi ----
  // chat_id tren URL (/c/<id>). Temporary chat giu /c/local -> khong co id that.
  function urlChatId(pathname) {
    const m = /\/c\/([^/?#]+)/.exec(String(pathname || ""));
    return m && m[1] !== "local" ? m[1] : "";
  }

  // Trang thai tab gui kem /poll va ping de Python doi chieu session.
  function tabInfo() {
    return {
      nonce: PAGE_NONCE,
      path: location.pathname,
      chat_id: urlChatId(location.pathname),
      busy: !!current || active > 0,
    };
  }

  // Tab hien tai con la chat cua session khong? "" = con; con lai la ly do (dung de bao
  // session_lost -> Python mo chat moi + replay day du).
  function sessionProblem(sj, nonce, pathname, temporary) {
    if (!sj || !sj.nonce) return "job khong mang nonce session";
    if (sj.nonce !== nonce) return "tab da reload/navigate (nonce khac)";
    const id = urlChatId(pathname);
    if (sj.chat_id) {
      if (temporary) {
        if (id) return "tab dang o chat " + id + ", khong phai temporary chat cua session";
      } else if (id !== sj.chat_id) {
        return "URL chat_id=" + (id || "-") + " khac chat cua session " + sj.chat_id;
      }
    }
    return "";
  }

  // chat_id / parent_id THAT trong body completion (app tu tinh parent_id tu cay tin nhan).
  function sessionInfoOfBody(req) {
    const msgs = Array.isArray(req && req.messages) ? req.messages : [];
    const last = msgs.length ? msgs[msgs.length - 1] : null;
    const pick = (...v) => {
      for (const x of v) if (typeof x === "string") return x;
      return "";
    };
    return {
      chat_id: pick(req && req.chat_id, req && req.chatId),
      parent_id: pick(req && req.parent_id, req && req.parentId,
        last && last.parent_id, last && last.parentId),
    };
  }

  // Fork: ghi parent_id vao DUNG 4 cho app dat (top-level parent_id/parentId, messages[].parent_id/
  // parentId) de tin moi thanh nhanh anh em cua nut do. userAction (vd "edit") ghi vao messages.
  function applyForkParent(req, parentId, userAction) {
    if (!req || typeof req !== "object" || !parentId) return false;
    req.parent_id = parentId;
    req.parentId = parentId;
    for (const m of Array.isArray(req.messages) ? req.messages : []) {
      if (!m || typeof m !== "object") continue;
      m.parent_id = parentId;
      m.parentId = parentId;
      if (userAction) m.user_action = userAction;
    }
    return true;
  }

  // Job session.mode=fork: tra init moi voi parent_id da ghi de, hoac null neu khong ghi duoc
  // (caller PHAI chan request, khong duoc de no di tiep tren nhanh chinh).
  function rewriteRequestSession(job, init) {
    try {
      const sj = job && job.session;
      if (!sj || sj.mode !== "fork" || !sj.fork_parent_id) return null;
      if (!init || typeof init.body !== "string" || !init.body) return null;
      const req = JSON.parse(init.body);
      if (!applyForkParent(req, String(sj.fork_parent_id), String(sj.user_action || ""))) return null;
      fwd(job, "debug", "session fork: parent_id -> " + sj.fork_parent_id
        + " user_action=" + (sj.user_action || "(giu nguyen)"));
      return Object.assign({}, init, { body: JSON.stringify(req) });
    } catch (e) { return null; }
  }

  // Bao Python: request completion cua job session da di (= delta da vao chat Qwen), kem
  // nonce + chat_id + parent_id doc tu body that.
  async function announceSession(job, input, init) {
    try {
      if (!job || !job.session) return;
      let body = init && init.body;
      if (typeof body !== "string" && input && typeof input.clone === "function") {
        body = await input.clone().text();
      }
      if (typeof body !== "string" || !body) return;
      const info = sessionInfoOfBody(JSON.parse(body));
      send(job.id, { kind: "session", mode: job.session.mode, nonce: PAGE_NONCE,
        chat_id: info.chat_id, parent_id: info.parent_id });
    } catch (e) { /* khong anh huong app */ }
  }

  // ---- Che do Thinking / Auto / Fast (dropdown canh o prompt) ----
  const MODE_LABELS = ["auto", "thinking", "fast"];

  function modeLabelOf(el) {
    const t = normLabel(visibleText(el));
    return MODE_LABELS.includes(t) ? t : "";
  }

  function clickModeEl(el) {
    const t = (el.closest && el.closest('button, [role="button"]')) || el;
    t.click();
  }

  function modeCandidates() {
    try {
      return Array.from(document.querySelectorAll('button, [role="button"], [aria-haspopup]'))
        .filter(visible).map((e) => (visibleText(e) || textOf(e)).replace(/\s+/g, " ").slice(0, 22))
        .filter(Boolean).slice(-12).join(" | ");
    } catch (e) { return "?"; }
  }

  // Nut that su bat click cua dropdown che do (DOM Qwen):
  // .qwen-thinking-selector > .qwen-chat-v2-dropdown-menu > -trigger > -select
  // (.qwen-thinking-selector chi la wrapper; "selector" chua "select" nen de bi
  //  heuristic cu chon nham wrapper -> click khong mo menu.)
  function modeSelectButton() {
    let nodes = Array.from(document.querySelectorAll(
      ".qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select"));
    if (!nodes.length) {
      nodes = Array.from(document.querySelectorAll(
        ".qwen-chat-v2-dropdown-menu-select, .qwen-chat-v2-dropdown-menu-select-button"));
    }
    return nodes.find((el) => visible(el)
      && MODE_LABELS.includes(normLabel(visibleText(el).replace(/\s+/g, " ")))) || null;
  }

  function findModeTrigger() {
    const direct = modeSelectButton();
    if (direct) return direct;
    const comps = findComposers();
    const root = comps.length ? (comps[0].closest("form") || comps[0].parentElement) : null;
    const nodes = Array.from(document.querySelectorAll(
      'button, [role="button"], [aria-haspopup], [aria-expanded], [class*="dropdown" i], [class*="select" i]'));
    let best = null, bestScore = 0;
    for (const el of nodes) {
      if (!visible(el) || el.closest('[role="menu"], [role="listbox"]')) continue;
      const vt = visibleText(el);
      if (vt.length > 24) continue;
      let score = 0;
      if (MODE_LABELS.includes(normLabel(vt))) score += 5;
      else if (MODE_LABELS.includes(normLabel(el.getAttribute("aria-label") || ""))) score += 3;
      else continue;
      if (el.matches('button, [role="button"], [aria-haspopup]')) score += 2;
      if (el.getAttribute("aria-haspopup") || el.hasAttribute("aria-expanded")) score += 2;
      if (root && root.contains(el)) score += 3;
      if (el.querySelectorAll("*").length > 12) score -= 2;
      // Uu tien phan tu sau nhat (wrapper khong phai nut bam).
      if (Array.from(el.querySelectorAll("*")).some((c) => MODE_LABELS.includes(normLabel(visibleText(c))))) {
        score -= 4;
      }
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return best;
  }

  function modeMenuItems() {
    const sel = '[role="menuitem"], [role="menuitemradio"], .qwen-chat-v2-dropdown-menu-item';
    const out = [];
    for (const it of document.querySelectorAll(sel)) {
      if (visible(it) && !out.includes(it)) out.push(it);
    }
    return out;
  }

  function modeMenuDiag() {
    try {
      const menus = Array.from(document.querySelectorAll(
        '[role="menu"], .qwen-chat-v2-dropdown-menu-popup')).filter(visible).length;
      return "menus=" + menus + " items=["
        + modeMenuItems().map(modeItemLabel).join(",") + "]";
    } catch (e) { return "mode-diag-error:" + ((e && e.message) || e); }
  }

  function modeItemLabel(it) {
    const l = it.querySelector(".qwen-chat-v2-dropdown-menu-item-label");
    return normLabel((l && l.textContent) || it.textContent || "");
  }

  async function closeModeMenu(trigger) {
    const isOpen = () => Array.from(document.querySelectorAll('[role="menu"]')).some(visible);
    if (!isOpen()) return;
    pressEscape();
    document.dispatchEvent(new KeyboardEvent("keydown",
      { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true }));
    await sleep(300);
    if (isOpen() && trigger) {
      clickModeEl(trigger);
      await sleep(300);
    }
  }

  // job.mode: "auto" | "thinking" | "fast" (tu reasoning_effort). Rong = khong dung UI.
  async function selectThinkingMode(job) {
    try {
      const want = String((job && job.mode) || "").toLowerCase();
      if (!want || !MODE_LABELS.includes(want) || selectModeEnabled === false) {
        fwd(job, "debug", "bo qua chon mode (job.mode='" + ((job && job.mode) || "")
          + "' select_mode=" + selectModeEnabled + ")");
        return true;
      }
      let trig = null;
      for (let i = 0; i < 4 && !trig; i++) {
        trig = findModeTrigger();
        if (!trig) await sleep(500);
      }
      if (!trig) {
        fwd(job, "warn", "khong tim thay nut che do Thinking/Auto/Fast de chon '" + want
          + "' | ung vien: " + modeCandidates());
        return false;
      }
      const cur = modeLabelOf(trig);
      if (cur === want) {
        fwd(job, "debug", "mode UI da o '" + want + "' vis=" + visState());
        return true;
      }
      clickModeEl(trig);
      let items = [];
      const tOpen = Date.now();
      while (Date.now() - tOpen < 2500) {
        await sleep(150);
        items = modeMenuItems();
        if (items.length) break;
      }
      if (!items.length) {
        await closeModeMenu(trig);
        fwd(job, "warn", "menu che do khong mo sau khi click nut (" + modeMenuDiag()
          + "; trigger=<div class='" + String(trig.className || "").slice(0, 40)
          + "' label='" + triggerLabel(trig) + "'>)");
        return false;
      }
      const have = items.map(modeItemLabel).join(",");
      fwd(job, "debug", "mode menu mo (" + have + ")");
      const item = items.find((it) => modeItemLabel(it) === want);
      if (!item) {
        await closeModeMenu(trig);
        fwd(job, "warn", "khong thay muc '" + want + "' trong menu che do (menu: [" + have + "])");
        return false;
      }
      item.click();
      let after = "";
      const t0 = Date.now();
      while (Date.now() - t0 < 2000) {
        await sleep(150);
        after = modeLabelOf(trig);
        if (after === want) break;
        const sel = modeMenuItems().find((it) =>
          /dropdown-menu-item-selected/.test(String(it.className || "")));
        if (sel && modeItemLabel(sel) === want) { after = want; break; }
      }
      await closeModeMenu(trig);
      if (after === want) {
        fwd(job, "debug", "mode UI: '" + cur + "' -> '" + want + "' vis=" + visState());
        return true;
      }
      if (!after) {
        /* strict -> khong xac minh duoc = that bai */
        if (requireMode) {
          fwd(job, "warn", "mode UI: da bam '" + want + "' nhung khong doc duoc nhan nut -> "
            + "khong xac minh duoc (bridge_require_mode=true => that bai)");
          return false;
        }
        fwd(job, "debug", "mode UI: da bam '" + want + "' nhung khong doc duoc nhan nut (khong xac minh duoc)");
        return true;
      }
      fwd(job, "warn", "UI chua hien che do '" + want + "' (hien tai: '" + after + "')");
      return false;
    } catch (e) {
      fwd(job, "warn", "mode select error: " + ((e && e.message) || e));
      return false;
    }
  }

  // Chon model theo job.model. Loi UI chi warn, request van chay tiep.
  async function selectModel(job) {
    try {
      if (!job || !job.model || selectModelEnabled === false) return true;
      const targets = modelTargets(job.model);
      if (!targets.length) return true;
      const trigger = findModelTrigger(targets);
      if (!trigger) {
        send(job.id, { kind: "warn",
          message: "khong tim thay model picker de chon " + job.model });
        return false;
      }
      const currentModelLabel = triggerLabel(trigger);
      if (labelMatches(targets, currentModelLabel)) {
        debugLog("selectModel: already on correct model:", currentModelLabel);
        return true;
      }
      debugLog("selectModel: switching from '" + currentModelLabel + "' to '" + job.model + "'");

      trigger.click();
      await sleep(600);
      if (await pickWmsModel(targets)) {
        await sleep(600);
        if (await waitLabelMatches(targets, trigger, 2400)) {
          debugLog("selectModel: da chon '" + job.model + "' qua picker .wms");
          return true;
        }
        await closeModelPopup(trigger);
        send(job.id, { kind: "warn",
          message: "UI chua hien model " + job.model + " sau khi chon (hien tai: '"
            + triggerLabel(trigger) + "')" });
        return false;
      }
      const norms = targets.map(normLabel).filter(Boolean);
      const options = Array.from(document.querySelectorAll(
        '[role="option"], [role="menuitem"], [role="menuitemradio"], li, button, [role="button"]'
      )).filter((el) => visible(el)
        && !trigger.contains(el) && !el.contains(trigger));
      let best = null, bestScore = 0;
      for (const el of options) {
        const n = normLabel(textOf(el));
        if (!n) continue;
        let score = 0;
        if (norms.includes(n)) score += 5;
        else if (norms.some((t) => n.startsWith(t) || n.includes(t))) score += 4;
        else if (norms.some((t) => t.length >= 5 && n.length >= 4 && t.includes(n))) score += 2;
        if (el.closest("form")) score -= 1;
        if (score > bestScore) { best = el; bestScore = score; }
      }
      if (!best) {
        await closeModelPopup(trigger);
        send(job.id, { kind: "warn",
          message: "khong chon duoc model " + job.model + " trong UI (hien tai: '" + currentModelLabel + "'; "
            + "model dang thay: [" + visibleModelNames().join(", ") + "])" });
        return false;
      }
      best.click();
      await sleep(600);
      const ok = labelMatches(targets, triggerLabel(trigger));
      if (!ok) {
        send(job.id, { kind: "warn",
          message: "UI chua hien model " + job.model + " sau khi chon (hien tai: '"
            + triggerLabel(trigger) + "', aria-label: '" + textOf(trigger) + "')" });
      }
      return ok;
    } catch (e) {
      send(job.id, { kind: "warn",
        message: "model select error: " + ((e && e.message) || e) });
      return false;
    }
  }

  // strict=true (dang muon che do thuong): chi tin URL/cache, khong tin DOM/text
  // (nut toggle "temporary chat" luon hien o che do thuong -> false positive).
  function isTemporaryPage(strict) {
    if (pageLoadedAsTemporary) return true;
    if (/[?&]temporary-chat=true/.test(location.search) || /[?&]temporary-chat=true/.test(location.href)) {
      pageLoadedAsTemporary = true;
      return true;
    }
    if (strict) return false;
    const tempEl = document.querySelector(
      '[class*="temporary" i], [data-testid*="temporary" i], [aria-label*="temporary" i], [title*="temporary" i]'
    );
    if (tempEl && visible(tempEl)) return true;
    const bodyText = (document.body && document.body.innerText) || "";
    if (/temporary\s*chat\s*(is\s*on|enabled|bật|đang\s*bật)|cuộc\s*trò\s*chuyện\s*tạm\s*thời|临时对话/i.test(bodyText)) {
      pageLoadedAsTemporary = true;
      return true;
    }
    return false;
  }

  // Chan doan temporary mode: cho thay tin hieu nao dang dung (url/dom/text/cache).
  function temporaryEvidence() {
    let domHit = false, textHit = false;
    try {
      const el = document.querySelector(
        '[class*="temporary" i], [data-testid*="temporary" i], [aria-label*="temporary" i], [title*="temporary" i]'
      );
      domHit = !!(el && visible(el));
    } catch (e) { /* best-effort */ }
    try {
      const bodyText = (document.body && document.body.innerText) || "";
      textHit = /temporary\s*chat\s*(is\s*on|enabled|bật|đang\s*bật)|cuộc\s*trò\s*chuyện\s*tạm\s*thời|临时对话/i.test(bodyText);
    } catch (e) { /* best-effort */ }
    return {
      url: /[?&]temporary-chat=true/.test(location.search)
        || /[?&]temporary-chat=true/.test(location.href),
      dom: domHit, text: textHit, cached: pageLoadedAsTemporary,
      href: location.href,
    };
  }

  function modeUrl(want) {
    return BASE + (want ? "/?temporary-chat=true" : "/");
  }

  // Gioi han so lan tu navigate de doi temporary mode: tranh reload lap vo han
  // khi app tu xoa ?temporary-chat=true khoi URL.
  function allowIdleModeNav() {
    const now = Date.now();
    try {
      const prev = JSON.parse(sessionStorage.getItem(MODE_NAV_KEY) || "{}") || {};
      const recent = prev.at && now - prev.at < MODE_NAV_WINDOW_MS;
      const n = recent ? (prev.n || 0) : 0;
      if (n >= MODE_NAV_MAX) return false;
      sessionStorage.setItem(MODE_NAV_KEY, JSON.stringify({ at: now, n: n + 1 }));
      return true;
    } catch (e) {
      return true;
    }
  }

  // Doi temporary mode truoc khi gui. Tra ve false neu phai navigate lai.
  async function ensureMode(job) {
    try {
      const st = await readStatus();
      if (typeof st.temporary_chat !== "boolean") return true;
      const want = st.temporary_chat;
      if (want === isTemporaryPage(!want)) return true;
      job.mode_retries = (job.mode_retries || 0) + 1;
      if (job.mode_retries > 2) {
        send(job.id, { kind: "warn",
          message: "khong doi duoc temporary_chat=" + want
            + "; tiep tuc voi che do hien tai " + JSON.stringify(temporaryEvidence()) });
        return true;
      }
      try {
        job.raw.mode_retries = job.mode_retries;
        await post("/requeue", { job: job.raw });
      } catch (e) { /* best-effort */ }
      navigating = true;
      location.assign(modeUrl(want));
      return false;
    } catch (e) {
      log("mode check skipped:", (e && e.message) || e);
      return true;
    }
  }

  // Khi ranh: bao dam tab dang o dung temporary mode (khong co job de requeue).
  async function idleModeCheck() {
    const now = Date.now();
    if (now - lastModeCheck < MODE_CHECK_MS) return true;
    lastModeCheck = now;
    try {
      const st = await readStatus();
      if (typeof st.temporary_chat !== "boolean") return true;
      if (st.temporary_chat === isTemporaryPage(!st.temporary_chat)) {
        try { sessionStorage.removeItem(MODE_NAV_KEY); } catch (e) { /* best-effort */ }
        return true;
      }
      if (!allowIdleModeNav()) {
        log("temporary mode khong giu duoc sau " + MODE_NAV_MAX
          + " lan navigate; dung thu lai");
        return true;
      }
      navigating = true;
      location.assign(modeUrl(st.temporary_chat));
      return false;
    } catch (e) {
      log("idle mode check skipped:", (e && e.message) || e);
      return true;
    }
  }

  function isCompletionURL(u) {
    return typeof u === "string" && u.includes("/api/v2/chat/completions");
  }

  function urlOf(input) {
    if (typeof input === "string") return input;
    if (input && typeof input.url === "string") return input.url;
    return String(input);
  }

  // built-in tool cua Qwen (code_interpreter...) phai de Qwen
  // UI tu chay; chi native call cua tool client moi bi chan/tra ve Python.
  function nativeToolName(fc) {
    let n = String((fc && fc.name) || "").trim().toLowerCase();
    for (const p of ["functions.", "default_api."]) {
      if (n.startsWith(p)) n = n.slice(p.length);
    }
    return n;
  }

  function isQwenBuiltinCall(fc) {
    return qwenBuiltinTools.has(nativeToolName(fc));
  }

  // Qwen đôi khi gọi tool bằng native function_call thay vì text fence.
  // Gom name + arguments (Qwen stream cumulative) rồi trả cho proxy để client
  // tự chạy tool; riêng built-in của Qwen (config qwen_builtin_tools) thì để Qwen
  // tự chạy trong sandbox của nó.
  function nativeIdOf(fc) {
    for (const k of ["id", "call_id", "tool_call_id", "index"]) {
      const v = fc[k];
      if (v !== undefined && v !== null && v !== "") return String(v);
    }
    return "";
  }

  function jsonComplete(s) {
    const t = String(s || "").trim();
    if (!t.startsWith("{")) return false;
    try { JSON.parse(t); return true; } catch (e) { return false; }
  }

  function collectNativeCall(job, fc) {
    if (!fc) return;
    const name = typeof fc.name === "string" ? fc.name : "";
    const raw = typeof fc.arguments === "string"
      ? fc.arguments
      : (fc.arguments ? JSON.stringify(fc.arguments) : "");
    if (!Array.isArray(job._nativeCalls)) job._nativeCalls = [];
    /* tach theo id/index neu co; neu khong, tach
       khi arguments truoc da la 1 JSON HOAN CHINH va chunk moi mo dau '{'
       (khong phai cumulative) -> do la call MOI, du cung ten. */
    const cid = nativeIdOf(fc);
    let call = null;
    if (cid) {
      /* id/index trung nhung TEN khac -> call moi (khong ghi de ten) */
      call = job._nativeCalls.find((c) => c.cid === cid
        && (!name || !c.name || c.name === name)) || null;
      if (!call) {
        call = { cid: cid, name: name, arguments: "" };
        job._nativeCalls.push(call);
      }
    } else {
      call = job._nativeCalls[job._nativeCalls.length - 1] || null;
      const newByName = !!(name && call && call.name && call.name !== name);
      const boundary = !!(call && raw && jsonComplete(call.arguments)
        && String(raw).trim().startsWith("{")
        && !String(raw).startsWith(call.arguments));
      if (!call || newByName || boundary) {
        call = { cid: "", name: name, arguments: "" };
        job._nativeCalls.push(call);
      }
    }
    if (name) call.name = name;
    if (!raw) return;
    const prev = call.arguments || "";
    if (!prev) call.arguments = raw;
    else if (raw.startsWith(prev)) call.arguments = raw;   // cumulative
    else call.arguments = prev + raw;                      // incremental
  }

  // Quet can ngoac de tach nhieu JSON object dinh nhau (an toan voi string).
  function splitJsonObjects(s) {
    const out = [];
    const t = String(s || "");
    let depth = 0, start = -1, inStr = false, esc = false;
    for (let i = 0; i < t.length; i++) {
      const ch = t[i];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') { inStr = true; continue; }
      if (ch === "{") { if (depth === 0) start = i; depth++; }
      else if (ch === "}") {
        depth--;
        if (depth === 0 && start >= 0) { out.push(t.slice(start, i + 1)); start = -1; }
      }
    }
    return out;
  }

  async function flushNativeCalls(job) {
    const calls = job._nativeCalls || [];
    job._nativeCalls = [];
    const ready = calls.filter((c) => c && c.name);
    if (!ready.length) return false;
    for (const c of ready) {
      const args = String(c.arguments || "").trim();
      let parts;
      if (!args) {
        parts = ["{}"];
      } else {
        try { JSON.parse(args); parts = [args]; }
        catch (e) {
          parts = splitJsonObjects(args);
          if (parts.length > 1) {
            fwd(job, "warn", "native tool_call '" + c.name + "': arguments la "
              + parts.length + " JSON dinh nhau -> tach thanh call rieng ");
          } else {
            /* giu nguyen chuoi hong de proxy TU CHOI call,
               khong bien thanh {} roi tra cho client. */
            parts = [args];
            fwd(job, "warn", "native tool_call '" + c.name
              + "': arguments khong phai JSON hop le -> proxy se tu choi");
          }
        }
      }
      for (const p of parts) {
        await send(job.id, { kind: "tool_call", name: c.name, arguments: p,
          id: c.cid || undefined });
      }
    }
    settle(job, null);
    return true;
  }

  // Chan doan: delta co du lieu la nhung khong key nao duoc doc -> bao 1 lan
  // (kem phase + keys) de biet Qwen doi shape; xem __qwenUIBridgeDump().
  function noteShape(job, phase, d) {
    if (!job || !d || typeof d !== "object") return;
    const keys = Object.keys(d).filter((k) => k !== "phase"
      && k !== "status" && k !== "role");
    if (!keys.length) return;
    if (!keys.some((k) => k !== "content" && k !== "extra")) return;
    if (partsText(d.content) || partsText(d.reasoning_content)
        || (d.extra && partsText(d.extra.summary_thought))) return;
    const sig = String(phase || "") + "|" + keys.join(",");
    if (!job._shapes) job._shapes = new Set();
    if (job._shapes.has(sig)) return;
    job._shapes.add(sig);
    send(job.id, { kind: "warn",
      message: "delta khong doc duoc phase=" + (phase || "(none)")
        + " keys=" + keys.join(",") });
  }

  async function handleData(job, data) {
    touchJob(job);
    if (!job || job.settled) return;
    if (!data || data === "[DONE]") {
      if (data === "[DONE]" && !(await flushNativeCalls(job))) settle(job, null);
      return;
    }
    let obj;
    try { obj = JSON.parse(data); } catch (e) { return; }
    if (obj.success === false) {
      const d = obj.data || {};
      throw new Error(d.details || d.code || JSON.stringify(obj).slice(0, 300));
    }
    if (obj.ret) throw new Error(JSON.stringify(obj).slice(0, 300));
    if (obj.usage) await send(job.id, { kind: "usage", usage: obj.usage });

    for (const ch of obj.choices || []) {
      const d = ch.delta || ch.message || {};
      const phase = d.phase || "";
      const status = d.status || "";
      const thinkPhase = String(phase || "").toLowerCase();
      const isSummary = thinkPhase.includes("summary");
      const isThinking = !isSummary
        && (thinkPhase === "thinking" || thinkPhase === "think"
            || thinkPhase === "reasoning");

      // 0. Native function_call: Qwen đang gọi tool bằng structured call.
      // Chỉ can thiệp khi prompt có block "# Tool Use" (client khai báo tools);
      // function_call của tool built-in (code_interpreter...) để Qwen tự xử lý.
      if (d.function_call) {
        if (isQwenBuiltinCall(d.function_call)) {
          const bn = nativeToolName(d.function_call);
          if (!job._builtinNoted) job._builtinNoted = new Set();
          if (!job._builtinNoted.has(bn)) {
            job._builtinNoted.add(bn);
            await send(job.id, { kind: "debug",
              message: "de Qwen tu chay built-in tool: " + bn });
          }
        } else if (job.toolsActive) {
          collectNativeCall(job, d.function_call);
        }
        continue;
      }

      // Qwen tự chạy tool và trả kết quả ở role="function" (thường là
      // "Tool X does not exists."). Không forward rác này; trả call đã gom
      // cho client thay vì để Qwen xử lý tiếp.
      if (d.role === "function") {
        if (job.toolsActive) await flushNativeCalls(job);
        continue;
      }

      if (status === "finished" && (phase === "answer" || !phase)
          && job.toolsActive && Array.isArray(job._nativeCalls)
          && job._nativeCalls.length) {
        await flushNativeCalls(job);
        continue;
      }

      // 1. Full Thinking: reasoning_content, hoac content cua phase thinking
      // (string | array parts deu duoc).
      const reasoningText = partsText(d.reasoning_content);
      if (reasoningText) {
        await send(job.id, { kind: "delta", phase: "reasoning", text: reasoningText });
      } else if (isThinking) {
        const thinkText = partsText(d.content);
        if (thinkText) {
          await send(job.id, { kind: "delta", phase: "reasoning", text: thinkText });
        }
      }

      // 2. Summary Thinking:
      if (isSummary) {
        if (status === "finished") {
          job._blockFinished = true;
          continue;
        }
        const extra = d.extra || {};

        // 2a. Title: in đậm từng step title khi xuất hiện
        const rawTitles = (extra.summary_title && extra.summary_title.content !== undefined)
          ? extra.summary_title.content : extra.summary_title;
        const titleList = Array.isArray(rawTitles) ? rawTitles : (rawTitles ? [rawTitles] : []);
        if (titleList.length > 0) {
          if (!job._seenTitles) job._seenTitles = new Set();
          for (const item of titleList) {
            const t = partsText(item).trim();
            if (t && !job._seenTitles.has(t)) {
              job._seenTitles.add(t);
              await send(job.id, {
                kind: "delta",
                phase: "reasoning",
                text: `**${t}**\n\n`
              });
            }
          }
        }

        // 2b. Thought: xử lý cumulative arrays hoặc disjoint blocks
        const thoughtText = joinParts(extra.summary_thought, "\n\n");
        if (typeof job._thinkThought !== "string") job._thinkThought = "";
        if (thoughtText && thoughtText !== job._thinkThought) {
          let delta = "";
          if (job._thinkThought && thoughtText.startsWith(job._thinkThought)) {
            delta = thoughtText.slice(job._thinkThought.length);
            job._thinkThought = thoughtText;
          } else if (job._blockFinished && !thoughtText.startsWith(job._thinkThought)) {
            delta = "\n\n" + thoughtText;
            job._thinkThought = (job._thinkThought ? job._thinkThought + "\n\n" : "") + thoughtText;
          } else if (!job._thinkThought) {
            delta = thoughtText;
            job._thinkThought = thoughtText;
          } else {
            // Tiền tố có khác biệt nhỏ: tìm common prefix
            let p = 0;
            while (p < job._thinkThought.length && p < thoughtText.length
                   && job._thinkThought[p] === thoughtText[p]) {
              p++;
            }
            delta = thoughtText.slice(p);
            job._thinkThought = thoughtText;
          }
          job._blockFinished = false;
          if (delta) {
            await send(job.id, { kind: "delta", phase: "reasoning", text: delta });
          }
        }
        continue;
      }

      // 3. Final Answer: moi phase khong phai thinking/summary deu la answer.
      const isAnswer = !isThinking && !isSummary;
      if (isAnswer) {
        const answerText = partsText(d.content);
        if (answerText) {
          if (answerText.includes("The current content is empty, please regenerate")
              || answerText.includes("当前内容为空，请重新生成")) {
            throw new Error("Qwen error: The current content is empty, please regenerate.");
          }
          await send(job.id, { kind: "delta", phase: "content", text: answerText });
        }
      }

      noteShape(job, phase, d);
    }
  }

  async function pumpFetch(job, resp) {
    const ctype = resp.headers.get("content-type") || "";
    if (!ctype.includes("event-stream")) {
      const text = await resp.text();
      throw new Error("non-SSE (" + resp.status + "): " + text.slice(0, 400));
    }

    let idleTimer = null;
    const resetIdleTimer = () => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        settle(job, new Error("Upstream stream timeout: khong nhan duoc du lieu tu Qwen sau " + (STREAM_IDLE_TIMEOUT_MS / 1000) + "s"));
        /* read() dang treo nen finally khong chay -> cancel o day */
        try { const p = reader.cancel(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* best-effort */ }
      }, STREAM_IDLE_TIMEOUT_MS);
    };
    resetIdleTimer();

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (job.settled) break;
        resetIdleTimer();
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line.startsWith("data:")) continue;
          if (/think|reason|summar/i.test(line)) {
            lastRawDeltas.push(line.slice(0, 500));
            if (lastRawDeltas.length > 20) lastRawDeltas.shift();
          }
          await handleData(job, line.slice(5).trim());
        }
      }
    } finally {
      if (idleTimer) clearTimeout(idleTimer);
      /* job da settle ma stream con treo -> nhả reader */
      if (job && job.settled) {
        try { await reader.cancel(); } catch (e) { /* best-effort */ }
      }
    }
    settle(job, null);
  }

  async function parseSSEText(job, text) {
    for (const raw of String(text || "").split(/\r?\n/)) {
      const line = raw.trim();
      if (!line.startsWith("data:")) continue;
      await handleData(job, line.slice(5).trim());
      if (job.settled) return;
    }
    settle(job, null);
  }

  /* chan request SAI model / THIEU file TRUOC khi gui. */
  function countAttachments(v, depth) {
    if (depth > 5 || v === null || typeof v !== "object") return 0;
    let n = 0;
    if (Array.isArray(v)) {
      for (const x of v.slice(0, 50)) n += countAttachments(x, depth + 1);
      return n;
    }
    for (const k of Object.keys(v)) {
      const val = v[k];
      if (/^(files?|attachments?|file_ids?)$/i.test(k) && Array.isArray(val) && val.length) n += val.length;
      n += countAttachments(val, depth + 1);
    }
    return n;
  }

  // Tra ve ly do chan ("" = cho phep) tu body (string) cua request completion. Dong bo de dung
  // chung cho fetch (sau khi doc body) va XHR (send() khong await duoc). Chi hoat dong theo
  // config strict:
  //  - bridge_require_model: request mang model khac job.model -> chan
  //  - bridge_require_attachment: job co file ma request khong co attachment
  //    VA noi dung chua gui du bang text (content_chars < 95% file) -> chan
  //  - bridge_attachment_block: request CO attachment nhung metadata (ten/size/status/so luong)
  //    lech file proxy da tao -> chan neu kind nam trong danh sach, kind khac chi canh bao
  function strictProblemForBody(job, body) {
    try {
      if (!job || typeof body !== "string" || !body) return "";
      const wantModel = String(job.model || "").split("@think=")[0].trim();
      const checkModel = !!(requireModel && selectModelEnabled !== false && wantModel);
      const checkFile = !!(requireAttachment && job.file_info);
      const checkStale = !!(requireAttachment && !job.file_info && attachmentBlock.has("stale"));
      if (!checkModel && !checkFile && !checkStale) return "";
      const req = JSON.parse(body);
      if (!req || typeof req !== "object") return "";
      const msgs = Array.isArray(req.messages) ? req.messages : [];
      if (checkModel) {
        const norms = modelTargets(job.model).map(normLabel).filter(Boolean);
        const seen = [];
        if (typeof req.model === "string" && req.model) seen.push(req.model);
        const last = msgs.length ? msgs[msgs.length - 1] : null;
        if (last && typeof last === "object") {
          if (Array.isArray(last.models)) {
            for (const x of last.models) if (typeof x === "string" && x) seen.push(x);
          } else if (typeof last.model === "string" && last.model) {
            seen.push(last.model);
          }
        }
        const bad = seen.find((x) => !norms.includes(normLabel(x)));
        if (bad) {
          return "request gui model '" + bad + "' khac job.model '" + wantModel
            + "' (bridge_require_model=true -> chan, khong gui)";
        }
      }
      if (checkFile) {
        if (!countAttachments(req, 0)) {
          const chars = msgs.reduce((n, m) => n + partsText(m && m.content).length, 0);
          const need = Number(job.file_info.size) || 0;
          if (!(need > 0 && chars >= need * 0.95)) {
            return "request KHONG mang file dinh kem (attachments=0, content_chars=" + chars
              + ", file=" + need + " ky tu) -> chan, khong gui cau companion thay noi dung that "
              + "(bridge_require_attachment=true)";
          }
        } else {
          const hard = classifyRequestFiles(job, collectRequestFiles(req))
            .filter((p) => attachmentBlock.has(p.kind));
          if (hard.length) {
            return "metadata file dinh kem lech file proxy da tao: "
              + hard.map((p) => p.msg).join(" | ")
              + " (bridge_attachment_block -> chan, khong gui)";
          }
        }
      } else if (checkStale) {
        const stale = classifyRequestFiles(job, collectRequestFiles(req))
          .filter((p) => p.kind === "stale");
        if (stale.length) {
          return stale.map((p) => p.msg).join(" | ")
            + " (bridge_attachment_block co 'stale' -> chan, khong gui)";
        }
      }
    } catch (e) { /* loi chan doan khong duoc chan app */ }
    return "";
  }

  // Wrapper cho fetch: lay body (string hoac Request.clone) roi dung chung strictProblemForBody.
  async function strictRequestProblem(job, input, init) {
    try {
      if (!job || !(requireModel || requireAttachment)) return "";
      let body = init && init.body;
      if (typeof body !== "string" && input && typeof input.clone === "function") {
        body = await input.clone().text();
      }
      return strictProblemForBody(job, body);
    } catch (e) { /* loi chan doan khong duoc chan app */ }
    return "";
  }

  // Thong tin 1 file dinh kem trong body completion. Key that cua Qwen chua duoc xac nhan
  // nen doc theo nhieu ten key kha di; file0={...} trong log giu mau raw de doi chieu.
  function describeFileEntry(f) {
    const out = { name: "", size: null, status: "", type: "", id: "" };
    if (typeof f === "string") { out.id = f.slice(0, 60); return out; }
    if (!f || typeof f !== "object") return out;
    const scan = (o, depth) => {
      if (!o || typeof o !== "object" || depth > 3) return;
      for (const k of Object.keys(o)) {
        const v = o[k];
        const lk = k.toLowerCase();
        if (typeof v === "string") {
          if (!out.name && /^(name|filename|file_name|title)$/.test(lk)) out.name = v;
          else if (!out.status && lk === "status") out.status = v;
          else if (!out.type && /^(file_type|content_type|mime|mime_type)$/.test(lk)) out.type = v;
          else if (!out.id && /^(id|file_id)$/.test(lk)) out.id = v;
        } else if (typeof v === "number") {
          if (out.size === null && /^(size|file_size|bytes|content_length)$/.test(lk)) out.size = v;
        } else if (v && typeof v === "object" && !Array.isArray(v)) {
          scan(v, depth + 1);
        }
      }
    };
    scan(f, 0);
    return out;
  }

  function describeFileBrief(d) {
    return (d.name || "?") + "|" + (d.size === null ? "?" : d.size) + "B|"
      + (d.status || "?") + (d.type ? "|" + d.type : "");
  }

  // Gom file dinh kem (key files/attachments/file_ids) trong body, bo trung.
  function collectRequestFiles(req) {
    const out = [];
    const seen = new Set();
    const walk = (v, depth) => {
      if (depth > 5 || v === null || typeof v !== "object") return;
      if (Array.isArray(v)) { v.slice(0, 50).forEach((x) => walk(x, depth + 1)); return; }
      for (const k of Object.keys(v)) {
        const val = v[k];
        if (/^(files?|attachments?|file_ids?)$/i.test(k) && Array.isArray(val)) {
          for (const f of val.slice(0, 20)) {
            const d = describeFileEntry(f);
            const key = d.id || (d.name + "|" + d.size);
            if (seen.has(key)) continue;
            seen.add(key);
            out.push(d);
          }
        } else {
          walk(val, depth + 1);
        }
      }
    };
    walk(req, 0);
    return out;
  }

  // So file request that su gui voi file proxy da tao. Tra ve [{kind, msg}] (rong = on).
  // kind: name | size | status (dang upload/pending) | status_unknown (khong biet nghia: chi canh bao)
  //       | count | stale. Caller chan theo bridge_attachment_block, kind khac chi canh bao.
  function classifyRequestFiles(job, files) {
    const problems = [];
    const add = (kind, msg) => problems.push({ kind, msg });
    const exp = job && job.file_info;
    if (!exp) {
      if (files.length) {
        add("stale", "job KHONG co file nhung request mang " + files.length
          + " attachment (file cu con sot trong composer/trang?): "
          + files.map(describeFileBrief).join("; "));
      }
      return problems;
    }
    if (!files.length) return problems;   // truong hop nay da bao rieng (missing)
    // Chi dem entry co ten/size (entry chi co id chuoi la cach khac bieu dien cung 1 file).
    const detailed = files.filter((f) => f.name || typeof f.size === "number");
    const nFiles = detailed.length || files.length;
    if (nFiles !== 1) {
      add("count", "request mang " + nFiles + " file (ky vong 1): "
        + files.map(describeFileBrief).join("; "));
    }
    const expName = String(exp.filename || "");
    const named = files.filter((f) => f.name);
    // paste-text: Qwen tu dat ten document -> khong so ten duoc (van so size).
    const skipName = !!(job && job._attachStrategy === "paste-text");
    if (!skipName && expName && named.length && !named.some((f) => f.name === expName)) {
      add("name", "ten file gui di [" + named.map((f) => f.name).join(", ")
        + "] khac file proxy tao '" + expName + "' (DINH NHAM FILE?)");
    }
    const expBytes = Number(exp.bytes) || 0;
    const sized = files.filter((f) => typeof f.size === "number");
    if (expBytes && sized.length && !sized.some((f) => f.size === expBytes)) {
      add("size", "kich thuoc file gui di [" + sized.map((f) => f.size).join(", ")
        + "] != " + expBytes + " byte proxy da ghi (FILE THIEU/DU NOI DUNG? "
        + "neu Qwen tinh size khac thi bo 'size' khoi bridge_attachment_block)");
    }
    const OK_STATUS = /^(uploaded|success|done|finished|complete|completed)$/i;
    const PENDING_STATUS = /^(uploading|pending|processing|parsing|queued|queueing|waiting|loading|in_progress|in-progress)$/i;
    const notOk = files.filter((f) => f.status && !OK_STATUS.test(f.status));
    const pending = notOk.filter((f) => PENDING_STATUS.test(f.status));
    const unknown = notOk.filter((f) => !PENDING_STATUS.test(f.status));
    if (pending.length) {
      add("status", "trang thai file chua xong: " + pending.map(describeFileBrief).join("; "));
    }
    if (unknown.length) {
      add("status_unknown", "trang thai file la (khong thuoc nhom da biet, chi canh bao): "
        + unknown.map(describeFileBrief).join("; "));
    }
    return problems;
  }

  // API cu: chi tra danh sach thong bao.
  function checkRequestFiles(job, files) {
    return classifyRequestFiles(job, files).map((p) => p.msg);
  }

  // Hinh dang (key + kieu/gia tri cat ngan) cua 1 entry file trong request, de doi chieu key THAT
  // cua Qwen voi describeFileEntry (hien chi doan ten key).
  function fileShape(v, depth) {
    if (v === null) return null;
    if (v === undefined) return "undefined";
    if (typeof v === "string") return "str:" + v.slice(0, 40);
    if (typeof v === "number" || typeof v === "boolean") return v;
    if (depth > 3) return "...";
    if (Array.isArray(v)) return v.slice(0, 3).map((x) => fileShape(x, depth + 1));
    const out = {};
    for (const k of Object.keys(v).slice(0, 20)) out[k] = fileShape(v[k], depth + 1);
    return out;
  }

  // Chan doan chinh xac: request completion cua app co kem file/attachment khong?
  // (heuristic DOM co the bao sai). Chi doc body, khong sua request, khong throw.
  async function inspectCompletionRequest(job, input, init) {
    try {
      let body = init && init.body;
      if (typeof body !== "string" && input && typeof input.clone === "function") {
        body = await input.clone().text();
      }
      if (typeof body !== "string" || !body) return;
      const req = JSON.parse(body);
      if (!req || typeof req !== "object") return;
      const found = [];
      const rawFiles = [];     // entry file THAT (toi da 3) de log shape
      let fileSample = null;   // mau file dinh kem dau tien ()
      const walk = (v, path, depth) => {
        if (depth > 5 || v === null || typeof v !== "object") return;
        if (Array.isArray(v)) {
          v.slice(0, 50).forEach((x, i) => walk(x, path + "[" + i + "]", depth + 1));
          return;
        }
        for (const k of Object.keys(v)) {
          const val = v[k];
          if (/^(files?|attachments?|file_ids?)$/i.test(k)
              && Array.isArray(val) && val.length) {
            found.push(path + "." + k + "=" + val.length);
            for (const x of val) if (rawFiles.length < 3) rawFiles.push(x);
            if (!fileSample && val[0] && typeof val[0] === "object") {
              fileSample = {};
              for (const fk of Object.keys(val[0]).slice(0, 12)) {
                const fv = val[0][fk];
                if (typeof fv === "string") fileSample[fk] = fv.slice(0, 80);
                else if (typeof fv === "number" || typeof fv === "boolean") fileSample[fk] = fv;
              }
            }
          }
          walk(val, path + "." + k, depth + 1);
        }
      };
      walk(req, "$", 0);
      const msgs = Array.isArray(req.messages) ? req.messages : [];
      const chars = msgs.reduce((n, m) => n + partsText(m && m.content).length, 0);
      const last = msgs.length ? msgs[msgs.length - 1] : null;
      const reqModel = typeof req.model === "string" ? req.model : "";
      const msgModel = last && typeof last === "object"
        ? (Array.isArray(last.models) ? last.models.join(",") : String(last.model || "")) : "";
      const wantModel = String((job && job.model) || "").split("@think=")[0].trim();
      const modelBad = selectModelEnabled !== false && !!(wantModel && reqModel
        && normLabel(reqModel) !== normLabel(wantModel));
      let fcTxt = "";
      let topTxt = "";
      try {
        const msgFc = last && last.feature_config;
        fcTxt = JSON.stringify(msgFc || req.feature_config || null).slice(0, 1500);
        if (msgFc && req.feature_config) {
          fcTxt += " top_feature_config=" + JSON.stringify(req.feature_config).slice(0, 600);
        }
        const sc = {};
        for (const k of Object.keys(req)) {
          const v = req[k];
          if (typeof v === "number" || typeof v === "boolean") sc[k] = v;
          else if (typeof v === "string" && k !== "model") sc[k] = v.slice(0, 40);
        }
        topTxt = JSON.stringify(sc).slice(0, 400);
      } catch (e) { /* bo qua */ }
      const info = "completion request: model=" + reqModel + " msg_models=" + msgModel
        + " want=" + wantModel + " mode=" + ((job && job.mode) || "-")
        + " vis=" + visState() + " feature_config=" + fcTxt
        + " top=" + topTxt
        + " keys=" + Object.keys(req).join(",")
        + " messages=" + msgs.length + " content_chars=" + chars
        + " last_msg_keys=" + (last && typeof last === "object" ? Object.keys(last).join(",") : "-")
        + " attachments=[" + found.join(", ") + "]"
        + (fileSample ? " file0=" + JSON.stringify(fileSample) : "");
      const missing = !!(job && job.file_info) && !found.length;
      const files = collectRequestFiles(req);
      const verdict = checkRequestFiles(job, files);
      // Shape metadata chua duoc xac nhan voi request that: log 1 lan/job, canh bao neu khong doc
      // duoc gi, va doi chieu ten voi attachment card DOM (heuristic, chi canh bao).
      if (rawFiles.length && job && !job._shapeLogged) {
        job._shapeLogged = true;
        let shapeTxt = "";
        try { shapeTxt = JSON.stringify(rawFiles.map((x) => fileShape(x, 0))).slice(0, 1800); } catch (e) { /* bo qua */ }
        send(job.id, { kind: "debug",
          message: "file shape (entry that trong request, chuoi cat 40 ky tu): " + shapeTxt });
      }
      const shapeWarn = [];
      if (files.length && !files.some((f) => f.name || f.size !== null || f.status)) {
        shapeWarn.push("SHAPE FILE LA: khong doc duoc name/size/status tu entry file -> metadata "
          + "KHONG duoc doi chieu/chan (xem dong 'file shape', sua describeFileEntry)");
      }
      const cards = job && Array.isArray(job._cardTexts) ? job._cardTexts : [];
      const namedFiles = files.filter((f) => f.name);
      if (cards.length && namedFiles.length
          && !namedFiles.some((f) => cards.some((c) => c.indexOf(f.name.slice(0, 20)) >= 0))) {
        shapeWarn.push("ten file trong request [" + namedFiles.map((f) => f.name).join(", ")
          + "] khong khop attachment card DOM [" + cards.join("; ") + "] (heuristic DOM)");
      }
      const expInfo = (job && job.file_info)
        ? " expect=" + (job.file_info.filename || "?") + "/" + (job.file_info.bytes || "?")
          + "B/sha256:" + String(job.file_info.sha256 || "?").slice(0, 12)
          + " companion_len=" + String(job.attachment_prompt || "").length
        : " expect=no-file";
      const filesInfo = " files=[" + files.map(describeFileBrief).join("; ") + "]";
      const pre = modelBad ? "MODEL KHAC YEU CAU (request gui '" + reqModel
        + "' != job '" + wantModel + "'): " : "";
      const problemTxt = (verdict.length
        ? "FILE DINH KEM BAT THUONG: " + verdict.join(" | ") + " :: " : "")
        + (shapeWarn.length ? "FILE SHAPE/DOM: " + shapeWarn.join(" | ") + " :: " : "");
      send(job.id, { kind: (missing || modelBad || verdict.length || shapeWarn.length) ? "warn" : "debug",
        message: pre + problemTxt
          + (missing ? "request gui di KHONG thay file/attachment: " + info : info)
          + filesInfo + expInfo });
    } catch (e) {
      debugLog("inspectCompletionRequest skipped:", (e && e.message) || e);
    }
  }

  // Hook fetch: bat response cua chinh app, khong can doc DOM.
  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    /* giu jobRef TU LUC gui request. Truoc day
       `current` bi doc lai SAU await -> response cua job A gan nham cho job B. */
    const jobRef = current;
    const isCompletion = isCompletionURL(urlOf(input));
    if (jobRef && !jobRef.attached && !jobRef.settled && isCompletion) {
      const lostMsg = checkInlineIntegrity(jobRef, init);
      if (lostMsg) {
        learnInlineLimit(String(jobRef.prompt || "").length);
        const lostErr = new Error("Qwen UI " + lostMsg + " (se dung duong attach file o lan retry)");
        settle(jobRef, lostErr);
        throw lostErr;
      }
      if (forceRequestModel) {
        const forced = rewriteRequestModel(jobRef, init);
        if (forced) init = forced;
      }
      if (forceRequestMode) {
        const forcedMode = rewriteRequestMode(jobRef, init);
        if (forcedMode) init = forcedMode;
      }
      if (jobRef.session && jobRef.session.mode === "fork") {
        const forked = rewriteRequestSession(jobRef, init);
        if (!forked) {
          const forkErr = new Error("session fork: khong ghi de duoc parent_id trong body completion");
          forkErr.sessionLost = true;
          fwd(jobRef, "warn", forkErr.message);
          settle(jobRef, forkErr);
          throw forkErr;
        }
        init = forked;
      }
      /* strict -> chan request sai model / thieu file. */
      const strictMsg = await strictRequestProblem(jobRef, input, init);
      if (strictMsg) {
        const strictErr = new Error("Qwen UI request bi chan: " + strictMsg);
        fwd(jobRef, "warn", strictErr.message);
        settle(jobRef, strictErr);
        throw strictErr;
      }
      inspectCompletionRequest(jobRef, input, init);
      if (jobRef.session) announceSession(jobRef, input, init);
    }
    const resp = await origFetch.call(this, input, init);
    try {
      if (jobRef && isCompletion && !jobRef.attached && !jobRef.settled
          && current === jobRef) {
        jobRef.attached = true;
        pageSent = true;
        if (typeof jobRef._clearSendTimer === "function") jobRef._clearSendTimer();
        const clone = resp.clone();
        pumpFetch(jobRef, clone).catch((e) => settle(jobRef, e));
      } else if (jobRef && isCompletion) {
        debugLog("bo response completion cua job cu " + jobRef.id
          + " (settled=" + jobRef.settled + ", current="
          + ((current && current.id) || "null") + ")");
      }
    } catch (e) { /* khong anh huong app */ }
    return resp;
  };

  // Hook XHR phong khi app doi sang XHR.
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__qwenURL = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const job = current;   // giu job tu luc gui 
    if (job && !job.attached && !job.settled && isCompletionURL(this.__qwenURL)) {
      /* XHR cung phai qua guard nhu fetch: mat noi dung inline / metadata file lech / sai model. */
      if (typeof body === "string") {
        const xhrLost = checkInlineIntegrity(job, { body });
        const xhrStrict = xhrLost ? "" : strictProblemForBody(job, body);
        if (xhrLost || xhrStrict) {
          if (xhrLost) learnInlineLimit(String(job.prompt || "").length);
          const xErr = new Error(xhrLost
            ? "Qwen UI " + xhrLost + " (XHR; se dung duong attach file o lan retry)"
            : "Qwen UI request bi chan: " + xhrStrict + " (XHR)");
          fwd(job, "warn", xErr.message);
          settle(job, xErr);
          throw xErr;
        }
        inspectCompletionRequest(job, null, { body });
        if (job.session && job.session.mode === "fork") {
          const fErr = new Error("session fork: khong ghi de duoc parent_id qua XHR");
          fErr.sessionLost = true;
          fwd(job, "warn", fErr.message);
          settle(job, fErr);
          throw fErr;
        }
        if (job.session) announceSession(job, null, { body });
      } else if (!xhrBodyWarned) {
        xhrBodyWarned = true;
        fwd(job, "warn", "XHR completion body khong phai string (" + typeof body
          + "): khong guard/doi chieu duoc attachment");
      }
      job.attached = true;
      pageSent = true;
      if (typeof job._clearSendTimer === "function") job._clearSendTimer();
      /* send timer DA bi clear o tren, nen neu
         khong xu ly error/abort/timeout thi job treo vinh vien -> active
         khong giam -> worker khong nhan job moi du ping van bao song. */
      let done = false;
      let gotLoad = false;   /* load da nhan response hop le (parseSSEText se settle) */
      const clearWatchdog = () => {
        if (job._xhrTimer) { clearTimeout(job._xhrTimer); job._xhrTimer = null; }
      };
      const finish = (err) => {
        if (done || job.settled || !err) return;
        done = true;
        clearWatchdog();
        settle(job, err);
      };
      const armWatchdog = () => {
        clearWatchdog();
        job._xhrTimer = setTimeout(() => {
          finish(new Error("XHR completion timeout: khong co du lieu sau "
            + (STREAM_IDLE_TIMEOUT_MS / 1000) + "s"));
        }, STREAM_IDLE_TIMEOUT_MS);
      };
      armWatchdog();
      this.addEventListener("progress", armWatchdog);
      this.addEventListener("load", () => {
        clearWatchdog();
        try {
          const txt = String(this.responseText || "");
          const ct = String(this.getResponseHeader("content-type") || "");
          if (this.status >= 400) {
            finish(new Error("XHR completion HTTP " + this.status + ": " + txt.slice(0, 300)));
            return;
          }
          if (ct && !ct.includes("event-stream")) {
            finish(new Error("XHR non-SSE (" + this.status + "): " + txt.slice(0, 300)));
            return;
          }
          gotLoad = true;
          parseSSEText(job, txt).catch((e) => finish(e));
        } catch (e) { finish(e); }
      });
      this.addEventListener("error", () => finish(new Error("XHR completion loi mang (error)")));
      this.addEventListener("abort", () => finish(new Error("XHR completion bi abort")));
      this.addEventListener("timeout", () => finish(new Error("XHR completion timeout")));
      this.addEventListener("loadend", () => {
        /* loadend luon chay ngay sau load; parseSSEText la async nen KHONG
           duoc bao loi o day khi response da hop le (truoc day no giet job
           thanh cong truoc khi SSE duoc parse xong). */
        if (gotLoad) return;
        finish(new Error("XHR completion ket thuc ma khong tra du lieu hop le"));
      });
    }
    return origSend.apply(this, arguments);
  };

  function visible(el) {
    if (!el) return false;
    const r = el.getClientRects();
    if (!r.length) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none";
  }

  function findComposers() {
    const nodes = Array.from(document.querySelectorAll(
      'textarea, [contenteditable="true"], [role="textbox"]'
    ));
    return nodes.filter((el) => {
      if (!visible(el)) return false;
      if (el.disabled || el.readOnly) return false;
      if (el.getAttribute("aria-disabled") === "true") return false;
      return true;
    });
  }

  async function waitComposer() {
    const deadline = Date.now() + COMPOSER_WAIT_MS;
    while (Date.now() < deadline) {
      const list = findComposers();
      if (list.length) {
        // Uu tien o co placeholder lien quan chat/message.
        const scored = list.map((el) => {
          const ph = (el.getAttribute("placeholder") || el.getAttribute("aria-label") || "").toLowerCase();
          const score = /ask|message|chat|qwen|nhap|tin nhan|prompt/.test(ph) ? 1 : 0;
          return { el, score };
        });
        scored.sort((a, b) => b.score - a.score);
        return scored[0].el;
      }
      await sleep(200);
    }
    throw new Error("khong tim thay o nhap chat (composer)");
  }

  function setComposer(el, text) {
    el.focus();
    if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, text);
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: text,
      }));
      return;
    }
    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, text);
    else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function hasChatMessages() {
    const nodes = document.querySelectorAll(
      '[class*="message" i], [class*="bubble" i], [data-message-id], [class*="chat-item" i]'
    );
    return Array.from(nodes).some((el) => visible(el) && (el.innerText || el.textContent || "").trim().length > 10);
  }

  function findNewChatButton() {
    const rx = /new\s*chat|new\s*conversation|chat\s*mới|cuộc\s*trò\s*chuyện\s*mới|tạo\s*cuộc|new\s*chat/i;
    const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
    let best = null, bestScore = 0;
    for (const el of nodes) {
      if (!visible(el)) continue;
      const label = (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "").trim();
      let score = 0;
      if (rx.test(label)) score += 3;
      if (el.tagName === "A" && el.getAttribute("href") === "/") score += 1;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return bestScore >= 2 ? best : null;
  }

  async function tryNewChat(job) {
    if (!NEW_CHAT_EACH_JOB) return;
    const st = await readStatus();
    const wantTemp = st && st.temporary_chat;
    if (wantTemp && temporaryReloadEnabled) {
      // Temporary chat khong co nut New Chat: cach sach context la tai lai trang.
      if (!isTemporaryPage(false)) return true;   // ensureMode se dua tab ve temporary
      if (!pageDirty()) {
        debugLog("temporary chat: trang sach, khong can reload");
        return true;
      }
      if (((job && job.fresh_reloads) || 0) >= 1) {
        if (job) send(job.id, { kind: "warn",
          message: "da reload temporary chat nhung trang van co tin nhan; tiep tuc (context co the bi lan)" });
        return true;
      }
      return !(await reloadFreshTemporary(job));   // false = dang navigate
    }
    const btn = findNewChatButton();
    if (!btn) {
      const hasMsg = pageDirty();
      const warnMsg = "khong tim thay nut New Chat" +
        (wantTemp ? " trong temporary chat mode" : "") +
        (hasMsg ? " (CANH BAO: cuoc tro chuyen hien tai DA CO tin nhan, context co the bi lan!)" : " (trang hien tai dang rong)");
      log(warnMsg);
      if (job) send(job.id, { kind: hasMsg ? "warn" : "debug", message: warnMsg });
      return;
    }
    if (wantTemp && btn.tagName === "A" && btn.getAttribute("href") === "/") {
      // Thu tim nut New Chat khac (khong phai the <a> co href=/)
      const otherBtn = Array.from(document.querySelectorAll('button, [role="button"]')).find((el) => {
        if (!visible(el) || el === btn || el.contains(btn) || btn.contains(el)) return false;
        const label = (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "").trim();
        return /new\s*chat|new\s*conversation|chat\s*mới|tạo\s*cuộc/i.test(label);
      });
      if (otherBtn) {
        debugLog("tim thay nut New Chat thay the trong temporary chat:", textOf(otherBtn));
        otherBtn.click();
        await sleep(1200);
        return;
      }
      const hasMsg = pageDirty();
      const warnMsg = "dang o temporary chat mode; bo qua nut New Chat co href=/ de tranh thoat temporary" +
        (hasMsg ? " (CANH BAO: cuoc tro chuyen hien tai DA CO tin nhan, context co the bi lan!)" : " (phien hien tai chua co tin nhan)");
      log(warnMsg);
      if (job) send(job.id, { kind: hasMsg ? "warn" : "debug", message: warnMsg });
      return;
    }
    debugLog("clicking New Chat button:", textOf(btn));
    btn.click();
    await sleep(1200);
  }

  // Trang hien tai co the con tin nhan cu khong? Khong tin mu heuristic DOM:
  //  - da gui job tren trang nay  => chac chan co tin nhan
  //  - trang vua duoc bridge reload => sach (bo qua heuristic, tranh reload lap)
  //  - con lai: heuristic hasChatMessages() (nguoi dung co the da chat tay)
  function pageDirty() {
    if (pageUsed) return true;
    if (freshByBridge) return false;
    return hasChatMessages();
  }

  // Requeue job roi tai lai /?temporary-chat=true. Tra ve true neu da bat dau
  // navigate (caller phai dung ngay), false neu khong reload duoc (chay tiep).
  async function reloadFreshTemporary(job) {
    const n = ((job && job.fresh_reloads) || 0) + 1;
    try {
      job.raw.fresh_reloads = n;
      const rq = await post("/requeue", { job: job.raw });
      if (!rq || rq.ok !== true) {
        throw new Error("bridge tu choi /requeue: " + JSON.stringify(rq).slice(0, 120));
      }
    } catch (e) {
      send(job.id, { kind: "warn",
        message: "khong requeue duoc job de reload temporary chat: "
          + ((e && e.message) || e) + "; tiep tuc khong reload" });
      return false;
    }
    job.fresh_reloads = n;
    await send(job.id, { kind: "debug",
      message: "reload temporary chat de lam sach context (pageUsed=" + pageUsed
        + ", freshByBridge=" + freshByBridge + ")" });
    try { sessionStorage.setItem(FRESH_RELOAD_KEY, String(Date.now())); } catch (e) { /* best-effort */ }
    navigating = true;
    location.assign(modeUrl(true));
    return true;
  }

  function findSendButton(composer) {
    const scope = composer.closest("form") || document;
    const nodes = Array.from(scope.querySelectorAll('button, [role="button"]'));
    const rx = /send|gửi|gui|submit|arrow/i;
    let best = null, bestScore = 0;
    for (const el of nodes) {
      if (!visible(el)) continue;
      if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
      if (isStopButton(el)) continue;   // dang generate: nut Stop khong phai Send
      const label = (el.getAttribute("aria-label") || el.getAttribute("title")
        || el.getAttribute("data-testid") || el.textContent || "").toLowerCase();
      let score = 0;
      if (el.type === "submit") score += 3;
      if (rx.test(label)) score += 3;
      if (el.querySelector("svg")) score += 1;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return bestScore >= 3 ? best : null;
  }

  function pressEnter(el) {
    const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true };
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keypress", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
  }

  async function submitPrompt(composer, prompt, job) {
    await ensureAlive(job, "truoc khi dien prompt");
    saveJobState(job, "typing_prompt", "Setting prompt text");
    setComposer(composer, prompt);
    await sleep(350);
    /* thu lai 1 lan bang setComposerClean truoc
       khi ket luan nhap that bai; KHONG duoc thay bang cau mac dinh. */
    if (!composerText(composer).trim() && String(prompt || "").trim()) {
      /* job co the vua bi huy trong 350ms cho o tren
         -> KHONG dien lai prompt cu. Hoi proxy that (bo cache) ngay truoc thao tac UI. */
      await ensureAlive(job, "truoc khi dien lai prompt", true);
      setComposerClean(composer, prompt);
      await sleep(350);
    }
    {
      const afterLen = composerText(composer).trim().length;
      const nodes = findActiveAttachmentElements(composer).length;
      if (job) send(job.id, { kind: "debug",
        message: "inline prompt: len=" + String(prompt).length + " composerAfter=" + afterLen
          + " attachmentNodes=" + nodes + " sendBtn=" + !!findSendButton(composer) });
      if (afterLen === 0 && String(prompt).trim().length > 2000) {
        // Qwen doi text thanh document card; gui companion-only se mat noi dung
        // (log that: content_chars=119 attachments=[]). Fail de Python retry, lan
        // sau di duong attach file.
        learnInlineLimit(String(prompt).length);
        throw new Error("Qwen UI da doi prompt inline (" + String(prompt).length
          + " ky tu) thanh document; se dung duong attach file o lan retry");
      }
    }
    // UI tu doi text dai vua nhap thanh attachment card va lam rong composer:
    if (!composerText(composer).trim()) {
      /* chi duoc dien companion khi CO file
         dinh kem that. Khong co file -> bao loi de Python retry, tuyet doi
         khong gui "Please read the text and action" thay prompt cua user. */
      const hasAtt = !!(job && job.file_info)
        && findActiveAttachmentElements(composer).length > 0;
      if (!hasAtt) {
        throw new Error("khong dien duoc prompt vao composer (UI khong nhan text); "
          + "khong co attachment -> khong gui thay bang cau mac dinh");
      }
      const companion = (job && job.attachment_prompt) || attachmentPrompt
        || ATTACHMENT_COMPANION_PROMPT;
      log("composer empty after attaching file; inserting companion prompt:", companion);
      await ensureAlive(job, "truoc khi dien companion", true);
      setComposerClean(composer, companion);
      await sleep(350);
      if (!composerText(composer).trim()) {
        const warnMsg = "CANH BAO: Composer bi rong sau khi attach file va dien companion!";
        log(warnMsg);
        if (job) send(job.id, { kind: "warn", message: warnMsg });
      }
    }
    saveJobState(job, "submitting", "Waiting for send button");
    // Truoc day chi thu 1 lan sau 350ms: neu Send chua enable (UI con render hoac
    // dang generate) thi prompt dung luon trong composer. Dung vong lap co retry.
    let sent = await sendPrompt(composer, 60000, null, job);
    if (!sent && job && !job.settled && !job.attached
        && (await waitCaptchaClear(job, "luc bam Send"))) {
      sent = await sendPrompt(composer, 60000, null, job);   // captcha vua giai xong
    }
    if (!sent && job && !job.settled && !job.attached) {
      send(job.id, { kind: "warn",
        message: "khong xac nhan duoc da bam Send (nut Send khong san sang) | " + sendDiag(job, composer) });
    }
  }

  function composerText(el) {
    if (!el) return "";
    if (el.isContentEditable) return String(el.innerText || el.textContent || "");
    return String(el.value || "");
  }

  // Chụp nhanh các node trông giống attachment để biết paste/upload có tạo file mới.
  function attachmentSnapshot() {
    const sel = '[class*="file" i], [class*="attach" i], [class*="upload" i], '
      + '[class*="document" i], [class*="doc" i], [class*="chip" i], '
      + '[data-testid*="file" i], [data-testid*="attach" i], '
      + '[data-testid*="document" i]';
    return new Set(Array.from(document.querySelectorAll(sel)).filter(visible));
  }

  // Tìm các element attachment (file, document card, chip) đang hiện hữu
  function findActiveAttachmentElements(composer) {
    const scope = composer?.closest("form") || composer?.parentElement?.parentElement || document;
    const sel = '[class*="file" i], [class*="attach" i], [class*="upload" i], '
      + '[class*="document" i], [class*="doc" i], [class*="chip" i], '
      + '[data-testid*="file" i], [data-testid*="attach" i], '
      + '[data-testid*="document" i]';
    return Array.from(scope.querySelectorAll(sel)).filter((el) => {
      if (!visible(el)) return false;
      const isBtn = el.tagName === "BUTTON" || el.getAttribute("role") === "button";
      const txt = (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "").toLowerCase().trim();
      // Loại trừ nút bấm đính kèm (button upload file)
      if (isBtn && /^(attach|đính kèm|dính kèm|upload)/i.test(txt)) return false;
      return true;
    });
  }

  // Chờ Qwen tạo attachment từ text vừa paste. Ngoài việc xuất hiện node mới,
  // còn dấu hiệu text trong composer bị rút ngắn (paste -> document).
  async function waitForAttachment(composer, before, pastedLen, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const added = Array.from(attachmentSnapshot()).some((n) => !before.has(n));
      if (added) return true;
      if (pastedLen > 500) {
        const now = composerText(composer).trim().length;
        if (now < Math.max(200, pastedLen / 2)) return true;
      }
      await sleep(400);
    }
    return false;
  }

  // Xóa sạch composer (contenteditable lẫn input/textarea).
  function clearComposer(el) {
    el.focus();
    if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
      if (composerText(el).trim()) {
        el.textContent = "";
        el.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          inputType: "deleteContentBackward",
        }));
      }
      return;
    }
    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, "");
    else el.value = "";
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Thay text cũ bằng prompt ngắn; editor đôi khi giữ text cũ sau lần đầu nên
  // kiểm tra độ dài và xóa lại nếu cần.
  function setComposerClean(el, text) {
    clearComposer(el);
    setComposer(el, text);
    if (composerText(el).trim().length > text.length + 200) {
      clearComposer(el);
      setComposer(el, text);
    }
  }

  // Qwen UI biến text dài được paste thành document/txt. Mô phỏng đúng thao tác
  // Ctrl-V thay vì chèn file bằng tay (React thường bỏ qua file inject).
  async function pasteLongText(composer, text) {
    composer.focus();
    try {
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      composer.dispatchEvent(new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true,
      }));
    } catch (e) {
      log("paste event failed:", e);
    }
    await sleep(2000);
  }

  // UTF-8 byte length + sha256 (hex) cua text; sha256 = "" neu crypto.subtle khong co.
  async function fingerprintText(text) {
    const bytes = new TextEncoder().encode(text);
    let sha = "";
    try {
      if (window.crypto && crypto.subtle) {
        const d = await crypto.subtle.digest("SHA-256", bytes);
        sha = Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, "0")).join("");
      }
    } catch (e) { /* chi so byte */ }
    return { bytes: bytes.length, sha256: sha };
  }

  // Noi dung extension lay tu /file/<ten> co khop file Python da ghi khong (bytes + sha256).
  // Tra ve false khi LECH (attachLongPrompt chan neu job.blockFileMismatch), true khi khop hoac
  // khong du du lieu de doi chieu.
  async function verifyPromptFile(job, fileInfo, text) {
    try {
      if (!fileInfo || typeof fileInfo._inlineText === "string") return true;
      const name = fileInfo.filename || "?";
      const wantBytes = Number(fileInfo.bytes) || 0;
      const wantSha = String(fileInfo.sha256 || "");
      if (!wantBytes && !wantSha) {
        fwd(job, "debug", "file prompt " + name + ": proxy khong gui bytes/sha256, bo qua doi chieu");
        return true;
      }
      const fp = await fingerprintText(text);
      const bad = [];
      if (wantBytes && fp.bytes !== wantBytes) bad.push("bytes " + fp.bytes + " != " + wantBytes);
      if (wantSha && fp.sha256 && fp.sha256 !== wantSha) {
        bad.push("sha256 " + fp.sha256.slice(0, 12) + " != " + wantSha.slice(0, 12));
      }
      if (bad.length) {
        fwd(job, "warn", "FILE PROMPT LECH so voi file Python da ghi (" + name + "): "
          + bad.join(", ") + " -> noi dung dinh kem co the sai/thieu");
        return false;
      }
      fwd(job, "debug", "file prompt khop file Python: " + name + " bytes=" + fp.bytes
        + " sha256=" + (fp.sha256 ? fp.sha256.slice(0, 12) : "(khong co subtle)")
        + (fileInfo.lines ? " lines=" + fileInfo.lines : ""));
      return true;
    } catch (e) {
      debugLog("verifyPromptFile skipped:", (e && e.message) || e);
      return true;
    }
  }

  // Text hien thi cua cac attachment card dang co trong composer (heuristic DOM).
  function attachmentTexts(composer) {
    try {
      return findActiveAttachmentElements(composer).slice(0, 8).map((el) =>
        String(el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "")
          .replace(/\s+/g, " ").trim().slice(0, 70)).filter(Boolean);
    } catch (e) { return []; }
  }

  function reportStaleAttachments(job, composer) {
    const stale = attachmentTexts(composer)
      .filter((t) => /qwen_prompt_|\.(txt|md|log|json|csv|pdf)\b/i.test(t));
    if (stale.length) {
      fwd(job, "warn", "composer da co attachment TRUOC khi dinh kem file moi "
        + "(file cu con sot? heuristic DOM): [" + stale.join("; ") + "]");
    }
  }

  function reportAttachmentCards(job, composer, used, fname) {
    const t = attachmentTexts(composer);
    if (job) job._cardTexts = t;   // inspectCompletionRequest doi chieu ten file voi card
    fwd(job, "debug", "attachment cards sau '" + used + "': n=" + t.length
      + " [" + t.join("; ") + "] expect=" + fname);
  }

  async function fetchPromptText(fileInfo) {
    if (fileInfo && typeof fileInfo._inlineText === "string") return fileInfo._inlineText;
    const response = await fetch(`${BRIDGE}/file/${encodeURIComponent(fileInfo.filename)}`);
    if (!response.ok) {
      throw new Error("khong fetch duoc file tu bridge: " + response.status);
    }
    return await response.text();
  }

  // Fallback: đẩy file qua input[type=file] của Qwen UI.
  async function uploadFileViaUI(fileInfo, text, job) {
    log("uploading large prompt file:", fileInfo.filename);
    const file = new File([text], fileInfo.filename, { type: "text/plain" });

    let fileInput = document.querySelector('input[type="file"]');
    if (!fileInput) {
      // Click nút attachment (kẹp giấy) để UI sinh ra input[type="file"]
      const attachBtn = document.querySelector(
        '[aria-label*="Attach" i], [aria-label*="Đính kèm" i], [aria-label*="dính kèm" i], [data-testid*="attach" i]'
      );
      if (attachBtn) {
        attachBtn.click();
        await sleep(500);
        fileInput = document.querySelector('input[type="file"]');
      }
    }
    if (!fileInput) {
      log("khong tim thay input[type=file] cua Qwen UI");
      if (typeof job !== "undefined" && job) {
        send(job.id, { kind: "warn", message: "CANH BAO: uploadFileViaUI that bai - khong tim thay input[type=file] trong DOM. UI co the da doi layout." });
      }
      return false;
    }

    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "files"
    )?.set;
    if (nativeInputValueSetter) {
      nativeInputValueSetter.call(fileInput, dataTransfer.files);
    } else {
      fileInput.files = dataTransfer.files;
    }
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));
    fileInput.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(2000);
    return true;
  }

  // Prompt quá dài: paste để Qwen tự tạo document txt; nếu UI bỏ qua paste thì
  // thử upload file, cuối cùng mới gửi nguyên text.
  async function attachLongPrompt(composer, fileInfo, companionPrompt, job) {
    const promptText = companionPrompt || attachmentPrompt || ATTACHMENT_COMPANION_PROMPT;
    const RAW_FALLBACK_MAX = 125000;   // Qwen gioi han ~131072 ky tu / message
    const text = await fetchPromptText(fileInfo);
    const fname = fileInfo.filename || "";
    const before = attachmentSnapshot();
    const baseCount = findActiveAttachmentElements(composer).length;
    const fileOk = await verifyPromptFile(job, fileInfo, text);
    if (fileOk === false && job && job.blockFileMismatch) {
      throw new Error("noi dung file prompt lay tu bridge khong khop file Python da ghi "
        + "(bytes/sha256) -> khong dinh kem (bridge_attachment_block co 'content')");
    }
    reportStaleAttachments(job, composer);

    // Moi cach deu duoc kiem tra card; that bai thi xoa composer roi thu cach tiep.
    // Thu tu uu tien theo ty le thanh cong thuc te: paste-file, drop-file truoc.
    // paste-text / file-input de cuoi lam fallback (deu co the tao ra request sai).
    const strategies = [
      { name: "paste-file", wait: 12000, run: async () => {
          pasteFileEvent(composer, fileInfo, text);
          await sleep(1500);
          return 0;
      } },
      { name: "drop-file", wait: 12000, run: async () => {
          dropFileEvent(composer, fileInfo, text);
          await sleep(1500);
          return 0;
      } },
      { name: "paste-text", wait: 15000, run: async () => {
          await pasteLongText(composer, text);
          return composerText(composer).trim().length;
      } },
      { name: "file-input", wait: 15000, run: async () => {
          await uploadFileViaUI(fileInfo, text, job);
          return 0;
      } },
    ];

    // Khong co nhanh rieng cho file > 131072: A/B cho thay paste-file van tao
    // document card du ~320k ky tu, va nhanh ngang drop-file (~1-2s).
    // Neu gap bug attach sau nay, doi thu tu mang `strategies` o tren la du.
    let attached = false, used = "";
    for (const s of strategies) {
      /* dung ngay khi job het hieu luc */
      if (!(await jobAlive(job, true))) return;   /* bo cache */
      saveJobState(job, "attach_" + s.name, "Attaching " + fname + " via " + s.name);
      let pastedLen = 0;
      try {
        pastedLen = await s.run();
      } catch (e) {
        log("attach strategy " + s.name + " error:", (e && e.message) || e);
      }
      log("large prompt via " + s.name + ": composer=" + pastedLen + " chars");
      attached = await waitForAttachmentEx(composer, before, baseCount, pastedLen, fname, s.wait);
      if (attached) { used = s.name; break; }
      /* Job co the vua bi huy trong luc cho attachment (nhat la strategy cuoi):
         khong xoa composer / khong thu tiep. */
      if (!(await jobAlive(job, true))) return;
      if (job) send(job.id, { kind: "warn",
        message: "attach '" + s.name + "' khong tao duoc attachment card; thu cach khac" });
      clearComposer(composer);
      await sleep(400);
    }

    if (attached) {
      if (!(await jobAlive(job))) return;
      saveJobState(job, "file_uploaded", "File attached via " + used + ": " + fname);
      if (job) job._attachStrategy = used;   // classifyRequestFiles bo qua ten khi paste-text
      if (job) send(job.id, { kind: "debug", message: "large prompt attached via " + used });
      reportAttachmentCards(job, composer, used, fname);
      await sleep(800);
      if (!(await jobAlive(job, true))) return;
      setComposerClean(composer, promptText);
      await sleep(300);
      let currentText = composerText(composer).trim();
      if (!currentText) {
        log("attachment_prompt chua xuat hien trong composer, thu set lai lan 2...");
        if (!(await jobAlive(job, true))) return;
        setComposerClean(composer, promptText);
        await sleep(350);
        currentText = composerText(composer).trim();
      }
      if (!currentText) {
        const warnMsg = "CANH BAO: Thieu paste attachment_prompt vao composer! Composer van dang rong sau khi attach file.";
        log(warnMsg);
        if (job) send(job.id, { kind: "warn", message: warnMsg });
      } else {
        saveJobState(job, "prompt_inserted", "Attachment prompt inserted: " + currentText.slice(0, 30));
        debugLog("attachment_prompt pasted (" + currentText.length + " chars):", currentText.slice(0, 60));
      }
      await sleep(400);
      return;
    }

    // Khong cach nao tao duoc attachment.
    // Job da het hieu luc (huy luc strategy cuoi / trong luc sleep sau clearComposer):
    // khong dien lai text tho vao composer.
    if (!(await jobAlive(job, true))) return;
    const extra = (promptText && promptText !== ATTACHMENT_COMPANION_PROMPT
      && promptText !== attachmentPrompt) ? promptText : "";
    const fullText = extra ? text + "\n\n" + extra : text;
    if (fullText.length > RAW_FALLBACK_MAX) {
      // Gui text tho vuot gioi han -> Send bi khoa, tab treo den het timeout.
      // Bao loi ngay de Python retry.
      throw new Error("khong dinh kem duoc prompt dai (" + fullText.length
        + " ky tu) bang paste/upload/drop; text tho vuot " + RAW_FALLBACK_MAX);
    }
    saveJobState(job, "fallback_raw_text", "Could not attach file, sending raw text");
    log("khong tao duoc attachment; gui truc tiep text dai (" + fullText.length + ")");
    if (job) send(job.id, { kind: "warn", message: "Khong tao duoc attachment card; gui truc tiep text dai" });
    setComposerClean(composer, fullText);
    await sleep(400);
  }

  // Chờ send button sẵn sàng (thường là sau khi upload xong) rồi bấm.
  // Nếu UI chưa nhận, thử lại vài lần.
  async function sendPrompt(composer, timeoutMs = 45000, fallbackPrompt = null, job = null) {
    /* companion chi ton tai khi job co file. */
    const companion = (job && job.file_info)
      ? (fallbackPrompt || job.attachment_prompt || attachmentPrompt
         || ATTACHMENT_COMPANION_PROMPT)
      : "";
    if (job && job.file_info) timeoutMs = Math.max(timeoutMs, 90000);  // doi upload xong
    const deadline = Date.now() + timeoutMs;
    let clicks = 0, enters = 0, noBtnSince = 0, lastEnter = 0, stallSince = 0, sawText = false;
    /* Diag: lich su do dai composer + so click, de phan biet "text bi xoa TRUOC khi click"
       vs "click roi app don composer ma khong gui request". Chi ghi khi co job. */
    const trace = job ? (job._sendTrace = job._sendTrace || {
      t0: Date.now(), clicks: 0, enters: 0, samples: [], atClick: [],
      firstClickAt: -1, emptyAt: -1, lastSample: -1000,
    }) : null;
    const note = () => {
      if (!trace) return;
      const off = Date.now() - trace.t0;
      const len = composerText(composer).trim().length;
      trace.clicks = clicks;
      trace.enters = enters;
      if (trace.emptyAt < 0 && !len) trace.emptyAt = off;
      if (off - trace.lastSample >= 1000 && trace.samples.length < 20) {
        trace.lastSample = off;
        trace.samples.push(off + ":" + len);
      }
    };
    while (Date.now() < deadline) {
      touchJob(job);
      if (job && (job.attached || job.settled)) return !!job.attached;
      /* job het hieu luc -> dung, khong bam Send */
      if (job && !(await jobAlive(job, true))) return false;   /* bo cache */
      note();
      /* Inline prompt lon: composer rong sau Send / nut Send bien mat ma khong co
         completion request -> hoc nguong inline + bao loi SOM de Python retry di duong
         attach file, thay vi cho het 60s vong Send + 90s send-timeout. Chi new/legacy. */
      if (inlineStallWatch(job) && sawText && !job.attached) {
        const emptyNow = !composerText(composer).trim();
        const stuck = emptyNow || (noBtnSince && !findStopButton());
        if (stuck) {
          if (!stallSince) stallSince = Date.now();
          if (Date.now() - stallSince >= INLINE_STALL_GRACE_MS) {
            learnInlineLimit(String(job.prompt).length);
            throw new Error("inline prompt " + String(job.prompt).length
              + " ky tu: " + (emptyNow ? "composer rong" : "nut Send bien mat")
              + " sau Send nhung khong co completion request sau "
              + Math.round(INLINE_STALL_GRACE_MS / 1000)
              + "s; se dung duong attach file o lan retry | " + sendDiag(job, composer));
          }
        } else {
          stallSince = 0;
        }
      }
      const btn = findSendButton(composer);
      if (!btn) {
        // Khong co nut Send: UI dang generate/upload. Neu UI da ranh ma van khong
        // thay nut (doi layout) thi thu Enter, toi da 3 lan.
        if (!noBtnSince) noBtnSince = Date.now();
        if (Date.now() - noBtnSince > 4000 && Date.now() - lastEnter > 4000
            && enters < 3 && !findStopButton() && composerText(composer).trim()) {
          enters++;
          lastEnter = Date.now();
          log("khong thay nut Send; thu Enter (" + enters + ")");
          pressEnter(composer);
        }
        await sleep(400);
        continue;
      }
      noBtnSince = 0;

      // Composer rong truoc khi gui: chen companion prompt (tranh loi "content is empty")
      if (!composerText(composer).trim()) {
        /* khong co attachment that -> khong dien
           cau mac dinh, bao loi de Python retry. */
        const hasAtt = !!(job && job.file_info)
          && findActiveAttachmentElements(composer).length > 0;
        if (!hasAtt || !companion) {
          throw new Error("composer rong truoc khi gui va khong co attachment; "
            + "khong gui cau mac dinh thay prompt that");
        }
        log("composer is empty before sending; inserting companion prompt:", companion);
        setComposerClean(composer, companion);
        await sleep(350);
        if (!composerText(composer).trim()) {
          const warnMsg = "CANH BAO: Chuan bi gui nhung composer van rong sau khi dien companion!";
          log(warnMsg);
          if (job) send(job.id, { kind: "warn", message: warnMsg });
        }
      }

      // Job prompt lon: canh bao neu khong thay card (heuristic, chi de chan doan)
      if (job && job.file_info && !job._warnedNoAtt) {
        const fname = job.file_info.filename || "";
        const scope = composer.closest("form") || document.body;
        const hasAtt = findActiveAttachmentElements(composer).length > 0
          || (fname && String((scope && scope.innerText) || "").includes(fname));
        if (composerText(composer).trim() && !hasAtt) {
          job._warnedNoAtt = true;
          const warnMsg = "CANH BAO: khong thay attachment card trong DOM truoc khi bam gui (heuristic, co the sai - xem dong 'completion request' de biet file co duoc gui that)";
          log(warnMsg);
          send(job.id, { kind: "warn", message: warnMsg });
        }
      }

      /* phia tren co the vua dien companion + sleep 350ms */
      if (job && !(await jobAlive(job, true))) return false;
      saveJobState(job, "sending", "Clicking send button (click #" + (clicks + 1) + ")");
      const hadText = !!composerText(composer).trim();
      if (hadText) sawText = true;
      if (trace) {
        if (trace.firstClickAt < 0) trace.firstClickAt = Date.now() - trace.t0;
        if (trace.atClick.length < 20) trace.atClick.push(composerText(composer).trim().length);
      }
      btn.click();
      clicks++;
      log("send click", clicks);
      await sleep(1500);
      if (job && job.attached) return true;      // da bat duoc completion: dung click them
      if (!findSendButton(composer) && !inlineStallWatch(job)) return true;
      if (hadText && !composerText(composer).trim() && !inlineStallWatch(job)) return true;
      if (clicks >= 15) break;
    }
    return !!(job && job.attached);
  }

  const SEND_TIMEOUT_FILE_MS = 240000;  // job co file lon: paste/upload mat nhieu thoi gian
  const STOP_LABEL_RX = /\b(stop|interrupt)\b|dừng|ngừng|停止|终止/i;
  const STOP_CLASS_RX = /(^|[\s_-])stop([\s_-]|$)/i;

  function isStopButton(el) {
    if (!el || typeof el.getAttribute !== "function") return false;
    const label = [el.getAttribute("aria-label"), el.getAttribute("title"),
      el.getAttribute("data-testid"), el.getAttribute("data-icon")]
      .filter(Boolean).join(" ");
    if (STOP_LABEL_RX.test(label)) return true;
    const cls = typeof el.className === "string" ? el.className : "";
    if (STOP_CLASS_RX.test(cls)) return true;
    return !!(el.querySelector && el.querySelector('[class*="stop" i], [data-icon*="stop" i]'));
  }

  function findStopButton() {
    const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
    for (const el of nodes) {
      if (!visible(el)) continue;
      if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
      if (isStopButton(el)) return el;
    }
    return null;
  }

  // Client huy (ngat ket noi / HTTP abort) hoac job het han khi Qwen DANG generate: bam Stop de
  // chat Qwen dung that. Truoc day chi nha reader -> Qwen chay tiep den het (tren chat session
  // thanh tin assistant thua cua turn bi huy). Chi khi job da gui request (attached) va bi huy.
  async function stopGenerationOnCancel(job) {
    if (!job || !job.cancelled || !job.attached) return false;
    let clicked = 0;
    try {
      for (let i = 0; i < 3; i++) {
        let stop = findStopButton();
        if (!stop && i === 0) {   // nut Stop hien cham sau khi request di
          for (let w = 0; w < 5 && !stop; w++) { await sleep(300); stop = findStopButton(); }
        }
        if (!stop) break;
        stop.click();
        clicked++;
        await sleep(700);
      }
    } catch (e) { /* best-effort */ }
    if (clicked) log("job huy: da bam Stop tren Qwen (" + clicked + " lan)");
    return clicked > 0;
  }

  // Qwen co the van generate sau khi job truoc da settle (vd. native tool_call
  // da tra ve client roi Qwen tu chay tiep). Neu go prompt moi luc do, nut Send
  // bi thay bang Stop va prompt dung yen trong composer. Cho UI ranh, hoac bam
  // Stop (ket qua job truoc da lay xong) roi moi bat dau job moi.
  async function waitUiIdle(job) {
    const t0 = Date.now();
    let clicks = 0, stopSeenAt = 0;
    while (Date.now() - t0 < 12000) {
      const stop = findStopButton();
      if (!stop) {
        if (stopSeenAt) debugLog("waitUiIdle: UI idle sau " + (Date.now() - t0) + "ms");
        return true;
      }
      if (!stopSeenAt) stopSeenAt = Date.now();
      if (Date.now() - stopSeenAt > 1500 && clicks < 2) {
        clicks++;
        log("UI van dang generate sau job truoc; bam Stop (" + clicks + ")");
        if (job) send(job.id, { kind: "debug",
          message: "UI dang generate khi nhan job moi; bam Stop de giai phong composer" });
        stop.click();
        await sleep(800);
        continue;
      }
      await sleep(300);
    }
    if (job) send(job.id, { kind: "warn",
      message: "UI van o trang thai generating sau 12s; van tiep tuc gui" });
    return false;
  }

  // --- attach prompt lon: phat hien card + cac cach dua file vao UI ---
  function attachmentDetected(composer, before, baseCount, pastedLen, fname) {
    try {
      if (Array.from(attachmentSnapshot()).some((n) => !before.has(n))) return true;
      if (findActiveAttachmentElements(composer).length > baseCount) return true;
      if (pastedLen > 500) {
        const now = composerText(composer).trim().length;
        if (now < Math.max(200, pastedLen / 2)) return true;
      }
      if (fname) {
        const scope = composer.closest("form") || document.body;
        if (String((scope && scope.innerText) || "").includes(fname)) return true;
      }
    } catch (e) { /* heuristic */ }
    return false;
  }

  async function waitForAttachmentEx(composer, before, baseCount, pastedLen, fname, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (attachmentDetected(composer, before, baseCount, pastedLen, fname)) return true;
      await sleep(400);
    }
    return false;
  }

  function makeTextFile(fileInfo, text) {
    return new File([text], fileInfo.filename, { type: "text/plain" });
  }

  // Paste mot FILE (clipboardData.files) thay vi text: app xu ly nhu dan file.
  function pasteFileEvent(composer, fileInfo, text) {
    composer.focus();
    try {
      const dt = new DataTransfer();
      dt.items.add(makeTextFile(fileInfo, text));
      composer.dispatchEvent(new ClipboardEvent("paste", {
        clipboardData: dt, bubbles: true, cancelable: true,
      }));
    } catch (e) { log("paste-file failed:", (e && e.message) || e); }
  }

  // Tha file vao vung chat (dropzone cua Qwen).
  function dropFileEvent(composer, fileInfo, text) {
    try {
      const dt = new DataTransfer();
      dt.items.add(makeTextFile(fileInfo, text));
      const target = composer.closest("form") || composer;
      for (const type of ["dragenter", "dragover", "drop"]) {
        target.dispatchEvent(new DragEvent(type, {
          dataTransfer: dt, bubbles: true, cancelable: true,
        }));
      }
    } catch (e) { log("drop-file failed:", (e && e.message) || e); }
  }

  async function runJob(job) {
    try {
      await waitUiIdle(job);
    } catch (e) {
      log("waitUiIdle error:", (e && e.message) || e);
    }
    return runJobInner(job);
  }

  async function runJobInner(job) {
    current = job;
    saveJobState(job, "started", "Job " + job.id + " started");
    await maybeInlineToFile(job);
    let sendTimer = null;
    job._armSendTimer = () => {
      if (job.settled || job.attached) return;
      if (sendTimer) clearTimeout(sendTimer);
      sendTimer = setTimeout(() => {
        settle(job, new Error("UI send timeout: khong bat duoc completion response | " + sendDiag(job, null)));
      }, job.file_info ? SEND_TIMEOUT_FILE_MS : SEND_TIMEOUT_MS);
    };
    job._armSendTimer();
    job._clearSendTimer = () => {
      if (sendTimer) {
        clearTimeout(sendTimer);
        sendTimer = null;
      }
    };
    try {
      /* kiem tra job con song truoc MOI thao
         tac UI, va khong gui khi chon model/mode that bai neu config strict. */
      await ensureAlive(job, "truoc new-chat");
      await waitCaptchaClear(job, "truoc new-chat/reload");
      if (!(await jobAlive(job))) {
        /* settle de Python khong cho toi timeout */
        if (!job.settled) settle(job, new Error("job cancelled (cho captcha)"));
        return;
      }
      const inChat = !!job.session
        && (job.session.mode === "continue" || job.session.mode === "fork");
      if (inChat) {
        // Session: dung tiep chat dang mo -> KHONG new chat / reload / doi temporary mode.
        const problem = sessionProblem(job.session, PAGE_NONCE, location.pathname,
          isTemporaryPage(false));
        if (problem) {
          const lostErr = new Error("session: " + problem);
          lostErr.sessionLost = true;
          throw lostErr;
        }
        fwd(job, "debug", "session " + job.session.mode + ": dung tiep chat "
          + (job.session.chat_id || "(temporary)") + " path=" + location.pathname);
      } else {
        if ((await tryNewChat(job)) === false) return;  // dang reload; job da requeue
        saveJobState(job, "new_chat_done", "New chat step finished");
        if (!(await ensureMode(job))) return;
        await ensureAlive(job, "sau ensureMode");
        await waitPageReady(job);
      }
      await waitCaptchaClear(job, "sau khi trang san sang");
      await ensureAlive(job, "truoc selectModel");
      const modelOk = await selectModel(job);
      fwd(job, "debug", "selectModel: " + modelUiSummary(job));
      if (!modelOk && requireModel) {
        send(job.id, { kind: "warn", model_mismatch: true,
          message: "require_model=true: khong chon duoc model " + job.model
            + " -> huy job, khong gui voi model UI cu" });
        throw new Error("khong chon duoc model yeu cau (" + job.model
          + "); bridge_require_model=true");
      }
      await ensureAlive(job, "truoc selectMode");
      const modeOk = await selectThinkingMode(job);
      if (!modeOk && requireMode) {
        send(job.id, { kind: "warn", mode_mismatch: true,
          message: "require_mode=true: khong chon duoc che do " + job.mode
            + " -> huy job, khong gui voi che do UI cu" });
        throw new Error("khong chon duoc che do UI yeu cau (" + job.mode
          + "); bridge_require_mode=true");
      }
      await ensureAlive(job, "truoc composer");
      saveJobState(job, "model_selected", "Model " + job.model);
      send(job.id, { kind: "debug",
        message: "temporary evidence: " + JSON.stringify(temporaryEvidence()) });
      const composer = await waitComposer();
      job._composerEl = composer;   // sendDiag dung khi send-timeout khong co composer trong scope
      saveJobState(job, "composer_ready", "Composer found");
      pageUsed = true;  // tu day trang nay co tin nhan

      if (job.file_info) {
        // Large prompt: paste/upload de Qwen tao document, roi gui attachment.
        job.blockFileMismatch = !!(requireAttachment && attachmentBlock.has("content"));
        await attachLongPrompt(composer, job.file_info, job.attachment_prompt, job);
        await sendPrompt(composer, 45000, job.attachment_prompt, job);
      } else {
        // Normal flow
        await submitPrompt(composer, job.prompt, job);
      }
      /* job bi huy ma khong ai settle -> tranh treo toi send-timeout */
      if (job.cancelled && !job.settled) settle(job, new Error("job cancelled"));
      await job.promise;
    } catch (e) {
      settle(job, e);
    } finally {
      /* caller huy khi Qwen dang generate -> bam Stop (best-effort, toi ~4s) */
      try { await stopGenerationOnCancel(job); } catch (e) { /* best-effort */ }
      if (typeof job._clearSendTimer === "function") job._clearSendTimer();
      /* watchdog XHR khong duoc song qua job. */
      if (job._xhrTimer) { clearTimeout(job._xhrTimer); job._xhrTimer = null; }
      clearJobState();
      current = null;
    }
  }

  setInterval(() => {
    post("/event", { kind: "ping", tab: tabInfo(),
      inline_limit: Number.isFinite(inlineMaxChars) ? Math.round(inlineMaxChars) : 0 }).catch(() => {});
    /* Job dang generate ma caller da ngat: khong cho event/delta ke tiep (Qwen "nghi" lau thi
       khong co delta nao) -> hoi /alive, settle de runJobInner bam Stop. */
    const j = current;
    if (j && j.attached && !j.settled && !j.cancelled) {
      jobAlive(j, true).then((ok) => {
        if (!ok && !j.settled) settle(j, new Error("job het hieu luc (caller da ngat/huy)"));
      }).catch(() => {});
    }
  }, 5000);

  async function loop() {
    log("started on", BASE, "->", BRIDGE);
    for (;;) {
      /* navigation that bai -> khong ket poll vinh vien */
      if (navigating) {
        if (!navSince) navSince = Date.now();
        if (Date.now() - navSince > 20000) {
          log("navigation khong xay ra sau 20s; bo co navigating de poll lai");
          navigating = false;
          navSince = 0;
        }
        await sleep(500);
        continue;
      }
      navSince = 0;
      if (active >= MAX_JOBS) { await sleep(400); continue; }
      try {
        await refreshModels(false);
        if (!(await idleModeCheck())) { await sleep(500); continue; }
        const r = await post("/poll", { worker: location.host, models: knownModels,
          tab: tabInfo(),
          inline_limit: Number.isFinite(inlineMaxChars) ? Math.round(inlineMaxChars) : 0 });
        if (r && r.job) {
          active++;
          const job = {
            id: r.job.job_id,
            model: r.job.model,
            mode: r.job.mode,
            prompt: r.job.prompt,
            think: r.job.think,
            file_info: r.job.file_info,
            attachment_prompt: r.job.attachment_prompt || "",
            deadline: r.job.deadline ? r.job.deadline * 1000 : 0,
            idle_timeout: Number(r.job.idle_timeout) || 0,
            _lastActivity: Date.now(),
            raw: r.job,
            mode_retries: r.job.mode_retries || 0,
            fresh_reloads: r.job.fresh_reloads || 0,
            session: r.job.session && typeof r.job.session === "object" ? r.job.session : null,
            // Job session: Python bao tools_active (delta khong con tool block de doan).
            toolsActive: typeof r.job.tools_active === "boolean" ? r.job.tools_active
              : String(r.job.prompt || "").includes("# Tool Use")
                || String(r.job.attachment_prompt || "").includes("# Tool Use"),
            attached: false,
            settled: false,
          };
          job.promise = new Promise((resolve, reject) => {
            job.resolve = resolve;
            job.reject = reject;
          });
          job.promise.catch(() => {});
          runJob(job).finally(() => { active--; });
        }
      } catch (e) {
        const msg = (e && e.message) || String(e);
        console.warn("[qwen-ui-bridge] poll loop error:", msg);
        debugLog("poll loop error:", msg);
        await sleep(2000);
      }
    }
  }

  window.__qwenUIBridgeDebug = () => {
    console.log("composers:", findComposers());
    console.log("new chat:", findNewChatButton());
    const c = findComposers()[0];
    console.log("send button:", c ? findSendButton(c) : null);
    console.log("active attachments:", findActiveAttachmentElements(c));
    console.log("has chat messages:", hasChatMessages());
    console.log("temporary reload:", { enabled: temporaryReloadEnabled, pageUsed: pageUsed, freshByBridge: freshByBridge, dirty: pageDirty(), inlineMaxChars: inlineMaxChars });
    console.log("temporary chat page:", isTemporaryPage(), temporaryEvidence());
    console.log("debug enabled:", debugEnabled);
    console.log("active job in sessionStorage:", sessionStorage.getItem(ACTIVE_JOB_KEY));
    console.log("known models:", knownModels, "select enabled:", selectModelEnabled);
    const trigger = findModelTrigger(
      knownModels.map((m) => m.id || "").filter(Boolean));
    console.log("model trigger:", trigger,
      trigger ? { visible: visibleText(trigger), aria: textOf(trigger) } : "");
    console.log("last raw thinking deltas:", lastRawDeltas);
  };

  hydrationProbe().catch(() => {});
  loop();
})();