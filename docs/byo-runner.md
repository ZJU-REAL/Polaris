# BYO Runner（自带执行机）

> Part of the P3 autonomous-experiments track (#674 · #685). Design source:
> [Polaris 2.0 design report §15](https://github.com/ZJU-REAL/Polaris/blob/main/docs/rfcs/2026-09-02-polaris-2.0-design-report.zh.md).

Polaris 把「在哪跑实验」交给用户：实验室 GPU 机、云主机、家里的工作站，
只要能连上就能当 runner。接入分两档：

| Tier | 接入方式 | 状态 |
| --- | --- | --- |
| 1 | SSH 可达（平台主动连出去） | **已实现**（本页上半部分） |
| 2 | 出站 WebSocket agent（机器主动连回来，适合 NAT/内网机） | **传输层已实现**（#695，本页下半部分）；实验执行接线归 runner v2 后续 |

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

## Tier 2：出站 WebSocket agent（传输层已实现，#695）

场景：内网/NAT 后的机器，平台连不进去。方案学 GitHub self-hosted runner：
机器上跑一个常驻 agent（参考实现 `integrations/runner-agent/`），**出站**
长连接拉任务，永不要求开入站端口；复杂 NAT 场景文档化 Tailscale，不自研穿透。

当前范围：**传输层可用**——注册、长连接、心跳在线判定、任务派发座
（`dispatch_task`）、事件回传。实验执行链（runner v2 在此底座上的接线：
run 级派发、组件清单、产物分块上传、agent 自动升级、seq/ack 断点续传）
归后续 PR。服务端实现：`app/services/runner_ws.py`。

### 注册（短时效一次性 token）

```
POST /api/resources/runner-hosts/registration-tokens    （登录用户，Web 端）
  → {"token": "prt_…", "expires_in": 3600}              （一次性、1 小时时效）

$ python agent.py register --server https://polaris.example --token prt_…
  （agent 内部调 POST /api/resources/runner-hosts/register，
   携 token + name + machine 自述信息）
```

- token 是随机串 + Redis TTL，GETDEL 原子消费：**用后即焚**，重放/过期一律
  401（不区分原因）；
- 注册成功 = 自动创建 `host` 类 Resource（与 tier-1 同一张表，租约语义共用），
  `config.transport = "websocket"`、`config.machine` 存 agent 自述信息；
- 机器凭据落 `connection_credentials`（kind=`ws`）：**只存 agent secret 的
  sha256 摘要**（Fernet 加密后入 payload），明文 secret 仅注册响应返回一次，
  服务端无法找回；agent 存本地 `~/.polaris-runner/agent.json`（0600）。

### 任务拉取（出站 WS 长连接）

```
agent → wss://polaris.example/ws/runner-agents/connect
  → {"type": "auth", "resource_id": …, "secret": "pra_…"}   首帧鉴权
  ← {"type": "ready", "resource_id": …}                     鉴权通过
  ← {"type": "task", "task_id": …, "payload": {…}}          派发（payload 对传输层不透明）
  → {"type": "heartbeat"}                                   保活（agent 每 30s 一跳）
  → {"type": "event"|"result", "task_id": …, "data": {…}}   回传
```

- 鉴权走**首消息**而非 Authorization header/query：长期 secret 不进 URL 与
  各级访问日志；失败以 4401 关闭。WS 路径不挂 `/api` 前缀（nginx 按 `/ws`
  反代 Upgrade，与现有 WS 端点一致）；
- 在线判定：连接即在线（`config.agent_online`，连接边沿写库），90 秒收不到
  任何帧（含心跳）判离线并以 4408 关闭；
- 派发座 `dispatch_task(resource_id, payload) → task_id`：任务先进 Redis
  队列（**离线排队**，TTL 1 小时兜底），再 publish 唤醒在线连接——在线即推、
  离线排队、断线重连补投同一条路径；worker 进程也可经 Redis 向 API 进程的
  连接派发；
- 事件回传落专用 Redis 记录（`runner:task:{id}:events` 回放 list + 实时频道，
  同 paper-task 的「先回放后实时」模式但独立 key 空间），runner v2 接线时
  消费。seq/ack 断点续传（不丢不重的强保证）归接线 PR，当前档位：任务未投递
  即断线会留在队列里重连补投；投递后 agent 崩溃的重试语义由执行层定义。

### Ephemeral 承诺

tier-2 的任务**一律**容器内执行（tier-1 的裸机路径不下放）：注册即
`config.ephemeral = true`，**不提供关闭口**。agent 收到任务后 `docker run`
一次性容器，结束即销毁，工作区产物先回传再删除（执行部分随 runner v2 接线；
参考 agent 目前只执行 echo 型任务验证传输）。机器凭据可随时吊销（同 tier-1
语义，`DELETE /api/connection-credentials/{id}`）：吊销后新连接 4401 拒绝，
在跳的连接最迟一个心跳周期内被服务端以 4403 断开，Resource 标记不可用。
