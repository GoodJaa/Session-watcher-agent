#!/usr/bin/env python3
"""Session watcher for Claude Code: a cheap context dump of abandoned sessions.

A SessionStart hook spawns one detached watcher per session. The watcher
follows the session transcript (~/.claude/projects/<slug>/<session>.jsonl):

- the session ran an excluded command (config `exclude_commands`, matched as a
  name prefix of a slash command or a Skill call) -- watching stops and a dump
  already taken is removed: the session has its own closing ritual;
- the transcript has not changed for `idle_minutes` -- the transcript is shrunk
  into a digest locally (no LLM), the digest goes through a single `claude -p`
  call on a cheap model with no tools, no MCP, no settings and a short system
  prompt; the result is a tagged dump <dumps>/<project>/<date>_<sid8>.md with
  status `new`, listed in INDEX.md next to it;
- the session continues after a dump -- the next idle period refreshes it;
- idle for longer than `give_up_hours` -- the watcher exits.

A dump never enters a session by itself: `load` (the /dump-load command) does
it on request and marks the dump `used`; `clean` deletes used dumps.

Subcommands:
  hook                              SessionStart hook (reads the hook JSON on stdin)
  watch --session ID --transcript PATH [--claude BIN]
  list [all]                        dumps of the current project (or of all projects)
  load REF                          print a dump by tag or session id, mark it used
  clean [all]                       delete used dumps
  dump --transcript PATH [--dry-run]    dump now (--dry-run prints the digest only)
  digest --transcript PATH          print the digest
  status                            watched sessions
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

WINDOWS = os.name == "nt"
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
HOME = os.environ.get("SESSION_WATCHER_HOME") or os.path.join(CLAUDE_DIR, "session-watcher")
STATE_DIR = os.path.join(HOME, "state")
LOG = os.path.join(HOME, "watch.log")
CONFIG_FILE = os.path.join(HOME, "config.json")
# cwd of `claude -p`: outside any project, so no CLAUDE.md or project hooks load
RUN_DIR = os.path.join(HOME, "run")
CHILD_ENV = "SESSION_WATCHER_CHILD"
UTF8 = {"encoding": "utf-8", "errors": "replace"}

DEFAULTS = {
    "idle_minutes": 50,
    "model": "haiku",
    "exclude_commands": [],
    "dumps_dir": os.path.join(CLAUDE_DIR, "session-dumps"),
    "language": "auto",
    "max_words": 350,
    "max_digest_chars": 18000,
    "keep_days": 60,
    "give_up_hours": 12,
    "poll_seconds": 60,
    "llm_timeout_seconds": 240,
    "projects": [],
    "skip_entrypoints": ["sdk-cli", "sdk-ts", "sdk-py", "sdk"],
    "claude_bin": "",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    for key, default in DEFAULTS.items():
        env = os.environ.get("SESSION_WATCHER_" + key.upper())
        if env is None:
            continue
        if isinstance(default, bool):
            cfg[key] = env.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, (int, float)):
            cfg[key] = float(env)
        elif isinstance(default, list):
            cfg[key] = [x.strip() for x in env.split(",") if x.strip()]
        else:
            cfg[key] = env
    return cfg


CFG = load_config()

SYSTEM_PROMPT = (
    "You write a context dump of an interrupted session of an AI coding assistant, "
    "so that the next session can pick the work up without losses. Be terse and "
    "concrete: file paths, ticket keys, function names, commands, decisions. "
    "Use only what is in the digest; never invent."
)

USER_PROMPT = """Below is a digest of a session transcript (user turns `U:`, assistant replies `A:`, actions `T:`).
Write the dump strictly in the format below, at most {words} words, no preamble. Keep the `Title:` and `Tag:` labels and the `##` headings exactly as given, in English. {language}

Title: <up to 10 words>
Tag: <2-4 lowercase latin words joined by hyphens, naming the work, e.g. fix-csv-parser>

## Goal
<1-2 sentences: what the user wanted>

## Done
- <one item per result; files, commits>

## State when stopped
<where it stopped, what was in progress, what is unverified or uncommitted, whether the assistant was waiting for an answer>

## Decisions and facts
- <established facts and decisions that must not be lost>

## Open / next step
- <unanswered questions and the first step to continue>

## Key files
- <path - why it matters>

--- DIGEST ---
"""


def log(msg):
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s [%d] %s\n" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
    except OSError:
        pass


# ---------- platform ----------

def try_lock(f):
    """Non-blocking exclusive lock held for the life of the file object."""
    try:
        if WINDOWS:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if WINDOWS:  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def spawn(args):
    """Start the watcher detached from Claude Code, so it outlives the session process."""
    os.makedirs(HOME, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as out:
        kw = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True, cwd=HOME)
        if not WINDOWS:
            return subprocess.Popen(args, start_new_session=True, **kw)
        flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:  # leave Claude Code's job object, or the watcher dies with the session
            return subprocess.Popen(args, creationflags=flags | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            return subprocess.Popen(args, creationflags=flags, **kw)


def parent_of(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except OSError:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
        return int(out.stdout.strip() or 0)


def exe_of(pid):
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except OSError:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True)
        return out.stdout.strip()


def claude_ancestor():
    """Path of the claude binary that runs this hook, if it is an ancestor."""
    if WINDOWS:
        return None  # CLAUDE_CODE_EXECPATH and PATH cover it
    pid = os.getppid()
    try:
        for _ in range(8):
            if pid <= 1:
                return None
            exe = exe_of(pid)
            if os.path.basename(exe) == "claude" and os.path.isfile(exe):
                return exe
            pid = parent_of(pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def find_claude(hint=None):
    names = ("claude.exe", "claude.cmd", "claude") if WINDOWS else ("claude",)
    cands = [hint, CFG["claude_bin"], os.environ.get("CLAUDE_CODE_EXECPATH"), shutil.which("claude")]
    for d in (os.path.join(CLAUDE_DIR, "local"), os.path.expanduser("~/.local/bin"),
              "/usr/local/bin", "/opt/homebrew/bin"):
        cands += [os.path.join(d, n) for n in names]
    ext = []
    for d in ("~/.vscode", "~/.vscode-server", "~/.vscode-insiders", "~/.cursor",
              "~/.cursor-server", "~/.windsurf", "~/.windsurf-server"):
        for n in names:
            ext += glob.glob(os.path.expanduser(
                d + "/extensions/anthropic.claude-code-*/resources/native-binary/" + n))
    ext.sort(key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"[.-]", p)], reverse=True)
    return next((c for c in cands + ext if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


# ---------- transcript ----------

def iter_records(path, offset=0):
    """(record, end_offset) for complete lines starting at offset."""
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            try:
                yield json.loads(raw.decode("utf-8", "replace")), offset
            except ValueError:
                yield None, offset


def user_text(rec):
    """Text of a human turn, or None (tool results, meta records, ...)."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                return None
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts) if parts else None
    return None


COMMAND_TAG = re.compile(r"<command-name>/?([\w:.-]+)</command-name>")
TYPED_COMMAND = re.compile(r"^\s*/([\w:.-]+)")


def _excluded(name, prefixes):
    name = name.split(":")[-1]  # plugin:skill
    return any(name.startswith(p.lstrip("/")) for p in prefixes)


def exclusion_marker(rec, prefixes):
    """Name of the excluded command the record starts, if any."""
    if not rec or not prefixes or rec.get("isSidechain"):
        return None
    text = user_text(rec)
    if text is not None:
        m = COMMAND_TAG.search(text) or TYPED_COMMAND.match(text)
        if m and _excluded(m.group(1), prefixes):
            return "/" + m.group(1)
    if rec.get("type") == "assistant":
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                skill = str((b.get("input") or {}).get("skill", ""))
                if skill and _excluded(skill, prefixes):
                    return "Skill " + skill
    return None


def clean_user(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<(ide_[\w]+|local-command-[\w]+)>.*?</\1>", "", text, flags=re.S)
    m = re.search(r"<command-name>(.*?)</command-name>", text, flags=re.S)
    if m:
        args = re.search(r"<command-args>(.*?)</command-args>", text, flags=re.S)
        text = (m.group(1) + " " + (args.group(1) if args else "")).strip()
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def short(s, n):
    s = re.sub(r"[ \t]+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def rel(path, cwd):
    if not cwd or not isinstance(path, str) or not path:
        return path
    p, c = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(cwd))
    if p.startswith(c + os.sep):
        return os.path.normpath(path)[len(c) + 1:].replace(os.sep, "/")
    return path


def tool_line(name, inp, cwd, changed):
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = rel(inp.get("file_path") or inp.get("notebook_path", ""), cwd)
        changed.append(p)
        return ("edit", p)
    if name == "Read":
        return ("read", rel(inp.get("file_path", ""), cwd))
    if name in ("Grep", "Glob"):
        return ("tool", "%s %s" % (name.lower(), short(inp.get("pattern", ""), 80)))
    if name in ("Bash", "PowerShell"):
        cmd = inp.get("command", "")
        tag = " [commit]" if re.search(r"\bgit\s+commit\b", cmd) else ""
        return ("tool", "%s: %s%s" % (name.lower(), short(inp.get("description") or cmd, 140), tag))
    if name in ("Agent", "Task"):
        return ("tool", "agent: " + short(inp.get("description") or inp.get("prompt", ""), 120))
    if name == "Skill":
        return ("tool", "skill: %s %s" % (inp.get("skill", ""), short(inp.get("args", ""), 80)))
    if name == "TodoWrite":
        return ("todo", inp.get("todos") or [])
    if name == "AskUserQuestion":
        qs = [q.get("question", "") for q in inp.get("questions") or [] if isinstance(q, dict)]
        return ("tool", "asked the user: " + short(" | ".join(qs), 300))
    if name.startswith("mcp__"):
        key = next((str(v) for k, v in inp.items() if k in (
            "issueIdOrKey", "query", "jql", "sql", "cql", "pageId", "url")), "")
        return ("tool", "%s %s" % (name.split("__")[-1], short(key, 100)))
    return ("tool", name)


def parse(path):
    meta = {"cwd": None, "branch": None, "first": None, "last": None, "prompts": 0,
            "entrypoint": None, "resume_tokens": 0, "model": None}
    events, changed, todos, compact = [], [], None, None
    for rec, _ in iter_records(path):
        if not rec or rec.get("isSidechain"):
            continue
        ts = rec.get("timestamp")
        if ts and rec.get("type") in ("user", "assistant"):
            meta["first"] = meta["first"] or ts
            meta["last"] = ts
        meta["cwd"] = rec.get("cwd") or meta["cwd"]
        meta["branch"] = rec.get("gitBranch") or meta["branch"]
        meta["entrypoint"] = meta["entrypoint"] or rec.get("entrypoint")
        if rec.get("type") == "user" and rec.get("isCompactSummary"):
            c = (rec.get("message") or {}).get("content")
            compact = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            events.append(("compact", ""))
            continue
        text = user_text(rec)
        if text is not None:
            text = clean_user(text)
            if text:
                meta["prompts"] += 1
                events.append(("user", text))
            continue
        if rec.get("type") == "assistant":
            msg = rec.get("message") or {}
            u = msg.get("usage")
            if isinstance(u, dict):
                # the prompt of the last request plus its answer: what a resume re-sends
                meta["resume_tokens"] = sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
                meta["model"] = msg.get("model") or meta["model"]
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    events.append(("asst", b["text"].strip()))
                elif b.get("type") == "tool_use":
                    kind, val = tool_line(b.get("name", ""), b.get("input"), meta["cwd"], changed)
                    if kind == "todo":
                        todos = val
                    else:
                        events.append((kind, val))
    return meta, events, changed, todos, compact


def render(events, budget_user, budget_asst, keep_tools_from):
    """Digest of events; actions before keep_tools_from are dropped (edits are kept)."""
    out, reads = [], []

    def flush():
        if reads:
            out.append("T: read " + ", ".join(dict.fromkeys(reads)))
            reads.clear()

    n = len(events)
    for i, (kind, val) in enumerate(events):
        recent = i >= n - 12
        if kind == "read":
            if i >= keep_tools_from:
                reads.append(val)
            continue
        flush()
        if kind == "user":
            out.append("U: " + short(val, 1000 if recent else budget_user))
        elif kind == "asst":
            lim = 2500 if i == n - 1 else 800 if recent else budget_asst
            if lim:
                out.append("A: " + short(val, lim))
        elif kind == "edit":
            out.append("T: edit " + val)
        elif kind == "compact":
            out.append("T: --- context compacted ---")
        elif i >= keep_tools_from:
            out.append("T: " + val)
    flush()
    return "\n".join(out)


def build_digest(path, limit=None):
    limit = int(limit or CFG["max_digest_chars"])
    meta, events, changed, todos, compact = parse(path)
    n = len(events)
    # from detailed to terse until it fits
    plans = [(800, 500, 0), (500, 300, n // 2), (300, 150, n - 40), (200, 0, n - 20), (120, 0, n)]
    head = []
    if compact:
        head.append("Summary written at compaction (earlier part of the session):\n" + short(compact, 3000))
    if todos:
        head.append("Last todo list:\n" + "\n".join(
            "- [%s] %s" % (t.get("status", "?"), short(t.get("content", ""), 150))
            for t in todos if isinstance(t, dict)))
    if changed:
        head.append("Changed files: " + ", ".join(dict.fromkeys(changed)))
    head = "\n\n".join(head)
    for bu, ba, kt in plans:
        body = render(events, bu, ba, kt)
        if len(head) + len(body) <= limit:
            break
    if len(head) + len(body) > limit:  # still too long: cut the middle
        room = max(limit - len(head) - 100, 1000)
        body = body[: room // 3] + "\n… [middle of the session omitted] …\n" + body[-(room * 2 // 3):]
    digest = (head + "\n\n" + body).strip()
    return meta, digest, list(dict.fromkeys(changed))


# ---------- projects ----------

def project_root(cwd):
    if not cwd:
        return None
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                             capture_output=True, timeout=10, **UTF8)
        if out.returncode == 0 and out.stdout.strip():
            return os.path.normpath(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.normpath(cwd)


def dumps_dir_for(root):
    """Directory of the dumps of a project."""
    if CFG["dumps_dir"] == "project":
        return os.path.join(root, ".claude", "session-dumps")
    slug = re.sub(r"[^A-Za-z0-9]", "-", root or "unknown")
    return os.path.join(os.path.expanduser(CFG["dumps_dir"]), slug)


def all_dumps_dirs():
    if CFG["dumps_dir"] == "project":
        return []
    return [d for d in glob.glob(os.path.join(os.path.expanduser(CFG["dumps_dir"]), "*")) if os.path.isdir(d)]


def exclude_from_git(root, dumps):
    """In `project` mode keep the dumps out of git status via .git/info/exclude."""
    path = os.path.join(root, ".git", "info", "exclude")
    if not os.path.isdir(os.path.dirname(path)):
        return
    line = "/" + os.path.relpath(dumps, root).replace(os.sep, "/") + "/"
    try:
        with open(path, encoding="utf-8") as f:
            if line in f.read().splitlines():
                return
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n# session-watcher dumps\n%s\n" % line)


def project_allowed(root):
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.expanduser(p)))
    allow = [norm(p) for p in CFG["projects"]]
    r = norm(root or "")
    return not allow or any(r == p or r.startswith(p + os.sep) for p in allow)


# ---------- dump files ----------

FIELDS = ("tag", "status", "session", "project", "branch", "session_start", "session_end",
          "user_turns", "resume_tokens", "dump_tokens", "model", "dumped", "used_at", "used_in")


def read_dump(path):
    """(meta, body) of a dump file; meta comes from the front matter."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    meta = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
            text = text[end + 5:]
    return meta, text.lstrip("\n")


def write_dump(path, meta, body):
    fm = "\n".join("%s: %s" % (k, meta.get(k, "")) for k in FIELDS)
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write("---\n%s\n---\n\n%s" % (fm, body))
    os.replace(path + ".tmp", path)


def dump_files(dirs):
    out = []
    for d in dirs:
        out += glob.glob(os.path.join(d, "*_*.md"))
    return out


def title_of(body):
    first = body.lstrip().split("\n", 1)[0]
    return first.lstrip("# ").strip() or "untitled"


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "-".join(s.split("-")[:5])[:40].strip("-")


def unique_tag(tag, dumps, sid):
    taken = set()
    for p in dump_files([dumps]):
        m, _ = read_dump(p)
        if m.get("session") != sid:
            taken.add(m.get("tag"))
    base, n = tag, 2
    while tag in taken:
        tag, n = "%s-%d" % (base, n), n + 1
    return tag


def split_answer(body):
    """(title, tag, rest) from the model answer: `Title:` and `Tag:` lines first."""
    title, tag, lines = None, None, body.strip().split("\n")
    while lines and (title is None or tag is None):
        line = lines[0].strip()
        if not line:
            lines.pop(0)
            continue
        m = re.match(r"\W*tag\W*:\s*(.+)", line, re.I)
        if m and tag is None:
            tag = slug(m.group(1))
            lines.pop(0)
            continue
        if line.startswith("##") or title is not None:
            break
        m = re.match(r"(?:#\s*)?(?:\**[^\s:*]{1,12}\**\s*:\s*)?(.+)", line)
        title = short(m.group(1).strip("*_ "), 90)
        lines.pop(0)
    return title, tag, "\n".join(lines).strip()


def tokens_k(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return "%.1fk" % (n / 1000.0) if n < 10000 else "%dk" % round(n / 1000.0)


def call_llm(digest, claude_bin):
    if not claude_bin:
        return None, "claude binary not found (set claude_bin in config)"
    os.makedirs(RUN_DIR, exist_ok=True)
    env = dict(os.environ, **{CHILD_ENV: "1", "MAX_THINKING_TOKENS": "0"})
    lang = CFG["language"]
    lang = ("Write the content in the language the user mostly used in the session." if lang in ("", "auto")
            else "Write the content in %s." % lang)
    prompt = USER_PROMPT.format(words=int(CFG["max_words"]), language=lang) + digest
    base = [claude_bin, "-p", "--model", CFG["model"], "--output-format", "json",
            "--system-prompt", SYSTEM_PROMPT]
    # lean flags first; an older CLI that rejects one of them gets the plain call
    attempts = [base + ["--tools", "", "--no-session-persistence", "--strict-mcp-config",
                        "--setting-sources", "", "--disable-slash-commands"], base]
    err = ""
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, cwd=RUN_DIR,
                               env=env, timeout=CFG["llm_timeout_seconds"], **UTF8)
        except Exception as e:
            return None, "failed to run claude: %s" % e
        try:
            data = json.loads(r.stdout)
        except ValueError:
            err = "rc=%s %s" % (r.returncode, short(r.stderr or r.stdout, 300))
            if re.search(r"unknown option|unrecognized|invalid", r.stderr or "", re.I):
                continue
            return None, err
        if data.get("is_error") or not data.get("result"):
            return None, short(json.dumps(data, ensure_ascii=False), 300)
        u = data.get("usage") or {}
        usage = "in=%s cache_read=%s cache_write=%s out=%s cost=$%s" % (
            u.get("input_tokens"), u.get("cache_read_input_tokens"),
            u.get("cache_creation_input_tokens"), u.get("output_tokens"), data.get("total_cost_usd"))
        return data["result"].strip(), usage
    return None, err


def git_state(cwd):
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        out = subprocess.run(["git", "status", "--short", "--branch"], cwd=cwd,
                             capture_output=True, timeout=20, **UTF8)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = out.stdout.splitlines() if out.returncode == 0 else []
    if len(lines) > 21:
        lines = lines[:21] + ["… %d more" % (len(lines) - 21)]
    return "\n".join(lines)


def local_time(ts):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts or "?"


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def find_dump(sid, dumps=None):
    pattern = "*_%s.md" % sid[:8]
    for d in [dumps] if dumps else all_dumps_dirs():
        for p in glob.glob(os.path.join(d, pattern)):
            return p
    return None


def make_dump(transcript, claude_bin=None, dry_run=False):
    sid = os.path.basename(transcript)[: -len(".jsonl")]
    meta, digest, _ = build_digest(transcript)
    if dry_run:
        return digest
    if meta["prompts"] == 0:
        log("%s: no user turns, nothing to dump" % sid)
        return None
    root = project_root(meta["cwd"]) or os.path.dirname(transcript)
    answer, info = call_llm(digest, find_claude(claude_bin))
    if answer:
        title, tag, body = split_answer(answer)
        log("%s: dump via %s, %s" % (sid, CFG["model"], info))
    else:
        log("%s: LLM unavailable (%s), saving the raw digest" % (sid, info))
        title, tag = None, None
        body = "> LLM unavailable (%s). Raw transcript digest below.\n\n```\n%s\n```" % (
            info, short(digest, 8000))
    dumps = dumps_dir_for(root)
    os.makedirs(dumps, exist_ok=True)
    if CFG["dumps_dir"] == "project":
        exclude_from_git(root, dumps)
    stamp = local_time(meta["first"]).replace(" ", "_").replace(":", "")
    path = find_dump(sid, dumps) or os.path.join(dumps, "%s_%s.md" % (stamp, sid[:8]))
    old = read_dump(path)[0] if os.path.exists(path) else {}
    tag = old.get("tag") or unique_tag(tag or slug(title or "") or "session-" + sid[:8], dumps, sid)
    text = "# %s\n\nResume the original session: `claude --resume %s`\n\n%s" % (title or "untitled", sid, body)
    state = git_state(meta["cwd"])
    if state:
        text += "\n\n## Working tree at dump time\n\n```\n%s\n```\n" % state
    write_dump(path, {
        "tag": tag, "status": "new", "session": sid, "project": root, "branch": meta["branch"] or "",
        "session_start": local_time(meta["first"]), "session_end": local_time(meta["last"]),
        "user_turns": meta["prompts"], "resume_tokens": meta["resume_tokens"],
        "dump_tokens": len(text) // 3, "model": meta["model"] or "", "dumped": now(),
    }, text)
    write_index(dumps)
    return path


def write_index(dumps):
    cutoff = time.time() - CFG["keep_days"] * 86400
    rows = []
    for p in dump_files([dumps]):
        if os.path.getmtime(p) < cutoff:
            os.remove(p)
            continue
        m, body = read_dump(p)
        rows.append((m.get("session_start", ""), "- `%s` · %s · %s · %s → [%s](%s)" % (
            m.get("tag", "?"), m.get("status", "?"), m.get("session_start", "?"), title_of(body),
            os.path.basename(p), os.path.basename(p))))
    rows.sort(reverse=True)
    with open(os.path.join(dumps, "INDEX.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Session dumps\n\nWritten by session-watcher after %d min of inactivity. "
                "Newest first. Load one with /dump-load <tag>.\n\n" % CFG["idle_minutes"]
                + "\n".join(r for _, r in rows) + "\n")


def transcript_cwd(path):
    for rec, _ in iter_records(path):
        if rec and rec.get("cwd"):
            return rec["cwd"]
    return None


def drop_dump(sid, transcript=None):
    p = find_dump(sid)
    if not p and transcript:  # `project` mode: the dumps live in the session's project
        cwd = transcript_cwd(transcript)
        p = find_dump(sid, dumps_dir_for(project_root(cwd))) if cwd else None
    if p:
        os.remove(p)
        write_index(os.path.dirname(p))
        log("%s: dump removed, the session ran an excluded command" % sid)


# ---------- list / load / clean ----------

def scope_dirs(all_projects):
    if all_projects:
        return all_dumps_dirs()
    return [dumps_dir_for(project_root(os.getcwd()))]


def collect(all_projects):
    items = []
    for p in dump_files(scope_dirs(all_projects)):
        m, body = read_dump(p)
        items.append((p, m, body))
    items.sort(key=lambda x: x[1].get("session_start", ""), reverse=True)
    return items


def cmd_list(all_projects):
    items = collect(all_projects)
    if not items:
        return "No session dumps %s." % ("at all" if all_projects else "for this project (try: all)")
    head = ["tag", "status", "session date", "session id", "resume prefix", "dump", "title"]
    if all_projects:
        head.insert(1, "project")
    rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for p, m, body in items:
        cells = ["`%s`" % m.get("tag", "?"), m.get("status", "?"), m.get("session_start", "?"),
                 "`%s`" % m.get("session", "?"), tokens_k(m.get("resume_tokens")) + " tok",
                 tokens_k(m.get("dump_tokens")) + " tok", title_of(body).replace("|", "/")]
        if all_projects:
            cells.insert(1, os.path.basename(m.get("project", "")) or "?")
        rows.append("| " + " | ".join(cells) + " |")
    return ("\n".join(rows) + "\n\nresume prefix: tokens a `claude --resume` of that session would "
            "process again once its prompt cache has expired; dump: approximate size of the dump.")


def match(items, ref):
    ref = ref.strip().strip("`\"'")
    exact = [i for i in items if i[1].get("tag") == ref or i[1].get("session") == ref]
    if exact or len(ref) < 4:
        return exact
    return [i for i in items if i[1].get("session", "").startswith(ref) or i[1].get("tag", "").startswith(ref)]


def cmd_load(ref):
    if not ref.strip():
        return False, "Name a dump: /dump-load <tag | session id>.\n\n" + cmd_list(False)
    found = match(collect(False), ref) or match(collect(True), ref)
    if not found:
        return False, "No dump matches `%s`.\n\n%s" % (ref, cmd_list(False))
    if len(found) > 1:
        return False, "`%s` matches several dumps: %s. Name one by its full tag or session id." % (
            ref, ", ".join("`%s` (%s)" % (m.get("tag"), m.get("session")) for _, m, _ in found))
    path, meta, body = found[0]
    was = meta.get("status")
    meta.update(status="used", used_at=now(), used_in=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    write_dump(path, meta, body)
    write_index(os.path.dirname(path))
    note = "" if was != "used" else " It had been loaded before, so it may be stale."
    return True, ("Context dump `%s` of session %s (%s to %s, project %s, branch %s), "
                  "now marked as used.%s\n\n%s" % (
                      meta.get("tag"), meta.get("session"), meta.get("session_start"),
                      meta.get("session_end"), meta.get("project"), meta.get("branch"), note, body))


def cmd_clean(all_projects):
    removed, dirs = [], set()
    for p, m, _ in collect(all_projects):
        if m.get("status") == "used":
            os.remove(p)
            removed.append(m.get("tag", os.path.basename(p)))
            dirs.add(os.path.dirname(p))
    for d in dirs:
        write_index(d)
    if not removed:
        return "No used dumps to delete."
    return "Deleted %d used dump(s): %s." % (len(removed), ", ".join("`%s`" % t for t in removed))


# ---------- watcher ----------

def load_state(sid):
    try:
        with open(os.path.join(STATE_DIR, sid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(sid, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, sid + ".json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(p + ".tmp", p)


def watch(sid, transcript, claude_bin):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, sid + ".lock"), "a") as lock:
        if not try_lock(lock):
            return  # another watcher has this session
        _watch(sid, transcript, claude_bin)


def _watch(sid, transcript, claude_bin):
    st = load_state(sid)
    if st.get("excluded"):
        return
    st.setdefault("offset", 0)
    st.setdefault("dumped_mtime", 0)
    st["transcript"] = transcript
    st["pid"] = os.getpid()
    if claude_bin:
        st["claude"] = claude_bin
    started = time.time()
    poll, idle_limit = CFG["poll_seconds"], CFG["idle_minutes"] * 60
    save_state(sid, st)
    log("%s: watching %s" % (sid, transcript))
    while True:
        if not os.path.exists(transcript):
            if time.time() - started > 6 * 3600:
                log("%s: transcript never appeared, exiting" % sid)
                return
            time.sleep(poll)
            continue
        if os.path.getsize(transcript) < st["offset"]:
            st["offset"] = 0  # rewritten
        for rec, off in iter_records(transcript, st["offset"]):
            st["offset"] = off
            if rec and rec.get("entrypoint") in CFG["skip_entrypoints"]:
                st["excluded"] = "entrypoint " + rec["entrypoint"]
            marker = st.get("excluded") or exclusion_marker(rec, CFG["exclude_commands"])
            if marker:
                st["excluded"] = marker
                save_state(sid, st)
                log("%s: %s, watching stopped" % (sid, marker))
                drop_dump(sid, transcript)
                return
        save_state(sid, st)
        mtime = os.path.getmtime(transcript)
        idle = time.time() - mtime
        if idle >= idle_limit and mtime > st["dumped_mtime"]:
            try:
                path = make_dump(transcript, st.get("claude"))
                if path:
                    log("%s: dump -> %s" % (sid, path))
            except Exception as e:
                log("%s: dump failed: %r" % (sid, e))
            st["dumped_mtime"] = mtime
            save_state(sid, st)
        if idle >= CFG["give_up_hours"] * 3600:
            log("%s: idle for %d h, exiting" % (sid, CFG["give_up_hours"]))
            return
        time.sleep(poll)


# ---------- hook ----------

def read_stdin():
    stream = getattr(sys.stdin, "buffer", None)
    return stream.read().decode("utf-8", "replace") if stream else sys.stdin.read()


def hook():
    if os.environ.get(CHILD_ENV):
        return
    try:
        payload = json.loads(read_stdin())
    except ValueError:
        return
    sid, transcript = payload.get("session_id"), payload.get("transcript_path")
    if not sid or not transcript or not re.match(r"^[\w-]+$", sid):
        return
    root = project_root(payload.get("cwd"))
    if root and not project_allowed(root):
        return
    args = [sys.executable, os.path.abspath(__file__), "watch", "--session", sid, "--transcript", transcript]
    exe = claude_ancestor() or os.environ.get("CLAUDE_CODE_EXECPATH")
    if exe:
        args += ["--claude", exe]
    spawn(args)


def status():
    rows = []
    for p in sorted(glob.glob(os.path.join(STATE_DIR, "*.json")), key=os.path.getmtime, reverse=True):
        st = load_state(os.path.basename(p)[:-5])
        state = ("excluded: " + st["excluded"]) if st.get("excluded") else (
            "watching" if pid_alive(st.get("pid")) else "stopped")
        rows.append("%s  %-30s %s" % (os.path.basename(p)[:-5], state, "dumped" if st.get("dumped_mtime") else ""))
    return "\n".join(rows) or "no sessions watched yet"


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Session watcher for Claude Code")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("hook")
    sub.add_parser("status")
    w = sub.add_parser("watch")
    w.add_argument("--session", required=True)
    w.add_argument("--transcript", required=True)
    w.add_argument("--claude")
    for name in ("list", "clean"):
        sub.add_parser(name).add_argument("scope", nargs="*")
    sub.add_parser("load").add_argument("ref", nargs="*")
    d = sub.add_parser("dump")
    d.add_argument("--transcript", required=True)
    d.add_argument("--dry-run", action="store_true")
    g = sub.add_parser("digest")
    g.add_argument("--transcript", required=True)
    a = ap.parse_args()
    if a.cmd == "hook":
        try:
            hook()
        except Exception as e:  # never get in the way of a session start
            log("hook: %r" % e)
    elif a.cmd == "watch":
        watch(a.session, a.transcript, a.claude)
    elif a.cmd == "status":
        print(status())
    elif a.cmd == "list":
        print(cmd_list(any(s.strip("-") == "all" for s in a.scope)))
    elif a.cmd == "clean":
        print(cmd_clean(any(s.strip("-") == "all" for s in a.scope)))
    elif a.cmd == "load":
        print(cmd_load(" ".join(a.ref))[1])
    elif a.cmd == "dump":
        res = make_dump(a.transcript, dry_run=a.dry_run)
        print(res if res else "no dump written (see %s)" % LOG)
    elif a.cmd == "digest":
        print(build_digest(a.transcript)[1])


if __name__ == "__main__":
    main()
