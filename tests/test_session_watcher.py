"""Tests on synthetic transcripts with a fake `claude` binary: no network, no tokens.

    python -m unittest discover -s tests -v
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
ARGS_LOG = os.path.join(TMP, "claude-args.json")
WINDOWS = os.name == "nt"

FAKE_PY = os.path.join(TMP, "fake_claude.py")
with open(FAKE_PY, "w", encoding="utf-8") as f:
    f.write("""import json, os, sys
prompt = sys.stdin.read()
json.dump({"argv": sys.argv[1:], "prompt_len": len(prompt),
           "child": os.environ.get("SESSION_WATCHER_CHILD")}, open(%r, "w"))
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print(json.dumps({"is_error": True, "result": "boom"})); sys.exit(1)
print(json.dumps({"result": os.environ.get("FAKE_CLAUDE_ANSWER") or
                  "Title: Fix the parser\\nTag: Fix CSV Parser\\n\\n## Goal\\nFix it.",
                  "usage": {"input_tokens": 5, "output_tokens": 7}, "total_cost_usd": 0.001}))
""" % ARGS_LOG)
if WINDOWS:
    FAKE_CLAUDE = os.path.join(TMP, "claude.cmd")
    with open(FAKE_CLAUDE, "w") as f:
        f.write('@"%s" "%s" %%*\n' % (sys.executable, FAKE_PY))
else:
    FAKE_CLAUDE = os.path.join(TMP, "claude")
    with open(FAKE_CLAUDE, "w") as f:
        f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_PY))
    os.chmod(FAKE_CLAUDE, 0o755)

DUMPS = os.path.join(TMP, "dumps")
os.environ.update({
    "CLAUDE_CONFIG_DIR": os.path.join(TMP, "claude-config"),
    "SESSION_WATCHER_HOME": os.path.join(TMP, "home"),
    "SESSION_WATCHER_DUMPS_DIR": DUMPS,
    "SESSION_WATCHER_CLAUDE_BIN": FAKE_CLAUDE,
    "SESSION_WATCHER_EXCLUDE_COMMANDS": "handoff,wrap-",
    "SESSION_WATCHER_POLL_SECONDS": "0.05",
    "SESSION_WATCHER_IDLE_MINUTES": "0.001",
    "SESSION_WATCHER_GIVE_UP_HOURS": "0.0001",
})
os.environ.pop("CLAUDE_CODE_EXECPATH", None)
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


def assistant(*blocks, usage=None):
    msg = {"role": "assistant", "content": list(blocks), "model": "claude-test"}
    if usage:
        msg["usage"] = usage
    return {"type": "assistant", "message": msg, "timestamp": "2026-01-02T10:01:00Z", "cwd": PROJECT}


def text(t):
    return {"type": "text", "text": t}


def tool(name, **inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def command(name, args=""):
    return user("<command-message>%s</command-message>\n<command-name>/%s</command-name>\n"
                "<command-args>%s</command-args>" % (name, name, args))


def write_transcript(sid, records, age=3600):
    path = os.path.join(TMP, sid + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


BASIC = [
    user("Fix the CSV parser in src/parse.py"),
    assistant(text("Looking at the parser."), tool("Read", file_path=os.path.join(PROJECT, "src", "parse.py")),
              usage={"input_tokens": 3, "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 1000,
                     "output_tokens": 100}),
    tool_result("contents"),
    assistant(tool("Edit", file_path=os.path.join(PROJECT, "src", "parse.py"), old_string="a", new_string="b"),
              tool("Bash", command="git commit -m fix", description="Commit the fix")),
    assistant(text("Fixed and committed."),
              usage={"input_tokens": 2, "cache_read_input_tokens": 120000, "cache_creation_input_tokens": 3000,
                     "output_tokens": 500}),
]


def fresh():
    shutil.rmtree(DUMPS, ignore_errors=True)


class Exclusion(unittest.TestCase):
    P = ["handoff", "wrap-"]

    def test_slash_command(self):
        self.assertEqual(sw.exclusion_marker(command("handoff"), self.P), "/handoff")
        self.assertEqual(sw.exclusion_marker(command("wrap-up"), self.P), "/wrap-up")

    def test_plugin_command_and_typed(self):
        self.assertEqual(sw.exclusion_marker(command("myplug:handoff"), self.P), "/myplug:handoff")
        self.assertEqual(sw.exclusion_marker(user("/wrap-up now"), self.P), "/wrap-up")

    def test_skill_call(self):
        rec = assistant(tool("Skill", skill="handoff", args="KEY-1"))
        self.assertEqual(sw.exclusion_marker(rec, self.P), "Skill handoff")

    def test_not_excluded(self):
        for rec in (command("review"), user("please run /handoff later"),
                    tool_result("<command-name>/handoff</command-name>"),
                    assistant(tool("Skill", skill="review")),
                    dict(command("handoff"), isSidechain=True)):
            self.assertIsNone(sw.exclusion_marker(rec, self.P), rec)

    def test_no_prefixes_means_nothing_excluded(self):
        self.assertIsNone(sw.exclusion_marker(command("handoff"), []))


class Digest(unittest.TestCase):
    def test_contents(self):
        meta, digest, changed = sw.build_digest(write_transcript("d1", BASIC))
        self.assertIn("U: Fix the CSV parser", digest)
        self.assertIn("T: edit src/parse.py", digest)
        self.assertIn("[commit]", digest)
        self.assertIn("A: Fixed and committed.", digest)
        self.assertEqual(changed, ["src/parse.py"])
        self.assertEqual(meta["resume_tokens"], 123502)  # last request + its answer

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


class Answer(unittest.TestCase):
    def test_title_and_tag(self):
        self.assertEqual(sw.split_answer("Title: Fix it\nTag: fix-it\n\n## Goal\nx"),
                         ("Fix it", "fix-it", "## Goal\nx"))

    def test_translated_or_formatted_labels(self):
        self.assertEqual(sw.split_answer("# Починить парсер\n**Tag:** Fix Parser!\n## Goal")[:2],
                         ("Починить парсер", "fix-parser"))
        self.assertEqual(sw.split_answer("## Goal\nx"), (None, None, "## Goal\nx"))


class Dump(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_dump_front_matter_and_index(self):
        path = sw.make_dump(write_transcript("aaaa1111-dump", BASIC))
        meta, body = sw.read_dump(path)
        self.assertEqual(meta["tag"], "fix-csv-parser")
        self.assertEqual(meta["status"], "new")
        self.assertEqual(meta["resume_tokens"], "123502")
        self.assertTrue(body.startswith("# Fix the parser"))
        self.assertIn("claude --resume aaaa1111-dump", body)
        with open(os.path.join(os.path.dirname(path), "INDEX.md"), encoding="utf-8") as f:
            self.assertIn("`fix-csv-parser` · new", f.read())
        with open(ARGS_LOG) as f:
            call = json.load(f)
        self.assertEqual(call["child"], "1")
        for flag in ("--tools", "--setting-sources", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, call["argv"])

    def test_tags_are_unique_and_stable(self):
        a = sw.make_dump(write_transcript("aaaa0001-one", BASIC))
        b = sw.make_dump(write_transcript("bbbb0002-two", BASIC))
        self.assertEqual(sw.read_dump(b)[0]["tag"], "fix-csv-parser-2")
        sw.make_dump(write_transcript("aaaa0001-one", BASIC))  # refresh keeps the tag
        self.assertEqual(sw.read_dump(a)[0]["tag"], "fix-csv-parser")

    def test_refresh_makes_a_used_dump_new_again(self):
        path = sw.make_dump(write_transcript("cccc0003-ref", BASIC))
        with mock.patch.object(sw.os, "getcwd", return_value=PROJECT):
            sw.cmd_load("cccc0003")
        self.assertEqual(sw.read_dump(path)[0]["status"], "used")
        sw.make_dump(write_transcript("cccc0003-ref", BASIC))
        self.assertEqual(sw.read_dump(path)[0]["status"], "new")

    def test_llm_failure_keeps_the_raw_digest(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_FAIL": "1"}):
            path = sw.make_dump(write_transcript("bbbb2222-fail", BASIC))
        meta, body = sw.read_dump(path)
        self.assertIn("LLM unavailable", body)
        self.assertIn("Fix the CSV parser", body)
        self.assertEqual(meta["tag"], "session-bbbb2222")

    def test_no_user_turns_no_dump(self):
        self.assertIsNone(sw.make_dump(write_transcript("cccc3333-empty", [assistant(text("hi"))])))

    @unittest.skipIf(WINDOWS, "symlinks need extra rights on Windows")
    def test_project_through_a_symlink_is_the_same_project(self):
        link = os.path.join(TMP, "project-link")
        if not os.path.islink(link):
            os.symlink(PROJECT, link)
        recs = [dict(r, cwd=link) for r in BASIC]
        path = sw.make_dump(write_transcript("eeee9999-link", recs))
        with mock.patch.object(sw.os, "getcwd", return_value=os.path.realpath(PROJECT)):
            self.assertIn("eeee9999-link", sw.cmd_list(False))
        self.assertEqual(sw.read_dump(path)[0]["project"], os.path.realpath(PROJECT))

    def test_project_mode_writes_into_the_project(self):
        with mock.patch.dict(sw.CFG, {"dumps_dir": "project"}):
            path = sw.make_dump(write_transcript("dddd4444-proj", BASIC))
        self.assertTrue(path.startswith(os.path.join(os.path.realpath(PROJECT), ".claude", "session-dumps")))
        shutil.rmtree(os.path.join(PROJECT, ".claude"))


class Commands(unittest.TestCase):
    def setUp(self):
        fresh()
        self.cwd = mock.patch.object(sw.os, "getcwd", return_value=PROJECT)
        self.cwd.start()
        self.a = sw.make_dump(write_transcript("aaaa5555-first", BASIC))
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_ANSWER": "Title: Add login\nTag: add-login\n\n## Goal\ny"}):
            self.b = sw.make_dump(write_transcript("bbbb6666-second", BASIC))

    def tearDown(self):
        self.cwd.stop()

    def test_list_shows_tag_date_id_and_sizes(self):
        out = sw.cmd_list(False)
        self.assertIn("| tag | status | session date | session id | resume prefix | dump | title |", out)
        self.assertIn("`fix-csv-parser` | new | 2026-01-02", out)
        self.assertIn("`aaaa5555-first` | 124k tok", out)
        self.assertIn("project", sw.cmd_list(True).split("\n")[0])

    def test_load_by_tag_marks_used(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "current-1"}):
            ok, out = sw.cmd_load("add-login")
        self.assertTrue(ok)
        self.assertIn("# Add login", out)
        self.assertNotIn("tag: add-login", out)  # front matter stays in the file
        meta = sw.read_dump(self.b)[0]
        self.assertEqual((meta["status"], meta["used_in"]), ("used", "current-1"))
        self.assertEqual(sw.read_dump(self.a)[0]["status"], "new")

    def test_load_by_session_id_prefix(self):
        ok, out = sw.cmd_load("aaaa5555")
        self.assertTrue(ok)
        self.assertIn("Fix the parser", out)

    def test_load_ambiguous_unknown_and_empty(self):
        ok, out = sw.cmd_load("zzzz-nothing")
        self.assertFalse(ok)
        self.assertIn("No dump matches", out)
        self.assertIn("fix-csv-parser", out)  # the list follows
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_ANSWER": "Title: x\nTag: add-logout\n\n## Goal"}):
            sw.make_dump(write_transcript("cccc7777-third", BASIC))
        ok, out = sw.cmd_load("add-log")
        self.assertFalse(ok)
        self.assertIn("several dumps", out)
        self.assertFalse(sw.cmd_load("")[0])

    def test_clean_deletes_only_used(self):
        self.assertEqual(sw.cmd_clean(False), "No used dumps to delete.")
        sw.cmd_load("add-login")
        self.assertIn("`add-login`", sw.cmd_clean(False))
        self.assertFalse(os.path.exists(self.b))
        self.assertTrue(os.path.exists(self.a))

    def test_cli(self):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="cli-1")
        run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "session_watcher.py")] + list(a),
                                        cwd=PROJECT, env=env, capture_output=True, encoding="utf-8")
        self.assertIn("`add-login`", run("list", "").stdout)
        self.assertIn("# Add login", run("load", "add-login").stdout)
        self.assertIn("Deleted 1", run("clean", "").stdout)


class Watch(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_idle_session_is_dumped_then_excluded_dump_is_removed(self):
        sid = "eeee5555-watch"
        path = write_transcript(sid, BASIC)
        sw.watch(sid, path, None)  # idle beyond give_up: dumps, then exits
        self.assertIsNotNone(sw.find_dump(sid))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(command("handoff")) + "\n")
        sw.watch(sid, path, None)
        self.assertIsNone(sw.find_dump(sid))
        self.assertEqual(sw.load_state(sid)["excluded"], "/handoff")

    def test_excluded_before_idle_never_dumps(self):
        sid = "ffff6666-excl"
        sw.watch(sid, write_transcript(sid, [command("wrap-up")] + BASIC), None)
        self.assertIsNone(sw.find_dump(sid))

    def test_sdk_sessions_are_skipped(self):
        sid = "abab7777-sdk"
        sw.watch(sid, write_transcript(sid, [user("automated", entrypoint="sdk-cli")] + BASIC), None)
        self.assertIsNone(sw.find_dump(sid))

    def test_second_watcher_for_a_session_exits(self):
        sid = "acac8888-lock"
        os.makedirs(sw.STATE_DIR, exist_ok=True)
        with open(os.path.join(sw.STATE_DIR, sid + ".lock"), "a") as held:
            self.assertTrue(sw.try_lock(held))
            sw.watch(sid, write_transcript(sid, BASIC), None)
        self.assertIsNone(sw.find_dump(sid))


class Hook(unittest.TestCase):
    def run_hook(self, payload, env=None):
        out = io.StringIO()
        with mock.patch.object(sw, "spawn") as spawn, \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.dict(os.environ, env or {}):
            sw.hook()
        return spawn, out.getvalue()

    def test_spawns_a_watcher_and_prints_nothing(self):
        spawn, out = self.run_hook({"session_id": "s1", "transcript_path": "/t/s1.jsonl", "cwd": PROJECT,
                                    "source": "startup"})
        self.assertEqual(spawn.call_args[0][0][2:4], ["watch", "--session"])
        self.assertEqual(out, "")  # dumps reach a session only through /dump-load

    def test_child_of_the_watcher_is_ignored(self):
        spawn, _ = self.run_hook({"session_id": "s1", "transcript_path": "/t"}, {"SESSION_WATCHER_CHILD": "1"})
        spawn.assert_not_called()

    def test_projects_allowlist(self):
        with mock.patch.dict(sw.CFG, {"projects": [os.path.join(TMP, "elsewhere")]}):
            spawn, _ = self.run_hook({"session_id": "s1", "transcript_path": "/t", "cwd": PROJECT})
        spawn.assert_not_called()


class Platform(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(sw.pid_alive(os.getpid()))
        self.assertFalse(sw.pid_alive("nope"))

    def test_spawn_detaches(self):
        marker = os.path.join(TMP, "spawned")
        proc = sw.spawn([sys.executable, "-c", "open(%r, 'w').write('ok')" % marker])
        proc.wait(timeout=30)
        self.assertTrue(os.path.exists(marker))


class Installer(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_restores(self):
        cfg_dir = os.path.join(TMP, "inst")
        settings = os.path.join(cfg_dir, "settings.json")
        os.makedirs(cfg_dir, exist_ok=True)
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
        with open(settings, "w") as f:
            json.dump({"model": "opus", "hooks": {"PreToolUse": [other]}}, f)
        run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "install.py"),
                                         "--settings", settings] + list(a),
                                        check=True, capture_output=True, env=os.environ)
        run("--exclude", "handoff")
        run()
        with open(settings) as f:
            data = json.load(f)
        self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
        h = data["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual((h["command"], h["args"][-1]), (sys.executable, "hook"))
        self.assertEqual(data["hooks"]["PreToolUse"], [other])
        home = os.environ["SESSION_WATCHER_HOME"]
        self.assertTrue(os.path.exists(os.path.join(home, "session_watcher.py")))
        with open(os.path.join(home, "config.json")) as f:
            self.assertEqual(json.load(f)["exclude_commands"], ["handoff"])
        for name in ("dump-list", "dump-load", "dump-clean"):
            with open(os.path.join(cfg_dir, "commands", name + ".md"), encoding="utf-8") as f:
                body = f.read()
            self.assertIn('"$ARGUMENTS"`', body)
            self.assertIn("allowed-tools: Bash(", body)
        run("--uninstall")
        with open(settings) as f:
            self.assertEqual(json.load(f), {"model": "opus", "hooks": {"PreToolUse": [other]}})
        self.assertEqual(os.listdir(os.path.join(cfg_dir, "commands")), [])


if __name__ == "__main__":
    unittest.main()
