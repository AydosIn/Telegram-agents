from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable

from pathlib import Path

from telegram import InputFile, Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import Settings, load_settings
from app.formatting import format_task, truncate
from app.models import TaskRecord
from app.runner import AgentRunner
from app.store import Store
from app.task_lifecycle import TaskStatus


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


class TelegramCoordinator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.database_path)
        self.runner = AgentRunner(settings, self.store)
        self.task_locks: dict[str, asyncio.Lock] = {
            agent_name: asyncio.Lock() for agent_name in settings.agents
        }

    async def shutdown(self) -> None:
        await self.runner.shutdown_browser()

    def build_applications(self) -> list[Application]:
        applications: list[Application] = []
        for agent_name, agent in self.settings.agents.items():
            app = ApplicationBuilder().token(agent.token).build()
            app.bot_data["agent_name"] = agent_name
            app.add_handler(CommandHandler("chat", self.chat_command))
            app.add_handler(CommandHandler("task", self.task_command))
            app.add_handler(CommandHandler("ask", self.ask_command))
            app.add_handler(CommandHandler("handoff", self.handoff_command))
            app.add_handler(CommandHandler("status", self.status_command))
            app.add_handler(CommandHandler("push", self.push_command))
            app.add_handler(CommandHandler("approve_terminal", self.approve_terminal_command))
            app.add_handler(CommandHandler("approve_git_push", self.approve_git_push_command))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.name_message))
            applications.append(app)
        return applications

    async def name_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return
        if not update.message or not update.message.text:
            return

        target_agent, request = self.parse_named_message(update.message.text, context)
        if not target_agent or target_agent != self.agent_name(context):
            return

        if not request:
            await self.reply(update, f"I'm here. Ask me something like `{target_agent} what do you think?`")
            return

        use_workflow = (
            self.settings.workflow_from_mentions
            and self.settings.file_tools_enabled
            and target_agent in ("frontend", "backend")
        )
        if use_workflow:
            task = self.store.create_task(
                target_agent,
                request,
                self.sender_name(update),
                status=TaskStatus.IN_PROGRESS,
            )
            await self.reply(update, f"Workflow task opened:\n{format_task(task)}")
            asyncio.create_task(
                self.chat_and_report(
                    context.application,
                    target_agent,
                    request,
                    self.chat_id(update),
                    update.message.message_id,
                    workflow_task_id=task.id,
                ),
            )
            return

        asyncio.create_task(
            self.chat_and_report(
                context.application,
                target_agent,
                request,
                self.chat_id(update),
                update.message.message_id,
            )
        )

    async def chat_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return

        agent_name = self.agent_name(context)
        request = " ".join(context.args).strip()
        if not request:
            await self.reply(update, "Usage: `/chat <message>`")
            return

        asyncio.create_task(
            self.chat_and_report(
                context.application,
                agent_name,
                request,
                self.chat_id(update),
                update.message.message_id if update.message else None,
            )
        )

    async def task_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return

        agent_name = self.agent_name(context)
        request = " ".join(context.args).strip()
        if not request:
            await self.reply(update, "Usage: `/task <request>`")
            return

        task = self.store.create_task(agent_name, request, self.sender_name(update))
        await self.reply(update, f"Task created:\n{format_task(task)}")
        asyncio.create_task(self.execute_and_report(context.application, task, self.chat_id(update)))

    async def ask_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return

        agent_name = self.agent_name(context)
        if len(context.args) < 2:
            await self.reply(update, "Usage: `/ask <task_id> <question>`")
            return

        try:
            source_task_id = int(context.args[0])
            source_task = self.store.get_task(source_task_id)
        except (ValueError, KeyError):
            await self.reply(update, "I could not find that task id.")
            return

        question = " ".join(context.args[1:]).strip()
        request = (
            f"Question from Telegram about task #{source_task.id} "
            f"({source_task.agent}): {question}\n\n"
            f"Original request: {source_task.request}"
        )
        task = self.store.create_task(agent_name, request, self.sender_name(update))
        self.store.add_decision(source_task.id, agent_name, question)
        await self.reply(update, f"Got it. I opened a follow-up for `{agent_name}`.\n\n{format_task(task)}")
        asyncio.create_task(self.execute_and_report(context.application, task, self.chat_id(update)))

    async def handoff_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return

        from_agent = self.agent_name(context)
        if len(context.args) < 3:
            await self.reply(update, "Usage: `/handoff <task_id> <target_agent> <message>`")
            return

        try:
            source_task_id = int(context.args[0])
            self.store.get_task(source_task_id)
        except (ValueError, KeyError):
            await self.reply(update, "I could not find that source task id.")
            return

        to_agent = context.args[1].lower()
        if to_agent not in self.settings.agents:
            await self.reply(update, "Target agent must be `frontend`, `backend`, or `qa`.")
            return

        message = " ".join(context.args[2:]).strip()
        request = f"Handoff from {from_agent} for task #{source_task_id}:\n{message}"
        task = self.store.create_task(to_agent, request, self.sender_name(update))
        self.store.add_handoff(source_task_id, from_agent, to_agent, message, task.id)

        await self.reply(
            update,
            f"Handoff created for `{to_agent}`.\n\n{format_task(task)}",
        )

        target_app = self.find_application_for_agent(context.application, to_agent)
        asyncio.create_task(self.execute_and_report(target_app or context.application, task, self.chat_id(update)))

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return

        if context.args:
            try:
                task = self.store.get_task(int(context.args[0]))
            except (ValueError, KeyError):
                await self.reply(update, "I could not find that task id.")
                return

            logs = self.store.recent_logs(task.id)
            body = format_task(task)
            if logs:
                body += "\n\nRecent logs:\n" + "\n".join(logs)
            await self.reply(update, body)
            return

        tasks = self.store.list_tasks(limit=10)
        if not tasks:
            await self.reply(update, "No tasks yet.")
            return

        lines = [
            f"#{task.id} [{task.agent}] [{task.status}] - {truncate(task.request, 90)}"
            for task in tasks
        ]
        await self.reply(update, "Recent tasks:\n" + "\n".join(lines))

    async def push_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return

        if len(context.args) != 1:
            await self.reply(update, "Usage: `/push <task_id>`")
            return

        try:
            task = self.store.get_task(int(context.args[0]))
        except (ValueError, KeyError):
            await self.reply(update, "I could not find that task id.")
            return

        agent_name = self.agent_name(context)
        if task.agent != agent_name:
            await self.reply(
                update,
                f"Task #{task.id} belongs to {task.agent}. "
                f"Use that bot to push it.",
            )
            return

        await self.reply(update, f"Pushing task #{task.id} branch...")
        asyncio.create_task(self.push_and_report(context.application, task, self.chat_id(update)))

    async def execute_and_report(self, app: Application, task: TaskRecord, chat_id: int | None) -> None:
        async with self.task_locks[task.agent]:
            try:
                result = await self.runner.run_task(task)
                await app.bot.send_message(
                    chat_id=chat_id or self.settings.group_chat_id,
                    text=format_task(result),
                )
                if (
                    self.settings.auto_qa_review_task
                    and result.agent in ("frontend", "backend")
                    and result.status == TaskStatus.REVIEW
                ):
                    qa_task = self.store.create_task(
                        "qa",
                        f"Review implementation task #{result.id} ({result.agent}): {result.request}",
                        created_by=None,
                    )
                    await self.safe_send(
                        app,
                        f"QA review task #{qa_task.id} queued for implementation #{result.id}.",
                        chat_id,
                    )
                    asyncio.create_task(self.execute_and_report(app, qa_task, chat_id))
            except Exception as exc:
                logging.exception("Task execution failed")
                self.store.update_task(task.id, status=TaskStatus.FAILED, summary=str(exc))
                await self.safe_send(app, f"Task #{task.id} failed: {exc}", chat_id)

    async def push_and_report(self, app: Application, task: TaskRecord, chat_id: int | None) -> None:
        async with self.task_locks[task.agent]:
            try:
                result = await self.runner.push_task(task)
                await app.bot.send_message(
                    chat_id=chat_id or self.settings.group_chat_id,
                    text=format_task(result),
                )
            except Exception as exc:
                logging.exception("Push failed")
                self.store.update_task(task.id, status=TaskStatus.FAILED, summary=str(exc))
                await self.safe_send(app, f"Push for task #{task.id} failed: {exc}", chat_id)

    async def approve_terminal_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return
        if len(context.args) != 1:
            await self.reply(update, "Usage: `/approve_terminal <pending_id>`")
            return
        try:
            pending_id = int(context.args[0])
        except ValueError:
            await self.reply(update, "pending_id must be a number.")
            return

        if self.store.approve_terminal_pending(pending_id):
            await self.reply(
                update,
                f"Approved terminal command #{pending_id}. The agent can retry the tool with "
                f'`"approval_id": {pending_id}`.',
            )
        else:
            await self.reply(update, f"No pending terminal request #{pending_id} (already used or unknown).")

    async def approve_git_push_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.is_allowed_chat(update):
            return
        if len(context.args) != 1:
            await self.reply(update, "Usage: `/approve_git_push <pending_id>`")
            return
        try:
            pending_id = int(context.args[0])
        except ValueError:
            await self.reply(update, "pending_id must be a number.")
            return

        if self.store.approve_git_push_pending(pending_id):
            await self.reply(
                update,
                f"Approved git push #{pending_id}. The agent can retry `git_push` with "
                f'`"approval_id": {pending_id}`.',
            )
        else:
            await self.reply(update, f"No pending git push #{pending_id} (already used or unknown).")

    async def chat_and_report(
        self,
        app: Application,
        agent_name: str,
        message: str,
        chat_id: int | None,
        reply_to_message_id: int | None,
        *,
        workflow_task_id: int | None = None,
    ) -> None:
        try:
            if self.settings.file_tools_enabled:

                async def stream_emit(chunk: str) -> None:
                    body = chunk.strip()
                    if body:
                        await self.safe_send(
                            app,
                            truncate(f"[{agent_name} stream]\n{body}", 3500),
                            chat_id,
                            None,
                        )

                result = await self.runner.run_chat_with_tools(
                    agent_name,
                    message,
                    stream_emit=stream_emit,
                    task_id=workflow_task_id,
                )
            else:
                result = await self.runner.run_chat(agent_name, message)
            if result.ok:
                text = truncate(result.combined_output, 3500)
            else:
                logging.warning("Chat provider failed for %s: %s", agent_name, result.combined_output)
                text = self.fallback_chat_text(agent_name, result.combined_output)
            await self.safe_send(app, text, chat_id, reply_to_message_id=reply_to_message_id)
            if result.attachment_paths:
                await self.send_screenshot_paths(app, result.attachment_paths, chat_id)

            if workflow_task_id is not None:
                agent_cfg = self.settings.agents[agent_name]
                if result.ok:
                    st = TaskStatus.REVIEW
                    fc: str | None = None
                    if agent_cfg.repo:
                        files = await self.runner.changed_files(agent_cfg.repo)
                        fc = json.dumps(files) if files else None
                        if not files:
                            st = TaskStatus.DONE
                    self.store.update_task(
                        workflow_task_id,
                        status=st,
                        summary=truncate(result.combined_output, 2400),
                        files_changed=fc,
                    )
                    updated = self.store.get_task(workflow_task_id)
                    await self.safe_send(
                        app,
                        truncate(f"[workflow] Task #{workflow_task_id} → {st}\n{format_task(updated)}", 3500),
                        chat_id,
                        None,
                    )
                    if (
                        self.settings.auto_qa_review_task
                        and agent_name in ("frontend", "backend")
                        and st == TaskStatus.REVIEW
                    ):
                        qa_task = self.store.create_task(
                            "qa",
                            f"Review mention-workflow task #{workflow_task_id} ({agent_name}): {message[:400]}",
                            created_by=None,
                        )
                        await self.safe_send(
                            app,
                            f"QA review #{qa_task.id} queued for workflow #{workflow_task_id}.",
                            chat_id,
                            None,
                        )
                        asyncio.create_task(self.execute_and_report(app, qa_task, chat_id))
                else:
                    self.store.update_task(
                        workflow_task_id,
                        status=TaskStatus.FAILED,
                        summary=truncate(result.combined_output, 2400),
                    )
                    failed = self.store.get_task(workflow_task_id)
                    await self.safe_send(
                        app,
                        truncate(f"[workflow] Task #{workflow_task_id} failed.\n{format_task(failed)}", 3500),
                        chat_id,
                        None,
                    )
        except Exception as exc:
            logging.exception("Chat response failed")
            await self.safe_send(app, f"I hit an error while answering: {exc}", chat_id, reply_to_message_id)

    def fallback_chat_text(self, agent_name: str, provider_output: str) -> str:
        if "429" in provider_output or "quota" in provider_output.lower():
            return "Yes boss, I'm here. My Gemini quota is tapped right now, but the bot wiring is alive."
        err = (provider_output or "").strip()
        if err:
            # Surface runner/API failures instead of a misleading "I'm here" ping.
            return truncate(
                f"couldn't finish that run — check logs or retry.\n\n{err}",
                900,
            )
        if agent_name == "frontend":
            return "I'm here. Send me the UI thought and I'll keep it sharp."
        if agent_name == "backend":
            return "Yes boss, backend is here. Tell me what you want to check."
        if agent_name == "qa":
            return "QA is here. Send the thing and I'll look for risks."
        return "I'm here. What's up?"

    async def safe_send(
        self,
        app: Application,
        text: str,
        chat_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        try:
            await app.bot.send_message(
                chat_id=chat_id or self.settings.group_chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )
        except TelegramError:
            logging.exception("Could not send Telegram message")

    async def send_screenshot_paths(
        self,
        app: Application,
        paths: tuple[str, ...],
        chat_id: int | None,
    ) -> None:
        cid = chat_id or self.settings.group_chat_id
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                continue
            try:
                with path.open("rb") as fh:
                    await app.bot.send_photo(
                        chat_id=cid,
                        photo=InputFile(fh, filename=path.name),
                        caption=truncate(path.name, 900),
                    )
            except TelegramError:
                logging.exception("Could not send screenshot %s", path)

    async def reply(self, update: Update, text: str) -> None:
        if update.message:
            await update.message.reply_text(text)

    def chat_id(self, update: Update) -> int | None:
        chat = update.effective_chat
        return chat.id if chat else None

    async def is_allowed_chat(self, update: Update) -> bool:
        chat = update.effective_chat
        if not chat:
            return False

        allowed_ids = set(self.settings.group_chat_id_aliases)
        allowed_ids.add(self.settings.group_chat_id)

        for allowed_id in tuple(allowed_ids):
            allowed_ids.add(abs(allowed_id))
            allowed_ids.add(-abs(allowed_id))

        return str(chat.id) in {str(allowed_id) for allowed_id in allowed_ids}

    def sender_name(self, update: Update) -> str | None:
        user = update.effective_user
        if not user:
            return None
        return user.username or user.full_name or str(user.id)

    def agent_name(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        return str(context.application.bot_data["agent_name"])

    def find_application_for_agent(self, current_app: Application, agent_name: str) -> Application | None:
        applications = current_app.bot_data.get("applications", [])
        for app in applications:
            if app.bot_data.get("agent_name") == agent_name:
                return app
        return None

    def parse_named_message(
        self,
        text: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> tuple[str | None, str]:
        stripped = text.strip()
        lowered = stripped.lower()
        bot_username = context.bot.username
        username_match: tuple[str | None, str] | None = None

        if bot_username:
            mention = f"@{bot_username.lower()}"
            match = re.search(rf"(?<!\w){re.escape(mention)}(?!\w)", lowered)
            if match:
                request = stripped[match.end() :].strip(" :,-")
                username_match = (self.agent_name(context), request)

        if username_match is not None:
            return username_match

        for agent_name in self.settings.agents:
            pattern = rf"(?<!\w){re.escape(agent_name)}(?!\w)"
            match = re.search(pattern, lowered)
            if match:
                return agent_name, stripped[match.end() :].strip(" :,-")

        return None, ""


async def run() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = load_settings()
    coordinator = TelegramCoordinator(settings)
    applications = coordinator.build_applications()
    for app in applications:
        app.bot_data["applications"] = applications

    initialized: list[Application] = []
    started: list[Application] = []
    polling: list[Application] = []
    try:
        for app in applications:
            await app.initialize()
            initialized.append(app)
            await app.start()
            started.append(app)
            if app.updater is None:
                raise RuntimeError("Application updater is not available.")
            await app.updater.start_polling(drop_pending_updates=True)
            polling.append(app)

        print(
            f"Telegram AI agents are running (AI_PROVIDER={settings.ai_provider!r}). "
            "Press Ctrl+C to stop.",
        )
        await asyncio.Event().wait()
    finally:
        try:
            await coordinator.shutdown()
        except Exception:
            logging.exception("Playwright/browser shutdown")
        for app in reversed(polling):
            if app.updater is not None:
                await app.updater.stop()
        for app in reversed(started):
            await app.stop()
        for app in reversed(initialized):
            await app.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Stopped Telegram AI agents.")
