"""plaintext.markdown_to_plain 的独立测试（新增三）。

运行：python tools/test_plaintext.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from plaintext import contains_markdown, markdown_to_plain  # noqa: E402

SAMPLE = """# 合同审核意见

**合同金额**为 12,345 元，付款期限 `30 天`。

## 要点

- 甲方：某某公司
- 乙方：*某某集团*
1. 违约金 5%
2. 争议解决：仲裁

| 项目 | 金额 | 备注 |
|------|------|------|
| 货款 | 10000 | 已付 |
| 服务费 | 2345 | 未付 |

> 注意：需补充签署页。

```python
print(1)
```

详见 [原文](https://example.com/a) 与 ![印章](http://x/y.png)

---

完成。
"""

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))


def main():
    out = markdown_to_plain(SAMPLE)

    check("不含 # 标题标记", not any(line.lstrip().startswith("#") for line in out.splitlines()))
    check("不含 ** 粗体", "**" not in out)
    check("不含表格竖线", "|" not in out)
    check("不含 Markdown 代码围栏", "```" not in out)
    check("粗体文字被保留", "合同金额为 12,345 元" in out, out)
    check("行内代码符号被剥离", "付款期限 30 天" in out, out)
    check("标题文字保留并加下划线", "合同审核意见" in out and "======" in out)
    check("无序列表保留 -", "- 甲方：某某公司" in out)
    check("有序列表保留数字", "1. 违约金 5%" in out)
    check("引用被缩进", "  注意：需补充签署页。" in out)
    check("链接转 文字（地址）", "原文（https://example.com/a）" in out)
    check("图片转 [图片：alt]", "[图片：印章]" in out)
    check("代码块内容保留（缩进）", "    print(1)" in out, out)
    check("表格转为对齐文本", "货款" in out and "10000" in out and "服务费" in out)

    # 中文表格列对齐：各行的第二列必须从同一显示列开始
    lines = [line for line in out.splitlines() if line.startswith(("项目", "货款", "服务费"))]
    check("表格有三行", len(lines) == 3, lines)
    if len(lines) == 3:
        import re
        import unicodedata

        def disp(text):
            return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)

        starts = []
        for line in lines:
            match = re.search(r"\s{2,}", line)          # 第一列与第二列之间的填充
            starts.append(disp(line[: match.end()]) if match else -1)
        check("第二列起始显示列一致（中日韩按 2 列计）", len(set(starts)) == 1, str(starts))

    check("contains_markdown 判定转换结果为纯文本", not contains_markdown(out))
    check("contains_markdown 能识别 Markdown", contains_markdown(SAMPLE))
    check("空输入返回空串", markdown_to_plain("") == "")
    check("普通文本原样保留", markdown_to_plain("你好\n世界\n").strip() == "你好\n世界")

    failed = [name for name, ok, _ in CHECKS if not ok]
    for name, ok, detail in CHECKS:
        print(f"{'✅' if ok else '❌'} {name}" + ("" if ok else f"  ← {detail!r}"))
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    if failed:
        print("样例输出：\n" + out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
