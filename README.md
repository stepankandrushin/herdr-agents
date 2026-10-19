# herdr-agents

How I run Claude Code, Codex and pi side by side in [herdr](https://herdr.dev): my herdr config,
and a small program that adds to herdr's agents sidebar, for each agent pane, the folder it runs
in, how long ago it last did something, how much context its session holds, and the last shell
command it ran with the last line of that command's output. Claude Code and Codex tabs are named
after their sessions.

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

Two files do it:

- `config.toml`, my herdr config: the sidebar rows that show these values, and the rest of what
  I set ([The config](#the-config)).
- `herdr-agents.py`, one Python file, standard library only. It runs as a Claude Code hook, and
  the hook starts a small background process (the ticker) that keeps every pane's figures
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
   git clone https://github.com/stepankandrushin/herdr-agents.git ~/herdr-agents
   ```

2. Register the hook in `~/.claude/settings.json`, on four events. It is async, so a prompt
   never waits on herdr, and it prints nothing:

   ```json
   {
     "hooks": {
       "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 ~/herdr-agents/herdr-agents.py", "timeout": 5, "async": true}]}],
       "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/herdr-agents/herdr-agents.py", "timeout": 5, "async": true}]}],
       "Stop":             [{"hooks": [{"type": "command", "command": "python3 ~/herdr-agents/herdr-agents.py", "timeout": 5, "async": true}]}],
       "StopFailure":      [{"hooks": [{"type": "command", "command": "python3 ~/herdr-agents/herdr-agents.py", "timeout": 5, "async": true}]}]
     }
   }
   ```

   Merge these into the hooks you already have rather than replacing them.

3. Take the config. Either use mine whole, as a link in place of yours, so that a `git pull`
   updates it too:

   ```sh
   ln -s ~/herdr-agents/config.toml ~/.config/herdr/config.toml
   ```

   or merge into your `~/.config/herdr/config.toml` the parts of [config.toml](config.toml) you
   want; the `[ui.sidebar.agents.rows_by_agent]` table is the one that shows the values. Then
   reload it: `herdr server reload-config`.

4. Start Claude Code in a herdr pane. Its first hook starts the ticker, which fills in every
   agent pane of that herdr server within two seconds. Claude Code reads its hooks when a
   session starts, so sessions that were already running take the hook once restarted; until
   then the ticker keeps their rows current anyway.

To update, `git pull`: the ticker notices that its file changed and restarts itself. A changed
config takes effect at the next `herdr server reload-config`.

## The config

[config.toml](config.toml), with a comment on each setting:

- **The agents sidebar** (`[ui.sidebar.agents.rows_by_agent]`): three rows for each agent pane.
  Claude Code and Codex panes show their folder, age and context on the first row, the session's
  name on the second, and the last command with its last line of output on the third. pi
  panes keep herdr's default first two rows (the workspace and the tab, then the agent's
  name), with the folder in place of the workspace, and get the context and the last command
  too. The values are in [What it shows](#what-it-shows).
- **Tabs** (`prompt_new_tab_name = false`): a new tab opens at once with herdr's number instead
  of asking for a name. Claude Code and Codex tabs then take their session names by themselves
  ([Tab names](#tab-names)); other tabs keep their numbers until you rename them.
- **The outer terminal's title** (`window_title`): the host, the workspace and the tab, so the
  terminal's window or tab list shows which session is in front, and on which machine.
- `onboarding = false` is herdr's own first-run marker: a new machine skips the tour.

## What it shows

Each value is a pane token, which herdr's sidebar rows name with a `$`:

| Token | Agents | What it is |
|---|---|---|
| `$place` | Claude Code, Codex, pi | The name of the folder the agent runs in (`~` for the home folder). herdr's own `workspace` names the folder the tab's workspace was opened in, which is wrong for a tab started elsewhere. |
| `$session_title` | Codex | Its session name from the terminal title, with the working spinner and temporary `Action Required` prefix removed. Used for the sidebar and automatic tab name. |
| `$last_active` | Claude Code, Codex | How long ago the session last did something: `now`, `7m`, `5h`, `3d`. It reads `now` for as long as herdr sees the agent at work. A resumed session shows the time of its last message, so herdr restoring its panes does not make them all look active. Codex's time comes from its current rollout, including turn completion and command events; a fresh session with no log has no age yet. The time itself is in `$last_active_at` (Unix seconds). |
| `$context` | Claude Code, Codex, pi | The context the session holds, to the thousand: `5k`, `120k`. It is the tokens of the last model call, input and reply, as each agent counts them for its own gauge: Claude Code's usage with the cache, Codex's `last_token_usage.total_tokens`, pi's `totalTokens`. Empty before the first reply, and after a compaction until the next reply, except in Claude Code, whose compaction records what it left. |
| `$last_cmd` | Claude Code, Codex, pi | The last shell command, `$ ` and its first line, without a leading `cd <dir> &&`. |
| `$last_out` | Claude Code, Codex, pi | The last line of that command's output that has a letter or digit in it. `…` while the command runs, `✗ <line>` if it failed, `✓` if it printed nothing. For a Claude Code background command or an output too long for the transcript, it is read from the file the output went to, so a long job shows its current line. |

When a row is too long for the sidebar, herdr shortens the folder before the age and the
context, which stay whole.

### Tab names

Claude Code and Codex tabs take the names their sidebar rows show: the session names in their
terminal titles, including names set with `/rename`. Codex's temporary `Action Required` prefix
is removed. Claude Code starts with "Claude Code";
Codex may show its folder until it generates a session title. A new session in the same tab
renames it again. When neither supported agent remains in the tab, it gets herdr's number back.

It renames only a tab that holds no other agent and whose label is still herdr's generated
number or the name herdr-agents gave it. A tab you renamed by hand keeps its name.

## How it works

- **The hook** runs on SessionStart, UserPromptSubmit, Stop and StopFailure. It reports the
  pane's activity time, names its tab, and starts the ticker if none is running for this herdr
  server. Outside herdr it exits at once.
- **The ticker** (`herdr-agents.py --tick`) runs one per herdr server, guarded by a lock. Every
  two seconds it asks herdr for its agent panes, rereads only the session logs that have grown,
  moves the ages on, and reports the tokens that changed. It reads each agent's own log:
  Claude Code's transcript (`~/.claude/projects/*/<session>.jsonl`, by the session id herdr's
  integration reports), the Codex rollout file the `codex` process holds open (found in
  `/proc/<pid>/fd`, or with `lsof` on macOS, there at most once in 30 seconds), and the pi
  session file herdr's pi integration reports. It reads the last megabyte of a log only, about
  a millisecond even for a log of tens of megabytes.
- It exits two minutes after the herdr server has gone; the next hook starts a new one. It
  restarts itself when `herdr-agents.py` changes.
- It talks to herdr over its socket (`HERDR_SOCKET_PATH`), the same way herdr's own integrations
  do, and reports under the metadata source `herdr-agents`.

The ticker is started only by a Claude Code hook, so a herdr server with no Claude Code pane
shows no figures for its Codex and pi panes.

Files it writes, in `$XDG_STATE_HOME` (default `~/.local/state`):

- `herdr-claude-tabs.json`: the names it gave to Claude Code and Codex tabs, so it can tell them
  from names set by hand. The filename is kept for existing installations.
- `herdr-claude-ticker-<hash>.lock`: one per herdr server, held by that server's ticker.

It honours `CLAUDE_CONFIG_DIR` for where Claude Code keeps its transcripts.

## Uninstall

Remove the four hooks from `~/.claude/settings.json` and the config (the link, or the rows you
merged), then stop the ticker: `pkill -f 'herdr-agents.py --tick'`.

## Development

```sh
python3 -m unittest discover -s tests
```

The tests build made-up session logs in a temporary folder and never touch herdr.

## License

MIT, see [LICENSE](LICENSE).
