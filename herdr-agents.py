#!/usr/bin/env python3
# herdr-agents: more in herdr's agents sidebar for Claude Code, Codex and pi panes.
# https://github.com/stepankandrushin/herdr-agents, MIT license. README.md says how to install it.
#
# A Claude Code hook (SessionStart, UserPromptSubmit, Stop, StopFailure). It does seven things in
# the herdr server the pane belongs to:
#
# - Reports how long ago each Claude Code or Codex session last did something as the pane token
#   $last_active, which herdr's sidebar rows can show (config.toml): now, 7m, 5h, 3d,
#   and "now" for as long as herdr sees it at work. The time itself is kept in $last_active_at.
#   A pane with no time yet takes it from its transcript's last message.
# - Reports the folder each agent pane runs in as $place, for Claude Code, Codex and pi: the
#   pane's working directory by name, where herdr's workspace label would name the folder the
#   workspace was opened in. And as $icon_place after the agent's emoji: 🦀 api.
# - Reports each agent pane's last shell command and the last line of its output as $last_cmd
#   and $last_out, for Claude Code, Codex and pi alike, read from the agent's own session log:
#   Claude Code's transcript, the Codex rollout its process holds open, the pi session file
#   herdr's pi integration names. A command still running shows "…", one that failed "✗".
# - Reports the start of each agent pane's last message to the user, on one line, from the
#   same log, as $last_message: Done, tests pass and the fix is committed…
# - Reports the context each agent pane's session holds, from the same log, as $context: the
#   tokens of its last model call to the thousand (5k, 120k).
# - Reports the sub-agents running in each Claude Code pane, which go on after the session's own
#   turn has ended: how many as $subagents (◐ 2), and each one's task and what it is doing now
#   as $subagent_1 to $subagent_3: ├ Fix the login test · Editing auth.py.
# - Names a Claude Code, Codex or pi pane's tab as the agent's emoji (🦀 Claude Code, 🌀 Codex,
#   🥧 pi) and the session name in its terminal title, including names set with /rename.
#   A new session in the same tab renames it again. When the agent
#   has gone from the tab (quit, or a shell or another agent in its place), the tab takes
#   herdr's number back. Only a tab holding no other agent, whose label is still herdr's
#   generated number or the name this gave it, so a tab renamed by hand keeps its name.
#
# The hook starts `herdr-agents.py --tick`, one per herdr server, which keeps all seven
# current: every two seconds it rereads the logs that have grown, redoes the ages that have
# moved on, follows each supported pane's title into its tab, and starts itself again when this
# file changes. It exits when the server has been gone for two minutes; the next hook starts a
# new one.
#
# Prints nothing: a SessionStart or UserPromptSubmit hook's stdout goes to the model.
import contextlib
import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time

SOURCE = "herdr-agents"
SOCK = os.environ.get("HERDR_SOCKET_PATH")
PANE = os.environ.get("HERDR_PANE_ID")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
STATE_DIR = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
STATE = os.path.join(STATE_DIR, "herdr-claude-tabs.json")
TICK = 2  # seconds between the ticker's passes
GONE = 120  # seconds without the server before the ticker gives up


def call(method, params):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(SOCK)
        req = {"id": "%s:%d" % (SOURCE, time.time_ns()), "method": method, "params": params}
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf or b"{}").get("result") or {}


def entries(path, size=1 << 20):
    """The log's entries, newest first, from its last megabyte."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        f.seek(max(0, end - size))
        lines = f.read().split(b"\n")
    if end > size:
        lines = lines[1:]  # cut in the middle
    for line in reversed(lines):
        try:
            yield json.loads(line)
        except ValueError:
            pass


def last_message_at(path, agent="claude"):
    for e in entries(path):
        relevant = e.get("type") in ("user", "assistant")
        if agent == "codex":
            p = e.get("payload") or {}
            relevant = (
                e.get("type") == "event_msg" and p.get("type") in (
                    "task_started", "task_complete", "turn_aborted", "user_message",
                    "agent_message", "item_started", "item_completed")
                or e.get("type") == "response_item" and p.get("type") in (
                    "message", "function_call", "function_call_output",
                    "custom_tool_call", "custom_tool_call_output"))
        if relevant and e.get("timestamp"):
            try:
                stamp = e["timestamp"].replace("Z", "+00:00")  # fromisoformat before 3.11
                return datetime.datetime.fromisoformat(stamp).timestamp()
            except (ValueError, TypeError, AttributeError):
                pass
    return None


def transcript(session_id):
    found = glob.glob(os.path.join(CLAUDE, "projects", "*", "%s.jsonl" % session_id))
    return found[0] if found else None


def label(at, now):
    minutes = int(now - at) // 60
    if minutes < 1:
        return "now"
    if minutes < 60:
        return "%dm" % minutes
    if minutes < 24 * 60:
        return "%dh" % (minutes // 60)
    return "%dd" % (minutes // (24 * 60))


def report(pane, at, now, agent="claude"):
    call("pane.report_metadata", {
        "pane_id": pane, "source": SOURCE, "agent": agent, "seq": time.time_ns(),
        "tokens": {"last_active": label(at, now) if at else None,
                   "last_active_at": "%d" % at if at else None}})


def refresh(agents, now, skip=None, cache=None):
    """Keep Claude and Codex ages current. Codex has no hook, so its own log supplies the time,
    even when the pane still carries tokens from a previous session."""
    if cache is None:
        cache = {}
    for a in agents:
        agent = a.get("agent")
        if agent not in ("claude", "codex") or a is skip:
            continue
        tokens = a.get("tokens") or {}
        if agent == "codex":
            path = session_log(a, cache)
            key = ("activity", a["pane_id"])
            signature = stamp(path)
            if key not in cache or cache[key][0] != signature:
                try:
                    at = last_message_at(path, agent) if path else None
                except OSError:
                    at = None
                cache[key] = (signature, at)
            at = cache[key][1]
        else:
            try:
                at = float(tokens["last_active_at"])
            except (KeyError, ValueError, TypeError):
                found = transcript((a.get("agent_session") or {}).get("value") or "-")
                at = found and last_message_at(found)
        if a.get("agent_status") == "working" and now - (at or 0) >= 60:
            at = now
        if not at and agent == "claude":
            continue
        age = label(at, now) if at else None
        epoch = "%d" % at if at else None
        if tokens.get("last_active") != age or tokens.get("last_active_at") != epoch:
            report(a["pane_id"], at, now, agent)


# The last shell command. Each reader returns (command, output, failed, follow), or None when
# the log holds no command yet. Output is None while the command runs; follow is the file that
# holds the output instead of the log (a background command's, or one too long for the log),
# which the ticker watches as well.

def texts(content, kind="text"):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == kind)


def tail(path, size=8192):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode(errors="replace")
    except OSError:
        return None


def claude_command(path):
    results = {}
    for e in entries(path):
        content = (e.get("message") or {}).get("content")
        if e.get("isSidechain") or not isinstance(content, list):
            continue
        for c in reversed(content):
            if e.get("type") == "user" and c.get("type") == "tool_result":
                results[c.get("tool_use_id")] = c
            elif e.get("type") == "assistant" and c.get("type") == "tool_use" and c.get("name") == "Bash":
                r = results.get(c.get("id"))
                cmd = (c.get("input") or {}).get("command") or ""
                out = r and texts(r.get("content"))
                background = out and BACKGROUND.match(out)
                moved = background or (out and PERSISTED.search(out[:300]))
                follow = moved and moved.group(1)
                saved = follow and tail(follow)
                if saved or (background and saved == ""):
                    out = saved or None  # a background command not printing yet runs on
                return cmd, out, bool(r and r.get("is_error")), follow
    return None


def codex_command(path):
    for e in entries(path):
        p = e.get("payload") or {}
        item = p.get("item") or {}
        if p.get("type") == "item_completed" and item.get("type") == "CommandExecution":
            cmd = item.get("command") or ""
            if isinstance(cmd, list):
                cmd = cmd[-1] if cmd else ""  # ["/bin/bash", "-lc", "..."]
            code = item.get("exit_code")
            out = item.get("aggregated_output") or ""
            if code:
                out += "\nExit code %s" % code
            return cmd, out, bool(code), None
    return None


def pi_command(path):
    results = {}
    for e in entries(path):
        m = e.get("message") or {}
        if m.get("role") == "toolResult":
            results[m.get("toolCallId")] = m
        elif m.get("role") == "assistant":
            for c in reversed(m.get("content") or []):
                if c.get("type") == "toolCall" and c.get("name") == "bash":
                    r = results.get(c.get("id"))
                    cmd = (c.get("arguments") or {}).get("command") or ""
                    return cmd, r and texts(r.get("content")), bool(r and r.get("isError")), None
    return None


READERS = {"claude": claude_command, "codex": codex_command, "pi": pi_command}


# The context in use: the tokens of the last model call, its input and its reply, as each agent
# counts them for its own context gauge. None before the first reply, and after a compaction
# until the next one, unless the log says what the compaction left.

def claude_context(path):
    for e in entries(path):
        if e.get("isSidechain"):
            continue
        if e.get("subtype") == "compact_boundary":
            return (e.get("compactMetadata") or {}).get("postTokens")
        u = e.get("type") == "assistant" and (e.get("message") or {}).get("usage")
        n = u and sum(u.get(k) or 0 for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        if n:  # a <synthetic> message (an error, a refusal) counts nothing
            return n
    return None


def codex_context(path):
    for e in entries(path):
        p = e.get("payload") or {}
        if e.get("type") == "compacted":
            return None
        if p.get("type") == "token_count" and p.get("info"):  # without info: rate limits only
            return (p["info"].get("last_token_usage") or {}).get("total_tokens") or None
    return None


def pi_context(path):
    for e in entries(path):
        m = e.get("message") or {}
        if e.get("type") == "compaction":
            return None
        u = m.get("role") == "assistant" and m.get("stopReason") not in ("aborted", "error") and m.get("usage")
        n = u and (u.get("totalTokens") or sum(u.get(k) or 0 for k in ("input", "output", "cacheRead", "cacheWrite")))
        if n:
            return n
    return None


CONTEXT = {"claude": claude_context, "codex": codex_context, "pi": pi_context}


# The agent's last message to the user: its last text, written between tool calls or at the end
# of a turn, or for pi a turn that ended without one ("Operation aborted", "Request timed out.").
# None before the first.

def claude_message(path):
    for e in entries(path):
        if e.get("type") == "assistant" and not e.get("isSidechain"):
            text = texts((e.get("message") or {}).get("content"))
            if text.strip():
                return text
    return None


def codex_message(path):
    for e in entries(path):
        p = e.get("payload") or {}
        if e.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
            text = texts(p.get("content"), "output_text")
            if text.strip():
                return text
    return None


def pi_message(path):
    for e in entries(path):
        m = e.get("message") or {}
        if m.get("role") != "assistant":
            continue
        if m.get("stopReason") in ("aborted", "error") and m.get("errorMessage"):
            return m["errorMessage"]
        text = texts(m.get("content"))
        if text.strip():
            return text
    return None


MESSAGE = {"claude": claude_message, "codex": codex_message, "pi": pi_message}


def kilo(n):
    """5k, 120k: to the thousand, short for the sidebar."""
    return "%dk" % int(n / 1000 + 0.5) if n else None
BACKGROUND = re.compile(r"^Command running in background with ID: \S+\. Output is being written to: (\S+?)\.?(\s|$)")
PERSISTED = re.compile(r"Full output saved to: (\S+)")
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)?|[@-Z\\-_])")
CD = re.compile(r"""^cd ("[^"]*"|'[^']*'|\S+) *(&&|;) *""")
EXIT = re.compile(r"^(?:Exit code|Command exited with code) (\d+)$")
NOISE = re.compile(  # the agent's, not the command's
    r"^(Shell cwd was reset to |Session cwd remains |\[Showing lines \d|</?persisted-output>$)")
EMPTY = ("(no output)", "(Bash completed with no output)")


def one_line(s):
    s = ANSI.sub("", s).split("\r")[-1]  # a progress bar's last state
    return " ".join("".join(ch if ch.isprintable() else " " for ch in s).split())


FENCE = re.compile(r"^\s*(```|~~~)")
MARK = re.compile(r"^\s*(#+|[-*+>])\s+")  # a heading's, a list item's, a quote's
LINK = re.compile(r"\[([^]]*)\]\([^)]*\)")


def gist(text):
    """A message as one line, from its start, without markdown's marks: the sidebar shows as
    much as fits."""
    if not text:
        return None
    lines = [MARK.sub("", line) for line in text.splitlines() if not FENCE.match(line)]
    return one_line(LINK.sub(r"\1", " ".join(lines)).replace("**", "").replace("`", ""))[:200] or None


def command_tokens(found):
    if not found:
        return {"last_cmd": None, "last_out": None}
    cmd, out, failed = found[:3]
    lines = cmd.strip().splitlines()
    cmd = CD.sub("", one_line(lines[0])) if lines else ""
    if out is None:
        return {"last_cmd": "$ " + cmd[:200], "last_out": "…"}
    code, last = None, ""
    for line in out.splitlines():
        line = one_line(line)
        m = EXIT.match(line)
        if m:
            code = m.group(1)
        elif any(ch.isalnum() for ch in line) and line not in EMPTY and not NOISE.match(line):
            last = line  # not a bare ] or } closing a JSON dump
    if failed:
        last = "✗ " + (last or ("exit " + code if code else "failed"))
    return {"last_cmd": "$ " + cmd[:200], "last_out": (last or "✓")[:200]}


def open_rollout(pane, cache):
    """The Codex rollout file the pane's codex process is writing."""
    info = call("pane.process_info", {"pane_id": pane}).get("process_info") or {}
    for p in info.get("foreground_processes") or []:
        if p.get("name") != "codex":
            continue
        pid, now = p["pid"], time.time()
        fds = "/proc/%d/fd" % pid
        last = cache.get(pane)
        if not os.path.isdir(fds) and last and last[0] == pid and now - last[2] < 30:
            return last[1]  # lsof costs a Mac a tenth of a second: once in 30 s
        if os.path.isdir(fds):
            targets = []
            for fd in os.listdir(fds):
                try:
                    targets.append(os.readlink(os.path.join(fds, fd)))
                except OSError:
                    pass
        else:  # macOS
            out = subprocess.run(["lsof", "-Fn", "-p", str(pid)], capture_output=True, text=True).stdout
            targets = [line[1:] for line in out.splitlines() if line.startswith("n")]
        found = [t for t in targets if "/sessions/" in t and os.path.basename(t).startswith("rollout-")
                 and t.endswith(".jsonl")]
        path = max(found, key=lambda t: os.path.getmtime(t) if os.path.exists(t) else 0) if found else None
        cache[pane] = (pid, path, now)
        return path
    return None


def session_log(a, cache):
    sess = a.get("agent_session") or {}
    if a.get("agent") == "claude":
        sid = sess.get("value")
        if sid and not cache.get(sid):
            cache[sid] = transcript(sid)
        return sid and cache.get(sid)
    if a.get("agent") == "pi":
        return sess.get("value") if sess.get("kind") == "path" else None
    if a.get("agent") == "codex":
        return open_rollout(a["pane_id"], cache)
    return None


def stamp(path):
    try:
        st = os.stat(path)
        return path, st.st_size, st.st_mtime_ns
    except (OSError, TypeError):
        return path


def commands(agents, seen, cache):
    """Report each agent pane's last command, last message and context whose log has changed
    since the last pass."""
    for a in agents:
        read = READERS.get(a.get("agent"))
        if not read:
            continue
        path = session_log(a, cache)
        last = seen.get(a["pane_id"])
        if last and last[0] == stamp(path) and stamp(last[1]) == last[2]:
            continue
        found = size = said = None
        try:
            if path and os.path.exists(path):
                size = CONTEXT[a["agent"]](path)
                said = MESSAGE[a["agent"]](path)
                found = read(path)
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # a log this cannot read: tried again once it grows
        follow = found and found[3]
        seen[a["pane_id"]] = (stamp(path), follow, stamp(follow))
        tokens = dict(command_tokens(found), context=kilo(size), last_message=gist(said))
        have = a.get("tokens") or {}
        if any(have.get(k) != v for k, v in tokens.items()):
            call("pane.report_metadata", {
                "pane_id": a["pane_id"], "source": SOURCE, "agent": a["agent"], "seq": time.time_ns(),
                "tokens": tokens})


# Claude Code's sub-agents. Each one writes its own log beside the session's transcript,
# <session>/subagents/agent-<id>.jsonl, and a meta file there with its task's description, which
# Claude Code marks "finished" when it ends: done, failed or stopped. SendMessage can wake a
# finished one, which takes the mark away again.

SUBAGENT_ROWS = 3  # $subagent_1 to $subagent_3; with more running, the last says how many more
VERBS = {"Read": "Reading", "Edit": "Editing", "MultiEdit": "Editing", "NotebookEdit": "Editing",
         "Write": "Writing", "Grep": "Searching", "Glob": "Finding", "WebFetch": "Fetching",
         "WebSearch": "Searching the web for"}


def action(c):
    """A tool call in words, the way Claude Code lists its agents: Editing notes.md, Searching
    TODO, or the call's own description (a Bash command's)."""
    name, args = c.get("name") or "", c.get("input") or {}
    target = next((args[k] for k in ("file_path", "notebook_path", "pattern", "query", "url")
                   if isinstance(args.get(k), str) and args[k]), None)
    if name in VERBS and target:
        if "file_path" in args or "notebook_path" in args:
            target = os.path.basename(target)
        elif name == "WebFetch":
            target = re.sub(r"^\w+://([^/]+).*", r"\1", target)
        return "%s %s" % (VERBS[name], target)
    if args.get("description"):
        return args["description"]
    if isinstance(args.get("command"), str) and args["command"].strip():
        return "$ " + CD.sub("", one_line(args["command"].strip().splitlines()[0]))
    return name.split("__")[-1] or None  # mcp__server__tool


def subagent_state(path):
    """(ended, doing): whether the sub-agent's last reply ended its turn, for Claude Code
    versions without the finished mark, and what it is doing: its newest tool call, or the text
    it wrote since."""
    ended = None
    for e in entries(path, 1 << 18):
        m = e.get("message") or {}
        if e.get("type") not in ("assistant", "user"):
            continue
        if ended is None:
            ended = e["type"] == "assistant" and m.get("stop_reason") not in (None, "tool_use")
        if e["type"] != "assistant" or not isinstance(m.get("content"), list):
            continue
        for c in reversed(m["content"]):
            if c.get("type") == "tool_use":
                return ended, action(c)
            if c.get("type") == "text" and (c.get("text") or "").strip():
                return ended, gist(c["text"])
    return bool(ended), None


def launched_at(path):
    """When the sub-agent started: its log's first entry."""
    with open(path, "rb") as f:
        when = json.loads(f.readline()).get("timestamp")
    return datetime.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def cached(cache, key, path, read):
    """read(path), read again only once the file has changed; None when it cannot be read."""
    signature = stamp(path)
    if key not in cache or cache[key][0] != signature:
        try:
            value = read(path)
        except (OSError, ValueError, AttributeError, TypeError):
            value = None
        cache[key] = (signature, value)
    return cache[key][1]


def process_start(pid):
    """When the process started, from ps's elapsed time ([[dd-]hh:]mm:ss), on Linux and macOS."""
    out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True).stdout
    days, _, clock = out.strip().rpartition("-")
    seconds = 0
    try:
        for part in clock.split(":"):
            seconds = seconds * 60 + int(part)
        return time.time() - seconds - int(days or 0) * 86400
    except ValueError:
        return None  # gone


def claude_started(pane, cache):
    """When the pane's Claude Code process started, or None when herdr does not say."""
    info = call("pane.process_info", {"pane_id": pane}).get("process_info") or {}
    for p in info.get("foreground_processes") or []:
        if "claude" in [p.get("name")] + [os.path.basename(arg) for arg in (p.get("argv") or [])[:2]]:
            key = ("started", p["pid"])
            if key not in cache:
                cache[key] = process_start(p["pid"])
            return cache[key]
    return None


def running_subagents(pane, path, cache):
    """The sub-agents running in the Claude Code session whose transcript is path, the first
    launched first: (task, doing). One without the finished mark whose log has not grown since
    this Claude Code started was left by an earlier run of the session, which quit or crashed
    with it running, so it runs no more."""
    folder = os.path.join(path[:-len(".jsonl")], "subagents")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    found, started = [], None
    for name in names:
        if not (name.startswith("agent-") and name.endswith(".meta.json")):
            continue
        meta = cached(cache, ("meta", folder, name), os.path.join(folder, name), read_json)
        log = os.path.join(folder, name[:-len(".meta.json")] + ".jsonl")
        if not isinstance(meta, dict) or meta.get("finished") or not os.path.exists(log):
            continue
        if started is None:
            started = claude_started(pane, cache) or 0
        if os.path.getmtime(log) < started - 2:
            continue
        ended, doing = cached(cache, ("state", log), log, subagent_state) or (False, None)
        if ended:
            continue
        task = one_line(meta.get("description") or meta.get("agentType") or "agent")
        found.append((cached(cache, ("launched", log), log, launched_at) or 0, task, doing))
    return [(task, doing) for at, task, doing in sorted(found, key=lambda f: f[0])]


def subagent_tokens(running):
    """$subagents, how many run (◐ 2), and a row for each: ├ its task · what it is doing."""
    n = len(running)
    shown = running if n <= SUBAGENT_ROWS else running[:SUBAGENT_ROWS - 1]
    rows = ["%s · %s" % (task, doing) if doing else task for task, doing in shown]
    if n > len(shown):
        rows.append("%d more" % (n - len(shown)))
    tokens = {"subagents": "◐ %d" % n if n else None}
    for i in range(SUBAGENT_ROWS):
        tokens["subagent_%d" % (i + 1)] = (
            "%s %s" % ("└" if i == len(rows) - 1 else "├", rows[i][:200]) if i < len(rows) else None)
    return tokens


def subagents(agents, cache):
    """Report each Claude Code pane's running sub-agents, every pass: they change while the
    session's own transcript does not."""
    for a in agents:
        if a.get("agent") != "claude":
            continue
        path = session_log(a, cache)
        tokens = subagent_tokens(running_subagents(a["pane_id"], path, cache) if path else [])
        have = a.get("tokens") or {}
        if any(have.get(k) != v for k, v in tokens.items()):
            call("pane.report_metadata", {
                "pane_id": a["pane_id"], "source": SOURCE, "agent": a["agent"], "seq": time.time_ns(),
                "tokens": tokens})


def place(a):
    """The folder the agent runs in, by name. herdr's workspace label is the folder the tab's
    workspace was opened in, which a tab started elsewhere does not change."""
    cwd = (a.get("foreground_cwd") or a.get("cwd") or "").rstrip("/")
    if not cwd:
        return None
    return "~" if cwd == os.path.expanduser("~") else os.path.basename(cwd)


def places(agents):
    """Each agent pane's folder as $place, and as $icon_place after the agent's emoji (🦀 api):
    herdr puts " · " between a row's tokens except after the state dot, so the two are one."""
    for a in agents:
        if a.get("agent") not in READERS:
            continue
        icon, where = ICONS[a["agent"]], place(a)
        tokens = {"place": where, "icon_place": "%s %s" % (icon, where) if where else icon}
        have = a.get("tokens") or {}
        if any(have.get(k) != v for k, v in tokens.items()):
            call("pane.report_metadata", {
                "pane_id": a["pane_id"], "source": SOURCE, "agent": a["agent"], "seq": time.time_ns(),
                "tokens": tokens})


def ticker_lock():
    """The lock the herdr server's one ticker holds, or None while another holds it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tag = hashlib.sha1(SOCK.encode()).hexdigest()[:12]
    f = open(os.path.join(STATE_DIR, "herdr-claude-ticker-%s.lock" % tag), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def tick():
    lock = ticker_lock()
    if not lock:
        return
    me = os.path.abspath(__file__)
    version = os.stat(me).st_mtime_ns
    seen, cache, named, reached = {}, {}, {}, time.time()
    while True:
        time.sleep(TICK)
        try:
            if os.stat(me).st_mtime_ns != version:
                lock.close()
                os.execv(sys.executable, [sys.executable, me, "--tick"])
        except OSError:
            pass
        try:
            agents = call("agent.list", {}).get("agents", [])
            reached = time.time()
            refresh(agents, reached, cache=cache)
            places(agents)
            commands(agents, seen, cache)
            tabs(agents, named)
            subagents(agents, cache)
        except (FileNotFoundError, ConnectionRefusedError):
            if time.time() - reached > GONE:
                return  # the server is gone; the next hook starts a new ticker
        except Exception:
            pass  # busy: the next pass


def start_ticker():
    lock = ticker_lock()
    if not lock:
        return  # running
    lock.close()
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--tick"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextlib.contextmanager
def given_names():
    """The names this gave to tabs, keyed by socket and tab id. The hook and the ticker both
    change them, so under a lock."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(STATE) as f:
                given = json.load(f)
        except (OSError, ValueError):
            given = {}
        before = dict(given)
        yield given
        if given != before:
            with open(STATE + ".tmp", "w") as f:
                json.dump(dict(list(given.items())[-500:]), f)
            os.replace(STATE + ".tmp", STATE)


def claude_title(a):
    """The name in a Claude pane's terminal title, as the sidebar shows it. None while the title
    is not Claude Code's own, such as the shell's as it quits: Claude Code puts its state before
    the name (✳, or a spinner while at work), which herdr strips."""
    raw = (a.get("terminal_title") or "").strip()
    name = (a.get("terminal_title_stripped") or "").strip()
    return name if name and name != raw else None


def codex_title(a):
    """Codex keeps its session title when idle, without the working spinner herdr strips."""
    name = (a.get("terminal_title_stripped") or "").strip()
    # Older herdr versions leave Codex's temporary approval status in the stripped title.
    return re.sub(r"^\[[^]]*\]\s*Action Required\s*\|\s*", "", name).strip() or None


def pi_title(a):
    """pi's terminal title after its "π - ": the session name and the folder, or the folder alone
    before the session has a name. None while the title is not pi's own."""
    raw = (a.get("terminal_title") or "").strip()
    if not raw.startswith("π - "):
        return None
    return raw[len("π - "):].strip() or None


TITLES = {"claude": claude_title, "codex": codex_title, "pi": pi_title}
# Which agent it is, before each tab name and the pane's folder. Two cells in any terminal.
ICONS = {"claude": "🦀", "codex": "🌀", "pi": "🥧"}


def alone(a, agents):
    return sum(b.get("tab_id") == a.get("tab_id") for b in agents) == 1


def name_tab(me, agents, name):
    tab_id = me.get("tab_id")
    if not tab_id or not alone(me, agents):
        return
    name = "%s %s" % (ICONS[me["agent"]], name)
    tab = call("tab.get", {"tab_id": tab_id}).get("tab") or {}
    current = tab.get("label") or ""
    key = "%s %s" % (SOCK, tab_id)
    with given_names() as given:
        if current == name or not (current.isdigit() or given.get(key) == current):
            return
        call("tab.rename", {"tab_id": tab_id, "label": name})
        given[key] = name


def release_tabs(agents):
    """herdr's number back on each tab this named whose supported agent has gone; the number is the
    tab's place in its workspace, which herdr names a new tab by."""
    held = {a.get("tab_id") for a in agents if a.get("agent") in TITLES}
    mine = SOCK + " "
    with given_names() as given:
        for key in [k for k in given if k.startswith(mine) and k[len(mine):] not in held]:
            tab_id = key[len(mine):]
            tab = call("tab.get", {"tab_id": tab_id}).get("tab")
            if tab and tab.get("label") == given[key]:
                tabs = call("tab.list", {"workspace_id": tab["workspace_id"]}).get("tabs") or []
                ids = [t.get("tab_id") for t in tabs]
                if tab_id in ids:
                    call("tab.rename", {"tab_id": tab_id, "label": str(ids.index(tab_id) + 1)})
            del given[key]  # renamed by hand since, or closed: no longer this one's


def tabs(agents, named):
    """Keep each Claude, Codex or pi tab named as its title. Look again when its title, its
    tab or the agents beside it change."""
    supported = [a for a in agents if a.get("agent") in TITLES]
    for pane in set(named) - {a["pane_id"] for a in supported}:
        del named[pane]  # gone: a new agent there, even with the same session, renames
    for a in supported:
        name = TITLES[a["agent"]](a)
        if a["agent"] in ("codex", "pi") and (a.get("tokens") or {}).get("session_title") != name:
            call("pane.report_metadata", {
                "pane_id": a["pane_id"], "source": SOURCE, "agent": a["agent"], "seq": time.time_ns(),
                "tokens": {"session_title": name}})
        seen = (a.get("tab_id"), name, alone(a, agents))
        if name and named.get(a["pane_id"]) != seen:
            name_tab(a, agents, name)
            named[a["pane_id"]] = seen
    release_tabs(agents)


def main():
    if os.environ.get("HERDR_ENV") != "1" or not SOCK or not PANE:
        return
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return
    if hook.get("agent_id"):  # a subagent's
        return
    now = time.time()
    path = hook.get("transcript_path")
    if path and not os.path.exists(path):
        path = None
    at = now
    if hook.get("hook_event_name") == "SessionStart" and hook.get("source") == "resume" and path:
        at = last_message_at(path) or now  # resumed, perhaps by herdr's own restart
    report(PANE, at, now)

    agents = call("agent.list", {}).get("agents", [])
    sid = hook.get("session_id")
    me = (next((a for a in agents if (a.get("agent_session") or {}).get("value") == sid), None)
          or next((a for a in agents if a.get("pane_id") == PANE), None))
    refresh(agents, now, skip=me)
    start_ticker()

    name = me and me.get("agent") == "claude" and claude_title(me)
    if name:
        name_tab(me, agents, name)


if __name__ == "__main__":
    try:
        tick() if sys.argv[1:] == ["--tick"] else main()
    except Exception:
        pass  # herdr gone or busy: the sidebar waits for the next event
