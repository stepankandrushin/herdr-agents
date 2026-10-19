"""Tests of herdr-agents.py's readers and formatting, on made-up session logs. They never
touch herdr."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("herdr_agents", os.path.join(HERE, "..", "herdr-agents.py"))
herdr_agents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(herdr_agents)


class Logs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def log(self, *events, name="log.jsonl"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        return path


def bash_call(id, command):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": id, "name": "Bash", "input": {"command": command}}]}}


def bash_result(id, text, error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": id, "content": text, "is_error": error}]}}


def usage(**n):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "usage": n}}


class Format(unittest.TestCase):
    def test_age(self):
        now = 1_000_000
        self.assertEqual(herdr_agents.label(now - 30, now), "now")
        self.assertEqual(herdr_agents.label(now - 7 * 60, now), "7m")
        self.assertEqual(herdr_agents.label(now - 5 * 3600 - 59 * 60, now), "5h")
        self.assertEqual(herdr_agents.label(now - 3 * 86400, now), "3d")

    def test_thousands(self):
        self.assertIsNone(herdr_agents.kilo(None))
        self.assertIsNone(herdr_agents.kilo(0))
        self.assertEqual(herdr_agents.kilo(5100), "5k")
        self.assertEqual(herdr_agents.kilo(5500), "6k")
        self.assertEqual(herdr_agents.kilo(120_300), "120k")

    def test_command_and_output(self):
        t = herdr_agents.command_tokens(("cd /src/api && pytest -q\nmore", "..\n2 passed in 0.1s\n", False, None))
        self.assertEqual(t, {"last_cmd": "$ pytest -q", "last_out": "2 passed in 0.1s"})

    def test_running(self):
        self.assertEqual(herdr_agents.command_tokens(("make", None, False, None))["last_out"], "…")

    def test_failed(self):
        t = herdr_agents.command_tokens(("false", "Exit code 1", True, None))
        self.assertEqual(t["last_out"], "✗ exit 1")
        t = herdr_agents.command_tokens(("pytest", "FAILED test_x.py::test_a\nExit code 1", True, None))
        self.assertEqual(t["last_out"], "✗ FAILED test_x.py::test_a")

    def test_no_output(self):
        self.assertEqual(herdr_agents.command_tokens(("touch x", "(no output)", False, None))["last_out"], "✓")

    def test_agent_lines_and_colours_skipped(self):
        out = "\x1b[32mbuilt\x1b[0m\n}\nShell cwd was reset to /src"
        self.assertEqual(herdr_agents.command_tokens(("make", out, False, None))["last_out"], "built")

    def test_no_command(self):
        self.assertEqual(herdr_agents.command_tokens(None), {"last_cmd": None, "last_out": None})

    def test_place(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            self.assertEqual(herdr_agents.place({"cwd": "/home/u"}), "~")
            self.assertEqual(herdr_agents.place({"cwd": "/home/u/src/api/", "foreground_cwd": "/srv/web"}), "web")
            self.assertIsNone(herdr_agents.place({}))


class Claude(Logs):
    def test_last_command(self):
        path = self.log(bash_call("a", "ls"), bash_result("a", "x"),
                        bash_call("b", "git status --short"), bash_result("b", " M app.py"))
        self.assertEqual(herdr_agents.claude_command(path)[:3], ("git status --short", " M app.py", False))

    def test_command_still_running(self):
        path = self.log(bash_call("a", "sleep 60"))
        self.assertEqual(herdr_agents.claude_command(path)[:3], ("sleep 60", None, False))

    def test_background_command_follows_its_output_file(self):
        out = os.path.join(self.tmp.name, "task.output")
        with open(out, "w") as f:
            f.write("step 1\nstep 2\n")
        path = self.log(bash_call("a", "./build.sh"), bash_result(
            "a", "Command running in background with ID: b1. Output is being written to: %s" % out))
        cmd, text, failed, follow = herdr_agents.claude_command(path)
        self.assertEqual((text, follow), ("step 1\nstep 2\n", out))

    def test_subagent_commands_ignored(self):
        side = dict(bash_call("s", "rm -rf build"), isSidechain=True)
        path = self.log(bash_call("a", "ls"), bash_result("a", "x"), side)
        self.assertEqual(herdr_agents.claude_command(path)[0], "ls")

    def test_context(self):
        path = self.log(usage(input_tokens=2, cache_creation_input_tokens=1000,
                              cache_read_input_tokens=40000, output_tokens=500))
        self.assertEqual(herdr_agents.claude_context(path), 41502)

    def test_context_skips_synthetic_message(self):
        path = self.log(usage(input_tokens=10, output_tokens=20), usage(input_tokens=0, output_tokens=0))
        self.assertEqual(herdr_agents.claude_context(path), 30)

    def test_context_after_compaction(self):
        path = self.log(usage(input_tokens=150000, output_tokens=1),
                        {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": 9000}})
        self.assertEqual(herdr_agents.claude_context(path), 9000)

    def test_last_message_time(self):
        path = self.log({"type": "user", "timestamp": "2026-01-02T03:04:05.000Z"}, {"type": "summary"})
        self.assertEqual(herdr_agents.last_message_at(path), 1767323045)


class Codex(Logs):
    def test_activity_uses_completion_and_ignores_restore_metadata(self):
        path = self.log(
            {"type": "response_item", "timestamp": "2026-01-02T03:00:00Z",
             "payload": {"type": "message", "role": "assistant"}},
            {"type": "event_msg", "timestamp": "2026-01-02T03:04:05.000Z",
             "payload": {"type": "task_complete"}},
            {"type": "event_msg", "timestamp": "2026-01-02T05:00:00Z",
             "payload": {"type": "thread_settings_applied"}},
            {"type": "event_msg", "timestamp": "2026-01-02T05:00:01Z",
             "payload": {"type": "token_count", "info": None}})
        self.assertEqual(herdr_agents.last_message_at(path, "codex"), 1767323045)

    def test_activity_includes_tool_calls_and_interruption(self):
        for kind, payload in (("response_item", "custom_tool_call_output"),
                              ("event_msg", "item_completed"),
                              ("event_msg", "turn_aborted")):
            with self.subTest(payload=payload):
                path = self.log({"type": kind, "timestamp": "2026-01-02T03:04:05Z",
                                 "payload": {"type": payload}})
                self.assertEqual(herdr_agents.last_message_at(path, "codex"), 1767323045)

    def test_resume_replaces_stale_pane_age_and_keeps_counting(self):
        at = 1767323045
        path = self.log({"type": "event_msg", "timestamp": "2026-01-02T03:04:05Z",
                         "payload": {"type": "task_complete"}})
        pane = {"agent": "codex", "pane_id": "w1:p1", "agent_status": "idle",
                "tokens": {"last_active": "now", "last_active_at": str(at + 10000)}}
        cache = {}
        with mock.patch.object(herdr_agents, "session_log", return_value=path), \
                mock.patch.object(herdr_agents, "call") as call, \
                mock.patch.object(herdr_agents, "last_message_at",
                                  wraps=herdr_agents.last_message_at) as read:
            herdr_agents.refresh([pane], at + 300, cache=cache)
            report = call.call_args.args[1]
            self.assertEqual(report["agent"], "codex")
            self.assertEqual(report["tokens"], {"last_active": "5m", "last_active_at": str(at)})
            pane["tokens"] = report["tokens"]
            herdr_agents.refresh([pane], at + 360, cache=cache)
            self.assertEqual(call.call_args.args[1]["tokens"]["last_active"], "6m")
            self.assertEqual(read.call_count, 1)
            with open(path, "a") as f:
                f.write(json.dumps({"type": "event_msg", "timestamp": "2026-01-02T03:10:05Z",
                                    "payload": {"type": "task_complete"}}) + "\n")
            herdr_agents.refresh([pane], at + 360, cache=cache)
            self.assertEqual(call.call_args.args[1]["tokens"]["last_active"], "now")
            self.assertEqual(read.call_count, 2)

    def test_working_then_idle_uses_the_completion_time(self):
        at = 1767323045
        path = self.log({"type": "response_item", "timestamp": "2026-01-02T03:04:05Z",
                         "payload": {"type": "message", "role": "user"}})
        pane = {"agent": "codex", "pane_id": "w1:p1", "agent_status": "working"}
        cache = {}
        with mock.patch.object(herdr_agents, "session_log", return_value=path), \
                mock.patch.object(herdr_agents, "call") as call:
            herdr_agents.refresh([pane], at + 300, cache=cache)
            self.assertEqual(call.call_args.args[1]["tokens"]["last_active"], "now")
            pane["tokens"] = call.call_args.args[1]["tokens"]
            pane["agent_status"] = "idle"
            with open(path, "a") as f:
                f.write(json.dumps({"type": "event_msg", "timestamp": "2026-01-02T03:09:15Z",
                                    "payload": {"type": "task_complete"}}) + "\n")
            herdr_agents.refresh([pane], at + 400, cache=cache)
            self.assertEqual(call.call_args.args[1]["tokens"],
                             {"last_active": "1m", "last_active_at": str(at + 310)})

    def test_fresh_session_clears_age_inherited_from_previous_agent(self):
        pane = {"agent": "codex", "pane_id": "w1:p1", "agent_status": "idle",
                "tokens": {"last_active": "3d", "last_active_at": "1000000"}}
        with mock.patch.object(herdr_agents, "session_log", return_value=None), \
                mock.patch.object(herdr_agents, "call") as call:
            herdr_agents.refresh([pane], 2000000)
            self.assertEqual(call.call_args.args[1]["tokens"],
                             {"last_active": None, "last_active_at": None})

    def test_last_command(self):
        path = self.log({"payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "command": ["/bin/bash", "-lc", "cargo test"],
            "exit_code": 101, "aggregated_output": "test result: FAILED"}}})
        self.assertEqual(herdr_agents.codex_command(path), ("cargo test", "test result: FAILED\nExit code 101", True, None))

    def test_context(self):
        path = self.log({"payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 23456}}}},
                        {"payload": {"type": "token_count", "info": None}})
        self.assertEqual(herdr_agents.codex_context(path), 23456)

    def test_context_empty_after_compaction(self):
        path = self.log({"payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 9}}}},
                        {"type": "compacted", "payload": {}})
        self.assertIsNone(herdr_agents.codex_context(path))


class Pi(Logs):
    def test_last_command(self):
        path = self.log(
            {"message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "echo hi"}}]}},
            {"message": {"role": "toolResult", "toolCallId": "t1", "content": [{"type": "text", "text": "hi"}]}})
        self.assertEqual(herdr_agents.pi_command(path), ("echo hi", "hi", False, None))

    def test_context_skips_aborted_reply(self):
        path = self.log({"message": {"role": "assistant", "usage": {"totalTokens": 8000}}},
                        {"message": {"role": "assistant", "stopReason": "aborted", "usage": {"totalTokens": 1}}})
        self.assertEqual(herdr_agents.pi_context(path), 8000)


class Hook(unittest.TestCase):
    def test_silent_outside_herdr(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HERDR_ENV": ""}), redirect_stdout(out):
            herdr_agents.main()
        self.assertEqual(out.getvalue(), "")


class Tabs(Logs):
    def setUp(self):
        super().setUp()
        self.pane = {"agent": "codex", "pane_id": "w1:p1", "tab_id": "w1:t1",
                     "terminal_title": "Fix login | api", "terminal_title_stripped": "Fix login | api"}
        self.tab = {"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}
        self.renames = []
        for name, value in (("SOCK", "/tmp/test.sock"), ("STATE_DIR", self.tmp.name),
                            ("STATE", os.path.join(self.tmp.name, "tabs.json"))):
            patch = mock.patch.object(herdr_agents, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(herdr_agents, "call", side_effect=self.api)
        patch.start()
        self.addCleanup(patch.stop)

    def api(self, method, params):
        if method == "tab.get":
            return {"tab": dict(self.tab)}
        if method == "tab.list":
            return {"tabs": [dict(self.tab)]}
        if method == "tab.rename":
            self.tab["label"] = params["label"]
            self.renames.append(params["label"])
            return {}
        if method == "pane.report_metadata":
            self.assertEqual(params["agent"], "codex")
            if params["pane_id"] == self.pane["pane_id"]:
                self.pane.setdefault("tokens", {}).update(params["tokens"])
            return {}
        self.fail("Unexpected method: " + method)

    def test_idle_working_and_renamed_codex_titles_follow_into_tab(self):
        named = {}
        herdr_agents.tabs([self.pane], named)
        self.assertEqual(self.tab["label"], "🌀 Fix login | api")
        self.pane["terminal_title"] = "⠇ Fix login | api"
        herdr_agents.tabs([self.pane], named)
        self.assertEqual(self.renames, ["🌀 Fix login | api"])
        self.pane.update(terminal_title="New session | api", terminal_title_stripped="New session | api")
        herdr_agents.tabs([self.pane], named)
        self.assertEqual(self.tab["label"], "🌀 New session | api")
        # Restarting the ticker keeps ownership of the name it assigned.
        herdr_agents.tabs([self.pane], {})
        self.assertEqual(self.tab["label"], "🌀 New session | api")

    def test_approval_status_does_not_become_session_name(self):
        named = {}
        for marker in ("!", "."):
            title = "[ %s ] Action Required | Fix login | api" % marker
            self.pane.update(terminal_title=title, terminal_title_stripped=title)
            herdr_agents.tabs([self.pane], named)
            self.assertEqual(self.tab["label"], "🌀 Fix login | api")
            self.assertEqual(self.pane["tokens"]["session_title"], "Fix login | api")
        self.assertEqual(self.renames, ["🌀 Fix login | api"])

    def test_manual_tab_name_is_preserved(self):
        named = {}
        herdr_agents.tabs([self.pane], named)
        self.tab["label"] = "My tab"
        self.pane["terminal_title_stripped"] = "New session | api"
        herdr_agents.tabs([self.pane], named)
        self.assertEqual(self.tab["label"], "My tab")
        herdr_agents.tabs([], named)
        self.assertEqual(self.tab["label"], "My tab")

    def test_multiple_agents_do_not_rename_tab(self):
        other = dict(self.pane, pane_id="w1:p2", agent="claude")
        herdr_agents.tabs([self.pane, other], {})
        self.assertEqual(self.renames, [])

    def test_codex_exit_releases_tab_number(self):
        named = {}
        herdr_agents.tabs([self.pane], named)
        herdr_agents.tabs([], named)
        self.assertEqual(self.tab["label"], "1")
        self.assertEqual(named, {})

    def test_claude_to_codex_keeps_automatic_naming(self):
        named = {}
        claude = dict(self.pane, agent="claude", terminal_title="✳ Fix docs",
                      terminal_title_stripped="Fix docs")
        herdr_agents.tabs([claude], named)
        self.assertEqual(self.tab["label"], "🦀 Fix docs")
        herdr_agents.tabs([self.pane], named)
        self.assertEqual(self.tab["label"], "🌀 Fix login | api")
        # An ordinary shell title still must not rename a departing Claude pane.
        claude.update(terminal_title="bash", terminal_title_stripped="bash")
        herdr_agents.tabs([claude], named)
        self.assertEqual(self.tab["label"], "🌀 Fix login | api")

    def test_pi_title_names_tab(self):
        named = {}
        pi = dict(self.pane, agent="pi", terminal_title="π - api", terminal_title_stripped="π - api")
        herdr_agents.tabs([pi], named)
        self.assertEqual(self.tab["label"], "🥧 api")
        pi.update(terminal_title="π - Fix docs - api", terminal_title_stripped="π - Fix docs - api")
        herdr_agents.tabs([pi], named)
        self.assertEqual(self.tab["label"], "🥧 Fix docs - api")
        # The shell's title as pi quits is not a session name.
        pi.update(terminal_title="bash", terminal_title_stripped="bash")
        herdr_agents.tabs([pi], named)
        self.assertEqual(self.tab["label"], "🥧 Fix docs - api")
        herdr_agents.tabs([], named)
        self.assertEqual(self.tab["label"], "1")

    def test_name_given_before_the_emoji_takes_it(self):
        with herdr_agents.given_names() as given:
            given["/tmp/test.sock w1:t1"] = "Fix login | api"
        self.tab["label"] = "Fix login | api"
        herdr_agents.tabs([self.pane], {})
        self.assertEqual(self.tab["label"], "🌀 Fix login | api")


if __name__ == "__main__":
    unittest.main()
