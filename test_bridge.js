// Unit test cho logic trong extension/bridge.js (khong can browser, khong dependency).
//
//   node --test test_bridge.js
//
// bridge.js la content script dang IIFE nen khong require() duoc. Test cat NGUYEN VAN than
// tung ham (tu `  function ten(` toi dong `  }` thut le 2 ky tu dau tien) roi dung lai bang
// new Function() voi cac phu thuoc (DOM/mang/sleep...) la stub. Ham goi them ham/bien khac
// ma khong co trong stub -> ReferenceError ngay (bo sung stub vao ctx).
//
// Khong thay the test UI that: selector DOM (findActiveAttachmentElements, composerText,
// ...) van phai thu tren chat.qwen.ai. Test nay khoa luong dieu khien: huy job giua chung,
// thu tu strategy, fallback, doi chieu file.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SRC = fs.readFileSync(path.join(__dirname, "extension", "bridge.js"), "utf8")
  .replace(/\r\n/g, "\n");

function extractFn(name) {
  const m = new RegExp("^  (async )?function " + name + "\\(", "m").exec(SRC);
  if (!m) return null;
  const end = SRC.indexOf("\n  }\n", m.index);
  if (end < 0) throw new Error("khong tim thay than ham " + name);
  return SRC.slice(m.index, end + 5);
}

// loadFns(["a", "b"], {dep: stub}) -> {a, b}. Ham khong co trong bridge.js -> null.
function loadFns(names, ctx) {
  const srcs = names.map(extractFn);
  if (srcs.some((s) => s === null)) return null;
  const keys = Object.keys(ctx);
  const body = srcs.join("\n") + "\nreturn {" + names.join(",") + "};";
  return new Function(...keys, body)(...keys.map((k) => ctx[k]));
}

const skipIfMissing = (fns, what) => (fns ? {} : { skip: what + " chua co trong bridge.js" });

// ------------------------------------------------------------------ jobAlive ----
function aliveEnv(getJSON) {
  return loadFns(["jobDead", "jobAlive"], { ALIVE_CACHE_MS: 1500, getJSON });
}

test("jobAlive: job null/settled/cancelled -> false, khong goi mang", async () => {
  let calls = 0;
  const { jobAlive } = aliveEnv(async () => { calls++; return { alive: true }; });
  assert.equal(await jobAlive(null, true), false);
  assert.equal(await jobAlive({ id: "a", settled: true }, true), false);
  assert.equal(await jobAlive({ id: "a", cancelled: true }, true), false);
  assert.equal(calls, 0);
});

test("jobAlive: qua deadline -> cancelled + false", async () => {
  const { jobAlive } = aliveEnv(async () => ({ alive: true }));
  const job = { id: "a", deadline: Date.now() - 1 };
  assert.equal(await jobAlive(job, true), false);
  assert.equal(job.cancelled, true);
});

test("jobAlive: proxy tra alive=false -> cancelled", async () => {
  const { jobAlive } = aliveEnv(async () => ({ alive: false }));
  const job = { id: "a" };
  assert.equal(await jobAlive(job, true), false);
  assert.equal(job.cancelled, true);
});

test("jobAlive: cache 1.5s, force=true bo cache", async () => {
  let calls = 0;
  const { jobAlive } = aliveEnv(async () => { calls++; return { alive: true }; });
  const job = { id: "a" };
  await jobAlive(job);
  await jobAlive(job);
  assert.equal(calls, 1);
  await jobAlive(job, true);
  assert.equal(calls, 2);
});

test("jobAlive: loi mang khong ket luan job chet", async () => {
  const { jobAlive } = aliveEnv(async () => { throw new Error("offline"); });
  const job = { id: "a" };
  assert.equal(await jobAlive(job, true), true);
  assert.notEqual(job.cancelled, true);
});

// ------------------------------------------------------------ attachLongPrompt ----
const TEXT = "x".repeat(1000);

// Moi moi truong tra ve {fn, calls, state}. `onWait(n)` / `onSleep(n)` cho kich ban huy job.
function attachEnv(opts = {}) {
  const state = { dead: false, composer: "", waits: 0, clears: 0, sleeps: 0 };
  const calls = [];
  const rec = (name) => (...a) => { calls.push([name, ...a]); };
  const ctx = {
    attachmentPrompt: "AP",
    ATTACHMENT_COMPANION_PROMPT: "COMP",
    fetchPromptText: async () => opts.text || TEXT,
    attachmentSnapshot: () => ({}),
    findActiveAttachmentElements: () => [],
    verifyPromptFile: opts.verify || (async () => {}),
    reportStaleAttachments: () => {},
    reportAttachmentCards: () => {},
    jobAlive: async () => !state.dead,
    saveJobState: rec("saveJobState"),
    log: () => {},
    debugLog: () => {},
    send: rec("send"),
    sleep: async () => {
      state.sleeps++;
      if (opts.onSleep) opts.onSleep(state);
    },
    pasteFileEvent: rec("pasteFileEvent"),
    dropFileEvent: rec("dropFileEvent"),
    pasteLongText: async () => { calls.push(["pasteLongText"]); },
    uploadFileViaUI: async () => { calls.push(["uploadFileViaUI"]); },
    composerText: () => state.composer,
    waitForAttachmentEx: async () => {
      state.waits++;
      if (opts.onWait) return opts.onWait(state);
      return false;
    },
    clearComposer: () => { state.clears++; state.composer = ""; calls.push(["clearComposer"]); },
    setComposerClean: (_c, t) => { state.composer = t; calls.push(["setComposerClean", t]); },
  };
  const fns = loadFns(["attachLongPrompt"], ctx);
  const run = (companion, job) =>
    fns.attachLongPrompt({}, { filename: "qwen_prompt_x.txt" }, companion, job || { id: "job1" });
  const names = () => calls.map((c) => c[0]);
  return { run, calls, names, state };
}

test("attach: moi strategy that bai + job song -> fallback dien text tho", async () => {
  const env = attachEnv();
  await env.run();
  assert.equal(env.state.waits, 4);
  assert.equal(env.state.clears, 4);
  assert.deepEqual(env.calls.filter((c) => c[0] === "setComposerClean"),
    [["setComposerClean", TEXT]]);
});

test("attach: attach thanh cong o strategy dau -> chi dien companion prompt", async () => {
  const env = attachEnv({
    onWait: (st) => true,
  });
  await env.run();
  assert.equal(env.state.clears, 0);
  assert.deepEqual(env.calls.filter((c) => c[0] === "setComposerClean"),
    [["setComposerClean", "AP"]]);
  assert.equal(env.state.composer, "AP");
});

test("attach: huy trong luc cho strategy CUOI -> khong xoa composer, khong dien text", async () => {
  const env = attachEnv({
    onWait: (st) => { if (st.waits === 4) st.dead = true; return false; },
  });
  await env.run();                      // khong throw
  assert.equal(env.state.waits, 4);
  assert.equal(env.state.clears, 3);    // chi 3 lan sau strategy 1-3, KHONG lan thu 4
  assert.ok(!env.names().includes("setComposerClean"), "khong duoc dien text cu");
});

test("attach: huy trong sleep sau clearComposer cuoi -> khong dien text tho", async () => {
  const env = attachEnv({
    onSleep: (st) => { if (st.clears === 4) st.dead = true; },
  });
  await env.run();
  assert.equal(env.state.clears, 4);
  assert.ok(!env.names().includes("setComposerClean"), "khong duoc dien text cu");
});

test("attach: huy o strategy 2 -> dung han, khong chay strategy 3/4", async () => {
  const env = attachEnv({
    onWait: (st) => { if (st.waits === 2) st.dead = true; return false; },
  });
  await env.run();
  assert.equal(env.state.waits, 2);
  assert.equal(env.state.clears, 1);
  assert.ok(!env.names().includes("pasteLongText"));
  assert.ok(!env.names().includes("uploadFileViaUI"));
  assert.ok(!env.names().includes("setComposerClean"));
});

test("attach: text tho vuot 125000 ky tu + job song -> throw de Python retry", async () => {
  const env = attachEnv({ text: "y".repeat(126000) });
  await assert.rejects(env.run(), /khong dinh kem duoc prompt dai/);
  assert.ok(!env.names().includes("setComposerClean"));
});

test("attach: text tho vuot 125000 ky tu + job da huy -> thoat im lang", async () => {
  const env = attachEnv({
    text: "y".repeat(126000),
    onWait: (st) => { if (st.waits === 4) st.dead = true; return false; },
  });
  await env.run();                      // khong throw
  assert.ok(!env.names().includes("setComposerClean"));
});

test("attach: companion rieng duoc ghep vao text tho o fallback", async () => {
  const env = attachEnv();
  await env.run("EXTRA");
  const set = env.calls.filter((c) => c[0] === "setComposerClean");
  assert.deepEqual(set, [["setComposerClean", TEXT + "\n\nEXTRA"]]);
});

// ------------------------------------------- doi chieu file / request (neu co) ----
const sha = async (t) => Buffer.from(
  await crypto.subtle.digest("SHA-256", new TextEncoder().encode(t))).toString("hex");

function verifyEnv() {
  const sent = [];
  const fns = loadFns(["fingerprintText", "verifyPromptFile"], {
    window: { crypto: globalThis.crypto },
    crypto: globalThis.crypto,
    fwd: (_job, kind, msg) => sent.push([kind, msg]),
    debugLog: () => {},
  });
  return { fns, sent };
}

test("verifyPromptFile: khop bytes + sha256 -> debug", async (t) => {
  const { fns, sent } = verifyEnv();
  if (!fns) return t.skip("verifyPromptFile chua co trong bridge.js");
  const text = "xin chào\n" + "z".repeat(50);
  const info = { filename: "f.txt", bytes: Buffer.byteLength(text), sha256: await sha(text) };
  await fns.verifyPromptFile({ id: "j" }, info, text);
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], "debug");
  assert.match(sent[0][1], /khop/);
});

test("verifyPromptFile: lech noi dung -> warn", async (t) => {
  const { fns, sent } = verifyEnv();
  if (!fns) return t.skip("verifyPromptFile chua co trong bridge.js");
  const info = { filename: "f.txt", bytes: 10, sha256: "00".repeat(32) };
  await fns.verifyPromptFile({ id: "j" }, info, "abc");
  assert.equal(sent[0][0], "warn");
  assert.match(sent[0][1], /LECH/);
});

test("verifyPromptFile: khong co bytes/sha256 hoac file inline -> khong warn", async (t) => {
  const { fns, sent } = verifyEnv();
  if (!fns) return t.skip("verifyPromptFile chua co trong bridge.js");
  await fns.verifyPromptFile({ id: "j" }, { filename: "f.txt" }, "abc");
  await fns.verifyPromptFile({ id: "j" }, { _inlineText: "abc", bytes: 1 }, "abc");
  assert.ok(sent.every((s) => s[0] === "debug"));
});

function requestEnv() {
  return loadFns(
    ["describeFileEntry", "describeFileBrief", "collectRequestFiles",
      "classifyRequestFiles", "checkRequestFiles"], {});
}

// Hinh dang file cua Qwen la GIA DINH (chua co capture that): sua khi co mau that.
const qwenFile = (over = {}) => ({
  type: "file", id: "f1", name: "qwen_prompt_ab.txt", size: 1000, status: "uploaded",
  file: { id: "f1", filename: "qwen_prompt_ab.txt",
          meta: { name: "qwen_prompt_ab.txt", size: 1000, content_type: "text/plain" } },
  ...over,
});
const body = (files) => ({ messages: [{ role: "user", content: "hi", files }] });
const EXP = { filename: "qwen_prompt_ab.txt", bytes: 1000 };

test("checkRequestFiles: dung file -> khong van de", (t) => {
  const e = requestEnv();
  if (!e) return t.skip("helper request chua co trong bridge.js");
  const files = e.collectRequestFiles(body([qwenFile()]));
  assert.equal(files.length, 1);
  assert.deepEqual(e.checkRequestFiles({ file_info: EXP }, files), []);
});

test("checkRequestFiles: sai ten / thieu dung luong / 2 file / status / file sot", (t) => {
  const e = requestEnv();
  if (!e) return t.skip("helper request chua co trong bridge.js");
  const check = (job, files) => e.checkRequestFiles(job, e.collectRequestFiles(body(files)));
  const job = { file_info: EXP };
  assert.match(check(job, [qwenFile({ id: "f2", name: "old.txt", file: { id: "f2" } })])[0],
    /DINH NHAM/);
  assert.match(check(job, [qwenFile({ size: 400, file: { id: "f1" } })])[0], /THIEU/);
  assert.match(check(job, [qwenFile(), qwenFile({ id: "f9", file: { id: "f9" } })])[0],
    /2 file/);
  assert.match(check(job, [qwenFile({ status: "uploading" })])[0], /trang thai/);
  assert.match(check({}, [qwenFile()])[0], /KHONG co file/);
  assert.deepEqual(check(job, []), []);     // thieu han file: bao rieng o noi khac
});

test("collectRequestFiles: bo trung cung file xuat hien 2 cho", (t) => {
  const e = requestEnv();
  if (!e) return t.skip("helper request chua co trong bridge.js");
  const req = { files: [qwenFile()], messages: [{ files: [qwenFile()] }] };
  assert.equal(e.collectRequestFiles(req).length, 1);
});

// ------------------------------- chan metadata file lech (bridge_attachment_block) ----
function guardEnv(block) {
  const attachmentBlock = new Set(block);
  const fns = loadFns(
    ["partsText", "describeFileEntry", "describeFileBrief", "collectRequestFiles",
      "classifyRequestFiles", "countAttachments", "strictProblemForBody"],
    {
      requireModel: false, selectModelEnabled: true, requireAttachment: true,
      attachmentBlock, modelTargets: () => [], normLabel: (s) => s,
    });
  return { fns, attachmentBlock };
}
const GUARD_JOB = { file_info: { filename: "qwen_prompt_ab.txt", bytes: 1000, size: 1000 } };
const reqBody = (files) =>
  JSON.stringify({ messages: [{ role: "user", content: "hi", files }] });
const DEFAULT_BLOCK = ["name", "size", "status", "count", "content"];

test("strictProblemForBody: dung file -> cho phep", (t) => {
  const g = guardEnv(DEFAULT_BLOCK);
  if (!g.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  assert.equal(g.fns.strictProblemForBody(GUARD_JOB, reqBody([qwenFile()])), "");
});

test("strictProblemForBody: sai ten / size / dang upload / 2 file -> CHAN", (t) => {
  const g = guardEnv(DEFAULT_BLOCK);
  if (!g.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  const run = (files) => g.fns.strictProblemForBody(GUARD_JOB, reqBody(files));
  assert.match(run([qwenFile({ id: "f2", name: "old.txt", file: { id: "f2" } })]), /old\.txt/);
  assert.match(run([qwenFile({ size: 400, file: { id: "f1" } })]), /400/);
  assert.match(run([qwenFile({ status: "uploading" })]), /uploading/);
  assert.match(run([qwenFile(), qwenFile({ id: "f9", file: { id: "f9" } })]), /2 file/);
});

test("strictProblemForBody: van chan khi thieu attachment (companion don thuan)", (t) => {
  const g = guardEnv(DEFAULT_BLOCK);
  if (!g.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  assert.match(g.fns.strictProblemForBody(GUARD_JOB, reqBody(undefined)), /KHONG mang file/);
});

test("strictProblemForBody: kind khong nam trong block -> chi canh bao, khong chan", (t) => {
  const wrongName = [qwenFile({ id: "f2", name: "old.txt", file: { id: "f2" } })];
  const g1 = guardEnv(["size"]);
  if (!g1.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  assert.equal(g1.fns.strictProblemForBody(GUARD_JOB, reqBody(wrongName)), "");
  const g2 = guardEnv([]);                                  // [] = hanh vi cu
  assert.equal(g2.fns.strictProblemForBody(GUARD_JOB, reqBody(wrongName)), "");
});

test("strictProblemForBody: status la / shape la -> khong chan (tranh false positive)", (t) => {
  const g = guardEnv(DEFAULT_BLOCK);
  if (!g.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  assert.equal(g.fns.strictProblemForBody(GUARD_JOB, reqBody([qwenFile({ status: "ready" })])), "");
  assert.equal(g.fns.strictProblemForBody(GUARD_JOB, reqBody([{ weird: "x" }])), "");
});

test("strictProblemForBody: paste-text bo qua ten nhung van so size", (t) => {
  const g = guardEnv(DEFAULT_BLOCK);
  if (!g.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  const job = { ...GUARD_JOB, _attachStrategy: "paste-text" };
  const renamed = qwenFile({ id: "f2", name: "Pasted_Text.txt", file: { id: "f2" } });
  assert.equal(g.fns.strictProblemForBody(job, reqBody([renamed])), "");
  const wrongSize = qwenFile({ id: "f2", name: "Pasted_Text.txt", size: 5, file: { id: "f2" } });
  assert.match(g.fns.strictProblemForBody(job, reqBody([wrongSize])), /5/);
});

test("strictProblemForBody: stale chi chan khi bat opt-in", (t) => {
  const g1 = guardEnv(DEFAULT_BLOCK);
  if (!g1.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  assert.equal(g1.fns.strictProblemForBody({}, reqBody([qwenFile()])), "");
  const g2 = guardEnv([...DEFAULT_BLOCK, "stale"]);
  assert.match(g2.fns.strictProblemForBody({}, reqBody([qwenFile()])), /KHONG co file/);
});

test("strictProblemForBody: body khong phai string -> khong chan", (t) => {
  const g = guardEnv(DEFAULT_BLOCK);
  if (!g.fns) return t.skip("strictProblemForBody chua co trong bridge.js");
  assert.equal(g.fns.strictProblemForBody(GUARD_JOB, null), "");
  assert.equal(g.fns.strictProblemForBody(GUARD_JOB, {}), "");
});

test("verifyPromptFile: tra false khi lech, true khi khop", async (t) => {
  const { fns } = verifyEnv();
  if (!fns) return t.skip("verifyPromptFile chua co trong bridge.js");
  const text = "abc";
  const ok = { filename: "f.txt", bytes: 3, sha256: await sha(text) };
  assert.equal(await fns.verifyPromptFile({ id: "j" }, ok, text), true);
  assert.equal(await fns.verifyPromptFile({ id: "j" }, { ...ok, bytes: 9 }, text), false);
});

test("attachLongPrompt: file lech + blockFileMismatch -> nem loi, chua dinh kem gi", async () => {
  const env = attachEnv({ verify: async () => false });
  await assert.rejects(
    () => env.run(undefined, { id: "job1", blockFileMismatch: true }), /khong khop/);
  assert.ok(!env.names().includes("pasteFileEvent"));
  assert.ok(!env.names().includes("dropFileEvent"));
});

test("attachLongPrompt: file lech nhung khong bat blockFileMismatch -> khong nem loi 'khong khop'", async () => {
  const env = attachEnv({ verify: async () => false });
  await env.run(undefined, { id: "job1" }).catch((e) =>
    assert.doesNotMatch(String(e && e.message), /khong khop file Python/));
});

// ------------------------------------------------------------ rewriteRequestMode ----
function modeEnv(cfg) {
  const sent = [];
  const fns = loadFns(["rewriteRequestMode"], {
    modeFeatureConfig: cfg,
    fwd: (_j, kind, msg) => sent.push([kind, msg]),
    debugLog: () => {},
  });
  return { fns, sent };
}
const bodyInit = (o) => ({ method: "POST", body: JSON.stringify(o) });

test("rewriteRequestMode: ghi de key khai bao vao feature_config message + top", (t) => {
  const { fns, sent } = modeEnv({ thinking: { k_think: true } });
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  const init = bodyInit({ feature_config: { k_think: false },
    messages: [{ feature_config: { k_think: false, keep: 1 } }] });
  const out = fns.rewriteRequestMode({ id: "j", mode: "thinking" }, init);
  const b = JSON.parse(out.body);
  assert.equal(b.feature_config.k_think, true);
  assert.deepEqual(b.messages[0].feature_config, { k_think: true, keep: 1 });
  assert.equal(out.method, "POST");
  assert.equal(sent[0][0], "debug");
  assert.match(sent[0][1], /msg\.k_think: false -> true/);
});

test("rewriteRequestMode: da dung gia tri -> null (khong doi body)", (t) => {
  const { fns } = modeEnv({ fast: { k: 0 } });
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  const init = bodyInit({ messages: [{ feature_config: { k: 0 } }] });
  assert.equal(fns.rewriteRequestMode({ id: "j", mode: "fast" }, init), null);
});

test("rewriteRequestMode: mode chua khai bao / khong co mode -> null, warn 1 lan", (t) => {
  const { fns, sent } = modeEnv({ thinking: { k: 1 } });
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  const init = bodyInit({ messages: [{ feature_config: {} }] });
  const job = { id: "j", mode: "auto" };
  assert.equal(fns.rewriteRequestMode(job, init), null);
  assert.equal(fns.rewriteRequestMode(job, init), null);
  assert.equal(sent.filter((s) => s[0] === "warn").length, 1);
  assert.equal(fns.rewriteRequestMode({ id: "j2" }, init), null);   // khong co mode
  assert.equal(sent.length, 1);
});

test("rewriteRequestMode: body khong co feature_config -> warn, khong ghi de", (t) => {
  const { fns, sent } = modeEnv({ thinking: { k: 1 } });
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  const out = fns.rewriteRequestMode({ id: "j", mode: "thinking" },
    bodyInit({ messages: [{ content: "hi" }] }));
  assert.equal(out, null);
  assert.match(sent[0][1], /khong co feature_config/);
});

test("rewriteRequestMode: body khong phai JSON / khong phai string -> null, khong throw", (t) => {
  const { fns } = modeEnv({ thinking: { k: 1 } });
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  const job = { id: "j", mode: "thinking" };
  assert.equal(fns.rewriteRequestMode(job, { body: "{not json" }), null);
  assert.equal(fns.rewriteRequestMode(job, { body: new Uint8Array(2) }), null);
  assert.equal(fns.rewriteRequestMode(job, undefined), null);
});

// ------------------------------------------ mode theo request that cua Qwen (capture) ----
// feature_config cua 3 request completion THAT. Fast KHONG co thinking_format.
const REAL_FC = {
  auto: { thinking_enabled: true, output_schema: "phase", research_mode: "normal",
          auto_thinking: true, thinking_mode: "Auto", thinking_format: "summary", auto_search: true },
  thinking: { thinking_enabled: true, output_schema: "phase", research_mode: "normal",
              auto_thinking: false, thinking_mode: "Thinking", thinking_format: "summary",
              auto_search: true },
  fast: { thinking_enabled: false, output_schema: "phase", research_mode: "normal",
          auto_thinking: false, thinking_mode: "Fast", auto_search: true },
};
// Giong DEFAULT_MODE_FEATURE_CONFIG trong qwen_web2api/config.py (null = xoa key).
const REAL_PATCH = {
  auto: { thinking_enabled: true, auto_thinking: true, thinking_mode: "Auto",
          thinking_format: "summary" },
  thinking: { thinking_enabled: true, auto_thinking: false, thinking_mode: "Thinking",
              thinking_format: "summary" },
  fast: { thinking_enabled: false, auto_thinking: false, thinking_mode: "Fast",
          thinking_format: null },
};

test("rewriteRequestMode: moi trang thai UI -> moi mode ra dung feature_config that", (t) => {
  const { fns } = modeEnv(REAL_PATCH);
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  for (const from of Object.keys(REAL_FC)) {
    for (const to of Object.keys(REAL_FC)) {
      const init = bodyInit({ model: "qwen3.7-plus",
        messages: [{ role: "user", content: "hi", feature_config: { ...REAL_FC[from] } }] });
      const out = fns.rewriteRequestMode({ id: "j", mode: to }, init);
      const sent = JSON.parse((out || init).body);
      assert.deepEqual(sent.messages[0].feature_config, REAL_FC[to], from + " -> " + to);
      assert.equal(sent.messages[0].content, "hi");
      assert.equal(sent.model, "qwen3.7-plus");
      assert.equal(out === null, from === to, from + " -> " + to + ": null chi khi da dung");
    }
  }
});

test("rewriteRequestMode: gia tri null xoa key, khong co key thi khong doi", (t) => {
  const { fns, sent } = modeEnv({ fast: { thinking_mode: "Fast", thinking_format: null } });
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  const job = { id: "j", mode: "fast" };
  const out = fns.rewriteRequestMode(job, bodyInit({
    messages: [{ feature_config: { thinking_mode: "Thinking", thinking_format: "summary" } }] }));
  const fc = JSON.parse(out.body).messages[0].feature_config;
  assert.deepEqual(fc, { thinking_mode: "Fast" });
  assert.ok(!("thinking_format" in fc));
  assert.match(sent[0][1], /thinking_format: "summary" -> \(xoa\)/);
  // da khong co key -> khong tinh la khac biet
  assert.equal(fns.rewriteRequestMode(job, bodyInit({
    messages: [{ feature_config: { thinking_mode: "Fast" } }] })), null);
});

test("rewriteRequestMode: null cung xoa o feature_config top-level", (t) => {
  const { fns } = modeEnv({ fast: { thinking_format: null } });
  if (!fns) return t.skip("rewriteRequestMode chua co trong bridge.js");
  const out = fns.rewriteRequestMode({ id: "j", mode: "fast" }, bodyInit({
    feature_config: { thinking_format: "summary", keep: 1 },
    messages: [{ feature_config: { thinking_format: "summary" } }] }));
  const b = JSON.parse(out.body);
  assert.deepEqual(b.feature_config, { keep: 1 });
  assert.deepEqual(b.messages[0].feature_config, {});
});

// ------------------------------------------------------------------ session ----
// Che do session (bridge_session_mode): helper thuan cua extension. Phan DOM/luong dieu khien
// (runJobInner bo tryNewChat khi job continue/fork) khong test duoc o day -> thu tren
// chat.qwen.ai that (xem AGENTS.md muc session).
function sessEnv(extra = {}) {
  const sent = [];
  const ctx = Object.assign({
    PAGE_NONCE: "NONCE-1",
    location: { pathname: "/c/local" },
    current: null,
    active: 0,
    send: (id, ev) => { sent.push([id, ev]); },
    fwd: (job, kind, message) => { sent.push([job && job.id, { kind, message }]); },
  }, extra);
  const fns = loadFns(["urlChatId", "tabInfo", "sessionProblem", "sessionInfoOfBody",
    "applyForkParent", "rewriteRequestSession", "announceSession"], ctx);
  return { fns, sent };
}

test("urlChatId: /c/<id> -> id; /c/local, goc, query -> rong", (t) => {
  const { fns } = sessEnv();
  if (!fns) return t.skip("ham session chua co trong bridge.js");
  assert.equal(fns.urlChatId("/c/1e814088-4969-4901-b03c-08dabdac8d50"), "1e814088-4969-4901-b03c-08dabdac8d50");
  assert.equal(fns.urlChatId("/c/local"), "");
  assert.equal(fns.urlChatId("/"), "");
  assert.equal(fns.urlChatId(undefined), "");
});

test("tabInfo: nonce + path + chat_id + busy", (t) => {
  const { fns } = sessEnv({ location: { pathname: "/c/abc" }, active: 1 });
  if (!fns) return t.skip("ham session chua co trong bridge.js");
  assert.deepEqual(fns.tabInfo(), { nonce: "NONCE-1", path: "/c/abc", chat_id: "abc", busy: true });
});

test("sessionProblem: nonce, chat_id va temporary", (t) => {
  const { fns } = sessEnv();
  if (!fns) return t.skip("ham session chua co trong bridge.js");
  const sj = { nonce: "NONCE-1", chat_id: "C1" };
  assert.equal(fns.sessionProblem(sj, "NONCE-1", "/c/C1", false), "");
  assert.match(fns.sessionProblem(sj, "OTHER", "/c/C1", false), /nonce khac/);
  assert.match(fns.sessionProblem(sj, "NONCE-1", "/c/C2", false), /khac chat cua session/);
  assert.match(fns.sessionProblem(sj, "NONCE-1", "/", false), /khac chat cua session/);
  // temporary: URL luon /c/local, khong doi chieu duoc chat_id; co id that tren URL -> sai chat
  assert.equal(fns.sessionProblem(sj, "NONCE-1", "/c/local", true), "");
  assert.match(fns.sessionProblem(sj, "NONCE-1", "/c/C2", true), /khong phai temporary/);
  assert.match(fns.sessionProblem(null, "NONCE-1", "/", false), /nonce/);
  assert.equal(fns.sessionProblem({ nonce: "NONCE-1", chat_id: "" }, "NONCE-1", "/c/X", false), "");
});

test("sessionInfoOfBody: lay chat_id/parent_id that (top-level uu tien, null -> rong)", (t) => {
  const { fns } = sessEnv();
  if (!fns) return t.skip("ham session chua co trong bridge.js");
  assert.deepEqual(fns.sessionInfoOfBody({ chat_id: "C", parent_id: "P", parentId: "Q",
    messages: [{ parent_id: "R" }] }), { chat_id: "C", parent_id: "P" });
  assert.deepEqual(fns.sessionInfoOfBody({ chatId: "C", parent_id: null, parentId: "",
    messages: [{ parent_id: null }] }), { chat_id: "C", parent_id: "" });
  assert.deepEqual(fns.sessionInfoOfBody({ messages: [{ parentId: "M" }] }),
    { chat_id: "", parent_id: "M" });
  assert.deepEqual(fns.sessionInfoOfBody(null), { chat_id: "", parent_id: "" });
});

const forkBody = () => JSON.stringify({
  chat_id: "C", chatId: "C", parent_id: "OLD", parentId: "OLD",
  messages: [{ role: "user", content: "x", user_action: "chat", parent_id: "OLD", parentId: "OLD" }],
});

test("applyForkParent: ghi de du 4 cho parent_id + user_action, giu phan con lai", (t) => {
  const { fns } = sessEnv();
  if (!fns) return t.skip("ham session chua co trong bridge.js");
  const req = JSON.parse(forkBody());
  assert.equal(fns.applyForkParent(req, "NEW", "edit"), true);
  assert.equal(req.parent_id, "NEW");
  assert.equal(req.parentId, "NEW");
  assert.equal(req.messages[0].parent_id, "NEW");
  assert.equal(req.messages[0].parentId, "NEW");
  assert.equal(req.messages[0].user_action, "edit");
  assert.equal(req.chat_id, "C");
  assert.equal(req.messages[0].content, "x");
  const keep = JSON.parse(forkBody());
  fns.applyForkParent(keep, "NEW", "");
  assert.equal(keep.messages[0].user_action, "chat");
  assert.equal(fns.applyForkParent(keep, "", "edit"), false);
  assert.equal(fns.applyForkParent(null, "NEW", ""), false);
});

test("rewriteRequestSession: chi fork; continue/new/khong session -> null", (t) => {
  const { fns, sent } = sessEnv();
  if (!fns) return t.skip("ham session chua co trong bridge.js");
  const init = { method: "POST", body: forkBody() };
  const out = fns.rewriteRequestSession({ id: "j",
    session: { mode: "fork", fork_parent_id: "P9", user_action: "edit" } }, init);
  const b = JSON.parse(out.body);
  assert.equal(b.parent_id, "P9");
  assert.equal(b.messages[0].parentId, "P9");
  assert.equal(out.method, "POST");
  assert.match(sent[0][1].message, /parent_id -> P9/);
  assert.equal(fns.rewriteRequestSession({ id: "j", session: { mode: "continue" } }, init), null);
  assert.equal(fns.rewriteRequestSession({ id: "j", session: { mode: "new" } }, init), null);
  assert.equal(fns.rewriteRequestSession({ id: "j" }, init), null);
  // fork nhung thieu parent / body khong phai JSON -> null (caller chan request)
  assert.equal(fns.rewriteRequestSession({ id: "j", session: { mode: "fork" } }, init), null);
  assert.equal(fns.rewriteRequestSession({ id: "j", session: { mode: "fork", fork_parent_id: "P" } },
    { body: "not json" }), null);
});

test("announceSession: gui event session voi nonce + id doc tu body (ke ca Request.clone)", async (t) => {
  const { fns, sent } = sessEnv();
  if (!fns) return t.skip("ham session chua co trong bridge.js");
  await fns.announceSession({ id: "j", session: { mode: "continue" } }, null, { body: forkBody() });
  assert.deepEqual(sent[0], ["j", { kind: "session", mode: "continue", nonce: "NONCE-1",
    chat_id: "C", parent_id: "OLD" }]);
  const req = { clone: () => ({ text: async () => forkBody() }) };
  await fns.announceSession({ id: "k", session: { mode: "fork" } }, req, {});
  assert.equal(sent[1][1].parent_id, "OLD");
  await fns.announceSession({ id: "n" }, null, { body: forkBody() });   // job khong session
  await fns.announceSession({ id: "m", session: { mode: "new" } }, null, { body: "{bad" });
  assert.equal(sent.length, 2);
});
