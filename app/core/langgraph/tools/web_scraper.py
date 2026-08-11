"""网页抓取与 Web-RAG 工具模块 (web_scraper).

提供异步网页深度抓取与 DOM 净化工具，支持转换为结构化 Markdown、生成文档大纲、
直接将完整长文持久化到工作区，以及长文本安全截断。
配合 duckduckgo_search 与 execute_python_code 代码沙箱，形成完整的 Web-RAG 与动态爬虫分析架构。
"""

import re
from typing import Optional
import bs4
import httpx
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import logger
from app.core.langgraph.tools.file_ops import _get_safe_path

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


def _html_to_markdown(soup: bs4.BeautifulSoup, main_element: bs4.Tag) -> tuple[str, list[str]]:
    """将 HTML DOM 树转换为结构清晰的 Markdown 文本并提取标题大纲."""
    outline: list[str] = []

    # 1. 转换标题 h1~h6
    for level in range(1, 7):
        for header in main_element.find_all(f"h{level}"):
            h_text = header.get_text(strip=True)
            if h_text:
                prefix = "#" * level
                outline.append(f"{'  ' * (level - 1)}- {h_text}")
                header.replace_with(soup.new_string(f"\n\n{prefix} {h_text}\n\n"))

    # 2. 转换表格 table
    for table in main_element.find_all("table"):
        table_md = []
        rows = table.find_all("tr")
        for r in rows:
            cols = [td.get_text(strip=True).replace("\n", " ") for td in r.find_all(["th", "td"])]
            if cols:
                table_md.append("| " + " | ".join(cols) + " |")
        if table_md:
            if len(table_md) > 1:
                header_cols = len(table_md[0].split("|")) - 2
                sep_line = "| " + " | ".join(["---"] * max(1, header_cols)) + " |"
                table_md.insert(1, sep_line)
            table.replace_with(soup.new_string("\n\n" + "\n".join(table_md) + "\n\n"))

    # 3. 转换代码块 pre/code
    for pre in main_element.find_all("pre"):
        code_text = pre.get_text()
        pre.replace_with(soup.new_string(f"\n\n```\n{code_text.strip()}\n```\n\n"))

    # 4. 转换无序/有序列表
    for ul in main_element.find_all(["ul", "ol"]):
        list_items = []
        is_ol = ul.name == "ol"
        for idx, li in enumerate(ul.find_all("li", recursive=False), 1):
            li_text = li.get_text(strip=True)
            if li_text:
                bullet = f"{idx}." if is_ol else "-"
                list_items.append(f"{bullet} {li_text}")
        if list_items:
            ul.replace_with(soup.new_string("\n\n" + "\n".join(list_items) + "\n\n"))

    raw_text = main_element.get_text(separator="\n")
    # 5. 压缩多余连续换行
    clean_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()
    return clean_text, outline


@tool
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
async def scrape_webpage(
    url: str,
    max_chars: int = 15000,
    save_to_workspace_file: Optional[str] = None,
) -> str:
    """深度抓取并提取指定网页 (URL) 的正文文字，自动解析为 Markdown 结构（保留层级标题、表格与列表）.

    在搜索工具 (duckduckgo_search) 返回候选链接后，可调用此工具读取网页正文全貌。
    支持将完整超长正文直接保存到工作区文件（save_to_workspace_file），避免多轮截断与切片编写代码。

    参数：
        url: 完整的网页 URL 链接，必须以 http:// 或 https:// 开头
        max_chars: 返回文本的最大字符数（默认 15000 字符，最大支持 50000 字符）
        save_to_workspace_file: 可选。指定保存到工作区的文件相对路径（如 'web_article.md'），保存时写入完整全文不截断。
    """
    if not url or not url.strip():
        return "[ERROR] 未传入有效的网页 URL 链接。"

    target_url = url.strip()
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        return f"[ERROR] URL 格式不合规：必须以 'http://' 或 'https://' 开头 (传入: {target_url})"

    logger.info("scraping_webpage_start", url=target_url, save_file=save_to_workspace_file)

    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=15.0,
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

        # 3. 转换为结构化 Markdown 文本并生成大纲
        clean_text, outline = _html_to_markdown(soup, main_content)
        total_len = len(clean_text)

        # 4. 如果指定了保存到工作区文件，写入完整全文
        saved_file_info = ""
        if save_to_workspace_file and save_to_workspace_file.strip():
            try:
                target_file = _get_safe_path(save_to_workspace_file.strip())
                target_file.parent.mkdir(parents=True, exist_ok=True)
                with open(target_file, "w", encoding="utf-8") as f:
                    f.write(f"# {title}\n\nURL: {target_url}\n\n{clean_text}")
                saved_file_info = f"\n📁 [已将完整全文 ({total_len} 字符) 保存至工作区文件]: '{save_to_workspace_file}'"
                logger.info(
                    "webpage_saved_to_workspace",
                    url=target_url,
                    file_path=save_to_workspace_file,
                    total_chars=total_len,
                )
            except Exception as fe:
                saved_file_info = f"\n⚠️ [写入工作区文件失败]: {str(fe)}"

        # 5. 限制返回给 LLM 的正文字符数
        capped_max_chars = min(max(max_chars, 1000), 50000)
        is_truncated = total_len > capped_max_chars
        displayed_text = clean_text[:capped_max_chars] if is_truncated else clean_text

        outline_str = "\n".join(outline[:20]) if outline else "无明显标题层级"
        if len(outline) > 20:
            outline_str += f"\n...（共 {len(outline)} 个章节）"

        result_str = (
            f"--- 网页抓取成功 ---\n"
            f"目标 URL: {target_url}\n"
            f"网页标题: {title}\n"
            f"全文总字数: {total_len} 字符"
            f"{saved_file_info}\n\n"
            f"=== 📑 网页大纲目录 ===\n"
            f"{outline_str}\n\n"
            f"=== 📝 网页正文内容 {'(已展示前 ' + str(capped_max_chars) + ' 字符，完整内容已在工作区或请调大 max_chars)' if is_truncated else ''} ===\n"
            f"{displayed_text}"
        )

        logger.info("scraping_webpage_success", url=target_url, total_length=total_len, is_truncated=is_truncated)
        return result_str

    except httpx.TimeoutException:
        logger.warning("scraping_webpage_timeout", url=target_url)
        return f"[ERROR] 抓取网页超时 (超过 15.0 秒限制，URL: {target_url})"
    except Exception as e:
        logger.exception("scraping_webpage_failed", url=target_url, error=str(e))
        return f"[ERROR] 抓取网页发生系统异常：{str(e)}"
