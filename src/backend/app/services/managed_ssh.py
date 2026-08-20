"""Generic durable command execution over an existing SSH session.

The backend persists one uniform process envelope for every command.  It does
not inspect command names and does not need per-command lifecycle adapters.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.services.managed_commands import CommandSnapshot, OperationContext, redact_text

_OPERATION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# 单轮增量读取的上限：远端一次刷太多日志时只取这么多，剩下的下一轮接着读。
_MAX_OUTPUT_CHUNK_BYTES = 262_144


class SSHResultLike(Protocol):
    exit_status: int
    stdout: str
    stderr: str


class SSHSessionLike(Protocol):
    async def write_file(self, path: str, content: str) -> None: ...


RunCommand = Callable[[str, float | None], Awaitable[SSHResultLike]]


@dataclass(slots=True, frozen=True)
class ManagedCommandHandle:
    operation_id: str
    attempt_id: str
    context: OperationContext
    process_id: int
    process_group_id: int


@dataclass(slots=True, frozen=True)
class OutputChunk:
    stream: str
    text: str
    offset: int


@dataclass(slots=True, frozen=True)
class ManagedGPUUsage:
    """GPU processes which can be attributed to one managed process group."""

    status: str
    process_alive: bool
    process_ids: tuple[int, ...] = ()
    used_memory_mib: int = 0


@dataclass(slots=True, frozen=True)
class ManagedStopResult:
    """Verified result of an attempt-scoped remote stop request."""

    status: str
    confirmed: bool

    def __bool__(self) -> bool:
        return self.confirmed


class ManagedCommandLaunchError(RuntimeError):
    """A managed operation failed before a durable handle could be recovered."""

    def __init__(
        self,
        context: OperationContext,
        command: str,
        cause: BaseException,
    ) -> None:
        self.operation_context = context
        self.command = redact_text(context.display_command or command)
        self.stdout = redact_text(getattr(cause, "stdout", ""), tail=True)
        self.stderr = redact_text(getattr(cause, "stderr", ""), tail=True)
        self.exit_status = getattr(cause, "exit_status", None)
        self.original_exception = cause
        detail = str(cause).strip() or self.stderr or self.stdout or type(cause).__name__
        super().__init__(f"managed command launch failed: {detail}")


class SSHManagedCommands:
    """Persist, inspect, stream, and stop process-group-bound commands."""

    def __init__(
        self,
        *,
        session: SSHSessionLike,
        run: RunCommand,
        shell_workdir: str,
        sftp_workdir: str,
    ) -> None:
        self._session = session
        self._run = run
        self._shell_workdir = shell_workdir
        self._sftp_workdir = sftp_workdir

    @staticmethod
    def _validate_operation_id(operation_id: str) -> str:
        value = operation_id.strip().lower()
        if not _OPERATION_RE.fullmatch(value):
            raise ValueError(f"invalid managed operation id: {operation_id!r}")
        return value

    def _operation_dir(self, operation_id: str) -> str:
        safe = self._validate_operation_id(operation_id)
        return f"{self._shell_workdir}/.polaris/operations/{safe}"

    def _attempt_prefix(self, operation_id: str, attempt_id: str) -> str:
        safe_attempt = str(uuid.UUID(attempt_id))
        return f"{self._operation_dir(operation_id)}/attempts/{safe_attempt}"

    def _sftp_attempt_prefix(self, operation_id: str, attempt_id: str) -> str:
        safe = self._validate_operation_id(operation_id)
        safe_attempt = str(uuid.UUID(attempt_id))
        return f"{self._sftp_workdir}/.polaris/operations/{safe}/attempts/{safe_attempt}"

    @staticmethod
    def _pid_from_output(value: object) -> int | None:
        for line in reversed(str(value or "").splitlines()):
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            return pid if pid > 0 else None
        return None

    async def _recover_started_handle(
        self,
        *,
        operation_id: str,
        attempt_id: str,
        context: OperationContext,
        fallback_output: object = "",
    ) -> ManagedCommandHandle | None:
        """Recover when SSH times out after the detached process actually started."""
        prefix = self._attempt_prefix(operation_id, attempt_id)
        fallback_pid = self._pid_from_output(fallback_output)
        pid: int | None = None
        pgid: int | None = None
        for _ in range(10):
            with contextlib.suppress(Exception):
                pid = await self._read_positive_int(f"{prefix}.pid")
                if pid is not None:
                    pgid = await self._read_positive_int(f"{prefix}.pgid") or pid
                    break
            await asyncio.sleep(0.1)
        pid = pid or fallback_pid
        if pid is None:
            return None
        return ManagedCommandHandle(
            operation_id=operation_id,
            attempt_id=attempt_id,
            context=context,
            process_id=pid,
            process_group_id=pgid or pid,
        )

    async def start(
        self,
        context: OperationContext,
        command: str,
        *,
        attempt_id: str | None = None,
    ) -> ManagedCommandHandle:
        operation_id = self._validate_operation_id(context.operation)
        attempt_id = str(uuid.UUID(attempt_id)) if attempt_id else str(uuid.uuid4())
        operation_dir = self._operation_dir(operation_id)
        prefix = self._attempt_prefix(operation_id, attempt_id)
        sftp_prefix = self._sftp_attempt_prefix(operation_id, attempt_id)
        try:
            await self._run(f"mkdir -p {operation_dir}/attempts", 60)
        except Exception as exc:
            raise ManagedCommandLaunchError(context, command, exc) from exc
        lock = f"{operation_dir}/launch.lock"
        try:
            acquired = await self._run(
                f"i=0; until mkdir {lock} 2>/dev/null; do "
                f"if [ -d {lock} ] && [ $(( $(date +%s) - $(stat -c %Y {lock}) )) -gt 120 ]; "
                f"then rmdir {lock} 2>/dev/null || true; fi; "
                "i=$((i+1)); [ $i -lt 240 ] || exit 75; sleep 0.25; done",
                65,
            )
        except Exception as exc:
            raise ManagedCommandLaunchError(context, command, exc) from exc
        if acquired.exit_status != 0:
            cause = RuntimeError("timed out waiting for managed command launch lock")
            raise ManagedCommandLaunchError(context, command, cause)
        try:
            # A concurrent worker may have launched this logical operation while we
            # waited. Attach to it instead of starting a duplicate remote process.
            current = await self.current_attempt_id(operation_id)
            if current is not None:
                current_prefix = self._attempt_prefix(operation_id, current)
                current_pid = await self._read_positive_int(f"{current_prefix}.pid")
                current_exit = await self._read_int(f"{current_prefix}.exit")
                if current_pid is not None and current_exit is None:
                    alive = await self._run(f"kill -0 {current_pid} 2>/dev/null", 60)
                    if alive.exit_status == 0:
                        current_pgid = (
                            await self._read_positive_int(f"{current_prefix}.pgid") or current_pid
                        )
                        return ManagedCommandHandle(
                            operation_id=operation_id,
                            attempt_id=current,
                            context=context,
                            process_id=current_pid,
                            process_group_id=current_pgid,
                        )

            launcher = (
                "#!/usr/bin/env bash\n"
                "set +e\n"
                f"prefix={prefix}\n"
                "echo $$ > ${prefix}.pid\n"
                "ps -o pgid= -p $$ | tr -d ' ' > ${prefix}.pgid\n"
                "awk '{print $22}' /proc/$$/stat > ${prefix}.start_ticks 2>/dev/null || true\n"
                "date +%s > ${prefix}.started\n"
                "touch ${prefix}.stdout ${prefix}.stderr\n"
                # 命令单独成行：写成 `{ cmd; }` 时，只要 cmd 以注释收尾
                # （`bash run.sh # 说明`），后面的 `; }` 会被一起注释掉，
                # 整个启动脚本变成语法错误。换行后注释只吃掉它自己那一行。
                "{\n"
                f"{command}\n"
                "} >${prefix}.stdout 2>${prefix}.stderr\n"
                "status=$?\n"
                "printf '%s\\n' \"$status\" > ${prefix}.exit.tmp\n"
                "mv ${prefix}.exit.tmp ${prefix}.exit\n"
                "exit $status\n"
            )
            await self._session.write_file(f"{sftp_prefix}.sh", launcher)
            pointer = f"{operation_dir}/current"
            await self._run(
                f"printf '%s\\n' {attempt_id} > {pointer}.tmp && mv {pointer}.tmp {pointer}",
                60,
            )
            launch_command = (
                f"chmod 700 {prefix}.sh || exit $?; "
                f"nohup setsid bash {prefix}.sh >/dev/null 2>&1 < /dev/null & "
                "pid=$!; printf '%s\\n' \"$pid\""
            )
            try:
                launch = await self._run(launch_command, 60)
            except Exception as exc:
                recovered = await self._recover_started_handle(
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    context=context,
                    fallback_output=getattr(exc, "stdout", ""),
                )
                if recovered is not None:
                    return recovered
                raise ManagedCommandLaunchError(context, command, exc) from exc
            if launch.exit_status != 0:
                recovered = await self._recover_started_handle(
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    context=context,
                    fallback_output=launch.stdout,
                )
                if recovered is not None:
                    return recovered
                cause = RuntimeError(redact_text(launch.stderr or launch.stdout, tail=True))
                raise ManagedCommandLaunchError(context, command, cause)
            pid = self._pid_from_output(launch.stdout)
            if pid is None:
                recovered = await self._recover_started_handle(
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    context=context,
                    fallback_output=launch.stdout,
                )
                if recovered is not None:
                    return recovered
                cause = RuntimeError(
                    f"managed command launch returned no PID: {redact_text(launch.stdout)!r}"
                )
                raise ManagedCommandLaunchError(context, command, cause)
            # The launcher records the real PGID. It normally equals the returned
            # PID; use the PID as a safe initial value until the first snapshot.
            return ManagedCommandHandle(
                operation_id=operation_id,
                attempt_id=attempt_id,
                context=context,
                process_id=pid,
                process_group_id=pid,
            )
        except ManagedCommandLaunchError:
            raise
        except Exception as exc:
            raise ManagedCommandLaunchError(context, command, exc) from exc
        finally:
            with contextlib.suppress(Exception):
                await self._run(f"rmdir {lock} 2>/dev/null || true", 60)

    async def current_attempt_id(self, operation_id: str) -> str | None:
        path = f"{self._operation_dir(operation_id)}/current"
        result = await self._run(f"cat {path} 2>/dev/null", 60)
        if result.exit_status != 0:
            return None
        try:
            return str(uuid.UUID(result.stdout.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            return None

    async def recover_current(
        self, context: OperationContext
    ) -> ManagedCommandHandle | None:
        """Recover the durable handle most recently selected for an operation.

        Unlike :meth:`start`, recovery also returns an attempt which has already
        written its exit status.  A worker can therefore finish bookkeeping for
        the exact remote command it launched before the restart, instead of
        starting a duplicate or falling back to legacy ``run.log/run.exit`` files.
        """
        operation_id = self._validate_operation_id(context.operation)
        # Re-read the pointer after recovering the pid/pgid.  A concurrent launch
        # may rotate ``current`` while these files are being inspected; observing
        # it is safe, but returning it as the current logical operation is not.
        for _ in range(2):
            attempt_id = await self.current_attempt_id(operation_id)
            if attempt_id is None:
                return None
            handle = await self._recover_started_handle(
                operation_id=operation_id,
                attempt_id=attempt_id,
                context=context,
            )
            if handle is None:
                return None
            if await self.current_attempt_id(operation_id) == attempt_id:
                return handle
        return None

    async def _read_int(self, path: str) -> int | None:
        result = await self._run(f"cat {path} 2>/dev/null", 60)
        if result.exit_status != 0:
            return None
        try:
            return int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    async def _read_positive_int(self, path: str) -> int | None:
        """读 pid / pgid 专用：非正数一律当读失败。

        这些值会拼进 ``kill -- -<pgid>``。文件被截断或写坏时读出 0 / 负数，
        再送进 kill 就不是"没杀成"而是打偏——0 指调用方自己的进程组，
        -1 在 kill 语义里是"所有能签名的进程"。宁可当没读到，回退到 handle。
        """
        value = await self._read_int(path)
        return value if value is not None and value > 0 else None

    async def snapshot(
        self,
        handle: ManagedCommandHandle,
        *,
        previous_token: str | None = None,
        diagnostic_evidence: dict[str, str] | None = None,
    ) -> CommandSnapshot:
        prefix = self._attempt_prefix(handle.operation_id, handle.attempt_id)
        pid = await self._read_positive_int(f"{prefix}.pid") or handle.process_id
        pgid = await self._read_positive_int(f"{prefix}.pgid") or handle.process_group_id
        started = await self._read_int(f"{prefix}.started") or int(time.time())
        exit_status = await self._read_int(f"{prefix}.exit")
        alive_result = await self._run(f"kill -0 {pid} 2>/dev/null", 60)
        alive = alive_result.exit_status == 0
        stat_result = await self._run(
            f"stat -c '%s %Y' {prefix}.stdout {prefix}.stderr 2>/dev/null",
            60,
        )
        sizes: list[int] = []
        mtimes: list[int] = []
        for line in stat_result.stdout.splitlines():
            try:
                size_text, mtime_text = line.split()[-2:]
                sizes.append(int(size_text))
                mtimes.append(int(mtime_text))
            except (ValueError, IndexError):
                continue
        stdout = await self._run(f"tail -c 8000 {prefix}.stdout 2>/dev/null", 60)
        stderr = await self._run(f"tail -c 8000 {prefix}.stderr 2>/dev/null", 60)
        process = await self._run(
            f"ps -o etimes=,time=,stat=,wchan= -p {pid} 2>/dev/null",
            60,
        )
        cpu_seconds: float | None = None
        process_state: str | None = None
        if process.stdout.strip():
            fields = process.stdout.strip().split()
            if len(fields) >= 3:
                process_state = " ".join(fields[2:])
                try:
                    parts = [int(part) for part in fields[1].split(":")]
                    cpu_seconds = float(
                        sum(
                            value * (60**index)
                            for index, value in enumerate(reversed(parts))
                        )
                    )
                except ValueError:
                    pass
        now = time.time()
        snapshot = CommandSnapshot(
            operation_id=handle.operation_id,
            attempt_id=handle.attempt_id,
            context=handle.context,
            elapsed_seconds=max(0.0, now - started),
            process_alive=alive,
            exit_status=exit_status,
            stdout_tail=stdout.stdout if stdout.exit_status == 0 else "",
            stderr_tail=stderr.stdout if stderr.exit_status == 0 else "",
            output_bytes=sum(sizes),
            seconds_since_output=max(0.0, now - max(mtimes or [started])),
            cpu_seconds=cpu_seconds,
            process_state=process_state,
            process_id=pid,
            process_group_id=pgid,
            diagnostic_evidence=diagnostic_evidence,
        )
        snapshot.output_changed = (
            previous_token is not None and snapshot.progress_token != previous_token
        )
        return snapshot

    async def read_output(
        self,
        handle: ManagedCommandHandle,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
    ) -> tuple[list[OutputChunk], int, int]:
        prefix = self._attempt_prefix(handle.operation_id, handle.attempt_id)
        chunks: list[OutputChunk] = []
        next_offsets: list[int] = []
        for stream, offset in (("stdout", stdout_offset), ("stderr", stderr_offset)):
            safe_offset = max(0, int(offset))
            # 走 base64 拿字节：偏移量是字节口径，而 SSH 结果是解码后的 str。
            # 直接用 len(text.encode()) 记账，遇到多字节字符被切断或非法 UTF-8
            # （中文日志、带 unicode 的进度条都会）就会与真实消费字节数错位，
            # 下一轮要么重复吐要么吞掉一段。head -c 同时给单轮读取封顶，
            # 免得两次轮询之间刷了几百 MB 日志被整包拉回后端。
            result = await self._run(
                f"tail -c +{safe_offset + 1} {prefix}.{stream} 2>/dev/null "
                f"| head -c {_MAX_OUTPUT_CHUNK_BYTES} | base64 | tr -d '\\n'",
                60,
            )
            raw = b""
            if result.exit_status == 0 and result.stdout.strip():
                with contextlib.suppress(Exception):
                    raw = base64.b64decode(result.stdout.strip(), validate=True)
            next_offset = safe_offset + len(raw)
            next_offsets.append(next_offset)
            if raw:
                chunks.append(
                    OutputChunk(
                        stream=stream,
                        text=raw.decode("utf-8", errors="replace"),
                        offset=next_offset,
                    )
                )
        return chunks, next_offsets[0], next_offsets[1]

    async def diagnose(self, handle: ManagedCommandHandle) -> dict[str, str]:
        pid = int(handle.process_id)
        probes = {
            "process_tree": (
                f"ps -o pid,ppid,pgid,stat,etime,time,wchan:32,args -p {pid} --no-headers "
                f"2>/dev/null; ps --ppid {pid} -o pid,ppid,pgid,stat,etime,time,args "
                "--no-headers 2>/dev/null"
            ),
            "resources": "LC_ALL=C free -m 2>/dev/null; df -Pk . 2>/dev/null",
            "network": f"ss -tpn 2>/dev/null | grep -F 'pid={pid},' | head -n 20",
        }
        evidence: dict[str, str] = {}
        for name, command in probes.items():
            result = await self._run(command, 60)
            evidence[name] = redact_text(result.stdout or result.stderr or "unavailable", tail=True)
        return evidence

    async def gpu_usage(self, handle: ManagedCommandHandle) -> ManagedGPUUsage:
        """Return GPU use attributable to this attempt, never machine-wide use.

        The process group is the durable identity of a managed command.  We
        deliberately do not treat an unrelated PID from ``nvidia-smi`` as this
        command merely because it runs on the same host.
        """
        current = await self.current_attempt_id(handle.operation_id)
        if current != handle.attempt_id:
            return ManagedGPUUsage(status="superseded", process_alive=False)
        snapshot = await self.snapshot(handle)
        if not snapshot.process_alive:
            return ManagedGPUUsage(status="exited", process_alive=False)

        pgid = int(snapshot.process_group_id or handle.process_group_id)
        processes = await self._run("ps -eo pid=,ppid=,pgid= 2>/dev/null", 60)
        if processes.exit_status != 0:
            return ManagedGPUUsage(status="process_probe_unavailable", process_alive=True)

        rows: dict[int, tuple[int, int]] = {}
        for line in processes.stdout.splitlines():
            try:
                pid, ppid, row_pgid = (int(value) for value in line.split()[:3])
            except (ValueError, IndexError):
                continue
            rows[pid] = (ppid, row_pgid)
        related = {pid for pid, (_, row_pgid) in rows.items() if row_pgid == pgid}
        # Include descendants as a defensive fallback for shells which create a
        # separate process group.  This remains command-scoped and does not use
        # executable-name adapters.
        changed = True
        while changed:
            changed = False
            for pid, (ppid, _) in rows.items():
                if ppid in related and pid not in related:
                    related.add(pid)
                    changed = True

        gpu = await self._run(
            "nvidia-smi --query-compute-apps=pid,used_memory "
            "--format=csv,noheader,nounits 2>/dev/null",
            60,
        )
        if gpu.exit_status != 0:
            return ManagedGPUUsage(status="gpu_probe_unavailable", process_alive=True)
        matches: list[int] = []
        used_memory = 0
        for line in gpu.stdout.splitlines():
            fields = [field.strip() for field in line.split(",", 1)]
            try:
                pid = int(fields[0])
                memory = int(fields[1])
            except (ValueError, IndexError):
                continue
            if pid in related:
                matches.append(pid)
                used_memory += max(0, memory)
        return ManagedGPUUsage(
            status="active" if matches else "idle",
            process_alive=True,
            process_ids=tuple(sorted(matches)),
            used_memory_mib=used_memory,
        )

    async def stop(self, handle: ManagedCommandHandle) -> ManagedStopResult:
        """Stop only the still-current process identity represented by ``handle``.

        The former implementation checked ``current`` and process metadata in
        separate SSH commands before signalling.  A new attempt (or PID reuse)
        could slip into those gaps.  This script shares the launch lock and
        validates attempt, PID/PGID, and (when available) Linux start ticks in
        the same remote critical section which sends the signal.
        """
        operation_dir = self._operation_dir(handle.operation_id)
        prefix = self._attempt_prefix(handle.operation_id, handle.attempt_id)
        expected_pid = int(handle.process_id)
        expected_pgid = int(handle.process_group_id)
        if expected_pid <= 0 or expected_pgid <= 0:
            return ManagedStopResult(status="invalid_identity", confirmed=False)
        script = (
            "#!/usr/bin/env bash\n"
            "# polaris-managed-stop\n"
            "set +e\n"
            f"operation_dir={operation_dir}\n"
            f"prefix={prefix}\n"
            f"expected_attempt={handle.attempt_id}\n"
            f"expected_pid={expected_pid}\n"
            f"expected_pgid={expected_pgid}\n"
            "lock=${operation_dir}/launch.lock\n"
            "i=0\n"
            "until mkdir \"$lock\" 2>/dev/null; do\n"
            "  i=$((i+1)); [ $i -lt 40 ] || { printf 'lock_busy\\n'; exit 75; }\n"
            "  sleep 0.25\n"
            "done\n"
            "trap 'rmdir \"$lock\" 2>/dev/null || true' EXIT\n"
            "current=$(cat \"${operation_dir}/current\" 2>/dev/null)\n"
            "[ \"$current\" = \"$expected_attempt\" ] || "
            "{ printf 'attempt_changed\\n'; exit 76; }\n"
            "pid=$(cat \"${prefix}.pid\" 2>/dev/null)\n"
            "pgid=$(cat \"${prefix}.pgid\" 2>/dev/null)\n"
            "case $pid:$pgid in *[!0-9:]*|:*) printf 'invalid_identity\\n'; exit 76;; esac\n"
            "[ \"$pid\" = \"$expected_pid\" ] && [ \"$pgid\" = \"$expected_pgid\" ] || "
            "{ printf 'identity_changed\\n'; exit 76; }\n"
            "if ! kill -0 \"$pid\" 2>/dev/null; then printf 'already_exited\\n'; exit 0; fi\n"
            "saved_ticks=$(cat \"${prefix}.start_ticks\" 2>/dev/null)\n"
            "if [ -n \"$saved_ticks\" ]; then\n"
            "  current_ticks=$(awk '{print $22}' \"/proc/$pid/stat\" 2>/dev/null)\n"
            "  [ \"$current_ticks\" = \"$saved_ticks\" ] || "
            "{ printf 'process_reused\\n'; exit 76; }\n"
            "fi\n"
            "kill -TERM -- -\"$pgid\" 2>/dev/null || true\n"
            "i=0\n"
            "while kill -0 \"$pid\" 2>/dev/null && [ $i -lt 20 ]; do "
            "sleep 0.25; i=$((i+1)); done\n"
            "if kill -0 \"$pid\" 2>/dev/null; then\n"
            "  kill -KILL -- -\"$pgid\" 2>/dev/null || true\n"
            "  sleep 0.25\n"
            "fi\n"
            "if kill -0 \"$pid\" 2>/dev/null; then printf 'stop_unconfirmed\\n'; exit 1; fi\n"
            "printf 'stopped\\n'\n"
        )
        result = await self._run(script, 70)
        status = (result.stdout or "").strip().splitlines()
        outcome = status[-1] if status else "stop_unconfirmed"
        return ManagedStopResult(
            status=outcome,
            confirmed=result.exit_status == 0 and outcome in {"stopped", "already_exited"},
        )
