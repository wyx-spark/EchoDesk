"""EchoDesk 后端配置管理。

支持"基础配置 + 用户补丁覆盖"（config.user.json 中的字段优先），
仿 DeepSeek-Harness 的 profile patch 机制：应用只写回基础配置，
用户手工补丁保持不变。
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# 上下文窗口的硬性下限（低于此值无法正常对话）
MIN_CONTEXT = 512

# 已知模型的训练上下文窗口（文件名关键词 → tokens）。
# 用于在无需加载模型时也能按"所选模型"决定窗口大小；运行时还会用 GGUF 元数据校准。
DEFAULT_MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "qwen3.5-0.8b": 32768,
    "qwen3-0.6b": 32768,
    "qwen3": 40960,
    "qwen2.5": 32768,
    "llama-3": 8192,
    "llama3": 8192,
    "gemma": 8192,
    "phi-3": 4096,
}


class SummarizeConfig(BaseModel):
    """长文档分段摘要（预处理）配置。

    文件全文超出窗口一定比例时，先分段生成摘要再合并注入，而不是直接截断。
    """

    enabled: bool = True
    # 文件 token 占窗口比例超过该值即触发分段摘要
    trigger_ratio: float = Field(default=0.5, ge=0.05, le=0.95)
    # 单段字符数与段间重叠字符数（重叠避免切断上下文语义）
    chunk_chars: int = Field(default=3000, ge=500, le=20000)
    overlap_chars: int = Field(default=200, ge=0, le=2000)
    # 单段摘要与最终合并摘要的长度上限（tokens）
    max_summary_tokens: int = Field(default=400, ge=64, le=2048)
    merge_max_tokens: int = Field(default=800, ge=128, le=4096)
    # 专用摘要模型路径；留空表示复用当前对话模型
    model: str = ""
    # 低于该字符数不触发摘要，直接注入全文
    min_chars: int = Field(default=4000, ge=0)
    # 摘要中保留原文指针（文件名 + 段落区间），便于按需回查原文
    keep_pointer: bool = True


class AutomationConfig(BaseModel):
    """浏览器自动化配置（任务四）。

    安全约定：``enabled`` 只有在 ``safety_accepted`` 为真时才允许为真
    （在 _check_safety 校验器里强制），并且记录协议版本与同意时间以便审计。
    """

    enabled: bool = False
    # ---------- 安全协议 ----------
    safety_accepted: bool = False
    safety_version: str = "1.0"
    safety_accepted_at: str = ""
    # ---------- 任务列表页 ----------
    task_list_url: str = ""
    task_list_selector: str = ""      # CSS 或 XPath（以 //、(、xpath= 开头判为 XPath）
    task_row_selector: str = ""       # 可选：行容器选择器
    next_page_selector: str = ""      # 可选：下一页按钮
    # ---------- 任务详情页 ----------
    task_detail_file_selector: str = ""
    file_extensions: List[str] = Field(
        default_factory=lambda: ["pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "txt"]
    )
    # ---------- 任务指令与生成 ----------
    instruction: str = "摘取合同的主要信息并生成财务审核意见"
    llm_temperature: float = Field(default=0.3, ge=0.0, le=1.0)
    llm_max_tokens: int = Field(default=512, ge=64, le=4096)
    # ---------- 运行参数 ----------
    max_tasks: int = Field(default=20, ge=1, le=500)
    page_timeout_ms: int = Field(default=30000, ge=3000, le=300000)
    wait_network_idle: bool = True
    retries: int = Field(default=1, ge=0, le=5)
    download_dir: str = ""            # 空 = 系统临时目录下 echodesk_downloads
    # ---------- 去重与登录态 ----------
    dedupe: bool = True
    processed_file: str = "automation_processed.json"
    login_state_file: str = "browser_state.json"
    manual_login_wait_seconds: int = Field(default=180, ge=0, le=3600)
    # ---------- 浏览器 ----------
    headless: bool = False            # 固定有头模式：用户可见浏览器操作过程
    browser_channel: str = "msedge"
    # ---------- 结果与审计 ----------
    save_results: bool = True
    results_dir: str = "automation_results"
    audit_log_file: str = "automation_log.jsonl"
    # ---------- 定时触发（4.4） ----------
    schedule_enabled: bool = False
    schedule_times: List[str] = Field(default_factory=lambda: ["09:00"])
    start_with_windows: bool = False

    @field_validator("file_extensions", "schedule_times", mode="before")
    @classmethod
    def _split_list(cls, value):
        """前端表单里这两项以逗号分隔的字符串提交，这里统一拆成列表。"""
        if isinstance(value, str):
            items = [item.strip() for item in value.replace("，", ",").split(",")]
            return [item for item in items if item]
        return value

    @field_validator("file_extensions")
    @classmethod
    def _normalize_ext(cls, value: List[str]) -> List[str]:
        """后缀统一为小写、去掉前导点，便于大小写不敏感比较。"""
        return [str(item).strip().lstrip(".").lower() for item in value if str(item).strip()]

    @field_validator("schedule_times")
    @classmethod
    def _normalize_times(cls, value: List[str]) -> List[str]:
        """校验 HH:MM 格式并补零为两位小时，非法项直接剔除（避免定时器崩溃）。"""
        import re as _re
        result = []
        for item in value:
            text = str(item).strip()
            match = _re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
            if match:
                result.append(f"{int(match.group(1)):02d}:{match.group(2)}")
        return result

    @model_validator(mode="after")
    def _check_safety(self) -> "AutomationConfig":
        """安全协议是自动化的前置条件：未勾选时强制关闭开关。"""
        if self.enabled and not self.safety_accepted:
            print(
                "⚠️ 自动化开关需要先勾选安全协议，已自动关闭（automation.enabled=False）",
                file=sys.stderr, flush=True,
            )
            self.enabled = False
        if self.headless:
            # 任务书 4.5：仅使用有头模式，用户可见浏览器操作；这里强制纠正
            print("⚠️ 自动化仅支持有头模式，已忽略 headless=True", file=sys.stderr, flush=True)
            self.headless = False
        return self


class MarketplaceConfig(BaseModel):
    """技能/插件市场配置（任务五 5.6）。"""

    enabled: bool = True
    github_token: str = ""                                  # 可选：提高 GitHub API 限额
    cache_ttl_hours: int = Field(default=6, ge=1, le=168)
    skill_topic: str = "echodesk-plugin"                    # 统一 topic，不混用其它标签
    auto_update_check: bool = True
    cache_file: str = "market_cache.json"
    per_page: int = Field(default=20, ge=1, le=100)
    install_dir: str = "skills"
    registry_file: str = "skills_registry.json"
    api_base: str = "https://api.github.com"


class Config(BaseModel):
    """应用配置，字段与 config.json 一一对应。"""

    model_path: str = "Qwen3.5-0.8B-Q4_K_M.gguf"
    model_repo: str = "unsloth/Qwen3.5-0.8B-GGUF"
    hf_endpoint: str = "https://hf-mirror.com"
    default_mode: str = "general"
    # 请求的上下文窗口：默认 8192（v0.1.0 为 4096，合同类长文档严重不足）
    context_length: int = 8192
    # 上下文窗口配置上限：校验用，任何来源的取值都不会超过它
    max_context: int = 32768
    # 自动按所选模型的训练窗口收敛（不超出模型训练长度）
    auto_context: bool = True
    # 模型训练窗口注册表（文件名关键词 → tokens）；运行时用 GGUF 元数据校准
    model_context_windows: Dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_MODEL_CONTEXT_WINDOWS))
    # 长文档分段摘要
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    # 浏览器自动化（任务四）
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    # 技能/插件市场（任务五）
    marketplace: MarketplaceConfig = Field(default_factory=MarketplaceConfig)
    max_history: int = 10
    search_default: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    max_tokens: int = 1024
    search_results: int = 3
    # 文件解析字符上限；0 表示不限制（长文档交由分段摘要处理，避免直接截断丢信息）
    file_max_chars: int = 200000
    theme: str = "dark"
    warmup: bool = True
    # 推理性能：0 表示按本机 CPU 核数自动设置
    n_threads: int = 0
    n_batch: int = 512
    # GPU 加速层数：0 = 纯 CPU（有 NVIDIA 显卡可调大，如 99=全部）
    n_gpu_layers: int = 0
    # 会话持久化文件（空字符串表示关闭自动保存）
    session_file: str = "session.json"
    # 模型档案注册表：别名 -> {path, repo, context_length, temperature}
    models: Dict[str, Dict[str, Any]] = {}
    # 扩展上下文后生成明显变慢时，在回复末尾给出提示
    speed_hint: bool = True
    speed_min_tps: float = 3.0

    @model_validator(mode="after")
    def _clamp_context(self) -> "Config":
        """上下文窗口上限校验：任何来源的取值都钳制到 [MIN_CONTEXT, max_context]。"""
        if self.max_context < MIN_CONTEXT:
            raise ValueError(f"max_context 不得小于 {MIN_CONTEXT}")
        original = self.context_length
        self.context_length = max(MIN_CONTEXT, min(int(self.context_length), int(self.max_context)))
        if self.context_length != original:
            print(
                f"⚠️ context_length={original} 超出允许范围，已钳制为 {self.context_length}"
                f"（上限 max_context={self.max_context}）",
                file=sys.stderr, flush=True,
            )
        return self

    def context_for_model(self, model_path: str, train_context: int = 0, requested: Optional[int] = None) -> int:
        """按"所选模型"决定实际使用的上下文窗口。

        取以下三者的最小值（下限 MIN_CONTEXT）：
          1. 配置请求的 context_length（可用 requested 覆盖，如模型档案自带的值）；
          2. 模型训练窗口（GGUF 元数据 train_context，或 model_context_windows 注册表）；
          3. 配置上限 max_context。

        auto_context=False 时忽略第 2 项（用户明确要求按配置值强制开窗）。
        """
        window = self.lookup_train_context(model_path) or int(train_context or 0)
        limit = int(self.max_context)
        want = int(self.context_length if requested is None else requested)
        if self.auto_context and window > 0:
            limit = min(limit, window)
        return max(MIN_CONTEXT, min(want, limit))

    def lookup_train_context(self, model_path: str) -> int:
        """从 model_context_windows 注册表按文件名关键词匹配训练窗口。"""
        base = os.path.basename(model_path or "").lower()
        best = 0
        for key, value in (self.model_context_windows or {}).items():
            if key and key.lower() in base:
                best = max(best, int(value))
        return best

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        """按优先级查找配置文件，并叠加用户补丁（config.user.json 同目录优先）。"""
        base_path = cls._find(path)
        if base_path is None:
            raise FileNotFoundError(f"找不到配置文件: {path}")
        with open(base_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 用户补丁覆盖层：与基础配置同目录的 config.user.json，字段优先
        user_path = os.path.join(os.path.dirname(base_path), "config.user.json")
        if os.path.exists(user_path):
            with open(user_path, "r", encoding="utf-8") as f:
                user = json.load(f)
            data.update(user)

        return cls(**data)

    def save(self, path: str = "config.json") -> None:
        """将当前配置写回基础配置 JSON 文件（不触碰 config.user.json 覆盖层）。"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=4, ensure_ascii=False)

    @staticmethod
    def _find(path: str) -> str | None:
        """按优先级查找配置文件的真实路径。"""
        candidates = [os.path.join(os.getcwd(), path)]
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(os.path.dirname(sys.executable), path))
        if getattr(sys, "_MEIPASS", False):
            candidates.append(os.path.join(sys._MEIPASS, path))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), path))
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return None
