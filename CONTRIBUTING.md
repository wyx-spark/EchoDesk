# Contributing to EchoDesk

> A Chinese reference translation of this document is available at
> [`CONTRIBUTING.zh-Hans.md`](CONTRIBUTING.zh-Hans.md); if the two ever disagree, this English version prevails.

Thanks for taking the time to help. EchoDesk is a local-first AI desktop assistant for
Windows, and every kind of contribution is useful: bug reports, fixes, documentation,
translations and new skills.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

- [Ways to contribute](#ways-to-contribute)
- [Filing an issue](#filing-an-issue)
- [Development environment](#development-environment)
- [Building and packaging](#building-and-packaging)
- [Tests](#tests)
- [Code style](#code-style)
- [Commit messages](#commit-messages)
- [Branch and pull-request flow](#branch-and-pull-request-flow)
- [Contributing a translation](#contributing-a-translation)
- [Contributing a skill](#contributing-a-skill)
- [Repository hygiene](#repository-hygiene)
- [Licensing of contributions](#licensing-of-contributions)

## Ways to contribute

| I want to... | Start here |
| --- | --- |
| Report a bug | Open a [bug report issue](https://github.com/VoidEntropiaStudio/EchoDesk/issues) (see [Filing an issue](#filing-an-issue)); include version, OS, steps and logs. |
| Request a feature | Open a [feature request issue](https://github.com/VoidEntropiaStudio/EchoDesk/issues) describing the scenario and the workflow you want. |
| Ask a question or discuss an idea | Start a thread in [GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions) — Q&A, skill sharing and translation coordination all live there. |
| Fix or improve code | Read [Development environment](#development-environment), then open a pull request. |
| Translate the UI, messages or docs | Follow [Contributing a translation](#contributing-a-translation). |
| Publish a skill | Follow [Contributing a skill](#contributing-a-skill). |
| Improve documentation | Documentation lives in `docs/` (and `docs/<locale>/` for localizations); PRs are welcome. |

## Filing an issue

All public feedback goes through this repository on GitHub. The project publishes **no support
email address, no phone number and no postal address**, so GitHub is the only channel — nothing
gets lost in a private inbox:

- **Bugs and other reproducible problems** → open an issue at
  <https://github.com/VoidEntropiaStudio/EchoDesk/issues>.
- **Questions, ideas, skill sharing and translation coordination** → start a thread in
  [GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions).
- **Code, documentation or translation changes** → open a pull request, see
  [Branch and pull-request flow](#branch-and-pull-request-flow).

Issue templates live in [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) — pick the one that
matches your report (bug, feature request, skill submission, translation). A good report lets a
maintainer reproduce the problem without guessing, so include:

- **Version**: the version shown by `/status` or in the release folder name
  (for example `v0.2.1`).
- **Environment**: Windows version and build, whether WPS Office / Microsoft Office and
  Microsoft Edge are installed, and whether you run the portable build or a build from source.
- **Exact steps**: what you clicked or typed, in order. For automation problems, include the
  selector configuration and the URL pattern (redact anything confidential).
- **Expected vs. actual behaviour**: what you expected and what happened instead.
- **Logs**: see [SECURITY.md](SECURITY.md) for log locations — the two crash logs are
  `frontend_crash.log` in the program folder and `backend_crash.log` inside `backend\`.
  Both are appended to on failure, so an empty file means "no crash recorded".

For anything that looks like a security problem, **do not open a public issue** — follow the
private reporting process in [SECURITY.md](SECURITY.md).

## Development environment

### Getting the code (SSH only)

Clone and push over SSH — this repository does not use HTTPS remote URLs:

```bash
git clone git@github.com:VoidEntropiaStudio/EchoDesk.git
cd EchoDesk
```

If you already have a working copy, check that the remote is SSH:

```bash
git remote -v
# origin  git@github.com:VoidEntropiaStudio/EchoDesk.git (fetch)
# origin  git@github.com:VoidEntropiaStudio/EchoDesk.git (push)

# If it shows https://…, switch it with one command:
git remote set-url origin git@github.com:VoidEntropiaStudio/EchoDesk.git
```

You need a key first: `ssh-keygen -t ed25519 -C "machine label"`, then add the **full
contents** of `~/.ssh/id_ed25519.pub` to GitHub under Settings → SSH and GPG keys.
`ssh -T git@github.com` should then answer
`Hi <username>! You've successfully authenticated`.
If your network blocks port 22, use 443 — see
[docs/en/DEVELOPER.md](docs/en/DEVELOPER.md), section "Version control and pushing (SSH only)".

Requirements:

- **Python 3.13** for the backend (`backend/`).
- A **.NET SDK** able to build the frontend's target framework, `net10.0-windows`
  (see `frontend/EchoDesk.csproj`).
- Optional, for automation work: **Microsoft Edge** installed locally, because the automation
  engine drives the system Edge through Playwright (`channel = msedge`).

```powershell
# Backend: create a virtual environment and install the pinned dependency set
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt

# Frontend
dotnet build frontend\EchoDesk.csproj -c Release
```

The WPF frontend starts the **packaged** backend executable (`backend\EchoDeskBackend.exe`
in a release, `dist\EchoDeskBackend\EchoDeskBackend.exe` when you build with PyInstaller).
`ECHODESK_BACKEND` overrides that path, which is the quickest loop for frontend work:

```powershell
# Build the backend once (product: dist\EchoDeskBackend\EchoDeskBackend.exe)
pyinstaller --noconfirm --distpath dist --workpath build\pyi backend\EchoBackend.spec

# Run the frontend from source against it
$env:ECHODESK_BACKEND = "$PWD\dist\EchoDeskBackend\EchoDeskBackend.exe"
dotnet run --project frontend\EchoDesk.csproj
```

The backend can also run standalone for protocol work (`python backend\main.py`): it reads one
JSON request per line on stdin and writes one JSON response per line on stdout. The action list,
response types and module map are documented in [docs/DEVELOPER.md](docs/DEVELOPER.md).

A model is only needed for actual inference: put a `.gguf` file in `backend\models\` (source runs)
or in the packaged backend's `models\` folder. Everything else — parsing, automation plumbing,
i18n, protocol — can be developed and tested without one.

## Building and packaging

```powershell
# Full portable build -> publish\EchoDesk-v0.2.2\
& tools\build_release.ps1

& tools\build_release.ps1 -SkipBackend     # only rebuild the WPF frontend
& tools\build_release.ps1 -SkipFrontend    # only rebuild the packaged Python backend
& tools\build_release.ps1 -SkipClean -SkipBackend -SkipFrontend   # re-run resource/metadata steps
& tools\build_release.ps1 -IncludeModels   # include local .gguf models (large offline bundle)
```

Two things about the build are easy to get wrong, and both are commented in the scripts:

- `rcedit` must **not** touch PyInstaller output (it rebuilds the PE image and destroys the
  appended PKG archive); the backend writes its version resource and icon through the spec instead.
- PowerShell can treat a native command's stderr as a terminating error, which used to abort
  builds on a warning; `tools\build_release.ps1` wraps native calls for that reason.

Please also keep an eye on **package size**: EchoDesk ships a self-contained frontend plus a
PyInstaller backend, so a new dependency that is large — or that pulls in a large transitive tree —
is a real cost. Prefer the standard library, prefer small permissively licensed libraries, and
mention the size impact of a new dependency in your pull request.

## Tests

```powershell
# Required checks for any pull request
python tools\selftest.py             # backend unit-level self-test (exit code 0 = pass)
python tools\test_file_parsers.py    # smoke test for every supported file format

# Additional suites, depending on what you touched
python tools\test_plaintext.py       # Markdown -> plain-text conversion (result files)
python tools\test_results_dir.py     # automation result folder layout and result.txt
python tools\test_automation_e2e.py  # end-to-end automation against a local static site (needs Edge + Playwright)
python tools\test_model_hub_e2e.py   # model-hub protocol against the packaged backend
python tools\test_reasoning.py       # thinking/reasoning split: tag edge cases, protocol channels, history
powershell -File tools\ui_probe.ps1  # WPF UI probe: settings window opens fast, no crash log
python tools\gen_licenses.py         # regenerate docs\THIRD_PARTY_LICENSES.md
```

If a test prints a skip message because your machine lacks a model, Edge or the packaged backend,
say so in the pull request instead of leaving it unmentioned.

## Code style

**Python (backend, tools)**

- 4-space indentation; no tabs.
- Type hints on function signatures (`def parse(path: str) -> List[str]:`), and
  `from __future__ import annotations` where a module already uses it.
- **Docstrings and comments are written in Chinese** — that is the existing convention across
  `backend/` and `tools/`, and it should be kept so the codebase stays consistent. Module
  docstrings explain *why* a design was chosen, which is the most valuable part of this codebase.
- Keep user-visible strings out of the code path where a language pack entry can carry them
  (see [Contributing a translation](#contributing-a-translation)).
- Fail soft: the backend must never let a single bad file, skill or automation task take down the
  process. Catch, log and continue.

**C# / XAML (frontend)**

- File-scoped namespaces (`namespace EchoDesk.Services;`), nullable reference types are enabled
  (`<Nullable>enable</Nullable>`) and implicit usings are on.
- No third-party NuGet packages: the frontend uses `System.Text.Json` for the protocol.
  Please keep it that way.
- UI text follows the same localization rules as the backend; long-running work must not block
  the UI thread, and settings-window work loads asynchronously after the window is shown.

**Markdown and docs**

- One sentence per line is not required, but keep tables and fenced code blocks clean.
- Use fenced code blocks with a language tag (`powershell`, `jsonc`, `text`).
- No tabs, no trailing whitespace, and relative links with forward slashes.
- Do not commit screenshots that contain real customer data, file paths or credentials.

## Commit messages

The project uses Chinese, conventional-ish prefixes, optionally with a task number and a
priority tag, followed by a full-width colon and a description of the change:

```text
修复一+修复二 P0：设置窗口卡死（空引用致进程终止）与命令栏 Enter 误执行
新增四 P1：模型选择栏（HF 镜像/官方双源搜索 + 详情 + 下载入库 + 模型库管理）
优化五 P2：状态栏信息分层（顶部精简 + 输入框下方可折叠轻量状态区）
配置：config.json 补齐 automation 与 marketplace 配置段
构建脚本：新增 .gitattributes 固定换行符
交付：v0.2.0 修复记录 + 变更日志 + 发布物纳入 docs 与最终实测
```

- Prefixes in use: `新增` / `修复` / `优化` / `配置` / `构建脚本` / `交付`, plus `任务N`
  when the change tracks a numbered task in the version's task book.
- Priority tags `P0`–`P3` are used for larger changes; `P0` means data loss or crash risk.
- Keep the subject in one line and describe the user-visible effect, not just the file touched.
- English subjects are acceptable for documentation-only or tooling changes, but prefer staying
  consistent with the surrounding history.

## Branch and pull-request flow

1. Fork the repository (or create a branch directly if you have write access) and branch off
   `master`, for example `fix/settings-window-focus` or `docs/en-skill-guide`.
2. Keep a pull request focused on one topic. Unrelated refactors make the review — and any
   possible revert — much harder.
3. Run the required tests from [Tests](#tests) and state the results in the description.
4. In the pull request description, explain: what changed, why, how you verified it, and whether
   the change affects package size or the third-party license inventory (a new dependency means
   `docs/THIRD_PARTY_LICENSES.md` must be regenerated).
5. Use `draft` pull requests freely while you iterate; mark them ready when the tests pass.
6. Maintainers review, may ask for changes, and then merge. Small, well-explained pull requests
   are reviewed fastest.

Never commit models, build output, user data or credentials — see
[Repository hygiene](#repository-hygiene).

## Contributing a translation

EchoDesk's localization is language-pack based and designed so a translation is a self-contained
contribution. A locale consists of these files:

| File | Contents |
| --- | --- |
| `backend/lang/<locale>.json` | Backend system messages, error texts and command help. |
| `frontend/Resources/Lang/<locale>.json` | UI strings: labels, buttons, menus, dialogs, tooltips. |
| `docs/<locale>/` | Localized documentation (README, user guide, FAQ, automation guide). |

Rules and workflow:

1. **Pick a locale tag.** Use a BCP 47 style tag that matches the existing files:
   `zh-Hans`, `en`, `zh-Hant`, `ja`, `ko`, `de`, `fr`, `es`, `pt-BR`.
   `zh-Hans` is the source of truth for key coverage; `en` is the first fallback.
2. **Extract the strings to translate.** Run

   ```powershell
   python tools\extract_strings.py --scaffold <locale>
   ```

   which creates the scaffold files for the new locale from the source language. Keys are
   semantic (`settings.automation.task_url.label`), and an entry may carry a comment describing
   its context and placeholders — please keep those comments so later translators keep the
   context too.
3. **Translate the values only.** Do not rename keys, reorder sections, or translate
   placeholders such as `{count}` / `{path}`; code substitutes them verbatim.
4. **Check completeness** before opening the pull request:

   ```powershell
   python tools\check_lang.py --locale <locale>
   ```

   It reports missing keys and keys that no longer exist upstream. The runtime falls back
   `requested locale → en → zh-Hans → the key itself`, so missing keys are visible rather than
   silent, but a complete pack is still what we want to merge.
5. **Test it in the app.** Set `language` in `backend\config.json` to your locale (or switch it in
   Settings) and check that the UI updates immediately, that error messages and `/help` follow the
   language, and that no string overflows its cell or button — CJK and German text in particular
   need a width check.
6. **Open a pull request** titled like `新增 i18n：<locale> 语言包（界面 + 后端 + 文档）` and list
   which files you added and which parts are still missing or machine-assisted.
7. Machine translation is an acceptable starting point, but say so in the pull request, and expect
   a review pass by a native speaker before the locale is advertised as supported.

Localized documentation belongs in `docs/<locale>/`. Keep code snippets and command names in
English; translate the surrounding explanation.

## Contributing a skill

A skill is a folder with a `skill.json` manifest plus an entry package — no changes to EchoDesk
itself are needed, which makes skills the easiest way to extend the app:

1. Read the skill development guide: [docs/zh-Hans/SKILL_GUIDE.md](docs/zh-Hans/SKILL_GUIDE.md)
   (Chinese) and [docs/en/SKILL_GUIDE.md](docs/en/SKILL_GUIDE.md) (English). Both versions cover
   the manifest format, the capability declarations and the review rules.
2. Declare only the capabilities you actually use (`commands`, `ui_panels`, `automation_hooks`,
   `file_parsers`), state your license, and keep the manifest honest about network access.
3. Test locally before publishing: run the skill from the local plugin path, verify that its
   commands appear and execute, and confirm that a failing skill produces a log entry instead of
   taking down the app.
4. Publish it on GitHub with a `README.md`, and add the topic **`echodesk-plugin`** to the
   repository. EchoDesk only discovers repositories carrying that topic, and a repository whose
   root `skill.json` lacks `echodesk.bundle` is shown as unverified and cannot be installed.
5. Optionally open a skill-submission issue using the template in [`.github/`](.github/) so the
   maintainers can review the permission declarations and, if everything looks right, mention it
   in the release notes.

Skills run with your user's privileges. EchoDesk does not auto-execute packaged `prepare` scripts,
and it strips environment variables whose names contain `KEY`, `SECRET`, `TOKEN` or `PASSWORD`
before any such execution — do not design a skill that depends on secrets being present in the
environment.

## Repository hygiene

The following must never be committed (`.gitignore` already covers most of them):

- Model files (`*.gguf`) and the `publish/`, `build/`, `dist/` output trees.
- User data: `session.json`, `snippets.json`, `ui_state.json`, `browser_state.json`,
  `automation_log.jsonl`, `results/`, `skills/`,
  `skills_registry.json`, `models_registry.json`, `market_cache.json`.
- Crash logs (`frontend_crash.log`, `backend_crash.log`).
- Any file containing a token, password or personal path — including a real GitHub token in
  `backend\config.json` (`marketplace.github_token` must stay empty in the repository; put personal
  values in `backend\config.user.json`, which is ignored).
- Screenshots or logs containing real documents, customer names or internal URLs.

Before opening a pull request, run `git status` and confirm that no unexpected file is staged.
Keep the existing `.gitattributes` line-ending rules in place: `.ps1`/`.bat`/`.cmd` are CRLF,
everything else is LF. Do not reformat whole files for whitespace-only changes.

## Licensing of contributions

EchoDesk is released under the [Apache License 2.0](LICENSE), and contributions are accepted under
the same license (inbound = outbound); no separate contributor license agreement is required.
By opening a pull request you confirm that you wrote the contribution or have the right to submit
it, and that it does not introduce a dependency whose license conflicts with Apache-2.0.

If your change adds, removes or upgrades a dependency, regenerate the inventory
(`python tools\gen_licenses.py`) and say so in the pull request, so
[NOTICE](NOTICE) and [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md) stay accurate.
