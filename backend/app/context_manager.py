from __future__ import annotations

import os
from datetime import datetime


class ContextManager:
    """Manages per-agent Markdown context files in the workspace directory."""

    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir
        self.context_dir = os.path.join(workspace_dir, "context")
        os.makedirs(self.context_dir, exist_ok=True)

    def _path(self, agent_id: str) -> str:
        return os.path.join(self.context_dir, f"{agent_id}.md")

    def create(self, agent_id: str, role_name: str) -> str:
        """Create a fresh context file for an agent. Returns the file path."""
        path = self._path(agent_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = (
            f"# {role_name} - {agent_id}\n"
            f"> Status: idle | Updated: {now}\n\n"
            f"## Current Task\n_No task assigned yet._\n\n"
            f"## Decisions\n\n"
            f"## Output\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def read(self, agent_id: str) -> str:
        """Read the full context markdown for an agent."""
        path = self._path(agent_id)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def write(self, agent_id: str, content: str) -> None:
        """Overwrite the full context file for an agent."""
        path = self._path(agent_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def update_status(self, agent_id: str, status: str, task: str | None = None) -> None:
        """Update the status line and optionally the current task section."""
        content = self.read(agent_id)
        if not content:
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Update status line
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("> Status:"):
                lines[i] = f"> Status: {status} | Updated: {now}"
                break
        # Update current task if provided
        if task is not None:
            in_task_section = False
            task_start = -1
            task_end = -1
            for i, line in enumerate(lines):
                if line.strip() == "## Current Task":
                    in_task_section = True
                    task_start = i + 1
                elif in_task_section and line.startswith("## "):
                    task_end = i
                    break
            if task_start > 0:
                if task_end < 0:
                    task_end = len(lines)
                lines[task_start:task_end] = [task, ""]

        self.write(agent_id, "\n".join(lines))

    def append_output(self, agent_id: str, output: str) -> None:
        """Append text to the Output section of the context file."""
        content = self.read(agent_id)
        if not content:
            return
        # Find the Output section and append
        output_marker = "## Output"
        idx = content.find(output_marker)
        if idx >= 0:
            insert_pos = idx + len(output_marker)
            content = content[:insert_pos] + "\n" + output + content[insert_pos:]
        else:
            content += f"\n## Output\n{output}\n"
        self.write(agent_id, content)

    def set_result(self, agent_id: str, role_name: str, task: str, result: str) -> None:
        """Replace the context file with the final result of an agent run."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = (
            f"# {role_name} - {agent_id}\n"
            f"> Status: completed | Updated: {now}\n\n"
            f"## Current Task\n{task}\n\n"
            f"## Output\n{result}\n"
        )
        self.write(agent_id, content)

    def build_context_prompt(self, from_agents: list[str]) -> str:
        """Read multiple agents' context files and format them as a prompt section."""
        if not from_agents:
            return ""
        parts: list[str] = []
        for aid in from_agents:
            ctx = self.read(aid)
            if ctx:
                parts.append(f"<agent-context id=\"{aid}\">\n{ctx}\n</agent-context>")
        if not parts:
            return ""
        return (
            "The following are context documents maintained by other agents on this project. "
            "Use them to understand prior decisions, requirements, and architecture.\n\n"
            + "\n\n".join(parts)
        )

    def list_all(self) -> dict[str, str]:
        """Return {agent_id: content} for all context files."""
        result: dict[str, str] = {}
        if not os.path.isdir(self.context_dir):
            return result
        for fname in os.listdir(self.context_dir):
            if fname.endswith(".md"):
                agent_id = fname[:-3]
                result[agent_id] = self.read(agent_id)
        return result

    def delete(self, agent_id: str) -> None:
        """Remove a context file."""
        path = self._path(agent_id)
        if os.path.exists(path):
            os.remove(path)
