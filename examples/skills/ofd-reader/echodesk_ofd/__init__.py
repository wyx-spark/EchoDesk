# -*- coding: utf-8 -*-
"""OFD 文档读取 —— EchoDesk 官方示例插件（文件解析扩展）。

OFD（Open Fixed-layout Document）是国产版式文档格式，电子发票、公文常用它。
EchoDesk 内置解析器不认识 ``.ofd``，本插件通过 ``api.register_file_parser``
把 ``.ofd`` 接管过来，之后 ``/file xxx.ofd``、自动化下载的 ofd 附件都能被读取。

实现方式（零第三方依赖）：
  OFD 本质是一个 ZIP 包，正文以 XML 存放。本插件解压后按 ``OFD.xml`` 里声明的
  页面顺序读取 ``Doc_*/Pages/*/Content.xml``，把 ``TextCode`` 元素的文字按
  页面拼起来。复杂版式（矢量图形里的文字、嵌套签章）无法完整还原，
  但"把文字交给模型"这个用途足够。

能力声明（capabilities.permissions）：只读取用户明确指定的那个文件与自己的目录，
不联网、不扫描其它路径。
"""
import os
import re
import xml.etree.ElementTree as ET
import zipfile

#: OFD 正文里承载文字的元素名（各厂商实现略有差异，这里都认）
_TEXT_TAGS = ("TextCode", "Text")


def _strip_ns(tag):
    """去掉 XML 命名空间前缀：``{ns}TextCode`` → ``TextCode``。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _texts_from_xml(data):
    """从一个 Content.xml 里按出现顺序取出全部文字。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # 有些实现里 XML 头部不规整，退化成宽松的正则提取
        raw = data.decode("utf-8", errors="replace")
        return [m.group(1) for m in re.finditer(r"<[^>]*TextCode[^>]*>([^<]*)<", raw)]

    out = []
    for node in root.iter():
        if _strip_ns(node.tag) in _TEXT_TAGS:
            text = (node.text or "").strip()
            if text:
                out.append(text)
    return out


def read_ofd(path):
    """把 OFD 文件转成纯文本（供 EchoDesk 的 /file 与自动化流程使用）。"""
    if not os.path.isfile(path):
        return "解析失败: 文件不存在（{0}）".format(path)
    if not zipfile.is_zipfile(path):
        # 注意：返回以"解析失败"开头会被主程序识别为失败，并本地化成当前语言
        return "解析失败: 不是有效的 OFD 文件（应为 ZIP 容器）"

    pages = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            # 页面正文文件按路径排序，保证页面顺序稳定
            contents = sorted(
                n for n in names
                if n.lower().endswith("content.xml") and "/pages/" in n.lower().replace("\\", "/")
            )
            if not contents:
                # 兜底：任何 Content.xml
                contents = sorted(n for n in names if n.lower().endswith("content.xml"))
            for index, name in enumerate(contents, 1):
                try:
                    texts = _texts_from_xml(zf.read(name))
                except (KeyError, OSError, zipfile.BadZipFile):
                    continue
                if texts:
                    pages.append("【第 {0} 页】\n{1}".format(index, "\n".join(texts)))
    except zipfile.BadZipFile as exc:
        return "解析失败: OFD 容器损坏（{0}）".format(exc)
    except OSError as exc:
        return "解析失败: 读取失败（{0}）".format(exc)

    if not pages:
        return "文件内容为空"
    return "\n\n".join(pages)


def register(api):
    """注册扩展名解析器 + 一个便于手动验证的 /ofd 指令。"""

    ok = api.register_file_parser("ofd", read_ofd)
    if not ok:
        api.warn("未能接管 .ofd（可能已被内置解析器或其它技能占用）")

    def handle(args):
        if not args:
            return api.lang("ofd.usage", "用法：/ofd <文件路径>")
        path = args[0]
        text = read_ofd(path)
        if text.startswith(("解析失败", "文件内容为空")):
            return api.lang("ofd.failed", "❌ 读取失败：{error}", error=text)
        preview = text[:400]
        return api.lang(
            "ofd.ok",
            "✅ 已读取 {name}（{chars} 字符）\n\n预览：\n{preview}",
            name=os.path.basename(path), chars=len(text), preview=preview,
        )

    api.register_command(
        "/ofd", handle,
        label="@cmd.ofd.label",
        usage="/ofd <文件路径>",
        description="@cmd.ofd.description",
    )
