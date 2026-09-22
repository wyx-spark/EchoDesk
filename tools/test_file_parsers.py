"""冒烟测试：验证内置文件解析器对所有支持格式都可用。

覆盖 .docx / .xlsx / .pptx / .pdf / .txt / .csv（含 GB18030 编码），
在临时目录里现场生成测试文件再逐个解析，不需要任何外部素材。

用法：python tools\\test_file_parsers.py
退出码：0 = 全部通过，1 = 存在失败项（可用于 CI）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

T = os.path.join(os.environ["TEMP"], "echodesk_parser_test")
os.makedirs(T, exist_ok=True)

# ---------- 造测试文件 ----------
from docx import Document

d = Document()
d.add_heading("测试合同", 0)
d.add_paragraph("甲方：张三；金额 12345 元。")
tb = d.add_table(rows=2, cols=2)
tb.cell(0, 0).text = "项目"
tb.cell(0, 1).text = "金额"
tb.cell(1, 0).text = "服务费"
tb.cell(1, 1).text = "1000"
d.save(os.path.join(T, "a.docx"))

from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.append(["姓名", "分数"])
ws.append(["李四", 95])
wb.save(os.path.join(T, "b.xlsx"))

from pptx import Presentation

p = Presentation()
s = p.slides.add_slide(p.slide_layouts[5])
s.shapes.title.text = "第一页标题"
p.save(os.path.join(T, "c.pptx"))

import pymupdf

doc = pymupdf.open()
pg = doc.new_page()
pg.insert_text((72, 72), "PDF hello contract 12345")
doc.save(os.path.join(T, "d.pdf"))
doc.close()

with open(os.path.join(T, "e.txt"), "w", encoding="utf-8") as f:
    f.write("纯文本内容 · 中文测试")
with open(os.path.join(T, "f.csv"), "w", encoding="gb18030") as f:
    f.write("名称,数量\n螺丝,10\n")

# ---------- 逐个解析 ----------
from file_parser import parse_file_with_wps

ok = True
for name in ["a.docx", "b.xlsx", "c.pptx", "d.pdf", "e.txt", "f.csv"]:
    result = parse_file_with_wps(os.path.join(T, name), 0)
    flat = result[:150].replace("\n", " / ")
    failed = result.startswith(("解析失败", "不支持的文件类型", "文件内容为空"))
    if failed:
        ok = False
    print(f"{'FAIL' if failed else 'OK  '} {name:8} -> {flat}")

print("\n结果:", "全部通过" if ok else "存在失败项")
sys.exit(0 if ok else 1)
