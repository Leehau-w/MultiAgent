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
    PermissionMode,
    PipelineStage,
    ProjectMeta,
)
from .project import Project
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(name: str) -> str:
    """Make a filesystem-safe slug from a folder name. Empty → 'project'."""
    s = _SLUG_RE.sub("-", name).strip("-")
    return s or "project"


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
        if stages is None:
            stages = self._default_pipeline()

        await project.broadcast_raw({
            "type": "pipeline_status",
            "data": {
                "status": "running",
                "requirement": requirement[:200],
                "stages": [s.model_dump() for s in stages],
                "current_stage": 0,
            },
        })

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

                if stage.parallel:
                    tasks = [
                        project._run_agent(aid, stage_prompt, context_from=prior_ids)
                        for aid in agent_ids
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for aid, result in zip(agent_ids, results):
                        if isinstance(result, asyncio.CancelledError):
                            raise result
                        if isinstance(result, BaseException):
                            logger.error(
                                "Pipeline stage %s agent %s failed: %s",
                                stage.name, aid, result,
                            )
                            pipeline_failed = True
                else:
                    for aid in agent_ids:
                        await project._run_agent(aid, stage_prompt, context_from=prior_ids)

            status = "error" if pipeline_failed else "completed"
            await project.broadcast_raw({
                "type": "pipeline_status",
                "data": {"status": status, "current_stage": len(stages)},
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

    def _default_pipeline(self) -> list[PipelineStage]:
        return [
            PipelineStage(name="analysis", agents=["pm"]),
            PipelineStage(name="design", agents=["td"]),
            PipelineStage(name="implementation", agents=["developer", "developer"], parallel=True),
            PipelineStage(name="review", agents=["reviewer"]),
        ]
