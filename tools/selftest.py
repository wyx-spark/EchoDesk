"""EchoDesk 后端自检（单元级）：不需要加载模型即可跑完。

覆盖范围：
  1. 配置层：上下文窗口上限校验、按模型收敛窗口、安全协议强制、后缀与定时规范化；
  2. GGUF 元数据：真实模型文件的训练窗口/KV Cache 估算（无模型时跳过）；
  3. 分段摘要：分段重叠与边界切分、map-reduce 调用链、原文指针；
  4. 对话管理：长文档摘要优先注入、会话保存/恢复字段；
  5. 自动化：安全协议前置校验、审计日志脱敏；
  6. 技能市场：环境变量清洗（缺模块时跳过）。

用法：python tools\\selftest.py
退出码：0 = 全部通过，1 = 存在失败项（可用于 CI 或发布前自检）。
"""
import os
import sys
import traceback
from typing import List, Tuple

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 1. 配置层

def test_config() -> None:
    section("配置层")
    import config

    # 上限校验：超出 max_context 会被钳制
    cfg = config.Config(context_length=999999, max_context=32768)
    check("context_length 超上限被钳制", cfg.context_length == 32768, f"得到 {cfg.context_length}")

    # 下限保护
    cfg = config.Config(context_length=8)
    check("context_length 下限保护", cfg.context_length == config.MIN_CONTEXT, f"得到 {cfg.context_length}")

    # 按模型收敛：取配置值/训练窗口/上限的最小值
    cfg = config.Config(context_length=8192, max_context=32768, auto_context=True)
    check("按训练窗口收敛（窗口更小）",
          cfg.context_for_model("llama-3-8b-q4.gguf", 8192) == 8192,
          f"得到 {cfg.context_for_model('llama-3-8b-q4.gguf', 8192)}")
    check("请求值更小时取请求值",
          cfg.context_for_model("qwen3-0.6b.gguf", 32768) == 8192,
          f"得到 {cfg.context_for_model('qwen3-0.6b.gguf', 32768)}")
    cfg_no_auto = config.Config(context_length=16384, auto_context=False)
    check("auto_context=False 时忽略训练窗口",
          cfg_no_auto.context_for_model("llama-3.gguf", 8192) == 16384,
          f"得到 {cfg_no_auto.context_for_model('llama-3.gguf', 8192)}")

    # 配置文件编码与容错（v0.2.1 模型思考内容分离：界面探针实测出来的缺陷）
    # 现象：Windows 记事本保存的 UTF-8 带 BOM，旧实现用 utf-8 读 → JSONDecodeError
    # → 后端启动即崩、程序随之退出。这里把它固化成回归断言。
    import json as _json
    import os as _os
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        base = {"model_path": "bom-model.gguf", "context_length": 4096}
        base_path = _os.path.join(tmp, "config.json")
        with open(base_path, "w", encoding="utf-8-sig", newline="\n") as f:
            _json.dump(base, f, ensure_ascii=False)
        loaded = config.Config.load(base_path)
        check("带 BOM 的 config.json 能正常解析", loaded.model_path == "bom-model.gguf",
              f"得到 {loaded.model_path!r}")
        check("带 BOM 时其余字段也生效", loaded.context_length == 4096,
              f"得到 {loaded.context_length}")

        # 覆盖层带 BOM：应当生效而不是被忽略
        with open(_os.path.join(tmp, "config.user.json"), "w", encoding="utf-8-sig", newline="\n") as f:
            _json.dump({"model_path": "patched.gguf"}, f)
        check("带 BOM 的 config.user.json 覆盖层生效",
              config.Config.load(base_path).model_path == "patched.gguf")

        # 覆盖层写坏：只忽略覆盖层，不能连基础配置一起抛
        with open(_os.path.join(tmp, "config.user.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write("{ 这不是 JSON }")
        fallback = config.Config.load(base_path)
        check("损坏的 config.user.json 被忽略而不是让启动失败",
              fallback.model_path == "bom-model.gguf", f"得到 {fallback.model_path!r}")

    # ---------- 随包分发的配置文件必须与配置模型对齐 ----------
    #
    # 为什么要断言这一条（v0.2.1 优化已知问题-2 打包时踩到）：新增 `config.files`
    # 段时只改了 Config 模型，忘了往 backend/config.json 与 deploy/config.json 里
    # 写默认值 —— 功能靠 pydantic 默认值照常工作，单元测试也全绿，
    # 但用户打开 config.json 根本看不到这个段，也就无从配置。
    # 这是"只有对比配置文件与配置模型才能发现"的一类缺陷，所以固化成断言。
    import json as _json2
    import os as _os2
    root = _os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__)))
    model = config.Config()
    sections = [name for name, field in type(model).model_fields.items()
                if hasattr(getattr(model, name), "model_fields")]
    for rel in ("backend/config.json", "deploy/config.json"):
        path = _os2.path.join(root, rel.replace("/", _os2.sep))
        if not _os2.path.exists(path):
            check(f"{rel} 存在", False, path)
            continue
        with open(path, "r", encoding="utf-8-sig") as f:
            data = _json2.load(f)
        missing = [name for name in sections if name not in data]
        check(f"{rel} 含全部 {len(sections)} 个配置段", not missing,
              f"缺少 {missing}")
        # 段内字段也要在：否则"看得见段、改不了项"，同样等于没有
        for name in sections:
            if name not in data or not isinstance(data[name], dict):
                continue
            expected = set(getattr(model, name).model_fields)
            absent = sorted(expected - set(data[name]))
            check(f"{rel} 的 {name} 段含全部字段", not absent, f"缺少 {absent}")

    # 安全协议：未勾选时强制关闭自动化
    auto = config.AutomationConfig(enabled=True, safety_accepted=False)
    check("未勾选安全协议 → 自动化开关被强制关闭", auto.enabled is False, f"得到 {auto.enabled}")
    auto = config.AutomationConfig(enabled=True, safety_accepted=True)
    check("已勾选安全协议 → 开关保持开启", auto.enabled is True)

    # 仅允许有头模式
    auto = config.AutomationConfig(headless=True)
    check("headless 被强制纠正为 False", auto.headless is False)

    # 后缀规范化：大写、前导点、中文逗号
    auto = config.AutomationConfig(file_extensions="PDF, .Docx ，xlsx , ")
    check("文件后缀规范化", auto.file_extensions == ["pdf", "docx", "xlsx"], f"得到 {auto.file_extensions}")

    # 定时时间规范化与非法项剔除
    auto = config.AutomationConfig(schedule_times="7:30, 18:05, 25:00, abc")
    check("定时时间补零并剔除非法项", auto.schedule_times == ["07:30", "18:05"], f"得到 {auto.schedule_times}")

    # 摘要配置默认值
    cfg = config.Config()
    check("摘要触发比例默认 0.5", abs(cfg.summarize.trigger_ratio - 0.5) < 1e-9)
    check("默认上下文窗口 8192", cfg.context_length == 8192, f"得到 {cfg.context_length}")

    # 新增三：results目录的配置默认值与程序根目录解析
    check("结果顶层文件夹默认「results」", auto.results_root_name == "results",
          f"得到 {auto.results_root_name}")
    check("结果保存位置默认为空（= 程序根目录）", auto.results_location == "", f"得到 {auto.results_location}")
    check("任务名称有默认值", bool(auto.task_name), f"得到 {auto.task_name}")
    check("program_root() 指向存在的目录", os.path.isdir(config.program_root()),
          f"得到 {config.program_root()}")


# --------------------------------------------- 1b. 结果文件纯文本化（新增三）

def test_plaintext() -> None:
    section("Markdown → 纯文本")
    import plaintext

    md = (
        "# 标题\n\n**金额** 12,345 元。\n\n"
        "| 项目 | 金额 |\n|------|------|\n| 货款 | 10000 |\n\n- 要点一\n"
    )
    out = plaintext.markdown_to_plain(md)
    check("剥离 # 标题标记", not any(l.lstrip().startswith("#") for l in out.splitlines()))
    check("剥离 ** 粗体标记", "**" not in out)
    check("剥离表格竖线", "|" not in out)
    check("保留正文内容", "12,345 元" in out and "要点一" in out)
    check("结果不含 Markdown 标记", plaintext.contains_markdown(out) is False)
    check("能识别输入含 Markdown", plaintext.contains_markdown(md) is True)
    check("空输入返回空串", plaintext.markdown_to_plain("") == "")


# ------------------------------------------------------- 2. GGUF 元数据

def test_gguf() -> None:
    section("GGUF 元数据")
    import gguf_meta

    meta = gguf_meta.read_metadata("__not_exists__.gguf")
    check("文件不存在时返回空字典", meta == {})

    # 找一个真实模型文件（开发机或发布物里都可能存在）
    # 说明：不写死文件名 —— 默认模型会换（v0.2.1 就从 Qwen3.5-0.8B 换成了
    # Qwen3-0.6B），写死等于每次换模型都得改测试。发布目录也从 v0.2.1 起
    # 把程序本体放进 app\ 子目录，这里一并按当前布局找。
    dirs = [os.path.join(BACKEND, "models")]
    publish = os.path.join(BACKEND, "..", "publish")
    if os.path.isdir(publish):
        for name in sorted(os.listdir(publish), reverse=True):
            if name.startswith("EchoDesk-v"):
                dirs.append(os.path.join(publish, name, "app", "backend", "models"))
    model = None
    for folder in dirs:
        if not os.path.isdir(folder):
            continue
        found = sorted(f for f in os.listdir(folder) if f.lower().endswith(".gguf"))
        if found:
            model = os.path.join(folder, found[0])
            break
    if not model:
        check("真实模型元数据解析（未找到模型文件，跳过）", True, "skipped")
        return

    window = gguf_meta.resolve_context_window(model, 0)
    check("读到模型训练窗口", window > 0, f"得到 {window}")
    kv = gguf_meta.estimate_kv_cache_bytes(model, 8192)
    check("KV Cache 估算为正数", bool(kv and kv > 0), f"得到 {kv}")
    info = gguf_meta.describe_model(model, 8192)
    check("架构名可读", isinstance(info.get("architecture"), str) and info["architecture"] != "未知",
          f"得到 {info.get('architecture')}")
    # 8192 窗口的 KV 占用应远小于 1GB（默认模型是 1B 以下的小模型）
    check("KV Cache 估算在合理量级", bool(kv and kv < 1024 ** 3), f"得到 {kv}")


# --------------------------------------------------------- 3. 分段摘要

class _FakeEngine:
    """假引擎：不加载模型，记录调用并按需返回可预测的摘要文本。"""

    def __init__(self, model_path: str = "fake.gguf") -> None:
        self.model_path = model_path
        self.calls: List[dict] = []

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 3)

    def chat_complete(self, messages, temperature=0.7, max_tokens=1024) -> str:
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        content = messages[-1]["content"]
        return f"摘要{len(self.calls)}: " + content[:60].replace("\n", " ")


def test_summarizer() -> None:
    section("分段摘要")
    from config import SummarizeConfig
    from summarizer import DocumentSummarizer, split_chunks

    # 分段：覆盖全文、段间有重叠、不产生空段
    text = "".join(f"第{i}段内容。数字 {i} 金额 {i * 100} 元。\n" for i in range(200))
    chunks = split_chunks(text, 300, 50)
    check("分段覆盖全文末尾", chunks[-1][1] == len(text), f"末段结束 {chunks[-1][1]} / {len(text)}")
    check("分段起点严格递增", all(chunks[i][0] < chunks[i + 1][0] for i in range(len(chunks) - 1)))
    check("段间存在重叠", any(chunks[i + 1][0] < chunks[i][1] for i in range(len(chunks) - 1)))
    check("无空段", all(a < b for a, b, _ in chunks))
    check("空文本返回空列表", split_chunks("", 300, 50) == [])

    # 触发判断
    cfg = SummarizeConfig(min_chars=100, trigger_ratio=0.5)
    engine = _FakeEngine()
    summ = DocumentSummarizer(engine, cfg)
    long_text = "内容" * 500
    check("超过窗口比例时触发摘要", summ.needs_summary(long_text, 512) is True)
    check("远小于窗口时不触发", summ.needs_summary("短文本", 8192) is False)
    check("低于 min_chars 不触发", summ.needs_summary("短" * 20, 64) is False)

    # 完整 map-reduce：应产生多段摘要 + 归并（文本要足够长才能切出多段）
    long_text = "".join(f"第{i}条：金额 {i * 137} 元，日期 2026-01-{(i % 28) + 1:02d}。\n" for i in range(400))
    check("测试文本长度足以分段", len(long_text) > cfg.chunk_chars * 2, f"得到 {len(long_text)} 字符")
    result = summ.summarize(long_text, source_name="长文档.docx", force=True)
    check("摘要结果标记为已摘要", result.summarized is True)
    check("分段数 > 1", result.chunk_count > 1, f"得到 {result.chunk_count}")
    check("输出含原文指针", "【原文指针】" in result.text)
    check("输出含长文档摘要标题", "【长文档摘要】" in result.text)
    check("保留了源文件名", "长文档.docx" in result.text)
    check("调用了模型（map 至少一次）", len(engine.calls) >= result.chunk_count,
          f"调用 {len(engine.calls)} 次，段数 {result.chunk_count}")
    check("原文指针记录了各段字符区间", result.pointer.startswith("chars=") and "-" in result.pointer)

    # 关闭摘要时原样返回
    off = DocumentSummarizer(_FakeEngine(), SummarizeConfig(enabled=False))
    plain = off.summarize(long_text, force=True)
    check("关闭摘要时原样返回", plain.summarized is False and plain.text == long_text)


# --------------------------------------------------------- 4. 对话管理

def test_conversation() -> None:
    section("对话管理")
    from conversation import ConversationManager

    conv = ConversationManager(max_history=5, default_mode="general")

    # 未设置摘要时注入全文
    conv.set_file("合同.docx", "合同正文内容" * 50)
    msgs = conv.build_messages("这份合同写了什么", include_file=True)
    check("无摘要时注入文件全文", "合同正文内容" in msgs[-1]["content"])

    # 设置摘要后优先注入摘要
    conv.set_file_summary("【长文档摘要】合同.docx\n关键条款摘要", {"chunks": 3, "origin_chars": 300})
    msgs = conv.build_messages("这份合同写了什么", include_file=True)
    check("有摘要时优先注入摘要", "关键条款摘要" in msgs[-1]["content"])
    check("有摘要时不再注入全文", "合同正文内容" not in msgs[-1]["content"])
    check("系统提示告知摘要已就绪", "长文档摘要已就绪" in msgs[0]["content"])

    # 换文件时清掉旧摘要，避免张冠李戴
    conv.set_file("另一份.docx", "另一份内容")
    check("换文件清空旧摘要", conv.file_summary == "" and conv.file_summary_meta == {})

    # 会话持久化包含摘要字段
    import json
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "echodesk_selftest_session.json")
    conv.set_file_summary("摘要X", {"chunks": 2, "origin_chars": 100})
    conv.add_message("user", "你好")
    conv.save_session(path, "model.gguf")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    check("会话文件含 file_summary", data.get("file_summary") == "摘要X")
    check("会话文件含 file_summary_meta", data.get("file_summary_meta", {}).get("chunks") == 2)

    conv2 = ConversationManager()
    ok, prev = conv2.load_session(path)
    check("会话恢复成功", ok is True)
    check("恢复后摘要仍在", conv2.file_summary == "摘要X")
    check("恢复后记录了上次模型", prev == "model.gguf")
    os.remove(path)

    # 模式别名
    check("模式别名可切换（编号）", conv2.set_mode("2") and conv2.get_mode() == "summary")
    check("模式别名可切换（缩写）", conv2.set_mode("ct") and conv2.get_mode() == "contract")
    check("非法模式被拒绝", conv2.set_mode("nope") is False)


# ----------------------------------------------------------- 5. 自动化

def test_automation() -> None:
    section("自动化")
    try:
        import automation
    except ImportError as e:
        check("导入 automation（缺失，跳过）", True, f"skipped: {e}")
        return

    from config import AutomationConfig

    engine = automation.AutomationEngine(AutomationConfig())
    errors = engine.validate()
    check("未配置网址时校验报错", len(errors) > 0, f"得到 {errors}")

    # 未勾选安全协议时 run() 必须直接报错且不启动浏览器
    events = list(engine.run())
    kinds = [e.get("type") for e in events]
    check("未勾选安全协议时 run() 直接报错", "error" in kinds, f"事件类型 {kinds}")
    check("报错信息提到安全协议",
          any("安全协议" in str(e.get("content", "")) for e in events))

    # 脱敏：token/password/secret 的值应被替换
    redacted = automation._redact("token=abcdef123 password=hunter2 secret=xyz key=qqq 正常文本")
    check("审计脱敏替换 token 值", "abcdef123" not in redacted, f"得到 {redacted}")
    check("审计脱敏替换 password 值", "hunter2" not in redacted)
    check("审计脱敏保留普通文本", "正常文本" in redacted)
    check("审计脱敏限制长度", len(automation._redact("很长的内容" * 200, 50)) <= 60)

    # 状态接口
    status = engine.status()
    check("status() 含 running 字段", "running" in status, f"得到 {status}")


# --------------------------------------------------------- 6. 技能市场

def test_marketplace() -> None:
    section("技能市场")
    try:
        import marketplace
    except ImportError as e:
        check("导入 marketplace（缺失，跳过）", True, f"skipped: {e}")
        return

    os.environ["ECHODESK_SELFTEST_TOKEN"] = "should-be-removed"
    os.environ["ECHODESK_SELFTEST_PASSWORD"] = "should-be-removed"
    os.environ["ECHODESK_SELFTEST_PLAIN"] = "keep-me"
    from config import MarketplaceConfig

    market = marketplace.Marketplace(MarketplaceConfig())
    env = market._sanitized_env()
    check("环境变量清洗移除 TOKEN", "ECHODESK_SELFTEST_TOKEN" not in env)
    check("环境变量清洗移除 PASSWORD", "ECHODESK_SELFTEST_PASSWORD" not in env)
    check("环境变量清洗保留普通变量", env.get("ECHODESK_SELFTEST_PLAIN") == "keep-me")
    for key in ("ECHODESK_SELFTEST_TOKEN", "ECHODESK_SELFTEST_PASSWORD", "ECHODESK_SELFTEST_PLAIN"):
        os.environ.pop(key, None)

    # 仓库名校验：拒绝 URL 与非法形式，接受 owner/name
    normalize = getattr(market, "_normalize_repo", None)
    if normalize is not None:
        for bad in ("https://github.com/a/b", "a", "a/b/c", "ftp://x", ""):
            try:
                value = normalize(bad)
                rejected = not value
            except Exception:
                rejected = True
            check(f"非法仓库名被拒绝: {bad!r}", rejected)
        check("合法仓库名通过校验", bool(normalize("owner/name")) is True,
              f"得到 {normalize('owner/name')!r}")


def test_model_match() -> None:
    """仓库内模型文件匹配（download_utils 的纯函数部分，不联网）。

    v0.2.1 默认模型切换时发现：原来的评分给含 ``q4_k_m`` 的候选**无条件加 0.3**，
    于是显式配置 Q6_K 也会被解析成 Q4_K_M（下错文件、且没有任何提示）。
    这里的候选列表就是当时实测的真实仓库文件清单，作为回归夹具。
    """
    section("模型文件匹配")
    try:
        import download_utils as du
    except ImportError as e:
        check("导入 download_utils（缺失，跳过）", True, f"skipped: {e}")
        return

    check("量化代码提取", du.quant_of("Qwen3-0.6B-Q6_K.gguf") == "q6_k",
          f"得到 {du.quant_of('Qwen3-0.6B-Q6_K.gguf')!r}")
    check("IQ 量化代码提取", du.quant_of("x-IQ4_XS.gguf") == "iq4_xs",
          f"得到 {du.quant_of('x-IQ4_XS.gguf')!r}")
    check("无量化代码时返回空串", du.quant_of("Qwen3-0.6B.gguf") == "",
          f"得到 {du.quant_of('Qwen3-0.6B.gguf')!r}")
    # 回归：取主干必须幂等。模型名里的版本号带点（Qwen3.5），用 os.path.splitext
    # 会按最后一个点切，把量化代码切掉 —— 于是"提示里点名了 Q6_K"被判成未点名。
    stem = du._stem("Qwen3.5-0.8B-Q6_K.gguf")
    check("主干提取幂等", du._stem(stem) == stem, f"{stem!r} → {du._stem(stem)!r}")
    check("不带扩展名也能取出量化", du.quant_of("qwen3.5-0.8b-q6_k") == "q6_k",
          f"得到 {du.quant_of('qwen3.5-0.8b-q6_k')!r}")

    bartowski = ["Qwen_Qwen3.5-0.8B-Q4_K_M.gguf", "Qwen_Qwen3.5-0.8B-Q6_K.gguf",
                 "Qwen_Qwen3.5-0.8B-Q8_0.gguf", "mmproj-Qwen_Qwen3.5-0.8B-f16.gguf"]
    unsloth_06 = ["Qwen3-0.6B-Q4_K_M.gguf", "Qwen3-0.6B-Q5_K_M.gguf",
                  "Qwen3-0.6B-Q6_K.gguf", "Qwen3-0.6B-Q8_0.gguf"]

    # 回归：显式点名量化时必须给到那个量化（修复前这里会返回 Q4_K_M）
    picked = du.rank_model_files(bartowski, "Qwen_Qwen3.5-0.8B-Q6_K.gguf")
    check("显式 Q6_K 提示解析到 Q6_K", picked == "Qwen_Qwen3.5-0.8B-Q6_K.gguf", f"得到 {picked}")
    picked = du.rank_model_files(bartowski, "Qwen3.5-0.8B-Q6_K.gguf")
    check("带前缀差异的 Q6_K 提示解析到 Q6_K", picked == "Qwen_Qwen3.5-0.8B-Q6_K.gguf", f"得到 {picked}")
    picked = du.rank_model_files(unsloth_06, "Qwen3-0.6B-Q6_K.gguf")
    check("自身默认模型文件解析到自身", picked == "Qwen3-0.6B-Q6_K.gguf", f"得到 {picked}")

    # 未点名量化时，通用档位的偏好仍然保留（历史行为）
    picked = du.rank_model_files(unsloth_06, "Qwen3-0.6B")
    check("未点名量化时仍优先 Q4_K_M", picked == "Qwen3-0.6B-Q4_K_M.gguf", f"得到 {picked}")

    # 仓库里没有该量化时退化为最接近的一个（并仍会回报给用户确认）
    picked = du.rank_model_files(unsloth_06, "Qwen3-0.6B-Q3_K_M.gguf")
    check("量化不存在时退化为最接近的候选", picked.endswith(".gguf"), f"得到 {picked}")


def test_protocol_keys() -> None:
    """前后端协议键名交叉核对（v0.2.1 默认模型切换一轮新增）。

    **起因是一个真实缺陷**：设置页「模型档位」的下载分支读响应里的 `hub_detail`，
    而后端 `_handle_model_hub_detail` 返回的键是 `detail` —— 后端明明把完整文件清单发了过来，
    前端读错了键，于是**用户每次点档位都显示"无法获取模型详情"**（本轮实测踩到）。
    后端协议测试与 GUI 探针都发现不了它：两边各自看都是"对的"（后端返回没问题、
    前端也确实走了"失败"分支）；只有把两边放在一起看才看得出来。

    做法：在 `frontend\**\*.cs` 里找 `action = "<动作>"` 之后**第一次**读的响应键
    （`TryGetProperty("<键>")`，这是仓库里读响应的统一写法），
    再要求该键在 `backend\main.py` 里作为 **JSON 键**出现过（`"<键>":`）。
    只覆盖 `model_` 开头的动作（模型中心/档位这一组），避免对动态构造的嵌套对象误报。
    """
    section("前后端协议键名")
    import glob
    import re

    main_py = os.path.join(BACKEND, "main.py")
    if not os.path.isfile(main_py):
        check("读取 backend/main.py（缺失，跳过）", True, "skipped")
        return
    with open(main_py, encoding="utf-8") as fh:
        backend_src = fh.read()
    backend_keys = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)":', backend_src))

    pairs = []
    frontend = os.path.join(os.path.dirname(BACKEND), "frontend")
    for path in glob.glob(os.path.join(frontend, "**", "*.cs"), recursive=True):
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for m in re.finditer(r'action\s*=\s*"(model_[a-z_]+)"', src):
            tail = src[m.end(): m.end() + 1200]
            prop = re.search(r'TryGetProperty\("([A-Za-z_][A-Za-z0-9_]*)"', tail)
            if prop:
                pairs.append((os.path.basename(path), m.group(1), prop.group(1)))

    check("找到前端读取 model_* 响应键的位置", len(pairs) > 0, f"找到 {len(pairs)} 处")
    unknown = [f"{f}: {a} → {k}" for f, a, k in pairs if k not in backend_keys]
    check("前端读的响应键都在后端出现过", not unknown, "；".join(unknown))
    # 反向锚定：模型详情这一个键必须两边一致（本轮就是这里错的）
    detail_keys = {k for _, a, k in pairs if a == "model_hub_detail"}
    check("model_hub_detail 的响应键一致（detail）", detail_keys == {"detail"},
          f"前端读到 {sorted(detail_keys)}")


def test_imports() -> None:
    """真实导入每一个后端模块。

    v0.2.1 增加：`ast.parse` 只检查语法，**不执行**模块级代码。发布构建里曾经因此
    漏掉一个 `NameError: name 'List' is not defined`——函数注解在没有
    `from __future__ import annotations` 时于**定义时**求值，语法检查发现不了，
    但打包后的后端一启动就崩。这里逐个真正 import 一遍，把这类问题挡在发版之前。
    """
    import importlib

    modules = [
        "i18n", "update_check", "config", "command_handler", "conversation",
        "file_parser", "gguf_meta", "plaintext", "skills_runtime", "summarizer",
        "download_utils", "search_engine", "llm_engine", "model_hub", "marketplace",
        "automation", "main", "version",
    ]
    failures = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    check(f"全部 {len(modules)} 个后端模块可导入", not failures, "; ".join(failures))


def main() -> int:
    tests = [test_imports, test_config, test_plaintext, test_gguf, test_summarizer,
             test_conversation, test_automation, test_marketplace, test_model_match,
             test_protocol_keys]
    for fn in tests:
        try:
            fn()
        except Exception:
            RESULTS.append((f"{fn.__name__} 执行异常", False, traceback.format_exc(limit=2)))

    print("\n================ 结果汇总 ================")
    failed = 0
    for name, ok, detail in RESULTS:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        line = f"[{flag}] {name}"
        if not ok and detail:
            line += f"  → {detail}"
        print(line)
    total = len(RESULTS)
    print(f"------------------------------------------")
    print(f"共 {total} 项，通过 {total - failed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
