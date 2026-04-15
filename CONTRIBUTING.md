# Contributing & Push Workflow

## What Gets Pushed

Only **essential tool files** are pushed to the repository:

| Category | Path | Pushed |
|----------|------|--------|
| Backend source | `backend/` | Yes |
| Frontend source | `frontend/` (except `node_modules/`, `dist/`) | Yes |
| Config | `backend/config/roles.yaml` | Yes |
| Documentation | `docs/`, `README.md`, `CONTRIBUTING.md` | Yes |
| Startup scripts | `start.bat`, `start.sh` | Yes |
| Docker | `Dockerfile`, `docker-compose.yml` | Yes |
| Git config | `.gitignore` | Yes |

## What Does NOT Get Pushed

| Category | Path | Reason |
|----------|------|--------|
| Dev process docs | `.dev/` | Internal notes, dev logs, drafts |
| Claude Code config | `.claude/` | Session-specific, per-machine |
| Runtime data | `workspace/context/*.md` | Generated at runtime |
| Virtual env | `.venv/`, `venv/` | Machine-specific |
| Node modules | `node_modules/` | Installed from package.json |
| Build output | `dist/` | Built from source |
| Secrets | `.env`, `.env.local` | API keys, credentials |
| IDE settings | `.vscode/`, `.idea/` | Personal preferences |

## Workflow Before Each Push

1. **Check staged files**: `git status` — verify no dev-only files are included
2. **Review changes**: `git diff --staged` — confirm only tool-related changes
3. **Commit**: write a clear commit message describing the change
4. **Push**: `git push origin master`

## Adding New Files

- Source code, configs, docs for end-users → push
- Dev notes, experiment logs, personal tooling → put in `.dev/` (gitignored)
- If unsure, default to `.dev/` first — it can always be promoted later

## Commit Message Convention

```
<type>: <short description>

Types: feat, fix, docs, refactor, chore
```

Examples:
- `feat: add Ollama provider support`
- `fix: resolve port conflict in start.bat`
- `docs: update user guide with pipeline instructions`
