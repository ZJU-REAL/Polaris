"""用户目录的流程包（#763 后续：把「流程本身插件化」这条信条接到用户手里）。

模块头本来就写着「用户数据目录的包后续接入」——这里把它做完。

为什么要紧：一条 CAD→CAE 流水线（建模 → 网格 → 求解 → 分析 → 回到建模）就是一个
流程包。在此之前它只能靠改仓库来表达，所以「流程本身插件化」在设计信条里有、
在用户手里没有。
"""

import textwrap

import pytest
import yaml

from app.services import process_packs as pp


def _pack(name: str, *, phases: list[dict] | None = None) -> dict:
    return {
        "kind": "research-process",
        "name": name,
        "phases": phases or [{"id": "run", "actions": ["experiment.run"]}],
    }


@pytest.fixture
def user_packs(tmp_path, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    directory = tmp_path / "packs"
    directory.mkdir(parents=True)
    return directory


def test_builtin_packs_still_load_without_a_user_dir(user_packs):
    # 用户目录存在但为空：内置包照常
    assert "base/experiment" in pp.known_pack_names()


def test_user_pack_becomes_selectable(user_packs):
    (user_packs / "mine.yaml").write_text(
        yaml.safe_dump(_pack("cae/impact"), allow_unicode=True), encoding="utf-8"
    )
    names = pp.known_pack_names()
    assert "cae/impact" in names
    # 追加而不是替换：内置包仍在
    assert "base/experiment" in names


def test_a_broken_user_pack_does_not_take_down_the_others(user_packs):
    """手写的包写坏了是常态，不该让别的包连同整个实验流程一起停摆。"""
    (user_packs / "broken.yaml").write_text("phases: [oops", encoding="utf-8")
    (user_packs / "good.yaml").write_text(
        yaml.safe_dump(_pack("cae/good"), allow_unicode=True), encoding="utf-8"
    )
    names = pp.known_pack_names()
    assert "cae/good" in names
    assert "base/experiment" in names


def test_a_broken_builtin_pack_still_fails_loudly(monkeypatch, tmp_path):
    """不对称是有意的：内置包坏了是仓库的 bug，早炸早知道。"""
    bad_builtin = tmp_path / "builtin"
    bad_builtin.mkdir()
    (bad_builtin / "broken.yaml").write_text("phases: [oops", encoding="utf-8")
    monkeypatch.setattr(pp, "BUILTIN_PACKS_DIR", bad_builtin)
    with pytest.raises(pp.ProcessPackError):
        pp.known_pack_names()


def test_user_pack_overrides_a_builtin_of_the_same_name(user_packs):
    """磁盘上的文件是真相（file-over-app）。可预测胜过替用户做主。"""
    override = _pack("base/experiment", phases=[{"id": "only", "actions": ["experiment.run"]}])
    override["guidance"] = "我自己的实验流程"
    (user_packs / "override.yaml").write_text(
        yaml.safe_dump(override, allow_unicode=True), encoding="utf-8"
    )
    pack = pp.load_pack("base/experiment")
    assert pack.guidance == "我自己的实验流程"
    assert [p.id for p in pack.phases] == ["only"]


def test_a_cad_to_cae_pipeline_is_expressible(user_packs):
    """具体判据：一条 CAD→CAE 流水线能不能用流程包表达出来。

    建模 → 网格 → 求解 → 分析，且求解那步声明为循环（拿到结果回去改设计）。
    这正是把专业软件串起来的那条链的形状。
    """
    spec = textwrap.dedent(
        """
        kind: research-process
        name: cae/impact-study
        phases:
          - id: model
            actions: [experiment.run]
          - id: mesh
            actions: [experiment.run]
          - id: solve
            actions: [experiment.run, experiment.analyze]
            loop: { kind: spiral, exit_check: experiment.analyze }
          - id: report
            actions: [experiment.report]
        guidance: |
          参数化建模 → 网格与材料 → 显式动力学求解 → 结果回代设计。
        """
    ).strip()
    (user_packs / "cae.yaml").write_text(spec, encoding="utf-8")

    pack = pp.load_pack("cae/impact-study")
    assert [p.id for p in pack.phases] == ["model", "mesh", "solve", "report"]
    solve = next(p for p in pack.phases if p.id == "solve")
    # 迭代拓扑是这条链的要害：求解结果要能回去改设计，而不是一条直线走完
    assert solve.loop is not None
    assert solve.loop.kind == "spiral"
    assert solve.loop.exit_check == "experiment.analyze"


def test_unknown_pack_lists_what_is_available(user_packs):
    with pytest.raises(pp.ProcessPackError) as exc:
        pp.load_pack("cae/nope")
    # 可诊断：报错要说明有哪些可选，而不是只说「未知」
    assert "base/experiment" in str(exc.value)
