"""Muse-only file access and append operations for the Continuity Hub."""
import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from hub_status import HUB_ROOT, HandoffDraft, PublishedHandoff, publish_for_project, hub_command

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)]
MAX_BYTES = 1024 * 1024
APPEND_LOCK = Lock()


def require_muse_peer(request: Request, response: Response):
    allowed = os.getenv("MUSE_HUB_PEER_IPS", "").split()
    try:
        client = ipaddress.ip_address(request.client.host) if request.client else None
    except ValueError:
        client = None
    if client is None or str(client) not in allowed:
        raise HTTPException(403, "This workspace is assigned to the Muse device")
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/hub/workspace", dependencies=[Depends(require_muse_peer)])


def allowed_workspace_path(path: PurePosixPath) -> bool:
    parts = path.parts
    if not parts or parts == ("AGENTS.md",) or parts == ("README.md",):
        return True
    if parts[0] == "personal":
        return len(parts) == 1 or bool(
            len(parts) == 2 and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md", parts[1])
        )
    if parts[0] == "projects":
        return len(parts) == 1 or bool(
            re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", parts[1])
            and (len(parts) == 2 or (len(parts) == 3 and parts[2] in {"STATUS.md", "HANDOFF.md"}))
        )
    return False


def workspace_path(relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or any(c in relative for c in ("\\", ":", "\0")):
        raise HTTPException(400, "Use a path relative to the Hub")
    if not allowed_workspace_path(path):
        raise HTTPException(403, "Path is unavailable through this workspace")
    root = HUB_ROOT.resolve()
    try:
        resolved = (root / path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "Hub path not found") from None
    if not resolved.is_relative_to(root) or not allowed_workspace_path(
        PurePosixPath(resolved.relative_to(root).as_posix())
    ):
        raise HTTPException(403, "Path is unavailable through this workspace")
    return resolved


@router.get("/files", operation_id="list_hub_files")
def list_files(path: Annotated[str, Query(max_length=1024)] = "."):
    """List a Hub directory. Paths are relative to the Hub root."""
    folder = workspace_path(path)
    if not folder.is_dir():
        raise HTTPException(404, "Hub directory not found")
    root = HUB_ROOT.resolve()
    entries = []
    for child in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        try:
            relative = child.relative_to(root).as_posix()
            resolved = workspace_path(relative)
            if resolved.is_dir() or resolved.is_file():
                entries.append({"path": relative, "kind": "directory" if resolved.is_dir() else "file"})
        except (HTTPException, OSError, RuntimeError):
            continue
    return {"entries": entries}


@router.get("/file", operation_id="read_hub_file")
def read_file(
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    encoding: Annotated[str, Query(pattern="^(text|base64)$")] = "text",
    offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
):
    """Read a Hub file as UTF-8, or request byte chunks as base64."""
    file = workspace_path(path)
    if not file.is_file():
        raise HTTPException(404, "Hub file not found")
    if encoding == "text" and offset:
        raise HTTPException(400, "Use base64 for byte offsets")
    with file.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(MAX_BYTES + 1)
    more = len(data) > MAX_BYTES
    if encoding == "base64":
        return {"encoding": encoding, "content": base64.b64encode(data[:MAX_BYTES]).decode("ascii"),
                "next_offset": offset + MAX_BYTES if more else None}
    if more:
        raise HTTPException(413, "Use base64 to read this file in chunks")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError:
        raise HTTPException(415, "Use base64 for files that are not UTF-8") from None
    return {"encoding": "text", "content": content}


def read_hub_command(*arguments: str):
    try:
        result = subprocess.run(
            [*hub_command(), *arguments], capture_output=True, timeout=15,
            env={key: value for key, value in os.environ.items() if key != "BRIDGE_TOKEN"},
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        if result.stderr.decode("utf-8", errors="replace").startswith(("Hub: \u5c1a\u65e0", "Hub: \u6ca1\u6709\u627e\u5230")):
            raise HTTPException(404, "Hub project or handoff not found")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    raise HTTPException(503, "Hub storage unavailable")


@router.get("/projects", operation_id="list_hub_projects")
def list_projects():
    """List all Hub projects and their latest handoff numbers."""
    return {"projects": read_hub_command("projects")}


@router.get("/projects/{project}", operation_id="read_hub_project")
def read_project(project: Slug, handoff_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None):
    """Read the latest handoff for a project, or read a specific handoff."""
    if handoff_id is None:
        return read_hub_command("status", project)
    return read_hub_command("status", project, "--id", str(handoff_id))


class WorkspaceHandoffDraft(HandoffDraft):
    expected_id: int | None = Field(ge=1, le=2**63 - 1, strict=True)


@router.post("/projects/{project}/handoffs", operation_id="append_hub_project_handoff",
             status_code=201, response_model=PublishedHandoff)
def append_handoff(project: Slug, draft: WorkspaceHandoffDraft):
    """Append a handoff based on the current number. Use null for a new project."""
    return publish_for_project(project, draft)


class PersonalNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: UUID
    section: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    replace_text: Annotated[str, StringConstraints(min_length=1, max_length=4000)] | None = None


def write_atomically(path: Path, data: bytes):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_receipts(path: Path, receipts: dict):
    write_atomically(path, json.dumps(receipts, sort_keys=True).encode("utf-8"))


def section_body_range(document: bytes, section: str) -> tuple[int, int]:
    headings = []
    fence = None
    offset = 0
    for line in document.splitlines(keepends=True):
        fence_match = re.match(rb" {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if fence_match:
            marker, remainder = fence_match.groups()
            if fence is None:
                fence = marker
            elif marker.startswith(fence) and not remainder.strip():
                fence = None
        elif fence is None:
            heading = re.match(rb" {0,3}(#{1,6})[ \t]+([^\r\n]+)", line)
            if heading:
                level = len(heading[1])
                title = re.sub(rb"[ \t]+#+[ \t]*$", b"", heading[2]).strip().decode("utf-8")
                headings.append((offset, level, title))
        offset += len(line)

    sections = [title for _, level, title in headings if level == 2]
    matches = [index for index, (_, level, title) in enumerate(headings)
               if level == 2 and title == section]
    if not matches:
        raise HTTPException(404, detail={"error": "Section not found", "sections": sections})
    if len(matches) > 1:
        raise HTTPException(409, "Section heading is ambiguous")
    heading_offset = headings[matches[0]][0]
    next_heading = matches[0] + 1
    start = document.find(b"\n", heading_offset)
    end = headings[next_heading][0] if next_heading < len(headings) else len(document)
    return (start + 1 if start >= 0 else end), end


def insert_under_heading(document: bytes, end: int, content: bytes) -> bytes:
    before = document[:end].rstrip(b"\r\n")
    after = document[len(before):]
    newline = b"\r\n" if b"\r\n" in document else b"\n"
    content = content.replace(b"\r\n", b"\n").replace(b"\n", newline)
    last_line = before.splitlines()[-1]
    continues_list = all(re.match(rb"[-+*][ \t]", line) for line in (last_line, content))
    separator = newline if continues_list else newline * 2
    return before + separator + content + (after or newline)


def replace_line(document: bytes, start: int, end: int, old: bytes, content: bytes) -> bytes:
    pattern = rb"(?m)^" + re.escape(old) + rb"(?=\r?$)"
    matches = list(re.finditer(pattern, document[start:end]))
    if len(matches) != 1:
        raise HTTPException(409, "Expected exactly one matching line in the selected section; read it again")
    match = matches[0]
    return document[:start + match.start()] + content + document[start + match.end():]


@router.post("/personal/{topic}/notes", operation_id="append_hub_personal_note", status_code=201)
def append_personal_note(topic: Slug, note: PersonalNote):
    """Read the file and select an existing ## heading. Append Markdown content, or set replace_text to the complete old line to replace it in place with content. Reuse the entry ID on retry."""
    if topic.startswith("muse-"):
        raise HTTPException(404, "Use the existing primary topic, without the muse- prefix")
    if "<!-- muse-note:" in note.content:
        raise HTTPException(422, "Reserved marker in note")
    if re.search(r"(?m)^ {0,3}#{1,6}(?:[ \t]|$)", note.content):
        raise HTTPException(422, "Send content without another heading")
    content = note.content.encode("utf-8")
    legacy_digest = hashlib.sha256(content).hexdigest()
    digest = hashlib.sha256(note.section.encode("utf-8") + b"\0" + content).hexdigest()
    action = "append"
    if note.replace_text is not None:
        if not note.replace_text.strip() or any(c in note.replace_text + note.content for c in "\r\n"):
            raise HTTPException(422, "Replacement requires one complete old line and one new line")
        if note.replace_text == note.content:
            raise HTTPException(422, "The replacement matches the original line")
        action = "replace"
        digest = hashlib.sha256(
            json.dumps([note.section, note.replace_text, note.content], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    entry_id = str(note.entry_id)
    with APPEND_LOCK:
        folder = workspace_path("personal")
        if folder != HUB_ROOT.resolve() / "personal" or not folder.is_dir():
            raise HTTPException(409, "Personal note directory is unavailable")
        target = folder / f"{topic}.md"
        if target.is_symlink():
            raise HTTPException(409, "Personal note destination is unavailable")
        if not target.is_file():
            raise HTTPException(404, "Personal memory topic must already exist")

        private = HUB_ROOT.resolve() / "private"
        receipts_dir = private / "append-receipts"
        if private.is_symlink() or receipts_dir.is_symlink():
            raise HTTPException(409, "Personal note receipts are unavailable")
        receipts_dir.mkdir(parents=True, exist_ok=True)
        receipts_path = receipts_dir / f"{topic}.json"
        if receipts_path.is_symlink():
            raise HTTPException(409, "Personal note receipts are unavailable")
        receipts = (
            json.loads(receipts_path.read_text(encoding="utf-8"))
            if receipts_path.exists() else {"entries": {}}
        )
        previous = target.read_bytes()
        before = hashlib.sha256(previous).hexdigest()
        entries = receipts["entries"]
        log = receipts.setdefault("log", {})
        pending = receipts.get("pending")
        if pending:
            # A previous process may have stopped between replacing the file
            # and marking the receipt complete.
            if before == pending["after"]:
                entries[pending["id"]] = pending["digest"]
                if "record" in pending:
                    log[pending["id"]] = {
                        **pending["record"],
                        "saved_at": datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat(),
                        "time_source": "file_mtime",
                    }
            elif before != pending["before"]:
                raise HTTPException(409, "Previous note result is unclear; review the topic before retrying")
            del receipts["pending"]
            save_receipts(receipts_path, receipts)

        if entry_id in entries:
            if entries[entry_id] != digest and not (action == "append" and entries[entry_id] == legacy_digest):
                raise HTTPException(409, "This entry ID already has different content")
        else:
            start, end = section_body_range(previous, note.section)
            record = {"agent": "Muse", "section": note.section, "action": action, "content_sha256": legacy_digest}
            if note.replace_text is None:
                updated = insert_under_heading(previous, end, content)
            else:
                updated = replace_line(previous, start, end, note.replace_text.encode("utf-8"), content)
                record["before"] = note.replace_text
            receipts["pending"] = {
                "id": entry_id, "digest": digest, "record": record,
                "before": before, "after": hashlib.sha256(updated).hexdigest(),
            }
            save_receipts(receipts_path, receipts)
            if target.read_bytes() != previous:
                del receipts["pending"]
                save_receipts(receipts_path, receipts)
                raise HTTPException(409, "Topic changed; read it again before writing")
            write_atomically(target, updated)
            entries[entry_id] = digest
            log[entry_id] = {
                **record, "saved_at": datetime.now(timezone.utc).isoformat(), "time_source": "server_clock",
            }
            del receipts["pending"]
            save_receipts(receipts_path, receipts)
        record = log.get(entry_id, {})
    return {
        "path": f"personal/{target.name}", "section": note.section, "entry_id": note.entry_id,
        "action": action, "agent": record.get("agent"), "saved_at": record.get("saved_at"),
        "time_source": record.get("time_source"),
    }
