"""Local AI Continuity Hub CLI and storage."""
import argparse
import json
import os
import re
import sqlite3
import sys
from tempfile import mkstemp
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FIELDS = ("agent", "goal", "done", "verification", "blockers", "next_step")
OPTIONAL = ("scope", "touched")
MIRROR = "本文件由 hub.py export 从 data/projects.sqlite3 生成，请勿手工修改。"
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    based_on INTEGER REFERENCES handoffs(id),
    saved_at TEXT NOT NULL,
    payload TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS handoffs_project ON handoffs(project, id)",
)


class HubError(ValueError):
    pass


class HandoffStorageError(HubError):
    pass


class HandoffConflict(HubError):
    def __init__(self, current_id):
        self.current_id = current_id
        actual = f"#{current_id}" if current_id else "空"
        super().__init__(f"交接基准已变化，当前为 {actual}。请先重读当前状态再提交。")


def slug(value):
    if not isinstance(value, str) or len(value) > 80 or not SLUG.fullmatch(value):
        raise HubError("名称须为小写英文字母、数字或连字符，例 interests-and-aesthetics。")
    return value


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def when(saved_at):
    stamp = datetime.fromisoformat(saved_at)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().isoformat(timespec="seconds")


def listed(references):
    return "\n".join(f"- {item}" for item in references) if references else "无"


def render_status(project, record):
    return "\n".join([
        f"# {project} 当前状态",
        "",
        f"- 交接: #{record['id']} · {when(record['saved_at'])} · {record['agent']}",
        f"- 验证: {record['verification']}",
        f"- 允许改动: {record.get('scope', '未声明')}",
        f"- 上次改动: {record.get('touched', '未声明')}",
        "",
        f"> {MIRROR}",
        "",
        "## 目标",
        record["goal"],
        "",
        "## 已完成",
        record["done"],
        "",
        "## 卡在",
        record["blockers"],
        "",
        "## 下一步",
        record["next_step"],
        "",
        "## 相关文件",
        listed(record["references"]),
        "",
    ])


def render_history(project, records):
    lines = [f"# {project} 交接记录", "", f"> {MIRROR}"]
    for record in records:
        based_on = f"#{record['based_on']}" if record["based_on"] else "首份"
        lines += [
            "",
            f"## #{record['id']} · {when(record['saved_at'])} · {record['agent']}",
            "",
            f"- 基于: {based_on}",
            f"- 目标: {record['goal']}",
            f"- 已完成: {record['done']}",
            f"- 验证: {record['verification']}",
            f"- 卡在: {record['blockers']}",
            f"- 下一步: {record['next_step']}",
            f"- 相关文件: {'、'.join(record['references']) or '无'}",
            f"- 允许改动: {record.get('scope', '未声明')}",
            f"- 上次改动: {record.get('touched', '未声明')}",
        ]
    return "\n".join(lines)


def read_payload(source):
    if source:
        path = Path(source)
        if not path.is_file():
            raise HubError(f"找不到交接文件 {source}。")
        raw = path.read_bytes()
    else:
        raw = sys.stdin.buffer.read()
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise HubError(f"交接 JSON 无法按 UTF-8 解析：{error}") from error


def write_markdown(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


class Hub:
    def __init__(self, root):
        self.root = Path(root)
        self.database = self.root / "data" / "projects.sqlite3"

    def personal_topics(self):
        return sorted(
            path.stem
            for path in (self.root / "personal").glob("*.md")
            if SLUG.fullmatch(path.stem)
        )

    def personal(self, topic):
        topic = slug(topic)
        if topic == "film-preferences":
            topic = "interests-and-aesthetics"
        path = self.root / "personal" / (topic + ".md")
        if not path.is_file():
            raise HubError(f"没有个人记忆主题 {topic}。")
        return path.read_text(encoding="utf-8").strip()

    def personal_history(self, topic):
        topic = slug(topic)
        path = self.root / "private" / "append-receipts" / (topic + ".json")
        if not path.is_file():
            return []
        records = json.loads(path.read_text(encoding="utf-8")).get("log", {})
        return [
            {
                "entry_id": entry_id,
                "topic": topic,
                "section": record["section"],
                "action": record["action"],
                "agent": record["agent"],
                "saved_at": when(record["saved_at"]),
                "time_source": record["time_source"],
            }
            for entry_id, record in sorted(records.items(), key=lambda item: item[1]["saved_at"])
        ]

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return closing(connection)

    def projects(self):
        if not self.database.is_file():
            return []
        with self.connect() as connection:
            if not self.initialized(connection):
                return []
            rows = connection.execute(
                "SELECT project, MAX(id) AS latest_id FROM handoffs GROUP BY project ORDER BY project"
            ).fetchall()
        return [dict(row) for row in rows]

    def handoff(self, project, record_id=None):
        slug(project)
        if record_id is not None and (type(record_id) is not int or not 1 <= record_id <= 2**63 - 1):
            raise HubError("交接编号须为正整数。")
        if not self.database.is_file():
            raise HubError(f"尚无 {project} 的交接。")
        with self.connect() as connection:
            if not self.initialized(connection):
                raise HubError(f"尚无 {project} 的交接。")
            if record_id is None:
                row = self.latest(connection, project)
            else:
                row = connection.execute(
                    "SELECT * FROM handoffs WHERE project = ? AND id = ?", (project, record_id)
                ).fetchone()
        if row is None:
            raise HubError(f"没有找到 {project} 的指定交接。")
        return self.record(row)

    @staticmethod
    def initialized(connection):
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'handoffs'"
        ).fetchone() is not None

    @staticmethod
    def latest(connection, project):
        return connection.execute(
            "SELECT * FROM handoffs WHERE project = ? ORDER BY id DESC LIMIT 1", (project,)
        ).fetchone()

    @staticmethod
    def record(row):
        record = dict(row)
        record.update(json.loads(record.pop("payload")))
        return record

    def export(self, project=None):
        """Rebuild the Markdown mirror using the latest committed handoffs."""
        if project is not None:
            slug(project)
        if not self.database.is_file():
            raise HubError("尚无任何项目交接，无法导出。")
        written = []
        with self.connect() as connection, connection:
            # Hold the same lock as publish so two exporters can't replace
            # each other's files or leave a newer record behind an older mirror.
            connection.execute("BEGIN IMMEDIATE")
            if not self.initialized(connection):
                raise HubError("尚无任何项目交接，无法导出。")
            if project is not None:
                names = [project]
            else:
                names = [row["project"] for row in connection.execute(
                    "SELECT DISTINCT project FROM handoffs ORDER BY project"
                )]
            if not names:
                raise HubError("尚无任何项目交接，无法导出。")
            for name in names:
                rows = connection.execute(
                    "SELECT * FROM handoffs WHERE project = ? ORDER BY id", (name,)
                ).fetchall()
                if not rows:
                    raise HubError(f"尚无 {name} 的交接。")
                records = [self.record(row) for row in rows]
                folder = self.root / "projects" / name
                write_markdown(folder / "HANDOFF.md", render_history(name, records))
                write_markdown(folder / "STATUS.md", render_status(name, records[-1]))
                written.extend([f"projects/{name}/STATUS.md", f"projects/{name}/HANDOFF.md"])
        return written

    def require_fresh_storage(self):
        backups = self.database.parent / "backups"
        projects = self.root / "projects"
        if (any(backups.glob("*.sqlite3"))
                or any(projects.glob("*/HANDOFF.md"))
                or any(projects.glob("*/STATUS.md"))):
            raise HandoffStorageError(
                "发现已有交接历史或备份，但交接数据库缺失或未初始化。"
                "请先从备份恢复 data/projects.sqlite3，再发布。"
            )

    def backup(self):
        """Save a consistent snapshot, replacing today's copy only after success."""
        if not self.database.is_file():
            raise HandoffStorageError("交接数据库不存在，无法备份。")
        folder = self.database.parent / "backups"
        folder.mkdir(exist_ok=True)
        destination = folder / (datetime.now().astimezone().date().isoformat() + ".sqlite3")
        with self.connect() as lock, lock:
            # Serialize snapshot replacement so an older publisher cannot overwrite a newer copy.
            lock.execute("BEGIN IMMEDIATE")
            if not self.initialized(lock):
                raise HandoffStorageError("交接数据库尚未初始化，无法备份。")
            descriptor, name = mkstemp(dir=folder, suffix=".tmp")
            os.close(descriptor)
            temporary = Path(name)
            try:
                with self.connect() as source:
                    source.execute("VACUUM INTO ?", (str(temporary),))
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return destination.relative_to(self.root).as_posix()

    def publish(self, project, expected_id, payload):
        slug(project)
        if expected_id is not None and (type(expected_id) is not int or expected_id < 1):
            raise HubError("expected_id 须为当前交接编号，首份交接使用 null。")
        required = set(FIELDS) | {"references"}
        allowed = required | set(OPTIONAL)
        if not isinstance(payload, dict) or not required <= set(payload) <= allowed:
            raise HubError(
                "交接字段须为 " + "、".join((*FIELDS, "references"))
                + "，另可选用 " + "、".join(OPTIONAL) + "。"
            )
        for key in FIELDS:
            value = payload[key]
            if not isinstance(value, str) or not value.strip() or len(value) > 600:
                raise HubError(f"{key} 须填入 1 到 600 个字符。")
        for key in OPTIONAL:
            value = payload.get(key)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 600
            ):
                raise HubError(f"{key} 若填写须为 1 到 600 个字符。")
        references = payload["references"]
        if not isinstance(references, list) or len(references) > 5 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 500
            for value in references
        ):
            raise HubError("references 须是最多五条文件或证据位置组成的列表。")
        payload = {key: value for key, value in payload.items() if value is not None}

        if not self.database.is_file():
            self.require_fresh_storage()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self.initialized(connection):
                self.require_fresh_storage()
                for statement in SCHEMA:
                    connection.execute(statement)
            current = self.latest(connection, project)
            current_id = current["id"] if current else None
            if current_id != expected_id:
                raise HandoffConflict(current_id)
            saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            cursor = connection.execute(
                "INSERT INTO handoffs(project, based_on, saved_at, payload) VALUES (?, ?, ?, ?)",
                (project, expected_id, saved_at, dump(payload)),
            )
            row = connection.execute("SELECT * FROM handoffs WHERE id = ?", (cursor.lastrowid,)).fetchone()
        record = self.record(row)
        try:
            self.export(project)
        except (ValueError, OSError, sqlite3.Error) as error:
            record["mirror_warning"] = (
                f"交接 #{record['id']} 已保存，Markdown 刷新失败。"
                f"请运行 export {project} 重建，无需重新发布。原因：{error}"
            )
        try:
            self.backup()
        except (ValueError, OSError, sqlite3.Error) as error:
            record["backup_warning"] = (
                f"交接 #{record['id']} 已保存，备份失败。"
                f"请运行 backup 补做备份，无需重新发布。原因：{error}"
            )
        return record


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("personal-topics")
    commands.add_parser("personal-history", help="在本机查看 Muse 写入记忆的记录。").add_argument("topic")
    commands.add_parser("projects")
    commands.add_parser("backup", help="创建当前交接数据库的本地备份。")
    commands.add_parser("personal").add_argument("topic")
    status = commands.add_parser("status")
    status.add_argument("project")
    status.add_argument("--id", type=int)
    publish = commands.add_parser("publish", help="追加一条完整交接，默认从标准输入读取 UTF-8 JSON。")
    publish.add_argument("project")
    publish.add_argument("--expected", type=int)
    publish.add_argument("--from-file", dest="from_file", metavar="PATH")
    publish.add_argument("--json-errors", action="store_true", help="向标准输出返回机器可读的发布错误。")
    exporter = commands.add_parser("export", help="从数据库重建 projects/<项目>/ 下的 Markdown 镜像。")
    exporter.add_argument("project", nargs="?")
    exporter.add_argument("--all", action="store_true")
    serve = commands.add_parser("mcp")
    serve.add_argument("--include-personal", action="store_true")
    serve.add_argument("--allow-project-writes", action="store_true")
    args = parser.parse_args()
    hub = Hub(ROOT)
    try:
        if args.command == "personal-topics":
            print(dump(hub.personal_topics()))
        elif args.command == "personal-history":
            print(dump(hub.personal_history(args.topic)))
        elif args.command == "personal":
            print(hub.personal(args.topic))
        elif args.command == "projects":
            print(dump(hub.projects()))
        elif args.command == "backup":
            print(dump({"backup": hub.backup()}))
        elif args.command == "status":
            print(dump(hub.handoff(args.project, args.id)))
        elif args.command == "publish":
            payload = read_payload(args.from_file)
            print(dump(hub.publish(args.project, args.expected, payload)))
        elif args.command == "export":
            if bool(args.project) == bool(args.all):
                raise HubError("请给出一个项目代号，或用 --all 导出全部项目。")
            print(dump(hub.export(None if args.all else args.project)))
        elif args.command == "mcp":
            from mcp_server import run

            run(hub, args.include_personal, args.allow_project_writes)
    except (ValueError, OSError, sqlite3.Error) as error:
        if args.command == "publish" and args.json_errors:
            if isinstance(error, HandoffConflict):
                print(dump({"error": "stale_handoff", "current_id": error.current_id}))
            elif isinstance(error, HandoffStorageError):
                print(dump({"error": "storage_unavailable"}))
            elif isinstance(error, ValueError):
                print(dump({"error": "invalid_handoff"}))
            else:
                print(dump({"error": "storage_unavailable"}))
        else:
            print(f"Hub: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
