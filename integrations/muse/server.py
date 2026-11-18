"""Optional local HTTP entry point for Muse and compatible clients."""
import hmac
import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from hub_status import router as hub_status_router
from hub_workspace import router as hub_workspace_router

token = os.environ.get("BRIDGE_TOKEN", "")
if not token:
    raise RuntimeError("Set BRIDGE_TOKEN before starting the Muse bridge")
os.environ.setdefault("MUSE_HUB_PEER_IPS", "127.0.0.1 ::1")

bearer = HTTPBearer(auto_error=False)


def require_auth(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
        raise HTTPException(401, "Invalid bridge credentials", headers={"WWW-Authenticate": "Bearer"})


app = FastAPI(title="AI Continuity Hub Muse Bridge")
app.include_router(hub_status_router, dependencies=[Depends(require_auth)])
app.include_router(hub_workspace_router, dependencies=[Depends(require_auth)])


@app.get("/health")
def health():
    return {"ok": True}
