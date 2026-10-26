"""会话事件日志的查看 / 回放 / 校验 / 回填工具.

用法（在项目根目录执行）：

    # 1. 看事件字典：有哪些事件类型、哪些可以安全跳过
    python scripts/replay_session.py --dict

    # 2. 把历史 chat_message 回填成事件（幂等，已有事件的会话会跳过）
    python scripts/replay_session.py --backfill

    # 3. 校验：事件折叠出的消息 是否等于 chat_message 表里的消息
    python scripts/replay_session.py --verify --all

    # 4. 看某个会话的事件流水 + 投影结果
    python scripts/replay_session.py --session-id <uuid>

    # 5. 只看历史某一刻（丢掉未提交的尾巴）
    python scripts/replay_session.py --session-id <uuid> --balanced
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

# 将项目根目录添加到 pythonpath（与 scripts/run_memory_worker.py 保持一致的写法）
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from sqlmodel import (  # noqa: E402
    Session,
    col,
    select,
)

from app.models.chat_message import ChatMessage  # noqa: E402
from app.models.session import Session as ChatSession  # noqa: E402
from app.services.database import database_service  # noqa: E402
from app.services.events import (  # noqa: E402
    EVENT_SPECS,
    EventDraft,
    EventType,
    VerificationResult,
    event_store,
)

console = Console()
_PREVIEW_WIDTH = 90


# --------------------------------------------------------------------------- 数据访问


def load_sessions() -> list[ChatSession]:
    """读取全部会话（按创建时间排序）."""
    with Session(database_service.engine) as db:
        rows = db.exec(select(ChatSession).order_by(col(ChatSession.created_at))).all()
    return list(rows)


def load_session(session_id: str) -> Optional[ChatSession]:
    """按 ID 读取会话."""
    with Session(database_service.engine) as db:
        return db.get(ChatSession, session_id)


def load_chat_messages(session_id: str) -> list[ChatMessage]:
    """读取某个会话的历史问答（按写入顺序）."""
    with Session(database_service.engine) as db:
        rows = db.exec(
            select(ChatMessage)
            .where(col(ChatMessage.session_id) == session_id)
            .order_by(col(ChatMessage.created_at), col(ChatMessage.id))
        ).all()
    return list(rows)


# --------------------------------------------------------------------------- 子命令


def print_event_dictionary() -> None:
    """打印事件字典."""
    table = Table(title="会话事件字典 (v1)", show_lines=False)
    table.add_column("类型", style="cyan", no_wrap=True)
    table.add_column("版本", justify="right")
    table.add_column("可跳过", justify="center")
    table.add_column("含义")

    for spec in EVENT_SPECS.values():
        table.add_row(
            spec.type.value,
            str(spec.schema_version),
            "[green]yes[/green]" if spec.ignorable else "[red]no[/red]",
            spec.purpose,
        )

    console.print(table)
    console.print(
        "[dim]“可跳过”= 老版本代码回放时遇到不认识的该类型事件，可以安全忽略而不影响投影；"
        "标记为 no 的类型如果读不懂，回放必须报错。[/dim]"
    )


async def backfill_session(session: ChatSession) -> int:
    """把一个会话的历史问答回填成事件（幂等）.

    参数：
        session: 目标会话。

    返回：
        int: 本次写入的事件条数（0 表示跳过）。
    """
    if await event_store.last_seq(session.id) > 0:
        return 0

    rows = load_chat_messages(session.id)
    if not rows:
        return 0

    drafts: list[EventDraft] = [
        EventDraft(
            EventType.SESSION_CREATED,
            {
                "user_id": session.user_id,
                "name": session.name,
                "username": session.username,
                "source": "backfill",
            },
        )
    ]

    for row in rows:
        # 每条历史问答映射成一轮完整对话；turn_id 带上原始记录 id，可反查来源
        turn_id = f"backfill-{row.id}"
        drafts.extend(
            [
                EventDraft(
                    EventType.TURN_START,
                    {"source": "backfill", "legacy_chat_message_id": row.id},
                    turn_id=turn_id,
                ),
                EventDraft(EventType.MESSAGE_USER, {"content": row.question}, turn_id=turn_id),
                EventDraft(EventType.MESSAGE_ASSISTANT, {"content": row.answer}, turn_id=turn_id),
                EventDraft(EventType.TURN_END, {"status": "ok", "source": "backfill"}, turn_id=turn_id),
            ]
        )

    await event_store.append(session.id, drafts)
    return len(drafts)


async def cmd_backfill() -> None:
    """回填所有还没有事件的会话."""
    sessions = load_sessions()
    total_events = 0
    filled = 0

    table = Table(title="历史数据回填")
    table.add_column("会话", style="cyan")
    table.add_column("历史消息数", justify="right")
    table.add_column("写入事件数", justify="right")
    table.add_column("结果")

    for session in sessions:
        message_count = len(load_chat_messages(session.id))
        written = await backfill_session(session)
        total_events += written
        if written:
            filled += 1
        table.add_row(
            session.id[:8],
            str(message_count),
            str(written),
            "[green]已回填[/green]" if written else "[dim]跳过[/dim]",
        )

    console.print(table)
    console.print(f"[bold]完成：{filled} 个会话共写入 {total_events} 条事件[/bold]")


async def cmd_verify(sessions: list[ChatSession]) -> int:
    """校验投影与既有消息表是否一致，返回不一致的会话数."""
    table = Table(title="投影校验：事件折叠结果 vs chat_message 表")
    table.add_column("会话", style="cyan")
    table.add_column("名称", overflow="fold")
    table.add_column("投影消息数", justify="right")
    table.add_column("消息表条数", justify="right")
    table.add_column("结果")

    mismatches = 0
    for session in sessions:
        result: VerificationResult = await event_store.verify_messages(session.id, session.user_id)
        if not result.matches:
            mismatches += 1
        table.add_row(
            session.id[:8],
            (session.name or "")[:24],
            str(len(result.expected)),
            str(len(result.actual)),
            "[green]一致[/green]" if result.matches else "[red]不一致[/red]",
        )

        if not result.matches and result.first_mismatch is not None:
            index = result.first_mismatch
            expected = result.expected[index] if index < len(result.expected) else None
            actual = result.actual[index] if index < len(result.actual) else None
            console.print(
                Panel(
                    f"第 {index} 条开始不一致\n\n投影: {expected}\n\n消息表: {actual}",
                    title=f"{session.id[:8]} 差异明细",
                    border_style="red",
                )
            )

    console.print(table)
    if mismatches:
        console.print(f"[bold red]{mismatches} 个会话不一致[/bold red]")
    else:
        console.print("[bold green]全部一致[/bold green]")
    return mismatches


async def cmd_show(session: ChatSession, upto_seq: Optional[int], balanced: bool, as_json: bool) -> None:
    """打印单个会话的事件流水与投影."""
    records = await event_store.read(session.id, until_seq=upto_seq)
    projections = await event_store.replay(session.id, upto_seq=upto_seq, balanced=balanced)
    committed = await event_store.committed_tail(session.id)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "session_id": session.id,
                    "name": session.name,
                    "events": [
                        {
                            "seq": record.seq,
                            "type": record.type,
                            "turn_id": record.turn_id,
                            "data": record.data,
                        }
                        for record in records
                    ],
                    "projections": projections,
                    "committed_tail_seq": committed,
                },
                ensure_ascii=False,
            )
        )
        return

    console.print(
        Panel(
            f"会话 {session.id}\n名称: {session.name or '(未命名)'}    用户: {session.user_id}\n"
            f"事件数: {len(records)}    最后一条已提交轮次: seq={committed}",
            title="会话概览",
            border_style="cyan",
        )
    )

    events_table = Table(title="事件流水（账本）")
    events_table.add_column("seq", justify="right", style="dim")
    events_table.add_column("类型", style="cyan", no_wrap=True)
    events_table.add_column("轮次", style="dim", no_wrap=True)
    events_table.add_column("载荷")

    for record in records:
        preview = json.dumps(record.data, ensure_ascii=False)
        if len(preview) > _PREVIEW_WIDTH:
            preview = f"{preview[:_PREVIEW_WIDTH]}…"
        events_table.add_row(
            str(record.seq),
            record.type,
            (record.turn_id or "-")[:10],
            preview,
        )

    console.print(events_table)

    messages = projections.get("messages", {}).get("messages", [])
    summary = projections.get("summary", {})

    messages_table = Table(title=f"messages 投影（折叠出 {len(messages)} 条消息）")
    messages_table.add_column("#", justify="right", style="dim")
    messages_table.add_column("role", no_wrap=True)
    messages_table.add_column("content")
    for index, message in enumerate(messages):
        content = str(message.get("content", ""))
        if len(content) > _PREVIEW_WIDTH:
            content = f"{content[:_PREVIEW_WIDTH]}…"
        messages_table.add_row(str(index), str(message.get("role")), content)
    console.print(messages_table)

    console.print(
        Panel(
            "\n".join(f"{key}: {value}" for key, value in summary.items()),
            title="summary 投影（另一个投影，同一份日志算出来的）",
            border_style="green",
        )
    )

    if balanced:
        console.print(
            "[dim]已启用 --balanced：未提交的尾巴（最后一个 turn/start 之后没有 turn/end 的部分）已被丢弃。[/dim]"
        )


async def cmd_show_all_sources(upto_seq: Optional[int], balanced: bool) -> None:
    """对每个会话做一次概览（事件数 / 投影消息数）."""
    table = Table(title="全部会话概览")
    table.add_column("会话", style="cyan")
    table.add_column("名称", overflow="fold")
    table.add_column("事件数", justify="right")
    table.add_column("投影消息数", justify="right")
    table.add_column("轮次数", justify="right")
    table.add_column("工具调用", justify="right")

    for session in load_sessions():
        records = await event_store.read(session.id, until_seq=upto_seq)
        projections = await event_store.replay(session.id, upto_seq=upto_seq, balanced=balanced)
        summary: dict[str, Any] = projections.get("summary", {})
        table.add_row(
            session.id[:8],
            (session.name or "")[:24],
            str(len(records)),
            str(len(projections.get("messages", {}).get("messages", []))),
            str(summary.get("turn_count", 0)),
            str(summary.get("tool_call_count", 0)),
        )

    console.print(table)


# --------------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器."""
    parser = argparse.ArgumentParser(
        description="会话事件日志工具：查看事件字典、回填历史、校验投影、回放会话",
    )
    parser.add_argument("--dict", action="store_true", help="打印事件字典")
    parser.add_argument("--backfill", action="store_true", help="把历史 chat_message 回填成事件（幂等）")
    parser.add_argument("--verify", action="store_true", help="校验投影与 chat_message 是否一致")
    parser.add_argument("--all", action="store_true", help="对所有会话执行")
    parser.add_argument("--session-id", help="只处理指定会话")
    parser.add_argument("--upto", type=int, default=None, help="只回放到指定 seq（历史快照）")
    parser.add_argument("--balanced", action="store_true", help="丢弃未提交的尾巴再折叠")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    return parser


async def main() -> int:
    """命令行主流程，返回进程退出码."""
    args = build_parser().parse_args()

    if args.dict:
        print_event_dictionary()
        return 0

    if args.backfill:
        await cmd_backfill()
        return 0

    if args.verify:
        if args.session_id:
            session = load_session(args.session_id)
            if session is None:
                console.print(f"[red]会话不存在: {args.session_id}[/red]")
                return 1
            sessions = [session]
        elif args.all:
            sessions = load_sessions()
        else:
            console.print("[red]--verify 需要配合 --session-id 或 --all[/red]")
            return 1
        return 1 if await cmd_verify(sessions) else 0

    if args.session_id:
        session = load_session(args.session_id)
        if session is None:
            console.print(f"[red]会话不存在: {args.session_id}[/red]")
            return 1
        await cmd_show(session, args.upto, args.balanced, args.json)
        return 0

    if args.all:
        await cmd_show_all_sources(args.upto, args.balanced)
        return 0

    print_event_dictionary()
    console.print("[dim]提示：加 --all 看所有会话概览，加 --verify --all 做一致性校验。[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
