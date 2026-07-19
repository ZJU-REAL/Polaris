"""Runner 抽象的单元测试：现有 SSH 执行器就是 RemoteHostRunner，且满足 kind 无关的 Runner 接口。

外加 ContainerRunner：执行原语应在容器内跑（docker exec 包裹），文件原语走 host 侧；
以及 container 规格的严格白名单校验（防注入）与 open_runner 的声明式分派。"""

import uuid

import pytest

from app.agents.voyage import runner
from app.services import ssh_exec

# Runner 接口应覆盖的 kind 无关原语（实验循环只依赖这些）。
_PRIMITIVES = (
    "workdir",
    "mkdir_workdir",
    "write_files",
    "read_file",
    "list_dir",
    "read_metrics_json",
    "setup_venv",
    "run_smoke",
    "run_plot",
    "launch_run",
    "check_pid",
    "read_exit_code",
    "tail_log",
    "kill_pid",
    "close",
)


def test_remote_host_runner_is_current_ssh_executor():
    """现有行为 = RemoteHostRunner（SSH 主机上的裸机 venv 执行）——零行为变化的解耦。"""
    assert runner.RemoteHostRunner is ssh_exec.SSHExecutor


def test_runner_protocol_covers_primitives():
    for name in _PRIMITIVES:
        assert hasattr(runner.Runner, name), f"Runner 协议缺原语：{name}"


def test_ssh_executor_satisfies_runner():
    """现有 SSHExecutor 结构上满足 Runner（未来 Container/Api Runner 挂同一接口）。"""
    for name in _PRIMITIVES:
        assert hasattr(ssh_exec.SSHExecutor, name), f"SSHExecutor 缺原语：{name}"


def test_container_runner_is_runner_subclass():
    """ContainerRunner 复用同一接口（继承 SSHExecutor，故文件原语等全部满足 Runner）。"""
    assert issubclass(runner.ContainerRunner, ssh_exec.SSHExecutor)
    for name in _PRIMITIVES:
        assert hasattr(runner.ContainerRunner, name), f"ContainerRunner 缺原语：{name}"


# ---- container 规格白名单校验（安全边界：值会拼进 docker 命令） ----


def test_parse_container_spec_valid_and_defaults():
    spec = runner.parse_container_spec(
        {"image": "verlai/verl:vllm017.latest", "gpus": "device=0,1"}
    )
    assert spec is not None
    assert spec.image == "verlai/verl:vllm017.latest"
    assert spec.gpus == "device=0,1"
    assert spec.shm_size == "16g"  # 默认
    assert spec.mounts == {"~/hf": "/hf:ro"}  # 默认挂载
    assert spec.workdir_mount == "/work"


def test_parse_container_spec_missing_image_is_none():
    """无 image / 非 dict → None（=退回裸机，不用容器）。"""
    assert runner.parse_container_spec(None) is None
    assert runner.parse_container_spec({}) is None
    assert runner.parse_container_spec({"gpus": "all"}) is None
    assert runner.parse_container_spec("verl") is None


@pytest.mark.parametrize(
    "bad_image",
    ["evil; rm -rf /", "img$(whoami)", "a b", "img`id`", "img'x", 'img"x'],
)
def test_parse_container_spec_rejects_injection_in_image(bad_image):
    """非法 image（含 shell 元字符/空格/引号）整体拒绝——绝不进 docker 命令。"""
    assert runner.parse_container_spec({"image": bad_image}) is None


def test_parse_container_spec_drops_bad_gpus_and_mounts():
    """image 合法但 gpus/mounts 非法 → 丢弃该字段回退默认，而非拒绝整个 spec。"""
    spec = runner.parse_container_spec(
        {
            "image": "myimg:1",
            "gpus": "device=0; reboot",  # 非法 → 丢弃
            "shm_size": "16g; rm",  # 非法 → 回退默认
            "mounts": {"/data": "/data", "/bad;x": "/y"},  # 后者非法 → 只留合法项
        }
    )
    assert spec is not None
    assert spec.gpus is None
    assert spec.shm_size == "16g"
    assert spec.mounts == {"/data": "/data"}  # 非法挂载被剔除


# ---- docker exec 命令拼装（纯字符串构造，不触 SSH/DB） ----


def _container_runner(gpus="device=2,3", image="verlai/verl:vllm017.latest"):
    exp_id = str(uuid.uuid4())
    spec = runner.ContainerSpec(image=image, gpus=gpus)
    return runner.ContainerRunner(
        object(),  # 仅测字符串构造，不调用 run，session 用不到
        exp_id=exp_id,
        host="gpu.example",
        project_id=uuid.uuid4(),
        spec=spec,
    )


def test_docker_run_cmd_has_gpus_mounts_and_workdir():
    r = _container_runner(gpus="device=2,3")
    cmd = r._docker_run_cmd()
    assert cmd.startswith("docker run -d")
    assert f"--name polaris_{r.exp_id}" in cmd
    assert "--gpus '\"device=2,3\"'" in cmd  # 多卡需引号形式
    assert "--shm-size 16g" in cmd
    assert "-v ~/hf:/hf:ro" in cmd
    assert f"-v {r.workdir}:/work" in cmd  # host workdir ←→ /work
    assert cmd.endswith("-w /work verlai/verl:vllm017.latest tail -f /dev/null")


def test_docker_run_cmd_gpus_all_and_count():
    assert "--gpus all" in _container_runner(gpus="all")._docker_run_cmd()
    assert "--gpus 4" in _container_runner(gpus="4")._docker_run_cmd()


def test_dexec_wraps_in_container_and_cds_to_workdir():
    r = _container_runner()
    inner = r._dexec_workdir("bash run.sh --smoke")
    assert inner == f"docker exec polaris_{r.exp_id} bash -lc 'cd /work && bash run.sh --smoke'"


def test_dexec_rejects_single_quote_to_avoid_injection():
    r = _container_runner()
    with pytest.raises(ssh_exec.SSHExecError):
        r._dexec("echo 'oops'")
