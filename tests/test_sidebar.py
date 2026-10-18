"""Tests of herdr-sidebar.py's readers and formatting, on made-up session logs. They never
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
spec = importlib.util.spec_from_file_location("sidebar", os.path.join(HERE, "..", "herdr-sidebar.py"))
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


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
        self.assertEqual(sidebar.label(now - 30, now), "now")
        self.assertEqual(sidebar.label(now - 7 * 60, now), "7m")
        self.assertEqual(sidebar.label(now - 5 * 3600 - 59 * 60, now), "5h")
        self.assertEqual(sidebar.label(now - 3 * 86400, now), "3d")

    def test_thousands(self):
        self.assertIsNone(sidebar.kilo(None))
        self.assertIsNone(sidebar.kilo(0))
        self.assertEqual(sidebar.kilo(5100), "5k")
        self.assertEqual(sidebar.kilo(5500), "6k")
        self.assertEqual(sidebar.kilo(120_300), "120k")

    def test_command_and_output(self):
        t = sidebar.command_tokens(("cd /src/api && pytest -q\nmore", "..\n2 passed in 0.1s\n", False, None))
        self.assertEqual(t, {"last_cmd": "$ pytest -q", "last_out": "2 passed in 0.1s"})

    def test_running(self):
        self.assertEqual(sidebar.command_tokens(("make", None, False, None))["last_out"], "…")

    def test_failed(self):
        t = sidebar.command_tokens(("false", "Exit code 1", True, None))
        self.assertEqual(t["last_out"], "✗ exit 1")
        t = sidebar.command_tokens(("pytest", "FAILED test_x.py::test_a\nExit code 1", True, None))
        self.assertEqual(t["last_out"], "✗ FAILED test_x.py::test_a")

    def test_no_output(self):
        self.assertEqual(sidebar.command_tokens(("touch x", "(no output)", False, None))["last_out"], "✓")

    def test_agent_lines_and_colours_skipped(self):
        out = "\x1b[32mbuilt\x1b[0m\n}\nShell cwd was reset to /src"
        self.assertEqual(sidebar.command_tokens(("make", out, False, None))["last_out"], "built")

    def test_no_command(self):
        self.assertEqual(sidebar.command_tokens(None), {"last_cmd": None, "last_out": None})

    def test_place(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            self.assertEqual(sidebar.place({"cwd": "/home/u"}), "~")
            self.assertEqual(sidebar.place({"cwd": "/home/u/src/api/", "foreground_cwd": "/srv/web"}), "web")
            self.assertIsNone(sidebar.place({}))


class Claude(Logs):
    def test_last_command(self):
        path = self.log(bash_call("a", "ls"), bash_result("a", "x"),
                        bash_call("b", "git status --short"), bash_result("b", " M app.py"))
        self.assertEqual(sidebar.claude_command(path)[:3], ("git status --short", " M app.py", False))

    def test_command_still_running(self):
        path = self.log(bash_call("a", "sleep 60"))
        self.assertEqual(sidebar.claude_command(path)[:3], ("sleep 60", None, False))

    def test_background_command_follows_its_output_file(self):
        out = os.path.join(self.tmp.name, "task.output")
        with open(out, "w") as f:
            f.write("step 1\nstep 2\n")
        path = self.log(bash_call("a", "./build.sh"), bash_result(
            "a", "Command running in background with ID: b1. Output is being written to: %s" % out))
        cmd, text, failed, follow = sidebar.claude_command(path)
        self.assertEqual((text, follow), ("step 1\nstep 2\n", out))

    def test_subagent_commands_ignored(self):
        side = dict(bash_call("s", "rm -rf build"), isSidechain=True)
        path = self.log(bash_call("a", "ls"), bash_result("a", "x"), side)
        self.assertEqual(sidebar.claude_command(path)[0], "ls")

    def test_context(self):
        path = self.log(usage(input_tokens=2, cache_creation_input_tokens=1000,
                              cache_read_input_tokens=40000, output_tokens=500))
        self.assertEqual(sidebar.claude_context(path), 41502)

    def test_context_skips_synthetic_message(self):
        path = self.log(usage(input_tokens=10, output_tokens=20), usage(input_tokens=0, output_tokens=0))
        self.assertEqual(sidebar.claude_context(path), 30)

    def test_context_after_compaction(self):
        path = self.log(usage(input_tokens=150000, output_tokens=1),
                        {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": 9000}})
        self.assertEqual(sidebar.claude_context(path), 9000)

    def test_last_message_time(self):
        path = self.log({"type": "user", "timestamp": "2026-01-02T03:04:05.000Z"}, {"type": "summary"})
        self.assertEqual(sidebar.last_message_at(path), 1767323045)


class Codex(Logs):
    def test_last_command(self):
        path = self.log({"payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "command": ["/bin/bash", "-lc", "cargo test"],
            "exit_code": 101, "aggregated_output": "test result: FAILED"}}})
        self.assertEqual(sidebar.codex_command(path), ("cargo test", "test result: FAILED\nExit code 101", True, None))

    def test_context(self):
        path = self.log({"payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 23456}}}},
                        {"payload": {"type": "token_count", "info": None}})
        self.assertEqual(sidebar.codex_context(path), 23456)

    def test_context_empty_after_compaction(self):
        path = self.log({"payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 9}}}},
                        {"type": "compacted", "payload": {}})
        self.assertIsNone(sidebar.codex_context(path))


class Pi(Logs):
    def test_last_command(self):
        path = self.log(
            {"message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "echo hi"}}]}},
            {"message": {"role": "toolResult", "toolCallId": "t1", "content": [{"type": "text", "text": "hi"}]}})
        self.assertEqual(sidebar.pi_command(path), ("echo hi", "hi", False, None))

    def test_context_skips_aborted_reply(self):
        path = self.log({"message": {"role": "assistant", "usage": {"totalTokens": 8000}}},
                        {"message": {"role": "assistant", "stopReason": "aborted", "usage": {"totalTokens": 1}}})
        self.assertEqual(sidebar.pi_context(path), 8000)


class Hook(unittest.TestCase):
    def test_silent_outside_herdr(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HERDR_ENV": ""}), redirect_stdout(out):
            sidebar.main()
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
