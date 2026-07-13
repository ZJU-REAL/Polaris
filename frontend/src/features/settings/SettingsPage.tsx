import { UnderConstruction } from '../shared/UnderConstruction';

export function SettingsPage() {
  return (
    <UnderConstruction
      eyebrow="Polaris · Settings"
      icon="settings"
      title="设置"
      titleEn="Settings"
      sub="研究方向、模型路由、算力主机与账号管理"
      subEn="Directions, model routing, compute hosts and account management"
      milestone="M2"
      features={[
        { zh: '研究方向 registry', desc: '方向的 categories/keywords/relevance 阈值/目标会议配置，启停与默认方向切换。' },
        { zh: 'LLM 模型路由表', desc: '按任务类型（生成/评审/摘要）配置模型与参数，业务代码只经 LLM 抽象层调用。' },
        { zh: 'SSH 主机与密钥', desc: 'GPU 节点连接配置；私钥 Fernet 加密入库，不落日志。' },
        { zh: '用户与邀请码', desc: '邀请码注册管理、成员角色与审批权限。' },
        { zh: '通知偏好', desc: '闸门/实验完成的 WebSocket 通知与提醒策略。' },
      ]}
    />
  );
}
