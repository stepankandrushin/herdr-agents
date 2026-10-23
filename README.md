# herdr-agents

How I run Claude Code, Codex and pi side by side in [herdr](https://herdr.dev): my herdr config,
and a small program that adds to herdr's agents sidebar, for each agent pane, the folder it runs
in, how long ago it last did something, how much context its session holds, and the start of
its last message to you (or the last shell command it ran, with the last line of that command's
output); for Claude Code also the sub-agents still running in it, each with its task and what
it is doing. Claude Code, Codex and pi tabs are
named after their sessions, with the agent's emoji first: `🦀 Fix docs`, `🌀 Fix login | api`,
`🥧 Fix tests - api`.

```
● 🦀 api · now · 57k
  Fix the flaky login test
  The redirect test fails only when the session cookie is older than the token, so I'm …
○ 🦀 docs · 2m · 92k · ◐ 2
  Release notes for 2.4
  Both agents are running; I'll report when they finish.
  ├ Check the changelog links · Fetching docs.example.com
  └ Draft the upgrade guide · Editing upgrade.md
○ 🌀 docs · 3h · 12k
  Changelog for 2.4
  Committed the 2.4 changelog on main; the release notes link to it.
```

With a dozen agents open, herdr shows that each one exists and whether it is working. This adds
what you would otherwise switch tabs to find out: which project it is in, whether it finished a
minute ago or yesterday, whether its context is close to full, and what it is doing right now.
A Claude Code session whose turn has ended shows as done or idle while the sub-agents it started
in the background work on; `◐ 2` and a row for each say that it is still at work, and on what.

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
  Each pane shows the agent's emoji (🦀 Claude Code, 🌀 Codex, 🥧 pi) with its folder, then
  its age and context, on the first row, the session's name on the second, and the start of its
  last message to you on the third. pi has no age. A Claude Code pane with sub-agents running
  adds their number to the first row (`◐ 2`, yellow like herdr's working dot) and a row
  for each, up to three; herdr leaves out a row with nothing in it, so the rows are there only
  while sub-agents run. The values are in
  [What it shows](#what-it-shows). A comment there gives the third row that shows the last
  command and its output instead.
- **Tabs** (`prompt_new_tab_name = false`): a new tab opens at once with herdr's number instead
  of asking for a name. Claude Code, Codex and pi tabs then take their session names by themselves
  ([Tab names](#tab-names)); other tabs keep their numbers until you rename them.
- **The outer terminal's title** (`window_title`): the host, the workspace and the tab, so the
  terminal's window or tab list shows which session is in front, and on which machine.
- `onboarding = false` is herdr's own first-run marker: a new machine skips the tour.

## What it shows

Each value is a pane token, which herdr's sidebar rows name with a `$`:

| Token | Agents | What it is |
|---|---|---|
| `$place` | Claude Code, Codex, pi | The name of the folder the agent runs in (`~` for the home folder). herdr's own `workspace` names the folder the tab's workspace was opened in, which is wrong for a tab started elsewhere. |
| `$icon_place` | Claude Code, Codex, pi | The agent's emoji and `$place`: `🦀 api`, `🌀 docs`, `🥧 api`. One token, because herdr puts ` · ` between a row's tokens everywhere but after the state dot, so it reads `● 🦀 api`. |
| `$session_title` | Codex, pi | Its session name from the terminal title: Codex's with the working spinner and temporary `Action Required` prefix removed, pi's without its `π - `. Used for the sidebar and automatic tab name. |
| `$last_active` | Claude Code, Codex | How long ago the session last did something: `now`, `7m`, `5h`, `3d`. It reads `now` for as long as herdr sees the agent at work. A resumed session shows the time of its last message, so herdr restoring its panes does not make them all look active. Codex's time comes from its current rollout, including turn completion and command events; a fresh session with no log has no age yet. The time itself is in `$last_active_at` (Unix seconds). |
| `$context` | Claude Code, Codex, pi | The context the session holds, to the thousand: `5k`, `120k`. It is the tokens of the last model call, input and reply, as each agent counts them for its own gauge: Claude Code's usage with the cache, Codex's `last_token_usage.total_tokens`, pi's `totalTokens`. Empty before the first reply, and after a compaction until the next reply, except in Claude Code, whose compaction records what it left. |
| `$last_message` | Claude Code, Codex, pi | The start of the agent's last message to you, on one line: the text it wrote last, between tool calls or at the end of its turn, with markdown's marks (`**`, backticks, headings, list bullets, link targets) removed. herdr cuts it to the sidebar's width. For pi, a turn that ended without a reply shows pi's reason: `Operation aborted`, `Request timed out.` Empty before the first message. |
| `$subagents` | Claude Code | How many sub-agents run in the session, `◐ 2`; empty when none. A sub-agent runs from its launch until Claude Code marks it finished (done, failed or stopped), and again while a message wakes it. The session's own state dot shows done or idle once its turn has ended, though its background sub-agents work on. |
| `$subagent_1` … `$subagent_3` | Claude Code | A row for each running sub-agent, the first launched first: `├`, its task as the session described it, and what it is doing now, its newest tool call in words (`Editing auth.py`, `Searching TODO`, a command's description) or the text it wrote since: `├ Fix the login test · Editing auth.py`. The last one starts with `└`. With more than three running, the third reads `└ 2 more`. On a narrow sidebar herdr cuts the row after the task. |
| `$last_cmd` | Claude Code, Codex, pi | The last shell command, `$ ` and its first line, without a leading `cd <dir> &&`. |
| `$last_out` | Claude Code, Codex, pi | The last line of that command's output that has a letter or digit in it. `…` while the command runs, `✗ <line>` if it failed, `✓` if it printed nothing. For a Claude Code background command or an output too long for the transcript, it is read from the file the output went to, so a long job shows its current line. |

When a row is too long for the sidebar, herdr shortens the folder before the age and the
context, which stay whole.

### Tab names

Claude Code, Codex and pi tabs take the session names in their terminal titles, including names
set with `/rename`, after an emoji for the agent: 🦀 Claude Code, 🌀 Codex, 🥧 pi: the name
their sidebar rows show. Codex's temporary `Action Required` prefix
is removed, and so is pi's `π - `. Claude Code starts with "Claude Code";
Codex may show its folder until it generates a session title; pi shows its folder after the
session name, or alone until the session has one. A new session in the same tab renames it
again. When none of these agents remains in the tab, it gets herdr's number back.

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
  session file herdr's pi integration reports. In Codex's default mode the `codex` process
  holds none: its app-server daemon, which every `codex` started without `--no-daemon` shares,
  writes them all (`~/.codex/app-server-daemon/daemon.pid` names it). The pane's own is then the
  one whose session name, from `~/.codex/session_index.jsonl`, the pane's terminal title shows,
  so a session shows its log from when Codex names it, a few seconds into its first turn; a
  terminal title set with `/title` to leave the name out shows none. It reads the last megabyte of a log only, about
  a millisecond even for a log of tens of megabytes.
- For a Claude Code session's sub-agents it reads the folder beside the transcript,
  `<session>/subagents/`: each sub-agent's `agent-<id>.meta.json` (its task, and the `finished`
  mark Claude Code sets when it ends) and its own log `agent-<id>.jsonl`, the end of it, for what
  it is doing. A sub-agent without the mark whose log has not grown since the pane's Claude Code
  started was left by a run that quit or crashed, and does not count; the start comes from
  `ps -o etime=` on the process herdr reports. Claude Code versions without the mark: a
  sub-agent counts until its log ends with a finished reply.
- It exits two minutes after the herdr server has gone; the next hook starts a new one. It
  restarts itself when `herdr-agents.py` changes.
- It talks to herdr over its socket (`HERDR_SOCKET_PATH`), the same way herdr's own integrations
  do, and reports under the metadata source `herdr-agents`.

The ticker is started only by a Claude Code hook, so a herdr server with no Claude Code pane
shows no figures for its Codex and pi panes.

Files it writes, in `$XDG_STATE_HOME` (default `~/.local/state`):

- `herdr-claude-tabs.json`: the names it gave to Claude Code, Codex and pi tabs, so it can tell them
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
