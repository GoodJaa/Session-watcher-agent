"""Tests on synthetic transcripts with a fake `claude` binary: no network, no tokens.

    python3 -m unittest discover -s tests -v
"""

import atexit
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="session-watcher-test-")
atexit.register(shutil.rmtree, TMP, True)
FAKE_CLAUDE = os.path.join(TMP, "claude")
ARGS_LOG = os.path.join(TMP, "claude-args.json")

with open(FAKE_CLAUDE, "w") as f:
    f.write("""#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
json.dump({"argv": sys.argv[1:], "prompt_len": len(prompt),
           "child": os.environ.get("SESSION_WATCHER_CHILD")}, open(%r, "w"))
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print(json.dumps({"is_error": True, "result": "boom"})); sys.exit(1)
print(json.dumps({"result": "Title: Fix the parser\\n\\n## Goal\\nFix it.",
                  "usage": {"input_tokens": 5, "output_tokens": 7}, "total_cost_usd": 0.001}))
""" % ARGS_LOG)
os.chmod(FAKE_CLAUDE, 0o755)

os.environ.update({
    "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude-config"),
    "SESSION_WATCHER_HOME": os.path.join(TMP, "home"),
    "SESSION_WATCHER_DUMPS_DIR": os.path.join(TMP, "dumps"),
    "SESSION_WATCHER_CLAUDE_BIN": FAKE_CLAUDE,
    "SESSION_WATCHER_EXCLUDE_COMMANDS": "mv-,close",
    "SESSION_WATCHER_POLL_SECONDS": "0.05",
    "SESSION_WATCHER_IDLE_MINUTES": "0.001",
    "SESSION_WATCHER_GIVE_UP_HOURS": "0.0001",
})
sys.path.insert(0, ROOT)
sw = importlib.import_module("session_watcher")

PROJECT = os.path.join(TMP, "project")
os.makedirs(PROJECT, exist_ok=True)


def user(text, **kw):
    return dict({"type": "user", "message": {"role": "user", "content": text},
                 "timestamp": "2026-01-02T10:00:00Z", "cwd": PROJECT, "gitBranch": "main",
                 "entrypoint": "cli"}, **kw)


def tool_result(text):
    return user([{"type": "tool_result", "tool_use_id": "x", "content": text}])


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)},
            "timestamp": "2026-01-02T10:01:00Z", "cwd": PROJECT}


def text(t):
    return {"type": "text", "text": t}


def tool(name, **inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def command(name, args=""):
    return user("<command-message>%s</command-message>\n<command-name>/%s</command-name>\n"
                "<command-args>%s</command-args>" % (name, name, args))


def write_transcript(sid, records, age=3600):
    path = os.path.join(TMP, sid + ".jsonl")
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


BASIC = [
    user("Fix the CSV parser in src/parse.py"),
    assistant(text("Looking at the parser."), tool("Read", file_path=PROJECT + "/src/parse.py")),
    tool_result("contents"),
    assistant(tool("Edit", file_path=PROJECT + "/src/parse.py", old_string="a", new_string="b"),
              tool("Bash", command="git commit -m fix", description="Commit the fix")),
    assistant(text("Fixed and committed.")),
]


class Exclusion(unittest.TestCase):
    P = ["mv-", "close"]

    def test_slash_command(self):
        self.assertEqual(sw.exclusion_marker(command("mv-close"), self.P), "/mv-close")
        self.assertEqual(sw.exclusion_marker(command("close-session"), self.P), "/close-session")

    def test_plugin_command_and_typed(self):
        self.assertEqual(sw.exclusion_marker(command("myplug:mv-resume"), self.P), "/myplug:mv-resume")
        self.assertEqual(sw.exclusion_marker(user("/mv-note something"), self.P), "/mv-note")

    def test_skill_call(self):
        rec = assistant(tool("Skill", skill="mv-resume", args="KEY-1"))
        self.assertEqual(sw.exclusion_marker(rec, self.P), "Skill mv-resume")

    def test_not_excluded(self):
        for rec in (command("review"), user("please run /mv-close later"),
                    tool_result("<command-name>/mv-close</command-name>"),
                    assistant(tool("Skill", skill="review")),
                    dict(command("mv-close"), isSidechain=True)):
            self.assertIsNone(sw.exclusion_marker(rec, self.P), rec)

    def test_no_prefixes_means_nothing_excluded(self):
        self.assertIsNone(sw.exclusion_marker(command("mv-close"), []))


class Digest(unittest.TestCase):
    def test_contents(self):
        _, digest, changed = sw.build_digest(write_transcript("d1", BASIC))
        self.assertIn("U: Fix the CSV parser", digest)
        self.assertIn("T: edit src/parse.py", digest)
        self.assertIn("[commit]", digest)
        self.assertIn("A: Fixed and committed.", digest)
        self.assertEqual(changed, ["src/parse.py"])

    def test_system_reminders_and_commands_are_cleaned(self):
        recs = [user("hello<system-reminder>secret noise</system-reminder>"), command("review", "PR 12")]
        _, digest, _ = sw.build_digest(write_transcript("d2", recs))
        self.assertNotIn("secret noise", digest)
        self.assertIn("U: /review PR 12", digest)

    def test_long_session_fits_the_limit(self):
        recs = []
        for i in range(400):
            recs += [user("question %d " % i + "x" * 900),
                     assistant(text("answer %d " % i + "y" * 900), tool("Read", file_path="/f%d" % i))]
        _, digest, _ = sw.build_digest(write_transcript("d3", recs), limit=18000)
        self.assertLessEqual(len(digest), 18000)
        self.assertIn("question 0", digest)
        self.assertIn("answer 399", digest)


class Dump(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.environ["SESSION_WATCHER_DUMPS_DIR"], ignore_errors=True)

    def test_dump_and_index(self):
        path = sw.make_dump(write_transcript("aaaa1111-dump", BASIC))
        with open(path) as f:
            body = f.read()
        self.assertTrue(body.startswith("# Fix the parser"))
        self.assertIn("claude --resume aaaa1111-dump", body)
        self.assertIn("## Goal", body)
        with open(os.path.join(os.path.dirname(path), "INDEX.md")) as f:
            self.assertIn("Fix the parser", f.read())
        with open(ARGS_LOG) as f:
            call = json.load(f)
        self.assertEqual(call["child"], "1")
        for flag in ("--tools", "--setting-sources", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, call["argv"])

    def test_llm_failure_keeps_the_raw_digest(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_FAIL": "1"}):
            path = sw.make_dump(write_transcript("bbbb2222-fail", BASIC))
        with open(path) as f:
            body = f.read()
        self.assertIn("LLM unavailable", body)
        self.assertIn("Fix the CSV parser", body)

    def test_no_user_turns_no_dump(self):
        self.assertIsNone(sw.make_dump(write_transcript("cccc3333-empty", [assistant(text("hi"))])))

    def test_project_mode_writes_into_the_project(self):
        with mock.patch.dict(sw.CFG, {"dumps_dir": "project"}):
            path = sw.make_dump(write_transcript("dddd4444-proj", BASIC))
        self.assertTrue(path.startswith(os.path.join(PROJECT, ".claude", "session-dumps")))
        shutil.rmtree(os.path.join(PROJECT, ".claude"))


class Watch(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.environ["SESSION_WATCHER_DUMPS_DIR"], ignore_errors=True)

    def test_idle_session_is_dumped_then_excluded_dump_is_removed(self):
        sid = "eeee5555-watch"
        path = write_transcript(sid, BASIC)
        sw.watch(sid, path, None)  # idle beyond give_up: dumps, then exits
        self.assertIsNotNone(sw.find_dump(sid))
        with open(path, "a") as f:
            f.write(json.dumps(command("mv-close")) + "\n")
        sw.watch(sid, path, None)
        self.assertIsNone(sw.find_dump(sid))
        self.assertEqual(sw.load_state(sid)["excluded"], "/mv-close")

    def test_excluded_before_idle_never_dumps(self):
        sid = "ffff6666-excl"
        path = write_transcript(sid, [command("mv-resume", "KEY-1")] + BASIC)
        sw.watch(sid, path, None)
        self.assertIsNone(sw.find_dump(sid))

    def test_sdk_sessions_are_skipped(self):
        sid = "abab7777-sdk"
        path = write_transcript(sid, [user("automated", entrypoint="sdk-cli")] + BASIC)
        sw.watch(sid, path, None)
        self.assertIsNone(sw.find_dump(sid))


class Hook(unittest.TestCase):
    def run_hook(self, payload, env=None):
        out = io.StringIO()
        with mock.patch.object(sw, "spawn") as popen, \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.dict(os.environ, env or {}):
            sw.hook()
        return popen, out.getvalue()

    def test_spawns_a_detached_watcher(self):
        popen, _ = self.run_hook({"session_id": "s1", "transcript_path": "/t/s1.jsonl", "cwd": PROJECT})
        args = popen.call_args[0][0]
        self.assertEqual(args[2:4], ["watch", "--session"])

    def test_child_of_the_watcher_is_ignored(self):
        popen, _ = self.run_hook({"session_id": "s1", "transcript_path": "/t"}, {"SESSION_WATCHER_CHILD": "1"})
        popen.assert_not_called()

    def test_announces_the_latest_dump(self):
        shutil.rmtree(os.environ["SESSION_WATCHER_DUMPS_DIR"], ignore_errors=True)
        sw.make_dump(write_transcript("12345678-old", BASIC))
        _, out = self.run_hook({"session_id": "new-one", "transcript_path": "/t", "cwd": PROJECT,
                                "source": "startup"})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Fix the parser", ctx)
        _, out = self.run_hook({"session_id": "new-one", "transcript_path": "/t", "cwd": PROJECT,
                                "source": "compact"})
        self.assertEqual(out, "")

    def test_projects_allowlist(self):
        with mock.patch.dict(sw.CFG, {"projects": ["/somewhere/else"]}):
            popen, _ = self.run_hook({"session_id": "s1", "transcript_path": "/t", "cwd": PROJECT})
        popen.assert_not_called()


class Installer(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_keeps_other_hooks(self):
        settings = os.path.join(TMP, "settings.json")
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
        with open(settings, "w") as f:
            json.dump({"model": "opus", "hooks": {"PreToolUse": [other]}}, f)
        run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "install.py"),
                                         "--settings", settings] + list(a),
                                        check=True, capture_output=True, env=os.environ)
        run("--exclude", "mv-")
        run()
        with open(settings) as f:
            data = json.load(f)
        self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
        self.assertEqual(data["hooks"]["PreToolUse"], [other])
        self.assertTrue(os.path.exists(os.path.join(os.environ["SESSION_WATCHER_HOME"], "session_watcher.py")))
        with open(os.path.join(os.environ["SESSION_WATCHER_HOME"], "config.json")) as f:
            self.assertEqual(json.load(f)["exclude_commands"], ["mv-"])
        run("--uninstall")
        with open(settings) as f:
            self.assertEqual(json.load(f), {"model": "opus", "hooks": {"PreToolUse": [other]}})


if __name__ == "__main__":
    unittest.main()
