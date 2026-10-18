# herdr-sidebar

More in [herdr](https://herdr.dev)'s agents sidebar for Claude Code, Codex and pi panes: the
folder each agent runs in, how long ago it last did something, how much context its session
holds, and the last shell command it ran with the last line of that command's output. Claude
Code tabs are named after their sessions.

```
● api · now · 57k
  Fix the flaky login test
  $ pytest -q tests/test_login.py · ✗ FAILED tests/test_login.py::test_redirect
○ docs · 3h · 12k
  Changelog for 2.4
  $ git status --short · ✓
```

With a dozen agents open, herdr shows that each one exists and whether it is working. This adds
what you would otherwise switch tabs to find out: which project it is in, whether it finished a
minute ago or yesterday, whether its context is close to full, and what it is doing right now.

It is one Python file, `herdr-sidebar.py`, standard library only. It runs as a Claude Code hook,
and the hook starts a small background process (the ticker) that keeps every pane's figures
current, Codex and pi panes included.

## Requirements

- herdr, with its Claude Code integration (`herdr integration install claude`), and its pi
  integration for pi panes (`herdr integration install pi`). Codex needs none: its log is found
  from its process. Used with herdr 0.9.3 to 0.9.5.
- Python 3.9 or newer (the system Python of macOS is enough).
- Linux or macOS.

## Install

1. Clone the repository anywhere:

   ```sh
   git clone https://github.com/stepankandrushin/herdr-sidebar.git ~/herdr-sidebar
   ```

2. Register the hook in `~/.claude/settings.json`, on four events. It is async, so a prompt
   never waits on herdr, and it prints nothing:

   ```json
   {
     "hooks": {
       "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 ~/herdr-sidebar/herdr-sidebar.py", "timeout": 5, "async": true}]}],
       "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/herdr-sidebar/herdr-sidebar.py", "timeout": 5, "async": true}]}],
       "Stop":             [{"hooks": [{"type": "command", "command": "python3 ~/herdr-sidebar/herdr-sidebar.py", "timeout": 5, "async": true}]}],
       "StopFailure":      [{"hooks": [{"type": "command", "command": "python3 ~/herdr-sidebar/herdr-sidebar.py", "timeout": 5, "async": true}]}]
     }
   }
   ```

   Merge these into the hooks you already have rather than replacing them.

3. Add the sidebar rows from [config.example.toml](config.example.toml) to
   `~/.config/herdr/config.toml` and reload it: `herdr server reload-config`.

4. Start Claude Code in a herdr pane. Its first hook starts the ticker, which fills in every
   agent pane of that herdr server within two seconds. Claude Code reads its hooks when a
   session starts, so sessions that were already running take the hook once restarted; until
   then the ticker keeps their rows current anyway.

To update, `git pull`: the ticker notices that its file changed and restarts itself.

## What it shows

Each value is a pane token, which herdr's sidebar rows name with a `$`:

| Token | Agents | What it is |
|---|---|---|
| `$place` | Claude Code, Codex, pi | The name of the folder the agent runs in (`~` for the home folder). herdr's own `workspace` names the folder the tab's workspace was opened in, which is wrong for a tab started elsewhere. |
| `$last_active` | Claude Code | How long ago the session last did something: `now`, `7m`, `5h`, `3d`. It reads `now` for as long as herdr sees the agent at work. A resumed session shows the time of its last message, so herdr restoring its panes does not make them all look active. The time itself is in `$last_active_at` (Unix seconds). |
| `$context` | Claude Code, Codex, pi | The context the session holds, to the thousand: `5k`, `120k`. It is the tokens of the last model call, input and reply, as each agent counts them for its own gauge: Claude Code's usage with the cache, Codex's `last_token_usage.total_tokens`, pi's `totalTokens`. Empty before the first reply, and after a compaction until the next reply, except in Claude Code, whose compaction records what it left. |
| `$last_cmd` | Claude Code, Codex, pi | The last shell command, `$ ` and its first line, without a leading `cd <dir> &&`. |
| `$last_out` | Claude Code, Codex, pi | The last line of that command's output that has a letter or digit in it. `…` while the command runs, `✗ <line>` if it failed, `✓` if it printed nothing. For a Claude Code background command or an output too long for the transcript, it is read from the file the output went to, so a long job shows its current line. |

When a row is too long for the sidebar, herdr shortens the folder before the age and the
context, which stay whole.

### Tab names

A Claude Code pane's tab takes the name its sidebar row shows: the session name in Claude
Code's terminal title, "Claude Code" until the session has one, then the title Claude Code gives
it or the one set with `/rename`. A new session in the same tab renames it again. When Claude
Code leaves the tab (it quits, or a shell or another agent takes its place), the tab gets herdr's
number back.

It renames only a tab that holds no other agent and whose label is still herdr's generated
number or the name herdr-sidebar gave it. A tab you renamed by hand keeps its name.

## How it works

- **The hook** runs on SessionStart, UserPromptSubmit, Stop and StopFailure. It reports the
  pane's activity time, names its tab, and starts the ticker if none is running for this herdr
  server. Outside herdr it exits at once.
- **The ticker** (`herdr-sidebar.py --tick`) runs one per herdr server, guarded by a lock. Every
  two seconds it asks herdr for its agent panes, rereads only the session logs that have grown,
  moves the ages on, and reports the tokens that changed. It reads each agent's own log:
  Claude Code's transcript (`~/.claude/projects/*/<session>.jsonl`, by the session id herdr's
  integration reports), the Codex rollout file the `codex` process holds open (found in
  `/proc/<pid>/fd`, or with `lsof` on macOS, there at most once in 30 seconds), and the pi
  session file herdr's pi integration reports. It reads the last megabyte of a log only, about
  a millisecond even for a log of tens of megabytes.
- It exits two minutes after the herdr server has gone; the next hook starts a new one. It
  restarts itself when `herdr-sidebar.py` changes.
- It talks to herdr over its socket (`HERDR_SOCKET_PATH`), the same way herdr's own integrations
  do, and reports under the metadata source `herdr-sidebar`.

The ticker is started only by a Claude Code hook, so a herdr server with no Claude Code pane
shows no figures for its Codex and pi panes.

Files it writes, in `$XDG_STATE_HOME` (default `~/.local/state`):

- `herdr-claude-tabs.json`: the names it gave to tabs, so it can tell them from names set by hand.
- `herdr-claude-ticker-<hash>.lock`: one per herdr server, held by that server's ticker.

It honours `CLAUDE_CONFIG_DIR` for where Claude Code keeps its transcripts.

## Uninstall

Remove the four hooks from `~/.claude/settings.json` and the rows from herdr's config, then stop
the ticker: `pkill -f 'herdr-sidebar.py --tick'`.

## Development

```sh
python3 -m unittest discover -s tests
```

The tests build made-up session logs in a temporary folder and never touch herdr.

## License

MIT, see [LICENSE](LICENSE).
