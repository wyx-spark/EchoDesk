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

def _write_minimal_pdf(path, text):
    """用裸 PDF 语法生成一个带文本层的单页 PDF。

    v0.2.1（任务书 1.3）把 PDF 解析从 PyMuPDF 换成了 pypdf，而 pypdf 只能读不能写，
    因此这里不再依赖任何 PDF 库来造样本：直接拼出最小可用的 PDF（含 xref 表），
    这样测试既验证了 pypdf 的文本提取，也不再要求安装 AGPL 的 PyMuPDF。
    """
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    with open(path, "wb") as f:
        f.write(bytes(out))


_write_minimal_pdf(os.path.join(T, "d.pdf"), "PDF hello contract 12345")

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
