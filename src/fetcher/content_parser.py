"""
HTML → 纯文本转换、内容清洗、截断
"""
import re
from bs4 import BeautifulSoup


def html_to_text(html: str) -> str:
    """将 HTML 转换为纯文本，去除脚本/样式标签。"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    # Full pages often contain thousands of characters of navigation before
    # the story. Extract the article body before applying the length budget.
    for selector in ("[itemprop='articleBody']", ".td-post-content", ".wp-block-post-content", ".entry-content", "#js_content", "article", "main"):
        candidates = soup.select(selector)
        if candidates:
            body = max(candidates, key=lambda node: len(node.get_text()))
            if len(body.get_text(strip=True)) >= 200:
                soup = body
                break
    for tag in soup.select('nav, header, footer, aside, form'):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return _clean_text(text)


def _clean_text(text: str) -> str:
    """折叠空白、去除零宽字符等噪音。"""
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)   # 零宽字符
    text = re.sub(r"\s+", " ", text)                           # 折叠空白
    return text.strip()


def truncate(text: str, max_chars: int = 3000) -> str:
    """截断文本，避免 token 爆炸。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def parse_article_content(raw_content: str, max_chars: int = 3000) -> str:
    """
    完整处理流程：HTML → 文本 → 清洗 → 截断。
    raw_content 可能是 HTML 也可能是纯文本。
    """
    # 如果包含 HTML 标签则解析，否则直接清洗
    if re.search(r"<[a-zA-Z][\s\S]*?>", raw_content):
        text = html_to_text(raw_content)
    else:
        text = _clean_text(raw_content)
    return truncate(text, max_chars)
