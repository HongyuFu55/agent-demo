"""处理聊天交互的 Chatbot API 接口.

本模块提供普通聊天、流式聊天、消息历史查询和聊天历史清理接口。
"""

import re
from datetime import datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse

from app.api.v1.auth import get_current_user
from app.core.config import settings
from app.core.langgraph.graph import LangGraphAgent
from app.core.langgraph.tools.file_ops import WORKSPACE_DIR
from app.core.limiter import limiter
from app.core.logging import logger
from app.core.metrics import llm_stream_duration_seconds
from app.models.session import Session
from app.models.user import User
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Message,
)
from app.schemas.sse import (
    SSEEvent,
    SSEEventType,
)
from app.services.chat_history import read_history
from app.services.database import database_service
from app.services.events import (
    EventDraft,
    EventType,
    event_store,
    record_turn,
)
from app.services.session_naming import maybe_name_session

router = APIRouter()
agent = LangGraphAgent()
MAX_LOG_CONTENT_LENGTH = 500


async def get_owned_session(session_id: str, user: User) -> Session:
    """加载聊天会话，并校验它属于当前登录用户."""
    session = await database_service.get_user_session(session_id, user.id)
    if session is None:
        logger.warning("会话访问被拒绝", session_id=session_id, user_id=user.id)
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def get_latest_user_message(messages: list[Message]) -> str:
    """从聊天请求中返回最新的用户消息内容."""
    user_message = next((message.content for message in reversed(messages) if message.role == "user"), None)
    if user_message is None:
        raise HTTPException(status_code=422, detail="At least one user message is required")
    return user_message


def get_latest_assistant_message(messages: list[Message]) -> str:
    """从处理结果中返回最新的助手回复内容."""
    assistant_message = next((message.content for message in reversed(messages) if message.role == "assistant"), None)
    if assistant_message is None:
        raise HTTPException(status_code=500, detail="Assistant response was not generated")
    return assistant_message


def truncate_for_log(value: str, max_length: int = MAX_LOG_CONTENT_LENGTH) -> str:
    """截断日志中的长文本，避免请求内容把日志刷得过长."""
    if len(value) <= max_length:
        return value
    return f"{value[:max_length]}...<truncated:{len(value) - max_length}>"


def build_chat_request_log_payload(chat_request: ChatRequest) -> dict:
    """构建可安全写入日志的聊天请求入参."""
    return {
        "session_id": chat_request.session_id,
        "message_count": len(chat_request.messages),
        "messages": [
            {
                "role": message.role,
                "content": truncate_for_log(message.content),
                "content_length": len(message.content),
            }
            for message in chat_request.messages
        ],
    }


@router.post("/chat", response_model=ChatResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["chat"][0])  # limiter.limit就是装饰器，添加限流限制
async def chat(
    request: Request,
    chat_request: ChatRequest,
    user: User = Depends(get_current_user),
):
    """使用 LangGraph 处理聊天请求.

    参数：
        request: FastAPI 请求对象，用于限流。
        chat_request: 包含消息的聊天请求。
        user: 从登录 token 解析出的当前用户。

    返回：
        ChatResponse: 处理后的聊天响应。

    抛出：
        HTTPException: 处理请求出错时抛出。
    """
    try:
        session = await get_owned_session(chat_request.session_id, user)
        logger.info("收到聊天请求", session_id=session.id, user_id=user.id, message_count=len(chat_request.messages))
        logger.info("收到聊天请求入参", session_id=session.id, user_id=user.id, parameters=build_chat_request_log_payload(chat_request))

        if settings.SESSION_NAMING_ENABLED:
            maybe_name_session(session.id, session.name, chat_request.messages)

        user_message = get_latest_user_message(chat_request.messages)

        # 一轮对话 = 一组事件：turn/start → message/user → message/assistant → turn/end
        async with record_turn(session_id=session.id, user_message=user_message, source="chat") as turn:
            result = await agent.get_response(
                chat_request.messages, session.id, user_id=str(session.user_id), username=session.username
            )
            answer = get_latest_assistant_message(result)
            event_ok = await turn.assistant(answer, model=agent.llm_service.get_llm().get_name())

            # 影子写入：事件日志与 chat_message 双写，用 replay_session.py --verify 比对一致性
            chat_msg = await database_service.create_chat_message(
                session_id=session.id,
                user_id=user.id,
                question=user_message,
                answer=answer,
                metadata={"source": "chat"},
            )
            if event_ok and not chat_msg:
                await turn.error(kind="dual_write_db_failure", message="事件写入成功但数据库消息表写入失败")
                logger.error("dual_write_inconsistency", session_id=session.id, event_ok=event_ok, db_ok=False)

        logger.info("聊天请求处理完成", session_id=session.id, user_id=user.id)

        return ChatResponse(messages=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("chat_request_failed", session_id=chat_request.session_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["chat_stream"][0])
async def chat_stream(
    request: Request,
    chat_request: ChatRequest,
    user: User = Depends(get_current_user),
):
    """使用 LangGraph 处理流式聊天请求.

    参数：
        request: FastAPI 请求对象，用于限流。
        chat_request: 包含消息的聊天请求。
        user: 从登录 token 解析出的当前用户。

    返回：
        StreamingResponse: 聊天补全的流式响应。

    抛出：
        HTTPException: 处理请求出错时抛出。
    """
    try:
        session = await get_owned_session(chat_request.session_id, user)
        logger.info("收到流式聊天请求", session_id=session.id, user_id=user.id, message_count=len(chat_request.messages))
        logger.info("收到流式聊天请求入参", session_id=session.id, user_id=user.id, parameters=build_chat_request_log_payload(chat_request))

        if settings.SESSION_NAMING_ENABLED:
            maybe_name_session(session.id, session.name, chat_request.messages)

        async def event_generator():
            """生成符合 W3C 规范的标准 Typed SSE 事件流（四步提交架构）."""
            tokens: list[str] = []
            model_name = agent.llm_service.get_llm().get_name()
            user_message = get_latest_user_message(chat_request.messages)

            # 事件日志包住整轮：无论正常结束、异常还是断流，都会尝试写 turn/end
            async with record_turn(
                session_id=session.id, user_message=user_message, source="chat_stream"
            ) as turn:
                try:
                    with llm_stream_duration_seconds.labels(model=model_name).time():
                        async for event in agent.get_stream_response(
                            chat_request.messages, session.id, user_id=str(session.user_id), username=session.username
                        ):
                            if event.event == SSEEventType.TOKEN:
                                tokens.append(event.content)
                                yield event.to_sse_packet()
                            elif event.event == SSEEventType.TOOL_START:
                                await turn.tool_started(
                                    tool_call_id=event.tool_call_id,
                                    name=event.tool_name or "unknown",
                                    args=event.tool_args,
                                )
                                yield event.to_sse_packet()
                            elif event.event == SSEEventType.TOOL_END:
                                await turn.tool_finished(
                                    tool_call_id=event.tool_call_id,
                                    name=event.tool_name,
                                    duration_ms=event.duration_ms,
                                    result_preview=event.tool_result,
                                )
                                yield event.to_sse_packet()
                            elif event.event == SSEEventType.INTERRUPT:
                                turn.interrupted("human_approval_required")
                                yield event.to_sse_packet()
                            elif event.event == SSEEventType.DONE:
                                # 截留内部完成信号：终结帧必须在落盘后由本函数统一发射
                                pass
                            else:
                                yield event.to_sse_packet()

                    answer = "".join(tokens)

                    # ① 步骤 1：写入事件日志 (MESSAGE_ASSISTANT)
                    event_ok = await turn.assistant(answer, model=model_name)

                    # ② 步骤 2：持久化消息数据库 (chat_message)
                    chat_msg = await database_service.create_chat_message(
                        session_id=session.id,
                        user_id=user.id,
                        question=user_message,
                        answer=answer,
                        metadata={"source": "chat_stream"},
                    )
                    db_ok = chat_msg is not None

                    # 若出现双写不一致，执行 M4 差别化告警
                    if event_ok and not db_ok:
                        await turn.error(kind="dual_write_db_failure", message="事件写入成功但数据库消息表写入失败")
                        logger.error("dual_write_inconsistency", session_id=session.id, event_ok=event_ok, db_ok=db_ok)

                    # ③ 步骤 3：关轮 (TURN_END 必须早于 DONE，保证历史读取平衡性)
                    turn_ok = await turn.finish()

                    # ④ 步骤 4：宣布最终完成（带 committed 与 message_id）
                    is_committed = bool(event_ok and db_ok and turn_ok)
                    done_event = SSEEvent(
                        event=SSEEventType.DONE,
                        type=SSEEventType.DONE.value,
                        content="",
                        done=True,
                        metadata={
                            "message_id": chat_msg.id if chat_msg else None,
                            "committed": is_committed,
                            "turn_id": turn.turn_id,
                        },
                    )
                    yield done_event.to_sse_packet()

                    logger.info("流式聊天四步提交完成", session_id=session.id, user_id=user.id, committed=is_committed)

                except Exception as e:
                    await turn.error(kind="stream_failure", message=str(e))
                    logger.exception(
                        "stream_chat_request_failed",
                        session_id=session.id,
                        error=str(e),
                    )
                    error_event = SSEEvent(
                        event=SSEEventType.ERROR,
                        type=SSEEventType.ERROR.value,
                        content=f"❌ 流式响应异常: {str(e)}",
                        done=True,
                    )
                    yield error_event.to_sse_packet()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "stream_chat_request_failed",
            session_id=chat_request.session_id,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages", response_model=ChatResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["messages"][0])
async def get_session_messages(
    request: Request,
    response: Response,
    session_id: str = Query(..., description="聊天会话 ID"),
    user: User = Depends(get_current_user),
):
    """获取指定会话的所有消息.

    自 M2 起，历史数据默认从事件日志的 messages 投影读出
    （`SESSION_EVENT_READ_SOURCE=chat_message` 可回退到旧路径）。
    响应头 `X-History-Source` 会标明本次数据来自投影还是兜底的消息表。

    参数：
        request: FastAPI 请求对象，用于限流。
        response: FastAPI 响应对象，用于写入来源标记。
        session_id: 要读取的聊天会话 ID。
        user: 从登录 token 解析出的当前用户。

    返回：
        ChatResponse: 会话中的全部消息。

    抛出：
        HTTPException: 获取消息出错时抛出。
    """
    try:
        session = await get_owned_session(session_id, user)
        history = await read_history(session.id, user.id)
        response.headers["X-History-Source"] = history.source.value
        logger.info(
            "chat_history_read",
            session_id=session.id,
            user_id=user.id,
            source=history.source.value,
            message_count=len(history.messages),
            event_count=history.event_count,
        )
        return ChatResponse(messages=history.messages)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_messages_failed", session_id=session_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/messages")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["messages"][0])
async def clear_chat_history(
    request: Request,
    session_id: str = Query(..., description="聊天会话 ID"),
    user: User = Depends(get_current_user),
):
    """清空指定会话的所有消息.

    参数：
        request: FastAPI 请求对象，用于限流。
        session_id: 要清理的聊天会话 ID。
        user: 从登录 token 解析出的当前用户。

    返回：
        dict: 表示聊天历史已清空的消息。
    """
    try:
        session = await get_owned_session(session_id, user)
        await database_service.delete_chat_messages(session.id, user.id)
        # 事件日志不删除历史，只追加一条"清空"事实——投影据此把消息列表归零，
        # 但账本里仍然留着"曾经有过这些消息"的痕迹，便于审计与回溯
        await event_store.append(session.id, [EventDraft(EventType.SESSION_CLEARED, {})])
        await agent.clear_chat_history(session.id)
        return {"message": "Chat history cleared successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("clear_chat_history_failed", session_id=session_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# 允许上传的文件扩展名白名单
ALLOWED_UPLOAD_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".xlsx",
    ".xls",
    ".csv",
    ".txt",
    ".md",
    ".json",
    ".pdf",
}
MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB


@router.post("/upload")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["upload"][0])
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(..., description="所属会话 ID"),
    user: User = Depends(get_current_user),
):
    """用户会话隔离的文件与图片上传接口.

    安全特性：
    1. 校验会话所属权，防止越权写入其他用户的会话；
    2. 严格文件名清洗与防路径穿越（过滤 ../、绝对路径）；
    3. 隔离存储于 storage/workspace/users/{user_id}/{session_id}/；
    4. 扩展名白名单与文件大小限制 (15MB)。
    """
    try:
        # 1. 验证会话归属权
        session = await get_owned_session(session_id, user)

        # 2. 验证原始文件名并提取扩展名
        raw_filename = file.filename or ""
        if not raw_filename.strip():
            raise HTTPException(status_code=400, detail="Uploaded file must have a valid filename")

        # 防路径穿越：提取纯文件名，去除任何目录前缀
        base_filename = Path(raw_filename).name
        if ".." in base_filename or "/" in base_filename or "\\" in base_filename:
            logger.warning(
                "upload_path_traversal_attempt_blocked",
                raw_filename=raw_filename,
                user_id=user.id,
                session_id=session.id,
            )
            raise HTTPException(status_code=400, detail="Malicious path traversal sequence detected in filename")

        # 扩展名白名单校验
        file_ext = Path(base_filename).suffix.lower()
        if file_ext not in ALLOWED_UPLOAD_EXTENSIONS:
            logger.warning("upload_unsupported_file_extension", ext=file_ext, user_id=user.id)
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file extension '{file_ext}'. Allowed: {sorted(list(ALLOWED_UPLOAD_EXTENSIONS))}",
            )

        # 3. 构建用户与会话隔离沙箱目录
        # 目录规则：storage/workspace/users/{user_id}/{session_id}/
        user_session_dir = (WORKSPACE_DIR / "users" / str(user.id) / str(session.id)).resolve()
        user_session_dir.mkdir(parents=True, exist_ok=True)

        # 清洗文件名并加入时间戳前缀，防止同名覆盖
        clean_name = re.sub(r"[^\w\.\-\u4e00-\u9fa5]", "_", Path(base_filename).stem)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_saved_filename = f"{timestamp}_{clean_name}{file_ext}"

        target_file_path = (user_session_dir / safe_saved_filename).resolve()

        # 防路径穿越二次强断言：目标物理路径必须位于当前用户会话沙箱目录之内
        try:
            target_file_path.relative_to(user_session_dir)
        except ValueError:
            logger.error("upload_sandbox_escape_detected", target_path=str(target_file_path))
            raise HTTPException(status_code=400, detail="Invalid destination path outside session sandbox")

        # 4. 读取内容并做大小限制校验，安全落盘
        content = await file.read()
        file_size = len(content)
        if file_size > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({file_size} bytes). Maximum allowed is {MAX_FILE_SIZE_BYTES} bytes (15MB).",
            )

        with open(target_file_path, "wb") as f:
            f.write(content)

        # 5. 生成相对于工作区的安全相对路径
        relative_path_for_agent = f"users/{user.id}/{session.id}/{safe_saved_filename}"

        # 判定文件大类
        is_image = file_ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        is_table = file_ext in {".xlsx", ".xls", ".csv"}
        file_category = "image" if is_image else ("table" if is_table else "document")

        logger.info(
            "file_uploaded_successfully",
            user_id=user.id,
            session_id=session.id,
            file_name=base_filename,
            saved_path=relative_path_for_agent,
            size_bytes=file_size,
        )

        return {
            "success": True,
            "file_name": base_filename,
            "saved_name": safe_saved_filename,
            "relative_path": relative_path_for_agent,
            "file_size": file_size,
            "file_category": file_category,
            "content_type": file.content_type or "application/octet-stream",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("file_upload_failed", user_id=user.id, session_id=session_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to process file upload: {str(e)}")

