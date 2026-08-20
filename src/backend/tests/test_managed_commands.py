from app.services.managed_commands import (
    CommandAction,
    CommandSnapshot,
    CommandState,
    ModelAssessment,
    OperationContext,
    RecoveryPlan,
    RepairScope,
    adjudicate_command,
    failure_from_snapshot,
    may_apply_recovery_automatically,
    redact_text,
)


def _snapshot(**overrides):
    values = {
        "operation_id": "setup",
        "attempt_id": "attempt-1",
        "context": OperationContext(
            phase="dependency.install",
            operation="install dependencies",
            display_command="pip install -r requirements.txt",
            target="host",
            soft_timeout_seconds=60,
            stall_timeout_seconds=120,
            repair_scope=RepairScope.DEPENDENCY_FILES,
        ),
        "elapsed_seconds": 180,
        "process_alive": True,
        "exit_status": None,
        "stdout_tail": "downloading package",
        "output_bytes": 20,
        "seconds_since_output": 180,
        "process_id": 123,
        "process_group_id": 123,
    }
    values.update(overrides)
    return CommandSnapshot(**values)


def test_progress_is_stronger_than_model_stall_guess():
    snap = _snapshot(output_changed=True)
    advice = ModelAssessment(
        state=CommandState.STALLED,
        confidence=0.99,
        reason="guess",
        proposed_action=CommandAction.STOP_AND_REPAIR,
        safe_to_interrupt=True,
    )
    assert adjudicate_command(snap, advice).action == CommandAction.CONTINUE_MONITORING


def test_unknown_at_stall_threshold_asks_without_stopping():
    advice = ModelAssessment(
        state=CommandState.UNKNOWN,
        confidence=0.2,
        reason="insufficient evidence",
    )
    verdict = adjudicate_command(_snapshot(), advice)
    assert verdict.action == CommandAction.ASK_USER_WHILE_RUNNING


def test_missing_model_fails_safe_and_eventually_asks():
    before = _snapshot(seconds_since_output=30)
    stalled = _snapshot(seconds_since_output=180)
    assert adjudicate_command(before, None).action == CommandAction.CONTINUE_MONITORING
    assert adjudicate_command(stalled, None).action == CommandAction.ASK_USER_WHILE_RUNNING


def test_slow_operation_gets_one_extension_then_user_review():
    advice = ModelAssessment(
        state=CommandState.SLOW,
        confidence=0.9,
        reason="healthy but slow",
        proposed_action=CommandAction.EXTEND_OBSERVATION,
    )
    assert adjudicate_command(_snapshot(), advice).action == CommandAction.EXTEND_OBSERVATION
    assert (
        adjudicate_command(_snapshot(), advice, silent_extensions=1).action
        == CommandAction.ASK_USER_WHILE_RUNNING
    )


def test_high_confidence_stall_requires_diagnostic_before_stop():
    advice = ModelAssessment(
        state=CommandState.STALLED,
        confidence=0.95,
        reason="process is blocked",
        proposed_action=CommandAction.STOP_AND_REPAIR,
        safe_to_interrupt=True,
    )
    assert adjudicate_command(_snapshot(), advice).action == CommandAction.RUN_DIAGNOSTIC
    assert (
        adjudicate_command(_snapshot(), advice, diagnostics_run=1).action
        == CommandAction.STOP_AND_REPAIR
    )


def test_nonzero_exit_returns_real_failure_evidence_and_redacts_secrets():
    snap = _snapshot(
        process_alive=False,
        exit_status=1,
        stderr_tail="token=secret-value\nHTTP 500",
    )
    verdict = adjudicate_command(snap, None)
    report = failure_from_snapshot(snap)
    assert verdict.action == CommandAction.REPORT_FAILURE
    assert report.exit_status == 1
    assert report.stderr_tail is not None
    assert "secret-value" not in report.stderr_tail
    assert report.repair_scope == RepairScope.DEPENDENCY_FILES


def test_automatic_repair_is_confidence_scope_and_progress_bounded():
    safe = RecoveryPlan(
        diagnosis="invalid generated dependency pin",
        confidence=0.9,
        repair_scope=RepairScope.DEPENDENCY_FILES,
        proposed_changes=("requirements.txt",),
        expected_evidence="pip exits zero",
        minimal_retry="dependency.install",
    )
    assert may_apply_recovery_automatically(safe, repeated_without_progress=0)
    assert not may_apply_recovery_automatically(safe, repeated_without_progress=2)
    infrastructure = RecoveryPlan(
        diagnosis="registry unavailable",
        confidence=0.99,
        repair_scope=RepairScope.INFRASTRUCTURE,
        proposed_changes=("requirements.txt",),
        expected_evidence="registry responds",
        minimal_retry="artifact.download",
    )
    assert not may_apply_recovery_automatically(
        infrastructure, repeated_without_progress=0
    )


# ---- 脱敏加固（见 test_managed_ssh 里的输出读取加固） ----


def test_redaction_covers_prefixed_env_var_secrets():
    """密钥在实验脚本里几乎都写成带前缀的环境变量名。

    `\btoken` 的词边界在 `HF_TOKEN` 里根本不成立——下划线是单词字符——所以
    只按裸词匹配会把最常见的一类原样漏出去。
    """
    text = (
        "export HF_TOKEN=hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG\n"
        "export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345\n"
        "MY_DB_PASSWORD=hunter2\n"
    )
    redacted = redact_text(text)
    assert "hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in redacted
    assert "wJalrXUtnFEMI/K7MDENG" not in redacted
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz012345" not in redacted
    assert "hunter2" not in redacted
    assert redacted.count("[REDACTED]") == 4


def test_redaction_covers_self_identifying_key_prefixes():
    """裸密钥（前后没有 key= 形式）靠自身前缀识别。"""
    text = "curl -H 'x: sk-abcdefghijklmnopqrstuvwx' && git push https://ghp_abcdefghijklmnopqrst01"
    redacted = redact_text(text)
    assert "sk-abcdefghijklmnopqrstuvwx" not in redacted
    assert "ghp_abcdefghijklmnopqrst01" not in redacted


def test_redaction_keeps_ordinary_output_intact():
    """别把正常日志误伤：不含密钥的行要原样保留。"""
    text = "Epoch 3/10 loss=0.1234 acc=0.98\nSaving checkpoint to /data/ckpt-3.pt\n"
    assert redact_text(text) == text


def test_snapshot_dict_redacts_the_command_and_target_too():
    """display_command / target 跟着快照进 API 与前端，同样可能带密钥。"""
    snapshot = CommandSnapshot(
        operation_id="experiment-run",
        attempt_id="00000000-0000-0000-0000-000000000001",
        context=OperationContext(
            phase="application.run",
            operation="experiment-run",
            display_command="python train.py --token ghp_abcdefghijklmnopqrst01",
            target="https://user:s3cret@gpu-01",
        ),
        elapsed_seconds=1.0,
        process_alive=True,
        exit_status=None,
        stdout_tail="HF_TOKEN=hf_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
    )
    data = snapshot.to_dict()
    assert "ghp_abcdefghijklmnopqrst01" not in data["context"]["display_command"]
    assert "s3cret" not in data["context"]["target"]
    assert "hf_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in data["stdout_tail"]
