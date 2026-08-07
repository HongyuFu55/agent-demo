"""网页抓取与 Web-RAG 工具模块 (web_scraper).

提供异步网页深度抓取与 DOM 净化工具，支持清理噪音节点与长文本安全截断。
配合 duckduckgo_search 与 execute_python_code 代码沙箱，形成完整的 Web-RAG 与动态爬虫分析架构。
"""

import re
from typing import Optional
import bs4
import httpx
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import logger

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 需要过滤清除的噪音 DOM 标签
NOISE_TAGS = ["script", "style", "nav", "footer", "header", "iframe", "noscript", "svg", "form"]


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def scrape_webpage(url: str, max_chars: int = 4000) -> str:
    """深度抓取并提取指定网页 (URL) 的正文文字内容。

    在搜索工具 (duckduckgo_search) 返回候选链接后，可调用此工具读取网页正文全貌。
    如果遇到极度复杂的动态表格或特定数据，也可组合 execute_python_code 编写针对性 Python 爬虫。

    参数：
        url: 完整的网页 URL 链接，必须以 http:// 或 https:// 开头
        max_chars: 截断最大字符数（默认 4000 字符，防冲垮 LLM 上下文）
    """
    if not url or not url.strip():
        return "[ERROR] 未传入有效的网页 URL 链接。"

    target_url = url.strip()
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        return f"[ERROR] URL 格式不合规：必须以 'http://' 或 'https://' 开头 (传入: {target_url})"

    logger.info("scraping_webpage_start", url=target_url)

    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=10.0,
            verify=False,
        ) as client:
            response = await client.get(target_url)

        if response.status_code != 200:
            logger.warning("scraping_webpage_http_error", url=target_url, status_code=response.status_code)
            return f"[ERROR] 抓取网页失败，HTTP 状态码回应: {response.status_code}"

        html_content = response.text
        if not html_content.strip():
            return f"[ERROR] 网页返回内容为空 (URL: {target_url})"

        # 使用 BeautifulSoup 解析并清理 DOM
        soup = bs4.BeautifulSoup(html_content, "html.parser")

        # 1. 移除噪音 DOM 节点
        for tag in soup(NOISE_TAGS):
            tag.decompose()

        # 2. 提取 title 与正文
        title = soup.title.string.strip() if soup.title and soup.title.string else "无标题网页"

        # 优先提取 <main> 或 <article> 标签内容，若无则提取 <body>
        main_content = soup.find("main") or soup.find("article") or soup.body or soup

        # 2.1 将 HTML 中的 <table> 标签自动转化为 Markdown 表格文本
        for table in main_content.find_all("table"):
            table_md = []
            rows = table.find_all("tr")
            for r in rows:
                cols = [td.get_text(strip=True) for td in r.find_all(["th", "td"])]
                if cols:
                    table_md.append("| " + " | ".join(cols) + " |")
            if table_md:
                # 插入分隔符行
                if len(table_md) > 1:
                    header_cols = len(table_md[0].split("|")) - 2
                    sep_line = "| " + " | ".join(["---"] * max(1, header_cols)) + " |"
                    table_md.insert(1, sep_line)
                table.replace_with(soup.new_string("\n\n" + "\n".join(table_md) + "\n\n"))

        raw_text = main_content.get_text(separator="\n")

        # 3. 清理多余空行与空白字符
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        clean_text = "\n".join(lines)

        # 4. 超长内容安全截断
        is_truncated = False
        if len(clean_text) > max_chars:
            clean_text = clean_text[:max_chars]
            is_truncated = True

        result_str = (
            f"--- 网页抓取成功 ---\n"
            f"目标 URL: {target_url}\n"
            f"网页标题: {title}\n"
            f"正文字数: {len(clean_text)} 字符 {'(已截断前 ' + str(max_chars) + ' 字符)' if is_truncated else ''}\n\n"
            f"=== 网页正文内容 ===\n"
            f"{clean_text}"
        )

        logger.info("scraping_webpage_success", url=target_url, text_length=len(clean_text))
        return result_str

    except httpx.TimeoutException:
        logger.warning("scraping_webpage_timeout", url=target_url)
        return f"[ERROR] 抓取网页超时 (超过 10.0 秒限制，URL: {target_url})"
    except Exception as e:
        logger.exception("scraping_webpage_failed", url=target_url, error=str(e))
        return f"[ERROR] 抓取网页发生系统异常：{str(e)}"
