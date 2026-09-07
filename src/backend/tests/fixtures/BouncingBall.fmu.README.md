# BouncingBall.fmu 来源与许可

- 来源：Modelica Association [Reference-FMUs](https://github.com/modelica/Reference-FMUs)
  v0.0.39 发布包 `Reference-FMUs-0.0.39.zip` 内的 `2.0/BouncingBall.fmu`（FMI 2.0
  co-simulation，弹跳小球模型，变量 h/v/g/e）。
- 许可：2-Clause BSD（Copyright (c) 2024, Modelica Association Project "FMI"），
  与本仓库测试夹具用途兼容。原文见发布包 LICENSE.txt。
- 用途：`tests/test_runner_fmu.py` 的 fmu 后端端到端用例（#682）。注意包内只带
  x86_64 的 darwin64/linux64/win 二进制；arm64 环境的用例会尝试用包内 sources/
  现场编译，编译器缺席则 skip。
