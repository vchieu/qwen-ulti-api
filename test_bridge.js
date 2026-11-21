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

// =============================== session_patch3: hydrate / diag / stall ======
function pageReadyEnv(opts = {}) {
  const state = { now: 1000, sent: [], warns: [] };
  const ctx = {
    PAGE_READY_MAX_MS: 8000,
    TEMP_DOM_WAIT_MS: 3000,
    // Gate moi: pageSent (request DA di). Giu opts.pageUsed lam alias cho test cu.
    pageSent: !!(opts.pageSent || opts.pageUsed),
    pageReadyMode: opts.mode || "full",
    selectModelEnabled: false,          // giong config that: bridge_select_model=false
    Date: { now: () => state.now },
    sleep: async (ms) => { state.now += ms; },
    modelTargets: () => [],
    findModelTrigger: () => (opts.picker ? {} : null),
    findComposers: () => (opts.noComposer ? [] : [{}]),
    isTemporaryPage: () => opts.temporary !== false,
    temporaryEvidence: () => ({
      dom: opts.domAt ? state.now >= opts.domAt : !!opts.dom,
    }),
    send: (_id, ev) => { state.sent.push(ev); },
    fwd: (_job, kind, msg) => { state.warns.push([kind, msg]); },
  };
  return { fns: loadFns(["waitPageReady"], ctx), state };
}

test("waitPageReady: select_model=false VAN phai doi temporary DOM (khong break ngay)", async (t) => {
  const env = pageReadyEnv({ dom: false });          // trang chua hydrate
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  const elapsed = env.state.now - 1000;
  assert.ok(elapsed >= 3000, "phai doi it nhat TEMP_DOM_WAIT_MS, khong chi sleep 1s");
  assert.equal(env.state.warns.length, 1, "phai warn trang chua hydrate");
  assert.match(env.state.warns[0][1], /CHUA hydrate/);
  assert.match(env.state.sent[0].message, /tempDom=false/);
});

test("waitPageReady: temporary DOM xuat hien -> di tiep som, khong warn", async (t) => {
  const env = pageReadyEnv({ domAt: 2000 });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  assert.ok(env.state.now - 1000 < 3000, "hydrate xong thi khong cho het nguong");
  assert.deepEqual(env.state.warns, []);
  assert.match(env.state.sent[0].message, /tempDom=true/);
});

test("waitPageReady: che do thuong (khong temporary) khong doi temp DOM", async (t) => {
  const env = pageReadyEnv({ temporary: false, dom: false });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  assert.ok(env.state.now - 1000 <= 1200);
  assert.deepEqual(env.state.warns, []);
});

test("waitPageReady: khong thay composer -> cho het nguong + warn", async (t) => {
  const env = pageReadyEnv({ noComposer: true, dom: true });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  assert.ok(env.state.now - 1000 >= 8000);
  assert.match(env.state.warns[0][1], /composer=false/);
});

test("waitPageReady: pageUsed=true -> bo qua (khong doi, khong log)", async (t) => {
  const env = pageReadyEnv({ pageUsed: true });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  assert.equal(env.state.now, 1000);
  assert.deepEqual(env.state.sent, []);
});

test("chatMessageStats: dem bubble visible + du dai, bo node ngan/an", (t) => {
  const nodes = [
    { innerText: "ngan", hidden: false },
    { innerText: "z".repeat(40), hidden: true },
    { innerText: "a".repeat(40), hidden: false },
    { innerText: "b".repeat(40), hidden: false },
  ];
  const fns = loadFns(["chatMessageStats"], {
    document: { querySelectorAll: () => nodes },
    visible: (el) => !el.hidden,
  });
  if (!fns) return t.skip("chatMessageStats chua co trong bridge.js");
  const st = fns.chatMessageStats();
  assert.equal(st.n, 2);
  assert.ok(st.last.startsWith("b"));
});

test("sendDiag: gom trace + card + bubble + tempDom", (t) => {
  const job = {
    _composerEl: {},
    _sendTrace: {
      clicks: 2, enters: 1, firstClickAt: 120, emptyAt: 900,
      samples: ["0:25000", "1000:0"], atClick: [25000, 0],
    },
  };
  const fns = loadFns(["sendDiag"], {
    pageDiag: () => "PD",
    findActiveAttachmentElements: () => [1, 2],
    chatMessageStats: () => ({ n: 0, last: "" }),
    temporaryEvidence: () => ({ dom: false }),
  });
  if (!fns) return t.skip("sendDiag chua co trong bridge.js");
  const out = fns.sendDiag(job, null);
  assert.match(out, /PD/);
  assert.match(out, /clicks=2 enters=1/);
  assert.match(out, /firstClick=\+120ms/);
  assert.match(out, /emptyAt=\+900ms/);
  assert.match(out, /composerLens=\[0:25000,1000:0\]/);
  assert.match(out, /lenAtClick=\[25000,0\]/);
  assert.match(out, /cards=2/);
  assert.match(out, /chatMsgs=0/);
  assert.match(out, /tempDom=false/);
});

function sendPromptEnv(opts = {}) {
  const state = { now: 1000, text: "hello", btn: true, clicks: 0, learned: [] };
  const job = Object.assign({
    id: "j1", prompt: opts.prompt || "hello", attached: false, settled: false,
  }, opts.job || {});
  const ctx = {
    INLINE_STALL_MIN_CHARS: 20000,
    INLINE_STALL_GRACE_MS: 8000,
    attachmentPrompt: "AP",
    ATTACHMENT_COMPANION_PROMPT: "COMP",
    Date: { now: () => state.now },
    sleep: async (ms) => { state.now += ms; },
    jobAlive: async () => true,
    findSendButton: () => (state.btn ? {
      click: () => { state.clicks++; if (opts.onClick) opts.onClick(state, job); },
    } : null),
    findStopButton: () => null,
    composerText: () => state.text,
    setComposerClean: (_c, t) => { state.text = t; },
    findActiveAttachmentElements: () => [],
    pressEnter: () => {},
    saveJobState: () => {},
    send: () => {},
    log: () => {},
    learnInlineLimit: (n) => { state.learned.push(n); },
    pageDiag: () => "PD",
    chatMessageStats: () => ({ n: 0, last: "" }),
    temporaryEvidence: () => ({ dom: false }),
  };
  return { fns: loadFns(["inlineStallWatch", "sendDiag", "sendPrompt"], ctx), state, job };
}

test("sendPrompt: inline lon + composer rong/nut Send mat -> hoc nguong + throw som + diag", async (t) => {
  const env = sendPromptEnv({
    prompt: "x".repeat(25000),
    onClick: (st) => { st.text = ""; st.btn = false; },
  });
  if (!env.fns) return t.skip("sendPrompt/inlineStallWatch chua co trong bridge.js");
  await assert.rejects(() => env.fns.sendPrompt({}, 60000, null, env.job), /attach file/);
  assert.deepEqual(env.state.learned, [25000]);
  assert.ok(env.state.now - 1000 < 20000, "phai fail trong ~8-10s, khong cho het 60s");
  const tr = env.job._sendTrace;
  assert.ok(tr && tr.atClick.length >= 1, "phai ghi lai len luc click");
  assert.equal(tr.atClick[0], 5, "len truoc click phai la text cu");
  assert.ok(tr.emptyAt >= 0, "phai ghi thoi diem composer rong");
});

test("sendPrompt: inline lon + nut Send bien mat (text con) -> throw som", async (t) => {
  const env = sendPromptEnv({
    prompt: "x".repeat(25000),
    onClick: (st) => { st.btn = false; },
  });
  if (!env.fns) return t.skip("sendPrompt/inlineStallWatch chua co trong bridge.js");
  await assert.rejects(() => env.fns.sendPrompt({}, 60000, null, env.job), /attach file/);
  assert.deepEqual(env.state.learned, [25000]);
});

test("sendPrompt: continue session KHONG stall-fallback (retry co the nhan doi delta)", async (t) => {
  const env = sendPromptEnv({
    prompt: "x".repeat(25000),
    job: { session: { mode: "continue" } },
    onClick: (st) => { st.text = ""; },
  });
  if (!env.fns) return t.skip("sendPrompt/inlineStallWatch chua co trong bridge.js");
  assert.equal(await env.fns.sendPrompt({}, 60000, null, env.job), true);  // hanh vi cu
  assert.deepEqual(env.state.learned, []);
});

test("sendPrompt: prompt nho khong stall-fallback", async (t) => {
  const env = sendPromptEnv({ prompt: "hello", onClick: (st) => { st.text = ""; } });
  if (!env.fns) return t.skip("sendPrompt/inlineStallWatch chua co trong bridge.js");
  assert.equal(await env.fns.sendPrompt({}, 60000, null, env.job), true);
  assert.deepEqual(env.state.learned, []);
});

// -------------------------------------------------- stopGenerationOnCancel ----
function stopEnv(opts = {}) {
  const state = { clicks: 0, sleeps: 0, logs: [] };
  const btn = { click: () => { state.clicks++; if (!opts.sticky) state.visible = false; } };
  state.visible = opts.visible !== false;
  state.appearAfter = opts.appearAfter || 0;   // so lan sleep truoc khi nut Stop hien
  const ctx = {
    findStopButton: () => ((state.visible && state.sleeps >= state.appearAfter) ? btn : null),
    sleep: async () => { state.sleeps++; },
    log: (m) => { state.logs.push(m); },
  };
  return { fns: loadFns(["stopGenerationOnCancel"], ctx), state };
}

test("stopGenerationOnCancel: job huy + da attached + dang generate -> bam Stop", async (t) => {
  const { fns, state } = stopEnv();
  if (!fns) return t.skip("stopGenerationOnCancel chua co trong bridge.js");
  assert.equal(await fns.stopGenerationOnCancel({ cancelled: true, attached: true }), true);
  assert.equal(state.clicks, 1);
  assert.match(state.logs[0], /bam Stop/);
});

test("stopGenerationOnCancel: khong huy / chua gui request / khong co job -> khong bam", async (t) => {
  const { fns, state } = stopEnv();
  if (!fns) return t.skip("stopGenerationOnCancel chua co trong bridge.js");
  assert.equal(await fns.stopGenerationOnCancel({ cancelled: false, attached: true }), false);
  assert.equal(await fns.stopGenerationOnCancel({ cancelled: true, attached: false }), false);
  assert.equal(await fns.stopGenerationOnCancel(null), false);
  assert.equal(state.clicks, 0);
});

test("stopGenerationOnCancel: nut Stop hien cham -> doi toi ~1.5s roi bam", async (t) => {
  const { fns, state } = stopEnv({ appearAfter: 3 });
  if (!fns) return t.skip("stopGenerationOnCancel chua co trong bridge.js");
  assert.equal(await fns.stopGenerationOnCancel({ cancelled: true, attached: true }), true);
  assert.equal(state.clicks, 1);
});

test("stopGenerationOnCancel: Qwen da dung san (khong co nut Stop) -> khong bam, false", async (t) => {
  const { fns, state } = stopEnv({ visible: false });
  if (!fns) return t.skip("stopGenerationOnCancel chua co trong bridge.js");
  assert.equal(await fns.stopGenerationOnCancel({ cancelled: true, attached: true }), false);
  assert.equal(state.clicks, 0);
});

test("stopGenerationOnCancel: nut Stop khong tat -> toi da 3 lan bam", async (t) => {
  const { fns, state } = stopEnv({ sticky: true });
  if (!fns) return t.skip("stopGenerationOnCancel chua co trong bridge.js");
  await fns.stopGenerationOnCancel({ cancelled: true, attached: true });
  assert.equal(state.clicks, 3);
});

// ================== F1/F2: nguong inline bao ve Python + companion + fingerprint ====
function adoptEnv(opts = {}) {
  const state = { posted: [], stored: [] };
  const ctx = {
    INLINE_LIMIT_KEY: "K",
    INLINE_LIMIT_CAP: 32000,
    inlineMaxChars: opts.limit === undefined ? Infinity : opts.limit,
    sessionStorage: { setItem: (k, v) => state.stored.push([k, v]), getItem: () => null },
    console: { log: () => {} },
    post: (p, b) => { state.posted.push([p, b]); return Promise.resolve({}); },
    tabInfo: () => ({ nonce: "N" }),
  };
  return { fns: loadFns(["adoptInlineLimit", "learnInlineLimit"], ctx), state };
}

test("adoptInlineLimit: chi giam, ghi sessionStorage, bo gia tri rac", (t) => {
  const env = adoptEnv({ limit: 40000 });
  if (!env.fns) return t.skip("adoptInlineLimit chua co trong bridge.js");
  assert.equal(env.fns.adoptInlineLimit(32000), true);
  assert.deepEqual(env.state.stored, [["K", "32000"]]);
  assert.equal(env.fns.adoptInlineLimit(35000), false);   // khong nang len
  assert.equal(env.fns.adoptInlineLimit(32000), false);   // bang nhau
  for (const bad of [0, -1, "abc", null, undefined, NaN]) {
    assert.equal(env.fns.adoptInlineLimit(bad), false, String(bad));
  }
  assert.equal(env.fns.adoptInlineLimit(30000), true);    // giam tiep tu 32000
});

test("learnInlineLimit: bao Python NGAY qua /event ping, khong bao lai khi khong giam", (t) => {
  const env = adoptEnv();
  if (!env.fns) return t.skip("learnInlineLimit chua co trong bridge.js");
  env.fns.learnInlineLimit(95991);
  assert.equal(env.state.posted.length, 1);
  const [path, body] = env.state.posted[0];
  assert.equal(path, "/event");
  assert.equal(body.kind, "ping");
  assert.equal(body.inline_limit, 32000);                 // tran INLINE_LIMIT_CAP
  assert.deepEqual(body.tab, { nonce: "N" });
  env.fns.learnInlineLimit(95991);
  assert.equal(env.state.posted.length, 1);
});

function inlineFileEnv(opts = {}) {
  const sent = [];
  const ctx = {
    inlineMaxChars: opts.limit === undefined ? 32000 : opts.limit,
    attachmentPrompt: "CONFIG_COMPACTED_HISTORY",
    ATTACHMENT_COMPANION_PROMPT: "COMP",
    fingerprintText: async (txt) => ({
      bytes: Buffer.byteLength(txt, "utf8"), sha256: "ab".repeat(32),
    }),
    send: (id, ev) => sent.push(ev),
  };
  return { fns: loadFns(["maybeInlineToFile"], ctx), sent };
}

test("maybeInlineToFile: file_info CO bytes/sha256 + companion uu tien session", async (t) => {
  const env = inlineFileEnv();
  if (!env.fns) return t.skip("maybeInlineToFile chua co trong bridge.js");
  const text = "xin chào ".repeat(4000);                  // 36000 ky tu, 40000 byte
  const job = { id: "j1", prompt: text,
                session: { mode: "new", attach_prompt: "SESSION_FULL" } };
  assert.equal(await env.fns.maybeInlineToFile(job), true);
  assert.equal(job.file_info.filename, "qwen_prompt_inline_j1.txt");
  assert.equal(job.file_info.size, text.length);
  assert.equal(job.file_info.bytes, Buffer.byteLength(text, "utf8"));
  assert.ok(job.file_info.bytes > job.file_info.size);
  assert.equal(job.file_info.sha256, "ab".repeat(32));
  assert.equal(job.file_info._inlineText, text);
  assert.equal(job.attachment_prompt, "SESSION_FULL");    // KHONG dung config
  assert.match(env.sent[0].message, /attach file/);
  assert.match(env.sent[0].message, /companion=session\//);
});

test("maybeInlineToFile: khong session -> config; Python da tach -> giu nguyen", async (t) => {
  const env = inlineFileEnv();
  if (!env.fns) return t.skip("maybeInlineToFile chua co trong bridge.js");
  const legacy = { id: "j2", prompt: "x".repeat(40000) };
  assert.equal(await env.fns.maybeInlineToFile(legacy), true);
  assert.equal(legacy.attachment_prompt, "CONFIG_COMPACTED_HISTORY");
  const split = { id: "j3", prompt: "x".repeat(40000),
                  attachment_prompt: "PYTHON_COMPANION",
                  session: { mode: "new", attach_prompt: "SESSION_FULL" } };
  assert.equal(await env.fns.maybeInlineToFile(split), true);
  assert.equal(split.attachment_prompt, "PYTHON_COMPANION");
});

test("maybeInlineToFile: duoi nguong / da co file_info -> khong doi, khong log", async (t) => {
  const env = inlineFileEnv();
  if (!env.fns) return t.skip("maybeInlineToFile chua co trong bridge.js");
  const small = { id: "a", prompt: "x".repeat(31999) };
  assert.equal(await env.fns.maybeInlineToFile(small), false);
  assert.equal(small.file_info, undefined);
  const fi = { filename: "qwen_prompt_py.txt", bytes: 10 };
  const hasFile = { id: "b", prompt: "x".repeat(50000), file_info: fi };
  assert.equal(await env.fns.maybeInlineToFile(hasFile), false);
  assert.equal(hasFile.file_info, fi);
  assert.equal(env.sent.length, 0);
});

test("classifyRequestFiles: inline file co bytes -> guard size bat duoc lech", (t) => {
  const e = requestEnv();
  if (!e) return t.skip("helper request chua co trong bridge.js");
  const name = "qwen_prompt_inline_j.txt";
  const reqFile = (size) => ({
    type: "file", id: "f1", name, size, status: "uploaded",
    file: { id: "f1", filename: name,
            meta: { name, size, content_type: "text/plain" } },
  });
  const job = { file_info: { filename: name, size: 36000, bytes: 40000 } };
  const check = (size) => e.checkRequestFiles(job, e.collectRequestFiles(body([reqFile(size)])));
  assert.deepEqual(check(40000), []);
  assert.match(check(39000)[0], /THIEU/);
});

// ================= hydrate: che do A/B + gate pageSent + duong cong hydrate ========
test("waitPageReady: mode=sleep_only -> chi sleep 1s, khong doi, khong warn", async (t) => {
  const env = pageReadyEnv({ dom: false, mode: "sleep_only" });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  assert.equal(env.state.now - 1000, 1000);
  assert.deepEqual(env.state.warns, []);
  assert.match(env.state.sent[0].message, /mode=sleep_only/);
  assert.match(env.state.sent[0].message, /loop=0ms\/1 rounds/);
});

test("waitPageReady: mode=off -> khong doi, khong sleep", async (t) => {
  const env = pageReadyEnv({ dom: false, mode: "off" });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  assert.equal(env.state.now, 1000);
  assert.deepEqual(env.state.warns, []);
  assert.match(env.state.sent[0].message, /mode=off/);
});

test("waitPageReady: gate theo pageSent chu khong pageUsed (retry van duoc doi)", async (t) => {
  const chua = pageReadyEnv({ dom: false, pageSent: false });
  const roi = pageReadyEnv({ dom: false, pageSent: true });
  if (!chua.fns || !roi.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await chua.fns.waitPageReady({ id: "j" });
  await roi.fns.waitPageReady({ id: "j" });
  assert.ok(chua.state.now - 1000 >= 3000, "chua gui request -> van phai doi");
  assert.equal(roi.state.now, 1000, "da gui request -> bo qua");
  assert.deepEqual(roi.state.sent, []);
});

test("waitPageReady: log tach loop/sleep/rounds", async (t) => {
  const env = pageReadyEnv({ domAt: 2000 });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  const m = env.state.sent[0].message;
  // domAt=2000, sleep(300): thay dom o lan kiem tra now=2200 -> loop=1200ms, 5 rounds.
  assert.match(m, /loop=1200ms\/\d+ rounds/);
  assert.match(m, /sleep=1000ms/);
  assert.match(m, /total=2200ms/);
});

function hydrationProbeEnv(opts = {}) {
  const state = { now: 5000, posted: [] };
  const el = { "__reactProps$x": {} };
  const ctx = {
    HYDRATION_PROBE_MS: 1000,
    HYDRATION_PROBE_STEP_MS: 200,
    Date: { now: () => state.now },
    sleep: async (ms) => { state.now += ms; },
    location: { pathname: "/c/local" },
    findComposers: () => {
      if (opts.boom) throw new Error("DOM chet");
      return (opts.composerAt != null && state.now >= opts.composerAt) ? [el] : [];
    },
    findModelTrigger: () => (opts.pickerAt != null && state.now >= opts.pickerAt ? {} : null),
    temporaryEvidence: () => ({ dom: opts.domAt != null && state.now >= opts.domAt }),
    post: (p, b) => { state.posted.push([p, b]); return Promise.resolve({}); },
  };
  return { fns: loadFns(["hydrationProbe"], ctx), state };
}

test("hydrationProbe: ghi moc thoi gian tung tin hieu, gui 1 event probe", async (t) => {
  const env = hydrationProbeEnv({ composerAt: 5200, domAt: 5600 });
  if (!env.fns) return t.skip("hydrationProbe chua co trong bridge.js");
  await env.fns.hydrationProbe();
  assert.equal(env.state.posted.length, 1);
  const [path, body] = env.state.posted[0];
  assert.equal(path, "/event");
  assert.equal(body.kind, "probe");
  assert.equal(body.job_id, "none");
  assert.deepEqual(body.probe, { composer: 200, picker: -1, tempDom: 600, reactProps: 200 });
});

test("hydrationProbe: khong thay gi -> -1 van gui; DOM nem loi -> khong crash", async (t) => {
  const empty = hydrationProbeEnv();
  const boom = hydrationProbeEnv({ boom: true });
  if (!empty.fns || !boom.fns) return t.skip("hydrationProbe chua co trong bridge.js");
  await empty.fns.hydrationProbe();
  await boom.fns.hydrationProbe();
  assert.deepEqual(empty.state.posted[0][1].probe,
    { composer: -1, picker: -1, tempDom: -1, reactProps: -1 });
  assert.equal(boom.state.posted.length, 1);
});

test("hydrationProbe: tham so ms rut ngan cua so do", async (t) => {
  const env = hydrationProbeEnv({ domAt: 5400 });
  if (!env.fns) return t.skip("hydrationProbe chua co trong bridge.js");
  await env.fns.hydrationProbe(400);
  assert.deepEqual(env.state.posted[0][1].probe,
    { composer: -1, picker: -1, tempDom: 400, reactProps: -1 });
});

test("waitPageReady: tempDom chi xong TRONG luc sleep -> lay mau lai, khong warn nham", async (t) => {
  // domAt phai nam trong (4000, 5000]: vong loop break vi timeout o now=4000 (elapsed 3000),
  // trong luc sleep(1000) dong ho chay tiep den 5000 -> tempDom bat dau true.
  // (domAt=3500 se co dom=true ngay tu now=3700, break som 2700ms -> khong test duoc truong hop.)
  const env = pageReadyEnv({ domAt: 4500 });
  if (!env.fns) return t.skip("waitPageReady chua co trong bridge.js");
  await env.fns.waitPageReady({ id: "j" });
  const m = env.state.sent[0].message;
  assert.match(m, /tempDom=true/);          // sau sleep: da hydrate
  assert.match(m, /tempDomAtLoop=false/);   // truoc sleep: chua
  assert.deepEqual(env.state.warns, []);    // khong warn nham nua
  assert.match(m, /loop=3000ms\/\d+ rounds/);
  assert.match(m, /total=4000ms/);
});
