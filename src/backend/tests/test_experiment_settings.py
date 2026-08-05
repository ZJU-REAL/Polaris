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
