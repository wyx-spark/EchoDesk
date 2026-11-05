# EchoDesk Developer Documentation

Chinese version: [`docs/zh-Hans/DEVELOPER.md`](../zh-Hans/DEVELOPER.md).
Related documents: [Automation guide](AUTOMATION_GUIDE.md) · [User guide](USER_GUIDE.md) · [FAQ](FAQ.md) · [Skill development guide](SKILL_GUIDE.md) · [Cross-platform reference](CROSS_PLATFORM.md).

This document is for frontend, backend and skill developers. It explains the architecture, the communication
protocol, the module layout, the build process and how to extend the program.

**How commands are written here**: the cross-platform form comes first (`python`, `git`, forward slashes),
and only genuine differences are expanded with a platform label (virtual-environment activation,
environment variables, the Windows-only release chain). Windows, macOS and Linux are all supported; the
capability matrix is in [CROSS_PLATFORM.md](CROSS_PLATFORM.md).

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
│  retrieval.py                  local document retrieval: BM25 + chunking, character-range traceability, incremental index (task 6)                                       │
│  conversation.py               conversation history, modes, file context, session persistence                                                                            │
│  agent.py                      agent loop: multi-step tool calls, step limit, event stream, interruption (task 2)                                                        │
│  agent_tools.py                tool layer: Tool/ToolRegistry, built-in tool set, permission tiers safe/sensitive/guard                                                   │
│  command_handler.py            / command parsing and dispatch (including external commands registered by skills)                                                         │
│  file_parser.py                file parsing: WPS COM first (can read encrypted documents) → built-in lightweight parsers                                                 │
│  skills_runtime.py             skill runtime loader: discovery/priority/trust/unload/panel/hooks (task 2)                                                                │
│  mcp_client.py                 MCP stdio client and tool bridge (in-house, no new dependencies; task 3)                                                                  │
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

**Two frontends, one backend.** The diagram above is the WPF frontend (`frontend/`, target framework
`net10.0-windows`, Windows only). The cross-platform frontend lives in
`frontend-avalonia/EchoDesk.Avalonia/` (target framework `net10.0`, Windows + macOS + Linux) and drives the
same backend over the same protocol. Both are run from a source check-out with the same .NET CLI — only the
WPF project is Windows-only:

```bash
# all three platforms: cross-platform (Avalonia) frontend; it finds backend/main.py by walking up from bin/
dotnet run --project frontend-avalonia/EchoDesk.Avalonia/EchoDesk.Avalonia.csproj

# Windows only: WPF frontend
dotnet run --project frontend/EchoDesk.csproj
```

The backend runs on all three platforms (`python backend/main.py`); which capability is available where is
listed in [CROSS_PLATFORM.md](CROSS_PLATFORM.md) section 2.

### 1.1 System prompt layers and length budget (v0.2.1 prompt optimisation)

The system prompt is assembled in the following order (see `MODES` / `_CORE_RULES` /
`_REPLY_LANGUAGE` and `get_system_prompt()` in `conversation.py`):

| Layer | Content | Who writes it | Relation to the other layers |
| --- | --- | --- | --- |
| 1 | **Custom instructions** | the user, through `/prompt <text>` | highest priority: **replaces** layers 2 and 3 |
| 2 | **Mode block** | `conversation.MODES` (three modes: chat / contract / agent) | scenario and tone; replaced by layer 1 |
| 3 | **Task layer** | `prompts.TASK_TEMPLATES` (`/task`) | **stacked** after layer 2; it wins on conflict (the template ends with a `[优先级]` line) |
| 4 | **Shared core** | `conversation._CORE_RULES` | perspective / boundaries / output discipline, in effect under any combination |
| 5 | **Language instruction** | `conversation._REPLY_LANGUAGE` | follows the **interface language** (the current `i18n` locale) |
| 6 | Session context | `【早期对话摘要】` / `【已上传文件】` | determined by `summary` and the file state |

**The six elements**: every mode block carries six items of its own — "role / output format / **how much
detail to give** / chain of thought / few-shot example / tone" (`contract` and `agent` add one
scenario-specific "boundaries" item on top). The discipline that has nothing to do with the scenario
(if you do not know, say you do not know; do not invent sources; give the most likely answer before asking;
offer 2-3 directions when the question is vague, …) sits in the seven rules of the shared core `_CORE_RULES`:
writing it 3 times both wastes context for nothing and means a few places get missed whenever it is changed.

**Length budget and rationale** (guarded by the assertions in section 9 of `tools/test_capability.py`):
a single mode block ≤ 480 characters (measured 243~335), and mode block + shared core + language instruction
≤ 700 characters (measured 607~699). The default context is 8192 tokens, and Chinese is at the scale of about
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
- Every response carries a `type` field: `ready` / `system` / `assistant` / `delta` / `done` / `error` / `need_download` / `summary_progress` / `context`.
- Lines that are not JSON (diagnostics a third-party library printed to stdout by mistake) are ignored by
  the frontend.

### 2.2 Startup handshake

The backend prints `ready` only after **session restore + warm-up have finished**:

```json
{"type":"ready","version":"v0.2.1","studio":"Void Entropia Studio","restored":3,
 "model_ready":true,"model_state":{"ready":true,"model":"Qwen3-0.6B-Q6_K.gguf","models_dir":"...","n_ctx":8192,"guide":""},
 "model_missing":{},"history":[{"role":"user","content":"..."}]}
```

> The `studio` field in the protocol is **always the English `Void Entropia Studio`** (the
> legal-entity identifier, like the rcedit metadata and the SBOM — it does not follow the UI
> language). What the Chinese UI shows comes from the language pack
> (`main.studioName` = 熵寂工作室, `main.studioNameFull` = 熵寂工作室（VoidEntropiaStudio）);
> the backend's `/help`, `/status` and `/about` use the language-pack keys
> `app.studio` / `app.studio_full` the same way. In other words: **the name mapping lives in the
> language packs, while the protocol and build metadata stay English.**

**`model_ready` tells "the backend is up" apart from "inference is available"** (v0.2.1). Since v0.2.1 a
missing model **does not stop the backend**: it starts with an empty engine, keeps serving every request
that does not need inference, and answers inference requests with guidance. The frontend keeps its input
enabled, shows "no model" in the status bar, and switches back automatically once a model is hot-loaded.

When the model is missing, one **non-blocking** notice is emitted *before* `ready`:

```json
{"type":"need_download","blocking":false,"model":"Qwen3-0.6B-Q6_K.gguf","size":495107776,
 "repo":"unsloth/Qwen3-0.6B-GGUF","current":"Qwen3-0.6B-Q6_K.gguf",
 "target":"Qwen3-0.6B-Q6_K.gguf","models_dir":"...","content":"..."}
```

`blocking: false` is the whole point: this is a **notice**, not a question. The old behaviour (the backend
blocked reading stdin for an answer, and exited when the user declined or closed stdin) has been removed —
`action=exit` from the frontend is now the only way to quit. Behaviour without a model:

| category | requests | behaviour without a model |
| --- | --- | --- |
| inference | `chat`, `chat_stream` | guidance text, never a crash |
| needs a model | segmented summarisation, `automation_run` | guidance up front (automation does not even open the browser) |
| does not need a model | file parsing, skill market, settings read/write, model market search/download, session history | work normally |
| status | `context`, `model_state` | `model_ready: false` + the real reason |

### 2.3 Request actions at a glance

| action | Description | Response form |
| --- | --- | --- |
| `chat` | non-streaming: an ordinary question or a `/` command | one `assistant` or `system` line (with `used`/`limit`) |
| `chat_stream` | streaming conversation | several `delta` lines + one `done` line (with `used`/`limit`) |
| `stop` | interrupt the current streaming generation / automation | one `system` line |
| `file` | upload and parse a file (segmented summarisation automatically when too long) | one `system` line |
| `context` | query context usage | one `context` line + `used`/`limit` |
| `model_download` | download a model and hot-swap to it | several `delta` progress lines + `done` |
| `file_stream` | load a file, streaming segmented-summarisation progress | `delta` + several `summary_progress` lines + `done` |
| `model_state` | machine-readable model state | `system` + `model_state` |
| `model_scan` | rescan the models folder (registers hand-copied `.gguf`; auto-loads if there is exactly one) | `system` + `model_scan` + `model_state` |
| `model_load` | hot-load a model by file name (no restart) | `system` + `model_load` + `model_state` |
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
| `agent_state` | agent tool and permission state (tool list, enabled flags, step limit) | `system` + an `agent` object |
| `agent_configure` | write agent configuration (sensitive-tool authorisation, step limit) | `system` + `agent_config` + `agent` |
| `mcp_state` | MCP server status and discovered tools | `system` + an `mcp` object |
| `mcp_apply` | apply MCP configuration (enable/disable servers, change command and timeout), **effective at runtime** | `system` + `mcp` |
| `retrieval_state` | observable local-retrieval state (indexed files, chunks, last query) | `system` + a `retrieval` object |
| `capability_settings` | write inference parameters back (temperature / top_p / repeat penalty / max tokens / preset / context strategy / two switches) | `system` + `settings` |
| `model_profile_save` | add or edit a model tier (`rename_from` renames it) | `system` + `model_profile_save` + `model_profiles` |
| `model_profile_delete` | delete a tier registration (**never deletes files on disk**; a tier in use refuses to go) | `system` + `model_profile_delete` + `model_profiles` |
| `skills_reload` | hot-reload plugins: development sources (`skills_dev/`, the local manifest) reload alone, installed plugins are left untouched | `system` + `skills` + `commands` |
| `skills_log` | read the development log `skills_dev.log` | `system` + `log` |
| `skills_clear_log` / `skills_export_log` | clear / export the development log | `system` + `ok` / the export path |

### 2.4 Example

```jsonc
// frontend → backend
{"action":"chat_stream","message":"用一句话说明什么是回声。"}
// backend → frontend
{"type":"delta","content":"回声是"}
{"type":"delta","content":"声音在传播"}
{"type":"done","content":"回声是……","used":138,"limit":6553}
```

### 2.5 Segmented summarisation progress (`summary_progress`)

`file_stream` (and any streaming request that ends up summarising a long document) emits structured
progress lines alongside the text:

```json
{"type":"summary_progress","phase":"map","status":"running","index":3,"total":12,
 "chars":3000,"tokens":184,"elapsed":42.1,"chunk_elapsed":11.4,"tps":16.1,"slow":false,
 "stream":true,"ok_chunks":2,"partial_chunks":0,"failed_chunks":0,"timeout_chunks":0,
 "skipped_chunks":0,"slow_chunks":0,"content":"… 第 3/12 段处理中：已生成 184 字符，该段已用 11 秒"}
```

- `phase`: `prepare` / `map` / `reduce` / `done` / `aborted` / `failed`;
- `status`: `start` / `running` / `ok` / `failed` / `timeout` / `skipped` / `retry`;
- `content` is the localised one-line rendering (the backend resolves it from the language pack);
  everything else is structured, so the UI can drive a progress bar without parsing text;
- `tokens` is the number of characters produced so far in the current segment, which is how the UI can tell
  "still producing output" from "silent";
- `slow: true` means the segment passed `summarize.slow_chunk_seconds` — **slow is not broken** and the run
  continues;
- `stream: false` means the engine has no streaming API, so heartbeats and intra-segment interruption are
  only possible between segments (the UI says so rather than pretending otherwise);
- aborting (`action=stop`) returns `phase: aborted`, keeps the full text in context and injects **no**
  half-finished summary;
- every failure (call exception, timeout, all segments failing) has its own status, and a run that fell back
  for some segments reports that in the final `done` text.

`summarizer.py` deliberately emits structure only — no UI text — so the algorithm does not depend on the
interface language. The engine call is made through `engine.chat_stream` when available, which is what makes
heartbeats, per-segment timeouts and mid-segment interruption possible at all.

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

**Platform note.** The COM path is **Windows-only**: `file_parser.py` uses it only where the platform is
Windows and a WPS/Office automation object is available. On macOS and Linux parsing goes straight to the
built-in pure-Python parsers, legacy binary `.doc` / `.xls` cannot be read at all, and uploading such a file
reports the capability as Windows-only **instead of** as a parse failure — see
[CROSS_PLATFORM.md](CROSS_PLATFORM.md) section 3.1.

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

**Platform note.** The whole feature is **Windows-only**: it drives the Microsoft Edge installed on the
machine, with a visible window and a reusable sign-in state, and its configuration surface is part of the
Windows UI. On macOS and Linux the capability reports itself unavailable — `Windows-only capability (this is
not a missing dependency)` — and offers the documented alternatives (an MCP server, a skill, or running the
automation on a Windows machine and bringing the results over); see [CROSS_PLATFORM.md](CROSS_PLATFORM.md)
section 3.2.

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

## 4. Configuration reference (backend/config.json)

```jsonc
{
  "model_path": "Qwen3-0.6B-Q6_K.gguf",
  "model_repo": "unsloth/Qwen3-0.6B-GGUF",
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
  "agent": { "max_steps": 4, "allow_sensitive": [], "max_observation_chars": 1200 },
  "retrieval": {                            // local retrieval (task 6); mode is keyword only in this version
    "enabled": true, "mode": "keyword", "chunk_chars": 600, "overlap_chars": 100,
    "top_k": 4, "max_context_chars": 2400, "min_score": 0.0
  },
  "mcp": {                                  // MCP stdio client (task 3); off by default, never auto-connects
    "enabled": false, "timeout_seconds": 20.0, "auto_connect": false,
    "servers": [ { "name": "", "command": "", "args": [], "env": {},
                   "enabled": true, "passthrough_env": [] } ]
  },
  "automation": { /* see backend/config.json; includes the safety protocol and the selectors */ },
  "marketplace": { "skill_topic": "echodesk-plugin", "cache_ttl_hours": 6, "github_token": "" }
}
```

**Configuration override mechanism**: fields in `config.user.json` in the same directory take precedence over
`config.json`; the application only writes back to `config.json`, so handwritten user patches are never
overwritten. (`config.user.json` is already in `.gitignore`, and is a good place for personal settings such as a
GitHub Token.)

**Where the file lives — the same relative layout on all three platforms**: `backend/config.json` in a source
check-out, `app/backend/config.json` in a release bundle (backslashes are only Windows path separators, so the
`/` form is the one to write in commands and scripts). The **user data directory** — UI state, chat history,
batch results, UI language packs — sits **next to** the program instead, in the outer folder of the release:
the program folder itself on Windows, and `~/Library/Application Support/EchoDesk` on macOS or
`$XDG_DATA_HOME/EchoDesk` (or `~/.local/share/EchoDesk`) on Linux when the program folder is not named `app`.
`ECHODESK_DATA` overrides it on every platform. It is **not** `%APPDATA%\EchoDesk`: no code path writes there
(the only occurrences were stale comments, corrected in v0.2.2), and the backend reaches the same answer through
`config.user_data_root()`, which climbs out of the `app/` and `backend/` container folders.
Backend artifacts configured with a **relative** path (session file, models, audit log, installed skills) stay
in the backend working directory — `app/backend/` in a release — and are not moved by `ECHODESK_DATA`; all of
those settings accept `~` and absolute paths, so an enterprise deployment can point them at a managed drive.
See [CROSS_PLATFORM.md](CROSS_PLATFORM.md) section 5.

---

## 5. Building and releasing

**Backend dependencies — the same commands on all three platforms** (only activation differs):

```bash
# create a virtual environment and install the pinned dependency set
python -m venv .venv
python -m pip install -r backend/requirements.txt

# macOS / Linux: activate it (bash, zsh)
. .venv/bin/activate
```

```powershell
# Windows: activate it (PowerShell; in cmd.exe use .venv\Scripts\activate.bat)
.\.venv\Scripts\Activate.ps1
```

**Windows only — the shipped release chain.** `tools/build_release.ps1`, `tools/make_installer.ps1`,
Inno Setup, `rcedit` and the WPF UI probes exist on Windows only, because they build the installer and the
portable folder Windows users download:

```powershell
# 1) download the build tools (rcedit, not committed to the repository)
cmd /c tools\fetch_tools.bat

# 2) one-shot release (multi-file frontend + onedir backend + metadata + installer + SHA256SUMS)
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

**All three platforms — icons and portable bundles (no installer involved).** These are plain Python tools,
so the same commands work on Windows, macOS and Linux, and a Windows build machine can prepare the
Linux/macOS bundles as well (the standalone backend carries a relocatable prebuilt interpreter instead of
relying on PyInstaller, which cannot cross-compile):

```bash
# 1) icon sources (Pillow; re-run after changing the colour scheme)
python tools/make_icon.py

# 2) standalone backend for every RID, or a single one (repeat the flag for more)
python tools/pack_backend.py
python tools/pack_backend.py --platform osx-arm64

# 3) frontend + backend bundle for one target RID (dist/EchoDesk-<version>-<rid>.tar.gz; .zip for win-x64)
python tools/pack_crossplatform.py --rid linux-x64
python tools/pack_crossplatform.py --rid osx-arm64 --backend-mode standalone

# dry run first if you only want to see the steps
python tools/pack_crossplatform.py --plan

# icon and desktop-entry assets for the non-Windows platforms
python tools/gen_platform_icons.py
```

There is nothing else to install for a portable bundle: on macOS/Linux it is
`tar -xzf dist/EchoDesk-<version>-<rid>.tar.gz && cd EchoDesk-<version>-<rid> && ./start.sh`
(run `chmod +x start.sh app/EchoDesk` first if the executable bit was lost during extraction); on Windows
unpack the `.zip` and run `启动 EchoDesk.bat` or `app/EchoDesk.exe`. User data does **not** go into the bundle:
it lands in the per-platform data directory described in section 4, unless `ECHODESK_DATA` says otherwise.

**Installer (Inno Setup 6)**: the single deliverable `EchoDesk-v<version>-Setup.exe` (**no bundled model** —
models are downloaded on demand, or shipped separately as a model add-on pack for offline use) is compiled
from `tools/installer.iss`, and the actual
caller is `tools/make_installer.ps1` (feasibility check + compile + integrity assertions + optional signing +
SHA256). The default install location is **the folder the installer itself sits in** (`[Code] GetDefaultDir`
returns `ExtractFileDir(ExpandConstant('{srcexe}'))`), and it can still be changed in the wizard; shortcuts are
created by the `[Icons]` section with the working directory fixed to `{app}` (the outer layer), so that `results/`
and user data are produced in the outer layer rather than inside `app/`. Inno Setup 6 is required (the script
detects it automatically, or you can point at it with `-ISCC`). The Linux and macOS deliverables are the
`.tar.gz` bundles from `tools/pack_crossplatform.py`, with no installer at all; macOS additionally needs code
signing and notarization, which the packaging tool documents in the bundle's `SIGNING.txt` instead of running.

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

**Tests** — the first group runs on all three platforms, the second needs a Windows desktop:

```bash
# all three platforms
python tools/test_file_parsers.py                # smoke test for parsing six formats (exit code 0/1)
python tools/check_standalone_backend.py         # packaged standalone backend: structure, launchers, protocol
python tools/check_crossplatform_release.py      # the finished archives: format, executable bits, digests
python tools/gen_licenses.py                     # regenerate the third-party licence list
```

```powershell
# Windows only
powershell -File tools\ui_probe.ps1              # WPF UI probe: settings window opens fast, no crash log
& tools\verify_release.ps1 -SkipGui              # top-level release gate, minus the interactive-desktop items
```

**Version control and pushing (SSH only)**:

Since v0.2.1 cross-platform-2, every Git remote operation in this repository uses
**SSH, never HTTPS**. HTTPS pushes to GitHub were repeatedly interrupted on the
maintainer's machine (`RPC failed; HTTP 408` / `Recv failure: Connection was reset`);
SSH is not affected by the same network path and needs no credentials per push.
Three places must agree. The Git operations themselves are identical on all three platforms:

```bash
# the remote must be SSH (scp form user@host:path)
git remote -v
git remote set-url origin git@github.com:wyx-spark/EchoDesk.git
```

The push wrapper and the SSH gate are PowerShell scripts and therefore Windows-only:

```powershell
# one-command push (branch + tag; the preflight covers the working tree, the tag invariant, the installer hash and SSH connectivity)
& tools\push_release.ps1 -DryRun     # check only
& tools\push_release.ps1             # check, then push

# the SSH gate (already part of verify_release.ps1; -TestConnection really dials GitHub)
& tools\check_git_ssh.ps1 -TestConnection
```

On macOS and Linux the same checks are done by hand — `git status`, `git tag vX.Y.Z`, `git push origin master
--tags` — and the preflight those scripts perform (remote is SSH, tag matches the release, installer hash) is
what a pull request should state. `push_release.ps1` **asserts** that the remote is SSH: an HTTPS remote fails the
preflight outright and prints the `git remote set-url` fix, instead of pushing anyway
and reporting a confusing transport error later. The gate pins all three places
(remote, script default, documentation examples) so a later edit cannot drift back.

**Key setup and troubleshooting** (on failure the script prints the same list). The paths differ only in
where the home directory is; the commands are otherwise the same:

```bash
# macOS / Linux
ls -l ~/.ssh                                  # id_ed25519 and id_ed25519.pub must exist
chmod 600 ~/.ssh/id_ed25519                   # ssh refuses a private key that group/others can read
ssh-add ~/.ssh/id_ed25519                     # hand it to ssh-agent
ssh -T git@github.com                         # expect: Hi <username>! You've successfully authenticated
```

```powershell
# Windows (PowerShell); %USERPROFILE%\.ssh is the same directory as ~/.ssh
Get-ChildItem $env:USERPROFILE\.ssh           # id_ed25519 and id_ed25519.pub must exist
ssh-add $env:USERPROFILE\.ssh\id_ed25519      # hand it to ssh-agent
ssh -T git@github.com                         # expect: Hi <username>! You've successfully authenticated
```

Add the **full contents** of `id_ed25519.pub` (the public key, never the private one)
to GitHub under Settings → SSH and GPG keys. **If your network blocks port 22**, use
443 by adding this to `~/.ssh/config` (`%USERPROFILE%\.ssh\config` on Windows — the same file):

```
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```

**Web links stay HTTPS**: Issues / Discussions / Releases / Advisories links must remain
`https://` (an SSH address cannot be opened in a browser). The gate checks git operation
examples only, not web links, and it does not rewrite the historical notes under
`records/`, which record what was true at the time.

---

## 6. Extension points

### 6.1 Existing extension points

- **New commands**: register in `command_handler.py`'s `_handlers` plus `HELP_TEXT`, and keep the `Commands`
  array in the frontend `MainWindow.xaml.cs` in sync (only then are they visible in the command panel and in
  `/help`).
- **New protocol actions**: dispatch them in `main.py`'s `handle()` (one-shot) or `stream()` (streaming); add the
  corresponding method to `BackendService` on the frontend. Send long tasks through `stream()` so they reuse stop
  interruption and progress reporting.
- **New file formats**: add a `_read_xxx` in `file_parser.py` and dispatch it in `_parse_builtin`, add the
  extension to `_TEXT_EXTS` or to a new branch, and update the Filter of the frontend file dialog.

### 6.2 The tool interface (one agent tool is one `Tool`)

See `backend/agent_tools.py`:

```python
Tool(name="calc", description="计算算术表达式", args_hint='{"expr": "表达式"}',
     risk=RISK_SAFE, run=tool_calc, example='{"tool": "calc", "args": {...}, "why": "..."}')
```

- **Registering and calling**: `ToolRegistry.register(tool)`; `default_tools()` is the built-in set;
  `ToolRegistry.run(name, args)` returns `(ok, observation text)` and records every call in `registry.calls`
  (observability); `public_list()` serves the UI and the docs.
- **Convention: a tool never raises, it returns its failure as text.** `ToolRegistry.run` catches exceptions
  anyway, and `looks_like_failure()` applies one shared rule (`_FAILURE_PREFIXES` / `_FAILURE_MARKERS`) so
  "cannot compute", "nothing found" and "MCP peer error" all count as failures — otherwise the UI shows them
  as successes and the user sees "the tool ran but produced nothing".
- **Permission tiers**: `safe` (pure computation and "reading only what the user already put in", on by
  default), `sensitive` (sends data out or reads arbitrary local content; must be pre-authorised in
  `config.agent.allow_sensitive`), `guard` (tools with write side effects — **this version has none**; the tier
  is kept so the vocabulary stays uniform). `ToolRegistry.is_allowed()` is the only decision point, and an
  unauthorised call is **not a silent failure**: it returns an observation saying "not enabled, here is how to
  enable it", so the model can switch approach or just answer.
- **Tools that need host capabilities** (conversation, search, summarizer) are injected through `ToolContext`
  and take `(args, ctx)`, listed in one table `_CTX_TOOLS` — deliberately **not** signature-checked (that breaks
  under packaging/decorators), so one glance at the table shows which tools touch host state.
- **The "tool protocol + available tools" block of the prompt** is appended at runtime by
  `ToolRegistry.protocol_text()`: the available set changes with permissions, and the mode block has a
  700-character budget — appending at runtime costs nothing there and never lists a tool that is not enabled.
- **The loop** lives in `agent.AgentLoop.run()`: its events are `agent_step` / `tool_call` / `tool_result`, and
  setting `stop_event` stops it (the **same interruption path as streaming chat**). Once `max_steps` is reached
  it appends one more instruction — "call no more tools, answer from what you have" — and converges.
  Tool calls are parsed by `parse_tool_call()` (a pure function, unit-testable) which tolerates code fences and
  surrounding prose.

### 6.3 MCP integration (the stdio-only trade-off)

- `mcp_client.StdioMcpClient`: an in-house ~200-line JSON-RPC 2.0 client speaking one JSON object per line over
  stdin/stdout, implementing only `initialize` / `tools/list` / `tools/call` (plus
  `notifications/initialized`). Calls are bounded by a timeout (`mcp.timeout_seconds`, 20 s by default), so a
  wedged external process cannot stall the program.
- `mcp_client.McpBridge`: turns the server declarations from the configuration (`McpServer`: `command` /
  `args` / `env` / `enabled` / `passthrough_env`) into clients; `discover()` fetches the tool list, `call()`
  invokes a tool and `status()` feeds the UI and `/mcp`.
- `register_mcp_tools(registry, bridge)`: wraps discovered tools as ordinary `Tool` objects in the **same**
  `ToolRegistry`, named `mcp__<server>__<tool>`, always with `risk="sensitive"` and with the MCP `inputSchema`
  as the argument hint. Permissions, auditing, visibility and interruption therefore reuse the agent machinery
  instead of a second set of rules.
- **Why not the official `mcp` package**: it pulls in `anyio` / `httpx` / `sse-starlette` / `starlette` and
  more, most of the server-side parts of which are useless for a client-only feature, while the release is
  pinned to the 350–400 MB range.
- **The cost, stated plainly**: this version supports **stdio transport only** — no HTTP/SSE, no
  resources / prompts / sampling, no roots. Should the official package be adopted later, `McpServer` and
  `register_mcp_tools` would not change: only the transport implementation would, which is exactly why
  "protocol" and "transport" are written separately.
- **Starting a process is explicit**: `auto_connect` defaults to `false`, so only
  `refresh_agent_tools(discover_mcp=True)` (triggered by `/mcp connect` or a settings write) actually launches
  an external process; a disabled server is `close()`d and its tools disappear from the list immediately.

### 6.4 Retrieval extension points

See `backend/retrieval.py`:

- `Retriever.add_file(name, text)` indexes only that one file (**incremental** — existing files are untouched
  byte for byte, and a file with the same name replaces the previous content); `remove_file(name)` removes one
  file exactly and rebuilds the BM25 statistics; `search(query, top_k, files)` returns
  `List[ScoredChunk]` (`files` narrows the scope); `as_context(query)` renders text with numbering and
  `file name (chars a-b)`, returning an empty string when nothing matched (so the caller **injects no empty
  block**).
- **The only extension point is `Retriever.search()`**: `RetrievalConfig.mode` already reserves
  `hybrid` / `vector`, and as long as the return value stays `List[ScoredChunk]`, injection, traceability,
  permissions and `/status` need no changes. Selecting those two values today degrades to keyword (no error, so
  a configuration edit can never stop the program from starting).
- Tokenisation and chunking live in this module: Chinese 2-grams + English words + **numeric tokens**
  (numbers must be captured whole, otherwise key-fact questions such as "what is the penalty rate" fail to
  retrieve); chunks never cut a sentence, they overlap, and their ranges are **absolute offsets into the
  original text** — the basis of traceability, because the user has to be able to check "chars 210-480"
  against the source.
- The division of labour with segmented summarisation is decided by
  `main.Backend._retrieval_context_for()`: short documents are not retrieved, long ones get "summary for the
  whole picture + retrieval for the detail". **Do not turn this into "retrieve over every file"**: injecting the
  full text of a short document is simply more accurate, and forced retrieval only loses information.

### 6.5 Plugin dependencies and version validation

See `backend/skills_runtime.py`:

- `parse_manifest(manifest)["dependencies"]` normalises to `{plugin id: version requirement}`. In `skill.json`,
  `dependencies` may be a **list** (`["other"]`, `["other>=1.2"]`) or a **dict**
  (`{"other": ">=1.2"}`); `_parse_dependencies()` unifies both.
- `version_satisfies(version, spec)` supports `>=` / `>` / `<=` / `<` / `==`; `parse_requirement()` accepts an
  optional `v` prefix (copying a GitHub tag such as `v1.2.0` is common, and not accepting it would let
  validation be bypassed silently); an empty or unparseable requirement is **always let through** (better
  lenient than wrongly blocking), and version segments are padded to equal length before comparing
  (`1.0` and `1.0.0` are the same version).
- `SkillLoader._check_dependencies(records)` decides once, after discovery and before the per-plugin load,
  because dependencies can be mutually entangled and every manifest must be known first. A missing dependency,
  a disabled dependency or an unsatisfied version **refuses to load and states the reason**; a dependency that
  declares no version is **warned about and let through** (development plugins routinely omit it).
- `echodesk.min_version` is checked against the program version (`APP_VERSION`) with the same
  `version_satisfies`.
- `SkillLoader._verify_declared_commands()` compares the declared commands with the registered ones and writes
  any mismatch (including "registered commands but declared nothing") to the warnings and to `skills_dev.log`.
- Loading, unloading and registration catch `BaseException`, not just `Exception`: a plugin's `sys.exit()` or
  `raise SystemExit` passes straight through `Exception` and used to take the backend process down with it. The
  same isolation on the command execution path lives in `command_handler.py`.

### 6.6 Skill development specification

- See `docs/zh-Hans/SKILL_GUIDE.md` (Chinese) and `docs/en/SKILL_GUIDE.md` (English).
