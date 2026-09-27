# EchoDesk Developer Documentation

Chinese version: [`docs/zh-Hans/DEVELOPER.md`](../zh-Hans/DEVELOPER.md).
Related documents: [Automation guide](AUTOMATION_GUIDE.md) · [User guide](USER_GUIDE.md) · [FAQ](FAQ.md) · [Skill development guide](SKILL_GUIDE.md).

This document is for frontend, backend and skill developers. It explains the architecture, the communication
protocol, the module layout, the build process and how to extend the program.

---

## 1. Overall architecture

```
┌─────────────────────── app\EchoDesk.exe (C# WPF, net10.0-windows) ───────────────────────────────────────────────────────────────────────────────────────────────────────┐
│  MainWindow                    main chat UI: message list, input area, status bar, notification bar, shortcuts                                                           │
│  SettingsWindow                settings window: General / Model / Automation / Installed / Skill market / Developer                                                      │
│  SettingsWindow.xaml.cs→       talks to the backend through BackendService                                                                                               │
│  Services/BackendService.cs    child process management + JSON line protocol client (with a request gate, serialised stdout reads)                                       │
│  Services/ShortcutService.cs   directory layout (program directory / user data directory) + launch shortcut detection/creation                                           │
│  Services/ThemeService.cs      three-state theme (follow system/light/dark) and palette switching (fix 2)                                                                │
│  Services/Localization.cs      multi-language: language pack loading, fallback chain, Loc.Key attached-property fill-in                                                  │
│  Services/Emoji.cs             emoji fallback (environments that do not support it switch to ASCII markers, fix 6)                                                       │
│  Services/AppSettings.cs       ui_state.json: window state, font size, theme, language, emoji policy                                                                     │
│  Utilities/MarkdownRenderer.cs Markdown → FlowDocument rendering (brushes go through palette resource references)                                                        │
│  Utilities/TrustDialog.cs      local plugin batch trust confirmation dialog (fix 1)                                                                                      │
│  Utilities/SnippetManager.cs   canned phrase templates (snippets.json)                                                                                                   │
└───────────────────────────────┬──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                │ stdin/stdout: one JSON object per line (UTF-8, no BOM)
┌───────────────────────────────┴──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                    app\backend\EchoDeskBackend.exe (Python 3.13 + PyInstaller onedir)                                                                                    │
│  main.py                       main loop: read stdin → dispatch; long tasks produce streaming responses on a background thread (supports stop interruption)              │
│  config.py                     configuration model (pydantic): window ceiling validation, segmented summarisation, automation, skill market                              │
│  model_profile.py              model capability profile (parameter count/quantisation/window/KV/speed), capability hints, hardware recommendation, summary cache (fix 4) │
│  prompts.py                    task-oriented prompt templates (role/output format/chain of thought/few-shot) and vague-question detection (fix 4)                        │
│  emoji.py                      emoji output capability detection and ASCII fallback (fix 6)                                                                              │
│  i18n.py                       multi-language: language pack loading, fallback chain, placeholder tolerance, external language pack priority                             │
│  llm_engine.py                 llama.cpp wrapper: window/training window logging, KV Cache estimation, streaming generation and speed statistics                         │
│  gguf_meta.py                  lightweight GGUF metadata parsing (read the training window, layer count and KV head count without loading the model)                     │
│  summarizer.py                 long-document map-reduce segmented summarisation (chunking/overlap/per-chunk summary/recursive merge/source pointers)                     │
│  conversation.py               conversation history, modes, file context, session persistence                                                                            │
│  command_handler.py            / command parsing and dispatch (including external commands registered by skills)                                                         │
│  file_parser.py                file parsing: WPS COM first (can read encrypted documents) → built-in lightweight parsers                                                 │
│  skills_runtime.py             skill runtime loader: discovery/priority/trust/unload/panel/hooks (task 2)                                                                │
│  download_utils.py             model repository query and download (hf-mirror mirror)                                                                                    │
│  search_engine.py              DuckDuckGo web search                                                                                                                     │
│  automation.py                 browser automation driving Edge through Playwright (headed mode)                                                                          │
│  marketplace.py                GitHub topic skill market: search/detail/install/uninstall (cache + offline fallback)                                                     │
│  update_check.py               automatic update check (plan A: check and notify only, no download or replacement)                                                        │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Why a child process with stdin/stdout rather than a local HTTP service**:
it takes no port, triggers no firewall prompt and needs no authentication handling; the cost is that you have to
guarantee message boundaries yourself (one JSON object per line) and concurrency safety (the backend guards
stdout with `_io_lock`, and the frontend serialises reads behind a request gate).

### 1.1 System prompt layers and length budget (v0.2.1 prompt optimisation)

The system prompt is assembled in the following order (see `MODES` / `_CORE_RULES` /
`_REPLY_LANGUAGE` and `get_system_prompt()` in `conversation.py`):

| Layer | Content | Who writes it | Relation to the other layers |
| --- | --- | --- | --- |
| 1 | **Custom instructions** | the user, through `/prompt <text>` | highest priority: **replaces** layers 2 and 3 |
| 2 | **Mode block** | `conversation.MODES` (6 modes) | scenario and tone; replaced by layer 1 |
| 3 | **Task layer** | `prompts.TASK_TEMPLATES` (`/task`) | **stacked** after layer 2; it wins on conflict (the template ends with a `[优先级]` line) |
| 4 | **Shared core** | `conversation._CORE_RULES` | perspective / boundaries / output discipline, in effect under any combination |
| 5 | **Language instruction** | `conversation._REPLY_LANGUAGE` | follows the **interface language** (the current `i18n` locale) |
| 6 | Session context | `【早期对话摘要】` / `【已上传文件】` | determined by `summary` and the file state |

**The six elements**: every mode block carries five items of its own — "role / output format / chain of thought /
few-shot example / tone" — and the sixth, "boundaries" (if you do not know, say you do not know; do not invent
sources; when the information is insufficient, ask about the single most important point first), sits in the
shared core: it has nothing to do with the scenario, so writing it 6 times both wastes context for nothing and
means a few places get missed whenever it is changed.

**Length budget and rationale** (guarded by the assertions in section 9 of `tools/test_capability.py`):
a single mode block ≤ 480 characters (measured 209~313), and mode block + shared core + language instruction
≤ 700 characters (measured 417~521). The default context is 8192 tokens, and Chinese is at the scale of about
1 character per token, so 700 characters take roughly 8.5%, leaving the remainder for file summaries and
conversation history; models in the 0.8B class are especially sensitive to long prompts, so the rules are shared
rather than padded out.

**Language division of labour**: the **body** of the prompt stays in Chinese and does not go into a language pack
(translating the body into English would weaken wording designed around a small model going off track); the
**reply language** is decided by layer 5 following the interface language — a Chinese interface asks for
Simplified Chinese, an English interface asks for English, and a language that is not built in asks to "follow the
language of the question". The review-opinion prompt in `automation.py` hard-coding Simplified Chinese is an
**independent business decision** (review opinions are written into `results\` for the record), as noted in the
comments.

---

## 2. Communication protocol

### 2.1 Basic conventions

- Encoding: **UTF-8** (on startup the backend forces `sys.stdout`/`stderr` to UTF-8 with
  `errors="replace"`, to avoid a cp1252 encoding failure under an English locale making the process exit
  silently — this was a real failure in v0.1.0).
- Direction: frontend → backend (stdin), one request object per line; backend → frontend (stdout), one
  response object per line.
- Every response carries a `type` field: `ready` / `system` / `assistant` / `delta` / `done` / `error` / `need_download` / `context`.
- Lines that are not JSON (diagnostics a third-party library printed to stdout by mistake) are ignored by
  the frontend.

### 2.2 Startup handshake

The backend prints this only after **model loading + session restore + warm-up have all finished**:

```json
{"type":"ready","version":"v0.2.0","studio":"Void Entropia Studio","restored":3,"history":[{"role":"user","content":"..."}]}
```

The frontend releases input only once it reads `ready`. If the local model is missing before startup:

```json
{"type":"need_download","model":"Qwen3.5-0.8B-Q4_K_M.gguf","size":532517120,"repo":"unsloth/Qwen3.5-0.8B-GGUF","current":"...","content":"..."}
```

The frontend shows a confirmation dialog; on agreement it sends `model_download` (the backend downloads →
continues initialisation → finally prints `ready`), and on refusal or on stdin closing the backend exits
straight away.

### 2.3 Request actions at a glance

| action | Description | Response form |
| --- | --- | --- |
| `chat` | non-streaming: an ordinary question or a `/` command | one `assistant` or `system` line (with `used`/`limit`) |
| `chat_stream` | streaming conversation | several `delta` lines + one `done` line (with `used`/`limit`) |
| `stop` | interrupt the current streaming generation / automation | one `system` line |
| `file` | upload and parse a file (segmented summarisation automatically when too long) | one `system` line |
| `context` | query context usage | one `context` line + `used`/`limit` |
| `model_download` | download a model and hot-swap to it | several `delta` progress lines + `done` |
| `automation_config` | read the automation configuration | `system` + an `automation` object |
| `automation_config_set` | save the automation configuration | `system` + the normalised `automation` |
| `automation_test` | test selectors (open a browser to preview the matches) | one `system` line (with the match count and samples) |
| `automation_run` | run automation once | several `delta` progress lines + `done` (with `processed`/`skipped`/`failed`/`results`) |
| `automation_status` | automation run status | `system` + `automation` |
| `market_search` | search skills | `system` + `market` (items/pagination/offline) |
| `market_detail` | skill details | `system` + `detail` |
| `market_install` | install a skill | one `system` line |
| `market_uninstall` | uninstall a skill | one `system` line |
| `market_installed` | list of installed skills | `system` + an `installed` array |
| `market_status` | market connection status | `system` + `market_status` |

### 2.4 Example

```jsonc
// frontend → backend
{"action":"chat_stream","message":"用一句话说明什么是回声。"}
// backend → frontend
{"type":"delta","content":"回声是"}
{"type":"delta","content":"声音在传播"}
{"type":"done","content":"回声是……","used":138,"limit":6553}
```

---

## 3. Backend module notes

### 3.1 Context window (task 1.1)

- `context_length` defaults to **8192** (4096 in v0.1.0); `max_context` is the hard ceiling (default 32768).
- Actual window = `min(requested value, model training window, max_context)`, with a floor of 512;
  when `auto_context=false` the training-window term is ignored.
- The model training window is taken from **GGUF metadata** first (`gguf_meta.py` parses only the header KV
  section, without loading the model), and falls back to keyword matching against the
  `config.model_context_windows` registry when it cannot be obtained.
- On load/reload it prints a window and KV Cache estimation log:
  `上下文窗口: 8192 tokens（模型训练窗口 262144 tokens，KV Cache 约 384.0 MB，线程 16，GPU 层 0）`
- KV Cache estimation formula: `n_ctx × n_layer × n_head_kv × (key_dim + value_dim) × 2 bytes` (fp16).

### 3.2 Long-document segmented summarisation (task 1.1)

Trigger: the file's token count > window × `summarize.trigger_ratio` (default 0.5) and its character count
≥ `min_chars`. `/summarize` can also force it by hand.

Flow (`summarizer.py`, map-reduce):
1. split by `chunk_chars`, keeping an `overlap_chars` overlap between chunks, with split points preferably at
   paragraph/period boundaries;
2. summarise each chunk (`max_summary_tokens`); the prompt requires numbers/dates/amounts/parties/obligations/
   conclusions to be preserved;
3. when the chunk summaries together exceed `merge_max_tokens`, group them by budget and summarise again,
   recursively until within budget (depth limit 4 plus a truncation fallback);
4. the output carries **source pointers**: the source file name plus the source character range for each chunk,
   so it can be traced back.

At injection time the summary replaces the full text (`conversation.build_messages` checks `file_summary`), and
the summarisation progress goes to stderr, where the frontend displays it live in the conversation window (a long
task never "looks stuck").

### 3.3 File parsing (v0.2.0 rewrite)

WPS COM first (it can read encrypted Office documents), falling back to the built-in parsers on failure:

| Extension | Implementation | Licence |
| --- | --- | --- |
| .docx | python-docx (paragraphs + tables) | MIT |
| .xlsx/.xlsm | openpyxl (sheet by sheet, row by row) | MIT |
| .pptx | python-pptx (text boxes + tables per slide) | MIT |
| .pdf | PyMuPDF (text layer) | AGPL/commercial |
| .xls | xlrd | BSD |
| .txt/.md/.csv and others | built-in multi-encoding detection (utf-8-sig→utf-8→gb18030→utf-16→big5→latin-1) | — |

**Why pyxtxt is no longer used** (removed in v0.2.0):
its `estrattori` subpackage imports every parser in the same directory automatically inside `__init__` with
`os.listdir`, so packaging had to collect the whole directory (otherwise startup fails immediately with
`FileNotFoundError`), and collecting the whole directory drags in
easyocr→torch/torchvision/scipy/cv2/skimage/numba, blowing the backend size up from ~178MB to 976MB; its
dependency chain also contains GPL/LGPL components, and distributing it externally creates copyleft obligations.
See `docs/THIRD_PARTY_LICENSES.md` for details.

### 3.4 Automation (task 4)

`automation.py` drives the local Edge (`channel="msedge"`) with Playwright's **sync API**, and **forces headed
mode** (the user can see the operations; a `headless` setting in the configuration is corrected to False by the
validator). The engine is a generator: `run(stop_event, on_progress)` yields `delta`/`done`/`error` events one at
a time, and `main.py` forwards them to the frontend unchanged, so progress is visible in real time and `stop` can
interrupt it.

**Sign-in state (v0.2.1 automation optimisation, task 1)**: `launch_persistent_context` opens a **persistent user
data directory** (`automation.login_state_dir`, default `browser_profile`, a sibling of `browser_state.json`), and
cookies / localStorage are written to disk by the browser itself and reused automatically on later runs. It
**returns a context rather than a browser** (and `context.browser` is always None), so the three steps
"launch → create context → close" are wrapped up in `_BrowserSession`, and callers only touch that one object.
`login_state_file` (`browser_state.json`) degrades into a backup/migration file: cookies are imported once when
the new directory is first used (so existing users need not sign in again after upgrading), and exported once
more at the end of every session. `_login_present()` decides in order of reliability: `login_check_selector` (an
element that appears only after signing in) → `login_url_keywords` (URL keywords) → returning `None`, meaning
"cannot tell, do not block". `clear_login_state()` refuses to run while a run is in progress (the browser holds
the files).

**href extraction (task 2)**: a selector is only responsible for **locating the element**; reading attributes is a
separate step. `_classify_href()` normalises the raw attribute value into `_HrefInfo(raw, url, kind)`, where
`kind` ∈ `absolute` / `relative` / `javascript` / `fragment` / `empty` / `other`; the four cases where `click_only`
is true (pseudo-links, in-page anchors, empty href, non-navigable protocols) are made to **fall back to clicking
the element** by `_click_into()`, which also catches a new tab or a navigation in the current page.
`_collect_links()` deduplication is corrected accordingly to **deduplicate by URL only for elements that actually
yield a URL** — deduplicating by `href` used to collapse the dozens of `href="javascript:void(0)"` tasks on one
page into one.

**Visual picking (task 3)**: `PICKER_JS` is an arrow function injected into the page (the banner text is passed in
by the backend according to the interface language, so the script no longer hard-codes Chinese); on click it
reports back `isLink` / `hasHref` / `linksInside`; `_preview_pick()` uses that to produce a preview of "the program
will extract the following URLs from these elements: …", to block non-link elements where a link is required
(returning `blocking`, and the caller sends no `done` event and writes no configuration), and to explain that
pseudo-links will switch to a click.

Safety design:
- `enabled` is only permitted when `safety_accepted` is true (enforced by the `config.AutomationConfig` validator);
- the protocol version and the consent time go into the audit log;
- downloaded files go to the system temporary directory and are **deleted immediately once each task is
  processed**;
- the audit log (JSONL) is redacted: only file names are recorded, never body text, never tokens/cookies, and
  sensitive keys and values are replaced with `***`; login-state events (`login_state_used` / `login_required` /
  `login_completed` / `login_timeout` / `login_state_cleared` / `login_state_migrated`) record only the path, the
  result and counts, and **never the cookie contents**.

### 3.5 Skill market (task 5)

`marketplace.py` has three layers: data source (GitHub Search API + local cache) → service layer
(search/detail/install/uninstall) → the frontend market tab.

- **Skills are discovered only from the topic `echodesk-plugin`**, and arbitrary URLs are not accepted;
  `install`/`detail` accept only the `owner/name` form, with regex validation.
- Cache `market_cache.json`, TTL `cache_ttl_hours`; on network failure or API rate limiting it returns the cached
  snapshot and flags `offline`.
- Repository verification: a `skill.json` in the repository root containing an `echodesk.bundle` field →
  `verified=true`; otherwise the card shows "unverified" and the install button is greyed out.
- Install: download the tarball (limit 50MB, rejecting members containing `..`/absolute paths) → validate
  skill.json → copy to `skills/<id>` → register in the registry (with sha256, license, dependencies) → clean up
  the temporary directory → read back for verification.
- **The prepare script is not executed automatically**; `_sanitized_env()` removes environment variables whose
  names contain KEY/SECRET/TOKEN/PASSWORD.

---

## 4. Configuration reference (backend\config.json)

```jsonc
{
  "model_path": "Qwen3.5-0.8B-Q4_K_M.gguf",
  "model_repo": "unsloth/Qwen3.5-0.8B-GGUF",
  "hf_endpoint": "https://hf-mirror.com",   // mainland mirror; leave empty to use the official endpoint
  "context_length": 8192,                   // requested context window
  "max_context": 32768,                     // hard ceiling (used for validation)
  "auto_context": true,                     // whether to converge to the model's training window
  "model_context_windows": { "qwen3.5-0.8b": 32768 },  // keyword → training window
  "summarize": {
    "enabled": true, "trigger_ratio": 0.5, "chunk_chars": 3000, "overlap_chars": 200,
    "max_summary_tokens": 400, "merge_max_tokens": 800, "model": "", "min_chars": 4000,
    "keep_pointer": true
  },
  "file_max_chars": 200000,                 // 0 = no limit
  "speed_hint": true, "speed_min_tps": 3.0,
  "automation": { /* see backend/config.json; includes the safety protocol and the selectors */ },
  "marketplace": { "skill_topic": "echodesk-plugin", "cache_ttl_hours": 6, "github_token": "" }
}
```

**Configuration override mechanism**: fields in `config.user.json` in the same directory take precedence over
`config.json`; the application only writes back to `config.json`, so handwritten user patches are never
overwritten. (`config.user.json` is already in `.gitignore`, and is a good place for personal settings such as a
GitHub Token.)

---

## 5. Building and releasing

```powershell
# 1) generate the icon (re-run after changing the colour scheme)
python tools\make_icon.py

# 2) download the build tools (rcedit, not committed to the repository)
cmd /c tools\fetch_tools.bat

# 3) one-shot release (multi-file frontend + onedir backend + metadata + installer + SHA256SUMS)
& tools\build_release.ps1 -IncludeModels -MakeInstaller

# when migrating models/configuration from the previous release
& tools\build_release.ps1 -IncludeModels -MigrateFrom publish\EchoDesk-v0.1.0

# repack one side only
& tools\build_release.ps1 -SkipBackend                            # rebuild the frontend only
& tools\build_release.ps1 -SkipFrontend                           # rebuild the backend only
& tools\build_release.ps1 -SkipClean -SkipBackend -SkipFrontend   # re-run only asset collection/metadata

# repack the two installers only (once a release directory has been built)
& tools\make_installer.ps1
```

**Installer (Inno Setup 6)**: the two deliverables `EchoDesk-v<version>-Setup.exe` (with models) and
`EchoDesk-v<version>-NoModel-Setup.exe` (without models) are compiled from `tools\installer.iss`, and the actual
caller is `tools\make_installer.ps1` (feasibility check + compile + integrity assertions + optional signing +
SHA256). The default install location is **the folder the installer itself sits in** (`[Code] GetDefaultDir`
returns `ExtractFileDir(ExpandConstant('{srcexe}'))`), and it can still be changed in the wizard; shortcuts are
created by the `[Icons]` section with the working directory fixed to `{app}` (the outer layer), so that `results\`
and user data are produced in the outer layer rather than inside `app\`. Inno Setup 6 is required (the script
detects it automatically, or you can point at it with `-ISCC`).

**Three pitfalls in the build script (already commented in the code)**:
1. **`rcedit` must not be applied to PyInstaller output** — PyInstaller appends the PKG archive after the PE
   image, and rcedit rebuilding the PE loses that data, so the backend fails at startup with
   `Could not load PyInstaller's embedded PKG archive`. Backend metadata is therefore written with the spec's
   `version`/`icon` parameters before the archive is appended, and rcedit handles only the frontend exe.
2. **`rcedit` must likewise not be applied to the final Inno Setup installer** — Inno appends the install data
   after the PE image (measured: the last PE section of a minimal installer ends at byte 893,952 while the file
   totals 2,097,164 bytes), and rcedit drops that entire 1.2 MB of appended data, producing an 893,952-byte hollow
   shell that fails as soon as it runs. Use the `[Setup] VersionInfo*` directives for the installer's version
   resource (`signtool` is safe).
3. Under `ErrorActionPreference=Stop`, PowerShell treats a native command's stderr as a terminating error, so
   PyInstaller/dotnet abort the build on a mere warning — wrap them all in `Invoke-Native`.

**Tests**:
```powershell
python tools\test_file_parsers.py     # smoke test for parsing six formats (exit code 0/1)
python tools\gen_licenses.py          # regenerate the third-party licence list
```

---

## 6. Extension points

- **New commands**: register in `command_handler.py`'s `_handlers` plus `HELP_TEXT`, and keep the `Commands`
  array in the frontend `MainWindow.xaml.cs` in sync (only then are they visible in the command panel and in
  `/help`).
- **New protocol actions**: dispatch them in `main.py`'s `handle()` (one-shot) or `stream()` (streaming); add the
  corresponding method to `BackendService` on the frontend. Send long tasks through `stream()` so they reuse stop
  interruption and progress reporting.
- **New file formats**: add a `_read_xxx` in `file_parser.py` and dispatch it in `_parse_builtin`, add the
  extension to `_TEXT_EXTS` or to a new branch, and update the Filter of the frontend file dialog.
- **Skill development specification**: see `docs/zh-Hans/SKILL_GUIDE.md` (Chinese) and `docs/en/SKILL_GUIDE.md`
  (English).
