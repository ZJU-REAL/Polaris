# BYO Runner（自带执行机）

> Part of the P3 autonomous-experiments track (#674 · #685). Design source:
> [Polaris 2.0 design report §15](rfcs/2026-09-02-polaris-2.0-design-report.zh.md).

Polaris 把「在哪跑实验」交给用户：实验室 GPU 机、云主机、家里的工作站，
只要能连上就能当 runner。接入分两档：

| Tier | 接入方式 | 状态 |
| --- | --- | --- |
| 1 | SSH 可达（平台主动连出去） | **已实现**（本页上半部分） |
| 2 | 出站 WebSocket agent（机器主动连回来，适合 NAT/内网机） | **未实现**，协议草案见下 |

## Tier 1：SSH 直连（已实现）

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

## Tier 2：出站 WebSocket agent（未实现，协议草案）

> Not implemented. 本节是设计笔记，落地前以此为讨论基线；实现时另立 RFC。

场景：内网/NAT 后的机器，平台连不进去。方案学 GitHub self-hosted runner：
机器上跑一个常驻 agent，**出站**长连接拉任务，永不要求开入站端口；
复杂 NAT 场景文档化 Tailscale，不自研穿透。

### 注册（短时效 token）

```
POST /api/resources/runner-hosts/registration-token   （用户，Web 端）
  → {"token": "prt_…", "expires_in": 3600}            （一次性、短命）

$ polaris-runner register --url https://polaris.example --token prt_…
```

agent 用 token 换取长期凭据（仅限该机器身份），token 立即作废。注册成功 =
服务端自动创建 `host` 类 Resource（与 tier-1 同一张表，租约语义共用），
`config.transport = "websocket"`。

### 任务拉取（出站 WS 长连接）

```
agent → wss://polaris.example/api/runner-agent/ws     （Authorization: 机器凭据）
  ← {"kind": "hello", "agent_version": "…"}            agent 自述版本/硬件（probe 结果）
  → {"kind": "upgrade", "payload_url": …}              版本过期时先升级（同 tier-1 钉定语义）
  → {"kind": "task", "run_id": …, "spec": {…}}         派发：流程包 + 物料 + container spec
  ← {"kind": "ack", "run_id": …}
```

- 派发以 run 为单位（voyage run 整体交给 runner，携带组件清单）；
- 无任务时心跳保活；断线重连后按 run_id 对账续传（桌面/服务端离线不炸 run）。

### 事件回传

```
  ← {"kind": "event", "run_id": …, "seq": N, "event": {…}}   日志/指标/状态，seq 单调
  ← {"kind": "artifact", "run_id": …, "name": …}             产物分块上传
  → {"kind": "ack_event", "run_id": …, "seq": N}             服务端确认位点
```

seq + ack 位点保证断线重连后从确认点续传，不丢不重。

### Ephemeral 承诺

tier-2 的任务**一律**容器内执行（tier-1 的裸机路径不下放）：agent 收到任务后
`docker run` 一次性容器，结束即销毁，工作区产物先回传再删除。机器凭据可随时
吊销（同 tier-1 语义），吊销后 WS 连接被服务端主动断开、Resource 标记不可用。
