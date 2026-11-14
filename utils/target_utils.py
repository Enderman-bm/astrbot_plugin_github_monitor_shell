"""推送目标工具：规范化、去重与去重键组合。"""

from typing import Iterable, List


def normalize_targets(items) -> List[str]:
    """把字符串 / 列表 / None 统一为去空白后的目标字符串列表"""
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    cleaned: List[str] = []
    for item in items:
        if item is None:
            continue
        value = str(item).strip()
        if value:
            cleaned.append(value)
    return cleaned


def dedupe_targets(items) -> List[str]:
    """规范化并保持首次出现顺序去重"""
    return list(dict.fromkeys(normalize_targets(items)))


def combine_target_keys(private_targets: Iterable | None,
                        group_targets: Iterable | None) -> List[str]:
    """组合私聊 / 群聊目标，生成用于发送记录去重的键列表

    私聊目标加 ``dm:`` 前缀，避免与群号 / 群 UMO 冲突；
    群聊目标保持原样，以兼容旧版 ``sent_notifications.json`` 里只存群号的记录。
    """
    keys = [f"dm:{target}" for target in dedupe_targets(private_targets)]
    keys.extend(dedupe_targets(group_targets))
    return list(dict.fromkeys(keys))
