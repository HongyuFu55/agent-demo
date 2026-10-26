"""共享数据库引擎的持有者.

为什么需要这个文件？
    事件存储要复用全局连接池，但如果 `store.py` 直接 `import database_service`，
    就会形成一条隐式依赖：只要导入事件模块，就会触发建表连接。
    测试、脚本、命令行工具想"只算投影不连库"时就很难受。

    所以这里做一个极小的注册表：`DatabaseService` 建好 engine 后注册进来，
    事件存储用的时候再取。导入顺序自由，也没有循环依赖。
"""

from typing import Optional

from sqlalchemy import Engine

_engine: Optional[Engine] = None


def register_engine(engine: Engine) -> None:
    """注册全局数据库引擎（由 DatabaseService 在初始化时调用）.

    参数：
        engine: SQLAlchemy 同步引擎。
    """
    global _engine
    _engine = engine


def get_engine() -> Engine:
    """取全局数据库引擎.

    返回：
        Engine: 已注册的引擎。

    抛出：
        RuntimeError: 尚未注册（通常是没导入 app.services.database）。
    """
    if _engine is None:
        raise RuntimeError("事件存储尚未绑定数据库引擎：请先导入 app.services.database，或向 EventStore 注入 engine")
    return _engine


def has_engine() -> bool:
    """是否已经注册引擎."""
    return _engine is not None
