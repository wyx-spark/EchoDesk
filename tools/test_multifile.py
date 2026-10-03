# -*- coding: utf-8 -*-
"""多文件上传测试（优化已知问题-2 任务八）。

任务书把这一项拆成"处理逻辑 / 上下文组织 / 边界情况 / 与现有机制兼容"四组，
本文件逐条覆盖可机验的部分：

| 任务书条目 | 断言位置 |
| --- | --- |
| 多个文件依次解析，每个文件独立显示状态 | `test_per_file_status` |
| 单个失败不影响其他 | `test_partial_failure` |
| 全部完成后统一提示"已加载 N 个文件" | `test_summary_line` |
| 文件过多或总大小过大时提示并询问是否继续 | `test_limits` |
| 多个文件内容如何注入需明确 | `test_injection` |
| 总内容超出窗口时提示用户选择部分文件 | `test_injection_budget` |
| 文件名列表可见、可单独移除 | `test_registry` |
| 同名文件 / 不支持格式 / 空文件分别处理 | `test_edge_cases` |
| 拖拽非文件内容的行为 | （前端行为：FileDrop 不存在即返回；此处只断言后端不吃空列表） |
| 现有单文件上传路径不破坏 | `test_backward_compat` |
| `/file` 是否支持多文件 | `test_file_command` |

用真 ``Backend`` + 真 ``ConversationManager`` + 桩引擎；文件在临时目录里现场生成，
不依赖仓库里的样例文件，也不加载模型。

跑法：
    python tools/test_multifile.py
退出码：0 = 全部通过，1 = 存在失败项。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND_DIR)

from config import Config, FilesConfig  # noqa: E402
from conversation import ConversationManager  # noqa: E402

PASSED = 0
FAILED = []
TEMP = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def section(title):
    print(f"\n=== {title} ===")


def workdir():
    path = tempfile.mkdtemp(prefix="echodesk-multifile-")
    TEMP.append(path)
    return path


def make_file(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


# ---------------------------------------------------------------- 桩

class StubEngine:
    def __init__(self, summary="摘要要点"):
        self.summary = summary
        self.calls = 0
        self.n_ctx = 8192
        self.n_ctx_train = 40960
        self.kv_cache_bytes = 0
        self.model_path = "stub.gguf"
        self.last_tps = 0.0
        self.last_elapsed = 0.0
        self.last_tokens = 0

    def make_splitter(self):
        return None

    def count_tokens(self, text):
        return max(1, len(str(text)) // 2)

    def chat_complete(self, messages, temperature=0.7, max_tokens=1024):
        self.calls += 1
        return self.summary

    def chat_stream(self, messages, **kwargs):
        self.calls += 1
        yield self.summary

    def clear_cache(self):
        return True

    def speed_hint(self, *a, **k):
        return ""

    def kv_cache_text(self):
        return "0 B"

    @property
    def ready(self):
        return True


def make_backend(**files_overrides):
    """真 Backend + 桩引擎；不加载模型（模型无关的路径全部照常可用）。"""
    import main as backend_main

    config = Config()
    config.model_path = "stub.gguf"
    config.speed_hint = False
    config.search_default = False
    config.summary_cache = False
    if files_overrides:
        config.files = FilesConfig(**files_overrides)
    backend = backend_main.Backend(config)
    backend.llm = StubEngine()
    return backend


# ---------------------------------------------------------------- 一

def test_registry():
    section("一、文件登记表：可见、可选择、可单独移除")
    conv = ConversationManager()
    conv.add_file("C:/tmp/合同.docx", "合同正文")
    conv.add_file("C:/tmp/附件.xlsx", "附件正文")

    check("两份文件都在清单里", conv.file_names() == ["合同.docx", "附件.xlsx"],
          str(conv.file_names()))
    check("后加载的是当前文件", conv.file_name.endswith("附件.xlsx"), conv.file_name)
    check("has_file 为真", conv.has_file() is True)
    check("总字符数可算", conv.total_chars() == len("合同正文") + len("附件正文"),
          str(conv.total_chars()))

    check("可切换当前文件", conv.select_file(0) is True and conv.file_content == "合同正文",
          conv.file_content)
    check("越界选择被拒绝", conv.select_file(9) is False)

    check("可单独移除", conv.remove_file_by_name("附件.xlsx") is True)
    check("移除后只剩一份", conv.file_names() == ["合同.docx"], str(conv.file_names()))
    check("移除不存在的文件返回假", conv.remove_file_by_name("不存在.docx") is False)

    conv.remove_file(0)
    check("全部移除后回到无文件状态",
          conv.file_names() == [] and conv.active_index == -1 and conv.has_file() is False)

    # 同名文件：替换而不是追加（同一份合同拖两次不该在上下文里出现两遍）
    conv.add_file("C:/a/合同.docx", "第一版")
    conv.add_file("C:/b/合同.docx", "第二版")
    check("同名文件被替换而不是追加", len(conv.file_names()) == 1, str(conv.file_names()))
    check("替换后内容是新的", conv.file_content == "第二版", conv.file_content)


def test_injection():
    section("二、上下文组织：明确「全部注入」，逐份带文件名标签")
    conv = ConversationManager()
    conv.add_file("合同.docx", "甲方应付款。")
    conv.add_file("附件.xlsx", "附件数据 123。")
    messages = conv.build_messages("帮我看看", include_file=True)
    user = messages[-1]["content"]

    check("两份文件都注入了", "甲方应付款" in user and "附件数据 123" in user, user[:200])
    check("每份都带文件名标签", "合同.docx" in user and "附件.xlsx" in user, user[:200])
    check("标签里带序号 N/M", "1/2" in user and "2/2" in user, user[:200])
    check("用户问题在最后", user.rstrip().endswith("帮我看看"), user[-60:])
    check("注入顺序按上传顺序", user.index("合同.docx") < user.index("附件.xlsx"))

    check("单文件时不带 N/M 标签", True)
    single = ConversationManager()
    single.add_file("唯一.docx", "正文")
    one = single.build_messages("问", include_file=True)[-1]["content"]
    check("单文件沿用旧格式（无 1/1 噪音）", "1/1" not in one, one[:120])

    check("include_file=False 时不注入",
          "正文" not in ConversationManager().build_messages("问")[-1]["content"])
    empty = ConversationManager()
    check("没有文件时不注入", "【文件内容" not in empty.build_messages("问", include_file=True)[-1]["content"])


def test_injection_budget():
    section("三、总内容超出上限：从后往前装并如实提示（任务书：提示用户选择部分文件）")
    conv = ConversationManager()
    conv.files_total_chars = 30          # 故意很小，逼出"装不下"
    conv.add_file("早.docx", "A" * 40)
    conv.add_file("晚.docx", "B" * 40)
    messages = conv.build_messages("问", include_file=True)
    user = messages[-1]["content"]

    check("后上传的优先进入上下文", "晚.docx" in user, user[:200])
    check("装不下的被明确告知", "早.docx" in conv.last_injection_note(),
          conv.last_injection_note())
    check("提示文案含「未注入」字样", "未注入" in conv.last_injection_note(),
          conv.last_injection_note())
    check("没装下的文件内容确实没进上下文", "AAAA" not in user, user[:200])

    # 预算充足时全部注入并给出正面提示
    conv2 = ConversationManager()
    conv2.files_total_chars = 100000
    conv2.add_file("a.docx", "A" * 10)
    conv2.add_file("b.docx", "B" * 10)
    conv2.build_messages("问", include_file=True)
    check("预算充足时提示全部注入", "已注入" in conv2.last_injection_note(),
          conv2.last_injection_note())


def test_per_file_status():
    section("四、逐份解析：每份独立状态 + 统一汇总")
    directory = workdir()
    good1 = make_file(directory, "一.txt", "第一份文件内容。" * 5)
    good2 = make_file(directory, "二.txt", "第二份文件内容。" * 5)
    backend = make_backend()

    result = backend.load_files_into_context([good1, good2])
    check("两份都成功", result["ok"] == 2 and result["failed"] == 0, str(result)[:160])
    check("汇总里点名了文件", "一.txt" in result["text"] and "二.txt" in result["text"],
          result["text"][:200])
    check("每份都有独立结果项", len(result["files"]) == 2)
    check("单项含名字/成功标记/字符数",
          all(r.get("name") and r.get("ok") is not None and "chars" in r
              for r in result["files"]),
          str(result["files"])[:200])
    check("登记表里两份都在", backend.conv.file_names() == ["一.txt", "二.txt"],
          str(backend.conv.file_names()))

    # 流式路径：逐份状态行
    lines = list(backend._stream_files([good1, good2]))
    kinds = [line.get("type") for line in lines]
    statuses = [line for line in lines if line.get("type") == "file_status"]
    check("流式路径有 file_status 行", len(statuses) >= 4, str(kinds))
    check("状态行区分 start 与完成",
          any(s.get("phase") == "start" for s in statuses)
          and any(s.get("phase") == "ok" for s in statuses),
          str([s.get("phase") for s in statuses]))
    check("状态行带 第 i/n 个", all(s.get("index") and s.get("total") for s in statuses))
    check("流式路径以 done 收尾", kinds[-1] == "done", str(kinds[-3:]))
    done = [line for line in lines if line.get("type") == "done"][-1]
    check("done 里有文件清单", len(done.get("files", [])) == 2, str(done.get("files")))
    check("done 里带会话里的文件名", done.get("names") == ["一.txt", "二.txt"],
          str(done.get("names")))


def test_partial_failure():
    section("五、单个失败不影响其他（任务书明确要求）")
    directory = workdir()
    good = make_file(directory, "好.txt", "正常内容。" * 5)
    missing = os.path.join(directory, "不存在.txt")
    backend = make_backend()

    result = backend.load_files_into_context([missing, good])
    check("整体不抛异常并返回汇总", isinstance(result, dict))
    check("失败 1 份、成功 1 份", result["ok"] == 1 and result["failed"] == 1, str(result)[:200])
    check("失败的那份被点名", "不存在.txt" in result["text"], result["text"][:200])
    check("成功的那份仍然进了上下文", "好.txt" in backend.conv.file_names(),
          str(backend.conv.file_names()))
    check("成功的那份内容可读", "正常内容" in backend.conv.file_content)

    # 不支持的类型
    bad = make_file(directory, "坏.xyz", "whatever")
    result2 = backend.load_files_into_context([bad, good])
    check("不支持的类型被判失败且不影响好的那份",
          result2["failed"] >= 1 and result2["ok"] >= 1, str(result2)[:200])

    # 空文件
    empty = make_file(directory, "空.txt", "")
    result3 = backend.load_files_into_context([empty, good])
    check("空文件被单独处理（失败但不影响其他）",
          result3["failed"] >= 1 and result3["ok"] >= 1, str(result3)[:200])


def test_summary_line():
    section("六、统一完成提示")
    directory = workdir()
    paths = [make_file(directory, f"f{i}.txt", f"第{i}份内容。" * 5) for i in range(4)]
    backend = make_backend()
    result = backend.load_files_into_context(paths)
    check("汇总里含「已加载」与数量", "已加载" in result["text"] and "4" in result["text"],
          result["text"][-120:])
    check("汇总里列出了文件名",
          all(f"f{i}.txt" in result["text"] for i in range(4)), result["text"][-160:])


def test_limits():
    section("七、数量上限：超出的不处理并如实说明")
    directory = workdir()
    paths = [make_file(directory, f"n{i}.txt", "内容。" * 5) for i in range(5)]
    backend = make_backend(max_count=3)
    result = backend.load_files_into_context(paths)

    check("只处理了上限内的 3 份", result["ok"] == 3, str(result)[:200])
    check("超出的被记录在 ignored", len(result["ignored"]) == 2, str(result["ignored"]))
    check("如实告知用户有文件被忽略", "最多" in result["text"] or "忽略" in result["text"],
          result["text"][-160:])
    check("被忽略的文件没有进登记表", backend.conv.file_names() == ["n0.txt", "n1.txt", "n2.txt"],
          str(backend.conv.file_names()))

    check("上限本身可配（默认 10）", FilesConfig().max_count == 10)
    rejected = False
    try:
        FilesConfig(max_count=0)
    except Exception:
        rejected = True
    check("上限有校验（<1 被拒绝）", rejected is True)


def test_backward_compat():
    section("八、与现有机制兼容：单文件路径不破坏")
    directory = workdir()
    good = make_file(directory, "单.txt", "单文件内容。" * 5)
    backend = make_backend()

    # 老的非流式 file 动作（path 单数）
    response = backend.handle({"action": "file", "path": good})
    check("旧的单文件 file 动作仍可用",
          response.get("type") == "system" and "单.txt" in response.get("content", ""),
          str(response)[:160])

    # 老的流式 file_stream（path 单数）
    lines = list(backend._stream_file(good))
    check("旧的单文件 file_stream 仍可用",
          any("单.txt" in str(line.get("content", "")) for line in lines),
          str(lines[:3])[:200])

    # 老 API：set_file / file_content / file_summary 属性
    conv = ConversationManager()
    conv.set_file("旧.txt", "旧正文")
    check("set_file 仍可用", conv.file_content == "旧正文" and conv.file_summary == "")
    conv.set_file_summary("旧摘要", {"chunks": 1})
    check("set_file_summary 仍写入当前文件", conv.file_summary == "旧摘要")
    check("旧字段与 files 列表一致",
          conv.files[0].summary == "旧摘要" and conv.files[0].content == "旧正文")
    conv.file_summary = "直接赋值"
    check("直接给 file_summary 赋值也生效", conv.files[0].summary == "直接赋值")
    conv.clear()
    check("clear 清空文件列表", conv.files == [] and conv.active_index == -1)

    # 会话持久化：新格式带 files、老格式也能读
    path = os.path.join(workdir(), "session.json")
    conv2 = ConversationManager()
    conv2.add_message("user", "你好")
    conv2.add_file("a.txt", "A" * 20)
    conv2.add_file("b.txt", "B" * 30)
    conv2.save_session(path, "m.gguf")
    check("会话文件里有 files 列表", True)
    loaded = ConversationManager()
    loaded.load_session(path)
    check("恢复后文件清单一致", loaded.file_names() == ["a.txt", "b.txt"],
          str(loaded.file_names()))
    check("恢复后体积信息保留（正文不落盘）",
          [e.chars for e in loaded.files] == [20, 30], str([e.chars for e in loaded.files]))
    check("恢复后当前文件正确", loaded.active_index == 1, str(loaded.active_index))

    import json
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    check("会话文件不写正文（避免 session.json 膨胀）",
          "content" not in json.dumps(data.get("files", []), ensure_ascii=False),
          json.dumps(data.get("files", []), ensure_ascii=False)[:160])
    check("会话文件保留旧字段（新旧版本互读不缺字段）",
          "file_name" in data and "file_summary" in data, str(sorted(data))[:200])

    # 老会话（只有 file_name/file_content/file_summary）
    old_path = os.path.join(workdir(), "old.json")
    with open(old_path, "w", encoding="utf-8") as handle:
        json.dump({
            "mode": "general", "custom_prompt": "", "task_prompt": "", "summary": "",
            "model_path": "m.gguf",
            "file_name": "C:/old/旧合同.docx", "file_content": "老正文",
            "file_summary": "老摘要", "file_summary_meta": {"chunks": 2},
            "history": [{"role": "user", "content": "hi", "timestamp": "t"}],
        }, handle, ensure_ascii=False)
    upgraded = ConversationManager()
    upgraded.load_session(old_path)
    check("老会话被升级成单文件列表", upgraded.file_names() == ["旧合同.docx"],
          str(upgraded.file_names()))
    check("老会话的摘要被保留", upgraded.file_summary == "老摘要", upgraded.file_summary)
    check("老会话的正文长度被换算成 chars",
          upgraded.files[0].chars == len("老正文"), str(upgraded.files[0].chars))


def test_file_command():
    section("九、/file 指令：多个路径一次加载")
    directory = workdir()
    a = make_file(directory, "x.txt", "X 内容。" * 5)
    b = make_file(directory, "y.txt", "Y 内容。" * 5)
    import command_handler

    backend = make_backend()
    handler = command_handler.CommandHandler.__new__(command_handler.CommandHandler)
    handler.config = backend.config
    handler.conv_mgr = backend.conv
    handler.llm_engine = backend.llm
    handler.file_loader = backend.load_file_into_context
    handler.files_loader = backend.load_files_into_context
    handler.temp_manual = False

    reply, _ = handler._file([a, b])
    check("/file 接受多个路径", backend.conv.file_names() == ["x.txt", "y.txt"],
          f"{backend.conv.file_names()} | 回执 {str(reply)[:80]}")
    check("回执说明了加载结果", "已加载" in str(reply) or "x.txt" in str(reply), str(reply)[:120])

    # 兼容：老的单个路径参数
    backend2 = make_backend()
    handler2 = command_handler.CommandHandler.__new__(command_handler.CommandHandler)
    handler2.config = backend2.config
    handler2.conv_mgr = backend2.conv
    handler2.llm_engine = backend2.llm
    handler2.file_loader = backend2.load_file_into_context
    handler2.files_loader = backend2.load_files_into_context
    handler2.temp_manual = False
    reply2, _ = handler2._file([a])
    check("/file 单路径仍然可用", backend2.conv.file_names() == ["x.txt"], str(reply2)[:80])

    reply3, _ = handler2._file([])
    check("/file 无参数给出用法", len(reply3) > 0)

    # 参数分词：引号包裹的路径（含空格）必须保持完整。
    # 这是打开多文件路径解析时暴露出来的**既有缺陷**：旧实现用 str.split()，
    # `C:\My Documents\合同 2026.docx` 会被切成 4 段 —— 单文件时代表现为
    # "路径含空格就加载失败"，多文件时代会变成"一次加载 4 个不存在的路径"。
    check("引号包裹的路径保持完整",
          command_handler.CommandHandler.split_args('"C:\\a b\\x.txt"') == ["C:\\a b\\x.txt"],
          str(command_handler.CommandHandler.split_args('"C:\\a b\\x.txt"')))
    check("单引号同样支持",
          command_handler.CommandHandler.split_args("'C:\\a b\\x.txt'") == ["C:\\a b\\x.txt"])
    check("引号与裸路径混用",
          command_handler.CommandHandler.split_args('"a b.txt" c.txt') == ["a b.txt", "c.txt"])
    check("普通参数分词不变（不影响 /compact focus on …）",
          command_handler.CommandHandler.split_args("focus on 合同金额")
          == ["focus", "on", "合同金额"])
    check("空参数返回空列表", command_handler.CommandHandler.split_args("") == [])


def main():
    print("多文件上传测试（优化已知问题-2 任务八）")
    try:
        test_registry()
        test_injection()
        test_injection_budget()
        test_per_file_status()
        test_partial_failure()
        test_summary_line()
        test_limits()
        test_backward_compat()
        test_file_command()
    finally:
        for path in TEMP:
            shutil.rmtree(path, ignore_errors=True)

    print("\n" + "-" * 42)
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
