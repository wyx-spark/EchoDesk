# OFD document reading — an EchoDesk example plugin

> A Chinese reference translation is available at [README.zh-Hans.md](README.zh-Hans.md); if the two ever disagree, the English version prevails.

An official example plugin that adds **`.ofd`** file parsing to EchoDesk.
OFD (Open Fixed-layout Document) is a Chinese fixed-layout document format, widely used for
electronic invoices and official documents; the built-in parsers in the main program cover only
Office / TXT / PDF, so this example shows how to extend the supported file formats with a skill.

## What it provides

| Capability | Detail |
| --- | --- |
| File parser extension | Takes over `.ofd`: both `/file 发票.ofd` and ofd attachments downloaded by automation can be read |
| Command | `/ofd <file path>` — to verify the parse result by hand |
| Dependencies | Standard library only (`zipfile` + `xml.etree`), no third-party packages |

## How it works

An OFD file is at heart a ZIP container with its body stored as XML. After unzipping, the plugin
reads `Doc_*/Pages/*/Content.xml` in path order and joins the text of the `TextCode` elements page
by page into plain text. Complex layouts (text inside vector graphics, nested seals) cannot be
reproduced completely, but that is enough for the purpose of "handing the text to the model"; when
pixel-accurate reproduction is needed, use a dedicated rendering library instead.

**The convention for a failed parse**: the main program recognises failure from a prefix in the
return value, so when the parser runs into a problem it must return a string starting with `解析失败:`
(rather than raising an exception). The main program translates it into the current interface
language and **never** hands the error string to the model.

## Directory structure

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

## Trying it out

Dropping the plugin into the development directory is enough for it to be picked up. That directory
is `skills_dev/` under the backend working directory, and where that is depends on how EchoDesk was
installed (the same resolution rule as the Hello World example):

| How EchoDesk is installed | Backend working directory | Put the plugin directory here |
| --- | --- | --- |
| Windows installer / portable zip | `<release directory>\app\backend\` | `<release directory>\app\backend\skills_dev\` |
| macOS / Linux archive | `<release directory>/app/backend/app/` | `<release directory>/app/backend/app/skills_dev/` |
| Source checkout (backend run from `backend/`) | `backend/` | `backend/skills_dev/` |

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

When the reload succeeds, the "Installed" page shows that this plugin takes over the format `.ofd`;
`/status` also lists the "formats added by skills".

## Permission declaration

```json
{ "network": false, "filesystem": ["skill_dir", "user_selected_file"] }
```

It reads only the file the user explicitly selected and the plugin's own directory; it does not use
the network and does not scan any other path.

## Licence

Apache-2.0, the same as EchoDesk itself.
