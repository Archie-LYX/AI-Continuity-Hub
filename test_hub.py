"""Local regression checks for memory retrieval and conflicting handoffs."""
import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from hub import ROOT, Hub, HubError


def sample(agent):
    return {
        "agent": agent,
        "goal": "测试两次接力。",
        "done": "已记录当前进度。",
        "verification": "本地样例。",
        "blockers": "无。",
        "next_step": "完成最后一次核对。",
        "references": ["README.md"],
    }


class HubTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(dir=ROOT)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.hub = Hub(self.root)

    def test_publish_refreshes_markdown(self):
        first = self.hub.publish("demo", None, sample("first"))
        second = self.hub.publish("demo", first["id"], sample("second"))
        mirror = self.root / "projects" / "demo"
        status = (mirror / "STATUS.md").read_text(encoding="utf-8")
        self.assertIn(f"交接: #{second['id']}", status)
        history = (mirror / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertEqual(history.count("## #"), 2)

    def test_missing_database_preserves_history_and_backup_can_restore_it(self):
        first = self.hub.publish("demo", None, sample("first"))
        second = self.hub.publish("demo", first["id"], sample("second"))
        mirror = self.root / "projects" / "demo"
        original = {path.name: path.read_bytes() for path in mirror.iterdir()}
        backup, = (self.root / "data" / "backups").glob("*.sqlite3")
        with closing(sqlite3.connect(backup)) as database:
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone(), ("ok",))

        self.hub.database.unlink()
        with self.assertRaisesRegex(HubError, "请先从备份恢复"):
            self.hub.publish("demo", None, sample("restart"))
        self.assertFalse(self.hub.database.exists())
        self.assertEqual({path.name: path.read_bytes() for path in mirror.iterdir()}, original)

        shutil.copyfile(backup, self.hub.database)
        self.assertEqual(self.hub.handoff("demo")["id"], second["id"])
        self.hub.export()
        self.assertEqual({path.name: path.read_bytes() for path in mirror.iterdir()}, original)
        with self.assertRaises(HubError):
            self.hub.publish("demo", first["id"], sample("stale"))

    def test_publish_warns_if_markdown_refresh_fails(self):
        blocked = self.root / "projects"
        blocked.write_text("locked", encoding="utf-8")
        published = self.hub.publish("demo", None, sample("first"))
        self.assertIn("mirror_warning", published)
        self.assertEqual(self.hub.handoff("demo")["id"], published["id"])
        blocked.unlink()
        self.hub.export("demo")
        status = (blocked / "demo" / "STATUS.md").read_text(encoding="utf-8")
        self.assertIn(f"交接: #{published['id']}", status)

    def test_incomplete_handoff_does_not_replace_state(self):
        first = self.hub.publish("demo", None, sample("first"))
        with self.assertRaises(HubError):
            self.hub.publish("demo", first["id"], {"agent": "incomplete"})
        self.assertEqual(self.hub.handoff("demo"), first)

    def test_only_one_concurrent_update_can_use_the_same_base(self):
        first = self.hub.publish("demo", None, sample("first"))

        def update(agent):
            try:
                return Hub(self.root).publish("demo", first["id"], sample(agent))
            except HubError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ("second-a", "second-b")))
        accepted = [result for result in results if result is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.hub.handoff("demo"), accepted[0])
        self.assertEqual(self.hub.handoff("demo", first["id"]), first)

    def test_unknown_handoff_field_is_rejected(self):
        with self.assertRaises(HubError):
            self.hub.publish("demo", None, {**sample("first"), "mood": "happy"})
        self.assertEqual(self.hub.projects(), [])

    def test_personal_lookup_through_stdio(self):
        for name in ("hub.py", "mcp_server.py"):
            shutil.copyfile(ROOT / name, self.root / name)
        personal = self.root / "personal"
        personal.mkdir()
        text = "# Synthetic memory\n- Example test content in a temporary directory.\n"
        (personal / "interests-and-aesthetics.md").write_text(text, encoding="utf-8")
        client = self.start_client(self.root, "--include-personal")
        tools = self.exchange(client, "tools/list")["result"]["tools"]
        names = {entry["name"] for entry in tools}
        self.assertIn("read_personal_memory", names)
        self.assertNotIn("publish_project_handoff", names)
        result = self.call(client, "read_personal_memory", topic="interests-and-aesthetics")
        legacy = self.call(client, "read_personal_memory", topic="film-preferences")
        memory = result["content"][0]["text"]
        self.assertEqual(legacy["content"][0]["text"], memory)
        self.assertEqual(memory, text.strip())

    def test_isolated_project_round_trip_through_stdio(self):
        for name in ("hub.py", "mcp_server.py"):
            shutil.copyfile(ROOT / name, self.root / name)
        writer = self.start_client(self.root, "--allow-project-writes")
        reader = self.start_client(self.root)
        tools = self.exchange(reader, "tools/list")["result"]["tools"]
        self.assertEqual({item["name"] for item in tools}, {"list_projects", "read_project_handoff"})
        first = self.call(writer, "publish_project_handoff", project="demo", expected_id=None, payload=sample("first"))
        self.assertFalse(first["isError"])
        first_id = json.loads(first["content"][0]["text"])["id"]
        read = self.call(reader, "read_project_handoff", project="demo")
        self.assertEqual(json.loads(read["content"][0]["text"])["agent"], "first")
        second = self.call(writer, "publish_project_handoff", project="demo", expected_id=first_id, payload=sample("second"))
        self.assertFalse(second["isError"])
        old = self.call(reader, "read_project_handoff", project="demo", record_id=first_id)
        self.assertEqual(json.loads(old["content"][0]["text"])["agent"], "first")
        read = self.call(reader, "read_project_handoff", project="demo")
        self.assertEqual(json.loads(read["content"][0]["text"])["agent"], "second")

    def start_client(self, root, *options):
        client = subprocess.Popen(
            [sys.executable, "-B", str(root / "hub.py"), "mcp", *options],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8",
        )

        def close():
            client.stdin.close()
            try:
                client.wait(timeout=5)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait()
            client.stdout.close()

        self.addCleanup(close)
        self.exchange(client, "initialize", {"protocolVersion": "2025-11-25"})
        client.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        client.stdin.flush()
        return client

    def exchange(self, client, method, params=None):
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        client.stdin.write(json.dumps(request) + "\n")
        client.stdin.flush()
        return json.loads(client.stdout.readline())

    def call(self, client, name, **arguments):
        return self.exchange(client, "tools/call", {"name": name, "arguments": arguments})["result"]


CHINESE = {
    "agent": "Example assistant",
    "goal": "让发布命令支持从文件读取含中文的交接。",
    "done": "新增 --from-file，按 UTF-8 读取并忽略 BOM。",
    "verification": "中文样例经命令行写入后原样读回。",
    "blockers": "无",
    "next_step": "验收 Markdown 导出再接镜像。",
    "references": ["hub.py", "test_hub.py"],
}


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(dir=ROOT)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        shutil.copyfile(ROOT / "hub.py", self.root / "hub.py")
        self.hub = Hub(self.root)

    def cli(self, *arguments, stdin=None):
        return subprocess.run(
            [sys.executable, "-B", str(self.root / "hub.py"), *arguments],
            input=stdin, capture_output=True, encoding="utf-8",
        )

    def handoff_file(self, name, payload, encoding="utf-8-sig"):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding=encoding)
        return path

    def test_publish_from_file_with_chinese_and_bom(self):
        path = self.handoff_file("first.json", CHINESE)
        result = self.cli("publish", "demo", "--from-file", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["goal"], CHINESE["goal"])
        self.assertEqual(self.hub.handoff("demo")["done"], CHINESE["done"])

    def test_from_file_still_rejects_a_stale_base(self):
        first = self.hub.publish("demo", None, CHINESE)
        accepted = self.handoff_file("second.json", {**CHINESE, "agent": "Codex"})
        stale = self.handoff_file("stale.json", {**CHINESE, "agent": "Scout"})
        base = str(first["id"])
        self.assertEqual(self.cli("publish", "demo", "--expected", base, "--from-file", str(accepted)).returncode, 0)
        conflict = self.cli("publish", "demo", "--expected", base, "--from-file", str(stale))
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("基准已变化", conflict.stderr)
        self.assertEqual(self.hub.handoff("demo")["agent"], "Codex")

    def test_export_is_repeatable_without_duplicating_history(self):
        self.hub.publish("demo", None, {**CHINESE, "scope": "只改 hub.py 与测试", "touched": "hub.py"})
        self.hub.publish("demo", 1, {**CHINESE, "agent": "Codex", "goal": "导出 Markdown 镜像。", "touched": "hub.py"})
        result = self.cli("export", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.root / "projects" / "demo" / "STATUS.md"
        history = self.root / "projects" / "demo" / "HANDOFF.md"
        before = (status.read_bytes(), history.read_bytes())
        self.assertEqual(self.cli("export", "demo").returncode, 0)
        self.assertEqual((status.read_bytes(), history.read_bytes()), before)

        status_text = before[0].decode("utf-8")
        history_text = before[1].decode("utf-8")
        self.assertIn("导出 Markdown 镜像。", status_text)
        self.assertIn("- 上次改动: hub.py", status_text)
        self.assertEqual(history_text.count("## #"), 2)
        self.assertEqual(history_text.count("只改 hub.py 与测试"), 1)

    def test_export_writes_plain_utf8_crlf_and_leaves_the_database_alone(self):
        record = self.hub.publish("demo", None, CHINESE)
        self.assertEqual(self.cli("export", "demo").returncode, 0)
        raw = (self.root / "projects" / "demo" / "STATUS.md").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(self.hub.handoff("demo")["id"], record["id"])

    def test_export_all_and_missing_project(self):
        self.hub.publish("alpha", None, CHINESE)
        self.hub.publish("beta", None, CHINESE)
        result = self.cli("export", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(json.loads(result.stdout)), [
            "projects/alpha/HANDOFF.md", "projects/alpha/STATUS.md",
            "projects/beta/HANDOFF.md", "projects/beta/STATUS.md",
        ])
        missing = self.cli("export", "nowhere")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("尚无 nowhere 的交接", missing.stderr)
        self.assertEqual(self.cli("export").returncode, 1)


if __name__ == "__main__":
    unittest.main()
