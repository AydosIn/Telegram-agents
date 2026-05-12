# Product Requirements Document

# Telegram AI Development Agents

Three Telegram bots act as a small AI development team for a web application. The system should be AI-provider neutral: it may use Claude, OpenAI, Codex CLI, a local model, or another API through a replaceable provider layer.

## 1. Overview

The product lets a project owner manage development work from a Telegram group. The owner can assign work to a frontend agent, backend agent, or QA agent using natural language. Each agent reads the relevant repository, performs its role, reports progress in the group, and keeps code changes behind approval gates.

The current implementation runs local `codex exec` commands from Telegram. The long-term product requirement is to support any compatible AI API by isolating model calls behind an agent runner/provider interface.

## 2. Goals

- Assign development tasks from Telegram without opening an IDE.
- Run three role-based agents: frontend, backend, and QA.
- Let agents coordinate through the Telegram group.
- Keep all code changes on task branches.
- Verify work before commits are created.
- Require explicit owner approval before pushing, merging, or deploying.
- Make the AI provider replaceable.

## 3. Non-Goals

- No direct commits to `main`.
- No automatic production deploys.
- No hidden agent activity outside the configured Telegram group.
- No hard dependency on Claude or any single AI vendor.
- No autonomous QA fixes without owner approval.

## 4. Target Users

- Project owner: assigns tasks, approves pushes/merges, reviews summaries.
- Frontend agent: works in the frontend repository.
- Backend agent: works in the backend repository.
- QA agent: reviews frontend/backend health and reports issues.

## 5. Core User Flow

1. The owner sends a message in the Telegram group, such as:

   ```text
   frontend add a search bar to the dashboard
   ```

2. The frontend bot creates a task record.
3. The agent checks that its repository is clean.
4. The agent creates a task branch.
5. The AI provider receives a role-specific prompt and works in the configured repository.
6. The coordinator checks changed files for unsafe paths.
7. Verification commands run.
8. If verification passes, the coordinator commits locally.
9. The bot reports the result in Telegram.
10. The owner can push the branch with:

   ```text
   /push <task_id>
   ```

## 6. Agent Roles

### Frontend Agent

- Owns UI, layout, styling, client-side logic, and frontend integration.
- Works in `FRONTEND_REPO`.
- Runs frontend verification, such as `npm run build`.
- Requests backend help when API changes are needed.

### Backend Agent

- Owns APIs, server logic, database access, validation, and authentication.
- Works in `BACKEND_REPO`.
- Runs backend verification, such as `python -m compileall .`.
- Documents API changes in Telegram summaries.

### QA Agent

- Reviews both frontend and backend.
- Runs verification across configured repositories.
- Reports bugs, risks, and suggested fixes.
- Does not implement changes unless a future approval workflow explicitly enables it.

## 7. AI Provider Requirement

The system must not assume Claude specifically. Model execution should be wrapped behind an interface with this responsibility:

- Accept an agent role, task id, user request, repository path, and safety rules.
- Execute the requested AI workflow through the configured provider.
- Return a final summary and command output.
- Never commit or push directly.

Supported provider options may include:

- Codex CLI through `codex exec`.
- OpenAI API.
- Anthropic Claude API.
- OpenRouter or another compatible API gateway.
- Local model runner.

Recommended environment shape:

```env
AI_PROVIDER=codex_cli
AI_MODEL=
AI_API_KEY=
AI_BASE_URL=
```

Provider-specific values should stay optional unless the selected provider requires them.

## 8. Telegram Commands

The first version supports both slash commands and natural name messages.

```text
/task <request>
/ask <task_id> <question>
/handoff <task_id> <target_agent> <message>
/status
/status <task_id>
/push <task_id>
```

Natural messages should work when bot privacy is disabled:

```text
frontend fix the navbar layout
backend add validation for registrations
qa check frontend/backend integration
```

## 9. Safety Rules

- Only respond inside `TELEGRAM_GROUP_CHAT_ID`.
- Implementation agents require a clean working tree before starting.
- Every task gets its own branch.
- Agents cannot edit secrets, `.env` files, dependency folders, caches, virtual environments, or databases.
- Verification must pass before a local commit is created.
- Pushes happen only after `/push <task_id>`.
- Production merge/deploy requires explicit owner approval.
- Only the project owner can approve production actions.
- Agents must report blocked states and failures in Telegram.

## 10. Data Requirements

The coordinator should persist:

- Task id.
- Agent name.
- Original request.
- Request author.
- Status.
- Branch name.
- Commit hash.
- Verification output.
- Summary.
- Handoffs.
- Decisions.
- Logs.

SQLite is acceptable for the first version.

## 11. Configuration

Required:

```env
FRONTEND_BOT_TOKEN=
BACKEND_BOT_TOKEN=
QA_BOT_TOKEN=
TELEGRAM_GROUP_CHAT_ID=
```

Optional:

```env
FRONTEND_REPO=C:\Users\user\projects\events-community-frontend
BACKEND_REPO=C:\Users\user\projects\events-community-backend
DATABASE_PATH=agents.sqlite3
GIT_REMOTE=origin
AI_PROVIDER=codex_cli
AI_MODEL=
AI_API_KEY=
AI_BASE_URL=
```

Current compatibility:

```env
CODEX_MODEL=
```

`CODEX_MODEL` may remain supported while migrating to the provider-neutral settings above.

## 12. MVP Scope

The MVP is complete when:

- Three Telegram bots run in the same group.
- Each bot only responds in the configured group.
- Frontend and backend tasks create branches, run AI work, verify, and commit locally.
- QA can run verification across both repositories.
- `/status` shows task state.
- `/push` pushes completed task branches.
- The AI execution layer can be swapped without rewriting Telegram command handling.

## 13. Future Scope

- Open pull requests automatically after push.
- Add scheduled daily QA reports.
- Add owner identity checks for approvals.
- Add deployment integration after merge approval.
- Add richer task state, including queued, running, blocked, failed, committed, pushed, PR opened, merged, and deployed.
- Add per-agent provider/model configuration.
- Add web dashboard for task history.

## 14. Success Criteria

- The owner can assign a coding task from Telegram and receive a committed branch.
- Frontend and backend agents can hand off work through the group.
- QA can report verification status without changing code.
- No unsafe files are modified.
- No branch is pushed without owner action.
- The system can move from Claude/Codex to another API by changing provider configuration and adapter code, not by rewriting the product.
