"""修复四（模型能力辅助与上下文管理）回归测试。

覆盖：
  * 参数量/量化的解析与"来源标注"（元数据 vs 文件名推断 vs 未知）；
  * 能力提示（KV 超内存、窗口过小、纯 CPU 大模型、速度过低）；
  * 硬件推荐与三档生成预设；
  * 任务型提示词模板（方向一）与模糊提问检测（方向七）；
  * **系统提示词的结构、长度上限与一致性**（v0.2.1 提示词优化，见第 9 节）；
  * 分段摘要缓存（方向八）的命中/失效/清理；
  * 手动压缩上下文（/compact 与 context_compress 动作）与滑动窗口模式。

运行：python tools\\test_capability.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import conversation as conv        # noqa: E402
import i18n                         # noqa: E402
import model_profile as mp          # noqa: E402
import prompts as prompt_lib        # noqa: E402

#: 中文（汉字）检测：第 8 节用它验证"英文界面下不再出现中文文案"。
_HAN = re.compile(r"[\u3000-\u30ff\u4e00-\u9fff]")

# 本文件的大量断言检查的是**中文文案本身**（能力提示、推荐理由、预设名称、
# 任务模板标签与追问话术），所以先把界面语言固定为源语言 zh-Hans。
# 否则在英文区域的机器上，同一批文案会随系统区域变成英文，断言随机失败——
# 那说明的是"测试依赖了运行环境"，而不是"多语言没生效"。
# 「切到英文后文案真的会变」由第 8 节单独覆盖。
i18n.set_locale("zh-Hans")

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}" + (f" —— {detail}" if detail else ""))


class FakeEngine:
    """最小可用的假引擎：只提供能力画像需要的属性。"""

    def __init__(self, model_path: str = "", n_ctx: int = 8192, train: int = 32768) -> None:
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_ctx_train = train
        self.n_threads = 8
        self.n_batch = 512
        self.n_gpu_layers = 0
        self.last_tps = 0.0
        self.last_tokens = 0

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 2)

    def kv_cache_text(self) -> str:
        return "未知"

    def clear_cache(self) -> bool:
        return True


class FakeConfig:
    def __init__(self, **kwargs) -> None:
        self.speed_min_tps = kwargs.get("speed_min_tps", 3.0)
        self.context_mode = kwargs.get("context_mode", "summarize")
        self.preset = kwargs.get("preset", "balanced")
        self.temperature = kwargs.get("temperature", 0.7)
        self.top_p = kwargs.get("top_p", 0.9)
        self.repeat_penalty = kwargs.get("repeat_penalty", 1.1)
        self.max_tokens = kwargs.get("max_tokens", 1024)
        self.clarify_vague = kwargs.get("clarify_vague", True)
        self.summary_cache = kwargs.get("summary_cache", True)
        self.max_history = kwargs.get("max_history", 10)
        self.default_mode = kwargs.get("default_mode", "general")
        self.model_path = kwargs.get("model_path", "")

    def save(self) -> None:
        pass


print("=== 1. 参数量、量化与来源标注 ===")
check("文件名推断参数量", mp.params_from_name("Qwen3.5-0.8B-Q4_K_M.gguf") == 800_000_000.0,
      str(mp.params_from_name("Qwen3.5-0.8B-Q4_K_M.gguf")))
check("MoE 参数量标记 (8x7B)", mp.params_from_name("Mixtral-8x7B-Instruct.gguf") == 56_000_000_000.0,
      str(mp.params_from_name("Mixtral-8x7B-Instruct.gguf")))
check("文件名推断量化", mp.quant_from_name("model-Q4_K_M.gguf") == "Q4_K_M")
check("IQ 量化识别", mp.quant_from_name("model-IQ2_XS.gguf") == "IQ2_XS")
check("无量化标记返回空", mp.quant_from_name("plain.gguf") == "")
check("无参数标记返回 None", mp.params_from_name("plain.gguf") is None)
check("元数据优先于文件名（file_type 枚举转名）",
      mp._FILE_TYPE_NAMES[15] == "Q4_K_M" and mp._FILE_TYPE_NAMES[2] == "Q4_0")
check("参数量估算需要 embedding_length/block_count",
      mp.estimate_parameter_count({}) is None)
check("声明了 general.parameter_count 时直接采用",
      mp.estimate_parameter_count({"general.parameter_count": 123456}) == 123456)

print("=== 2. 能力提示（方向七）===")
profile = mp.ModelProfile(path="x.gguf", parameters=600_000_000, train_context=32768,
                          usable_context=8192, kv_cache_bytes=200 * 1024 ** 2,
                          ram_gb=8.0, cpu_cores=8, n_threads=8, n_gpu_layers=0)
notes = mp.capability_warnings(profile, FakeConfig())
check("窗口只开了训练窗口的 25% 时给出提示",
      any("训练窗口" in n for n in notes), " / ".join(notes))

profile2 = mp.ModelProfile(path="y.gguf", parameters=7_000_000_000, train_context=8192,
                           usable_context=8192, kv_cache_bytes=40 * 1024 ** 3,
                           ram_gb=8.0, cpu_cores=4, n_threads=0, n_gpu_layers=0)
notes2 = mp.capability_warnings(profile2, FakeConfig())
check("KV Cache 超过内存一半时给出提示", any("KV Cache" in n for n in notes2), " / ".join(notes2))
check("纯 CPU 跑大模型时给出提示", any("n_gpu_layers" in n for n in notes2), " / ".join(notes2))
check("n_threads=0 时说明会自动设置", any("自动设置" in n for n in notes2), " / ".join(notes2))

profile3 = mp.ModelProfile(path="z.gguf", parameters=600_000_000, train_context=8192,
                           usable_context=8192, kv_cache_bytes=100 * 1024 ** 2,
                           ram_gb=32.0, cpu_cores=8, n_threads=8, n_gpu_layers=0,
                           last_tps=1.5, last_tokens=200)
notes3 = mp.capability_warnings(profile3, FakeConfig())
check("实测速度低于阈值时给出提示", any("字/秒" in n for n in notes3), " / ".join(notes3))

print("=== 3. 硬件推荐与生成预设（方向六）===")
rec32 = mp.recommend_config(32.0, 16)
check("32GB → 7B 级模型", rec32["params"] == "7B", str(rec32))
check("32GB → 16k 上下文", rec32["context_length"] == 16384, str(rec32))
check("线程数留一核", rec32["n_threads"] == 15, str(rec32["n_threads"]))
check("推荐里带依据说明", len(rec32["reasons"]) >= 3, str(rec32["reasons"]))
rec8 = mp.recommend_config(8.0, 4)
check("8GB → 1.5B 级模型", rec8["params"] == "1.5B", str(rec8["params"]))
rec_low = mp.recommend_config(None, 0)
check("读不到硬件时给出保守建议", rec_low["params"] == "0.8B" and rec_low["n_threads"] == 0,
      str(rec_low))
check("三档预设齐备", set(mp.PRESETS) == {"light", "balanced", "quality"})
check("未知预设返回 None", mp.apply_preset("nope") is None)
check("light 比 quality 回复短",
      mp.PRESETS["light"]["max_tokens"] < mp.PRESETS["quality"]["max_tokens"])
check("预设清单可用于界面", len(mp.preset_menu()) == 3 and "note" in mp.preset_menu()[0])

print("=== 4. 任务型提示词模板（方向一）===")
check("六个模板齐备", len(prompt_lib.TASK_TEMPLATES) == 6, str(prompt_lib.template_ids()))
check("按 id 取模板", (prompt_lib.get_template("contract") or {}).get("label") == "合同条款审查")
check("按中文标签取模板",
      (prompt_lib.get_template("合同条款审查") or {}).get("label") == "合同条款审查")
check("未知模板返回 None", prompt_lib.get_template("nope") is None)
built = prompt_lib.build_system_prompt("contract")
check("模板含具体角色", "财务专员" in built, built[:80])
check("模板含输出格式约束", "【付款条件】" in built)
check("模板含思维链要求", "[推理要求]" in built)
check("模板含少样本示例", "[示例]" in built and "输入1" in built)
check("模板强调不得编造", "臆测" in built)
check("未知模板产出空串", prompt_lib.build_system_prompt("nope") == "")
check("机器可读清单含示例数", all("example_count" in x for x in prompt_lib.public_list()))

print("=== 5. 模糊提问检测（方向七）===")
check("短指代 → 判为模糊", prompt_lib.is_vague_question("这个怎么办"))
check("泛动词 → 判为模糊", prompt_lib.is_vague_question("帮我弄一下"))
check("正常短问题 → 不判模糊", not prompt_lib.is_vague_question("1+1=?"))
check("较长具体问题 → 不判模糊",
      not prompt_lib.is_vague_question("请帮我审查这份采购合同的付款条款与违约责任"))
check("空串不判模糊", not prompt_lib.is_vague_question(""))
# 引导话术（v0.2.1 系统提示词重构）：整体来自语言包（task.clarify_prompt +
# task.guide_dir.*），这里检查"列方向让用户挑"这一行为特征，不依赖具体措辞。
# 旧版是"请补充这三点（对象/输出形式/约束）"——它把组织语言的责任推回给用户，
# 正是任务书现象三要修的东西。
_clarify = prompt_lib.clarification_prompt("帮我看看这个")
check("引导话术列出 3 个可选方向", all(f"{i}." in _clarify for i in (1, 2, 3)), _clarify[:80])
check("引导话术让用户回一个编号", "编号" in _clarify, _clarify[:60])
check("引导话术不再要求用户补三点信息",
      "具体对象" not in _clarify and "输出形式" not in _clarify, _clarify[:80])
check("引导话术的方向可随提问变化（不是一套固定模板）",
      prompt_lib.guide_direction_ids("这个报错怎么办")[0] == "diagnose"
      and prompt_lib.guide_direction_ids("帮我看看这份合同")[0] == "object",
      str(prompt_lib.guide_direction_ids("这个报错怎么办")))
check("引导方向始终是 3 个且不重复",
      all(len(set(prompt_lib.guide_direction_ids(q))) == 3 for q in
          ("帮我看看", "这个报错怎么办", "帮我写一段", "")),
      str(prompt_lib.guide_direction_ids("帮我看看")))

print("=== 6. 分段摘要缓存（方向八）===")
tmp = tempfile.mkdtemp(prefix="echodesk-cache-")
try:
    src = os.path.join(tmp, "doc.txt")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write("第一版内容")
    cache_file = os.path.join(tmp, "summary_cache.json")
    cache = mp.SummaryCache(cache_file)
    params = {"chunk_chars": 1000, "overlap_chars": 100}
    check("未写入时读不到", cache.get(src, params) is None)
    cache.put(src, params, "这是摘要", chunk_count=3, origin_chars=5000)
    cache.save()
    hit = cache.get(src, params)
    check("写入后可命中", bool(hit) and hit["text"] == "这是摘要", str(hit))
    check("命中计数递增", hit["hits"] == 1, str(hit.get("hits")))
    check("参数变化后指纹不同（不误命中）",
          cache.get(src, {"chunk_chars": 2000, "overlap_chars": 100}) is None)
    # 文件内容变化后必须失效（这是"缓存出错答案"的唯一防线）
    with open(src, "w", encoding="utf-8") as handle:
        handle.write("第二版内容，长度也不同")
    check("文件改动后缓存失效", cache.get(src, params) is None)
    stats = cache.stats()
    check("统计里有条目数", stats["entries"] >= 1, str(stats))
    check("清理返回条数", mp.SummaryCache(cache_file).clear() >= 1)
    check("清理后为空", mp.SummaryCache(cache_file).stats()["entries"] == 0)
    check("缓存文件可读 JSON（用户可自行查看/删除）", os.path.isfile(cache_file))
    check("路径为空时降级为内存缓存不报错", mp.SummaryCache("").stats()["entries"] == 0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("=== 7. 能力画像装配与 /status 段落 ===")
engine = FakeEngine(model_path=os.path.join(ROOT, "backend", "config.json"), n_ctx=4096)
profile = mp.build_profile(engine, FakeConfig())
check("画像含模型名", profile.name == "config.json", profile.name)
check("画像标注参数量来源", profile.parameters_source in ("GGUF 元数据", "按文件名推断", "未知"),
      profile.parameters_source)
check("画像含机器信息", profile.cpu_cores > 0, str(profile.cpu_cores))
public = profile.public()
check("机器可读字段齐全",
      {"parameters_text", "quantization", "train_context", "usable_context",
       "kv_cache_text", "speed_text", "warnings"} <= set(public), str(sorted(public)))
check("没有实测数据时速度写'尚未统计'", public["speed_text"] == "尚未统计", public["speed_text"])
check("窗口说明给出百分比",
      "训练窗口" in mp.ModelProfile(train_context=8192, usable_context=4096).context_usage_hint())
check("窗口用满时说明已用满",
      mp.ModelProfile(train_context=8192, usable_context=8192).context_usage_hint()
      == "已用满模型训练窗口")

print("=== 8. 能力/模板/预设文案随界面语言切换（任务三 中英双语）===")
i18n.set_locale("en")
try:
    _en_notes = mp.capability_warnings(profile2, FakeConfig())
    check("英文下能力提示不含中文", bool(_en_notes) and not any(_HAN.search(n) for n in _en_notes),
          " / ".join(_en_notes))
    _en_reasons = mp.recommend_config(32.0, 16)["reasons"]
    check("英文下推荐理由不含中文", not any(_HAN.search(r) for r in _en_reasons), str(_en_reasons))
    _light = mp.apply_preset("light")
    check("英文下预设名称与说明不含中文",
          not _HAN.search(str(_light.get("label", "")) + str(_light.get("note", ""))), str(_light))
    check("英文下任务模板标签与说明不含中文",
          not any(_HAN.search(str(x["label"]) + str(x["note"])) for x in prompt_lib.public_list()),
          str(prompt_lib.public_list()[0]))
    check("英文下追问话术不含中文", not _HAN.search(prompt_lib.clarification_prompt("x")))
    check("英文下参数量来源标注不含中文",
          not _HAN.search(mp.ModelProfile().parameters_source), mp.ModelProfile().parameters_source)
    # 模板正文（role/format）是发给模型的提示词内容，按设计**保持中文**
    contract_role = str((prompt_lib.get_template("contract") or {}).get("role", ""))
    check("英文下模板正文仍为中文提示词（刻意不进语言包）",
          bool(_HAN.search(contract_role)), contract_role[:60])
finally:
    i18n.set_locale("zh-Hans")
check("切回中文后能力提示恢复中文",
      any(_HAN.search(n) for n in mp.capability_warnings(profile2, FakeConfig())))

print("=== 9. 系统提示词：六要素结构、长度上限与一致性（v0.2.1 提示词优化）===")
# 审计口径：模式块必须自带「角色 / 输出格式 / 详略 / 思维 / 示例 / 语气」六要素，
# 全局性的「边界」写在共享主干 _CORE_RULES 里（理由见 conversation.py 文件头：
# 与场景无关的规则共享一份，既不白占上下文，也不会改一处漏五处）。
#
# 【详略】是 **v0.2.1 系统提示词重构**新增的第 6 个要素：上一轮补齐六要素后
# 约束加码过多，模型对任何问题都套满格式（任务书现象一）。下面这几条断言
# 守住"新增了判断详略的说明"，同时守住"长度上限没有因为放松而膨胀"。
_MODE_DIMS = ("【角色】", "【输出格式】", "【详略】", "【思维】", "【示例】", "【语气】")
_MODE_MAX = 480      # 单个模式块字符上限
_PROMPT_MAX = 700    # 模式块 + 共享主干 + 语言指令 的字符上限


def _t_mode_desc(locale: str, mode: str) -> str:
    """取某语言下某模式的界面说明（用完切回 zh-Hans，避免影响后续断言）。"""
    i18n.set_locale(locale)
    try:
        return i18n.t(f"mode.desc.{mode}")
    finally:
        i18n.set_locale("zh-Hans")

for _mode, _block in conv.MODES.items():
    _missing = [d for d in _MODE_DIMS if d not in _block]
    check(f"模式 {_mode}：六要素齐备", not _missing, str(_missing))
    check(f"模式 {_mode}：长度 ≤ {_MODE_MAX} 字符", len(_block) <= _MODE_MAX, f"{len(_block)} 字符")

for _mode in conv.MODES:
    _sp = conv.ConversationManager(default_mode=_mode).get_system_prompt()
    check(f"模式 {_mode}：系统提示词 ≤ {_PROMPT_MAX} 字符", len(_sp) <= _PROMPT_MAX, f"{len(_sp)} 字符")
    check(f"模式 {_mode}：含共享主干的边界要求", "不知道就说不知道" in _sp)

check("共享主干含视角澄清（'用户'指提问的人）", "「用户」指正在提问的人" in conv._CORE_RULES)
check("模式与温度预设一一对应", set(conv.MODES) == set(conv.MODE_TEMPERATURES),
      str(sorted(set(conv.MODES) ^ set(conv.MODE_TEMPERATURES))))

# --- v0.2.1 系统提示词重构：放松层与"不得改过头"的底线 ---
check("共享主干含「判断详略」（放松格式约束）",
      "详略自定" in conv._CORE_RULES and "能一句话答完就一句话" in conv._CORE_RULES)
check("共享主干含「先答后问」（边界声明不再退化成拒绝作答）",
      "先答后问" in conv._CORE_RULES and "不要只回「请提供更多信息」" in conv._CORE_RULES)
check("共享主干含探索性建议（摊开推测）",
      "摊开推测" in conv._CORE_RULES and "如果是 A，建议 X；如果是 B，建议 Y" in conv._CORE_RULES)
check("共享主干含引导（含糊提问主动给方向）",
      "引导" in conv._CORE_RULES and "2~3 种可能的方向" in conv._CORE_RULES)
check("共享主干仍保留防编造底线", "不编造事实、数据、法条、条款或出处" in conv._CORE_RULES)
check("模式块里不再有「必须分点」式的无条件格式要求",
      not any("分点时写" in block for block in conv.MODES.values()))

# 引导模式（本轮新增）：用户说不清想要什么时用它
check("新增引导模式 guide", "guide" in conv.MODES)
_guide = conv.MODES.get("guide", "")
check("引导模式要求列 2~3 种理解", "2~3 种" in _guide)
check("引导模式不在选定前展开方案", "不替我决定" in _guide or "选定前" in _guide)
check("引导模式有编号别名与缩写",
      conv.MODE_ALIASES.get("7") == "guide" and conv.MODE_ALIASES.get("gd") == "guide"
      and conv.MODE_SHORT_NAMES.get("guide") == "gd")
check("引导模式可被 set_mode 切换", conv.ConversationManager().set_mode("gd"))
check("引导模式出现在模式菜单里", "guide" in conv.ConversationManager.mode_menu())
check("引导模式的语言包说明非空（中英都要）",
      all(_t_mode_desc(loc, "guide") for loc in ("zh-Hans", "en")),
      f"{_t_mode_desc('zh-Hans', 'guide')!r} / {_t_mode_desc('en', 'guide')!r}")

# 关键格式硬要求不得因放松而丢失（任务书"防止改过头"）
check("合同模式仍要求摘录逐字一致", "逐字一致" in conv.MODES["contract"])
check("合同模式仍要求建议人工复核", "建议人工复核" in conv.MODES["contract"])
check("合同模式仍禁止臆测", "不要臆测" in conv.MODES["contract"])
check("翻译模式仍要求数字不得改动", "不改动数字" in conv.MODES["translate"])
check("摘要模式仍要求数字日期原样保留", "原样保留" in conv.MODES["summary"])

# 任务层同样改成条件式（v0.2.1 系统提示词重构）
check("任务模板 contract 改为先列常见风险", "先列出这类合同最常见的 3~5 条风险点" in
      str(prompt_lib.TASK_TEMPLATES["contract"]["format"]))
check("任务模板 contract 要求提示人工复核", "建议人工复核" in
      str(prompt_lib.TASK_TEMPLATES["contract"]["format"]))
check("任务模板 code 改为按复杂度决定详略", "一两句答完即可" in
      str(prompt_lib.TASK_TEMPLATES["code"]["format"]))
check("任务模板 summary 改为长短自适应", "200 字以内）时用一段话说清" in
      str(prompt_lib.TASK_TEMPLATES["summary"]["format"]))
check("任务模板 reason 不再要求信息不足先追问",
      "先追问" not in str(prompt_lib.TASK_TEMPLATES["reason"]["cot"])
      and "先按最可能的情况给出结论" in str(prompt_lib.TASK_TEMPLATES["reason"]["cot"]))
check("任务模板 translate 不再无条件解释译法", "不要解释你为什么这样译" in
      str(prompt_lib.TASK_TEMPLATES["translate"]["format"]))

# 一致性（v0.2.1 修掉的真问题）：/prompt 的包装语原先写死「不要反问」，
# 而模糊提问检测与 reason 模板都要求"信息不足先追问"——两条指令互相打架。
_custom = conv.ConversationManager()
_custom.set_custom_prompt("只回答一个字。")
_custom_prompt = _custom.get_system_prompt()
check("自定义指令包装语不再无条件禁止追问",
      not any(x in _custom_prompt for x in ("不要反问", "禁止追问", "不许问")), _custom_prompt[-120:])
check("自定义指令保留了「信息不足先答后问」的例外",
      "先给出最可能的答案" in _custom_prompt and "再问要不要按其它情况展开" in _custom_prompt)
check("自定义指令会替换模式块（符合 /prompt 的既有约定）", "【角色】" not in _custom_prompt)

# 模式层 + 任务层叠加：冲突必须由提示词里的裁决行确定，而不是留给模型猜
_layered = conv.ConversationManager(default_mode="contract")
_layered.set_task_prompt(prompt_lib.build_system_prompt("contract"))
_layered_prompt = _layered.get_system_prompt()
check("模式层与任务层可同时存在（叠加而非替换）",
      "【角色】" in _layered_prompt and "[任务模式]" in _layered_prompt)
check("模式层排在任务层之前",
      _layered_prompt.index("【角色】") < _layered_prompt.index("[任务模式]"))
check("两层冲突时有优先级裁决行", "[优先级]" in _layered_prompt)
check("叠加后不出现无条件拒绝追问的措辞",
      not any(x in _layered_prompt for x in ("不要反问", "禁止追问", "不许问")))

# 回复语言跟随界面语言，且整段提示词里只能出现一次（出现两次就是自相矛盾）
i18n.set_locale("zh-Hans")
_zh_prompt = conv.ConversationManager().get_system_prompt()
check("中文界面要求简体中文回复", "请始终使用简体中文回复" in _zh_prompt)
i18n.set_locale("en")
_en_prompt = conv.ConversationManager().get_system_prompt()
check("英文界面要求英文回复", "Always reply in English." in _en_prompt)
check("英文界面下不再残留中文回复指令", "请始终使用简体中文回复" not in _en_prompt)
check("回复语言指令只出现一次", _en_prompt.count("Always reply in English.") == 1)
check("提示词正文仍为中文（刻意不进语言包）", bool(_HAN.search(_en_prompt)))
check("未知语言回退为「跟随提问语言」",
      conv._REPLY_LANGUAGE.get("ja", conv._REPLY_LANGUAGE_FALLBACK) == conv._REPLY_LANGUAGE_FALLBACK)
i18n.set_locale("zh-Hans")

print()
print("=" * 60)
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
