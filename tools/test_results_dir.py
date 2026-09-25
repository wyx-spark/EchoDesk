"""results目录结构测试（新增三）。

不依赖浏览器与模型：直接驱动 AutomationEngine 的目录与结果文件逻辑，
验证「结果根目录 / YYYY-MM-DD / 任务名 / result.txt + 原始文件」这一结构，
以及结果文件必须是纯文本无 Markdown 标记。

运行：python tools/test_results_dir.py
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from automation import AutomationEngine  # noqa: E402
from config import program_root  # noqa: E402
from plaintext import contains_markdown  # noqa: E402

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))


class FakeConfig:
    def __init__(self, location):
        self.save_results = True
        self.results_location = location
        self.results_root_name = "results"
        self.task_name = "合同审查"
        self.instruction = "摘取合同的主要信息并生成财务审核意见"
        self.task_list_url = "https://example.com/tasks"
        self.download_dir = ""
        self.dedupe = False
        self.processed_file = ""


def main():
    tmp = tempfile.mkdtemp(prefix="echodesk_results_")
    try:
        cfg = FakeConfig(tmp)
        engine = AutomationEngine(cfg)

        # ---- 1. 默认位置 = 程序根目录；顶层文件夹名可自定义 ----
        default_engine = AutomationEngine(FakeConfig(""))
        check("保存位置留空 → 程序根目录",
              default_engine.results_root == os.path.join(program_root(), "results"),
              default_engine.results_root)
        check("结果根目录跟随自定义位置", engine.results_root == os.path.join(tmp, "results"),
              engine.results_root)
        cfg.results_root_name = "合同结果"
        check("顶层文件夹名可自定义", engine.results_root == os.path.join(tmp, "合同结果"), engine.results_root)
        cfg.results_root_name = "results"

        # ---- 2. 日期层 + 任务名层 + 同名追加序号 ----
        first = engine._prepare_task_dir()
        day = datetime.now().strftime("%Y-%m-%d")
        check("日期目录格式为 YYYY-MM-DD", os.path.basename(os.path.dirname(first)) == day, first)
        check("第三层为任务名称", os.path.basename(first) == "合同审查", first)
        engine._run_task_dir = first

        second = engine._prepare_task_dir()
        check("同名任务自动追加 -1", os.path.basename(second) == "合同审查-1", second)
        third = engine._prepare_task_dir()
        check("继续同名追加 -2", os.path.basename(third) == "合同审查-2", third)
        check("同一天多次运行合并在同一日期目录",
              os.path.dirname(first) == os.path.dirname(second) == os.path.dirname(third))

        # ---- 3. 结果文件固定为 result.txt，纯文本无 Markdown ----
        engine._run_task_dir = first
        engine._report_blocks = []
        engine._report_header = engine._build_report_header()
        engine._write_report(engine._report_blocks, engine._report_header)

        check("结果文件名为 result.txt", os.path.basename(engine.report_path) == "result.txt", engine.report_path)
        check("开始运行即建立 result.txt", os.path.isfile(engine.report_path))

        summary_md = (
            "# 审核意见\n\n"
            "**合同金额**为 12,345 元。\n\n"
            "| 项目 | 金额 |\n|------|------|\n| 货款 | 10000 |\n\n"
            "- 违约金 5%\n"
        )
        engine._save_result("合同A", "https://example.com/a", ["合同A.pdf", "附件A.docx"], summary_md,
                            index=1, total=2)
        engine._save_failure("合同B", "https://example.com/b", ["合同B.pdf"], "详情页没有符合后缀要求的文件链接",
                             index=2, total=2, screenshot=os.path.join(first, "failure-screenshot-task2-20260923_101010.png"))
        engine._save_summary_block("自动化完成：成功 1 个，跳过 0 个，失败 1 个。")

        with open(engine.report_path, encoding="utf-8") as fh:
            report = fh.read()

        check("结果文件不含 # 标题标记", not any(l.lstrip().startswith("#") for l in report.splitlines()))
        check("结果文件不含 ** 粗体", "**" not in report)
        check("结果文件不含表格竖线", "|" not in report)
        check("contains_markdown(结果文件) 为假", not contains_markdown(report))
        check("保留任务标题", "合同A" in report and "合同B" in report)
        check("保留任务链接", "https://example.com/a" in report)
        check("记录附件文件名", "合同A.pdf" in report and "附件A.docx" in report)
        check("保留可读的审核意见正文", "合同金额为 12,345 元" in report)
        check("失败任务写入失败原因", "失败原因：详情页没有符合后缀要求的文件链接" in report)
        check("失败任务记录截图文件名", "失败截图：failure-screenshot-task2-20260923_101010.png" in report)
        check("写入本轮汇总", "自动化完成：成功 1 个" in report)
        check("写入手工核对用的文件头", "任务名称：合同审查" in report and "运行日期：" in report)

        # ---- 4. 下载的原始文件被移动到任务目录 ----
        src = os.path.join(tmp, "src")
        os.makedirs(src, exist_ok=True)
        origin = os.path.join(src, "原始合同.pdf")
        with open(origin, "w", encoding="utf-8") as fh:
            fh.write("dummy")
        moved = engine._move_into_task_dir([origin])
        check("原始文件被移入任务目录", moved == ["原始合同.pdf"] and os.path.isfile(
            os.path.join(first, "原始合同.pdf")), str(moved))
        check("临时位置的文件已移走", not os.path.exists(origin))
        check("任务目录内容 = result.txt + 原始文件",
              # 两侧都排序：结果文件名改为 ASCII 的 result.txt 之后，它与中文附件名的
              # 码点顺序变了（'r' < '原'），只排一侧会变成顺序敏感的比较。
              sorted(os.listdir(first)) == sorted(["原始合同.pdf", "result.txt"]),
              str(os.listdir(first)))

        # ---- 5. 目录层级总览符合任务书示意 ----
        tree = os.path.join(tmp, "results", day)
        check("结构：results/日期/任务名/", os.path.isdir(tree) and os.path.isdir(first),
              str(os.listdir(tree)))

        failed = [name for name, ok, _ in CHECKS if not ok]
        for name, ok, detail in CHECKS:
            print(f"{'✅' if ok else '❌'} {name}" + ("" if ok else f"  ← {detail!r}"))
        print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
        if failed:
            print("\n结果文件内容：\n" + report)
            return 1
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
