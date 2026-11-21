"""Test che do session (qwen_web2api/session.py + noi vao server/bridge).

    python -m unittest test_session -v

Khong browser, khong network: extension duoc gia bang _FakeTab (tra loi job ngay qua
bridge._pending), server that chay tren loopback port 0.
"""
import copy
import http.client
import json
import queue
import threading
import time
import unittest
from unittest import mock

from qwen_web2api import bridge, qwen, server
from qwen_web2api import session as sess
from qwen_web2api.config import CONFIG

TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]
TOOLS2 = TOOLS + [{"type": "function", "function": {
    "name": "grep", "description": "g", "parameters": {"type": "object", "properties": {}}}}]
TAB = {"nonce": "N1", "chat_id": ""}


def call(cid, name="read_file", args='{"path": "a.py"}'):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def asst_calls(*ids):
    return {"role": "assistant", "content": None, "tool_calls": [call(i) for i in ids]}


def tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self._config = copy.deepcopy(CONFIG)
        self._bridge = {"_last_seen": bridge._last_seen,
            "_inline_limit": bridge._inline_limit, "_pending": bridge._pending,
                        "_tab": bridge._tab, "_tab_ts": bridge._tab_ts,
                        "_models": list(bridge._models)}
        self._cancelled = bridge._cancelled.copy()
        CONFIG["log_requests"] = False
        CONFIG["retry_attempts"] = 1
        CONFIG["retry_delay_sec"] = 0
        CONFIG["bridge_session_mode"] = True
        bridge._cancelled.clear()
        bridge._managed_jobs.clear()
        sess.STORE.reset("test setup")
        sess.STORE.last_reset = ""

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self._config)
        for k, v in self._bridge.items():
            setattr(bridge, k, v)
        bridge._cancelled.clear()
        bridge._cancelled.update(self._cancelled)
        sess.STORE.reset("test teardown")


def _commit_new(store, messages, reply="ok", calls=None, tools=TOOLS, nonce="N1",
                chat="C1", parent="", plan_tab=None):
    plan = store.plan(messages, tools, "auto", tab=plan_tab)
    plan.delta_chars = 100
    plan.out.update({"nonce": nonce, "chat_id": chat, "parent_id": parent, "sent": True})
    s = store.commit(plan, reply, calls)
    return plan, s


# ================================================================ pure logic ===
class TestHashing(_Sandbox):

    def test_assistant_text_only_counted_not_compared(self):
        a = sess.canon_message({"role": "assistant", "content": "Hello  there "})
        b = sess.canon_message({"role": "assistant", "content": "Hello there"})
        self.assertEqual(a, b)
        s1 = sess.canon_message({"role": "assistant", "content": "x"}, True)
        s2 = sess.canon_message({"role": "assistant", "content": "y"}, True)
        self.assertNotEqual(s1, s2)

    def test_crlf_and_strip_normalized_for_user_and_tool(self):
        self.assertEqual(sess.canon_message({"role": "user", "content": "a\r\nb\n"}),
                         sess.canon_message({"role": "user", "content": "a\nb"}))
        self.assertEqual(sess.canon_message(tool("c1", "x\r\n")),
                         sess.canon_message(tool("c1", "x")))

    def test_roles_and_tool_calls_distinguish(self):
        self.assertNotEqual(sess.canon_message({"role": "user", "content": "a"}),
                            sess.canon_message({"role": "system", "content": "a"}))
        self.assertEqual(sess.canon_message({"role": "developer", "content": "a"}),
                         sess.canon_message({"role": "system", "content": "a"}))
        self.assertNotEqual(sess.canon_message(asst_calls("c1")),
                            sess.canon_message(asst_calls("c2")))
        # tool_calls: chi id+name, noi dung assistant khac nhau van khop
        m = asst_calls("c1")
        m2 = dict(m, content="prose")
        self.assertEqual(sess.canon_message(m), sess.canon_message(m2))
        self.assertNotEqual(sess.canon_message(tool("c1", "a")),
                            sess.canon_message(tool("c2", "a")))

    def test_openai_tool_vs_cline_text_differ(self):
        self.assertNotEqual(sess.canon_message(asst_calls("c1")),
                            sess.canon_message({"role": "assistant", "content": "<x/>"}))

    def test_hash_ignore_drops_volatile_blocks(self):
        a = {"role": "user", "content": "task\n<environment_details>time 10:00</environment_details>"}
        b = {"role": "user", "content": "task\n<environment_details>time 10:05\nmore</environment_details>"}
        self.assertEqual(sess.canon_message(a), sess.canon_message(b))
        CONFIG["session_hash_ignore"] = []
        self.assertNotEqual(sess.canon_message(a), sess.canon_message(b))
        CONFIG["session_hash_ignore"] = ["(unclosed", r"time \d+:\d+"]   # regex sai bi bo qua
        self.assertEqual(sess.canon_message(a), sess.canon_message(
            {"role": "user", "content": "task\n<environment_details>time 11:11</environment_details>"}))

    def test_ctx_hash_changes_with_tools(self):
        self.assertEqual(sess.ctx_hash(TOOLS), sess.ctx_hash(copy.deepcopy(TOOLS)))
        self.assertNotEqual(sess.ctx_hash(TOOLS), sess.ctx_hash(TOOLS2))
        self.assertEqual(sess.ctx_hash(None), sess.ctx_hash([]))


class TestPlan(_Sandbox):
    M1 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]

    def _history(self):
        """Session da co: [sys,user,assistant(call c1)] sau luot 1 (new)."""
        _plan, s = _commit_new(sess.STORE, self.M1, "", [call("c1")])
        self.assertIsNotNone(s)
        return self.M1 + [asst_calls("c1")]

    def test_first_turn_is_new_and_commit_creates_session(self):
        plan = sess.STORE.plan(self.M1, TOOLS, "auto", tab=None)
        self.assertEqual((plan.kind, plan.reason), ("new", "no_session"))
        self.assertIsNone(sess.STORE.get())
        _plan, s = _commit_new(sess.STORE, self.M1, "", [call("c1")])
        self.assertEqual(s.nonce, "N1")
        self.assertEqual(len(s.hashes), 3)
        self.assertEqual(s.turns, 1)

    def test_commit_without_nonce_creates_no_session(self):
        plan = sess.STORE.plan(self.M1, TOOLS, "auto", tab=None)
        plan.out.update({"chat_id": "C1"})
        self.assertIsNone(sess.STORE.commit(plan, "ok", None))
        self.assertIsNone(sess.STORE.get())

    def test_continue_with_tool_result_inline(self):
        hist = self._history()
        msgs = hist + [tool("c1", "FILE BODY")]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        self.assertEqual(plan.kind, "continue")
        self.assertEqual(plan.base_n, 3)
        self.assertIsNone(plan.file_text)
        self.assertIn("[Tool results]", plan.prompt)
        self.assertIn("=== RESULT tool=read_file id=c1 ===\nFILE BODY\n=== END RESULT c1 ===",
                      plan.prompt)
        self.assertNotIn("# Tool Use", plan.prompt)
        self.assertNotIn("sys", plan.prompt)

    def test_multiple_results_keep_order_and_missing_marked(self):
        _commit_new(sess.STORE, self.M1, "", [call("c1"), call("c2"), call("c3")])
        hist = self.M1 + [asst_calls("c1", "c2", "c3")]
        msgs = hist + [tool("c1", "R1"), tool("c3", "R3")]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        p = plan.prompt
        self.assertLess(p.index("id=c1 ==="), p.index("id=c3 ==="))
        self.assertIn("=== RESULT tool=read_file id=c2 MISSING ===", p)

    def test_user_text_after_tool_results(self):
        hist = self._history()
        plan = sess.STORE.plan(hist + [tool("c1", "R"), {"role": "user", "content": "thanks"}],
                               TOOLS, "auto", tab=TAB)
        self.assertEqual(plan.kind, "continue")
        self.assertIn("[User message]\nthanks", plan.prompt)
        self.assertLess(plan.prompt.index("RESULT tool"), plan.prompt.index("[User message]"))

    def test_user_only_delta_verbatim_no_wrapper(self):
        hist = self._history()
        msgs = hist + [tool("c1", "R")]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        plan.delta_chars = 10
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "P1", "sent": True})
        sess.STORE.commit(plan, "final answer", None)
        msgs += [{"role": "assistant", "content": "final answer"},
                 {"role": "user", "content": "now fix b.py\r\nplease"}]
        plan2 = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        self.assertEqual(plan2.kind, "continue")
        self.assertEqual(plan2.prompt, "now fix b.py\nplease")

    def test_cline_style_no_tools_user_only(self):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "task"}]
        _commit_new(sess.STORE, msgs, "<read_file>x</read_file>", None, tools=None)
        msgs2 = msgs + [{"role": "assistant", "content": "<read_file>x</read_file>"},
                        {"role": "user", "content": "[read_file] result...\n<environment_details>"}]
        plan = sess.STORE.plan(msgs2, None, "auto", tab=TAB)
        self.assertEqual(plan.kind, "continue")
        self.assertFalse(plan.tools_active)
        self.assertEqual(plan.prompt, "[read_file] result...\n<environment_details>")
        self.assertEqual(sess.STORE.preamble(False), sess.DEFAULT_PROMPTS["preamble_plain"])
        self.assertNotIn("tool_call", sess.DEFAULT_PROMPTS["preamble_plain"])

    def test_reminder_every_n_turns(self):
        CONFIG["session_reminder_every"] = 2
        msgs = [{"role": "user", "content": "q0"}]
        _commit_new(sess.STORE, msgs, "a0", None, tools=None)
        msgs += [{"role": "assistant", "content": "a0"}, {"role": "user", "content": "q1"}]
        p1 = sess.STORE.plan(msgs, None, "auto", tab=TAB)       # turn 2 -> nhac
        self.assertIn("[Reminder]", p1.prompt)
        CONFIG["session_reminder_every"] = 0
        p2 = sess.STORE.plan(msgs, None, "auto", tab=TAB)
        self.assertEqual(p2.prompt, "q1")

    def test_tool_choice_required_appended(self):
        hist = self._history()
        plan = sess.STORE.plan(hist + [tool("c1", "R")], TOOLS, "required", tab=TAB)
        self.assertIn("You MUST call at least one tool", plan.prompt)

    def test_large_result_goes_to_file(self):
        CONFIG["session_file_threshold"] = 1000
        hist = self._history()
        plan = sess.STORE.plan(hist + [tool("c1", "Z" * 5000)], TOOLS, "auto", tab=TAB)
        self.assertEqual(plan.kind, "continue")
        self.assertIsNotNone(plan.file_text)
        self.assertIn("=== RESULT tool=read_file id=c1 ===", plan.file_text)
        self.assertIn("read_file#c1 (5 KB)", plan.prompt)
        self.assertNotIn("Z" * 100, plan.prompt)
        self.assertIn("```tool_call", plan.prompt)
        # nguong 0 = luon inline
        CONFIG["session_file_threshold"] = 0
        plan = sess.STORE.plan(hist + [tool("c1", "Z" * 5000)], TOOLS, "auto", tab=TAB)
        self.assertIsNone(plan.file_text)

    def test_large_user_message_goes_to_file_with_message_prompt(self):
        CONFIG["session_file_threshold"] = 1000
        msgs = [{"role": "user", "content": "q0"}]
        _commit_new(sess.STORE, msgs, "a0", None, tools=None)
        msgs += [{"role": "assistant", "content": "a0"},
                 {"role": "user", "content": "W" * 4000}]
        plan = sess.STORE.plan(msgs, None, "auto", tab=TAB)
        self.assertEqual(plan.file_text, "W" * 4000)
        self.assertIn("[Message]", plan.prompt)

    def test_attach_prompt_matches_delta_kind(self):
        hist = self._history()
        res = sess.STORE.plan(hist + [tool("c1", "R")], TOOLS, "auto", tab=TAB)
        self.assertIn("system output of your previous tool_call(s)",
                      res.job_session()["attach_prompt"])
        usr = sess.STORE.plan(hist + [tool("c1", "R"), {"role": "user", "content": "hi"}],
                              TOOLS, "required", tab=TAB)
        self.assertIn("system output", usr.job_session()["attach_prompt"])   # co tool result
        self.assertIn("You MUST call at least one tool", usr.job_session()["attach_prompt"])
        # delta chi la tin user: cau chung ve "next message", khong nhac tool result
        _plan, _s = _commit_new(sess.STORE, [{"role": "user", "content": "q0"}], "a0", None,
                                tools=None)
        p = sess.STORE.plan([{"role": "user", "content": "q0"},
                             {"role": "assistant", "content": "a0"},
                             {"role": "user", "content": "q1"}], None, "auto", tab=TAB)
        self.assertIn("my next message", p.job_session()["attach_prompt"])
        self.assertNotIn("tool_call", p.job_session()["attach_prompt"])

    def test_preamble_says_this_message(self):
        for key in ("preamble_tools", "preamble_plain"):
            self.assertIn("Everything in this message", sess.DEFAULT_PROMPTS[key])
            self.assertNotIn("Everything above", sess.DEFAULT_PROMPTS[key])

    def test_prompts_override_from_config(self):
        CONFIG["session_prompts"] = {"tool_results": "RES:\n{results}\n{user_part}GO",
                                     "bogus": "x", "nudge": ""}
        hist = self._history()
        plan = sess.STORE.plan(hist + [tool("c1", "R")], TOOLS, "auto", tab=TAB)
        self.assertTrue(plan.prompt.startswith("RES:\n=== RESULT"))
        self.assertTrue(plan.prompt.endswith("GO"))
        self.assertEqual(sess.prompts()["nudge"], sess.DEFAULT_PROMPTS["nudge"])


class TestResets(_Sandbox):
    M1 = TestPlan.M1

    def setUp(self):
        super().setUp()
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        self.hist = self.M1 + [asst_calls("c1")]
        self.good = self.hist + [tool("c1", "R")]

    def _kind(self, msgs, tools=TOOLS, tab=TAB, **kw):
        p = sess.STORE.plan(msgs, tools, "auto", tab=tab, **kw)
        return p.kind, p.reason

    def test_baseline_continue(self):
        self.assertEqual(self._kind(self.good)[0], "continue")

    def test_tab_reload_resets(self):
        self.assertEqual(self._kind(self.good, tab={"nonce": "N2"}), ("new", "tab_reloaded"))
        self.assertIsNone(sess.STORE.get())

    def test_no_tab_info_resets(self):
        self.assertEqual(self._kind(self.good, tab=None), ("new", "no_tab_info"))

    def test_tab_on_other_chat_resets(self):
        self.assertEqual(self._kind(self.good, tab={"nonce": "N1", "chat_id": "OTHER"}),
                         ("new", "tab_chat_changed"))

    def test_tools_changed_resets(self):
        self.assertEqual(self._kind(self.good, tools=TOOLS2), ("new", "tools_changed"))

    def test_edited_old_message_diverges(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "EDITED"},
                asst_calls("c1"), tool("c1", "R")]
        kind, why = self._kind(msgs)
        self.assertEqual(kind, "new")
        self.assertTrue(why.startswith("history_diverged@1/3"), why)

    def test_compaction_resets(self):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "summary of before"},
                {"role": "user", "content": "next"}]
        self.assertEqual(self._kind(msgs)[0], "new")

    def test_retry_same_request_resets_without_fork(self):
        kind, why = self._kind(self.hist)          # thieu tin moi: client retry
        self.assertEqual(kind, "new")
        self.assertTrue(why.startswith("retry_or_rewind"), why)

    def test_assistant_in_delta_resets(self):
        msgs = self.hist + [{"role": "assistant", "content": "client-made"},
                            {"role": "user", "content": "x"}]
        self.assertEqual(self._kind(msgs)[0], "new")
        self.assertIsNone(sess.STORE.get())

    def test_trailing_assistant_resets(self):
        msgs = self.good + [{"role": "assistant", "content": "x"}]
        self.assertEqual(self._kind(msgs)[0], "new")

    def test_api_changed_resets(self):
        self.assertEqual(self._kind(self.good, api="responses"), ("new", "api_changed"))

    def test_limits_reset(self):
        CONFIG["session_max_turns"] = 1
        self.assertEqual(self._kind(self.good), ("new", "max_turns"))

    def test_max_chars_and_idle(self):
        CONFIG["session_max_chars"] = 10
        sess.STORE.get().chars_sent = 11
        self.assertEqual(self._kind(self.good), ("new", "max_chars"))

    def test_idle_ttl(self):
        CONFIG["session_idle_ttl"] = 5
        sess.STORE.get().last_used = time.time() - 10
        self.assertEqual(self._kind(self.good), ("new", "idle_ttl"))

    def test_new_plan_resets_existing_session_immediately(self):
        self.assertIsNotNone(sess.STORE.get())
        self._kind(self.good, tab={"nonce": "OTHER"})
        self.assertIsNone(sess.STORE.get())

    def test_strict_assistant_text_mismatch(self):
        CONFIG["session_strict_assistant"] = True
        sess.STORE.reset("x")
        m = [{"role": "user", "content": "q"}]
        _commit_new(sess.STORE, m, "answer one", None, tools=None)
        ok = m + [{"role": "assistant", "content": "answer one"},
                  {"role": "user", "content": "q2"}]
        bad = m + [{"role": "assistant", "content": "something else"},
                   {"role": "user", "content": "q2"}]
        self.assertEqual(sess.STORE.plan(ok, None, "auto", tab=TAB).kind, "continue")
        self.assertEqual(sess.STORE.plan(bad, None, "auto", tab=TAB).kind, "new")


class TestCommitAndFail(_Sandbox):
    M1 = TestPlan.M1

    def test_commit_updates_chain_counters_and_checkpoints(self):
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        msgs = self.M1 + [asst_calls("c1"), tool("c1", "R")]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "ASSIST1", "sent": True})
        s = sess.STORE.commit(plan, "done", None)
        self.assertEqual(s.turns, 2)
        self.assertEqual(len(s.hashes), 5)
        self.assertEqual(s.checkpoints, [{"n": 0, "parent": None},
                                         {"n": 3, "parent": "ASSIST1"}])
        nxt = msgs + [{"role": "assistant", "content": "done"},
                      {"role": "user", "content": "thanks"}]
        self.assertEqual(sess.STORE.plan(nxt, TOOLS, "auto", tab=TAB).kind, "continue")

    def test_commit_with_other_nonce_resets(self):
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        plan = sess.STORE.plan(self.M1 + [asst_calls("c1"), tool("c1", "R")], TOOLS, "auto",
                               tab=TAB)
        plan.out.update({"nonce": "N2", "chat_id": "C1", "sent": True})
        self.assertIsNone(sess.STORE.commit(plan, "x", None))
        self.assertIsNone(sess.STORE.get())

    def test_commit_dirty_resets(self):
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        plan = sess.STORE.plan(self.M1 + [asst_calls("c1"), tool("c1", "R")], TOOLS, "auto",
                               tab=TAB)
        plan.out.update({"nonce": "N1", "dirty": True})
        self.assertIsNone(sess.STORE.commit(plan, "x", None))
        self.assertIsNone(sess.STORE.get())

    def test_fail_after_sent_resets_before_sent_keeps(self):
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        msgs = self.M1 + [asst_calls("c1"), tool("c1", "R")]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        sess.STORE.fail(plan, "ui timeout")           # chua gui
        self.assertIsNotNone(sess.STORE.get())
        plan.out["sent"] = True
        sess.STORE.fail(plan, "stream error")
        self.assertIsNone(sess.STORE.get())

    def test_fail_session_lost_resets(self):
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        plan = sess.STORE.plan(self.M1 + [asst_calls("c1"), tool("c1", "R")], TOOLS, "auto",
                               tab=TAB)
        plan.out["session_lost"] = True
        sess.STORE.fail(plan, "lost")
        self.assertIsNone(sess.STORE.get())

    def test_responses_api_commits_two_assistant_items(self):
        m = [{"role": "user", "content": "q"}]
        plan = sess.STORE.plan(m, TOOLS, "auto", tab=None, api="responses")
        plan.out.update({"nonce": "N1", "chat_id": "C1"})
        s = sess.STORE.commit(plan, "let me look", [call("c1")])
        self.assertEqual(len(s.hashes), 3)
        echoed = server._responses_messages([
            {"role": "user", "content": "q"},
            {"type": "function_call", "call_id": "c1", "name": "read_file",
             "arguments": '{"path": "a.py"}'},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "let me look"}]},
            {"type": "function_call_output", "call_id": "c1", "output": "R"}], None)
        self.assertEqual(sess.hash_messages(echoed[:3]), s.hashes)
        self.assertEqual(sess.STORE.plan(echoed, TOOLS, "auto", tab=TAB,
                                         api="responses").kind, "continue")


class TestFork(_Sandbox):
    M1 = TestPlan.M1

    def setUp(self):
        super().setUp()
        CONFIG["session_fork"] = True
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        self.m2 = self.M1 + [asst_calls("c1"), tool("c1", "R")]
        plan = sess.STORE.plan(self.m2, TOOLS, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "A1", "sent": True})
        sess.STORE.commit(plan, "answer", None)
        self.m3 = self.m2 + [{"role": "assistant", "content": "answer"},
                             {"role": "user", "content": "more"}]
        plan = sess.STORE.plan(self.m3, TOOLS, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "A2", "sent": True})
        sess.STORE.commit(plan, "answer2", None)

    def test_client_retry_forks_last_delta_under_same_parent(self):
        plan = sess.STORE.plan(self.m3, TOOLS, "auto", tab=TAB)   # khong co assistant cuoi
        self.assertEqual(plan.kind, "fork")
        self.assertEqual(plan.parent_id, "A2")
        self.assertEqual(plan.base_n, 5)
        self.assertEqual(plan.prompt, "more")
        self.assertEqual(plan.reason, "retry_same_history")
        js = plan.job_session()
        self.assertEqual((js["mode"], js["fork_parent_id"], js["user_action"]),
                         ("fork", "A2", "edit"))

    def test_undo_last_turn_and_new_message_forks(self):
        msgs = self.m2 + [{"role": "assistant", "content": "answer"},
                          {"role": "user", "content": "different"}]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        self.assertEqual((plan.kind, plan.parent_id, plan.base_n), ("fork", "A2", 5))
        self.assertEqual(plan.prompt, "different")

    def test_undo_two_turns_forks_at_earlier_checkpoint(self):
        msgs = self.m2[:3] + [tool("c1", "R2")]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        self.assertEqual((plan.kind, plan.parent_id, plan.base_n), ("fork", "A1", 3))

    def test_fork_commit_truncates_checkpoints_and_chain(self):
        plan = sess.STORE.plan(self.m3, TOOLS, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "A2", "sent": True})
        s = sess.STORE.commit(plan, "answer2b", None)
        self.assertEqual([c["n"] for c in s.checkpoints], [0, 3, 5])
        self.assertEqual(len(s.hashes), 7)

    def test_fork_disabled_resets(self):
        CONFIG["session_fork"] = False
        self.assertEqual(sess.STORE.plan(self.m3, TOOLS, "auto", tab=TAB).kind, "new")

    def test_divergence_before_any_checkpoint_resets(self):
        msgs = [{"role": "system", "content": "other system"}, {"role": "user", "content": "go"}]
        self.assertEqual(sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB).kind, "new")

    def test_edit_in_middle_with_assistant_in_delta_resets(self):
        msgs = self.M1 + [asst_calls("c1"), tool("c1", "CHANGED"),
                          {"role": "assistant", "content": "answer"},
                          {"role": "user", "content": "more"}]
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        # delta tu checkpoint 3 = tool CHANGED + assistant + user: co tin assistant do client
        # tu ghep => khong the fork o do -> mo chat moi (replay day du).
        self.assertEqual(plan.kind, "new")
        self.assertTrue(plan.reason.startswith("history_diverged@3/"), plan.reason)


class TestRetryCall(_Sandbox):
    M1 = TestPlan.M1

    def _plan(self):
        _commit_new(sess.STORE, self.M1, "", [call("c1")])
        plan = sess.STORE.plan(self.M1 + [asst_calls("c1"), tool("c1", "R")], TOOLS, "auto",
                               tab=TAB)
        return plan

    def test_append_sends_only_nudge_without_file(self):
        plan = self._plan()
        prompt, fi, s = plan.retry_call(plan.prompt, {"path": "x"}, "\n\nIMPORTANT: fix it")
        self.assertEqual(prompt, "[Format error]\nIMPORTANT: fix it")
        self.assertIsNone(fi)
        self.assertEqual(s["mode"], "continue")
        self.assertNotIn("fork_parent_id", s)

    def test_fork_nudge_resends_delta_under_attempt_parent(self):
        CONFIG["session_fork"] = True
        CONFIG["session_nudge_mode"] = "fork"
        plan = self._plan()
        plan.out["parent_id"] = "PARENT9"
        prompt, fi, s = plan.retry_call(plan.prompt, {"path": "x"}, "\n\nIMPORTANT: fix it")
        self.assertTrue(prompt.startswith(plan.prompt))
        self.assertTrue(prompt.endswith("IMPORTANT: fix it"))
        self.assertEqual(fi, {"path": "x"})
        self.assertEqual((s["mode"], s["fork_parent_id"]), ("fork", "PARENT9"))

    def test_fork_nudge_needs_fork_enabled_and_parent(self):
        CONFIG["session_nudge_mode"] = "fork"
        plan = self._plan()
        plan.out["parent_id"] = "P"
        self.assertEqual(plan.retry_call("p", None, "n")[2]["mode"], "continue")


# ============================================================= bridge/e2e ===
class _FakeTab:
    """Gia extension: nhan job tu bridge._pending va tra loi ngay."""

    def __init__(self, nonce="N1"):
        self.nonce = nonce
        self.chat_id = "CHAT-1"
        self.jobs = []
        self.replies = []            # moi phan tu: list event (sau event session) hoac callable
        self.counter = 0
        self.report_session = True

    def get(self, timeout=None):
        """/poll goi _pending.get: khong co job -> Empty (poll tra {"job": None})."""
        time.sleep(min(timeout or 0.05, 0.05))
        raise queue.Empty

    def qsize(self):
        return 0

    def push(self, text=None, error=None, session_lost=False, no_session=False):
        self.replies.append((text, error, session_lost, no_session))

    def put(self, job):
        self.jobs.append(job)
        q = bridge._jobs[job["job_id"]]
        text, error, lost, no_session = (self.replies.pop(0) if self.replies
                                         else ("default reply", None, False, False))
        sj = job.get("session") or {}
        if error and lost:
            q.put({"kind": "error", "message": error, "session_lost": True})
            return
        if self.report_session and not no_session:
            self.counter += 1
            q.put({"kind": "session", "mode": sj.get("mode"), "nonce": self.nonce,
                   "chat_id": self.chat_id, "parent_id": f"ASSIST{self.counter - 1}"
                   if sj.get("mode") != "new" else ""})
        if error:
            q.put({"kind": "error", "message": error})
            return
        q.put({"kind": "delta", "text": text})
        q.put({"kind": "done"})


class _HttpSandbox(_Sandbox):
    def setUp(self):
        super().setUp()
        self.tab = _FakeTab()
        bridge._pending = self.tab
        bridge._last_seen = time.time() + 3600
        bridge._remember_tab({"nonce": "N1", "path": "/c/local"})
        self.srv = server.ThreadedServer(("127.0.0.1", 0), server.QwenHandler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, path, payload):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request("POST", path, json.dumps(payload),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        c.close()
        return r.status, raw

    def chat(self, messages, tools=TOOLS, **kw):
        body = {"model": "qwen3.7-plus", "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        body.update(kw)
        status, raw = self.post("/v1/chat/completions", body)
        return status, (json.loads(raw) if status == 200 else raw)


FENCE = '```tool_call\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'


class TestSessionHttpFlow(_HttpSandbox):

    def test_tool_loop_new_then_continue(self):
        self.tab.push(FENCE)
        self.tab.push("The file is fine.")
        msgs = [{"role": "system", "content": "SYS RULES"},
                {"role": "user", "content": "check a.py"}]
        st, r1 = self.chat(msgs)
        self.assertEqual(st, 200)
        m1 = r1["choices"][0]["message"]
        self.assertEqual(m1["tool_calls"][0]["function"]["name"], "read_file")
        j1 = self.tab.jobs[0]
        self.assertEqual(j1["session"]["mode"], "new")
        self.assertTrue(j1["tools_active"])
        self.assertIn("# Tool Use", j1["prompt"])
        self.assertIn("# Session protocol", j1["prompt"])
        self.assertIn("SYS RULES", j1["prompt"])
        sres = sess.STORE.get()
        self.assertEqual((sres.nonce, sres.chat_id, sres.turns), ("N1", "CHAT-1", 1))

        msgs2 = msgs + [{"role": "assistant", "content": m1["content"],
                         "tool_calls": m1["tool_calls"]},
                        tool(m1["tool_calls"][0]["id"], "print('hi')")]
        st, r2 = self.chat(msgs2)
        self.assertEqual(st, 200)
        self.assertEqual(r2["choices"][0]["message"]["content"], "The file is fine.")
        j2 = self.tab.jobs[1]
        self.assertEqual(j2["session"]["mode"], "continue")
        self.assertEqual(j2["session"]["chat_id"], "CHAT-1")
        self.assertEqual(j2["session"]["nonce"], "N1")
        self.assertTrue(j2["tools_active"])
        self.assertNotIn("# Tool Use", j2["prompt"])
        self.assertNotIn("SYS RULES", j2["prompt"])
        self.assertIn("print('hi')", j2["prompt"])
        self.assertIn("[Tool results]", j2["prompt"])
        self.assertEqual(sess.STORE.get().turns, 2)
        self.assertEqual(sess.STORE.get().checkpoints[-1], {"n": 3, "parent": "ASSIST1"})

    def test_new_turn_counts_replay_chars(self):
        """Luot new = replay day du: chars_sent phai gom prompt da gui (khong chi cau tra loi)."""
        self.tab.push(FENCE)
        msgs = [{"role": "system", "content": "S" * 3000},
                {"role": "user", "content": "go"}]
        st, _r = self.chat(msgs)
        self.assertEqual(st, 200)
        sent = len(self.tab.jobs[0]["prompt"])
        self.assertGreater(sent, 3000)
        self.assertGreaterEqual(sess.STORE.get().chars_sent, sent)

    def test_session_mode_off_keeps_legacy_prompt(self):
        CONFIG["bridge_session_mode"] = False
        self.tab.push("plain")
        st, _r = self.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(st, 200)
        job = self.tab.jobs[0]
        self.assertNotIn("session", job)
        self.assertNotIn("tools_active", job)
        self.assertNotIn("Session protocol", job["prompt"])
        self.assertIsNone(sess.STORE.get())

    def test_tab_reload_triggers_full_replay(self):
        self.tab.push(FENCE)
        msgs = [{"role": "user", "content": "go"}]
        _st, r1 = self.chat(msgs)
        m1 = r1["choices"][0]["message"]
        bridge._remember_tab({"nonce": "N-NEW", "path": "/c/local"})
        self.tab.nonce = "N-NEW"
        msgs2 = msgs + [{"role": "assistant", "content": None, "tool_calls": m1["tool_calls"]},
                        tool(m1["tool_calls"][0]["id"], "R")]
        st, _r2 = self.chat(msgs2)
        self.assertEqual(st, 200)
        j2 = self.tab.jobs[1]
        self.assertEqual(j2["session"]["mode"], "new")
        self.assertIn("# Tool Use", j2["prompt"])
        self.assertIn("R", j2["prompt"])
        self.assertEqual(sess.STORE.get().nonce, "N-NEW")

    def test_error_after_send_is_not_retried_and_resets(self):
        CONFIG["retry_attempts"] = 3
        self.tab.push(FENCE)
        msgs = [{"role": "user", "content": "go"}]
        _st, r1 = self.chat(msgs)
        m1 = r1["choices"][0]["message"]
        msgs2 = msgs + [{"role": "assistant", "content": None, "tool_calls": m1["tool_calls"]},
                        tool(m1["tool_calls"][0]["id"], "R")]
        self.tab.push(error="stream died")
        st, _raw = self.chat(msgs2)
        self.assertEqual(st, 503)       # _map_error: loi bridge -> 503
        self.assertEqual(len(self.tab.jobs), 2)          # KHONG retry delta
        self.assertIsNone(sess.STORE.get())
        # client retry -> replay day du trong chat moi
        self.tab.push("recovered")
        st, r3 = self.chat(msgs2)
        self.assertEqual(st, 200)
        self.assertEqual(self.tab.jobs[2]["session"]["mode"], "new")

    def test_error_before_send_is_retried_and_session_kept(self):
        CONFIG["retry_attempts"] = 2
        self.tab.push("ok0")
        msgs = [{"role": "user", "content": "q0"}]
        self.chat(msgs, tools=None)
        msgs2 = msgs + [{"role": "assistant", "content": "ok0"},
                        {"role": "user", "content": "q1"}]
        self.tab.push(error="ui step failed", no_session=True)
        self.tab.push("ok1")
        st, r = self.chat(msgs2, tools=None)
        self.assertEqual(st, 200)
        self.assertEqual(len(self.tab.jobs), 3)
        self.assertEqual([j["session"]["mode"] for j in self.tab.jobs],
                         ["new", "continue", "continue"])
        self.assertIsNotNone(sess.STORE.get())

    def test_session_lost_from_extension_resets_no_retry(self):
        CONFIG["retry_attempts"] = 3
        self.tab.push("a0")
        msgs = [{"role": "user", "content": "q0"}]
        self.chat(msgs, tools=None)
        msgs2 = msgs + [{"role": "assistant", "content": "a0"},
                        {"role": "user", "content": "q1"}]
        self.tab.push(error="session: tab da reload", session_lost=True)
        st, _raw = self.chat(msgs2, tools=None)
        self.assertEqual(st, 503)       # _map_error: loi bridge -> 503
        self.assertEqual(len(self.tab.jobs), 2)
        self.assertIsNone(sess.STORE.get())

    def test_nudge_in_continue_sends_only_nudge_message(self):
        CONFIG["retry_attempts"] = 2
        CONFIG["tool_retry_on_miss"] = True
        CONFIG["tool_retry_attempts"] = 1
        self.tab.push(FENCE)
        msgs = [{"role": "user", "content": "go"}]
        _st, r1 = self.chat(msgs)
        m1 = r1["choices"][0]["message"]
        msgs2 = msgs + [{"role": "assistant", "content": None, "tool_calls": m1["tool_calls"]},
                        tool(m1["tool_calls"][0]["id"], "R")]
        self.tab.push('```tool_call\n{"name": "nope", "arguments": {}}\n```')   # bi drop
        self.tab.push("final text")
        st, r2 = self.chat(msgs2)
        self.assertEqual(st, 200)
        self.assertEqual(r2["choices"][0]["message"]["content"], "final text")
        jobs = self.tab.jobs
        self.assertEqual(len(jobs), 3)
        self.assertIn("[Tool results]", jobs[1]["prompt"])
        self.assertTrue(jobs[2]["prompt"].startswith("[Format error]"))
        self.assertNotIn("[Tool results]", jobs[2]["prompt"])
        self.assertEqual(jobs[2]["session"]["mode"], "continue")
        # reply hong + reply cuoi: chat Qwen da chua ca hai, session van hop le cho luot sau
        self.assertEqual(sess.STORE.get().turns, 2)

    def test_large_tool_result_attaches_file_with_inventory_prompt(self):
        CONFIG["session_file_threshold"] = 500
        self.tab.push(FENCE)
        msgs = [{"role": "user", "content": "go"}]
        _st, r1 = self.chat(msgs)
        m1 = r1["choices"][0]["message"]
        msgs2 = msgs + [{"role": "assistant", "content": None, "tool_calls": m1["tool_calls"]},
                        tool(m1["tool_calls"][0]["id"], "L" * 3000)]
        self.tab.push("ack")
        seen = {}
        orig_put = self.tab.put

        def spy(job):
            fi = job.get("file_info")
            if fi:
                with open(fi["path"], encoding="utf-8") as f:
                    seen["file"] = f.read()
            orig_put(job)
        self.tab.put = spy
        st, _r = self.chat(msgs2)
        self.assertEqual(st, 200)
        j = self.tab.jobs[1]
        self.assertTrue(j["file_info"]["filename"].startswith("qwen_prompt_"))
        self.assertIn("L" * 3000, seen["file"])
        self.assertIn("The attached file holds the output of your previous tool_call(s)",
                      j["attachment_prompt"])
        self.assertEqual(j["session"]["mode"], "continue")

    def test_streaming_no_tools_cline_style(self):
        self.tab.push("<read_file><path>a</path></read_file>")
        self.tab.push("all good")
        msgs = [{"role": "system", "content": "XML TOOLS PROMPT"},
                {"role": "user", "content": "task"}]
        st, raw = self.post("/v1/chat/completions",
                            {"model": "qwen3.7-plus", "messages": msgs, "stream": True})
        self.assertEqual(st, 200)
        self.assertIn("read_file", raw)
        self.assertIn("# Session protocol", self.tab.jobs[0]["prompt"])
        self.assertNotIn("tool_call", sess.STORE.preamble(False))
        self.assertFalse(self.tab.jobs[0]["tools_active"])
        msgs2 = msgs + [{"role": "assistant", "content": "<read_file><path>a</path></read_file>"},
                        {"role": "user", "content": "[read_file result]\nCONTENT"}]
        st, raw = self.post("/v1/chat/completions",
                            {"model": "qwen3.7-plus", "messages": msgs2, "stream": True})
        self.assertEqual(st, 200)
        self.assertIn("all good", raw)
        j2 = self.tab.jobs[1]
        self.assertEqual(j2["session"]["mode"], "continue")
        self.assertEqual(j2["prompt"], "[read_file result]\nCONTENT")
        self.assertEqual(sess.STORE.get().turns, 2)

    def test_volatile_env_details_do_not_break_history(self):
        sysm = {"role": "system", "content": "S"}
        m1 = [sysm, {"role": "user", "content": "task\n<environment_details>t=1</environment_details>"}]
        self.tab.push("<read_file><path>a</path></read_file>")
        self.chat(m1, tools=None)
        m2 = [sysm, {"role": "user", "content": "task\n<environment_details>t=2</environment_details>"},
              {"role": "assistant", "content": "<read_file><path>a</path></read_file>"},
              {"role": "user", "content": "[read_file] ok\n<environment_details>t=3</environment_details>"}]
        self.tab.push("fine")
        self.chat(m2, tools=None)
        self.assertEqual([j["session"]["mode"] for j in self.tab.jobs], ["new", "continue"])

    def test_native_tool_call_survives_in_continue_turn(self):
        """Delta khong co "# Tool Use": native tool_call van phai duoc nhan (tools_active)."""
        self.tab.push(FENCE)
        msgs = [{"role": "user", "content": "go"}]
        _st, r1 = self.chat(msgs)
        m1 = r1["choices"][0]["message"]
        msgs2 = msgs + [{"role": "assistant", "content": None, "tool_calls": m1["tool_calls"]},
                        tool(m1["tool_calls"][0]["id"], "R")]

        def native(job):
            self.tab.jobs.append(job)
            q = bridge._jobs[job["job_id"]]
            q.put({"kind": "session", "mode": "continue", "nonce": "N1", "chat_id": "CHAT-1",
                   "parent_id": "A0"})
            q.put({"kind": "tool_call", "name": "read_file", "id": "n1",
                   "arguments": {"path": "b.py"}})
            q.put({"kind": "done"})
        self.tab.put = native
        st, r2 = self.chat(msgs2)
        self.assertEqual(st, 200)
        self.assertNotIn("# Tool Use", self.tab.jobs[1]["prompt"])
        calls = r2["choices"][0]["message"]["tool_calls"]
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "b.py"})

    def test_fork_retry_through_server(self):
        CONFIG["session_fork"] = True
        self.tab.push(FENCE)
        msgs = [{"role": "user", "content": "go"}]
        _st, r1 = self.chat(msgs)
        m1 = r1["choices"][0]["message"]
        msgs2 = msgs + [{"role": "assistant", "content": None, "tool_calls": m1["tool_calls"]},
                        tool(m1["tool_calls"][0]["id"], "R")]
        self.tab.push("first answer")
        self.chat(msgs2)
        self.tab.push("second answer")
        st, r3 = self.chat(msgs2)             # client retry: cung history, khong co tin moi
        self.assertEqual(st, 200)
        self.assertEqual(r3["choices"][0]["message"]["content"], "second answer")
        j = self.tab.jobs[2]
        self.assertEqual(j["session"]["mode"], "fork")
        self.assertEqual(j["session"]["fork_parent_id"], "ASSIST1")   # parent cua delta o luot 2
        self.assertEqual(j["session"]["user_action"], "edit")
        self.assertIn("[Tool results]", j["prompt"])

    def test_foreign_request_between_turns_resets(self):
        self.tab.push("a0")
        msgs = [{"role": "user", "content": "q0"}]
        self.chat(msgs, tools=None)
        self.tab.push("title")
        self.chat([{"role": "user", "content": "Generate a title"}], tools=None)
        self.tab.push("a1")
        msgs2 = msgs + [{"role": "assistant", "content": "a0"},
                        {"role": "user", "content": "q1"}]
        self.chat(msgs2, tools=None)
        self.assertEqual([j["session"]["mode"] for j in self.tab.jobs], ["new", "new", "new"])
        self.assertIn("q0", self.tab.jobs[2]["prompt"])

    def test_extension_without_nonce_never_creates_session(self):
        self.tab.report_session = False
        self.tab.push("a0")
        self.chat([{"role": "user", "content": "q0"}], tools=None)
        self.assertIsNone(sess.STORE.get())

    def test_concurrent_requests_are_serialized(self):
        order = []
        orig_put = self.tab.put

        def slow(job):
            order.append(("start", job["prompt"][-4:]))
            time.sleep(0.15)
            orig_put(job)
            order.append(("end", job["prompt"][-4:]))
        self.tab.put = slow
        ths = [threading.Thread(target=self.chat,
                                args=([{"role": "user", "content": "req" + c}], None))
               for c in "AB"]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        kinds = [k for k, _ in order]
        self.assertEqual(kinds, ["start", "end", "start", "end"])

    def test_responses_api_session(self):
        self.tab.push(FENCE)
        self.tab.push("done here")
        body = {"model": "qwen3.7-plus", "input": "check", "instructions": "SYSI",
                "tools": [{"type": "function", "name": "read_file", "description": "r",
                           "parameters": TOOLS[0]["function"]["parameters"]}]}
        st, raw = self.post("/v1/responses", body)
        self.assertEqual(st, 200)
        out = json.loads(raw)["output"]
        fc = [o for o in out if o["type"] == "function_call"][0]
        body2 = dict(body, input=[
            {"role": "user", "content": "check"},
            {"type": "function_call", "call_id": fc["call_id"], "name": "read_file",
             "arguments": fc["arguments"]},
            {"type": "function_call_output", "call_id": fc["call_id"], "output": "R!"}])
        st, raw = self.post("/v1/responses", body2)
        self.assertEqual(st, 200)
        self.assertEqual(self.tab.jobs[1]["session"]["mode"], "continue")
        self.assertIn("R!", self.tab.jobs[1]["prompt"])


class TestFakeTabIsolation(_HttpSandbox):
    def test_bridge_tab_state_ttl_and_cleaning(self):
        bridge._remember_tab({"nonce": "X", "path": "/c/abc", "chat_id": "abc",
                              "busy": True, "page_used": "yes", "evil": "z"})
        t = bridge.tab_state()
        self.assertEqual(t, {"nonce": "X", "path": "/c/abc", "chat_id": "abc", "busy": True})
        CONFIG["bridge_worker_ttl"] = 0.05
        time.sleep(0.1)
        self.assertIsNone(bridge.tab_state())

    def test_poll_post_records_tab(self):
        bridge._tab, bridge._tab_ts = {}, 0.0
        srv = bridge.start_server.__globals__["_Server"](("127.0.0.1", 0), bridge._Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            CONFIG["bridge_poll_wait"] = 0.1
            c.request("POST", "/poll", json.dumps({"worker": "w", "tab": {"nonce": "Q9"}}),
                      {"Content-Type": "application/json",
                       "Origin": "chrome-extension://abc"})
            c.getresponse().read()
            self.assertEqual(bridge.tab_state()["nonce"], "Q9")
        finally:
            srv.shutdown()
            srv.server_close()


class TestInterruptRecovery(_Sandbox):
    """Client huy giua chung (interrupt) roi go tiep: tiep tuc trong chat, khong reload/replay."""
    Q0 = [{"role": "user", "content": "q0"}]

    def _interrupted_session(self, tools=None, **plan_out):
        _commit_new(sess.STORE, self.Q0, "a0", None, tools=tools)
        msgs = self.Q0 + [{"role": "assistant", "content": "a0"},
                          {"role": "user", "content": "python co ok hon node khong"}]
        plan = sess.STORE.plan(msgs, tools, "auto", tab=TAB)
        self.assertEqual(plan.kind, "continue")
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "A0", "sent": True})
        plan.out.update(plan_out)
        sess.STORE.fail(plan, "client disconnected; job cancelled")
        return msgs

    def test_interrupt_keeps_session_marks_interrupted(self):
        self._interrupted_session()
        s = sess.STORE.get()
        self.assertIsNotNone(s)
        self.assertEqual(s.hashes[-1], sess.INTERRUPTED)
        self.assertEqual(len(s.hashes), 4)
        self.assertEqual(s.labels[-1], "assistant(interrupted)")
        self.assertEqual(s.checkpoints[-1], {"n": 2, "parent": "A0"})

    def test_two_consecutive_user_messages_continue_with_second_only(self):
        msgs = self._interrupted_session()
        nxt = msgs + [{"role": "user", "content": "tiep di ban"}]
        plan = sess.STORE.plan(nxt, None, "auto", tab=TAB)
        self.assertEqual((plan.kind, plan.reason), ("continue", "after_interrupt"))
        self.assertEqual(plan.base_n, 3)
        self.assertEqual(plan.prompt, "tiep di ban")

    def test_client_keeping_partial_assistant_text_is_skipped_over(self):
        msgs = self._interrupted_session()
        nxt = msgs + [{"role": "assistant", "content": "Node thi..."},
                      {"role": "user", "content": "tiep di ban"}]
        plan = sess.STORE.plan(nxt, None, "auto", tab=TAB)
        self.assertEqual((plan.kind, plan.base_n), ("continue", 4))
        self.assertEqual(plan.prompt, "tiep di ban")

    def test_commit_after_interrupt_then_normal_continue(self):
        msgs = self._interrupted_session()
        nxt = msgs + [{"role": "user", "content": "tiep di ban"}]
        plan = sess.STORE.plan(nxt, None, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "A1", "sent": True})
        s = sess.STORE.commit(plan, "Python thi...", None)
        self.assertEqual(len(s.hashes), 5)
        self.assertNotIn(sess.INTERRUPTED, s.hashes)
        after = nxt + [{"role": "assistant", "content": "Python thi..."},
                       {"role": "user", "content": "cam on"}]
        p2 = sess.STORE.plan(after, None, "auto", tab=TAB)
        self.assertEqual((p2.kind, p2.prompt), ("continue", "cam on"))

    def test_nothing_new_or_diverged_after_interrupt_resets(self):
        msgs = self._interrupted_session()
        p = sess.STORE.plan(msgs, None, "auto", tab=TAB)        # client gui lai y het: retry
        self.assertEqual(p.kind, "new")
        self.assertIn("after_interrupt", p.reason)
        self.assertIn("client[", p.reason)
        self._interrupted_session()
        edited = [{"role": "user", "content": "EDITED"}, {"role": "assistant", "content": "a0"},
                  {"role": "user", "content": "x"}, {"role": "user", "content": "y"}]
        self.assertEqual(sess.STORE.plan(edited, None, "auto", tab=TAB).kind, "new")

    def test_assistant_with_tool_calls_after_interrupt_resets(self):
        msgs = self._interrupted_session()
        nxt = msgs + [asst_calls("c9"), {"role": "user", "content": "y"}]
        self.assertEqual(sess.STORE.plan(nxt, None, "auto", tab=TAB).kind, "new")

    def test_other_failures_still_reset(self):
        _commit_new(sess.STORE, self.Q0, "a0", None, tools=None)
        msgs = self.Q0 + [{"role": "assistant", "content": "a0"},
                          {"role": "user", "content": "q1"}]
        plan = sess.STORE.plan(msgs, None, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "sent": True})
        sess.STORE.fail(plan, "stream died")
        self.assertIsNone(sess.STORE.get())

    def test_interrupt_before_send_keeps_session_untouched(self):
        _commit_new(sess.STORE, self.Q0, "a0", None, tools=None)
        msgs = self.Q0 + [{"role": "assistant", "content": "a0"},
                          {"role": "user", "content": "q1"}]
        plan = sess.STORE.plan(msgs, None, "auto", tab=TAB)
        sess.STORE.fail(plan, "client disconnected")            # chua gui gi
        self.assertNotEqual(sess.STORE.get().hashes[-1], sess.INTERRUPTED)

    def test_interrupt_with_tab_reloaded_resets(self):
        self._interrupted_session()                              # session hop le
        _commit = sess.STORE.get()
        sess.STORE.reset("x")
        _commit_new(sess.STORE, self.Q0, "a0", None, tools=None)
        msgs = self.Q0 + [{"role": "assistant", "content": "a0"},
                          {"role": "user", "content": "q1"}]
        plan = sess.STORE.plan(msgs, None, "auto", tab=TAB)
        plan.out.update({"nonce": "OTHER", "chat_id": "C1", "sent": True})
        sess.STORE.fail(plan, "client disconnected")
        self.assertIsNone(sess.STORE.get())

    def test_interrupt_of_first_turn_keeps_new_session(self):
        plan = sess.STORE.plan(self.Q0, None, "auto", tab=None)
        plan.delta_chars = 500
        plan.out.update({"nonce": "N1", "chat_id": "C1", "sent": True})
        sess.STORE.fail(plan, "client disconnected; job cancelled")
        s = sess.STORE.get()
        self.assertEqual((s.hashes[-1], len(s.hashes)), (sess.INTERRUPTED, 2))
        nxt = self.Q0 + [{"role": "user", "content": "tiep"}]
        p = sess.STORE.plan(nxt, None, "auto", tab=TAB)
        self.assertEqual((p.kind, p.base_n, p.prompt), ("continue", 1, "tiep"))

    def test_divergence_reason_names_the_differing_messages(self):
        m = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        _commit_new(sess.STORE, m, "", [call("c1")])
        hist = m + [asst_calls("c1")]
        plan = sess.STORE.plan(hist + [tool("c1", "R")], TOOLS, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "sent": True})
        sess.STORE.commit(plan, "final", None)
        new = hist + [{"role": "user", "content": "dung python co loi hon node khong"}]
        p = sess.STORE.plan(new, TOOLS, "auto", tab=TAB)
        self.assertEqual(p.kind, "new")
        self.assertIn("client[3]=user(", p.reason)
        self.assertIn("'dung python co loi hon node kh'", p.reason)
        self.assertIn("session[3]=tool(1c)", p.reason)
        self.assertNotIn("fork:", p.reason)

    def test_fork_miss_lists_checkpoints(self):
        CONFIG["session_fork"] = True
        m = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        _commit_new(sess.STORE, m, "", [call("c1")])
        hist = m + [asst_calls("c1")]
        plan = sess.STORE.plan(hist + [tool("c1", "R")], TOOLS, "auto", tab=TAB)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "A1", "sent": True})
        sess.STORE.commit(plan, "final", None)
        edited = [{"role": "system", "content": "sys"}, {"role": "user", "content": "EDITED"},
                  asst_calls("c1"), tool("c1", "R")]
        p = sess.STORE.plan(edited, TOOLS, "auto", tab=TAB)
        self.assertEqual(p.kind, "new")
        self.assertIn("ckpts=[0, 3]", p.reason)


class TestInterruptHttp(_HttpSandbox):
    def setUp(self):
        super().setUp()
        self._ping = server.SSE_PING_INTERVAL
        server.SSE_PING_INTERVAL = 0.2          # heartbeat phat hien client ngat nhanh hon

    def tearDown(self):
        server.SSE_PING_INTERVAL = self._ping
        super().tearDown()

    def test_reason_strings_from_bridge_and_server_are_recognized(self):
        for r in ("browser bridge: client da ngat ket noi, job bi huy",
                  "client disconnected; job cancelled", "Client Disconnected"):
            self.assertTrue(sess._client_cancelled(r), r)
        for r in ("browser bridge: tab khong tra ket qua (timeout)", "job cancelled: extension cancel",
                  "UI send timeout", ""):
            self.assertFalse(sess._client_cancelled(r), r)

    def test_stream_disconnect_then_follow_up_continues_in_same_chat(self):
        self._interrupt_flow(40)           # server dang ghi delta -> loi ghi socket

    def test_idle_stream_disconnect_detected_by_heartbeat_continues_too(self):
        self._interrupt_flow(2)            # im lang sau part1 -> chi heartbeat phat hien

    def _interrupt_flow(self, n_deltas):
        self.tab.push("a0")
        q0 = [{"role": "user", "content": "q0"}]
        self.chat(q0, tools=None)
        a = {"role": "user", "content": "python co ok hon node khong"}
        orig_put = self.tab.put

        def slow_put(job):
            if len(self.tab.jobs) != 1:
                return orig_put(job)
            self.tab.jobs.append(job)
            q = bridge._jobs[job["job_id"]]
            q.put({"kind": "session", "mode": "continue", "nonce": "N1",
                   "chat_id": "CHAT-1", "parent_id": "ASSIST0"})

            def later():
                for i in range(n_deltas):
                    time.sleep(0.1)
                    q.put({"kind": "delta", "text": f"part{i} "})
            threading.Thread(target=later, daemon=True).start()
        self.tab.put = slow_put

        import socket as _socket
        body = json.dumps({"model": "qwen3.7-plus", "stream": True,
                           "messages": q0 + [{"role": "assistant", "content": "a0"}, a]}
                          ).encode()
        sock = _socket.create_connection(("127.0.0.1", self.port), timeout=30)
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Content-Type: application/json\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)
        buf = b""
        while b"part1" not in buf:
            chunk = sock.recv(4096)
            self.assertTrue(chunk, "server dong ket noi truoc khi stream")
            buf += chunk
        sock.shutdown(_socket.SHUT_RDWR)                  # = nguoi dung bam interrupt
        sock.close()
        deadline = time.time() + 10
        while time.time() < deadline:
            cur = sess.STORE.get()
            if cur is not None and cur.hashes and cur.hashes[-1] == sess.INTERRUPTED:
                break
            time.sleep(0.1)
        cur = sess.STORE.get()
        self.assertIsNotNone(cur, "session bi reset khi client interrupt")
        self.assertEqual(cur.hashes[-1], sess.INTERRUPTED)

        self.tab.put = orig_put
        self.tab.push("Python thi...")
        msgs3 = q0 + [{"role": "assistant", "content": "a0"}, a,
                      {"role": "user", "content": "tiep di ban"}]
        st, r3 = self.chat(msgs3, tools=None)
        self.assertEqual(st, 200)
        j = self.tab.jobs[-1]
        self.assertEqual(j["session"]["mode"], "continue")
        self.assertEqual(j["prompt"], "tiep di ban")
        self.assertEqual(j["session"]["chat_id"], "CHAT-1")
        self.assertEqual(sess.STORE.get().turns, 3)


class TestNewPlanAttachPrompt(_Sandbox):
    """F1: luot new (replay day du) phai mang attach_prompt rieng. Extension tu chuyen
    inline -> file thi composer dung cau nay, KHONG dung attachment_prompt cua config."""

    M = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "Thanks"}]

    def test_new_plan_carries_full_replay_attach_prompt(self):
        p = sess.STORE.plan(self.M, TOOLS, "auto", tab=None)
        self.assertEqual(p.kind, "new")
        ap = p.job_session()["attach_prompt"]
        self.assertIn("FULL conversation", ap)
        self.assertIn("LAST input", ap)
        self.assertNotIn("compressed", ap.lower())
        self.assertNotIn("my next message", ap)   # khong phai delta trong chat da co history

    def test_new_plan_without_tools_still_gets_prompt(self):
        p = sess.STORE.plan([{"role": "user", "content": "hi"}], None, "auto", tab=None)
        self.assertIn("FULL conversation", p.job_session()["attach_prompt"])

    def test_override_from_config(self):
        CONFIG["session_prompts"] = {"attach_full": "DOC FILE: {nothing}"}
        p = sess.STORE.plan(self.M, TOOLS, "auto", tab=None)
        self.assertEqual(p.job_session()["attach_prompt"], "DOC FILE: {nothing}")

    def test_plan_attach_prompt_wins(self):
        p = sess.STORE.plan(self.M, TOOLS, "auto", tab=None)
        p.attach_prompt = "RIENG"
        self.assertEqual(p.job_session()["attach_prompt"], "RIENG")

    def test_continue_delta_prompt_unchanged(self):
        _commit_new(sess.STORE, self.M, "ok", None, tools=None)
        msgs = self.M + [{"role": "assistant", "content": "ok"},
                         {"role": "user", "content": "next"}]
        p = sess.STORE.plan(msgs, None, "auto", tab=TAB)
        self.assertEqual(p.kind, "continue")
        ap = p.job_session()["attach_prompt"]
        self.assertIn("my next message", ap)
        self.assertNotIn("FULL conversation", ap)


class TestNewPlanAttachPromptHttp(_HttpSandbox):
    def test_new_job_payload_has_attach_prompt(self):
        self.tab.push("ok")
        st, _r = self.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(st, 200)
        sj = self.tab.jobs[0]["session"]
        self.assertEqual(sj["mode"], "new")
        self.assertIn("FULL conversation", sj["attach_prompt"])


class TestTaskAnchor(_Sandbox):
    """Luot continue chi co tool result mang [Current task] (tin user that cuoi cung) de Qwen
    khong tra loi chu de cu trong file lich su dinh kem o luot new."""

    M = [{"role": "system", "content": "sys"},
         {"role": "user", "content": "old topic about cats"},
         {"role": "assistant", "content": "meow"},
         {"role": "user", "content": "now fix   the\nparser bug"}]

    def _plan(self, result="FILE BODY", msgs=None):
        base = msgs or self.M
        _commit_new(sess.STORE, base, "", [call("c1")])
        return sess.STORE.plan(base + [asst_calls("c1"), tool("c1", result)],
                               TOOLS, "auto", tab=TAB)

    def test_inline_has_anchor_with_latest_user_request(self):
        p = self._plan()
        self.assertEqual(p.kind, "continue")
        self.assertIn('[Current task]', p.prompt)
        self.assertIn('"now fix the parser bug"', p.prompt)
        self.assertNotIn("old topic about cats", p.prompt)
        self.assertTrue(p.prompt.rstrip().endswith("earlier topics in any attached history."))

    def test_file_mode_anchor_in_composer_not_in_file(self):
        CONFIG["session_file_threshold"] = 50
        p = self._plan("X" * 200)
        self.assertIsNotNone(p.file_text)
        self.assertIn('"now fix the parser bug"', p.prompt)
        self.assertNotIn("[Current task]", p.file_text)

    def test_attach_prompt_has_anchor(self):
        p = self._plan()
        self.assertIn('"now fix the parser bug"', p.attach_prompt)

    def test_no_anchor_when_delta_has_user_message(self):
        _commit_new(sess.STORE, self.M, "", [call("c1")])
        msgs = self.M + [asst_calls("c1"), tool("c1", "R"),
                         {"role": "user", "content": "also this"}]
        p = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        self.assertEqual(p.kind, "continue")
        self.assertNotIn("[Current task]", p.prompt)
        self.assertNotIn("[Current task]", p.attach_prompt)

    def test_disabled_by_config(self):
        CONFIG["session_task_anchor"] = False
        p = self._plan()
        self.assertNotIn("[Current task]", p.prompt)
        CONFIG["session_task_anchor"] = True
        CONFIG["session_task_anchor_chars"] = 0
        sess.STORE.reset("again")
        self.assertNotIn("[Current task]", self._plan().prompt)

    def test_long_request_is_truncated(self):
        CONFIG["session_task_anchor_chars"] = 20
        msgs = self.M[:3] + [{"role": "user", "content": "A" * 100}]
        p = self._plan(msgs=msgs)
        self.assertIn('"' + "A" * 20 + ' ..."', p.prompt)
        self.assertNotIn("A" * 21, p.prompt)

    def test_hash_ignore_patterns_stripped_from_anchor(self):
        msgs = self.M[:3] + [{"role": "user", "content":
                              "<task>do X</task>\n<environment_details>NOISE</environment_details>"}]
        p = self._plan(msgs=msgs)
        self.assertIn("do X", p.prompt)
        self.assertNotIn("NOISE", p.prompt)

    def test_prompt_override_without_task_has_no_anchor(self):
        CONFIG["session_prompts"] = {"tool_results": "RES: {results}{user_part}"}
        p = self._plan()
        self.assertNotIn("[Current task]", p.prompt)
        self.assertIn("RES:", p.prompt)

    def test_task_anchor_prompt_override(self):
        CONFIG["session_prompts"] = {"task_anchor": "TASK={text}"}
        p = self._plan()
        self.assertIn("TASK=now fix the parser bug", p.prompt)

    def test_no_user_message_means_no_anchor(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "   "}]
        p = self._plan(msgs=msgs)
        self.assertNotIn("[Current task]", p.prompt)

    def test_anchor_does_not_break_hash_chain(self):
        p = self._plan()
        self.assertEqual(p.kind, "continue")
        p.delta_chars = 10
        p.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "", "sent": True})
        self.assertIsNotNone(sess.STORE.commit(p, "", [call("c2")]))
        msgs = self.M + [asst_calls("c1"), tool("c1", "FILE BODY"), asst_calls("c2"),
                         tool("c2", "R2")]
        p2 = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        self.assertEqual(p2.kind, "continue")
        self.assertIn('"now fix the parser bug"', p2.prompt)


class TestReplayCompactBreaksResetLoop(_Sandbox):
    """Vong lap new -> max_chars -> new: replay day du >= session_max_chars lam session vua tao het
    han ngay luot sau. Thu gon replay dua chars_sent ve duoi tran; hash chuoi khong doi."""

    def setUp(self):
        super().setUp()
        CONFIG["session_max_chars"] = 100000
        CONFIG["session_replay_compact"] = True
        CONFIG["session_replay_target_chars"] = 0
        CONFIG["inline_prompt_max_chars"] = 30000
        CONFIG["inline_prompt_use_learned"] = False

    def _history(self):
        msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "task"}]
        for i in range(12):
            cid = f"c{i}"
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                call(cid, "read_file", json.dumps({"path": f"f{i}.py", "pad": "P" * 20000}))]})
            msgs.append(tool(cid, f"Read file f{i}.py, lines 1-99\n" + "code\n" * 3000))
        msgs.append({"role": "user", "content": "now continue"})
        return msgs

    def _new_turn(self, msgs):
        plan = sess.STORE.plan(msgs, TOOLS, "auto", tab=TAB)
        fi, prompt, _m = server._compose_prompt(plan, msgs, TOOLS, "auto", "")
        server._remove_prompt_file(fi)
        plan.out.update({"nonce": "N1", "chat_id": "C1", "parent_id": "", "sent": True})
        s = sess.STORE.commit(plan, "reply", None)
        return plan, s

    def _next(self, msgs):
        nxt = msgs + [{"role": "assistant", "content": "reply"},
                      {"role": "user", "content": "next step"}]
        return sess.STORE.plan(nxt, TOOLS, "auto", tab=TAB)

    def test_without_compaction_session_dies_next_turn(self):
        CONFIG["session_replay_compact"] = False
        msgs = self._history()
        plan, s = self._new_turn(msgs)
        self.assertGreaterEqual(s.chars_sent, 100000)
        p2 = self._next(msgs)
        self.assertEqual(p2.kind, "new")
        self.assertEqual(p2.reason, "max_chars")

    def test_with_compaction_next_turns_continue_and_hashes_unchanged(self):
        msgs = self._history()
        plan, s = self._new_turn(msgs)
        self.assertLess(s.chars_sent, 100000)
        # hash session = hash cua messages GOC, khong phai ban da thu gon
        self.assertEqual(plan.hashes, sess.hash_messages(msgs))
        from qwen_web2api import replay
        compacted, st = replay.compact_messages(msgs, TOOLS, "auto")
        self.assertIsNotNone(st)
        self.assertNotEqual(sess.hash_messages(compacted), sess.hash_messages(msgs))
        p2 = self._next(msgs)
        self.assertEqual(p2.kind, "continue")
        self.assertEqual(p2.reason, "history_match")
        self.assertEqual(p2.base_n, len(msgs) + 1)


class TestRequestShape(unittest.TestCase):
    def test_shape_run_length_and_system_hash(self):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"},
                asst_calls("1"), tool("1", "r"), asst_calls("2"), tool("2", "r"), tool("2", "r"),
                {"role": "assistant", "content": "done"}, {"role": "user", "content": "n"}]
        s = server._request_shape(msgs, TOOLS, "auto")
        self.assertIn("msgs=9 tools=1 choice=auto sys=", s)
        self.assertIn("seq=s u A t A tx2 a u", s)
        # system doi (vd agent compaction) -> hash doi; khong tools -> tools=0
        other = [dict(msgs[0], content="S2")] + msgs[1:]
        s2 = server._request_shape(other, None, "auto")
        self.assertIn("tools=0", s2)
        self.assertNotEqual(s.split("sys=")[1][:8], s2.split("sys=")[1][:8])

    def test_shape_never_raises_on_odd_messages(self):
        self.assertIn("msgs=0", server._request_shape([], None, None))
        self.assertIn("seq=? s", server._request_shape(["junk", {"role": "developer", "content": [{"text": "x"}]}], None, None))


class TestReplayAdaptiveBudget(_Sandbox):
    def _history(self):
        msgs = [{"role": "system", "content": "S" * 5000}, {"role": "user", "content": "task"}]
        for i in range(40):
            msgs.append(asst_calls("c%d" % i))
            msgs.append(tool("c%d" % i, "Read file f%d.py\n" % i + "x" * 9000))
        msgs.append({"role": "user", "content": "next"})
        return msgs

    def test_adaptive_keeps_more_than_fixed_budget_but_fits_target(self):
        from qwen_web2api import replay
        from qwen_web2api.tools import messages_to_prompt
        msgs = self._history()
        CONFIG["session_max_chars"] = 400000          # target 200k
        out_a, st_a = replay.compact_messages(msgs, TOOLS, "auto")
        CONFIG["session_replay_adaptive"] = False
        out_f, st_f = replay.compact_messages(msgs, TOOLS, "auto")
        self.assertIsNotNone(st_a)
        self.assertIsNotNone(st_f)
        self.assertLessEqual(st_a["after"], st_a["target"])
        self.assertGreater(st_a["after"], st_f["after"])     # giu nhieu hon muc co dinh
        self.assertLess(st_a["tool_stubbed"], st_f["tool_stubbed"])
        self.assertEqual(messages_to_prompt(msgs, TOOLS, "auto").count("x" * 9000), 40)

    def test_target_ratio_config(self):
        from qwen_web2api import replay
        CONFIG["session_max_chars"] = 400000
        CONFIG["session_replay_target_chars"] = 0
        CONFIG["session_replay_target_ratio"] = 0.75
        self.assertEqual(replay.target_chars(), 300000)
        CONFIG["session_replay_target_ratio"] = 9          # ngoai khoang -> 0.5
        self.assertEqual(replay.target_chars(), 200000)


class TestReplayStubHead(unittest.TestCase):
    def test_stub_skips_git_crlf_warnings(self):
        from qwen_web2api import replay
        t = ("warning: in the working copy of 'a.js', LF will be replaced by CRLF\n"
             "warning: in the working copy of 'b.js', LF will be replaced by CRLF\n"
             "diff --git a/a.js b/a.js\n" + "+x\n" * 500)
        self.assertTrue(replay._stub(t).startswith("diff --git a/a.js b/a.js\n[... "))
        only = "warning: one\nwarning: two\n" * 300
        self.assertTrue(replay._stub(only).startswith("warning: one\n[... "))


if __name__ == "__main__":
    unittest.main()
    