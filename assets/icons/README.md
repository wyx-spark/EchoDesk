# 三平台图标 / Icons for all three platforms

> 本文件**单文件双语**（短说明，一屏内中英并排）。It is intentionally bilingual in one file.
>
> **本目录下的文件都是生成物，不要手工编辑** —— 改了会被下一次生成覆盖。
> Everything under this directory is **generated**; do not edit by hand.

## 源素材与产物 / Source and outputs

| 用途 / Purpose | 文件 / File | 来源 / Source |
| :--- | :--- | :--- |
| Windows | `../EchoDesk.ico` | **源素材**（唯一手工维护的图标文件）/ the single hand-maintained icon |
| Windows（运行时） | `../EchoDesk.png` | 256×256，界面上用到 / 256×256, used by the UI |
| macOS | `../EchoDesk.icns` | 由 `.ico` 生成（PNG 内嵌的 icns 容器） |
| Linux | `hicolor/<N>x<N>/apps/echodesk.png` | 由 `.ico` 生成（freedesktop 目录约定） |

生成命令 / Regeneration:

```bash
python tools/gen_platform_icons.py            # 生成 / generate
python tools/gen_platform_icons.py --check    # 校验是否与源素材一致 / verify (gate)
```

改了 `../EchoDesk.ico` 之后**必须重跑**，否则三平台图标会不一致；`--check` 就是为此设的门禁。

After changing `../EchoDesk.ico` you **must** re-run the generator, or the three platforms drift
apart. `--check` exists to catch exactly that.

## 为什么 `.icns` 是自己拼的 / Why the `.icns` is assembled by hand

`iconutil` 只在 macOS 上存在，而本项目的构建机是 Windows —— 没有任何 macOS 工具可用。
好在 icns 允许**直接内嵌 PNG**（`ic07`/`ic08`/`ic11`/`ic12`/`ic13` 这些块类型），
所以纯标准库就能产出合法容器，不需要图标转换器。

`iconutil` only exists on macOS, while this project builds on Windows — there is no macOS tooling
available. Fortunately icns allows embedding PNG data directly (`ic07`, `ic08`, `ic11`, `ic12`,
`ic13`), so a standard-library script can emit a valid container.

## 诚实的边界 / Honest limits

* **源素材最大 256×256**，因此**没有** 512/1024 的槽位，也没有 `hicolor/512x512`：
  不生成假的放大图。高分屏下系统会放大 256 的图，观感略糊。
  The source art tops out at 256×256, so there are **no** 512/1024 slots and no `hicolor/512x512`.
  Upscaled fakes are not generated. Retina displays will scale the 256 image up, which looks soft.
* 要真正的高分屏图标，需要用矢量源（或原始大图）重新导出 512/1024 再重跑本工具。
  For crisp high-DPI icons, re-export 512/1024 from the vector source and re-run the generator.
* Linux 的 `.desktop` 与 AppStream 元数据在 `deploy/linux/`，安装步骤见那里的 README。
  The Linux `.desktop` file and AppStream metadata live in `deploy/linux/`.

## Linux 安装 / Installing on Linux

```bash
# 图标：整棵 hicolor 树直接拷进去 / copy the whole hicolor tree
sudo cp -r assets/icons/hicolor/* /usr/share/icons/hicolor/
```

桌面条目会把 `Icon=echodesk` 解析到 `hicolor/*/apps/echodesk.png`。
The desktop entry resolves `Icon=echodesk` to `hicolor/*/apps/echodesk.png`.
