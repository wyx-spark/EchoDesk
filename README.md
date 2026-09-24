# EchoDesk

**A local, auditable, extensible AI desktop assistant for users working in strictly-controlled environments.**

EchoDesk is a Windows desktop application that keeps the entire AI stack on your own machine:
inference, document parsing, conversation history and browser automation all happen locally.
It is built for people who are not allowed to send their documents to a cloud AI service —
compliance-bound enterprises, network-isolated or air-gapped sites, and anyone who simply
wants their files to stay on the machine.

No cloud account. No API key. No telemetry.

[中文说明 →](README.zh-Hans.md)

- **Platform**: Windows 10 / 11 (x64), portable folder with a single entry point
- **Inference**: local GGUF models through llama.cpp (`llama-cpp-python`), CPU by default
- **Interface**: C# WPF desktop app, packaged backend written in Python 3.13
- **Languages**: Simplified Chinese and English shipped; more via community language packs
- **License**: [Apache-2.0](LICENSE)

## Why EchoDesk

| Differentiator | What it means in practice |
| --- | --- |
| **Fully local inference** | Prompts, documents, model output and conversation history never leave the machine. The backend loads a local `.gguf` model with llama.cpp; there is no cloud fallback and no hidden upload path. Network access exists only for features you start yourself (model download, web search, skill marketplace, automation). |
| **Reads encrypted WPS/Office documents** | Office documents are parsed through WPS/Office COM first, which is what makes password-protected and legacy `.doc` / `.ppt` files readable — a very common requirement inside enterprises. If WPS/Office is not installed, EchoDesk falls back to its own lightweight parsers for `.docx`, `.xlsx`, `.xlsm`, `.xls`, `.pptx`, `.pdf` and plain text. |
| **Batch web automation, configured visually** | Point EchoDesk at a task-list page, describe the task links, and it walks the list, opens each task, downloads the matching attachments, reviews them with the local model and writes a result file per task. Configuration works through a point-and-click selector picker and per-field help, with a selector test that opens the page and shows what matched before you run anything. |
| **Audit log and safety protocol** | Browser automation requires explicitly accepting a safety protocol; the protocol version and your consent time are written to an audit log. Every automation run is logged (task, time, downloaded file names) with no document text, model output, tokens or cookies. Downloaded files live in a temporary folder and are deleted as soon as the task is processed. |
| **Skill marketplace plus a local plugin interface** | Extend EchoDesk with skills discovered through the GitHub topic `echodesk-plugin`, or load a plugin straight from disk while you develop it — no need to publish first. Skills register commands, UI panels, automation hooks and extra file parsers. |
| **Multi-language** | The interface, system messages, error messages and command help are language-pack driven. Simplified Chinese and English ship today; additional locales are community contributions (see [Languages](#languages)). |

## Core capabilities

| Capability | Details |
| --- | --- |
| **Local chat** | Streaming answers from a local model, with adjustable temperature, prompt role, per-model profiles and desktop keyboard shortcuts. `/help` lists every command. |
| **Context management** | Default context window 8192 tokens (hard cap 32768), automatically converged to the model's real training window read from GGUF metadata. `/status` reports the window and an fp16 KV-cache estimate. |
| **Long documents** | Documents larger than a fraction of the window are not truncated: they are split, summarized chunk by chunk (map-reduce) and merged, keeping **source pointers** (file name plus character ranges) so answers can be traced back. `/summarize` triggers it manually. |
| **Document formats** | `.docx`, `.xlsx`, `.xlsm`, `.xls`, `.pptx`, `.pdf`, `.txt`, `.md`, `.csv` and similar plain-text formats; `.doc` / `.ppt` and encrypted files via WPS/Office COM. |
| **Model hub** | Search and download GGUF models from a Hugging Face mirror (`hf-mirror.com` by default, switchable to the official endpoint), verify SHA-256, register them in a local model library and hot-swap between them with `/model`. |
| **Browser automation** | Playwright drives the Microsoft Edge already installed on the machine, always in headed mode so you can watch it work. Supports pagination, task de-duplication, per-task download and review, pause-and-wait when a captcha or second factor appears, failure screenshots, batch scheduling and plain-text result files. |
| **Skills** | Install, verify and uninstall skills from the marketplace window (`Ctrl+M`); the marketplace validates `skill.json`, marks unverified skills as non-installable and never auto-executes packaged scripts. |
| **Optional web search** | Off by default. When you switch it on with `/search on`, only the current query keywords are sent to DuckDuckGo. |

## System requirements

| Component | Requirement |
| --- | --- |
| Operating system | Windows 10 or Windows 11, 64-bit (x64). |
| Frontend runtime | None to install. The WPF app (target framework `net10.0-windows`) is published self-contained, so the portable build carries its own runtime. |
| CPU | Any x64 CPU. Inference runs on CPU by default (`n_gpu_layers: 0` in `backend\config.json`). |
| Memory | 4 GB minimum; 8 GB or more recommended. The default model file is about 508 MB, and the KV cache for the default 8192-token window is roughly 0.3–0.4 GB on top of it. Larger context windows and larger models need more. |
| Disk | About 1 GB for the portable folder (roughly 0.9 GB when the model is included, about 0.4 GB without it). Allow 2 GB free while a model is being downloaded. |
| WPS Office or Microsoft Office (optional) | Required for encrypted documents and legacy `.doc` / `.ppt` files, which are read through COM. Without it, use the modern formats listed above. |
| Microsoft Edge (optional) | Required only for browser automation, which drives the locally installed Edge (`channel = msedge`). EchoDesk does not bundle a browser. |
| Network | Optional. Everything except model download, web search, the skill marketplace and automation works offline. |

## Quick start

### Option A — run the portable build (recommended, nothing to install)

1. Get the release folder, either from the
   [Releases page](https://github.com/VoidEntropiaStudio/EchoDesk/releases) of this repository
   (once a release archive has been published) or by building it yourself
   (see [Option B](#option-b--build-from-source)); the build lands in `publish\EchoDesk-v0.2.1\`.
2. Unzip it anywhere — desktop, another drive, even a USB stick.
3. Double-click **`EchoDesk-v0.2.1.lnk`**. That shortcut is the only entry point you need;
   `app\EchoDesk.exe` is the UI and `app\backend\EchoDeskBackend.exe` is started automatically —
   never launch the backend by hand.
4. On first launch, answer the model question described below, then start typing.
   Useful first commands: `/help`, `/file <path>`, `/model list`, `/status`.

Keyboard shortcuts worth knowing: `Ctrl+Enter` send, `Esc` stop generation, `Ctrl+O` upload a
file, `Ctrl+L` clear the conversation, `Ctrl+,` settings, `Ctrl+M` skill marketplace.

### Option B — build from source

Requirements: Python 3.13 (backend) and a .NET SDK able to build the project's target framework
`net10.0-windows` (frontend).

```powershell
# 1) Backend dependencies (Python 3.13; the pinned set lives in requirements.txt)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt

# 2) Frontend
dotnet build frontend\EchoDesk.csproj -c Release

# 3) Tests - they do not need a model file
python tools\selftest.py            # backend unit-level self-test, exit code 0/1
python tools\test_file_parsers.py   # smoke test for every supported file format

# 4) Full portable build -> publish\EchoDesk-v0.2.1\
& tools\build_release.ps1
& tools\build_release.ps1 -SkipBackend     # rebuild only the WPF frontend
& tools\build_release.ps1 -SkipFrontend    # rebuild only the packaged backend
& tools\build_release.ps1 -IncludeModels   # include local .gguf models (offline bundle)
```

The WPF frontend talks to the **packaged** backend executable, so a frontend-only development
loop needs that executable once. Build it and point the app at it with the `ECHODESK_BACKEND`
environment variable:

```powershell
# Build the backend with PyInstaller (product: dist\EchoDeskBackend\EchoDeskBackend.exe)
pyinstaller --noconfirm --distpath dist --workpath build\pyi backend\EchoBackend.spec

# Run the frontend from source against it
$env:ECHODESK_BACKEND = "$PWD\dist\EchoDeskBackend\EchoDeskBackend.exe"
dotnet run --project frontend\EchoDesk.csproj
```

The backend can also be started on its own (`python backend\main.py`): it speaks a JSON-per-line
protocol over stdin/stdout, documented in [docs/DEVELOPER.md](docs/DEVELOPER.md). Place a `.gguf`
model in `backend\models\` (source runs) or in the packaged backend's `models\` folder.

### First launch: the model download question

EchoDesk does **not** ship model weights and never downloads anything silently:

1. On start, the backend checks for the model named in `backend\config.json`
   (default `Qwen3.5-0.8B-Q4_K_M.gguf`, about 508 MB).
2. If the file is missing, it reports `need_download` and the UI asks whether to download it from
   the configured Hugging Face endpoint — `https://hf-mirror.com` by default, which you can point
   at the official `https://huggingface.co` or an internal mirror by editing `hf_endpoint` /
   `model_repo` / `model_path` in `backend\config.json`.
3. **Yes** downloads the model and continues starting up. **No** exits the program; copy any
   `.gguf` file into `app\backend\models\` and start again instead — that path needs no network.
4. If no launcher shortcut exists in the folder, EchoDesk asks once whether to create one, and
   then shows a short first-run guide. Per-machine settings can be kept out of version control by
   putting them in `app\backend\config.user.json`, which takes precedence over `config.json`.

### What is inside the portable folder

The portable build separates the program from your data: everything you may want to edit, back up
or move lives in the outer folder, while the program itself sits in `app\`.

| Path | Contents |
| --- | --- |
| `EchoDesk-v0.2.1.lnk` | The only entry point (targets `app\EchoDesk.exe`, working directory = this folder). |
| `批量处理结果\` | Automation output (`date\task\` folders with plain-text reports and downloaded files). |
| `ui_state.json`, `snippets.json` | Window state, font size, theme, language and reusable snippets. |
| `lang\` | Backend language packs (system messages, errors, command help). Drop in a new `<locale>.json` to add a language. |
| `examples\skills\` | Example plugin sources (Hello World and an OFD reader). |
| `docs\` | Documentation shipped with the release (`docs\zh-Hans\`, `docs\en\`), plus privacy policy, EULA and third-party licences. |
| `LICENSE`, `NOTICE`, `SECURITY.md` | Licence, attribution and security policy. |
| `app\EchoDesk.exe` | WPF frontend; its .NET dependencies are DLLs next to it. |
| `app\lang\ui\` | UI language packs (labels, buttons, dialogs). |
| `app\backend\EchoDeskBackend.exe` | Packaged Python backend (PyInstaller onedir), dependencies in `app\backend\_internal\`. |
| `app\backend\config.json` | All configuration: model, context window, summarization, automation, marketplace, model hub. |
| `app\backend\models\` | Local `.gguf` models. |
| `app\backend\session.json` | Conversation history (auto-saved and restored). |

Set the `ECHODESK_DATA` environment variable to move the outer data folder somewhere else (useful
on managed machines where the program folder itself is read-only).

## Screenshots and demo

No screenshots are committed to this repository yet. The only image assets that exist today are
`assets/EchoDesk.ico` (application icon) and `assets/EchoDesk.png`. The table below is a
placeholder checklist: capture these shots, drop the files into `assets/screenshots/`, and replace
each row with an image reference pointing at the suggested file name.

| # | What to capture | Suggested file |
| --- | --- | --- |
| 1 | Main window with a document under discussion and the context/KV-cache status line visible | `assets/screenshots/main-window.png` |
| 2 | Long-document summarization in progress (chunk counter plus source pointers) | `assets/screenshots/long-document.png` |
| 3 | Settings → Automation tab, showing field-level help and the selector test result | `assets/screenshots/automation-settings.png` |
| 4 | Headed Edge window driven by an automation run, next to EchoDesk's live progress log | `assets/screenshots/automation-run.png` |
| 5 | Skill marketplace (`Ctrl+M`) with verified / unverified / installed states, and the same window in Simplified Chinese | `assets/screenshots/marketplace.png` |

## Languages

EchoDesk is designed for global users, and localization is part of the product rather than an
afterthought:

- **Shipped today**: Simplified Chinese (`zh-Hans`) and English (`en`).
- **Language packs**: `backend/lang/<locale>.json` for backend messages, error texts and command
  help; `frontend/Resources/Lang/<locale>.json` for UI strings. Documentation lives under
  `docs/<locale>/`. Each entry uses a semantic key such as `settings.automation.task_url.label`
  and may carry a comment describing its context and placeholders.
- **Automatic detection**: `language` defaults to `auto`, so the first start follows the system
  locale; you can also pick a language in Settings, and the choice is remembered.
- **Fallback chain**: `requested locale → en → zh-Hans → the key itself`. Returning the key makes
  missing translations obvious instead of silently showing blanks.
- **Community locales**: `zh-Hant`, `ja`, `ko`, `de`, `fr`, `es` and `pt-BR` are the next
  candidates, and any locale is accepted — see the translation workflow in
  [CONTRIBUTING.md](CONTRIBUTING.md).

## Extending EchoDesk: skills and local plugins

EchoDesk is extensible in two ways, and both use the same runtime interface:

- **Skill marketplace** — open it with `Ctrl+M`. Skills are discovered through the GitHub topic
  `echodesk-plugin`; a repository is *verified* when its root contains a `skill.json` with an
  `echodesk.bundle` entry. Unverified repositories cannot be installed, packaged `prepare`
  scripts are never executed automatically, and the marketplace caches results so an offline or
  rate-limited GitHub API degrades gracefully.
- **Local plugin interface** — point EchoDesk at a plugin folder on disk and it is discovered and
  loaded like a marketplace skill, with no GitHub round-trip. A development directory
  (`backend\skills_dev\`) is the intended place for work in progress; check the skill guide for
  the current layout and API surface.

Start here:

- [Skill development guide (Chinese)](docs/zh-Hans/SKILL_GUIDE.md) — manifest format, capabilities,
  security review rules, publishing steps.
- [docs/en/SKILL_GUIDE.md](docs/en/SKILL_GUIDE.md) and
  [docs/zh-Hans/SKILL_GUIDE.md](docs/zh-Hans/SKILL_GUIDE.md) — localized versions of that guide
  (added as part of the v0.2.1 documentation set).
- [docs/DEVELOPER.md](docs/DEVELOPER.md) — architecture, the stdin/stdout protocol, module map
  and extension points for commands, protocol actions and file formats.

## Privacy, audit and compliance

EchoDesk is local by design, and the policy is written down rather than implied:

- **Always local**: chat and inference, file parsing, long-document summarization, conversation
  history, snippets and UI state, and the browser actions performed by automation.
- **Network only on request**: model download (you confirm it), web search (`/search on`),
  skill marketplace queries against the GitHub API, and automation requests to the site you
  configured yourself.
- **No telemetry**: no analytics, tracking, usage statistics or crash reporting of any kind.
- **Audited automation**: the safety protocol must be accepted before automation can be enabled;
  the protocol version and consent time go into `backend\automation_log.jsonl`, which records
  what ran and which files were downloaded — file names only, never document text, model output,
  tokens, cookies or passwords.
- **Data you control**: delete `backend\session.json`, `snippets.json`, `ui_state.json`,
  `backend\models\*.gguf` or `backend\browser_state.json` at any time to remove history,
  preferences, models or stored browser sessions. Read [docs/PRIVACY.md](docs/PRIVACY.md) for the
  itemized breakdown, and [docs/EULA.md](docs/EULA.md) for the usage terms — including the point
  that model output from a small local model can be wrong and is not professional advice.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/USER_GUIDE.md](docs/zh-Hans/USER_GUIDE.md) | End-user manual: install, daily use, long contracts, batch automation, skills. |
| [docs/FAQ.md](docs/zh-Hans/FAQ.md) | Quick answers and troubleshooting for startup, models, files, automation and the marketplace. |
| [docs/DEVELOPER.md](docs/DEVELOPER.md) | Architecture, JSON protocol, module responsibilities, config reference, build and release. |
| [docs/SKILL_GUIDE.md](docs/zh-Hans/SKILL_GUIDE.md) | How to write, submit and publish a skill. |
| [docs/PRIVACY.md](docs/PRIVACY.md) | What runs locally, what may go online, log redaction rules. |
| [docs/EULA.md](docs/EULA.md) | End-user license terms for the distributed build. |
| [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md) | Complete third-party component and license inventory. |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Feature, fix and known-limitation history per version. |
| [NOTICE](NOTICE) | Attribution notice for the main bundled third-party components. |

## Contributing, security and community

Contributions are welcome — bug reports, fixes, skills, translations and documentation alike.

- [CONTRIBUTING.md](CONTRIBUTING.md) — development environment, code style, commit conventions,
  pull-request flow, the translation workflow and the skill-contribution workflow.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — the behaviour we expect in this community.
- [SECURITY.md](SECURITY.md) — supported versions, how to report a vulnerability privately,
  what to include, and our response-time commitment. Please do not open public issues for
  security problems.
- Issue templates for bug reports, feature requests, skill submissions and translation help live
  in [`.github/`](.github/); when in doubt, open an issue and include your version, operating
  system, exact steps and the relevant log (see [SECURITY.md](SECURITY.md) for log locations).

## License

EchoDesk is released under the **Apache License 2.0** — see [LICENSE](LICENSE). The license
includes an explicit patent grant and permits commercial and enterprise use, modification and
redistribution, which is why it was chosen over a shorter permissive license.

- Attribution for the main bundled third-party components: [NOTICE](NOTICE).
- Full component and license inventory: [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md).
- Model weights are **not** part of the project and remain under the license chosen by their
  publisher; confirm the terms of the model you download.
- Copyright 2026 Void Entropia Studio.
