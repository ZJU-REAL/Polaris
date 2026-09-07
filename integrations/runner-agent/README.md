# Polaris BYO runner agent（tier-2 参考实现）

跑在你自己机器上的常驻 agent：**出站** WebSocket 连回 Polaris 平台拉任务，
适合 NAT/内网后、平台连不进来的机器（实验室工作站、家里的 GPU 机）。
不需要公网 IP，不需要开任何入站端口。协议与服务端实现见
[docs/byo-runner.md](../../docs/byo-runner.md)。

## 安装

需要 Python 3.10+：

```bash
pip install -r requirements.txt   # 仅一个依赖：websockets
```

## 注册（一次性）

1. 在 Polaris Web 端（设置 → 实验资源）生成注册 token：
   `POST /api/resources/runner-hosts/registration-tokens`，得到 `prt_...`
   （1 小时内有效、只能用一次）。
2. 在机器上执行：

```bash
python agent.py register --server https://polaris.example --token prt_xxx
```

注册成功后长期机器凭据（agent secret）写入 `~/.polaris-runner/agent.json`
（权限 0600）。secret 服务端只存摘要、**仅此一次下发**；文件丢了就在平台上
吊销对应凭据、重新注册。

## 运行

```bash
python agent.py run
```

agent 保持出站长连接、每 30 秒心跳；断线自动重连（5 秒退避）。当前为参考
实现：只执行 echo 型任务（payload 原样回传），容器化实验执行由后续版本接入。
建议用 systemd/tmux 常驻。

## 吊销

在平台上删除该机器对应的连接凭据（kind=ws）即吊销：在跳的连接最迟一个
心跳周期内被服务端断开，之后无法再连。

## NAT 很深 / 出站也受限？用 Tailscale

绝大多数 NAT 下直接出站 `wss://` 就能用，无需任何穿透。若机器所在网络
连平台地址都不可达（如只通内网），推荐 [Tailscale](https://tailscale.com/)
组网而不是自研穿透：

1. 平台服务器与 runner 机器都加入同一个 tailnet（`tailscale up`）;
2. `--server` 填平台的 Tailscale 地址（如 `https://polaris-srv.tailnet-xxx.ts.net`
   或 `http://100.x.y.z:8000`）；
3. 其余流程完全相同——agent 仍然只是出站连接，Tailscale 负责打洞与加密。
