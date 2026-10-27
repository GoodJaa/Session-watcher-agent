#!/usr/bin/env python3
"""Install or remove session-watcher.

  python3 install.py                      # all projects: hook in ~/.claude/settings.json
  python3 install.py --settings PROJECT/.claude/settings.local.json   # one project only
  python3 install.py --exclude mv- --exclude close   # commands that end watching
  python3 install.py --dumps project      # dumps in <project>/.claude/session-dumps/
  python3 install.py --uninstall          # remove the hook (dumps and config are kept)

The script is copied to ~/.claude/session-watcher/ (or $CLAUDE_CONFIG_DIR),
settings.json is backed up before it is changed, and an existing config.json
is updated only by the options given.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
HOME = os.environ.get("SESSION_WATCHER_HOME") or os.path.join(CLAUDE_DIR, "session-watcher")
SCRIPT = os.path.join(HOME, "session_watcher.py")
MARK = "session_watcher.py"


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


def strip_hook(data):
    """Settings without our SessionStart entries; True if something was removed."""
    groups = (data.get("hooks") or {}).get("SessionStart") or []
    kept, removed = [], False
    for g in groups:
        hooks = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
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


def install(args):
    os.makedirs(HOME, exist_ok=True)
    shutil.copy2(os.path.join(HERE, "session_watcher.py"), SCRIPT)
    os.chmod(SCRIPT, 0o755)
    print("script:", SCRIPT)

    cfg_path = os.path.join(HOME, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        with open(os.path.join(HERE, "config.example.json"), encoding="utf-8") as f:
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
    command = "%s %s hook" % (shlex.quote(sys.executable), shlex.quote(SCRIPT))
    data.setdefault("hooks", {}).setdefault("SessionStart", []).append(
        {"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    write_settings(args.settings, data)
    print("hook:", args.settings)
    print("\nDone. New Claude Code sessions are watched; running ones are not.")


def uninstall(args):
    data = read_settings(args.settings)
    if strip_hook(data):
        write_settings(args.settings, data)
        print("hook removed from", args.settings)
    else:
        print("no session-watcher hook in", args.settings)
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
