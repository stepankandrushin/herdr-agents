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


def say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


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

    def test_message_on_one_line(self):
        said = "## Done\n\n- **Tests** pass: `pytest -q`\n```sh\nmake\n```\nSee [notes](/src/api/notes.md)."
        self.assertEqual(herdr_agents.gist(said), "Done Tests pass: pytest -q make See notes.")
        self.assertIsNone(herdr_agents.gist(None))
        self.assertIsNone(herdr_agents.gist("```\n```"))

    def test_place(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            self.assertEqual(herdr_agents.place({"cwd": "/home/u"}), "~")
            self.assertEqual(herdr_agents.place({"cwd": "/home/u/src/api/", "foreground_cwd": "/srv/web"}), "web")
            self.assertIsNone(herdr_agents.place({}))

    def test_icon_place(self):
        reports = []
        panes = [{"agent": "claude", "pane_id": "p1", "cwd": "/src/api"},
                 {"agent": "pi", "pane_id": "p2"},
                 {"agent": "codex", "pane_id": "p3", "cwd": "/src/docs",
                  "tokens": {"place": "docs", "icon_place": "🌀 docs"}}]
        with mock.patch.object(herdr_agents, "call", lambda m, p: reports.append(p) or {}):
            herdr_agents.places(panes)
        self.assertEqual([(r["pane_id"], r["tokens"]) for r in reports],
                         [("p1", {"place": "api", "icon_place": "🦀 api"}),
                          ("p2", {"place": None, "icon_place": "🥧"})])


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

    def test_last_message(self):
        side = {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "sub"}]}}
        path = self.log(say("Fixed the redirect."), bash_call("a", "pytest"), bash_result("a", "ok"), side)
        self.assertEqual(herdr_agents.claude_message(path), "Fixed the redirect.")

    def test_message_reported_with_the_command(self):
        path = self.log(say("Running the tests."), bash_call("a", "pytest -q"), bash_result("a", "2 passed"))
        pane = {"agent": "claude", "pane_id": "w1:p1"}
        with mock.patch.object(herdr_agents, "session_log", return_value=path), \
                mock.patch.object(herdr_agents, "call") as call:
            herdr_agents.commands([pane], {}, {})
        tokens = call.call_args.args[1]["tokens"]
        self.assertEqual((tokens["last_message"], tokens["last_cmd"]), ("Running the tests.", "$ pytest -q"))

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


def tool(name, **args):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": name, "input": args}], "stop_reason": "tool_use"}}


def done(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}}


class Subagents(Logs):
    def subagent(self, id, meta, *events, at="2026-01-02T03:04:05.000Z", age=0):
        folder = os.path.join(self.tmp.name, "s1", "subagents")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "agent-%s.meta.json" % id), "w") as f:
            json.dump(meta, f)
        log = self.log(dict(say("start"), type="user", timestamp=at), *events,
                       name=os.path.join("s1", "subagents", "agent-%s.jsonl" % id))
        os.utime(log, (1_000_000 - age, 1_000_000 - age))
        return log

    def running(self, started=1_000_000 - 60):
        transcript = self.log(say("main"), name="s1.jsonl")
        with mock.patch.object(herdr_agents, "claude_started", return_value=started):
            return herdr_agents.running_subagents("w1:p1", transcript, {})

    def test_tool_calls_in_words(self):
        action = lambda name, **args: herdr_agents.action(tool(name, **args)["message"]["content"][0])
        self.assertEqual(action("Read", file_path="/src/api/notes.md"), "Reading notes.md")
        self.assertEqual(action("Edit", file_path="/src/api/auth.py", old_string="a"), "Editing auth.py")
        self.assertEqual(action("Grep", pattern="TODO", path="/src"), "Searching TODO")
        self.assertEqual(action("WebFetch", url="https://docs.python.org/3/x.html"), "Fetching docs.python.org")
        self.assertEqual(action("Bash", command="pytest -q", description="Run the tests"), "Run the tests")
        self.assertEqual(action("Bash", command="cd /src/api && pytest -q\nmore"), "$ pytest -q")
        self.assertEqual(action("mcp__browser__click", ref="e1"), "click")

    def test_doing_is_the_newest_call_or_text(self):
        log = self.subagent("a1", {}, tool("Read", file_path="/a/b.py"), bash_result("t", "x"))
        self.assertEqual(herdr_agents.subagent_state(log), (False, "Reading b.py"))
        log = self.subagent("a2", {}, tool("Read", file_path="/a/b.py"), say("Now the tests."))
        self.assertEqual(herdr_agents.subagent_state(log), (False, "Now the tests."))
        log = self.subagent("a3", {}, tool("Read", file_path="/a/b.py"), done("All **done**."))
        self.assertEqual(herdr_agents.subagent_state(log), (True, "All done."))

    def test_running_ones_first_launched_first(self):
        self.subagent("b", {"description": "Check the docs"}, tool("Grep", pattern="TODO"),
                      at="2026-01-02T03:05:00.000Z")
        self.subagent("a", {"description": "Fix the login test"}, tool("Edit", file_path="/s/auth.py"),
                      bash_result("t", "ok"), at="2026-01-02T03:04:00.000Z")
        self.subagent("c", {"description": "Finished", "finished": True}, tool("Read", file_path="/x"))
        self.assertEqual(self.running(), [("Fix the login test", "Editing auth.py"), ("Check the docs", "Searching TODO")])

    def test_woken_again_runs(self):
        self.subagent("a", {"description": "Fix", "revivedAt": 1}, done("Done."), bash_result("t", "more"))
        self.assertEqual(self.running(), [("Fix", "Done.")])

    def test_ended_turn_without_finished_mark_is_not_running(self):
        self.subagent("a", {"description": "Fix"}, tool("Read", file_path="/x"), done("Done."))
        self.assertEqual(self.running(), [])

    def test_left_by_an_earlier_run_is_not_running(self):
        self.subagent("a", {"description": "Fix"}, tool("Read", file_path="/x"), age=3600)
        self.assertEqual(self.running(started=1_000_000 - 60), [])
        self.assertEqual(self.running(started=1_000_000 - 7200), [("Fix", "Reading x")])

    def test_no_subagents(self):
        self.assertEqual(self.running(), [])

    def test_tokens(self):
        none = herdr_agents.subagent_tokens([])
        self.assertEqual(none, {"subagents": None, "subagent_1": None, "subagent_2": None, "subagent_3": None})
        two = herdr_agents.subagent_tokens([("Fix the login test", "Editing auth.py"), ("Check the docs", None)])
        self.assertEqual(two, {"subagents": "◐ 2", "subagent_1": "├ Fix the login test · Editing auth.py",
                               "subagent_2": "└ Check the docs", "subagent_3": None})
        five = herdr_agents.subagent_tokens([("t%d" % i, None) for i in range(5)])
        self.assertEqual([five["subagents"], five["subagent_1"], five["subagent_2"], five["subagent_3"]],
                         ["◐ 5", "├ t0", "├ t1", "└ 3 more"])

    def test_reported_and_cleared(self):
        transcript = self.log(say("main"), name="s1.jsonl")
        self.subagent("a", {"description": "Fix"}, tool("Read", file_path="/x"))
        pane = {"agent": "claude", "pane_id": "w1:p1"}
        with mock.patch.object(herdr_agents, "session_log", return_value=transcript), \
                mock.patch.object(herdr_agents, "claude_started", return_value=0), \
                mock.patch.object(herdr_agents, "call") as call:
            herdr_agents.subagents([pane], {})
            self.assertEqual(call.call_args.args[1]["tokens"]["subagent_1"], "└ Fix · Reading x")
            pane["tokens"] = call.call_args.args[1]["tokens"]
            call.reset_mock()
            herdr_agents.subagents([pane], {})
            call.assert_not_called()  # unchanged
            with open(os.path.join(self.tmp.name, "s1", "subagents", "agent-a.meta.json"), "w") as f:
                json.dump({"description": "Fix", "finished": True}, f)
            herdr_agents.subagents([pane], {})
            self.assertEqual(call.call_args.args[1]["tokens"]["subagents"], None)

    def test_process_start(self):
        for etime, seconds in (("05:03", 303), ("02:00:01", 7201), ("1-00:00:10", 86410)):
            run = mock.Mock(return_value=mock.Mock(stdout="  %s\n" % etime))
            with mock.patch.object(herdr_agents.subprocess, "run", run), \
                    mock.patch.object(herdr_agents.time, "time", return_value=1_000_000):
                self.assertEqual(herdr_agents.process_start(42), 1_000_000 - seconds)
        with mock.patch.object(herdr_agents.subprocess, "run", return_value=mock.Mock(stdout="")):
            self.assertIsNone(herdr_agents.process_start(42))


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

    def test_last_message(self):
        reply = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Committed on main."}]}}
        path = self.log(reply, {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "thanks"}]}})
        self.assertEqual(herdr_agents.codex_message(path), "Committed on main.")


class CodexDaemon(Logs):
    """Codex's default mode: the shared app-server daemon (pid 2) holds the rollouts, the pane's
    codex (pid 1) none."""
    API = "019a0000-0000-7000-8000-000000000001"
    DOCS = "019a0000-0000-7000-8000-000000000002"

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.tmp.name, "app-server-daemon"))
        with open(os.path.join(self.tmp.name, "app-server-daemon", "daemon.pid"), "w") as f:
            json.dump({"pid": 2, "processStartTime": "Sat Jan  3 10:00:00 2026"}, f)
        self.names = [{"id": self.API, "thread_name": "Fix login"},
                      {"id": self.DOCS, "thread_name": "Write the docs"}]
        self.held = {1: [], 2: [self.rollout(self.API), self.rollout(self.DOCS)]}
        for name, value in (("CODEX", self.tmp.name),
                            ("held_rollouts", lambda pid, cache: self.held.get(pid, [])),
                            ("call", lambda method, params: {"process_info": {"foreground_processes": [
                                {"pid": 1, "name": "codex", "cwd": "/src/api"}]}})):
            patch = mock.patch.object(herdr_agents, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def rollout(self, thread):
        return "/home/u/.codex/sessions/2026/01/03/rollout-2026-01-03T10-00-00-%s.jsonl" % thread

    def found(self, title):
        with open(os.path.join(self.tmp.name, "session_index.jsonl"), "w") as f:
            for e in self.names:
                f.write(json.dumps(e) + "\n")
        pane = {"agent": "codex", "pane_id": "w1:p1", "terminal_title_stripped": title}
        return herdr_agents.session_log(pane, {})

    def test_the_session_its_title_names(self):
        self.assertEqual(self.found("Fix login | api"), self.rollout(self.API))
        self.assertEqual(self.found("Write the docs | api"), self.rollout(self.DOCS))

    def test_renamed_and_long_names(self):
        self.names.append({"id": self.API, "thread_name": "Fix the login flow on every page of the app, then tests"})
        self.assertEqual(self.found("Fix the login flow on every page of the app, ... | api"),
                         self.rollout(self.API))
        self.assertIsNone(self.found("Fix login | api"))

    def test_none_before_the_session_has_a_name(self):
        self.names = []
        self.assertIsNone(self.found("api"))
        self.assertEqual(self.found(self.DOCS + " | api"), self.rollout(self.DOCS))  # the "thread" item

    def test_two_sessions_of_one_name_the_one_at_work(self):
        older, newer = (self.log({}, name="rollout-2026-01-03T10-00-00-%s.jsonl" % t) for t in (self.API, self.DOCS))
        os.utime(older, (1, 1))
        self.held[2] = [older, newer]
        self.names = [{"id": self.API, "thread_name": "Fix login"}, {"id": self.DOCS, "thread_name": "Fix login"}]
        self.assertEqual(self.found("Fix login | api"), newer)

    def test_a_codex_holding_its_own_rollout_needs_no_daemon(self):
        self.held[1] = ["/home/u/.codex/sessions/2026/01/03/rollout-2026-01-03T09-00-00-own.jsonl"]
        self.assertEqual(self.found("Fix login | api"), self.held[1][0])

    def test_no_daemon(self):
        os.remove(os.path.join(self.tmp.name, "app-server-daemon", "daemon.pid"))
        self.assertIsNone(self.found("Fix login | api"))


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

    def test_last_message(self):
        path = self.log({"message": {"role": "assistant", "content": [{"type": "text", "text": "Found it."}]}},
                        {"message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm"}]}})
        self.assertEqual(herdr_agents.pi_message(path), "Found it.")

    def test_aborted_turn_is_the_last_message(self):
        path = self.log({"message": {"role": "assistant", "content": [{"type": "text", "text": "Found it."}]}},
                        {"message": {"role": "assistant", "content": [], "stopReason": "aborted",
                                     "errorMessage": "Operation aborted"}})
        self.assertEqual(herdr_agents.pi_message(path), "Operation aborted")


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
            self.assertIn(params["agent"], ("codex", "pi"))
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
        pi = self.pane
        pi.update(agent="pi", terminal_title="π - api", terminal_title_stripped="π - api")
        herdr_agents.tabs([pi], named)
        self.assertEqual(self.tab["label"], "🥧 api")
        pi.update(terminal_title="π - Fix docs - api", terminal_title_stripped="π - Fix docs - api")
        herdr_agents.tabs([pi], named)
        self.assertEqual(self.tab["label"], "🥧 Fix docs - api")
        self.assertEqual(pi["tokens"]["session_title"], "Fix docs - api")
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
