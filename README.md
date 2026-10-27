# Session watcher for Claude Code

Claude Code sessions often end without a hand-over: you close the tab, go to lunch,
switch tasks, and the context lives on only in a transcript nobody reads.
Session watcher notices such sessions and takes a short, dense **context dump**
of each one: goal, what was done, where it stopped, decisions, next step, key files.
The next session (yours or the model's) can pick up from there.

- **Automatic.** A `SessionStart` hook starts a small background watcher per session.
- **Cheap.** The transcript is shrunk locally to a digest of at most ~18k characters.
  One `claude -p` call on Haiku then turns it into the dump, with no tools, no MCP,
  no settings and a short system prompt. That costs about **$0.002–0.005** and
  3–9k input tokens per dump.
- **Respects your own rituals.** Commands you name (say `/handoff` or `/mv-close`)
  take the session off watch, and any dump already taken for it is removed.
- **Findable.** Dumps are plain Markdown with an `INDEX.md` per project. A new session
  in the same project gets one line pointing at the latest dump. That costs about 60 tokens,
  and the model reads the dump only if you ask it to continue.
- **No dependencies.** It is a single Python 3.7+ file and uses the `claude` CLI you
  already have, with your usual login.

## How it works

```
SessionStart hook ──► watcher (detached, one per session)
                        │ reads new transcript lines every minute
                        ├─ excluded command seen ─► stop, delete its dump
                        ├─ SDK / `claude -p` session ─► stop
                        └─ transcript idle ≥ 50 min
                              ├─ digest (local): turns, replies, edits, commits, todos
                              ├─ claude -p --model haiku --tools "" ...  ─► dump
                              └─ ~/.claude/session-dumps/<project>/<date>_<id>.md + INDEX.md
```

If the session continues after a dump, the dump is refreshed after the next idle
period. After 12 h of idleness the watcher exits. Resuming the session
(`claude --resume`) starts a new watcher.

A dump looks like this:

```markdown
# Fix CSV parser crash on quoted newlines

- Session: `3f2a…` (resume: `claude --resume 3f2a…`)
- Project: `/work/app`, branch: `fix/csv`, cwd: `/work/app`
- Period: 2026-10-27 10:02 to 2026-10-27 11:40, user turns: 9
- Dumped: 2026-10-27 12:31 after 51 min idle

## Goal
## Done
## State when stopped
## Decisions and facts
## Open / next step
## Key files
## Working tree at dump time
```

## Install

```bash
git clone https://github.com/GoodJaa/Session-watcher-agent.git
cd Session-watcher-agent
python3 install.py                 # every project: hook goes to ~/.claude/settings.json
```

Common variants:

```bash
python3 install.py --exclude handoff --exclude mv-     # these commands end watching
python3 install.py --dumps project                     # dumps in <project>/.claude/session-dumps/
python3 install.py --settings ~/work/app/.claude/settings.local.json   # one project only
python3 install.py --uninstall                         # remove the hook, keep dumps
```

The installer copies `session_watcher.py` to `~/.claude/session-watcher/` and creates
`config.json` there. It backs up the settings file before adding the hook, and running
it again replaces the hook instead of adding a second one. Only sessions started after
installation are watched. If you run with `CLAUDE_CONFIG_DIR`, everything goes there
instead of `~/.claude`.

## Configuration

`~/.claude/session-watcher/config.json`. Every key is optional and can also be set as an
environment variable `SESSION_WATCHER_<KEY>`, for example `SESSION_WATCHER_IDLE_MINUTES=30`.

| Key | Default | Meaning |
|---|---|---|
| `idle_minutes` | `50` | Inactivity before a dump |
| `model` | `haiku` | Model for the dump (`--model` value) |
| `exclude_commands` | `[]` | Name prefixes of slash commands or skills that take a session off watch (`"mv-"` covers `/mv-close`, `/mv-resume`, `Skill mv-note`, `/plugin:mv-close`) |
| `dumps_dir` | `~/.claude/session-dumps` | Root for dumps, one folder per project; `"project"` writes to `<project>/.claude/session-dumps/` and adds it to `.git/info/exclude` |
| `announce` | `true` | Tell a new session in the project about the latest dump |
| `announce_days` | `7` | Only announce dumps newer than this |
| `language` | `auto` | Language of the dump; `auto` follows the session |
| `max_words` | `350` | Upper bound on dump length |
| `max_digest_chars` | `18000` | Upper bound on what goes into the model |
| `keep_days` | `60` | Older dumps are deleted |
| `projects` | `[]` | Only watch sessions under these directories (empty means all) |
| `skip_entrypoints` | `sdk-cli`, `sdk-ts`, `sdk-py`, `sdk` | Non-interactive sessions that are never watched |
| `give_up_hours` | `12` | Watcher exits after this much idleness |
| `claude_bin` | auto | Path to `claude`, if auto-detection fails |

The `claude` binary is found as the process that runs the hook, then on `PATH`,
then in `~/.local/bin` and `~/.claude/local`, then in the VS Code, Cursor or Windsurf extension folders.

## Commands

```bash
W=~/.claude/session-watcher/session_watcher.py
python3 $W status                              # watched sessions and their state
python3 $W dump --transcript <file.jsonl>      # dump a session now
python3 $W dump --transcript <file.jsonl> --dry-run   # only print the digest that would be sent
tail ~/.claude/session-watcher/watch.log       # what happened, with token usage per dump
```

Transcripts live in `~/.claude/projects/<project-slug>/<session-id>.jsonl`.

## Privacy and cost

Each dump sends a digest of the transcript to the model through your own `claude` login.
This is the provider the session already talked to. Nothing goes anywhere else.
The digest leaves out tool output, thinking and system reminders. It keeps your messages,
the assistant's replies and short lines for actions, such as file paths, command
descriptions, commit markers and the todo list. The log records tokens and cost for every dump.

## Limitations

- Linux and macOS only, because it relies on `fcntl` and detached processes. On Windows,
  use WSL.
- Idleness is measured by the transcript's modification time. A single command that runs
  silently for more than `idle_minutes` counts as idle.
- Sessions that are already running when you install are not watched. Their next start or
  resume is.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests run on synthetic transcripts with a fake `claude` binary, so they need no
network and spend no tokens.

## License

MIT
