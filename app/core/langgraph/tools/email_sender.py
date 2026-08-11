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
    """构建极具现代科技美感与 Anime.js 极客风格的 HTML 邮件模板."""
    paragraphs = []
    for raw_line in content.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- ") or line.startswith("* "):
            paragraphs.append(f"<li style='margin: 0.4rem 0; color: #cbd5e1; list-style-type: none; position: relative; padding-left: 1.2rem;'><span style='position: absolute; left: 0; color: #06b6d4; font-weight: bold;'>◈</span> {line[2:]}</li>")
        elif line.startswith("#"):
            title_text = line.lstrip("# ").strip()
            paragraphs.append(f"<h3 style='margin: 1.2rem 0 0.5rem 0; color: #38bdf8; font-size: 16px; border-bottom: 1px solid rgba(56, 189, 248, 0.2); padding-bottom: 4px;'>{title_text}</h3>")
        elif line.startswith(">"):
            paragraphs.append(f"<blockquote style='margin: 0.8rem 0; padding: 0.6rem 1rem; background: rgba(30, 41, 59, 0.6); border-left: 3px solid #8b5cf6; border-radius: 4px; color: #94a3b8; font-style: italic;'>{line.lstrip('> ')}</blockquote>")
        else:
            paragraphs.append(f"<p style='margin: 0.6rem 0; line-height: 1.7; color: #e2e8f0;'>{line}</p>")

    body_html = "".join(paragraphs)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
          background-color: #060913;
          color: #e2e8f0;
          margin: 0;
          padding: 24px 12px;
        }}
        .email-wrapper {{
          max-width: 620px;
          margin: 0 auto;
          background: linear-gradient(180deg, #0e1526 0%, #080d1a 100%);
          border: 1px solid rgba(59, 130, 246, 0.25);
          border-radius: 16px;
          overflow: hidden;
          box-shadow: 0 20px 50px rgba(0,0,0,0.8), 0 0 30px rgba(6, 182, 212, 0.12);
        }}
        .email-header {{
          background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 60%, #042f2e 100%);
          padding: 28px 24px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
          position: relative;
        }}
        .badge {{
          display: inline-flex;
          align-items: center;
          background: rgba(6, 182, 212, 0.15);
          color: #22d3ee;
          border: 1px solid rgba(6, 182, 212, 0.35);
          padding: 4px 12px;
          border-radius: 20px;
          font-size: 11px;
          font-weight: 600;
          letter-spacing: 0.8px;
          margin-bottom: 14px;
          box-shadow: 0 0 10px rgba(6, 182, 212, 0.2);
        }}
        .email-title {{
          margin: 0;
          font-size: 21px;
          font-weight: 700;
          color: #ffffff;
          line-height: 1.4;
          letter-spacing: 0.3px;
        }}
        .email-body {{
          padding: 28px 24px;
          font-size: 14.5px;
        }}
        .email-footer {{
          background: #050811;
          padding: 20px 24px;
          font-size: 12px;
          color: #64748b;
          border-top: 1px solid rgba(255, 255, 255, 0.05);
          text-align: center;
          line-height: 1.6;
        }}
      </style>
    </head>
    <body>
      <div class="email-wrapper">
        <div class="email-header">
          <div class="badge">⚡ FASTAPI LANGGRAPH AGENT NOTIFICATION</div>
          <h2 class="email-title">{subject}</h2>
        </div>
        <div class="email-body">
          {body_html}
        </div>
        <div class="email-footer">
          <div>🚀 本邮件由 <strong>FastAPI LangGraph AI 智能助手</strong> 自动生成并发送</div>
          <div style="margin-top: 4px; font-size: 11px; color: #475569;">© 2026 FastAPI LangGraph Agent Project • Powered by Anime.js Aesthetics</div>
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

    # 如果未配置授权码，触发安全防呆降级模式
    if not smtp_user or not smtp_password:
        logger.warning(
            "email_smtp_credentials_missing",
            user_configured=bool(smtp_user),
            password_configured=bool(smtp_password),
        )
        return (
            f"❌ 邮件发送失败：邮件发送服务尚未配置发件人或授权信息，无法真实发送邮件。"
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
        return f"✅ 邮件已成功发送至 {', '.join(raw_emails)}！主题：{subject}"

    except smtplib.SMTPAuthenticationError as e:
        logger.exception("smtp_auth_error", error=str(e))
        return "❌ 邮件发送失败：邮箱身份认证失败，请检查发件人授权码是否有效。"
    except Exception as e:
        logger.exception("smtp_send_error", error=str(e))
        return f"❌ 邮件发送失败，原因: {str(e)}"
