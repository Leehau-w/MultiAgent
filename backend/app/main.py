from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .models import (
    CreateAgentRequest,
    CreateProjectRequest,
    PermissionResponse,
    SendMessageRequest,
    StartAgentRequest,
    StartPipelineRequest,
)
from .budget import BudgetExceeded
from .compaction import compact_context, list_history, read_history
from .orchestrator import Orchestrator
from .project import Project
from .workflow import (
    Workflow,
    delete_workflow,
    load_workflow,
    save_workflow,
    workflow_path,
)
from .ws_manager import WSManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Windows: ensure Claude Code SDK can find git-bash when Git is not at the default path.
if sys.platform == "win32" and not os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
    import shutil as _shutil
    _git = _shutil.which("git")
    if _git:
        _d = os.path.dirname(os.path.abspath(_git))
        for _ in range(4):
            _d = os.path.dirname(_d)
            _candidate = os.path.join(_d, "bin", "bash.exe")
            if os.path.isfile(_candidate):
                os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = _candidate
                logger.info("Auto-detected CLAUDE_CODE_GIT_BASH_PATH=%s", _candidate)
                break

# --- Singletons ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIR = os.environ.get(
    "MULTIAGENT_WORKSPACE",
    os.path.join(os.path.dirname(BASE_DIR), "workspace"),
)
CONFIG_DIR = os.path.join(BASE_DIR, "config")
LEGACY_RECENT_PROJECTS_FILE = os.path.join(CONFIG_DIR, "recent_projects.json")

ws_manager = WSManager()
orchestrator = Orchestrator(ws_manager, CONFIG_DIR, WORKSPACE_DIR)

_VALID_MODES = {"manual", "workspace", "bypass"}


# ------------------------------------------------------------------ #
#  Project resolution helpers                                         #
# ------------------------------------------------------------------ #


def _project_or_404(project_id: str | None = None) -> Project:
    try:
        return orchestrator.get_project(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


def _migrate_legacy_recent_projects() -> None:
    """One-shot: convert old ``config/recent_projects.json`` into proper
    ``workspace/projects.json`` entries so v0.1.0 users keep their project
    history after upgrading.
    """
    if not os.path.isfile(LEGACY_RECENT_PROJECTS_FILE):
        return
    try:
        with open(LEGACY_RECENT_PROJECTS_FILE, "r", encoding="utf-8") as f:
            paths = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(paths, list):
        return
    for path in paths:
        if not isinstance(path, str) or not os.path.isdir(path):
            continue
        try:
            orchestrator.create_project(path)
        except ValueError as e:
            logger.info("Skipping legacy project %s: %s", path, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    orchestrator.load_roles()
    orchestrator.load_projects()

    # If no projects yet, migrate legacy recent_projects.json
    if not orchestrator.projects:
        _migrate_legacy_recent_projects()

    # Honor MULTIAGENT_PROJECT env var for headless / multi-instance use
    initial = os.environ.get("MULTIAGENT_PROJECT", "").strip()
    if initial and os.path.isdir(initial):
        try:
            orchestrator.create_project(initial)
        except ValueError as e:
            logger.warning("MULTIAGENT_PROJECT invalid: %s", e)

    logger.info(
        "MultiAgent backend started (projects=%d, active=%s)",
        len(orchestrator.projects), orchestrator.active_project_id,
    )
    yield
    logger.info("MultiAgent backend shutting down")


app = FastAPI(title="MultiAgent Studio", version="0.2.0", lifespan=lifespan)

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
#  Projects                                                           #
# ------------------------------------------------------------------ #


@app.get("/api/projects")
async def list_projects():
    return {
        "projects": [
            p.meta.model_dump(mode="json") for p in orchestrator.projects.values()
        ],
        "active": orchestrator.active_project_id,
    }


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest):
    try:
        project = orchestrator.create_project(req.project_dir, req.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return project.meta.model_dump(mode="json")


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    try:
        orchestrator.delete_project(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "active": orchestrator.active_project_id}


@app.put("/api/projects/active")
async def set_active_project(body: dict):
    pid = body.get("project_id")
    if not pid:
        raise HTTPException(400, "Missing project_id")
    try:
        orchestrator.set_active_project(pid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "active": pid}


# ------------------------------------------------------------------ #
#  Legacy single-project routes (resolve to active project)           #
# ------------------------------------------------------------------ #


@app.get("/api/project")
async def get_project_legacy():
    """Legacy endpoint returning the active project + history. Retained
    so older UI builds keep working during the v0.2.0 migration.
    """
    active = orchestrator.active_project_id
    if active is None:
        return {"path": "", "recent": []}
    project = orchestrator.projects[active]
    recent = [p.meta.project_dir for p in orchestrator.projects.values()]
    return {"path": project.project_dir, "recent": recent}


@app.put("/api/project")
async def set_project_legacy(body: dict):
    """Legacy endpoint that accepted a project path. Now creates or
    selects the corresponding project entry.
    """
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(400, "Missing path")
    try:
        project = orchestrator.create_project(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    recent = [p.meta.project_dir for p in orchestrator.projects.values()]
    return {"ok": True, "path": project.project_dir, "recent": recent}


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
#  Agents — both scoped and legacy                                    #
# ------------------------------------------------------------------ #


def _list_agents(project: Project) -> dict:
    return {
        "agents": {k: v.model_dump(mode="json") for k, v in project.agents.items()},
    }


@app.get("/api/agents")
async def list_agents_legacy():
    return _list_agents(_project_or_404())


@app.get("/api/projects/{project_id}/agents")
async def list_agents_scoped(project_id: str):
    return _list_agents(_project_or_404(project_id))


def _create_agent(project: Project, req: CreateAgentRequest) -> dict:
    try:
        agent = project.create_agent(req.role_id, req.agent_id)
        return agent.model_dump(mode="json")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents")
async def create_agent_legacy(req: CreateAgentRequest):
    return _create_agent(_project_or_404(), req)


@app.post("/api/projects/{project_id}/agents")
async def create_agent_scoped(project_id: str, req: CreateAgentRequest):
    return _create_agent(_project_or_404(project_id), req)


def _delete_agent(project: Project, agent_id: str) -> dict:
    try:
        project.delete_agent(agent_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/agents/{agent_id}")
async def delete_agent_legacy(agent_id: str):
    return _delete_agent(_project_or_404(), agent_id)


@app.delete("/api/projects/{project_id}/agents/{agent_id}")
async def delete_agent_scoped(project_id: str, agent_id: str):
    return _delete_agent(_project_or_404(project_id), agent_id)


def _clear_agents(project: Project) -> dict:
    """Stop + delete every agent in *project*. Returns the ids removed so
    the caller can reconcile UI state. Safe to call when the project is
    empty — returns an empty list."""
    ids = list(project.agents.keys())
    removed: list[str] = []
    for aid in ids:
        try:
            project.delete_agent(aid)
            removed.append(aid)
        except ValueError:
            # Already gone between list() and delete — fine.
            continue
    return {"ok": True, "removed": removed}


@app.delete("/api/agents")
async def clear_agents_legacy():
    return _clear_agents(_project_or_404())


@app.delete("/api/projects/{project_id}/agents")
async def clear_agents_scoped(project_id: str):
    return _clear_agents(_project_or_404(project_id))


def _start_agent(project: Project, agent_id: str, req: StartAgentRequest) -> dict:
    try:
        project.start_agent(agent_id, req.prompt, req.context_from)
        return {"ok": True, "agentId": agent_id}
    except BudgetExceeded as e:
        raise HTTPException(429, f"Budget exceeded ({e.reason}): {e.detail}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents/{agent_id}/start")
async def start_agent_legacy(agent_id: str, req: StartAgentRequest):
    return _start_agent(_project_or_404(), agent_id, req)


@app.post("/api/projects/{project_id}/agents/{agent_id}/start")
async def start_agent_scoped(project_id: str, agent_id: str, req: StartAgentRequest):
    return _start_agent(_project_or_404(project_id), agent_id, req)


async def _send_message(project: Project, agent_id: str, req: SendMessageRequest) -> dict:
    try:
        await project.send_user_message(agent_id, req.content)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents/{agent_id}/message")
async def send_message_legacy(agent_id: str, req: SendMessageRequest):
    return await _send_message(_project_or_404(), agent_id, req)


@app.post("/api/projects/{project_id}/agents/{agent_id}/message")
async def send_message_scoped(project_id: str, agent_id: str, req: SendMessageRequest):
    return await _send_message(_project_or_404(project_id), agent_id, req)


@app.post("/api/agents/{agent_id}/stop")
async def stop_agent_legacy(agent_id: str):
    _project_or_404().stop_agent(agent_id)
    return {"ok": True}


@app.post("/api/projects/{project_id}/agents/{agent_id}/stop")
async def stop_agent_scoped(project_id: str, agent_id: str):
    _project_or_404(project_id).stop_agent(agent_id)
    return {"ok": True}


@app.get("/api/agents/{agent_id}/context")
async def get_agent_context_legacy(agent_id: str):
    project = _project_or_404()
    return {"agentId": agent_id, "content": project.ctx.read(agent_id)}


@app.get("/api/projects/{project_id}/agents/{agent_id}/context")
async def get_agent_context_scoped(project_id: str, agent_id: str):
    project = _project_or_404(project_id)
    return {"agentId": agent_id, "content": project.ctx.read(agent_id)}


async def _compact_impl(project: Project, agent_id: str) -> dict:
    if agent_id not in project.agents:
        raise HTTPException(404, f"No such agent: {agent_id}")
    try:
        result = await compact_context(project.ctx, agent_id)
    except FileNotFoundError:
        raise HTTPException(404, f"No context file for {agent_id}")
    return {"ok": True, "agentId": agent_id, **result}


@app.post("/api/agents/{agent_id}/compact")
async def compact_agent_legacy(agent_id: str):
    return await _compact_impl(_project_or_404(), agent_id)


@app.post("/api/projects/{project_id}/agents/{agent_id}/compact")
async def compact_agent_scoped(project_id: str, agent_id: str):
    return await _compact_impl(_project_or_404(project_id), agent_id)


def _history_list_payload(project: Project, agent_id: str) -> dict:
    return {"agentId": agent_id, "entries": list_history(project.ctx, agent_id)}


@app.get("/api/agents/{agent_id}/history")
async def list_history_legacy(agent_id: str):
    return _history_list_payload(_project_or_404(), agent_id)


@app.get("/api/projects/{project_id}/agents/{agent_id}/history")
async def list_history_scoped(project_id: str, agent_id: str):
    return _history_list_payload(_project_or_404(project_id), agent_id)


def _history_read_payload(project: Project, agent_id: str, filename: str) -> dict:
    try:
        content = read_history(project.ctx, agent_id, filename)
    except FileNotFoundError:
        raise HTTPException(404, f"No such archive: {filename}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"agentId": agent_id, "filename": filename, "content": content}


@app.get("/api/agents/{agent_id}/history/{filename}")
async def read_history_legacy(agent_id: str, filename: str):
    return _history_read_payload(_project_or_404(), agent_id, filename)


@app.get("/api/projects/{project_id}/agents/{agent_id}/history/{filename}")
async def read_history_scoped(project_id: str, agent_id: str, filename: str):
    return _history_read_payload(_project_or_404(project_id), agent_id, filename)


def _stream_payload(project: Project, agent_id: str, limit: int) -> dict:
    entries = project.streams.tail(agent_id, limit=limit)
    return {
        "agentId": agent_id,
        "entries": [e.model_dump(mode="json") for e in entries],
    }


@app.get("/api/agents/{agent_id}/stream")
async def get_agent_stream_legacy(agent_id: str, limit: int = 500):
    return _stream_payload(_project_or_404(), agent_id, limit)


@app.get("/api/projects/{project_id}/agents/{agent_id}/stream")
async def get_agent_stream(project_id: str, agent_id: str, limit: int = 500):
    """Return the rolling tail of the agent's output stream.

    Useful for rehydrating the UI after a backend restart or a tab reload —
    the in-memory ``output_log`` only holds what's been emitted since
    startup, but the on-disk stream retains the last 500 entries.
    """
    return _stream_payload(_project_or_404(project_id), agent_id, limit)


# ------------------------------------------------------------------ #
#  Coordinator state                                                  #
# ------------------------------------------------------------------ #


def _coordinator_state_payload(project: Project) -> dict:
    path = project.coordinator_state_path
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        exists = True
    except FileNotFoundError:
        content = ""
        exists = False
    return {"path": path, "exists": exists, "content": content}


@app.get("/api/coordinator_state")
async def get_coordinator_state_legacy():
    return _coordinator_state_payload(_project_or_404())


@app.get("/api/projects/{project_id}/coordinator_state")
async def get_coordinator_state_scoped(project_id: str):
    return _coordinator_state_payload(_project_or_404(project_id))


def _coordinator_state_structured_payload(project: Project) -> dict:
    """Parsed YAML view of the coordinator's structured state.

    Tracks four blocks — facts, hypothesis, open_questions, decisions — per
    ``coordinator_state.py``. The raw MD scratchpad is at
    ``/coordinator_state`` (above); this endpoint is for the State tab of
    the frontend CoordinatorPanel.
    """
    from .coordinator_state import load_state, state_path

    state = load_state(project.workspace_dir)
    return {
        "path": state_path(project.workspace_dir),
        "version": state.version,
        "facts": [f.model_dump(mode="json") for f in state.facts],
        "hypothesis": state.hypothesis,
        "open_questions": list(state.open_questions),
        "decisions": [d.model_dump(mode="json") for d in state.decisions],
    }


@app.get("/api/coordinator_state/structured")
async def get_coordinator_state_structured_legacy():
    return _coordinator_state_structured_payload(_project_or_404())


@app.get("/api/projects/{project_id}/coordinator_state/structured")
async def get_coordinator_state_structured_scoped(project_id: str):
    return _coordinator_state_structured_payload(_project_or_404(project_id))


# ------------------------------------------------------------------ #
#  Errors                                                             #
# ------------------------------------------------------------------ #


def _errors_payload(project: Project, agent_id: str | None, limit: int) -> dict:
    items = project.errors.list(agent_id=agent_id, limit=limit)
    return {"errors": [e.model_dump(mode="json") for e in items]}


@app.get("/api/errors")
async def list_errors_legacy(agent_id: str | None = None, limit: int = 100):
    return _errors_payload(_project_or_404(), agent_id, limit)


@app.get("/api/projects/{project_id}/errors")
async def list_errors_scoped(project_id: str, agent_id: str | None = None, limit: int = 100):
    return _errors_payload(_project_or_404(project_id), agent_id, limit)


@app.delete("/api/errors")
async def clear_errors_legacy():
    _project_or_404().errors.clear()
    return {"ok": True}


@app.delete("/api/projects/{project_id}/errors")
async def clear_errors_scoped(project_id: str):
    _project_or_404(project_id).errors.clear()
    return {"ok": True}


# ------------------------------------------------------------------ #
#  Permissions                                                        #
# ------------------------------------------------------------------ #


@app.post("/api/permission")
async def resolve_permission(req: PermissionResponse):
    """Resolve a permission request. The request_id is project-agnostic —
    we try every project until we find the matching future.
    """
    for project in orchestrator.projects.values():
        if project.has_pending_permission(req.request_id):
            await project.resolve_permission(req.request_id, req.allow)
            return {"ok": True}
    return {"ok": True}  # silent no-op if already resolved


@app.get("/api/permission/mode")
async def get_permission_mode_legacy():
    project = _project_or_404()
    return {
        "mode": project.permission_mode,
        "agents": {aid: a.permission_mode for aid, a in project.agents.items()},
    }


@app.get("/api/projects/{project_id}/permission/mode")
async def get_permission_mode_scoped(project_id: str):
    project = _project_or_404(project_id)
    return {
        "mode": project.permission_mode,
        "agents": {aid: a.permission_mode for aid, a in project.agents.items()},
    }


async def _set_mode(project: Project, mode) -> dict:
    if mode not in _VALID_MODES:
        raise HTTPException(400, f"Invalid mode: {mode}")
    project.set_permission_mode(mode)  # type: ignore[arg-type]
    await project.broadcast_raw({
        "type": "permission_mode",
        "agent_id": "",
        "data": {"scope": "global", "mode": mode},
    })
    return {"ok": True, "mode": mode}


@app.put("/api/permission/mode")
async def set_permission_mode_legacy(body: dict):
    return await _set_mode(_project_or_404(), body.get("mode"))


@app.put("/api/projects/{project_id}/permission/mode")
async def set_permission_mode_scoped(project_id: str, body: dict):
    return await _set_mode(_project_or_404(project_id), body.get("mode"))


async def _set_agent_mode(project: Project, agent_id: str, mode) -> dict:
    if mode is not None and mode not in _VALID_MODES:
        raise HTTPException(400, f"Invalid mode: {mode}")
    try:
        project.set_agent_permission_mode(agent_id, mode)
    except ValueError as e:
        raise HTTPException(404, str(e))
    await project.broadcast_raw({
        "type": "permission_mode",
        "agent_id": agent_id,
        "data": {"scope": "agent", "agent_id": agent_id, "mode": mode},
    })
    return {"ok": True, "agent_id": agent_id, "mode": mode}


@app.put("/api/agents/{agent_id}/permission_mode")
async def set_agent_permission_mode_legacy(agent_id: str, body: dict):
    return await _set_agent_mode(_project_or_404(), agent_id, body.get("mode"))


@app.put("/api/projects/{project_id}/agents/{agent_id}/permission_mode")
async def set_agent_permission_mode_scoped(project_id: str, agent_id: str, body: dict):
    return await _set_agent_mode(_project_or_404(project_id), agent_id, body.get("mode"))


# ------------------------------------------------------------------ #
#  Pipeline                                                           #
# ------------------------------------------------------------------ #


@app.post("/api/pipeline/start")
async def start_pipeline_legacy(req: StartPipelineRequest):
    import asyncio
    asyncio.create_task(orchestrator.run_pipeline(None, req.requirement, req.stages))
    return {"ok": True, "message": "Pipeline started"}


@app.post("/api/projects/{project_id}/pipeline/start")
async def start_pipeline_scoped(project_id: str, req: StartPipelineRequest):
    import asyncio
    asyncio.create_task(orchestrator.run_pipeline(project_id, req.requirement, req.stages))
    return {"ok": True, "message": "Pipeline started"}


async def _resume_impl(project: Project, body: dict) -> dict:
    action = str(body.get("action") or "").strip()
    try:
        return await orchestrator.resume_pipeline(project.id, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/pipeline/resume")
async def resume_pipeline_legacy(body: dict):
    return await _resume_impl(_project_or_404(), body)


@app.post("/api/projects/{project_id}/pipeline/resume")
async def resume_pipeline_scoped(project_id: str, body: dict):
    return await _resume_impl(_project_or_404(project_id), body)


async def _approve_impl(project: Project, body: dict) -> dict:
    """User-initiated force-approve of the currently reviewed stage.

    Used by the frontend's "Override & continue" button on a blocker
    notification and by manual "skip gate" UI actions. Records the decision
    as a ``coordinator_decision`` WS event so the decisions log surfaces the
    user override.
    """
    summary = str(body.get("summary") or "").strip()
    try:
        return await orchestrator.approve_stage_from_user(project.id, summary)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/pipeline/approve")
async def approve_pipeline_legacy(body: dict):
    return await _approve_impl(_project_or_404(), body)


@app.post("/api/projects/{project_id}/pipeline/approve")
async def approve_pipeline_scoped(project_id: str, body: dict):
    return await _approve_impl(_project_or_404(project_id), body)


# ------------------------------------------------------------------ #
#  Notifications (Track B)                                            #
# ------------------------------------------------------------------ #


def _parse_since(raw: str | None):
    """Parse an ISO-8601 ``since`` query string into an aware datetime.

    Accepts the common ``...Z`` suffix as well as explicit ``+00:00``.  On
    empty or malformed input returns ``None`` — callers treat that as "no
    filter" rather than error-ing out the request (a stale client clock
    shouldn't break replay).
    """
    from datetime import datetime, timezone

    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _list_notifications(project: Project, since: str | None, limit: int) -> dict:
    from .notifications import read_notifications

    items = read_notifications(
        project.workspace_dir,
        since=_parse_since(since),
        limit=max(1, min(limit, 500)),
    )
    return {
        "notifications": [
            {
                "id": e.id,
                "level": e.level,
                "message": e.message,
                "action_required": e.action_required,
                "timestamp": e.ts.isoformat(),
            }
            for e in items
        ]
    }


@app.get("/api/notifications")
async def list_notifications_legacy(since: str | None = None, limit: int = 100):
    return await _list_notifications(_project_or_404(), since, limit)


@app.get("/api/projects/{project_id}/notifications")
async def list_notifications_scoped(
    project_id: str, since: str | None = None, limit: int = 100,
):
    return await _list_notifications(_project_or_404(project_id), since, limit)


# ------------------------------------------------------------------ #
#  Workflow (declarative pipeline)                                    #
# ------------------------------------------------------------------ #


def _workflow_payload(project: Project) -> dict:
    path = workflow_path(project.workspace_dir)
    wf = load_workflow(project.workspace_dir)
    if wf is None:
        return {"path": path, "exists": False, "workflow": None}
    return {"path": path, "exists": True, "workflow": wf.model_dump()}


def _save_workflow_from_body(project: Project, body: dict) -> dict:
    try:
        wf = Workflow(**body)
    except Exception as exc:  # noqa: BLE001 — pydantic + ad-hoc input
        raise HTTPException(400, f"Invalid workflow: {exc}")
    path = save_workflow(project.workspace_dir, wf)
    return {"ok": True, "path": path, "workflow": wf.model_dump()}


@app.get("/api/workflow")
async def get_workflow_legacy():
    return _workflow_payload(_project_or_404())


@app.get("/api/projects/{project_id}/workflow")
async def get_workflow_scoped(project_id: str):
    return _workflow_payload(_project_or_404(project_id))


@app.put("/api/workflow")
async def put_workflow_legacy(body: dict):
    return _save_workflow_from_body(_project_or_404(), body)


@app.put("/api/projects/{project_id}/workflow")
async def put_workflow_scoped(project_id: str, body: dict):
    return _save_workflow_from_body(_project_or_404(project_id), body)


@app.delete("/api/workflow")
async def delete_workflow_legacy():
    deleted = delete_workflow(_project_or_404().workspace_dir)
    return {"ok": True, "deleted": deleted}


@app.delete("/api/projects/{project_id}/workflow")
async def delete_workflow_scoped(project_id: str):
    deleted = delete_workflow(_project_or_404(project_id).workspace_dir)
    return {"ok": True, "deleted": deleted}


# Raw YAML access for the WorkflowEditor UI. We keep this separate from
# the structured GET/PUT above so the editor can round-trip comments and
# key ordering that Pydantic's model_dump would flatten away.


def _workflow_raw_payload(project: Project) -> dict:
    path = workflow_path(project.workspace_dir)
    if not os.path.isfile(path):
        return {"path": path, "exists": False, "content": ""}
    with open(path, "r", encoding="utf-8") as f:
        return {"path": path, "exists": True, "content": f.read()}


def _save_workflow_from_raw(project: Project, body: dict) -> dict:
    import yaml as _yaml  # local import — rarely used

    content = str(body.get("content") or "")
    if not content.strip():
        raise HTTPException(400, "workflow YAML content is empty")
    try:
        parsed = _yaml.safe_load(content)
    except _yaml.YAMLError as exc:
        raise HTTPException(400, f"YAML parse error: {exc}")
    if not isinstance(parsed, dict):
        raise HTTPException(400, "workflow YAML must be a mapping at the top level")
    try:
        wf = Workflow(**parsed)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Invalid workflow: {exc}")
    # Persist the user's raw text (preserves comments + ordering) then
    # round-trip once through the validator path so we know the loader
    # will accept it on next startup.
    os.makedirs(project.workspace_dir, exist_ok=True)
    path = workflow_path(project.workspace_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    # Sanity-check that the file round-trips — if it doesn't, we emit a
    # warning but leave the file in place; load_workflow tolerates bad
    # files by returning None so the user can see the error in the UI.
    reloaded = load_workflow(project.workspace_dir)
    return {
        "ok": True,
        "path": path,
        "workflow": wf.model_dump(),
        "reloaded_ok": reloaded is not None,
    }


@app.get("/api/workflow/raw")
async def get_workflow_raw_legacy():
    return _workflow_raw_payload(_project_or_404())


@app.get("/api/projects/{project_id}/workflow/raw")
async def get_workflow_raw_scoped(project_id: str):
    return _workflow_raw_payload(_project_or_404(project_id))


@app.put("/api/workflow/raw")
async def put_workflow_raw_legacy(body: dict):
    return _save_workflow_from_raw(_project_or_404(), body)


@app.put("/api/projects/{project_id}/workflow/raw")
async def put_workflow_raw_scoped(project_id: str, body: dict):
    return _save_workflow_from_raw(_project_or_404(project_id), body)


# ------------------------------------------------------------------ #
#  Budget                                                             #
# ------------------------------------------------------------------ #


def _budget_payload(project: Project) -> dict:
    return project.budget.snapshot()


@app.get("/api/budget")
async def get_budget_legacy():
    return _budget_payload(_project_or_404())


@app.get("/api/projects/{project_id}/budget")
async def get_budget_scoped(project_id: str):
    return _budget_payload(_project_or_404(project_id))


@app.post("/api/budget/reset")
async def reset_budget_legacy():
    _project_or_404().budget.reset()
    return {"ok": True}


@app.post("/api/projects/{project_id}/budget/reset")
async def reset_budget_scoped(project_id: str):
    _project_or_404(project_id).budget.reset()
    return {"ok": True}


# ------------------------------------------------------------------ #
#  Static frontend (production / Docker)                              #
# ------------------------------------------------------------------ #

FRONTEND_DIST = os.path.join(os.path.dirname(BASE_DIR), "frontend", "dist")
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")
