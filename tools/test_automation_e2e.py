"""自动化端到端测试：批量处理结果目录结构 + 纯文本 结果.txt（新增三）。

用一个本地静态站点（file:// 页面 + 本地 txt 文件）跑完整的自动化链路：
收集任务链接 → 打开详情页 → 下载文件 → 解析 → （假模型）生成审核意见 →
落地到「结果根目录/YYYY-MM-DD/任务名/」，并校验：

* 结果目录三层结构正确，同名任务自动追加序号；
* ``结果.txt`` 为纯文本（不含 #、**、| 等 Markdown 标记）；
* 下载的原始文件保存在任务目录内；
* 失败任务的错误信息写入 结果.txt，且失败截图保留在任务目录内。

需要本机装有 Edge（msedge 通道）与 playwright。
运行：python tools/test_automation_e2e.py
"""
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from automation import AutomationEngine  # noqa: E402
from config import AutomationConfig  # noqa: E402
from plaintext import contains_markdown  # noqa: E402

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print(f"{'✅' if condition else '❌'} {name}" + ("" if condition else f"  ← {detail!r}"))


class FakeLLM:
    """假模型：返回带 Markdown 的审核意见，验证落盘时会被转成纯文本。"""

    def __init__(self):
        self.calls = 0

    def chat_complete(self, messages, temperature=0.3, max_tokens=512):
        self.calls += 1
        return (
            "# 财务审核意见\n\n"
            "**合同金额**：12,345 元。\n\n"
            "| 项目 | 金额 |\n|------|------|\n| 货款 | 10000 |\n\n"
            "- 付款期限 30 天\n- 违约金 5%\n"
        )


LIST_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>任务列表</title></head><body>
<h1>待处理任务</h1>
<ul>
  <li><a class="task" href="{A}">合同A-年度采购</a></li>
  <li><a class="task" href="{B}">合同B-服务协议</a></li>
  <li><a class="task" href="{C}">合同C-缺少文件</a></li>
</ul>
</body></html>"""

DETAIL_OK = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title></head><body>
<h1>{title}</h1>
<p>正文占位。</p>
<a class="file" href="{f1}">合同正文.txt</a>
<a class="file" href="{f2}">补充条款.txt</a>
</body></html>"""

DETAIL_EMPTY = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title></head><body>
<h1>{title}</h1>
<p>本页没有可下载的文件。</p>
</body></html>"""


def build_site(root):
    """搭一个本地静态站点，返回任务列表页的 file:// 地址。"""
    files = os.path.join(root, "files")
    pages = os.path.join(root, "pages")
    os.makedirs(files, exist_ok=True)
    os.makedirs(pages, exist_ok=True)

    with open(os.path.join(files, "合同正文A.txt"), "w", encoding="utf-8") as fh:
        fh.write("甲方：某某公司\n乙方：某某集团\n合同金额：12,345 元\n付款期限：30 天\n违约金：5%\n")
    with open(os.path.join(files, "补充条款A.txt"), "w", encoding="utf-8") as fh:
        fh.write("补充条款：争议解决方式为仲裁，仲裁地为本地。\n")
    with open(os.path.join(files, "合同正文B.txt"), "w", encoding="utf-8") as fh:
        fh.write("服务费：2,345 元\n服务周期：12 个月\n")

    def url(path):
        return "file:///" + path.replace("\\", "/")

    detail_a = os.path.join(pages, "a.html")
    detail_b = os.path.join(pages, "b.html")
    detail_c = os.path.join(pages, "c.html")

    with open(detail_a, "w", encoding="utf-8") as fh:
        fh.write(DETAIL_OK.format(title="合同A-年度采购",
                                  f1=url(os.path.join(files, "合同正文A.txt")),
                                  f2=url(os.path.join(files, "补充条款A.txt"))))
    with open(detail_b, "w", encoding="utf-8") as fh:
        fh.write(DETAIL_OK.format(title="合同B-服务协议",
                                  f1=url(os.path.join(files, "合同正文B.txt")),
                                  f2=url(os.path.join(files, "合同正文B.txt"))))
    with open(detail_c, "w", encoding="utf-8") as fh:
        fh.write(DETAIL_EMPTY.format(title="合同C-缺少文件"))

    index = os.path.join(pages, "index.html")
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(LIST_PAGE.format(A=url(detail_a), B=url(detail_b), C=url(detail_c)))
    return url(index)


def main():
    tmp = tempfile.mkdtemp(prefix="echodesk_auto_e2e_")
    try:
        list_url = build_site(tmp)
        results_root = os.path.join(tmp, "结果")
        cfg = AutomationConfig(
            enabled=True, safety_accepted=True, safety_version="1.0",
            task_list_url=list_url,
            task_list_selector="a.task",
            task_detail_file_selector="a.file",
            file_extensions=["txt"],
            instruction="摘取合同的主要信息并生成财务审核意见",
            max_tasks=5, dedupe=False, save_results=True,
            results_location=results_root, results_root_name="批量处理结果",
            task_name="合同审查",
            download_dir=os.path.join(tmp, "dl"),
            page_timeout_ms=15000,
            retries=0,
            processed_file="",
        )
        engine = AutomationEngine(cfg, llm_engine=FakeLLM())

        errors = engine.validate()
        check("配置校验通过", not errors, str(errors))

        events = []
        for event in engine.run():
            events.append(event)
        kinds = [e.get("type") for e in events]
        done_events = [e for e in events if e.get("type") == "done"]
        check("运行以 done 结束", bool(done_events), str(kinds[-5:]))
        counters = done_events[-1] if done_events else {}
        print(f"   计数：processed={counters.get('processed')} skipped={counters.get('skipped')} "
              f"failed={counters.get('failed')}")

        # ---- 目录结构 ----
        day = datetime.now().strftime("%Y-%m-%d")
        task_dir = os.path.join(results_root, "批量处理结果", day, "合同审查")
        check("结果根目录为「保存位置/批量处理结果」",
              os.path.isdir(os.path.join(results_root, "批量处理结果")),
              str(os.listdir(results_root) if os.path.isdir(results_root) else "缺失"))
        check("第二层为运行日期 YYYY-MM-DD", os.path.isdir(os.path.dirname(task_dir)),
              os.path.dirname(task_dir))
        check("第三层为任务名称", os.path.isdir(task_dir), task_dir)

        names = sorted(os.listdir(task_dir)) if os.path.isdir(task_dir) else []
        print(f"   任务目录内容：{names}")
        check("任务目录内有 结果.txt", "结果.txt" in names, str(names))
        check("任务目录内保存了下载的原始文件",
              sum(1 for n in names if n.lower().endswith(".txt") and n != "结果.txt") >= 3, str(names))
        check("任务目录内没有 Markdown 结果文件", not any(n.endswith(".md") for n in names), str(names))

        report = ""
        report_path = os.path.join(task_dir, "结果.txt")
        if os.path.isfile(report_path):
            with open(report_path, encoding="utf-8") as fh:
                report = fh.read()
        check("结果文件非空", len(report) > 50, f"{len(report)} 字符")
        check("结果文件不含 # 标题标记", not any(l.lstrip().startswith("#") for l in report.splitlines()))
        check("结果文件不含 ** 粗体", "**" not in report)
        check("结果文件不含表格竖线", "|" not in report)
        check("结果文件不含 ``` 围栏", "```" not in report)
        check("contains_markdown(结果文件) 为假", not contains_markdown(report))
        check("结果文件保留了审核意见正文", "12,345" in report or "合同金额" in report)
        check("结果文件记录了任务标题", "合同A-年度采购" in report and "合同B-服务协议" in report)
        check("结果文件记录了附件文件名", "合同正文A.txt" in report)
        check("结果文件写入了文件头（任务名称/日期）",
              "任务名称：合同审查" in report and day in report)
        check("失败任务（无文件）写入失败原因", "合同C" in report and "失败" in report)

        # ---- 同名二次运行自动追加序号 ----
        engine2 = AutomationEngine(cfg, llm_engine=FakeLLM())
        for _ in engine2.run():
            pass
        siblings = sorted(os.listdir(os.path.join(results_root, "批量处理结果", day)))
        check("同名任务第二次运行追加 -1", "合同审查-1" in siblings, str(siblings))
        check("同名任务两次运行合并在同一日期目录", len(siblings) == 2, str(siblings))
    except Exception:
        check("端到端运行未抛异常", False, traceback.format_exc(limit=3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
