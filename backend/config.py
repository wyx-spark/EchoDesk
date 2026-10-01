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


def program_dir() -> str:
    """程序本体所在目录（打包后即 exe 所在目录；便携版布局下为 ``...\\app``）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_data_root() -> str:
    """用户数据根目录（修复七）。

    发布物采用便携版三层结构：``EchoDesk-v0.2.1\\app\\backend\\EchoDeskBackend.exe``，
    其中 ``app\\`` 是程序本体，外层才是用户能看到、能备份的目录
    （快捷方式、``ui_state.json``、``snippets.json``、``results\\``）。

    判定顺序（与前端 ``ShortcutService.UserDataDirectory`` 保持一致）：
    1. 环境变量 ``ECHODESK_DATA``；
    2. 后端目录的父目录名为 ``app`` → 再上一层（便携版布局）；
    3. 后端目录的父目录（源码运行 = 仓库根；单层部署 = 程序目录）。
    """
    override = (os.environ.get("ECHODESK_DATA") or "").strip()
    if override:
        try:
            return os.path.abspath(os.path.expanduser(override))
        except Exception:  # noqa: BLE001 - 环境变量异常时退回自动判定
            pass
    if getattr(sys, "frozen", False):
        backend_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        backend_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(backend_dir)
    if os.path.basename(parent).lower() == "app":
        return os.path.dirname(parent)
    return parent


def program_root() -> str:
    """程序根目录（新增三：results的默认位置）。

    保留函数名以兼容既有调用方；实现改为走 :func:`user_data_root`，
    这样"保存位置留空"的结果目录会落在便携版外层，而不是埋进 ``app\\``。
    """
    return user_data_root()

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

    v0.2.1 综合优化（任务四）新增：**过程反馈、心跳、超时与中断**。
    小模型 + 纯 CPU 下长文档摘要可能跑十几分钟，此前只把"第 i/N 段"打到 stderr，
    前端对话框里什么都看不到，用户无法区分"正在跑"与"已经卡死"。
    下面这组参数决定"多久没动静算异常"以及"单段太慢怎么办"。
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

    # ---------- 任务四：过程反馈 ----------
    # 是否向前端发进度事件（summary_progress）。关掉只影响"看不看得见"，不影响摘要结果；
    # 无人值守/脚本化调用时可以关掉以减少协议噪音。
    progress_events: bool = True
    # 心跳间隔（秒）：单段处理中每隔这么久至少发一次状态，避免长时间无任何输出。
    # 下限 1 秒是防止有人配成 0 把协议刷爆。
    heartbeat_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    # 单段超过这么多秒仍未完成，就在进度里标记为"异常缓慢"并给出提示
    # （只提示、不打断：慢不等于故障，任务书要求区分这两种情况）。
    slow_chunk_seconds: int = Field(default=60, ge=10, le=3600)
    # 单段超时（秒）；0 = 不限。判定依据是"该段从开始到现在"的墙钟时间。
    chunk_timeout_seconds: int = Field(default=0, ge=0, le=7200)
    # 单段超时后的行为：skip（跳过该段并继续，默认）/ retry（重试一次）/ abort（中止整个摘要）
    chunk_timeout_action: str = "skip"

    @field_validator("chunk_timeout_action")
    @classmethod
    def _check_timeout_action(cls, value: str) -> str:
        """校验超时行为，非法值回落到默认的 skip（配置文件可能被手改坏）。"""
        text = str(value or "").strip().lower()
        return text if text in ("skip", "retry", "abort") else "skip"


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
    # 登录态（v0.2.1 自动化优化 任务一）：主机制改为 Playwright 的**持久化用户数据目录**
    # （``launch_persistent_context``），Cookie / localStorage / 会话都由浏览器自己写进
    # ``login_state_dir`` 指向的目录，下次启动自动复用。
    #
    # ``login_state_file`` 因此退化为"迁移与备份用的 storage_state 文件"：首次使用新目录
    # 时若它存在，会把里面的 Cookie 导入一次（老用户不用重新登录）；每次会话结束仍会
    # 导出一次，便于用户备份或换机迁移。两个文件都**只在本机**，不含任何上传行为。
    login_state_file: str = "browser_state.json"
    login_state_dir: str = ""          # 空 = 与 login_state_file 同级的 browser_profile 目录
    # 登录态判定：优先用"登录后才出现的元素"（最可靠）；没配就用 URL 关键词兜底。
    login_check_selector: str = ""
    login_url_keywords: List[str] = Field(
        default_factory=lambda: ["login", "signin", "sign-in", "logon", "sso", "passport", "cas", "auth"]
    )
    manual_login_wait_seconds: int = Field(default=180, ge=0, le=3600)
    # ---------- 浏览器 ----------
    headless: bool = False            # 固定有头模式：用户可见浏览器操作过程
    browser_channel: str = "msedge"
    # ---------- 结果与审计 ----------
    save_results: bool = True
    # 结果目录（新增三）：结果根目录\YYYY-MM-DD\任务名\（同名自动加 -1、-2）
    # v0.2.1 发布任务书「全局要求一」：程序自动生成的目录名一律英文，
    # 因此 results_root_name 的默认值由「批量处理结果」改为 results，
    # task_name 的默认值由「自动化任务」改为 auto-task（结果文件名同理，
    # 见 automation.py 的 report_path）。
    results_root_name: str = "results"       # 顶层文件夹名，可由用户自定义
    results_location: str = ""               # 空 = 程序根目录；可指定其他位置
    task_name: str = "auto-task"             # 第三层目录名：本次运行的任务名称
    audit_log_file: str = "automation_log.jsonl"
    # ---------- 定时触发（4.4） ----------
    schedule_enabled: bool = False
    schedule_times: List[str] = Field(default_factory=lambda: ["09:00"])
    start_with_windows: bool = False

    @field_validator("file_extensions", "schedule_times", "login_url_keywords", mode="before")
    @classmethod
    def _split_list(cls, value):
        """前端表单里这几项以逗号分隔的字符串提交，这里统一拆成列表。"""
        if isinstance(value, str):
            items = [item.strip() for item in value.replace("，", ",").split(",")]
            return [item for item in items if item]
        return value

    @field_validator("file_extensions")
    @classmethod
    def _normalize_ext(cls, value: List[str]) -> List[str]:
        """后缀统一为小写、去掉前导点，便于大小写不敏感比较。"""
        return [str(item).strip().lstrip(".").lower() for item in value if str(item).strip()]

    @field_validator("login_url_keywords")
    @classmethod
    def _normalize_keywords(cls, value: List[str]) -> List[str]:
        """登录页 URL 关键词统一小写、去重（URL 匹配本身不区分大小写）。"""
        out: List[str] = []
        for item in value:
            word = str(item).strip().lower()
            if word and word not in out:
                out.append(word)
        return out

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


class ModelHubConfig(BaseModel):
    """模型选择栏配置（任务书 新增四）。

    数据源可在「自动 / 镜像 / 官方」之间切换；``hf_token`` 仅在下载 Gated 模型时需要，
    建议放在 config.user.json（已被 .gitignore 忽略）而不是基础配置里。
    """

    enabled: bool = True
    # auto：优先镜像站（国内通常更快），不可达时回退官方站
    source: str = "auto"                    # auto | mirror | official
    hf_token: str = ""                      # Gated 模型所需，留空则读 HF_TOKEN 环境变量
    models_dir: str = "models"              # 模型库目录（相对后端工作目录）
    registry_file: str = "models_registry.json"
    # 下载完整后比对大小；拿得到 sha256 且文件 ≤2GB 时再比对哈希
    verify_sha256: bool = True
    download_retries: int = Field(default=3, ge=1, le=10)
    search_limit: int = Field(default=20, ge=1, le=100)

    # ---------- v0.2.1 模型市场优化 ----------
    # 自动加载全部结果时的安全上限（任务二）：三个闸门任一触发即停并告知
    # "已加载 N 条，还有更多"。关键词过宽（如空关键词 = 整个模型库）时不至于失控。
    max_items: int = Field(default=5000, ge=20, le=20000)
    max_requests: int = Field(default=8, ge=1, le=50)
    max_seconds: float = Field(default=90.0, ge=5.0, le=600.0)
    # 本地分页每页条数（任务三：默认 20，界面可选 20/50/100）
    page_size: int = Field(default=20, ge=5, le=200)
    # 搜索结果磁盘缓存（任务二）：留空 = <用户数据目录>/cache/model_hub。
    # 0 表示每次搜索都重新拉取（缓存立即过期）。
    search_cache_dir: str = ""
    search_cache_ttl_hours: int = Field(default=6, ge=0, le=168)


class ReasoningConfig(BaseModel):
    """思考内容分离配置（v0.2.1 模型思考内容分离）。

    为什么配置项只有这几个（决策依据见
    ``records/v0.2.1-模型思考内容分离记录.md``）：

    * ``enabled`` —— 关掉后退回旧行为（思考标签原样留在回答里）。留这个开关是为了
      "出问题时能一键回到没改之前"，而不是鼓励关掉；
    * ``persist`` —— 思考内容是否写进会话历史。默认写：任务书任务五要求
      "重新加载含思考内容的历史会话，确认区分正确"，不持久化就没法满足；
    * ``history_max_chars`` —— 历史里每条思考内容的保留上限。思考往往比答案长得多，
      不设上限时 session.json 会被单个长思考撑大；超限的部分截断并加省略标记；
    * ``tags`` —— 追加自定义标签对（形如 ``[["[[think]]", "[[/think]]"]]``）。
      内置已覆盖常见形态（见 ``reasoning.DEFAULT_TAG_NAMES``），这里只做追加，
      避免一个笔误把内置标签一起弄丢。
    """

    enabled: bool = True
    persist: bool = True
    history_max_chars: int = Field(default=8000, ge=0, le=200000)
    tags: List[List[str]] = Field(default_factory=list)


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
    # ---------- 本地插件接口（任务二·补 2B） ----------
    # 开发中的插件目录：放进去即被识别，加载更宽松，不写入正式注册表
    dev_dir: str = "skills_dev"
    # 显式本地插件清单（可指定任意路径、别名、启用状态、加载顺序、备注）
    local_manifest: str = "skills_local.json"
    # 开发模式：详细日志 + 热重载
    dev_mode: bool = False
    # 修复一：开发者模式开关是否由用户显式设置过。
    # False 时按"skills_dev/ 里有插件就自动启用"处理（首次开箱即用），
    # True 时完全以 dev_mode 为准 —— 否则"关掉开关"会因为目录里确实有插件而被
    # 自动重新打开，表现为"开发者模式一旦开启就关不掉"。
    dev_mode_explicit: bool = False
    # 开发日志（开发者 Tab 内嵌展示最后 N 行）
    dev_log_file: str = "skills_dev.log"
    # 正式加载只接受注册表中 verified=true 的技能（任务二 2.4 加载白名单）
    require_verified: bool = True


class UpdateConfig(BaseModel):
    """自动更新（任务书 任务五 方案 A：只检查与提示，不自动下载替换）。

    方案 B/C（自动下载替换、增量清单）本版不实现，理由见 records/v0.2.1-研发记录.md。
    """

    enabled: bool = True
    # owner/name；开源发布后由维护者改为真实仓库。留空则跳过检查。
    repo: str = "VoidEntropiaStudio/EchoDesk"
    api_base: str = "https://api.github.com"
    check_on_startup: bool = True
    check_interval_hours: int = Field(default=24, ge=1, le=720)
    include_prerelease: bool = False
    # 企业环境可关掉自动检查（任务书 5.2「允许通过配置禁用自动检查」）
    last_check: str = ""
    last_seen_version: str = ""


class Config(BaseModel):
    """应用配置，字段与 config.json 一一对应。"""

    model_path: str = "Qwen3-0.6B-Q6_K.gguf"
    model_repo: str = "unsloth/Qwen3-0.6B-GGUF"
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
    # 模型选择栏（新增四）
    model_hub: ModelHubConfig = Field(default_factory=ModelHubConfig)
    # 思考内容分离（v0.2.1 模型思考内容分离）
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    # 自动更新（任务五 方案 A）
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    # 界面语言（任务三 3.4）：auto = 首次启动按系统区域自动选择；
    # 也可写 "zh-Hans" / "en" / "zh-Hant" / "ja" …；前端 ui_state.json 同名字段优先。
    language: str = "auto"
    max_history: int = 10
    search_default: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    max_tokens: int = 1024
    search_results: int = 3
    # ---------- 修复四：生成参数预设与上下文管理 ----------
    # 当前预设档位：light / balanced / quality（/preset 切换，决定下面三项的取值）
    preset: str = "balanced"
    # top_p 与重复惩罚：小模型"复读"和"跑题"主要由这两项控制
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    repeat_penalty: float = Field(default=1.1, ge=0.5, le=2.0)
    # 上下文超限时的处理方式：
    #   summarize —— 把早期对话交给模型压缩成摘要（默认，信息保留最好）
    #   slide     —— 直接丢弃最早的历史（滑动窗口，最快，不做额外推理）
    context_mode: str = "summarize"
    # 是否缓存长文档分段摘要（同一文件二次上传不重算，方向八）
    summary_cache: bool = True
    # 提问过于模糊时先追问而不是硬答（方向七）；关掉即回到"无论如何都作答"
    clarify_vague: bool = True
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
        """按优先级查找配置文件，并叠加用户补丁（config.user.json 同目录优先）。

        **编码**（v0.2.1 模型思考内容分离，界面探针实测出来的缺陷）：一律用
        ``utf-8-sig`` 读。Windows 上记事本保存 UTF-8 会带 BOM，而普通 ``utf-8``
        解码会直接抛 ``JSONDecodeError: Unexpected UTF-8 BOM`` —— 后端在启动阶段
        就崩掉、程序随之退出，用户看到的是"后端意外退出"，完全看不出是配置文件
        的 BOM 造成的。协议层早就在兼容 stdin 的 BOM（见 main.py 的读取循环），
        配置文件这一侧同样要兼容。

        **容错**：基础配置读不出来仍然抛异常（没有配置无法工作）；``config.user.json``
        是**可选覆盖层**，格式坏掉时只告警并忽略它，绝不因此让整个后端起不来 ——
        用户手改一个可选文件写出语法错误，代价不该是程序打不开。
        """
        base_path = cls._find(path)
        if base_path is None:
            raise FileNotFoundError(f"找不到配置文件: {path}")
        with open(base_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)

        # 用户补丁覆盖层：与基础配置同目录的 config.user.json，字段优先
        user_path = os.path.join(os.path.dirname(base_path), "config.user.json")
        if os.path.exists(user_path):
            try:
                with open(user_path, "r", encoding="utf-8-sig") as f:
                    user = json.load(f)
                if isinstance(user, dict):
                    data.update(user)
                else:
                    print("⚠️ config.user.json 顶层不是对象，已忽略该覆盖层",
                          file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 - 覆盖层坏了不能拖垮启动
                print(f"⚠️ config.user.json 解析失败，已忽略该覆盖层：{exc}",
                      file=sys.stderr, flush=True)

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
