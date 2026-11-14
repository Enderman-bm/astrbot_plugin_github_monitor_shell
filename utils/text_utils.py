"""文本工具：Commit message 展示、长文本截断与消息分片

QQ（OneBot/NapCat/Lagrange 等实现）对单条文本消息长度有限制，
内容过长会导致整条消息发送失败，因此提供截断与分片工具；
同时提供 Commit message 的 full / first_line 展示转换。
"""

from typing import List


def truncate_text(text: str, max_length: int, suffix: str = "\n…（内容过长，已截断）") -> str:
    """按最大字符数截断文本，超出时附加提示后缀"""
    text = (text or "").strip()
    if max_length <= 0 or len(text) <= max_length:
        return text
    return text[:max_length].rstrip() + suffix


def normalize_commit_message(text: str) -> str:
    """统一换行符并去掉首尾空白，保留正文内部换行"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def commit_message_first_line(text: str) -> str:
    """取 commit message 的第一行（第一个非空行），即提交标题"""
    normalized = normalize_commit_message(text)
    for line in normalized.split("\n"):
        if line.strip():
            return line.strip()
    return ""


def commit_message_for_display(text: str, mode: str = "full") -> str:
    """按展示模式返回 commit message

    mode:
    - full: 完整 message（保留正文换行）
    - first_line: 只保留第一行（提交标题）
    """
    normalized = normalize_commit_message(text)
    if str(mode).strip().lower() == "first_line":
        return commit_message_first_line(normalized)
    return normalized


def split_long_message(text: str, max_length: int) -> List[str]:
    """把超长文本拆分为多条不超过 max_length 的消息

    优先按换行边界拆分，保持段落完整；单行超过 max_length 时硬切。
    输入不超过限制时原样返回单元素列表。
    """
    text = (text or "").strip("\n")
    if max_length <= 0 or len(text) <= max_length:
        return [text] if text else [""]

    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        # 单行超长时先硬切
        while len(line) > max_length:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_length])
            line = line[max_length:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_length:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks
