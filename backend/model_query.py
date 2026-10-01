# -*- coding: utf-8 -*-
"""模型市场的结果集查询引擎：本地筛选 / 结构化筛选 / 排序 / 分页。

为什么单独成模块
----------------
任务书里"每页 20 条 + 完整分页控件 + 本地实时筛选 + 结构化筛选器 + 四种排序"
全都是**纯数据变换**，与网络、与 WPF 都无关。放进 ``model_hub.py`` 会被网络代码淹没，
放进 C# 则完全无法验证 —— 边界条件（第一页 / 最后一页 / 非法页码 / 筛完为空）
恰恰是任务书点名要验的东西，而本仓库有 Python 测试体系、没有 C# 测试体系。

因此：**数据怎么筛、怎么排、怎么分页在 Python 里做，前端只负责画**。
前端每次筛选变更发一次 ``model_hub_query``（进程内往返，毫秒级），
**不重新请求远端** —— 这就是任务书"本地实时筛选、不重新请求远端"的落法。

设计要点
--------
* 所有函数都是纯函数（输入 list/dict，输出新对象），不持有状态，便于逐条断言。
* **同一维度内多选 = 或，不同维度之间 = 与**。任务书写的是"筛选器为多选，
  条件之间为'与'关系"，但同维度内真做"与"必然恒为空（一个模型不可能同时属于
  "≤1B"和"20–40B"两个区间）。取业界通用的分面语义，并在记录里写明这处判断。
* 分面计数（``options``）基于**关键词筛选后、结构化筛选前**的结果集计算，
  否则用户一勾选，其它选项就消失了。这也是分面筛选的通行做法。
* ``quant`` 筛选比的是 ``quant_code``（语言无关的原始格式化代码），
  不是界面上的 ``quantization``（未知时是本地化过的"未知"）——
  拿本地化文案当筛选键会让界面语言一变、筛选条件就失效。
"""
from __future__ import annotations

import calendar
import math
import time as _time
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "PAGE_SIZES", "DEFAULT_PAGE_SIZE", "SORT_KEYS", "PARAM_BUCKETS", "UPDATE_BUCKETS",
    "ALL_PARAM_BUCKETS", "ALL_UPDATE_BUCKETS", "UNKNOWN",
    "keyword_haystack", "matches_keyword", "param_bucket", "update_bucket",
    "matches_filters", "sort_items", "filter_options", "query", "page_number",
    "normalize_filters",
]

#: 允许的每页条数（任务三：20 / 50 / 100，默认 20）
PAGE_SIZES: Tuple[int, ...] = (20, 50, 100)
DEFAULT_PAGE_SIZE = 20

#: 排序键：历史总下载量 / 点赞数 / 更新时间 / 参数量
SORT_KEYS: Tuple[str, ...] = ("downloads", "likes", "updated", "params")

#: 未知参数量 / 未知更新时间的归组键（两种语言下都显示为各自的"未知"）
UNKNOWN = "unknown"

#: 参数量区间：(键, 下界含, 上界含)。下界 None 表示不设下限。
PARAM_BUCKETS: Tuple[Tuple[str, Optional[float], Optional[float]], ...] = (
    ("p_le1", None, 1.0),
    ("p_1_3", 1.0, 3.0),
    ("p_3_8", 3.0, 8.0),
    ("p_8_20", 8.0, 20.0),
    ("p_20_40", 20.0, 40.0),
    ("p_gt40", 40.0, None),
)
ALL_PARAM_BUCKETS = tuple(key for key, _, _ in PARAM_BUCKETS) + (UNKNOWN,)

#: 更新时间区间：(键, 天数上界)。按"距今多少天以内"理解。
UPDATE_BUCKETS: Tuple[Tuple[str, int], ...] = (
    ("u_7", 7),
    ("u_30", 30),
    ("u_90", 90),
    ("u_365", 365),
)
ALL_UPDATE_BUCKETS = tuple(key for key, _ in UPDATE_BUCKETS) + ("u_old", UNKNOWN)

#: "是否本地已下载"的三态
INSTALLED_STATES = ("any", "yes", "no")


def _text(value: Any) -> str:
    """转字符串；``None`` → 空串。

    **不能用 ``str(value or "")``**：那样 ``0`` 会变成空串，
    于是"页码填 0"会被当成"没填"（实测：``page=0`` 时 ``page_corrected`` 回 False，
    界面上不会提示输入非法，而是悄悄跳到第 1 页）。
    """
    return "" if value is None else str(value)


def keyword_haystack(item: Dict[str, Any]) -> str:
    """拼出关键词筛选的检索文本：仓库名 + 描述 + 标签。

    任务书要求的筛选范围是"仓库名、描述、标签"。列表接口不返回模型卡正文，
    因此"描述"由接口能给的元数据合成（流水线标签 / 库 / 作者 / 文件名）。
    详见 ``model_hub.py`` 中 ``EXPAND_FIELDS`` 的说明与记录文件里的偏差条目。
    """
    parts = [
        _text(item.get("repo")), _text(item.get("name")), _text(item.get("author")),
        _text(item.get("description")), " ".join(_text(t) for t in (item.get("tags") or [])),
        _text(item.get("quantization")), _text(item.get("quant_code")),
    ]
    return " ".join(parts).lower()


def matches_keyword(item: Dict[str, Any], keyword: str) -> bool:
    """关键词是否命中：按空白拆词，**每个词都必须出现**（多词收窄，符合直觉）。

    单测依据：``Qwen3 27B`` 命中 ``unsloth/Qwen3.8-27B-GGUF``，而
    ``Qwen3 999B`` 不命中 —— 用整串当子串匹配的话后者会意外命中。
    """
    needle = _text(keyword).strip().lower()
    if not needle:
        return True
    hay = keyword_haystack(item)
    return all(token in hay for token in needle.split())


def param_bucket(param_b: Any) -> str:
    """参数量 → 区间键。解析不到参数量（0/None/非数字）时归入 ``unknown``。"""
    try:
        value = float(param_b or 0)
    except (TypeError, ValueError):
        return UNKNOWN
    if value <= 0:
        return UNKNOWN
    for key, low, high in PARAM_BUCKETS:
        if low is not None and value <= low:
            continue
        if high is not None and value > high:
            continue
        return key
    return UNKNOWN


def update_bucket(updated_at: Any, now: float) -> str:
    """更新时间（``YYYY-MM-DD``）→ 区间键；缺失或无法解析时归入 ``unknown``。

    ``now`` 由调用方传入（Unix 秒），保证测试可复现 —— 这个函数里不读系统时间。
    """
    text = _text(updated_at).strip()
    if len(text) < 10:
        return UNKNOWN
    try:
        stamp = calendar.timegm(_time.strptime(text[:10], "%Y-%m-%d"))
    except (ValueError, TypeError):
        return UNKNOWN
    days = (now - stamp) / 86400.0
    if days < -1:            # 未来时间（时区/服务端时钟漂移）按"最近"处理
        days = 0.0
    for key, limit in UPDATE_BUCKETS:
        if days <= limit:
            return key
    return "u_old"


def normalize_filters(raw: Any) -> Dict[str, Any]:
    """把前端传来的筛选对象整理成规整结构（容忍缺字段、类型写错、None）。"""
    data = raw if isinstance(raw, dict) else {}

    def as_list(key: str, allowed: Sequence[str]) -> List[str]:
        value = data.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        out: List[str] = []
        for entry in value:
            text = _text(entry).strip()
            if text and text in allowed and text not in out:
                out.append(text)
        return out

    installed = _text(data.get("installed") or "any").strip().lower()
    if installed not in INSTALLED_STATES:
        installed = "any"
    try:
        min_downloads = int(data.get("min_downloads") or 0)
    except (TypeError, ValueError):
        min_downloads = 0
    return {
        "params": as_list("params", ALL_PARAM_BUCKETS),
        # 量化格式来自数据本身（Q4_K_M / IQ2_XS …），不做白名单校验，
        # 但统一大写并去空，避免大小写差异导致筛不出来。
        "quant": [q.upper() for q in _string_list(data.get("quant"))],
        "updated": as_list("updated", ALL_UPDATE_BUCKETS),
        "installed": installed,
        "recommended": bool(data.get("recommended")),
        "min_downloads": max(0, min_downloads),
    }


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for entry in value:
        text = _text(entry).strip()
        if text and text not in out:
            out.append(text)
    return out


def matches_filters(item: Dict[str, Any], filters: Dict[str, Any], now: float) -> bool:
    """结构化筛选：**不同维度与、同维度或**（见模块文档）。"""
    params = filters.get("params") or []
    if params:
        bucket = param_bucket(item.get("param_b"))
        if bucket not in params:
            return False

    quants = filters.get("quant") or []
    if quants:
        code = _text(item.get("quant_code")).upper()
        # 仓库名里没有量化标签时 quant_code 为空 —— 这种条目在"按量化筛选"时
        # 不该被无声丢掉：用户搜的是"Q4_K_M"的模型，一个连量化都没写明的仓库
        # 也算潜在候选。归到"unknown"档，用户勾了未知档才会看到。
        if not code:
            code = UNKNOWN.upper()
        if code not in quants:
            return False

    updated = filters.get("updated") or []
    if updated:
        if update_bucket(item.get("updated_at"), now) not in updated:
            return False

    installed = filters.get("installed") or "any"
    if installed == "yes" and not item.get("installed"):
        return False
    if installed == "no" and item.get("installed"):
        return False

    if filters.get("recommended") and not item.get("recommended"):
        return False

    min_downloads = int(filters.get("min_downloads") or 0)
    if min_downloads:
        try:
            downloads = int(item.get("downloads") or 0)
        except (TypeError, ValueError):
            downloads = 0
        if downloads < min_downloads:
            return False
    return True


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def sort_items(items: List[Dict[str, Any]], sort_key: str) -> List[Dict[str, Any]]:
    """按任务书要求的四种维度排序（都是降序），并保证结果**稳定且可复现**。

    * ``downloads``：历史总下载量（**不是**服务端的 30 天口径，见 model_hub 说明）；
    * ``likes``：点赞数；
    * ``updated``：更新时间，缺失的排在最后；
    * ``params``：参数量，未知的排在最后，同规模再按下载量。

    并列时一律以 ``repo`` 升序做最终裁决 —— 没有这一步，两次相同查询可能给出
    不同顺序（Python 排序稳定，但输入顺序来自网络，本来就可能变），
    分页时就会出现"翻页看到重复条目"。
    """
    key = sort_key if sort_key in SORT_KEYS else "downloads"
    if key == "downloads":
        return sorted(items, key=lambda it: (-_int(it.get("downloads")), _text(it.get("repo"))))
    if key == "likes":
        return sorted(items, key=lambda it: (-_int(it.get("likes")),
                                             -_int(it.get("downloads")), _text(it.get("repo"))))
    if key == "updated":
        # ISO 日期字符串的字典序 == 时间序；空串用 '0' 兜底排到最后
        return sorted(items, key=lambda it: (_text(it.get("updated_at")) or "0",
                                             _text(it.get("repo"))), reverse=True)
    # params：未知参数量（0）排最后，其次按下载量
    return sorted(items, key=lambda it: (-_float(it.get("param_b")),
                                         -_int(it.get("downloads")), _text(it.get("repo"))))


def filter_options(items: List[Dict[str, Any]], now: float,
                   quant_labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """分面计数：给出每个筛选项当前可命中的条数（用于界面上的"Q4_K_M (123)"）。

    基于**关键词筛选后、结构化筛选前**的集合计算，见模块文档。
    """
    labels = quant_labels or {}
    params: Dict[str, int] = {key: 0 for key in ALL_PARAM_BUCKETS}
    updated: Dict[str, int] = {key: 0 for key in ALL_UPDATE_BUCKETS}
    quant: Dict[str, int] = {}
    installed = 0
    recommended = 0
    for item in items:
        params[param_bucket(item.get("param_b"))] += 1
        updated[update_bucket(item.get("updated_at"), now)] += 1
        code = _text(item.get("quant_code")).upper() or UNKNOWN.upper()
        quant[code] = quant.get(code, 0) + 1
        if item.get("installed"):
            installed += 1
        if item.get("recommended"):
            recommended += 1
    return {
        "params": params,
        "updated": updated,
        "quant": quant,
        "quant_labels": labels,
        "installed": installed,
        "recommended": recommended,
        "total": len(items),
    }


def page_number(value: Any, pages: int) -> Tuple[int, bool]:
    """把用户输入的页码夹到合法范围。返回 ``(页码, 是否被纠正)``。

    非法输入（空、非数字、0、负数、超出总页数）一律夹到 ``[1, pages]``，
    并回报"被纠正过"，让界面能给出提示而不是默默跳到别处。
    """
    total_pages = max(1, int(pages or 1))
    text = _text(value).strip()
    try:
        number = int(text)
    except (TypeError, ValueError):
        return 1, text != ""
    clamped = min(max(number, 1), total_pages)
    return clamped, clamped != number


def query(
    items: List[Dict[str, Any]],
    *,
    keyword: str = "",
    filters: Any = None,
    sort: str = "downloads",
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
    now: float = 0.0,
    total_available: Optional[int] = None,
    has_more: bool = False,
    loaded: Optional[int] = None,
    quant_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """在**已加载的结果集**上做 关键词筛选 → 结构化筛选 → 排序 → 分页。

    返回的 ``loaded`` / ``filtered`` / ``total_available`` 三个数就是任务书要求的
    "本地筛选后 47 条 / 已加载 1000 条 / 共 4356 条" 那三层数字：
    过滤完为空时分页仍按 1 页处理（``pages`` = 1），界面据此禁用翻页按钮。
    """
    cleaned = normalize_filters(filters)
    sort_key = sort if sort in SORT_KEYS else "downloads"

    matched = [it for it in items if matches_keyword(it, keyword)]
    options = filter_options(matched, now, quant_labels)
    filtered = [it for it in matched if matches_filters(it, cleaned, now)]
    ordered = sort_items(filtered, sort_key)

    size = _int(page_size) if _int(page_size) else DEFAULT_PAGE_SIZE
    if size not in PAGE_SIZES and size > 0:
        size = max(1, min(size, 500))          # 容忍自定义值，但不允许 0/负数
    pages = max(1, int(math.ceil(len(ordered) / float(size)))) if ordered else 1
    current, corrected = page_number(page, pages)
    start = (current - 1) * size
    page_items = ordered[start:start + size]

    total = len(items) if loaded is None else int(loaded)
    # "共 N 条"只有在**取完了**（has_more 为假）时才是精确值。
    # 撞上安全上限时服务端不返回总数，此时必须留 None，让界面显示"已加载 N 条（还有更多）"——
    # 把 loaded 当成总数显示会凭空少报（实测 GGUF 关键词下就是 5000 vs 实际的 8000+）。
    if total_available is None and not has_more:
        available = total
    else:
        available = None if total_available is None else int(total_available)
    empty_reason = ""
    if not ordered and total:
        # 本地筛选没结果的出口（任务四）：界面据此给出"是否重新搜索远端？"
        empty_reason = "local_no_match" if (keyword or cleaned.get("params") or cleaned.get("quant")
                                           or cleaned.get("updated")
                                           or cleaned.get("installed") != "any"
                                           or cleaned.get("recommended")) else "empty_dataset"
    return {
        "items": page_items,
        "page": current,
        "page_size": size,
        "pages": pages,
        "page_corrected": corrected,
        "filtered": len(ordered),
        "loaded": total,
        "total_available": available,
        "has_more": bool(has_more),
        "sort": sort_key,
        "keyword": _text(keyword).strip(),
        "filters": cleaned,
        "options": options,
        "empty_reason": empty_reason,
    }
