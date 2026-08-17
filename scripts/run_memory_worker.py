"""记忆 Worker 独立的 Python 命令行启动入口脚本."""

import asyncio
import sys
from pathlib import Path

# 将项目根目录添加到 pythonpath
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from app.services.memory_worker import main

if __name__ == "__main__":
    asyncio.run(main())
