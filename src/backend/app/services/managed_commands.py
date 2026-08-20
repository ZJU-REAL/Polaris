"""Executor-neutral evidence, timeout adjudication, and repair policy.

This module deliberately knows nothing about Docker, pip, git, or a specific
shell command.  Execution backends provide :class:`CommandSnapshot` objects;
the model may assess the evidence, but deterministic policy owns interruption
and mutation decisions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

_TAIL_CHARS = 8000
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [^-]+PRIVATE KEY-----.*?-----END [^-]+PRIVATE KEY-----",
            re.S,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
        r"\1[REDACTED]",
    ),
    (
        # 名字以这些词结尾即算敏感：HF_TOKEN / AWS_SECRET_ACCESS_KEY / OPENAI_API_KEY…
        # 不能只用 \b 起头——下划线是单词字符，\btoken 在 HF_TOKEN 里根本匹配不上，
        # 而实验脚本里的密钥几乎全是这种带前缀的环境变量名。
        re.compile(
            r"(?i)([A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?key|token|password|passwd|secret)"
            r"[A-Za-z0-9_.-]*)\s*[:=]\s*([^\s,;]+)"
        ),
        r"\1=[REDACTED]",
    ),
    (
        # 常见的自带前缀密钥：OpenAI sk-…、GitHub ghp_/gho_/ghs_/github_pat_、HF hf_、Slack xox…
        re.compile(
            r"(?i)\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
            r"|github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,})"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(r"(?i)(--(?:api[_-]?key|token|password|secret)\s+)[^\s]+"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@"), r"\1[REDACTED]@"),
)


def redact_text(value: Any, *, tail: bool = False) -> str:
    text = str(value or "")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[-_TAIL_CHARS:] if tail else text


class CommandState(StrEnum):
    PROGRESSING = "progressing"
    SLOW = "slow"
    STALLED = "stalled"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"


class CommandAction(StrEnum):
    CONTINUE_MONITORING = "continue_monitoring"
    EXTEND_OBSERVATION = "extend_observation"
    RUN_DIAGNOSTIC = "run_diagnostic"
    ASK_USER_WHILE_RUNNING = "ask_user_while_running"
    STOP_AND_REPAIR = "stop_and_repair"
    REPORT_FAILURE = "report_failure"
    MARK_SUCCEEDED = "mark_succeeded"


class FailureDomain(StrEnum):
    TRANSPORT = "transport"
    AUTHENTICATION = "authentication"
    EXTERNAL_SERVICE = "external_service"
    ARTIFACT = "artifact"
    ENVIRONMENT = "environment"
    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    APPLICATION = "application"
    UNKNOWN = "unknown"


class RepairScope(StrEnum):
    NONE = "none"
    CONNECTION = "connection"
    INFRASTRUCTURE = "infrastructure"
    DEPENDENCY_FILES = "dependency_files"
    APPLICATION_FILES = "application_files"
    EXPERIMENT_PLAN = "experiment_plan"
    USER_ACTION = "user_action"


@dataclass(slots=True, frozen=True)
class OperationContext:
    phase: str
    operation: str
    display_command: str
    target: str | None = None
    soft_timeout_seconds: float | None = None
    stall_timeout_seconds: float | None = None
    hard_timeout_seconds: float | None = None
    repair_scope: RepairScope = RepairScope.NONE


@dataclass(slots=True)
class CommandSnapshot:
    operation_id: str
    attempt_id: str
    context: OperationContext
    elapsed_seconds: float
    process_alive: bool
    exit_status: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_bytes: int = 0
    output_changed: bool = False
    seconds_since_output: float = 0
    cpu_seconds: float | None = None
    process_state: str | None = None
    process_id: int | None = None
    process_group_id: int | None = None
    diagnostic_evidence: dict[str, str] | None = None

    @property
    def progress_token(self) -> str:
        material = "\0".join(
            (
                str(self.output_bytes),
                self.stdout_tail[-1000:],
                self.stderr_tail[-1000:],
                str(self.cpu_seconds),
                str(self.exit_status),
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        context = asdict(self.context)
        # display_command / target 同样可能带密钥（命令行里的 --token 等），
        # 它们跟着快照一路进 API、前端和日志，必须和 tail 一样脱敏。
        context["display_command"] = redact_text(context.get("display_command"))
        if context.get("target"):
            context["target"] = redact_text(context["target"])
        data["context"] = context
        data["progress_token"] = self.progress_token
        data["stdout_tail"] = redact_text(self.stdout_tail, tail=True)
        data["stderr_tail"] = redact_text(self.stderr_tail, tail=True)
        if self.diagnostic_evidence:
            data["diagnostic_evidence"] = {
                key: redact_text(value, tail=True)
                for key, value in self.diagnostic_evidence.items()
            }
        return data


@dataclass(slots=True, frozen=True)
class ModelAssessment:
    state: CommandState
    confidence: float
    reason: str
    evidence: tuple[str, ...] = ()
    proposed_action: CommandAction = CommandAction.CONTINUE_MONITORING
    next_check_seconds: float = 30
    safe_to_interrupt: bool = False
    user_message: str | None = None


@dataclass(slots=True, frozen=True)
class CommandVerdict:
    action: CommandAction
    reason: str
    next_check_seconds: float = 0


def adjudicate_command(
    snapshot: CommandSnapshot,
    assessment: ModelAssessment | None,
    *,
    silent_extensions: int = 0,
    diagnostics_run: int = 0,
) -> CommandVerdict:
    """Apply bounded safety policy to evidence and optional model advice."""
    if snapshot.exit_status == 0:
        return CommandVerdict(CommandAction.MARK_SUCCEEDED, "command exited successfully")
    if snapshot.exit_status is not None:
        return CommandVerdict(
            CommandAction.REPORT_FAILURE,
            f"command exited with status {snapshot.exit_status}",
        )

    delay = 30.0
    if assessment is not None:
        delay = max(5.0, min(float(assessment.next_check_seconds), 600.0))

    if snapshot.process_alive and snapshot.output_changed:
        return CommandVerdict(
            CommandAction.CONTINUE_MONITORING,
            "remote process is alive and output changed",
            delay,
        )

    stalled = bool(
        snapshot.context.stall_timeout_seconds
        and snapshot.seconds_since_output >= snapshot.context.stall_timeout_seconds
    )
    hard_expired = bool(
        snapshot.context.hard_timeout_seconds
        and snapshot.elapsed_seconds >= snapshot.context.hard_timeout_seconds
    )
    if hard_expired:
        return CommandVerdict(
            CommandAction.ASK_USER_WHILE_RUNNING,
            "explicit hard deadline reached while the process is still alive",
            delay,
        )

    if (
        assessment is None
        or assessment.state == CommandState.UNKNOWN
        or assessment.confidence < 0.6
    ):
        if stalled:
            return CommandVerdict(
                CommandAction.ASK_USER_WHILE_RUNNING,
                "stall threshold reached without a reliable assessment",
                delay,
            )
        return CommandVerdict(
            CommandAction.CONTINUE_MONITORING,
            "insufficient evidence; preserve the remote process",
            delay,
        )

    if assessment.proposed_action == CommandAction.ASK_USER_WHILE_RUNNING:
        return CommandVerdict(
            CommandAction.ASK_USER_WHILE_RUNNING,
            "model recommends user review while preserving the process",
            delay,
        )

    if assessment.state == CommandState.SLOW:
        if silent_extensions >= 1 and snapshot.process_alive:
            return CommandVerdict(
                CommandAction.ASK_USER_WHILE_RUNNING,
                "slow operation already received one silent extension",
                delay,
            )
        return CommandVerdict(
            CommandAction.EXTEND_OBSERVATION,
            "healthy but slow operation receives one bounded extension",
            delay,
        )

    wants_stop = assessment.proposed_action == CommandAction.STOP_AND_REPAIR
    if assessment.state in {CommandState.STALLED, CommandState.FAILED} or wants_stop:
        if diagnostics_run < 1:
            return CommandVerdict(
                CommandAction.RUN_DIAGNOSTIC,
                "one read-only diagnostic is required before interruption",
                delay,
            )
        if stalled and assessment.confidence >= 0.85 and assessment.safe_to_interrupt:
            return CommandVerdict(
                CommandAction.STOP_AND_REPAIR,
                "high-confidence sustained stall confirmed after diagnostics",
            )
        return CommandVerdict(
            CommandAction.ASK_USER_WHILE_RUNNING,
            "interruption is not justified by deterministic evidence",
            delay,
        )

    if assessment.proposed_action == CommandAction.RUN_DIAGNOSTIC:
        if diagnostics_run >= 1:
            return CommandVerdict(
                CommandAction.ASK_USER_WHILE_RUNNING,
                "diagnostic already ran without observable progress",
                delay,
            )
        return CommandVerdict(CommandAction.RUN_DIAGNOSTIC, "run one diagnostic", delay)

    return CommandVerdict(
        CommandAction.CONTINUE_MONITORING,
        "model advice passed the non-destructive safety policy",
        delay,
    )


@dataclass(slots=True)
class FailureReport:
    phase: str
    operation: str
    command_display: str
    domain: FailureDomain
    exception_type: str
    message: str
    repair_scope: RepairScope
    target: str | None = None
    exit_status: int | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    elapsed_seconds: float | None = None
    cause_chain: list[str] = field(default_factory=list)
    fingerprint: str = ""
    progress_token: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify(context: OperationContext, evidence: str) -> FailureDomain:
    text = evidence.lower()
    if any(item in text for item in ("permission denied", "authentication failed", "invalid key")):
        return FailureDomain.AUTHENTICATION
    if any(item in text for item in ("connection lost", "connection closed", "broken pipe")):
        return FailureDomain.TRANSPORT
    if any(item in text for item in ("out of memory", "cuda oom", "no space left")):
        return FailureDomain.RESOURCE
    phase = context.phase.lower()
    for prefix, domain in (
        ("transport.", FailureDomain.TRANSPORT),
        ("artifact.", FailureDomain.ARTIFACT),
        ("environment.", FailureDomain.ENVIRONMENT),
        ("dependency.", FailureDomain.DEPENDENCY),
        ("application.", FailureDomain.APPLICATION),
        ("external_service.", FailureDomain.EXTERNAL_SERVICE),
    ):
        if phase.startswith(prefix):
            return domain
    return FailureDomain.UNKNOWN


def failure_from_snapshot(snapshot: CommandSnapshot) -> FailureReport:
    context = snapshot.context
    message = snapshot.stderr_tail or snapshot.stdout_tail or "command failed without output"
    evidence = f"{message}\n{snapshot.stdout_tail}\n{snapshot.stderr_tail}"
    normalized = re.sub(r"\d+", "N", evidence[-2000:])
    fingerprint = hashlib.sha256(
        f"{context.phase}\0{normalized}".encode()
    ).hexdigest()[:16]
    return FailureReport(
        phase=context.phase,
        operation=context.operation,
        command_display=redact_text(context.display_command),
        domain=_classify(context, evidence),
        exception_type="CommandExitError",
        message=redact_text(message, tail=True),
        repair_scope=context.repair_scope,
        target=redact_text(context.target) if context.target else None,
        exit_status=snapshot.exit_status,
        stdout_tail=redact_text(snapshot.stdout_tail, tail=True) or None,
        stderr_tail=redact_text(snapshot.stderr_tail, tail=True) or None,
        elapsed_seconds=snapshot.elapsed_seconds,
        fingerprint=fingerprint,
        progress_token=snapshot.progress_token,
    )


def failure_from_exception(
    exception: BaseException,
    context: OperationContext | None = None,
) -> FailureReport:
    """Preserve AsyncSSH/http client partial evidence instead of relying on str()."""
    command = getattr(exception, "command", None)
    stdout = redact_text(getattr(exception, "stdout", ""), tail=True)
    stderr = redact_text(getattr(exception, "stderr", ""), tail=True)
    exit_status = getattr(exception, "exit_status", None)
    if exit_status is None:
        exit_status = getattr(exception, "returncode", None)
    exception_context = getattr(exception, "operation_context", None)
    if not isinstance(exception_context, OperationContext):
        exception_context = None
    context = context or exception_context or OperationContext(
        phase="unknown",
        operation="unknown",
        display_command=str(command or "unknown command"),
    )
    message = redact_text(str(exception), tail=True).strip()
    if not message:
        message = stderr or stdout or type(exception).__name__
    evidence = f"{type(exception).__name__}\n{message}\n{stdout}\n{stderr}"
    normalized = re.sub(r"\d+", "N", evidence[-2000:])
    fingerprint = hashlib.sha256(
        f"{context.phase}\0{normalized}".encode()
    ).hexdigest()[:16]
    cause_chain: list[str] = []
    current: BaseException | None = exception
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(cause_chain) < 8:
        seen.add(id(current))
        cause_chain.append(
            redact_text(f"{type(current).__name__}: {str(current).strip()}", tail=True)
        )
        current = current.__cause__ or current.__context__
    return FailureReport(
        phase=context.phase,
        operation=context.operation,
        command_display=redact_text(str(command or context.display_command)),
        domain=_classify(context, evidence),
        exception_type=type(exception).__name__,
        message=message,
        repair_scope=context.repair_scope,
        target=redact_text(context.target) if context.target else None,
        exit_status=int(exit_status) if exit_status is not None else None,
        stdout_tail=stdout or None,
        stderr_tail=stderr or None,
        cause_chain=cause_chain,
        fingerprint=fingerprint,
        progress_token=fingerprint,
    )


@dataclass(slots=True, frozen=True)
class RecoveryPlan:
    diagnosis: str
    confidence: float
    repair_scope: RepairScope
    proposed_changes: tuple[str, ...]
    expected_evidence: str
    minimal_retry: str


def may_apply_recovery_automatically(
    plan: RecoveryPlan,
    *,
    repeated_without_progress: int,
) -> bool:
    """Only bounded generated-file repairs may proceed without user approval."""
    return bool(
        plan.confidence >= 0.85
        and repeated_without_progress < 2
        and plan.repair_scope
        in {RepairScope.DEPENDENCY_FILES, RepairScope.APPLICATION_FILES}
        and plan.proposed_changes
    )
