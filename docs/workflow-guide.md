# Workflow Guide (v0.2.0)

`workflow.yaml` is a per-project file that declares how agents chain together, what the project's resource limits are, and whether an LLM coordinator gets to make dispatch decisions. It lives at `workspace/{project_id}/workflow.yaml` and can be edited from the UI (Workflow button in the header) or by hand.

## When to use it

v0.1.0 shipped a hardcoded PM → TD → Developer ×2 → Reviewer pipeline. That still works when no `workflow.yaml` is present. You only need a workflow file when you want one of:

- **Different stages** (e.g. insert a QA agent, skip the TD, run three developers)
- **Budget limits** to cap spend/turns/wall-clock before kicking off a long run
- **Event-driven routing** where one agent's completion automatically re-tasks the next, possibly in parallel
- **Coordinator-driven flows** where a Claude agent inspects state and decides the next move

If none of those apply, delete the file and the default pipeline resumes.

## Minimal example

```yaml
version: 1

stages:
  - name: analysis
    agents: [pm]
  - name: design
    agents: [td]
  - name: implementation
    agents: [developer]
  - name: review
    agents: [reviewer]
```

This gives you the four-stage default with each stage as a named block. The orchestrator still walks stages top-to-bottom when a pipeline is started manually.

## Full example

```yaml
version: 1

stages:
  - name: analysis
    agents: [pm]
  - name: design
    agents: [td]
  - name: implementation
    agents: [developer, developer]
    parallel: true
  - name: review
    agents: [reviewer]

budget:
  max_total_cost_usd: 10.0
  max_total_turns: 200
  max_wall_clock_min: 60
  max_concurrent_agents: 5

coordinator:
  enabled: true
  role_id: coordinator
  allow_spawn: false
  max_spawned_agents: 5

triggers:
  - on: pm.completed
    start: td
    context_from: [pm]
  - on: td.completed
    start: [dev_backend, dev_frontend]
    context_from: [pm, td]
  - on: [dev_backend.completed, dev_frontend.completed]
    start: reviewer
    context_from: [pm, td, dev_backend, dev_frontend]
  - on: reviewer.completed
    decide: coordinator
```

## Triggers

A trigger fires when an agent event matches its `on` predicate:

| Predicate | Meaning |
|---|---|
| `pm.completed` | the agent `pm` just finished a turn |
| `dev.error` | agent `dev` raised a final error |
| `[a.completed, b.completed]` | AND-join — fires once **both** have completed, on the event that satisfies the last remaining leg |

**Dispatch options** (one per trigger):

- `start: agent_or_role` — start that agent (or spawn a new one from the role id) with the triggering agent's summary as the prompt. Pass a list for parallel dispatch.
- `decide: coordinator` — forward the event to the coordinator agent; its `get_inbox` tool will see it.
- `context_from: [a, b]` — include the named agents' context MD files in the started agent's prompt.

Triggers are evaluated in order; the **first match wins**. If no trigger matches and the coordinator is enabled, the event is still forwarded to the coordinator via the legacy `[AGENT_DONE]` notification.

## Budget

All four budget fields are optional. Omit a field to leave that dimension uncapped.

| Field | Behaviour |
|---|---|
| `max_total_cost_usd` | Sticky — once crossed, all starts refuse until Reset |
| `max_total_turns` | Sticky — cumulative turns across every agent |
| `max_wall_clock_min` | Sticky — measured from the first agent start |
| `max_concurrent_agents` | Transient — the next start refuses while the cap is hit, but subsequent starts succeed once capacity frees up |

The UI shows a budget bar in the project header when any cap is set; it turns amber at 70%, red at 90%. When a sticky cap trips, the bar shows a **Reset** button that clears counters and unblocks the project.

## Coordinator

When `coordinator.enabled: true` and a `coordinator` role agent has been created, every workflow event is eligible for LLM dispatch via a `decide: coordinator` trigger or as a fallback when no trigger matches.

The coordinator is **re-invoked fresh on every event** — there is no long-running session. Its memory lives in `workspace/{project_id}/coordinator_state.yaml`, in four blocks:

```yaml
version: 1

facts:
  - {ts: 2026-04-19T10:30:00Z, kind: agent_completed, agent: pm, summary: "PM produced 12 tasks"}

hypothesis: |
  Current understanding of the project, overwritten each turn.

open_questions:
  - "Does dev_backend need a retry?"

decisions:
  - {ts: 2026-04-19T10:32:00Z, decision: "Fan out devs", rationale: "Trigger rule"}
```

Each invocation must call `update_state()` exactly once to persist its reasoning; the tool merges `facts_append` / `decisions_append` (append-only) and replaces `hypothesis` / `open_questions` (mutable).

Additional MCP tools the coordinator can call:

- `start_agent(agent_id, prompt)` — re-task a worker
- `spawn_agent(role_id, agent_id, prompt)` — create + run a new agent (requires `allow_spawn: true`)
- `read_context(agent_id)` — read a worker's context MD
- `list_agents()` / `get_agent_status(agent_id)` — inspect runtime state
- `get_inbox(limit)` — recent events
- `mark_done()` — declare the coordinator has no further routing to do

You can hand-edit `coordinator_state.yaml` to nudge the coordinator between events. Deleting the file resets it cleanly — the loader handles a missing file as an empty fresh state.

## Compaction

As an agent's context MD grows past ~10k tokens, a **Compact** button appears in the `ContextViewer`. Clicking it:

1. Copies the current MD to `workspace/{project_id}/context/.history/{agent_id}_{ISO8601}.md`
2. Calls a cheap Haiku summarizer with a prompt that keeps the last 3 `##` sections verbatim and summarises the rest
3. Writes the compacted version back

A **View history** button in the same header opens a modal listing archived versions. No automatic compaction happens — it's always user-driven.

## Acceptance behaviours worth knowing

- Editing `workflow.yaml` takes effect on the next event — no backend restart needed.
- Budget caps are also re-read live from `workflow.yaml`, so raising a cap after hitting it unblocks the project (once you also click Reset).
- AND-join triggers persist completion state in the EventQueue; restarting an agent clears its completion flag so the next AND-join won't fire off the stale value.
- If a trigger points at an agent id that doesn't exist yet, the orchestrator tries to interpret the id as a role id and spawns a fresh agent of that role.
