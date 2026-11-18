# BYO Runner（自带执行机）

> Part of the P3 autonomous-experiments track (#674 · #685). Design source:
> [Polaris 2.0 design report §15](https://github.com/ZJU-REAL/Polaris/blob/main/docs/archive/rfcs/2026-09-02-polaris-2.0-design-report.zh.md).

Polaris 把「在哪跑实验」交给用户：实验室 GPU 机、云主机、家里的工作站，
只要 Polaris 能通过 SSH 连上，就能当 runner。

（曾经规划过第二档：机器上跑一个常驻 agent，出站 WebSocket 连回 Polaris。它只做到
传输层，而且需要 Polaris 有一个别的机器连得到的地址；本地引擎只监听 127.0.0.1，
这条路走不通，相关代码已删除。）

## SSH 直连

### 使用步骤

1. **录入 SSH 凭据**：设置页添加主机地址 + 用户名 + 私钥
   （`POST /api/ssh-credentials`，私钥 Fernet 加密入库，绝不回传、不进日志）。
2. **注册 runner 主机**（可选但推荐）：`POST /api/resources/runner-hosts`
   `{"name": "lab-a100", "credential_id": "…"}` —— 生成一个 `host` 类 Resource，
   进入资源/租约体系：同一台机器默认互斥（同一时刻一个实验），可 PATCH 调整
   容量与并发语义。
3. **创建实验**：`POST /projects/{id}/experiments` 时二选一——
   传 `resource_id` 指定跑在哪台注册机器上，或按老路径直接传 `credential_id`。

### Runner agent 版本钉定

学 VS Code Remote-SSH：用户只给地址，安装/升级全自动。每次建立 SSH 连接时，
平台比对远端 `~/.polaris-runner/VERSION` 与当前 `AGENT_VERSION`
（`app/services/byo_runner.py`）：

- 首连（无版本文件）→ 自动推送 agent 载荷（一组远端辅助脚本：环境探测
  `probe.sh`、工作区管理 `workdir.sh`、`run.exit` 退出码约定 `run-status.sh`）；
- 版本一致 → 一条 `cat` 即跳过；
- 版本过期 → 整包重推（VERSION 最后原子写入，中断不会留下半新半旧状态）。

推送失败不阻塞实验执行（agent 目前是辅助契约，执行路径仍走固定模板命令）。

### Ephemeral 执行（默认推荐）

GitHub self-hosted runner 的教训：非 ephemeral 环境的残留物会变成后门。因此：

- **容器化路径（推荐，ephemeral 方向）**：实验计划声明 `plan.container`
  （镜像/GPU/挂载白名单校验）后在 docker 容器内执行，主机侧只落 bind 挂载的
  工作目录。注册 runner 主机时 `ephemeral` 默认 `true` 即指向此路径。
- **裸机路径（non-ephemeral，显式保留）**：不声明 container 时在主机 venv 直跑，
  产物与虚拟环境留在 `~/polaris_runs/<exp_id>`（复查产物依赖这一点）。
  注册时显式传 `"ephemeral": false` 表示接受该语义。

默认值只影响新注册的主机；已有实验与 run 的行为不变。

### 凭据吊销

删除凭据（`DELETE /api/ssh-credentials/{id}` 或
`DELETE /api/connection-credentials/{id}`）即吊销：

- 仍有未终态实验引用该凭据 → **409 `CREDENTIAL_IN_USE`**（不做级联取消：
  替用户杀正在跑的实验副作用太大，先处置活跃实验再吊销）；
- 吊销成功后，关联的 `host` Resource 标记 `config.unavailable=true`
  （资源与租约历史保留，换绑新凭据即可恢复）。

### 密钥安全边界

- 私钥/口令 Fernet 加密入库，任何 API 响应不含密钥字段；
- 解密后的私钥只进 asyncssh 连接参数，不进日志、不进 Activity、不进异常文本
  （连接失败的 detail 会先过 `scrub_secrets` 抹除，测试钉住该行为）。
