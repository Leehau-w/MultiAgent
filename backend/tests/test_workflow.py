"""Smoke tests for declarative pipelines in ``workspace/<slug>/workflow.yaml``.

Covers the loader/saver roundtrip, pydantic validation on malformed
input, and the orchestrator's fallback chain (explicit stages → loaded
workflow → hardcoded default).
"""

from __future__ import annotations

import os

import pytest
import yaml

from app.models import PipelineStage
from app.orchestrator import Orchestrator
from app.workflow import (
    Workflow,
    delete_workflow,
    load_workflow,
    save_workflow,
    workflow_path,
)


# ------------------------------------------------------------------ #
#  Loader / saver                                                     #
# ------------------------------------------------------------------ #


def test_load_returns_none_when_missing(tmp_path):
    assert load_workflow(str(tmp_path)) is None


def test_save_and_load_roundtrip(tmp_path):
    wf = Workflow(
        stages=[
            PipelineStage(name="analysis", agents=["pm"]),
            PipelineStage(name="impl", agents=["developer", "developer"], parallel=True),
        ]
    )
    path = save_workflow(str(tmp_path), wf)
    assert os.path.isfile(path)

    loaded = load_workflow(str(tmp_path))
    assert loaded is not None
    assert loaded.version == 1
    assert [s.name for s in loaded.stages] == ["analysis", "impl"]
    assert loaded.stages[1].parallel is True
    assert loaded.stages[1].agents == ["developer", "developer"]


def test_load_rejects_empty_stages(tmp_path):
    # An empty stages list must be rejected — running an empty pipeline
    # would silently succeed without doing anything, which is worse than
    # loudly falling back to the default.
    path = workflow_path(str(tmp_path))
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"stages": []}, f)
    assert load_workflow(str(tmp_path)) is None


def test_load_rejects_stage_without_agents(tmp_path):
    path = workflow_path(str(tmp_path))
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"stages": [{"name": "x", "agents": []}]}, f)
    assert load_workflow(str(tmp_path)) is None


def test_load_ignores_garbage_yaml(tmp_path):
    path = workflow_path(str(tmp_path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(": not yaml\n  indented: but broken: :\n")
    assert load_workflow(str(tmp_path)) is None


def test_load_ignores_non_mapping(tmp_path):
    # Someone hand-writes a bare list instead of a mapping — we should
    # ignore it rather than crash deep in pydantic.
    path = workflow_path(str(tmp_path))
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(["pm", "td"], f)
    assert load_workflow(str(tmp_path)) is None


def test_save_is_atomic_no_tmp_left_behind(tmp_path):
    wf = Workflow(stages=[PipelineStage(name="s", agents=["pm"])])
    save_workflow(str(tmp_path), wf)
    # The atomic write stages to ``workflow.yaml.tmp`` and renames.
    # After a successful save the tmp file must be gone.
    assert not os.path.exists(workflow_path(str(tmp_path)) + ".tmp")


def test_delete_removes_file(tmp_path):
    wf = Workflow(stages=[PipelineStage(name="s", agents=["pm"])])
    save_workflow(str(tmp_path), wf)
    assert delete_workflow(str(tmp_path)) is True
    assert delete_workflow(str(tmp_path)) is False  # second call is a no-op


# ------------------------------------------------------------------ #
#  Orchestrator fallback chain                                        #
# ------------------------------------------------------------------ #


def test_orchestrator_uses_workflow_yaml_when_present(tmp_path, roles, monkeypatch):
    """If ``workflow.yaml`` exists and stages aren't passed explicitly,
    ``run_pipeline`` should load and use it."""
    from app.ws_manager import WSManager

    ws = WSManager()
    orch = Orchestrator(ws, str(tmp_path / "cfg"), str(tmp_path / "workspace"))
    orch.roles = roles
    # create_project checks the dir exists, so make one under tmp_path.
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    project = orch.create_project(str(code_dir), name="demo")

    custom = Workflow(stages=[PipelineStage(name="only", agents=["pm"])])
    save_workflow(project.workspace_dir, custom)

    # Intercept the pipeline early — we only care which stages it picked up.
    captured: dict = {}

    async def fake_broadcast(payload):
        if "stages" in payload.get("data", {}):
            captured["stages"] = payload["data"]["stages"]
        # Raise to abort before the orchestrator tries to create agents
        # for stages whose roles may not exist.
        raise RuntimeError("abort after first broadcast")

    monkeypatch.setattr(project, "broadcast_raw", fake_broadcast)

    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(orch.run_pipeline(project.id, "build it"))

    assert captured["stages"] == [
        {"name": "only", "agents": ["pm"], "parallel": False},
    ]


def test_orchestrator_falls_back_to_default_without_yaml(tmp_path, roles, monkeypatch):
    from app.ws_manager import WSManager

    ws = WSManager()
    orch = Orchestrator(ws, str(tmp_path / "cfg"), str(tmp_path / "workspace"))
    orch.roles = roles
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    project = orch.create_project(str(code_dir), name="demo")

    captured: dict = {}

    async def fake_broadcast(payload):
        if "stages" in payload.get("data", {}):
            captured["stages"] = payload["data"]["stages"]
        raise RuntimeError("abort after first broadcast")

    monkeypatch.setattr(project, "broadcast_raw", fake_broadcast)

    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(orch.run_pipeline(project.id, "build it"))

    names = [s["name"] for s in captured["stages"]]
    assert names == ["analysis", "design", "implementation", "review"]


def test_orchestrator_explicit_stages_win_over_yaml(tmp_path, roles, monkeypatch):
    """Explicit ``stages=[...]`` must not be overridden by workflow.yaml —
    the API caller should always be able to one-shot a pipeline."""
    from app.ws_manager import WSManager

    ws = WSManager()
    orch = Orchestrator(ws, str(tmp_path / "cfg"), str(tmp_path / "workspace"))
    orch.roles = roles
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    project = orch.create_project(str(code_dir), name="demo")

    # Persisted yaml says one thing...
    save_workflow(
        project.workspace_dir,
        Workflow(stages=[PipelineStage(name="fromyaml", agents=["pm"])]),
    )
    # ...but the caller passes another.
    explicit = [PipelineStage(name="explicit", agents=["td"])]

    captured: dict = {}

    async def fake_broadcast(payload):
        if "stages" in payload.get("data", {}):
            captured["stages"] = payload["data"]["stages"]
        raise RuntimeError("abort after first broadcast")

    monkeypatch.setattr(project, "broadcast_raw", fake_broadcast)

    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(orch.run_pipeline(project.id, "build it", stages=explicit))

    assert [s["name"] for s in captured["stages"]] == ["explicit"]
