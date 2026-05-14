from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.tools.terminal.models import TerminalRunResult

logger = logging.getLogger("app.tools.terminal")


StreamChunkCallback = Callable[[str], Awaitable[None]] | None


@dataclass
class StreamConfig:
    line_chunk_bytes: int = 2048
    """Read up to this many bytes before invoking stream callback."""


class ExecutionEnvironment(Protocol):
    """
    Sandbox boundary for running subprocesses.

    Phase 2: `LocalExecutionEnvironment` only. Future: `DockerExecutionEnvironment` with the
    same interface (argv, cwd, env, timeout, streaming).
    """

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_sec: int,
        on_stream_chunk: StreamChunkCallback = None,
        stream_config: StreamConfig | None = None,
    ) -> TerminalRunResult:
        ...


class LocalExecutionEnvironment:
    """Host-local execution with asyncio subprocess (no container)."""

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_sec: int,
        on_stream_chunk: StreamChunkCallback = None,
        stream_config: StreamConfig | None = None,
    ) -> TerminalRunResult:
        if not argv:
            return TerminalRunResult(False, None, "", "Empty argv.", False)

        _ = stream_config or StreamConfig()
        workdir = cwd.resolve()
        if not workdir.is_dir():
            return TerminalRunResult(False, None, "", f"Working directory does not exist: {workdir}", False)

        prepared = self._prepare_argv(argv, env or os.environ.copy())

        env_use = (env or os.environ.copy()).copy()
        env_use.setdefault("PYTHONUTF8", "1")

        try:
            process = await asyncio.create_subprocess_exec(
                *prepared,
                cwd=str(workdir),
                env=env_use,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
            )
        except FileNotFoundError as exc:
            return TerminalRunResult(False, None, "", str(exc), False)

        out_buf: list[bytes] = []
        err_buf: list[bytes] = []
        timed_out = False

        async def read_pipe_lines(pipe: asyncio.StreamReader | None, chunks: list[bytes], label: str) -> None:
            if pipe is None:
                return
            while True:
                line = await pipe.readline()
                if not line:
                    break
                chunks.append(line)
                if on_stream_chunk:
                    piece = line.decode("utf-8", errors="replace")
                    await on_stream_chunk(f"[{label}] {piece}")

        try:
            if on_stream_chunk is None:
                stdout_b, stderr_b = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_sec,
                )
                stdout = (stdout_b or b"").decode("utf-8", errors="replace")
                stderr = (stderr_b or b"").decode("utf-8", errors="replace")
                code = process.returncode
            else:
                assert process.stdout is not None and process.stderr is not None
                out_task = asyncio.create_task(read_pipe_lines(process.stdout, out_buf, "stdout"))
                err_task = asyncio.create_task(read_pipe_lines(process.stderr, err_buf, "stderr"))
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout_sec)
                except asyncio.TimeoutError:
                    timed_out = True
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(asyncio.gather(out_task, err_task), timeout=10)
                    stdout = b"".join(out_buf).decode("utf-8", errors="replace")
                    stderr = (b"".join(err_buf).decode("utf-8", errors="replace") + f"\n(timed out after {timeout_sec}s)").strip()
                    code = process.returncode if process.returncode is not None else -1
                    return TerminalRunResult(ok=False, exit_code=code, stdout=stdout, stderr=stderr, timed_out=True)

                await asyncio.wait_for(asyncio.gather(out_task, err_task), timeout=30)
                code = process.returncode
                stdout = b"".join(out_buf).decode("utf-8", errors="replace")
                stderr = b"".join(err_buf).decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            timed_out = True
            try:
                process.kill()
            except ProcessLookupError:
                pass
            stdout = b"".join(out_buf).decode("utf-8", errors="replace")
            stderr = (b"".join(err_buf).decode("utf-8", errors="replace") + f"\n(timed out after {timeout_sec}s)").strip()
            code = process.returncode if process.returncode is not None else -1

        ok = code == 0 and not timed_out
        return TerminalRunResult(ok=ok, exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out)

    def _prepare_argv(self, argv: list[str], env: dict[str, str]) -> list[str]:
        if not argv:
            raise ValueError("argv empty")
        executable = argv[0]
        resolved = shutil.which(executable, path=env.get("PATH"))
        if not resolved:
            return argv
        if os.name == "nt" and Path(resolved).suffix.lower() in {".bat", ".cmd"}:
            return ["cmd.exe", "/d", "/s", "/c", " ".join(self._quote_win(argv))]
        return [resolved, *argv[1:]]

    @staticmethod
    def _quote_win(parts: list[str]) -> list[str]:
        out: list[str] = []
        for p in parts:
            if " " in p or "\t" in p:
                out.append(f'"{p}"')
            else:
                out.append(p)
        return out
