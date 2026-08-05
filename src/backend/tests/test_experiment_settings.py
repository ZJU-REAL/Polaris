"""实验全局设置：字段校验 + 注入 env.sh / codegen 提示词。"""

import pytest

from app.agents.voyage import actions_experiment as ax
from app.services import experiment_settings as es


def test_validate_accepts_empty_and_normalizes_unknown_keys():
    """空值 = 不配置，合法；未知键丢弃，不让它混进 env.sh。"""
    assert es.validate({}) == es.DEFAULTS
    out = es.validate({"model_root": " /hf/model ", "不认识的键": "x"})
    assert out["model_root"] == "/hf/model"  # 顺手去掉首尾空白
    assert "不认识的键" not in out


def test_validate_rejects_shell_injection_and_traversal():
    """这些值会拼进远端 shell 的 export，非法字符必须挡在服务层。"""
    for bad in ("/hf/model; rm -rf /", "/hf/$(whoami)", "/hf/model && curl evil", "relative/path"):
        with pytest.raises(es.InvalidExperimentSettingError) as exc:
            es.validate({"model_root": bad})
        assert exc.value.field == "model_root"  # 报错要指出是哪个字段
    with pytest.raises(es.InvalidExperimentSettingError):
        es.validate({"model_root": "/hf/../etc"})  # 路径穿越
    for bad_url in ("ftp://x/y", "javascript:alert(1)", "https://pypi.org/simple; rm -rf /"):
        with pytest.raises(es.InvalidExperimentSettingError):
            es.validate({"pip_index_url": bad_url})


def test_validate_accepts_real_values():
    ok = es.validate(
        {
            "model_root": "/hf/model",
            "dataset_root": "~/data",
            "pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "hf_endpoint": "https://hf-mirror.com",
            "proxy_url": "http://10.205.2.46:7899",
        }
    )
    assert ok["model_root"] == "/hf/model"
    assert ok["pip_index_url"].endswith("/simple")


class _Ctx:
    """_platform_env_files 只用到 ctx 的 params，给个最小替身。"""

    def __init__(self, params: dict | None = None) -> None:
        self.run = type("R", (), {"checkpoint": {"params": params or {}}})()
        self.checkpoint = {"params": params or {}}


def test_env_sh_exports_configured_values():
    env = ax._platform_env_files(
        _Ctx(),
        proxy_url="http://10.205.2.46:7899",
        env_settings={
            "model_root": "/hf/model",
            "dataset_root": "/data",
            "pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "hf_endpoint": "https://hf-mirror.com",
            "proxy_url": "",
        },
    )["env.sh"]
    assert "export POLARIS_MODEL_ROOT=/hf/model" in env
    assert "export POLARIS_DATASET_ROOT=/data" in env
    assert "export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in env
    assert "export HF_ENDPOINT=https://hf-mirror.com" in env
    assert "http_proxy=http://10.205.2.46:7899" in env


def test_env_sh_omits_unconfigured_values():
    """没配的项一行都不写——env.sh 里不该出现空值导出。"""
    env = ax._platform_env_files(_Ctx(), env_settings=dict(es.DEFAULTS))["env.sh"]
    for key in ("POLARIS_MODEL_ROOT", "POLARIS_DATASET_ROOT", "PIP_INDEX_URL", "HF_ENDPOINT"):
        assert key not in env
    assert "export POLARIS_WORKDIR=$(pwd)" in env  # 恒定项还在


def test_codegen_prompt_states_model_root_as_fact():
    """模型路径必须作为事实进提示词——6c5df454 就是靠猜路径猜错才失败的。"""
    facts = ax._env_facts_prompt({"model_root": "/hf/model"})
    assert "/hf/model" in facts
    assert "/hf/model/Qwen/Qwen3-1.7B" in facts  # 给出完整示例，别让它少一层
    assert ax._env_facts_prompt(dict(es.DEFAULTS)) == ""  # 没配就不塞噪声


# ---- 失败分类器：样本取自真实失败的 stderr ----


def test_diagnose_model_path_points_at_configured_root():
    """voyage 6c5df454 的两次真实报错，都该归到「路径不存在」并指向配好的根目录。"""
    env = {"model_root": "/hf/model", "pip_index_url": ""}
    first = (
        "huggingface_hub.errors.HFValidationError: Repo id must be in the form "
        "'repo_name' or 'namespace/repo_name': '/hf/Qwen/Qwen3-1.7B'."
    )
    second = (
        "OSError: Can't load the configuration of '/hf/Qwen/Qwen3-1.7B'. If you were "
        "trying to load it from 'https://huggingface.co/models', make sure ..."
    )
    for err in (first, second):
        hint = ax.diagnose_failure(err, env)
        assert "/hf/model" in hint
        assert "/hf/model/Qwen/Qwen3-1.7B" in hint  # 给完整示例，别让它又少一层


def test_diagnose_model_path_without_configured_root():
    """没配根目录时不能瞎指路，只能建议改用可下载的 HF 名。"""
    hint = ax.diagnose_failure("huggingface_hub.errors.HFValidationError: Repo id ...", {})
    assert "HF 名" in hint
    assert "根目录" not in hint.replace("本机模型根目录", "")  # 不编造一个不存在的路径


def test_diagnose_other_known_signatures():
    assert "requirements.txt" in ax.diagnose_failure("ModuleNotFoundError: No module named 'x'", {})
    assert "显存" in ax.diagnose_failure("torch.cuda.OutOfMemoryError: CUDA out of memory", {})
    net = ax.diagnose_failure(
        "ERROR: Could not find a version that satisfies the requirement foo",
        {"pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple"},
    )
    assert "pypi.tuna.tsinghua.edu.cn" in net


def test_diagnose_stays_silent_when_unsure():
    """认不出就返回空串——宁可不提示，也不能给条误导的诊断。"""
    assert ax.diagnose_failure("Traceback: some entirely novel failure", {}) == ""
    assert ax.diagnose_failure("", {}) == ""


# ---- 指标里的 NaN/Inf 不能进 JSONB（voyage 6c5df454 第 1 轮运行的真实故障）----


def test_metric_lines_drop_non_finite_values():
    """NaN/Inf 必须在解析阶段丢掉：它们不是合法 JSON，进 JSONB 会被 Postgres 拒收，
    连累整轮运行判死。样本取自 6c5df454 实际产出的指标名。"""
    log = (
        'POLARIS_METRIC {"name": "accuracy/Qwen3-1.7B/baseline", "step": 1, "value": 0.5}\n'
        'POLARIS_METRIC {"name": "consistency/Qwen3-1.7B/baseline", "step": 1, "value": NaN}\n'
        'POLARIS_METRIC {"name": "ratio", "step": 1, "value": Infinity}\n'
        'POLARIS_METRIC {"name": "neg", "step": 1, "value": -Infinity}\n'
    )
    points = ax.parse_metric_lines(log)
    names = [p["name"] for p in points]
    assert names == ["accuracy/Qwen3-1.7B/baseline"]  # 只留有限值，其余整点丢弃

    # 关键断言：结果必须能被严格 JSON 序列化（allow_nan=False 即 Postgres 的口径）
    import json as _json

    _json.dumps(points, allow_nan=False)


def test_metrics_json_drops_non_finite_values():
    """metrics.json 两种形态（裸数值 / 点列表）都要过滤。"""
    points = ax.parse_metrics_json(
        '{"good": 1.5, "bad": NaN, "series": [{"step": 1, "value": 2.0},'
        ' {"step": 2, "value": NaN}], "flag": true}'
    )
    assert [(p["name"], p["value"]) for p in points] == [("good", 1.5), ("series", 2.0)]
    import json as _json

    _json.dumps(points, allow_nan=False)  # bool 也被挡掉，不会混进指标


# ---- 完成标准必须要求「有结果」（voyage 6c5df454：三轮两败仍宣告 done）----


def test_min_count_falls_back_to_checkpoint():
    """voyage 级完成标准求值时 observation 恒为 None，只能从 checkpoint 取。"""
    from app.agents.voyage import checks

    check = {"kind": "min_count", "field": "iterate.primary_metric_runs", "value": 1}
    # observation 没有该字段 → 回落 checkpoint
    assert checks._check_min_count(check, {}, {"iterate": {"primary_metric_runs": 2}}) is None
    # finalize 传的就是 None
    assert checks._check_min_count(check, None, {"iterate": {"primary_metric_runs": 1}}) is None
    # 一轮都没产出主指标 → 必须判失败，且理由能说清
    reason = checks._check_min_count(check, None, {"iterate": {"stopped_reason": "max_runs"}})
    assert reason and "primary_metric_runs" in reason


def test_experiment_done_criteria_requires_a_result():
    """光有「停止原因 + 报告」不够——那正是 6c5df454 假成功的组合。"""
    from app.agents.voyage import navigator
    from app.agents.voyage.checks import run_deterministic_checks

    criteria = navigator.done_criteria_for_kind("experiment")
    checks_list = criteria["checks"]

    # 三轮全废：有停止原因、有报告，但没有任何一轮产出主指标 → 不许 done
    empty_result = {"iterate": {"stopped_reason": "max_runs"}, "report_done": True}
    verdict, _ = run_deterministic_checks(checks_list, observation=None, checkpoint=empty_result)
    assert verdict is not None and not verdict["passed"]

    # 至少一轮有主指标 → 通过
    with_result = {
        "iterate": {"stopped_reason": "max_runs", "primary_metric_runs": 1},
        "report_done": True,
    }
    verdict, _ = run_deterministic_checks(checks_list, observation=None, checkpoint=with_result)
    assert verdict is None or verdict["passed"]
