"""基于网易 163 邮箱 (SMTP SSL) 的邮件通知工具.

支持纯异步非阻塞执行、HTML 富文本/纯文本自适应渲染、Tenacity 指数退避重试与未配置授权码的安全防呆降级。
"""

import asyncio
import re
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Annotated

from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import logger


def _validate_email(email_str: str) -> bool:
    """简单校验邮箱格式."""
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return bool(re.match(pattern, email_str.strip()))


def _build_html_content(subject: str, content: str) -> str:
    """构建极具现代科技美感的 HTML 邮件模板."""
    # 将换行符转为 HTML <br>
    html_paragraphs = "".join([f"<p style='margin: 0.6rem 0; line-height: 1.6;'>{line}</p>" for line in content.split("\n") if line.strip()])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0f19; color: #e2e8f0; margin: 0; padding: 20px; }}
        .email-container {{ max-width: 600px; margin: 0 auto; background: #131b2e; border: 1px solid #23324d; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}
        .email-header {{ background: linear-gradient(135deg, #3b82f6 0%, #06b6d4 100%); padding: 24px; color: #ffffff; }}
        .email-header h2 {{ margin: 0; font-size: 20px; font-weight: 600; letter-spacing: 0.5px; }}
        .email-body {{ padding: 24px; font-size: 15px; color: #cbd5e1; }}
        .email-footer {{ background: #0c1220; padding: 16px 24px; font-size: 12px; color: #64748b; border-top: 1px solid #1e293b; text-align: center; }}
        .badge {{ display: inline-block; background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4); padding: 4px 10px; border-radius: 6px; font-size: 12px; margin-bottom: 12px; }}
      </style>
    </head>
    <body>
      <div class="email-container">
        <div class="email-header">
          <div class="badge">🤖 FastAPI LangGraph AI Assistant</div>
          <h2>{subject}</h2>
        </div>
        <div class="email-body">
          {html_paragraphs}
        </div>
        <div class="email-footer">
          此邮件由 FastAPI LangGraph AI Agent 智能助手自动发送，请勿直接回复。
        </div>
      </div>
    </body>
    </html>
    """


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _send_sync_smtp(
    smtp_server: str,
    smtp_port: int,
    use_ssl: bool,
    user: str,
    password: str,
    from_name: str,
    to_emails: list[str],
    subject: str,
    content: str,
) -> None:
    """同步 SMTP 发送函数，封装于 Tenacity 重试中."""
    # 构建复合邮件格式 (纯文本 + HTML)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8").encode()
    msg["From"] = f"{Header(from_name, 'utf-8').encode()} <{user}>"
    msg["To"] = ", ".join(to_emails)

    # 纯文本正文
    part_text = MIMEText(content, "plain", "utf-8")
    msg.attach(part_text)

    # HTML 富文本正文
    html_body = _build_html_content(subject, content)
    part_html = MIMEText(html_body, "html", "utf-8")
    msg.attach(part_html)

    logger.info("smtp_connection_initiating", server=smtp_server, port=smtp_port, to_count=len(to_emails))

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15) as server:
            server.login(user, password)
            server.sendmail(user, to_emails, msg.as_string())
    else:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, to_emails, msg.as_string())


@tool
async def send_email_notification(
    to_email: Annotated[str, "收件人邮箱地址。如果是多个收件人，可以用英文逗号分隔，例如 'user1@163.com, user2@qq.com'"],
    subject: Annotated[str, "邮件主题/标题，应简明扼要概括邮件核心内容"],
    content: Annotated[str, "邮件正文内容，支持详细的文本描述、汇报总结、待办列表或结构化信息"],
) -> str:
    """通过 163 网易邮箱 SMTP 服务向指定收件人发送电子邮件通知。

    当需要给用户发送重要通知、分析报告、会议纪要、告警提醒或将对话内容以邮件形式存档发送时，调用此工具。
    """
    logger.info("send_email_tool_called", to_email=to_email, subject=subject)

    # 1. 解析与校验收件人列表
    raw_emails = [e.strip() for e in to_email.split(",") if e.strip()]
    if not raw_emails:
        return "❌ 发送失败：未提供有效的收件人邮箱地址。"

    invalid_emails = [e for e in raw_emails if not _validate_email(e)]
    if invalid_emails:
        return f"❌ 发送失败：以下邮箱格式不合法: {', '.join(invalid_emails)}"

    # 2. 检查 163 邮箱配置
    smtp_user = settings.SMTP_163_USER
    smtp_password = settings.SMTP_163_PASSWORD

    # 如果未配置授权码，触发安全防呆降级模式，不让 Agent 崩溃
    if not smtp_user or not smtp_password:
        logger.warning(
            "email_smtp_credentials_missing",
            user_configured=bool(smtp_user),
            password_configured=bool(smtp_password),
        )
        return (
            f"⚠️ [模拟发送模式 - 163邮箱未配置授权码]\n"
            f"系统尚未在 .env.development 中配置 'SMTP_163_USER' 或 'SMTP_163_PASSWORD' (客户端授权码)。\n\n"
            f"📧 待发邮件预览如下：\n"
            f"- 收件人: {', '.join(raw_emails)}\n"
            f"- 主题: {subject}\n"
            f"- 发件人: {settings.SMTP_163_DEFAULT_FROM_NAME} <未配置>\n"
            f"- 内容概览: {content[:200]}...\n\n"
            f"💡 提示：如需真实发出邮件，请在 .env.development 中填入 163 邮箱账号及 16 位客户端授权码。"
        )

    # 3. 异步非阻塞执行真实邮件发送
    try:
        await asyncio.to_thread(
            _send_sync_smtp,
            smtp_server=settings.SMTP_163_SERVER,
            smtp_port=settings.SMTP_163_PORT,
            use_ssl=settings.SMTP_163_USE_SSL,
            user=smtp_user,
            password=smtp_password,
            from_name=settings.SMTP_163_DEFAULT_FROM_NAME,
            to_emails=raw_emails,
            subject=subject,
            content=content,
        )

        logger.info("email_sent_successfully", to_emails=raw_emails, subject=subject)
        return f"✅ 邮件已成功发送至 {', '.join(raw_emails)}！\n主题：{subject}"

    except smtplib.SMTPAuthenticationError as e:
        logger.exception("smtp_auth_error", error=str(e))
        return f"❌ 邮件发送失败：163 邮箱身份认证失败 (535)。请检查 163 邮箱授权码是否正确有效。"
    except Exception as e:
        logger.exception("smtp_send_error", error=str(e))
        return f"❌ 邮件发送失败，原因: {str(e)}"
