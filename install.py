#!/usr/bin/env python3
"""Install or remove session-watcher.

  python3 install.py                      # all projects: hook in ~/.claude/settings.json
  python3 install.py --settings PROJECT/.claude/settings.local.json   # one project only
  python3 install.py --exclude handoff    # commands that end watching (repeatable)
  python3 install.py --dumps project      # dumps in <project>/.claude/session-dumps/
  python3 install.py --uninstall          # remove the hook and the commands (dumps are kept)

The script is copied to ~/.claude/session-watcher/ (or $CLAUDE_CONFIG_DIR), the
/dump-list, /dump-load and /dump-clean commands go to the commands/ folder next
to the settings file, settings.json is backed up before it is changed, and an
existing config.json is updated only by the options given.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
import time

WINDOWS = os.name == "nt"
HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
HOME = os.environ.get("SESSION_WATCHER_HOME") or os.path.join(CLAUDE_DIR, "session-watcher")
SCRIPT = os.path.join(HOME, "session_watcher.py")
MARK = "session_watcher.py"
CMD_MARK = "<!-- session-watcher -->"

COMMANDS = {
    "dump-list": ("List session-watcher dumps: tag, status, session date and id, resume prefix size",
                  "[all]", "list",
                  "Show the output above to the user exactly as printed, then stop. Do not open any dump."),
    "dump-load": ("Load a session-watcher dump into this session by tag or session id",
                  "<tag | session id>", "load",
                  "The output above is a context dump of an earlier Claude Code session, loaded at the "
                  "user's explicit request. If it is an error or a list of candidates instead, show it and "
                  "ask which dump to load. Otherwise take the dump as the starting context of this session: "
                  "in 3-5 lines say what the earlier session was doing, where it stopped and the next step "
                  "it names, then wait for the user. Do not redo finished work, and check the current files "
                  "and git state before changing anything: the dump may be outdated."),
    "dump-clean": ("Delete session-watcher dumps already loaded into a session (status used)",
                   "[all]", "clean",
                   "Report the output above to the user as is."),
}


def shell_path(p):
    """A path for a bash command line (Claude Code runs `!` commands in bash, Git Bash on Windows)."""
    p = p.replace("\\", "/") if WINDOWS else p
    return shlex.quote(p)


def read_settings(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        sys.exit("%s is not valid JSON; fix it first, nothing was changed" % path)


def write_settings(path, data):
    if os.path.exists(path):
        backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        print("backup:", backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(path + ".tmp", path)


def ours(h):
    return MARK in str(h.get("command", "")) or any(MARK in str(a) for a in h.get("args", []))


def strip_hook(data):
    """Settings without our SessionStart entries; True if something was removed."""
    groups = (data.get("hooks") or {}).get("SessionStart") or []
    kept, removed = [], False
    for g in groups:
        hooks = [h for h in g.get("hooks", []) if not ours(h)]
        removed |= len(hooks) != len(g.get("hooks", []))
        if hooks:
            kept.append(dict(g, hooks=hooks))
    if removed:
        if kept:
            data["hooks"]["SessionStart"] = kept
        else:
            del data["hooks"]["SessionStart"]
            if not data["hooks"]:
                del data["hooks"]
    return removed


def commands_dir(settings):
    return os.path.join(os.path.dirname(settings), "commands")


def write_commands(settings):
    d = commands_dir(settings)
    os.makedirs(d, exist_ok=True)
    base = "%s %s" % (shell_path(sys.executable), shell_path(SCRIPT))
    for name, (desc, hint, sub, instruction) in COMMANDS.items():
        path = os.path.join(d, name + ".md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                if CMD_MARK not in f.read():
                    print("skipped %s: a command of that name already exists and is not ours" % path)
                    continue
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write('---\ndescription: %s\nargument-hint: "%s"\nallowed-tools: Bash(%s %s:*)\n---\n'
                    '%s\n!`%s %s "$ARGUMENTS"`\n\n%s\n' % (
                        desc, hint, base, sub, CMD_MARK, base, sub, instruction))
        print("command: /%s -> %s" % (name, path))


def remove_commands(settings):
    for name in COMMANDS:
        path = os.path.join(commands_dir(settings), name + ".md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                mine = CMD_MARK in f.read()
            if mine:
                os.remove(path)
                print("command removed:", path)


def install(args):
    os.makedirs(HOME, exist_ok=True)
    shutil.copy2(os.path.join(HERE, "session_watcher.py"), SCRIPT)
    print("script:", SCRIPT)

    cfg_path = os.path.join(HOME, "config.json")
    src = cfg_path if os.path.exists(cfg_path) else os.path.join(HERE, "config.example.json")
    with open(src, encoding="utf-8") as f:
        cfg = json.load(f)
    if args.exclude:
        cfg["exclude_commands"] = sorted(set(cfg.get("exclude_commands", [])) | set(args.exclude))
    if args.dumps:
        cfg["dumps_dir"] = args.dumps
    if args.idle:
        cfg["idle_minutes"] = args.idle
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("config:", cfg_path)

    data = read_settings(args.settings)
    strip_hook(data)  # reinstall replaces, never duplicates
    # exec form: no shell, so the same entry works with bash, zsh and PowerShell
    data.setdefault("hooks", {}).setdefault("SessionStart", []).append({"hooks": [{
        "type": "command", "command": sys.executable, "args": [SCRIPT, "hook"], "timeout": 10}]})
    write_settings(args.settings, data)
    print("hook:", args.settings)
    write_commands(args.settings)
    print("\nDone. New Claude Code sessions are watched; running ones are not.")


def uninstall(args):
    data = read_settings(args.settings)
    if strip_hook(data):
        write_settings(args.settings, data)
        print("hook removed from", args.settings)
    else:
        print("no session-watcher hook in", args.settings)
    remove_commands(args.settings)
    print("kept: %s (script, config, log) and the dumps; delete them by hand if needed" % HOME)


def main():
    ap = argparse.ArgumentParser(description="Install session-watcher for Claude Code")
    ap.add_argument("--settings", default=os.path.join(CLAUDE_DIR, "settings.json"),
                    help="settings file to put the hook in (default: user settings)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="slash command / skill name prefix that ends watching (repeatable)")
    ap.add_argument("--dumps", help="dumps directory, or 'project' for <project>/.claude/session-dumps")
    ap.add_argument("--idle", type=float, help="idle minutes before a dump")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()
    args.settings = os.path.abspath(os.path.expanduser(args.settings))
    (uninstall if args.uninstall else install)(args)


if __name__ == "__main__":
    main()
