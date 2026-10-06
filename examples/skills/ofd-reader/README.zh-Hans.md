# OFD 文档读取 —— EchoDesk 示例插件

> 本文是 [README.md](README.md) 的中文参考译本。如两者有歧义，**以英文版为准**。

为 EchoDesk 增加 **`.ofd`** 文件解析能力的官方示例插件。
OFD（Open Fixed-layout Document）是国产版式文档格式，电子发票、公文常用它；
主程序内置解析器只覆盖 Office / TXT / PDF，因此这里演示如何用技能扩展文件格式。

## 它提供什么

| 能力 | 内容 |
| --- | --- |
| 文件解析扩展 | 接管 `.ofd`：`/file 发票.ofd` 与自动化下载的 ofd 附件都能被读取 |
| 指令 | `/ofd <文件路径>` —— 手动验证解析结果 |
| 依赖 | 仅标准库（`zipfile` + `xml.etree`），不引入任何第三方包 |

## 实现方式

OFD 本质是一个 ZIP 容器，正文以 XML 存放。插件解压后按路径顺序读取
`Doc_*/Pages/*/Content.xml`，把 `TextCode` 元素的文字按页拼成纯文本。
复杂版式（矢量图形内的文字、嵌套签章）无法完整还原，但"把文字交给模型"
这个用途足够；需要像素级还原时应改用专门的渲染库。

**解析失败的约定**：主程序通过返回值的前缀识别失败，因此解析器出问题时要返回
以 `解析失败:` 开头的字符串（而不是抛异常）。主程序会把它翻译成当前界面语言，
并且**绝不会**把错误串交给模型。

## 目录结构

```
ofd-reader/
├─ skill.json               插件清单（注意 capabilities.file_parsers: ["ofd"]）
├─ echodesk_ofd/
│  └─ __init__.py           入口：register(api) → api.register_file_parser("ofd", read_ofd)
├─ lang/
│  ├─ zh-Hans.json
│  └─ en.json
├─ README.md
└─ README.zh-Hans.md        中文译本（Chinese translation）
```

## 试用

放进开发目录即可被识别；开发目录就是后端工作目录下的 `skills_dev/`，
其位置随安装方式而变（与 Hello World 示例同一套解析规则）：

| EchoDesk 是怎么装的 | 后端工作目录 | 插件目录放这里 |
| --- | --- | --- |
| Windows 安装程序 / 便携版 zip | `<发布目录>\app\backend\` | `<发布目录>\app\backend\skills_dev\` |
| macOS / Linux 压缩包 | `<发布目录>/app/backend/app/` | `<发布目录>/app/backend/app/skills_dev/` |
| 源码检出（在 `backend/` 里跑后端） | `backend/` | `backend/skills_dev/` |

```
# 1) put the plugin into the development directory —
#    or use Settings -> "Install from folder" and pick this directory

#    Windows (PowerShell)
Copy-Item -Recurse .\ofd-reader "<release directory>\app\backend\skills_dev\"

#    macOS / Linux
cp -R ofd-reader "<release directory>/app/backend/skills_dev/"

# 2) on the "Developer" page of the Settings window, click "Reload local plugins"

# 3) back in the main window — use your own path to an .ofd file
/ofd <path to your .ofd file>
# once the parser is registered, /file works for the same file too
/file <path to your .ofd file>

# path shapes, for reference only:
#   Windows   D:\发票\2026-03.ofd
#   macOS     /Users/<name>/Documents/2026-03.ofd
#   Linux     ~/Documents/2026-03.ofd
```

重载成功时，「已安装」页会显示本插件接管的格式为 `.ofd`；
`/status` 里也会列出「技能扩展格式」。

## 权限声明

```json
{ "network": false, "filesystem": ["skill_dir", "user_selected_file"] }
```

只读取用户明确指定的那个文件与插件自己的目录，不联网、不扫描其它路径。

## 许可证

Apache-2.0，与 EchoDesk 本体一致。
