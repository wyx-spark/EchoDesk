# -*- coding: utf-8 -*-
"""
[EN]
Query engine for model-market result sets: local filtering, structured
filtering, sorting, and pagination.

This module is intentionally separate from ``model_hub.py``. The market
interface requires fixed page sizes, complete paging controls, local real-time
filtering, structured facets, and four sort orders. All of these are pure data
transformations, independent of the network and the UI. Implementing them in
Python makes boundary conditions testable in the existing Python test suite:
the first page, the last page, invalid page numbers, and empty results.

The frontend sends one ``model_hub_query`` request per filter change. The
request performs an in-process computation and does not re-query the remote
service.

Design notes:

* Every function is pure: inputs are lists or dictionaries and outputs are new
  objects, with no retained mutable state.
* Multiple selections within one dimension are ORed; different dimensions are
  ANDed. A model cannot simultaneously belong to two mutually exclusive size
  buckets, so AND semantics within a dimension would always yield an empty set.
* Facet counts (``options``) are computed after keyword filtering and before
  structured filtering, so selecting one facet does not remove the other
  options. This matches conventional faceted-search behaviour.
* The ``quant`` filter compares ``quant_code``, a language-independent code,
  rather than the localized ``quantization`` display label. Using localized
  text as a filter key would invalidate filters after a language switch.

[ZH]
模型市场结果集查询引擎：本地筛选、结构化筛选、排序与分页。

本模块与 ``model_hub.py`` 刻意分离。模型市场界面要求固定每页条数、完整分页
控件、本地实时筛选、结构化分面与四种排序。这些操作均为纯数据变换，与网络和
界面无关。将实现放在 Python 中，可以利用现有 Python 测试体系覆盖边界条件：
第一页、最后一页、非法页码与空结果。

前端在每次筛选变更时发送一次 ``model_hub_query`` 请求。该请求在进程内完成
计算，不重新请求远端服务。

设计说明：

* 所有函数均为纯函数：输入为列表或字典，输出为新对象，不保留可变状态。
* 同一维度内的多选为“或”，不同维度之间为“与”。一个模型不可能同时属于两个
  互斥的规模区间，因此同维度内采用“与”语义必然得到空集。
* 分面计数（``options``）在关键词筛选之后、结构化筛选之前计算，使用户勾选
  一个分面时不会移除其他选项。这符合分面检索的通行行为。
* ``quant`` 筛选比较 ``quant_code`` 这一语言无关代码，而不是本地化的
  ``quantization`` 显示文本。使用本地化文本作为筛选键会导致切换语言后筛选
  条件失效。
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

#: [EN] Supported page sizes: 20, 50, and 100, with 20 as the default.
#: [ZH] 支持的每页条数：20、50、100，默认 20。
PAGE_SIZES: Tuple[int, ...] = (20, 50, 100)
DEFAULT_PAGE_SIZE = 20

#: [EN] Sort keys: lifetime downloads, likes, update time, and parameter count.
#: [ZH] 排序键：历史总下载量、点赞数、更新时间与参数量。
SORT_KEYS: Tuple[str, ...] = ("downloads", "likes", "updated", "params")

#: [EN] Grouping key for unknown parameter counts or update times. Both locales
#: [EN] render their own localized label for this key.
#: [ZH] 未知参数量或未知更新时间的归组键。两种语言分别渲染各自的本地化标签。
UNKNOWN = "unknown"

#: [EN] Parameter-count buckets: (key, inclusive lower bound, inclusive upper
#: [EN] bound). A ``None`` lower bound means the bucket is unbounded below.
#: [ZH] 参数量区间：(键, 含下界, 含上界)。下界为 ``None`` 表示不设下限。
PARAM_BUCKETS: Tuple[Tuple[str, Optional[float], Optional[float]], ...] = (
    ("p_le1", None, 1.0),
    ("p_1_3", 1.0, 3.0),
    ("p_3_8", 3.0, 8.0),
    ("p_8_20", 8.0, 20.0),
    ("p_20_40", 20.0, 40.0),
    ("p_gt40", 40.0, None),
)
ALL_PARAM_BUCKETS = tuple(key for key, _, _ in PARAM_BUCKETS) + (UNKNOWN,)

#: [EN] Update-time buckets: (key, maximum age in days), interpreted as
#: [EN] "within the last N days".
#: [ZH] 更新时间区间：(键, 最大天数)，按“最近 N 天内”解释。
UPDATE_BUCKETS: Tuple[Tuple[str, int], ...] = (
    ("u_7", 7),
    ("u_30", 30),
    ("u_90", 90),
    ("u_365", 365),
)
ALL_UPDATE_BUCKETS = tuple(key for key, _ in UPDATE_BUCKETS) + ("u_old", UNKNOWN)

#: [EN] Tri-state filter for local download status.
#: [ZH] “是否已在本地下载”的三态筛选值。
INSTALLED_STATES = ("any", "yes", "no")


def _text(value: Any) -> str:
    """
    [EN]
    Convert a value to string, mapping ``None`` to an empty string.

    ``str(value or "")`` must not be used here: it maps ``0`` to an empty
    string, which would make page number 0 indistinguishable from an empty
    input. That would suppress the invalid-input indication and silently show
    the first page.

    [ZH]
    将值转换为字符串，``None`` 映射为空串。

    此处不得使用 ``str(value or "")``：该写法会把 ``0`` 映射为空串，使页码 0
    与空输入无法区分，从而抑制非法输入提示并静默显示第一页。
    """
    return "" if value is None else str(value)


def keyword_haystack(item: Dict[str, Any]) -> str:
    """
    [EN]
    Build the text searched by keyword filtering: repository name, description,
    and tags.

    The list endpoint does not return the model-card body, so the description is
    composed from metadata available in the response (pipeline tags, library,
    author, and file name). See ``EXPAND_FIELDS`` in ``model_hub.py`` for the
    field expansion rule.

    [ZH]
    构造关键词筛选所使用的检索文本：仓库名、描述与标签。

    列表接口不返回模型卡正文，因此描述由响应中可用的元数据合成（流水线标签、
    库、作者与文件名）。字段扩展规则见 ``model_hub.py`` 中的 ``EXPAND_FIELDS``。
    """
    parts = [
        _text(item.get("repo")), _text(item.get("name")), _text(item.get("author")),
        _text(item.get("description")), " ".join(_text(t) for t in (item.get("tags") or [])),
        _text(item.get("quantization")), _text(item.get("quant_code")),
    ]
    return " ".join(parts).lower()


def matches_keyword(item: Dict[str, Any], keyword: str) -> bool:
    """
    [EN]
    Return whether the keyword matches. The keyword is split on whitespace and
    every token must occur, so multiple tokens narrow the result set.

    This all-token rule is verified by unit tests: ``Qwen3 27B`` matches
    ``unsloth/Qwen3.8-27B-GGUF`` while ``Qwen3 999B`` does not. Treating the
    whole query as one substring would incorrectly match the latter.

    [ZH]
    返回关键词是否命中。关键词按空白拆分，每个词元都必须出现，因此多个词元会
    收窄结果集。

    该“全部词元命中”规则由单元测试覆盖：``Qwen3 27B`` 命中
    ``unsloth/Qwen3.8-27B-GGUF``，而 ``Qwen3 999B`` 不命中。若将整个查询作为
    单个子串匹配，后者会被错误命中。
    """
    needle = _text(keyword).strip().lower()
    if not needle:
        return True
    hay = keyword_haystack(item)
    return all(token in hay for token in needle.split())


def param_bucket(param_b: Any) -> str:
    """
    [EN]
    Map a parameter count to its bucket key. Values that cannot be parsed
    (0, ``None``, or non-numeric) are classified as ``unknown``.

    [ZH]
    将参数量映射为区间键。无法解析的值（0、``None`` 或非数字）归入 ``unknown``。
    """
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
    """
    [EN]
    Map an update timestamp (``YYYY-MM-DD``) to its bucket key. Missing or
    unparsable values are classified as ``unknown``.

    ``now`` is supplied by the caller in Unix seconds so that tests are
    reproducible; this function does not read the system clock.

    [ZH]
    将更新时间戳（``YYYY-MM-DD``）映射为区间键。缺失或无法解析的值归入
    ``unknown``。

    ``now`` 由调用方以 Unix 秒传入，以保证测试可复现；本函数不读取系统时钟。
    """
    text = _text(updated_at).strip()
    if len(text) < 10:
        return UNKNOWN
    try:
        stamp = calendar.timegm(_time.strptime(text[:10], "%Y-%m-%d"))
    except (ValueError, TypeError):
        return UNKNOWN
    days = (now - stamp) / 86400.0
    # [EN] Treat future timestamps (time-zone or server-clock skew) as recent.
    # [ZH] 未来时间戳（时区或服务端时钟偏差）按最近处理。
    if days < -1:
        days = 0.0
    for key, limit in UPDATE_BUCKETS:
        if days <= limit:
            return key
    return "u_old"


def normalize_filters(raw: Any) -> Dict[str, Any]:
    """
    [EN]
    Normalize the filter object supplied by the frontend. Missing fields,
    incorrectly typed values, and ``None`` are tolerated.

    [ZH]
    规范化前端传入的筛选对象。缺失字段、类型错误与 ``None`` 均可容忍。
    """
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
        # [EN] Quantization codes come from the data itself (Q4_K_M, IQ2_XS, and
        # [EN] so on). No allow-list is applied; values are upper-cased and
        # [EN] stripped so that case differences do not hide matches.
        # [ZH] 量化代码来自数据本身（Q4_K_M、IQ2_XS 等）。不进行白名单校验；
        # [ZH] 统一转为大写并去除空白，避免大小写差异导致无法匹配。
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
    """
    [EN]
    Apply structured filters: dimensions are ANDed and values within a
    dimension are ORed. See the module documentation for the rationale.

    [ZH]
    应用结构化筛选：不同维度之间为“与”，同一维度内的多个取值为“或”。
    设计依据见模块文档。
    """
    params = filters.get("params") or []
    if params:
        bucket = param_bucket(item.get("param_b"))
        if bucket not in params:
            return False

    quants = filters.get("quant") or []
    if quants:
        code = _text(item.get("quant_code")).upper()
        # [EN] A repository without a quantization tag has an empty quant_code.
        # [EN] Such entries must not be silently discarded by a quantization
        # [EN] filter: a repository that does not declare its quantization is
        # [EN] still a potential candidate. It is classified as unknown and is
        # [EN] visible only when the unknown facet is selected.
        # [ZH] 未声明量化标签的仓库，其 quant_code 为空。此类条目不得被量化筛选
        # [ZH] 静默丢弃：未声明量化的仓库仍然是潜在候选。将其归入 unknown 档，
        # [ZH] 仅在用户勾选未知档时显示。
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
    """
    [EN]
    Sort by one of the four supported dimensions. All dimensions are descending,
    and the result is stable and reproducible.

    * ``downloads``: lifetime download count, not the server's 30-day metric
      (see ``model_hub`` for the metric distinction).
    * ``likes``: like count.
    * ``updated``: update time, with missing values last.
    * ``params``: parameter count, with unknown values last and downloads as the
      secondary key.

    Ties are always resolved by ``repo`` in ascending order. Without this final
    key, two identical queries could return different orders because the input
    order comes from the network; pagination would then show duplicate entries
    across pages.

    [ZH]
    按四种受支持的维度排序。所有维度均为降序，结果稳定且可复现。

    * ``downloads``：历史总下载量，而非服务端的 30 天指标（口径差异见
      ``model_hub``）。
    * ``likes``：点赞数。
    * ``updated``：更新时间，缺失值排在最后。
    * ``params``：参数量，未知值排在最后，并以下载量作为次级排序键。

    并列时始终以 ``repo`` 升序作为最终裁决键。若缺少该键，两次相同查询可能
    返回不同顺序，因为输入顺序来自网络；分页时会出现跨页重复条目。
    """
    key = sort_key if sort_key in SORT_KEYS else "downloads"
    if key == "downloads":
        return sorted(items, key=lambda it: (-_int(it.get("downloads")), _text(it.get("repo"))))
    if key == "likes":
        return sorted(items, key=lambda it: (-_int(it.get("likes")),
                                             -_int(it.get("downloads")), _text(it.get("repo"))))
    if key == "updated":
        # [EN] ISO date strings sort lexicographically in chronological order;
        # [EN] an empty value is mapped to "0" so that it sorts last.
        # [ZH] ISO 日期字符串的字典序与时间序一致；空值映射为 "0" 以排在最后。
        return sorted(items, key=lambda it: (_text(it.get("updated_at")) or "0",
                                             _text(it.get("repo"))), reverse=True)
    # [EN] Parameter sort: unknown counts (0) last, then by downloads.
    # [ZH] 参数量排序：未知参数量（0）排在最后，其次按下载量排序。
    return sorted(items, key=lambda it: (-_float(it.get("param_b")),
                                         -_int(it.get("downloads")), _text(it.get("repo"))))


def filter_options(items: List[Dict[str, Any]], now: float,
                   quant_labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    [EN]
    Compute facet counts: the number of currently matching entries for each
    filter option (for example, ``Q4_K_M (123)`` in the interface).

    Counts are computed after keyword filtering and before structured filtering;
    see the module documentation.

    [ZH]
    计算分面计数：每个筛选项当前可命中的条目数（例如界面上的
    ``Q4_K_M (123)``）。

    计数在关键词筛选之后、结构化筛选之前计算；详见模块文档。
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
    """
    [EN]
    Clamp a user-supplied page number to the valid range and return
    ``(page, corrected)``.

    Invalid input (empty, non-numeric, 0, negative, or beyond the last page) is
    clamped to ``[1, pages]`` and reported as corrected, so the interface can
    inform the user instead of silently navigating elsewhere.

    [ZH]
    将用户输入的页码限制到合法范围，返回 ``(页码, 是否被纠正)``。

    非法输入（空、非数字、0、负数或超过末页）统一限制到 ``[1, pages]``，并
    报告为已纠正，使界面能够提示用户，而不是静默跳转到其他位置。
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
    """
    [EN]
    Apply keyword filtering, structured filtering, sorting, and pagination to
    the already loaded result set.

    ``loaded``, ``filtered``, and ``total_available`` form the three-level
    counts displayed by the interface (for example, "47 after local filtering /
    1000 loaded / 4356 total"). When filtering produces an empty set, pagination
    still reports one page so that the interface can disable the paging
    controls.

    [ZH]
    在已加载的结果集上依次执行关键词筛选、结构化筛选、排序与分页。

    ``loaded``、``filtered`` 与 ``total_available`` 组成界面显示的三层计数
    （例如“本地筛选后 47 条 / 已加载 1000 条 / 共 4356 条”）。筛选结果为空时，
    分页仍报告一页，使界面能够禁用翻页控件。
    """
    cleaned = normalize_filters(filters)
    sort_key = sort if sort in SORT_KEYS else "downloads"

    matched = [it for it in items if matches_keyword(it, keyword)]
    options = filter_options(matched, now, quant_labels)
    filtered = [it for it in matched if matches_filters(it, cleaned, now)]
    ordered = sort_items(filtered, sort_key)

    size = _int(page_size) if _int(page_size) else DEFAULT_PAGE_SIZE
    if size not in PAGE_SIZES and size > 0:
        # [EN] Accept custom page sizes while rejecting zero and negative values.
        # [ZH] 接受自定义每页条数，但拒绝 0 与负数。
        size = max(1, min(size, 500))
    pages = max(1, int(math.ceil(len(ordered) / float(size)))) if ordered else 1
    current, corrected = page_number(page, pages)
    start = (current - 1) * size
    page_items = ordered[start:start + size]

    total = len(items) if loaded is None else int(loaded)
    # [EN] "Total N" is exact only when loading is complete (``has_more`` is
    # [EN] false). If the service stops at a safety limit and does not return a
    # [EN] total, this value must remain ``None`` so that the interface can show
    # [EN] "N loaded (more available)". Reporting ``loaded`` as the total would
    # [EN] understate the count substantially.
    # [ZH] 仅当加载完成（``has_more`` 为假）时，“共 N 条”才是精确值。若服务端在
    # [ZH] 安全上限处停止且未返回总数，该值必须保持 ``None``，使界面能够显示
    # [ZH] “已加载 N 条（还有更多）”。将 ``loaded`` 当作总数会显著少报。

    if total_available is None and not has_more:
        available = total
    else:
        available = None if total_available is None else int(total_available)
    empty_reason = ""
    if not ordered and total:
        # [EN] Distinguish "no local match" from "empty dataset"; the interface
        # [EN] uses this to offer "search the remote again".
        # [ZH] 区分“本地无匹配”与“数据集为空”；界面据此提供“重新搜索远端”入口。
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
