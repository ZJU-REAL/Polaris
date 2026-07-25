"""事务邮件发送（目前只有密码重置）。

刻意用标准库 smtplib 而不是 aiosmtplib：发信都是人触发的低频动作，丢进线程池
调一次就够，省掉一个依赖和一次镜像重建。发信失败只记日志、不向调用方抛错——
忘记密码接口必须对「邮箱是否存在、信是否发出去」保持沉默，否则它就成了账号
枚举接口。

SMTP 连接方式由 POLARIS_SMTP_SECURITY 决定：
  ssl       建连即 TLS（465 / 浙大 994）
  starttls  明文建连后 STARTTLS 升级（587 / 25）
  none      不加密，仅限内网调试
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _build_message(to: str, subject: str, text: str, html: str) -> EmailMessage:
    settings = get_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.smtp_from_name, settings.email_from_address))
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _send_sync(msg: EmailMessage) -> None:
    """阻塞发信；调用方负责丢线程池。"""
    settings = get_settings()
    context = ssl.create_default_context()
    host, port, timeout = settings.smtp_host, settings.smtp_port, settings.smtp_timeout

    if settings.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    with server:
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


async def send_mail(to: str, subject: str, text: str, html: str) -> bool:
    """发一封信，返回是否成功。异常一律吞掉并记日志（见模块 docstring）。"""
    settings = get_settings()
    if not settings.email_enabled:
        logger.warning("未配置 SMTP（POLARIS_SMTP_HOST 为空），跳过发信：%s", subject)
        return False
    try:
        await asyncio.to_thread(_send_sync, _build_message(to, subject, text, html))
    except Exception:  # noqa: BLE001 —— 发信失败不能冒泡成 500
        logger.exception("发信失败：to=%s subject=%s", to, subject)
        return False
    logger.info("已发信：to=%s subject=%s", to, subject)
    return True


_PURPOSE_COPY = {
    "register": ("注册 Polaris 账号", "你正在注册 Polaris 账号"),
    "reset": ("重置 Polaris 密码", "我们收到了重置你 Polaris 账号密码的请求"),
}


async def send_verification_code(to: str, purpose: str, code: str, minutes: int) -> bool:
    """验证码邮件：注册验证与找回密码共用一套版式，文案按 purpose 切换。"""
    subject_tail, lead = _PURPOSE_COPY.get(purpose, _PURPOSE_COPY["register"])

    text = (
        f"{lead}，验证码如下（{minutes} 分钟内有效）：\n\n"
        f"    {code}\n\n"
        "请勿把验证码转发给任何人。如果这不是你本人的操作，忽略这封邮件即可。\n\n"
        "—— Polaris 北极星 AI 科研智能体\n"
    )
    html = f"""\
<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
            font-size:14px;line-height:1.7;color:#1c2430;max-width:520px">
  <p>{lead}，请在页面中填入下面的验证码：</p>
  <p style="margin:24px 0">
    <span style="display:inline-block;background:#f2f6fc;border:1px solid #d7e3f4;
                 border-radius:10px;padding:14px 26px;font-size:28px;font-weight:700;
                 letter-spacing:10px;color:#126DFF">{code}</span>
  </p>
  <p style="color:#6b7785;font-size:12.5px">
    验证码 <b>{minutes} 分钟</b>内有效，请勿转发给任何人。
  </p>
  <p style="color:#6b7785;font-size:12.5px">如果这不是你本人的操作，忽略这封邮件即可。</p>
  <p style="color:#94a3b3;font-size:12px;margin-top:28px">—— Polaris 北极星 AI 科研智能体</p>
</div>
"""
    return await send_mail(to, f"{code} —— {subject_tail}验证码", text, html)
