from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime

import yaml

from .models import (
    AgentRole,
    AgentStatus,
    PermissionMode,
    PipelineStage,
    ProjectMeta,
)
from .project import Project
from .workflow import load_workflow
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(name: str) -> str:
    """Make a filesystem-safe slug from a folder name. Empty → 'project'."""
    s = _SLUG_RE.sub("-", name).strip("-")
    return s or "project"


class _PipelineAborted(Exception):
    """Internal sentinel — the coordinator called ``mark_done("ABORT: ...")``.

    Raised by :meth:`Orchestrator._run_stage_with_gate` so ``run_pipeline``
    can mark the run as ``failed`` (not ``error``) with the coord's reason.
    """

    def __init__(self, stage_name: str, reason: str) -> None:
        super().__init__(reason)
        self.stage_name = stage_name
        self.reason = reason


def _append_user_override_decision(
    workspace_dir: str, decision: str, rationale: str,
) -> None:
    """Append a ``user_override`` entry to ``coordinator_state.yaml`` so the
    decisions log shows force-advances alongside the coord's own choices.

    Failure is logged and swallowed — a read-only filesystem should not
    prevent the pipeline from resuming.
    """
    from .coordinator_state import (
        DecisionEntry,
        StateUpdate,
        apply_update,
        load_state,
        save_state,
    )

    try:
        state = load_state(workspace_dir)
        new_state = apply_update(
            state,
            StateUpdate(
                decisions_append=[
                    DecisionEntry(
                        decision=f"[user_override] {decision}",
                        rationale=rationale,
                    )
                ],
            ),
        )
        save_state(workspace_dir, new_state)
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logger.warning(
            "Could not persist user_override decision to %s: %s",
            workspace_dir, exc,
        )


class Orchestrator:
    """Roles + projects registry. Each project runs independently; the
    orchestrator is just a directory of projects + a shared role pool.
    """

    def __init__(
        self,
        ws: WSManager,
        config_dir: str,
        workspace_root: str,
    ) -> None:
        self.ws = ws
        self.config_dir = config_dir
        self.workspace_root = workspace_root
        os.makedirs(workspace_root, exist_ok=True)

        self.roles: dict[str, AgentRole] = {}
        self.projects: dict[str, Project] = {}
        self.active_project_id: str | None = None

        self._projects_file = os.path.join(workspace_root, "projects.json")

    # ------------------------------------------------------------------ #
    #  Role management                                                    #
    # ------------------------------------------------------------------ #

    def load_roles(self, path: str | None = None) -> dict[str, AgentRole]:
        if path is None:
            path = os.path.join(self.config_dir, "roles.yaml")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # Mutate in place so existing Project instances (which hold a
        # reference to ``self.roles``) see the refreshed role definitions.
        self.roles.clear()
        for rid, rdata in data.get("roles", {}).items():
            self.roles[rid] = AgentRole(id=rid, **rdata)
        logger.info("Loaded %d roles from %s", len(self.roles), path)
        return self.roles

    def get_roles_yaml(self) -> str:
        path = os.path.join(self.config_dir, "roles.yaml")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def save_roles_yaml(self, content: str) -> None:
        path = os.path.join(self.config_dir, "roles.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.load_roles(path)

    # ------------------------------------------------------------------ #
    #  Projects                                                           #
    # ------------------------------------------------------------------ #

    def load_projects(self) -> None:
        """Read ``workspace/projects.json`` and rehydrate Project instances.
        Called once on startup.
        """
        if not os.path.isfile(self._projects_file):
            return
        try:
            with open(self._projects_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read %s: %s", self._projects_file, e)
            return

        for entry in data.get("projects", []):
            try:
                meta = ProjectMeta(**entry)
                project = Project(
                    meta=meta, ws=self.ws, roles=self.roles,
                    workspace_root=self.workspace_root,
                )
                project.rehydrate()
                self.projects[meta.id] = project
            except Exception as e:
                logger.warning("Skipping malformed project entry %r: %s", entry, e)
        self.active_project_id = data.get("active")
        if self.active_project_id and self.active_project_id not in self.projects:
            self.active_project_id = next(iter(self.projects), None)
        logger.info(
            "Loaded %d projects (active=%s)",
            len(self.projects), self.active_project_id,
        )

    def _save_projects(self) -> None:
        data = {
            "projects": [p.meta.model_dump(mode="json") for p in self.projects.values()],
            "active": self.active_project_id,
        }
        os.makedirs(os.path.dirname(self._projects_file), exist_ok=True)
        tmp = self._projects_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, self._projects_file)

    def _unique_slug(self, base: str) -> str:
        slug = _slugify(base)
        if slug not in self.projects:
            return slug
        i = 2
        while f"{slug}-{i}" in self.projects:
            i += 1
        return f"{slug}-{i}"

    def create_project(self, project_dir: str, name: str | None = None) -> Project:
        project_dir = os.path.normpath(project_dir)
        if not os.path.isdir(project_dir):
            raise ValueError(f"Directory does not exist: {project_dir}")

        # If the same project_dir is already registered, return it instead of duplicating
        for p in self.projects.values():
            if os.path.normpath(p.meta.project_dir) == project_dir:
                self.active_project_id = p.id
                self._save_projects()
                return p

        slug = self._unique_slug(os.path.basename(project_dir))
        meta = ProjectMeta(
            id=slug,
            name=name or slug,
            project_dir=project_dir,
            created_at=datetime.now(),
        )
        project = Project(
            meta=meta, ws=self.ws, roles=self.roles,
            workspace_root=self.workspace_root,
        )
        self.projects[slug] = project
        self.active_project_id = slug
        self._save_projects()
        logger.info("Created project %s at %s", slug, project_dir)
        return project

    def delete_project(self, project_id: str) -> None:
        project = self.projects.get(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        for aid in list(project.agents.keys()):
            project.stop_agent(aid)
        self.projects.pop(project_id, None)
        if self.active_project_id == project_id:
            self.active_project_id = next(iter(self.projects), None)
        self._save_projects()
        logger.info("Deleted project %s", project_id)

    def set_active_project(self, project_id: str) -> None:
        if project_id not in self.projects:
            raise ValueError(f"Unknown project: {project_id}")
        self.active_project_id = project_id
        self._save_projects()

    def get_project(self, project_id: str | None = None) -> Project:
        """Resolve a project by id, or return the active one if id is None."""
        pid = project_id or self.active_project_id
        if pid is None:
            raise ValueError("No project selected")
        project = self.projects.get(pid)
        if project is None:
            raise ValueError(f"Unknown project: {pid}")
        return project

    # ------------------------------------------------------------------ #
    #  Pipeline execution (scoped to one project)                         #
    # ------------------------------------------------------------------ #

    async def run_pipeline(
        self,
        project_id: str | None,
        requirement: str,
        stages: list[PipelineStage] | None = None,
    ) -> None:
        project = self.get_project(project_id)
        wf = load_workflow(project.workspace_dir)
        if stages is None:
            stages = wf.stages if wf is not None else self._default_pipeline()

        # Reset per-run pipeline state so a second run never inherits a
        # stale gate verdict or retry counter from the previous run.
        pipeline = project.reset_pipeline()

        # Auto-spawn coordinator when workflow.coordinator.enabled is true.
        # Idempotent: if a prior run or user action already created a
        # ``coord`` agent, we reuse it. The coordinator drives every
        # stage-gate decision from here onwards.
        coord_enabled = (
            wf is not None
            and wf.coordinator is not None
            and wf.coordinator.enabled
        )
        coord_role_id = (
            wf.coordinator.role_id
            if (wf is not None and wf.coordinator is not None)
            else None
        )
        # Default cap of 3 is applied only when a stage-gate run is
        # active — legacy ungated pipelines treat "no cap" as "no cap"
        # (they never RETRY anyway). Explicit None in budget = unlimited.
        raw_max_retries = (
            wf.budget.max_stage_retries
            if (wf is not None and wf.budget is not None)
            else None
        )
        max_stage_retries = raw_max_retries if raw_max_retries is not None else 3
        # Gate-verdict wall-clock cap. Default 600 s (10 min) applies when
        # stage-gate is active; a workflow can disable it by explicitly
        # setting ``gate_timeout_seconds: 0``.
        raw_gate_timeout = (
            wf.budget.gate_timeout_seconds
            if (wf is not None and wf.budget is not None)
            else None
        )
        if raw_gate_timeout is None:
            gate_timeout: float | None = 600.0
        elif raw_gate_timeout <= 0:
            gate_timeout = None
        else:
            gate_timeout = float(raw_gate_timeout)
        if coord_enabled and coord_role_id:
            existing = project.agents.get("coord")
            if existing is None:
                try:
                    project.create_agent(role_id=coord_role_id, agent_id="coord")
                except ValueError as exc:
                    logger.warning(
                        "auto-spawn coordinator failed (role=%s): %s",
                        coord_role_id, exc,
                    )
                    coord_enabled = False
            elif existing.role_id != coord_role_id:
                logger.warning(
                    "existing 'coord' agent has role=%s but workflow expects %s; "
                    "stage gate disabled for this run",
                    existing.role_id, coord_role_id,
                )
                coord_enabled = False
            if coord_enabled:
                pipeline.coordinator_agent_id = "coord"

        await project.broadcast_raw({
            "type": "pipeline_status",
            "data": {
                "status": "running",
                "requirement": requirement[:200],
                "stages": [s.model_dump(exclude_none=True) for s in stages],
                "current_stage": 0,
            },
        })

        # Spawn every stage's workers up-front (matches prior behavior so
        # the UI sees the full roster before stage 1 starts).
        stage_agents: list[list[str]] = []
        try:
            for stage in stages:
                agent_ids: list[str] = []
                for role_id in stage.agents:
                    aid = project.create_agent(role_id).id
                    agent_ids.append(aid)
                stage_agents.append(agent_ids)
        except Exception as exc:
            for ids in stage_agents:
                for aid in ids:
                    try:
                        project.delete_agent(aid)
                    except ValueError:
                        pass
            await project.broadcast_raw({
                "type": "pipeline_status",
                "data": {"status": "error", "error": f"setup failed: {exc}"},
            })
            raise

        # With workers registered, notify the coord that we're starting.
        if pipeline.coordinator_agent_id:
            await project._send_pipeline_started(
                pipeline.coordinator_agent_id,
                requirement,
                [s.name for s in stages],
            )

        pipeline_failed = False
        try:
            for i, (stage, agent_ids) in enumerate(zip(stages, stage_agents)):
                await project.broadcast_raw({
                    "type": "pipeline_status",
                    "data": {
                        "status": "running",
                        "current_stage": i,
                        "stage_name": stage.name,
                    },
                })

                prior_ids = [aid for aids in stage_agents[:i] for aid in aids]

                if i == 0:
                    stage_prompt = requirement
                else:
                    stage_prompt = (
                        f"Based on the previous agents' work (see their context documents), "
                        f"continue with the following requirement:\n\n{requirement}"
                    )

                stage_failed = await self._run_stage_with_gate(
                    project=project,
                    pipeline=pipeline,
                    stage=stage,
                    agent_ids=agent_ids,
                    prior_ids=prior_ids,
                    stage_prompt=stage_prompt,
                    max_stage_retries=max_stage_retries,
                    gate_timeout=gate_timeout,
                )
                if stage_failed:
                    pipeline_failed = True

            status = "error" if pipeline_failed else "completed"
            await project.broadcast_raw({
                "type": "pipeline_status",
                "data": {"status": status, "current_stage": len(stages)},
            })
        except _PipelineAborted as abort:
            logger.info(
                "Pipeline aborted at stage %s: %s",
                abort.stage_name, abort.reason,
            )
            await project.broadcast_raw({
                "type": "pipeline_status",
                "data": {
                    "status": "failed",
                    "stage_name": abort.stage_name,
                    "reason": abort.reason,
                },
            })
        except asyncio.CancelledError:
            for ids in stage_agents:
                for aid in ids:
                    project.stop_agent(aid)
            await project.broadcast_raw({
                "type": "pipeline_status",
                "data": {"status": "error", "error": "pipeline cancelled"},
            })
            raise
        except Exception as exc:
            logger.exception("Pipeline failed")
            await project.broadcast_raw({
                "type": "pipeline_status",
                "data": {"status": "error", "error": str(exc)},
            })

    async def _run_stage_with_gate(
        self,
        *,
        project: "Project",
        pipeline: "PipelineState",
        stage: PipelineStage,
        agent_ids: list[str],
        prior_ids: list[str],
        stage_prompt: str,
        max_stage_retries: int | None,
        gate_timeout: float | None = None,
    ) -> bool:
        """Run one stage, then loop review/rework until the coord APPROVEs.

        Returns ``True`` if any agent's run raised an exception (pipeline
        should be marked failed at the end), ``False`` otherwise. Does not
        swallow ``CancelledError`` — the caller handles stop semantics.

        When no coordinator is active (``pipeline.coordinator_agent_id`` is
        None), the stage runs exactly once with no gate — preserves v0.2.0
        behaviour for legacy / ungated pipelines.

        ``max_stage_retries`` caps how many times a RETRY verdict can push
        us back into the rework path. When the cap is reached, further
        RETRY verdicts are refused with a [STAGE_RETRY_EXHAUSTED] message
        and the loop waits for APPROVE (or pipeline abort / user override).

        ``gate_timeout`` is a wall-clock ceiling (seconds) on how long we
        wait for a single verdict.  On timeout we flip the pipeline into
        the pause path with a clear reason instead of blocking forever.
        """
        coord_id = pipeline.coordinator_agent_id
        stage_name = stage.name
        targets = list(agent_ids)
        # When True, the next iteration skips re-running agents and only
        # waits for a fresh verdict (coord previously asked for another
        # retry after the budget was exhausted).
        awaiting_post_exhaustion = False

        stage_failed = False
        while True:
            if not awaiting_post_exhaustion:
                if stage.parallel:
                    tasks = [
                        project._run_agent(aid, stage_prompt, context_from=prior_ids)
                        for aid in targets
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for aid, result in zip(targets, results):
                        if isinstance(result, asyncio.CancelledError):
                            raise result
                        if isinstance(result, BaseException):
                            logger.error(
                                "Pipeline stage %s agent %s failed: %s",
                                stage_name, aid, result,
                            )
                            stage_failed = True
                else:
                    for aid in targets:
                        await project._run_agent(
                            aid, stage_prompt, context_from=prior_ids,
                        )

            if coord_id is None:
                return stage_failed

            # Ask the coord to review this stage.
            pipeline.current_stage_name = stage_name
            pipeline.gate_verdict = None
            pipeline.gate_verdict_ready.clear()
            retries_so_far = pipeline.stage_retries.get(stage_name, 0)
            budget_exhausted = (
                max_stage_retries is not None
                and retries_so_far >= max_stage_retries
            )
            if budget_exhausted:
                await project._send_stage_retry_exhausted(
                    coord_id, stage_name, retries_so_far,
                    max_stage_retries or 0,
                )
            else:
                await project._send_stage_complete(
                    coord_id,
                    stage_name,
                    agent_ids,
                    acceptance_criteria=stage.acceptance_criteria,
                    retries_so_far=retries_so_far,
                    max_retries=max_stage_retries or 0,
                )
            await project.ws.stage_gate_review_started(project.id, stage_name)

            try:
                if gate_timeout is not None:
                    await asyncio.wait_for(
                        pipeline.gate_verdict_ready.wait(),
                        timeout=gate_timeout,
                    )
                else:
                    await pipeline.gate_verdict_ready.wait()
            except asyncio.TimeoutError:
                # Coord never responded. Promote to a pause so the user
                # can decide (retry / force_advance) instead of wedging.
                pipeline.pause_reason = (
                    f"Coordinator did not return a verdict within "
                    f"{int(gate_timeout or 0)}s for stage {stage_name!r}."
                )
                pipeline.gate_verdict_ready.set()
            if pipeline.cancelled:
                # reset_pipeline() retired this PipelineState while we
                # were waiting — abandon the loop and let the new run
                # take over.
                return stage_failed
            verdict = pipeline.gate_verdict
            pipeline.gate_verdict = None
            pipeline.gate_verdict_ready.clear()

            # Pause path — coord crashed or user requested a pause.
            # Broadcast pipeline_paused and wait for the user's resume.
            if pipeline.pause_reason is not None:
                reason = pipeline.pause_reason
                logger.info(
                    "[%s] pipeline paused at stage %s: %s",
                    project.id, stage_name, reason[:120],
                )
                await project.broadcast_raw({
                    "type": "pipeline_paused",
                    "data": {
                        "project_id": project.id,
                        "stage_name": stage_name,
                        "reason": reason,
                    },
                })

                # Wait for /pipeline/resume → sets resume_action + fires event
                pipeline.resume_ready.clear()
                await pipeline.resume_ready.wait()
                if pipeline.cancelled:
                    return stage_failed
                action = pipeline.resume_action
                pipeline.pause_reason = None
                pipeline.resume_action = None
                pipeline.resume_ready.clear()

                await project.broadcast_raw({
                    "type": "pipeline_resumed",
                    "data": {
                        "project_id": project.id,
                        "stage_name": stage_name,
                        "action": action or "retry",
                    },
                })

                if action == "force_advance":
                    await project.ws.stage_gate_resolved(
                        project.id, stage_name, "APPROVE",
                        "User force-advanced past gate (resume)",
                    )
                    return stage_failed

                # "retry" (default) — reset the coord if it's in error so
                # send_message can restart it, then re-emit STAGE_COMPLETE
                # on the next loop iteration.
                if coord_id:
                    coord_agent = project.agents.get(coord_id)
                    if coord_agent and coord_agent.status == AgentStatus.ERROR:
                        coord_agent.status = AgentStatus.IDLE
                awaiting_post_exhaustion = True  # don't re-run stage agents
                continue

            if verdict is None:
                logger.warning(
                    "[%s] gate verdict missing after event fired; treating as APPROVE",
                    project.id,
                )
                await project.ws.stage_gate_resolved(
                    project.id, stage_name, "APPROVE",
                    "auto-approved (missing verdict)",
                )
                return stage_failed

            await project.ws.stage_gate_resolved(
                project.id, stage_name, verdict.action, verdict.summary,
            )

            if verdict.action == "APPROVE":
                return stage_failed

            if verdict.action == "ABORT":
                # Coordinator signalled unrecoverable failure — propagate
                # via a sentinel exception so ``run_pipeline`` can mark the
                # whole pipeline as failed and surface the reason to the
                # user.  Summary carries the ``ABORT: ...`` reason.
                raise _PipelineAborted(stage_name, verdict.summary)

            if budget_exhausted:
                # Coord tried to RETRY beyond the budget — refuse, stay
                # in review-only mode, loop to wait for a better verdict.
                logger.warning(
                    "[%s] coord requested rework after retry budget "
                    "exhausted (stage=%s retries=%d/%s); refusing",
                    project.id, stage_name, retries_so_far, max_stage_retries,
                )
                awaiting_post_exhaustion = True
                continue

            # RETRY — re-run only the agents the coord listed. Unknown ids
            # are silently dropped (coord may name an agent outside the
            # stage in error; we don't want to surprise it by running a
            # worker from a prior stage).
            #
            # Just-spawned agents from ``spawn_and_rework`` join the stage
            # roster so subsequent STAGE_COMPLETE messages include them.
            for new_aid in verdict.spawned_agents:
                if new_aid in project.agents and new_aid not in agent_ids:
                    agent_ids.append(new_aid)
            rework_targets = [aid for aid in verdict.agents if aid in agent_ids]
            if not rework_targets:
                logger.warning(
                    "[%s] rework requested with no valid agents (got %s, stage has %s); "
                    "auto-approving to avoid loop",
                    project.id, verdict.agents, agent_ids,
                )
                return stage_failed

            pipeline.stage_retries[stage_name] = retries_so_far + 1
            targets = rework_targets
            stage_prompt = (
                f"{stage_prompt}\n\n--- Rework instruction from coordinator ---\n"
                f"{verdict.instruction}"
            )
            stage_failed = False  # fresh attempt
            awaiting_post_exhaustion = False

    # ------------------------------------------------------------------ #
    #  Pause / resume                                                      #
    # ------------------------------------------------------------------ #

    async def resume_pipeline(
        self, project_id: str | None, action: str,
    ) -> dict[str, str]:
        """Un-pause a paused pipeline with the user's chosen action.

        Called by the ``POST /api/projects/{slug}/pipeline/resume`` endpoint.
        ``action`` must be ``"retry"`` (re-invoke the coord with the same
        [STAGE_COMPLETE]) or ``"force_advance"`` (synthesize APPROVE and
        advance).  Refuses when no pause is actually pending.
        """
        project = self.get_project(project_id)
        pipeline = project.pipeline
        if pipeline.pause_reason is None:
            raise ValueError("Pipeline is not paused")
        if action not in ("retry", "force_advance"):
            raise ValueError(
                f"Unknown resume action {action!r} — "
                "must be 'retry' or 'force_advance'"
            )
        pipeline.resume_action = action  # type: ignore[assignment]
        pipeline.resume_ready.set()
        # Record the force-advance as a coordinator decision so the UI's
        # decisions log shows the user override explicitly.
        if action == "force_advance":
            stage_name = pipeline.current_stage_name
            rationale = pipeline.pause_reason or "coordinator failure"
            decision_text = (
                f"approved stage {stage_name} after coordinator failure"
            )
            _append_user_override_decision(
                project.workspace_dir, decision_text, rationale,
            )
            await project.broadcast_raw({
                "type": "coordinator_decision",
                "data": {
                    "scope": "user_override",
                    "decision": decision_text,
                    "rationale": rationale,
                },
            })
        return {"ok": "true", "action": action}

    async def approve_stage_from_user(
        self, project_id: str | None, summary: str = "",
    ) -> dict[str, str]:
        """User-driven approve (``POST /api/projects/{slug}/pipeline/approve``).

        When a stage review is pending (gate_verdict still None), synthesize
        an APPROVE verdict so the orchestrator advances.  Used both for the
        blocker-notification "Override & continue" button and for a manual
        approve when the coord is taking too long.
        """
        from .project import GateVerdict

        project = self.get_project(project_id)
        pipeline = project.pipeline
        if pipeline.current_stage_name is None:
            raise ValueError("No stage is currently under gate review")
        if pipeline.gate_verdict is not None:
            raise ValueError("A verdict has already been recorded")
        reason = summary or "User manually approved stage"
        pipeline.gate_verdict = GateVerdict(
            action="APPROVE", summary=f"User override: {reason}",
        )
        pipeline.gate_verdict_ready.set()
        stage_name = pipeline.current_stage_name
        decision_text = f"approved stage {stage_name}"
        _append_user_override_decision(
            project.workspace_dir, decision_text, reason,
        )
        await project.broadcast_raw({
            "type": "coordinator_decision",
            "data": {
                "scope": "user_override",
                "decision": decision_text,
                "rationale": reason,
            },
        })
        return {"ok": "true", "stage_name": stage_name}

    def _default_pipeline(self) -> list[PipelineStage]:
        return [
            PipelineStage(name="analysis", agents=["pm"]),
            PipelineStage(name="design", agents=["td"]),
            PipelineStage(name="implementation", agents=["developer", "developer"], parallel=True),
            PipelineStage(name="review", agents=["reviewer"]),
        ]
