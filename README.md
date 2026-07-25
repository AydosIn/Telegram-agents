# Telegram AI Development Agents

## What is it?

A multi-agent system that runs three AI-powered Telegram bots (Frontend, Backend, and QA) to automate software development tasks for the Events Community project. Each bot can receive coding tasks via Telegram, execute them using an AI provider (Codex CLI or Gemini API), verify the output, and commit changes to the appropriate repository.

## Why does this exist?

Managing a full-stack project across frontend and backend repositories requires constant context switching and manual coordination. This system automates the development workflow by letting you assign tasks to specialized bots in a single Telegram group — they handle the code generation, verification, and git operations while you focus on architecture and decision-making.

## When to use it?

- When you want to delegate small-to-medium development tasks (bug fixes, feature additions, refactoring) without opening an IDE
- When coordinating work across frontend and backend repos simultaneously
- When you want AI-assisted development with built-in safety rails (branch isolation, verification gates, manual push approval)

## How it can help?

- **Saves time:** Assign tasks via Telegram messages and let the bots write, verify, and commit code
- **Reduces errors:** Every change goes through automated verification before committing
- **Safe workflow:** Changes stay on isolated task branches and only push to the remote when you explicitly approve with `/push`
- **Multi-repo coordination:** Frontend and backend bots can hand off context to each other with `/handoff`

---

Runs three Telegram bots for the Events Community project:

- frontend agent
- backend agent
- QA agent

The bots coordinate in one Telegram group, run an **AI provider** (default: local `codex exec`; optional: **Gemini** over the API), verify changes, commit task branches locally, and only push when you send `/push`.

## Setup

1. Create three bots in [@BotFather](https://t.me/BotFather).
2. Create a Telegram **group** (or use an existing one) and **add all three bots** as members.
3. Copy `.env.example` to `.env` (only if you do not already have one) and fill in at least the four required values below. See [`.env.example`](.env.example) for optional `AI_PROVIDER`, `GEMINI_API_KEY` / `AI_API_KEY`, and repo overrides.

```env
FRONTEND_BOT_TOKEN=...
BACKEND_BOT_TOKEN=...
QA_BOT_TOKEN=...
TELEGRAM_GROUP_CHAT_ID=-100...
```

`TELEGRAM_GROUP_CHAT_ID` is the numeric id of that **same** group (supergroups are often negative, for example `-100...`). The apps ignore messages from any other chat. Practical ways to obtain it include forwarding a group message to an id-reveal bot, or temporarily logging `update.effective_chat.id` while testing.

4. **Optional — natural language in the group:** In BotFather run `/setprivacy`, pick each agent bot, and choose **Disable**. Otherwise rely on `/task`, replies, or `@BotUsername` so each bot receives text.

5. Install dependencies:

```powershell
cd C:\Users\user\projects\codex-telegram-agents
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

For real tasks you also need **git** on `PATH`, **clean** frontend/backend working trees, a configured **remote** for `/push`, and the tools used by verification (**npm** for the frontend build, **python** for backend `compileall`). With `AI_PROVIDER=codex_cli`, the **Codex** CLI must be installed and callable as `codex`.

6. Quick config check (loads `.env`, does not connect to Telegram):

```powershell
python scripts\smoke_check.py
```

7. Run the bots:

```powershell
python main.py
```

### AI providers

- **`AI_PROVIDER=codex_cli`** (default): runs `codex exec` in the task repository. Pass a model with `CODEX_MODEL` or, if that is empty, with **`AI_MODEL`** (same flag the PRD recommends for provider-neutral config).
- **`AI_PROVIDER=gemini`**: calls the Gemini API. Set **`AI_API_KEY`** or **`GEMINI_API_KEY`**. Optionally set **`AI_MODEL`** (default `gemini-2.0-flash`). The model returns JSON with full-file `path` / `content` entries; the coordinator writes them under the repo (blocked paths are rejected), then verification and git commit behave the same as with Codex.

## Commands

Use commands in the configured Telegram group:

```text
/task@FrontendBot Fix the homepage spacing
/task@BackendBot Add validation for registrations
/task@QABot Check frontend/backend integration
/ask@FrontendBot 12 What endpoint should this call?
/handoff@BackendBot 12 frontend The API now returns avatar_url
/status@QABot
/status@QABot 12
/push@FrontendBot 12
```

Telegram bot usernames depend on what you create in BotFather.

You can also talk to them by name after disabling bot privacy in BotFather:

```text
frontend fix the navbar layout
backend add registration validation
qa check the frontend/backend integration
@YourFrontendBotUsername clean up the homepage cards
```

To enable this:

1. Open BotFather.
2. Run `/setprivacy`.
3. Select each agent bot.
4. Choose `Disable`.

Without that privacy setting, Telegram only sends slash commands, direct replies, and some mentions to bots in groups.

## Safety Rules

- Bots only respond in `TELEGRAM_GROUP_CHAT_ID`.
- Implementation agents require a clean git working tree before starting.
- Each task gets a branch like `agent/frontend/task-12-fix-homepage`.
- Verification must pass before a commit is created.
- Pushes only happen through `/push`.
- Dirty backend/frontend repos are marked blocked instead of being modified.

## Default Repos

- Frontend: `C:\Users\user\projects\events-community-frontend`
- Backend: `C:\Users\user\projects\events-community-backend`

Override them with `FRONTEND_REPO` and `BACKEND_REPO`.
