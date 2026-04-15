from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .context_manager import ContextManager
from .models import (
    CreateAgentRequest,
    SendMessageRequest,
    StartAgentRequest,
    StartPipelineRequest,
)
from .orchestrator import Orchestrator
from .ws_manager import WSManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# --- Singletons ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIR = os.environ.get("MULTIAGENT_WORKSPACE", os.path.join(os.path.dirname(BASE_DIR), "workspace"))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
RECENT_PROJECTS_FILE = os.path.join(CONFIG_DIR, "recent_projects.json")

ws_manager = WSManager()
ctx_manager = ContextManager(WORKSPACE_DIR)
orchestrator = Orchestrator(ws_manager, ctx_manager, CONFIG_DIR)


def _load_recent_projects() -> list[str]:
    if os.path.isfile(RECENT_PROJECTS_FILE):
        with open(RECENT_PROJECTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_recent_projects(projects: list[str]) -> None:
    with open(RECENT_PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def _add_recent_project(path: str) -> list[str]:
    projects = _load_recent_projects()
    norm = os.path.normpath(path)
    projects = [p for p in projects if os.path.normpath(p) != norm]
    projects.insert(0, path)
    projects = projects[:10]
    _save_recent_projects(projects)
    return projects


@asynccontextmanager
async def lifespan(app: FastAPI):
    orchestrator.load_roles()
    logger.info("MultiAgent backend started")
    yield
    logger.info("MultiAgent backend shutting down")


app = FastAPI(title="MultiAgent Studio", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ #
#  WebSocket                                                          #
# ------------------------------------------------------------------ #


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(ws)


# ------------------------------------------------------------------ #
#  Project                                                            #
# ------------------------------------------------------------------ #


@app.get("/api/project")
async def get_project():
    return {
        "path": orchestrator.project_dir,
        "recent": _load_recent_projects(),
    }


@app.put("/api/project")
async def set_project(body: dict):
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(400, "Missing path")
    if not os.path.isdir(path):
        raise HTTPException(400, f"Directory does not exist: {path}")
    orchestrator.project_dir = os.path.normpath(path)
    recent = _add_recent_project(path)
    logger.info("Project directory set to: %s", orchestrator.project_dir)
    return {"ok": True, "path": orchestrator.project_dir, "recent": recent}


# ------------------------------------------------------------------ #
#  Roles                                                              #
# ------------------------------------------------------------------ #


@app.get("/api/roles")
async def list_roles():
    return {"roles": {k: v.model_dump() for k, v in orchestrator.roles.items()}}


@app.get("/api/config/roles")
async def get_roles_yaml():
    return {"content": orchestrator.get_roles_yaml()}


@app.put("/api/config/roles")
async def update_roles_yaml(body: dict):
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "Missing content")
    orchestrator.save_roles_yaml(content)
    return {"ok": True}


# ------------------------------------------------------------------ #
#  Agents                                                             #
# ------------------------------------------------------------------ #


@app.get("/api/agents")
async def list_agents():
    return {
        "agents": {k: v.model_dump(mode="json") for k, v in orchestrator.agents.items()}
    }


@app.post("/api/agents")
async def create_agent(req: CreateAgentRequest):
    try:
        agent = orchestrator.create_agent(req.role_id, req.agent_id)
        return agent.model_dump(mode="json")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    try:
        orchestrator.delete_agent(agent_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/agents/{agent_id}/start")
async def start_agent(agent_id: str, req: StartAgentRequest):
    try:
        orchestrator.start_agent(agent_id, req.prompt, req.context_from)
        return {"ok": True, "agentId": agent_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents/{agent_id}/message")
async def send_message(agent_id: str, req: SendMessageRequest):
    try:
        await orchestrator.send_message(agent_id, req.content)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents/{agent_id}/stop")
async def stop_agent(agent_id: str):
    orchestrator.stop_agent(agent_id)
    return {"ok": True}


@app.get("/api/agents/{agent_id}/context")
async def get_agent_context(agent_id: str):
    content = ctx_manager.read(agent_id)
    return {"agentId": agent_id, "content": content}


# ------------------------------------------------------------------ #
#  Pipeline                                                           #
# ------------------------------------------------------------------ #


@app.post("/api/pipeline/start")
async def start_pipeline(req: StartPipelineRequest):
    import asyncio

    asyncio.create_task(orchestrator.run_pipeline(req.requirement, req.stages))
    return {"ok": True, "message": "Pipeline started"}


# ------------------------------------------------------------------ #
#  Static frontend (production / Docker)                              #
# ------------------------------------------------------------------ #

FRONTEND_DIST = os.path.join(os.path.dirname(BASE_DIR), "frontend", "dist")
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")
