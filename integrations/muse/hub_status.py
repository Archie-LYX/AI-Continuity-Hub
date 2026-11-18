"""Read the Hub status and append handoffs for one fixed project."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PROJECT = os.environ.get("HUB_PROJECT", "demo")
if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", PROJECT) or len(PROJECT) > 80:
    raise RuntimeError("HUB_PROJECT must be a Hub project slug")
HUB_ROOT = Path(os.environ.get("HUB_ROOT", Path(__file__).resolve().parents[2])).expanduser().resolve()
STATUS_FILE = HUB_ROOT / "projects" / PROJECT / "STATUS.md"
HANDOFF_HEADER = re.compile(r"(?m)^- 交接: #([0-9]+) · ")
HandoffText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]

router = APIRouter()


class HubStatus(BaseModel):
    project: str
    status: str
    handoff_id: int | None = None


class HandoffDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    goal: HandoffText
    done: HandoffText
    verification: HandoffText
    scope: HandoffText | None = None
    touched: HandoffText | None = None
    blockers: HandoffText
    next_step: HandoffText
    references: list[Reference] = Field(default_factory=list, max_length=5)


class PublishedHandoff(BaseModel):
    id: int
    based_on: int | None
    mirror_updated: bool
    backup_saved: bool


@router.get("/hub/status", operation_id="get_hub_status", response_model=HubStatus)
def get_hub_status(response: Response) -> HubStatus:
    """Return the AI Continuity Hub project summary and its handoff number."""
    try:
        status = STATUS_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise HTTPException(status_code=503, detail="Hub status is unavailable") from None
    if not status.strip():
        raise HTTPException(status_code=503, detail="Hub status is unavailable")
    match = HANDOFF_HEADER.search(status)
    response.headers["Cache-Control"] = "no-store"
    return HubStatus(project=PROJECT, status=status, handoff_id=int(match[1]) if match else None)


def hub_command() -> list[str]:
    script = HUB_ROOT / "hub.py"
    python = os.environ.get("HUB_PYTHON", sys.executable)
    if os.name != "nt" and python.lower().endswith(".exe"):
        # WSL can use the same Windows SQLite runtime as a Windows Hub.
        script = subprocess.check_output(["wslpath", "-w", str(script)], text=True, timeout=5).strip()
    launcher_flags = ["-3"] if Path(python).name.lower() == "py.exe" else []
    return [python, *launcher_flags, "-B", str(script)]

@router.post(
    "/hub/handoff",
    operation_id="publish_hub_handoff",
    status_code=201,
    response_model=PublishedHandoff,
)
def publish_hub_handoff(draft: HandoffDraft) -> PublishedHandoff:
    """Append a Muse handoff after reading the current handoff number."""
    return publish_for_project(PROJECT, draft)


def publish_for_project(project: str, draft: HandoffDraft) -> PublishedHandoff:
    writer_env = {name: value for name, value in os.environ.items() if name != "BRIDGE_TOKEN"}
    payload = {"agent": "Muse", **draft.model_dump(exclude={"expected_id"})}
    try:
        arguments = [*hub_command(), "publish", project, "--json-errors"]
        if draft.expected_id is not None:
            arguments.extend(["--expected", str(draft.expected_id)])
        result = subprocess.run(
            arguments,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=writer_env,
            timeout=35,
        )
        published = json.loads(result.stdout)
        if not isinstance(published, dict):
            raise ValueError("Invalid Hub reply")
        if result.returncode == 0:
            return PublishedHandoff(
                id=published["id"],
                based_on=published["based_on"],
                mirror_updated="mirror_warning" not in published,
                backup_saved="backup_warning" not in published,
            )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise HTTPException(status_code=503, detail="Write result unknown; read /hub/status before retrying") from None
    if published.get("error") == "stale_handoff":
        raise HTTPException(
            status_code=409, detail={"error": "stale_handoff", "current_id": published.get("current_id")},
        )
    if published.get("error") == "invalid_handoff":
        raise HTTPException(status_code=422, detail="Invalid project handoff")
    raise HTTPException(status_code=503, detail="Write result unknown; read /hub/status before retrying")
