# EchoDesk Changelog

> A Chinese reference translation of this file is available at
> [`CHANGELOG.zh-Hans.md`](CHANGELOG.zh-Hans.md); if the two ever disagree, this English version prevails.

This file records the feature changes, fixes, and known limitations of each version.
Version numbers follow `MAJOR.MINOR.PATCH`; file version resources are uniformly `X.Y.Z.0`.

---

## v0.2.2

> **The cross-platform version.** v0.2.1 was released *before* any of this work: it contains no
> `frontend-avalonia/` directory and no platform-isation, and its tag (`v0.2.1`) points at the last
> commit of that state. Everything described under this heading — the cross-platform frontend
> migration, the platform-isation of the backend, and the three-platform packaging — is **v0.2.2**.
> For the complete implementation plan, key decisions, deviations, unfinished items and size records,
> see the internal development records (`records/v0.2.2-跨平台记录.md` and
> `records/v0.2.2-跨平台-2记录.md`; the file names keep the task-spec number they were written
> under, **not the release number**).

### Self-test fixes: uploaded files, web search, browser choice, agent reasoning

Task specification: `prompts/EchoDesk-v0.2.2自测问题修复-1.md`. The full record — per-item root
causes, reproduction evidence, the investigations that were run, what could *not* be reproduced,
and what is still outstanding — is `records/v0.2.2-自测问题修复记录.md` (internal,
**not shipped with releases**).

**Uploaded files are now actually given to the model.** Previously the document body was injected
only when the question happened to contain one of a fixed list of words (file, contract, clause,
summarise, …). The list contained "摘要一下" but not "总结", so the most natural questions — *"帮我总结一下"*,
*"分析一下"*, *"甲方是谁"* — put **not one character** of the document into the context, and the model was
left with nothing but the file name and an instruction telling it to ask the user to name the file.
The model's answer "I can't see any file content" was, from its point of view, correct. Injection is
now unconditional whenever a file is loaded, bounded as before by the `files.total_chars` budget.
The same fix removes a second error in the old check: it tested a field that is *emptied* when an
oversized document is spilled to disk. A regression assertion is registered in the gate so this
cannot come back a third time.

**Web search works again, and failures explain themselves.** The `bing.com` URL in the error message
did not come from this project (the repository contains no reference to it): the bundled
`duckduckgo-search` 8.1.1 pins its own backend to `backends = ["bing"]` and disables DuckDuckGo's
html/lite backends, and its HTTP client does not follow redirects — while `www.bing.com` is redirected
to `cn.bing.com` outside the US. It therefore *always* sees a 302 and reports a failure, which no
amount of retrying fixes. A built-in second source now follows the redirect and parses the results
itself (using only the standard library and the project's existing `requests` dependency), and search
failures are reported as one of a small set of user-facing kinds — network, timeout, certificate,
rate limit, changed source — instead of a raw Rust error string.

**The browser used by automation is now selectable.** `automation.browser` accepts `auto`, `edge`,
`chrome`, `chromium` or `firefox`; `auto` tries them in order and uses the first that starts, keeping
Edge first so that upgrades do not change existing behaviour. When every candidate fails, the error
lists what each browser reported, instead of surfacing a bare Playwright error.

**Agent steps now show their reasoning.** The Agent loop calls the model without streaming and never
split the reasoning out of the reply, and its progress events carried no text at all — so nothing was
renderable. Each step's reasoning is now separated and sent to the front end, and tool-call parsing
runs on the answer text only (reasoning that happened to mention calling a tool can no longer be
mistaken for a real call).

**Context usage reports what was really sent.** The figure shown next to the input box counted only
the system prompt and the conversation history, omitting this turn's injected document text,
retrieval snippets and search results — by far the largest part. Measured on a 3,200-character
document, it reported 355 tokens where the request actually carried 1,462. A second figure,
`used_last_request`, now reports the real number.

**The cross-platform front end could not start at all.** Two defects meant the Avalonia build —
the one the cross-platform packages ship — crashed on launch and then again on the first message:
the main window never called its generated `InitializeComponent()`, so every `x:Name` field was
`null` (the settings window and all seven settings pages *do* call it; only the main window missed
it), and four places cast the result of `FindResource` straight to a brush, which throws as soon as
the key resolves to `AvaloniaProperty.UnsetValue` instead of a brush. Both are fixed. The third
startup problem — the Settings window could not be opened at all — is fixed too; it is described in
"Settings window, agent steps, context figure" below, together with a **correction of the cause
previously recorded here**: it was not Avalonia's theme-variant resolution.

**The command box placeholder now lines up.** It was a hand-positioned overlay whose margin (16)
did not match the real text origin (15 = 1px border + 8 margin + 6 padding), so the prompt sat 2px
off the cursor; and — because the visibility switch from the WPF front end was not carried across —
it was never hidden, so it stayed on top of whatever you typed. It now uses the text box's own
placeholder, which the framework positions and hides itself.

**Settings could be saved to the wrong file.** Reading the configuration looks in four locations
(the working directory first, then the program directory and the packaged locations), but *saving*
it opened a plain relative path — so it wrote only to whatever the current working directory
happened to be. When that directory was not where the configuration actually lives, the change
landed in a **new, stray** `config.json` and the file actually being read was never updated:
settings appeared to save and then revert on restart. Worse, that stray file is the *first*
location the reader checks, so launching from that directory later would pick it up instead — the
configuration appearing to change by itself. It went unnoticed because the packaged front end sets
the backend's working directory to `app/backend/`, which happens to be where the configuration
lives; running from source, from a test, or from any other directory exposes it. Saving now targets
the same file that was read.

**Smaller fixes.** A missing `import re` meant oversized documents were never spilled to disk (the
feature had never worked; the pointer written into the context pointed at a directory that was never
created). `file_cache/` is now ignored by Git. The backend crash log is no longer polluted by
uncaught exceptions from `tools/` scripts, which previously made real backend crashes hard to spot.
A duplicate definition of `load_file_into_context` was removed — the old single-file version was
silently overriding the newer multi-file one.

### The cross-platform front end now matches the Windows one

The follow-up round on the same task specification, on the maintainer's instruction to bring the
Avalonia front end up to feature parity with the WPF one. The full record — what was already in
place, what was genuinely missing, the three real defects the probe found, and what is still
outstanding — is `records/v0.2.2-Avalonia功能对齐记录.md` (internal, **not shipped with releases**).

**The model market is there now.** It was the one whole feature the cross-platform front end did
not have: search with live progress, the local filter panel (keyword plus facets for parameter
count, quantization and last-updated, with counts), four sort orders, the full pager (first /
previous / next / last / jump-to-page / page size), the pinned default-model row, repository
details and per-file downloads with progress. All of it runs on the **same backend actions as the
WPF front end, unchanged** — `model_hub_load` (streaming, with `no_page` so the first page is
computed in exactly one place), `model_hub_query` (in-process filtering, sorting and paging — no
network round trip per keystroke), `model_hub_detail` and `model_hub_download`. Control names and
automation ids match the WPF ones deliberately, so one probe drives both front ends.

**Two features were added to both front ends, not just ported.** `backend/agent.py` already
emitted a `phase="thought"` event carrying each step's separated reasoning, but *neither* front end
rendered it — the per-step reasoning was being sent and silently dropped. Both now show it. And
the model-profile editor (create, rename, edit context length/temperature, delete) existed only in
the WPF settings window, so on the cross-platform build adding a profile meant editing
`config.json` by hand even though `model_profile_save` / `model_profile_delete` were already
implemented; the cross-platform page now has the same editor, with the same rule that only filled-in
fields are sent (an empty box means "I did not touch this", not "delete it").

**A JSON number-format bug that only System.Text.Json exposes.** `JsonElement.GetInt32()` throws
`FormatException` on a fractional value such as `recommend_basis.ram_gb`, where JSON.NET (used by
the WPF front end) silently truncates. One such field made the market's status line fail to render,
reporting "local filtering failed" instead — and because the exception ran inside the
language-change UI callback, switching language broke with it. Numbers are now read as doubles and
rounded.

**Every language key the cross-platform front end asks for now exists.** The front end calls
`Loc.T("key", "中文兜底")`, so a key that is missing from the language pack does not fail — it
quietly falls back to Chinese and the English UI shows Chinese. `tools/check_lang.py` checks the
opposite direction and cannot see this. A reverse audit (`artifacts/_audit_avalonia_keys.py`) found
12 such keys and they are all filled in, in both languages.

**New gate: `EchoDesk --probe market`.** It starts a real backend, walks the whole market
interaction (search → paging → page size → local filter → reset → facets → pinned-row details →
language switch) and asserts the market's own state snapshot, so "does filtering actually narrow
the results" is a number rather than an impression. It is wired into `verify_release.ps1`'s GUI
section, pointed at the developer build, alongside the existing window probe.

### Settings window, agent steps, context figure, and the gates that had gone blind

A follow-up round on the same task specification, plus the items the previous round left open.
The full record is `records/v0.2.2-自测问题修复记录.md` §16 (internal, **not shipped with releases**).

**Opening the Settings window no longer kills the process — and the cause was misdiagnosed.**
The previous entry blamed Avalonia 12.1.3's theme-variant resolution, based on where the stack
overflow surfaced. That was wrong. The real cause is a **synchronous cycle between rebuilding the
UI texts and the language selector**: `Loc.SetLocale` raises `LanguageChanged`, the settings window
rebuilds all its texts, the General page re-creates its combo items and re-selects the current
value — which the control reports as a genuine selection change — and that fires `SetLanguage`
again, straight back into `Loc.SetLocale`. It ran 340 rounds deep and overflowed the stack.
The two guards that break the cycle were already in place; this round established **why** they are
needed, verified it by bisection (disabling them reproduces the crash with exactly that stack), and
corrected the record. No theme code needed to change.

**"Avalonia was never actually run" is now impossible to repeat by accident.** That entire class of
defect existed because no gate ever constructed a window: the backend self-test deliberately opens
none, and `tools/ui_probe.ps1` drives the **WPF** build. A headless front-end probe now exists
(`EchoDesk --probe main|settings|agent|both`, driven by `tools/ui_probe_avalonia.ps1`): it really
constructs and shows the windows and reports the verdict as an exit code. Because a stack overflow
cannot be caught in .NET, "the process died without printing a verdict" *is* the failure signal.
Its `agent` mode starts a real backend, runs one real Agent turn, and feeds the events the backend
actually emitted into the front end's real dispatch path, then asserts the step rows were rendered
— so Agent step rendering is a number, not an impression (measured: 2 `agent_step` events → 3 rows).

**The two front ends are two delivery lines, not a choice between them.** The Windows portable
build and installer ship the **WPF** front end (`tools/build_release.ps1` publishes
`frontend/EchoDesk.csproj`; the tree contains `PresentationFramework*.dll` and no `Avalonia*.dll`),
while the three-platform packages ship the Avalonia one. The earlier "which front end do we fix?"
question rested on the opposite assumption and is therefore moot: the WPF findings from self-testing
are on the path Windows users actually run. `PROJECT.md` has been corrected accordingly.

**The context figure now reports what was really sent.** The backend was already sending
`used_last_request`; the front end still displayed only `used`, so users kept seeing the figure that
under-reports by roughly a factor of four. Both front ends now show the larger of the two, and the
tooltip spells both out.

**Genuine WPF crash on opening the Settings window.** `Walk<T>` in `SettingsWindow.Skills.cs`
cast `Tag` straight to `string`; the model page stores a whole row object in `Tag`, so the cast
threw `InvalidCastException` and wrote a front-end crash log every time the window opened.
It now uses `as string`, which simply does not match.

**Alt+↑ copies your previous question back into the input box** (task twelve), in both front ends,
without colliding with the existing shortcuts (`Ctrl+Enter` sends; the arrow keys are taken by the
command palette only while it is open).

**Backend INFO logs are bilingual.** Every line the backend writes to stderr is shown in the status
bar, so INFO messages are user-facing text. A single entry point (`i18n.log_user`) now resolves the
message through the language pack, and the 14 user-facing `log.info` calls in `main.py` were moved
to it in one pass — deliberately not piecemeal, because a half-translated set is harder to explain
than a consistently single-language one. Developer-facing diagnostics (internal paths, cache-hit
counts, compaction arithmetic) stay single-language, by the classification in the record.

**Three gates had gone blind and were fixed.** `tools/test_backend_e2e.py` asserted the version
number **literally** (`"v0.2.1"`), so it had been failing on every run since the version bump —
reporting a successful upgrade as a failure — and it silently "passed" (0 checks, exit 0) whenever
the release tree had no model. `tools/ui_probe.ps1` waited for the exact status string `已就绪`
after the wording had changed to `就绪`, so it reported "backend not ready" while the backend was
ready in five seconds; it also copied the test model into a directory that does not exist in a
release tree (which aborted the script before it launched anything). All three now report the real
state, and the model the probe needs is staged where the backend actually looks for it.
Two more had the opposite problem — failing on inputs unrelated to what they test: an i18n
self-check asserted that `check_lang.py --locale no-such` must exit 0, although `--locale` only
filters language packs and never the document-pair checks it was actually reacting to; and
`check_lang.py` counted a **already-recorded** deviation (`docs/THIRD_PARTY_LICENSES.md` is
generated in Chinese, so it cannot also link to a Chinese translation) as a hard problem, which
turned both the language gate and the i18n gate red indefinitely. Recorded deviations are now
counted and printed separately and no longer decide the exit code; they are still reported.
With all of that fixed, `tools/verify_release.ps1 -SkipE2E` — GUI checks included — passes
39 of 39 gates.

### Bilingual coverage and three-platform friendliness across the whole repository

Task specification: `prompts/EchoDesk-v0.2.2全面双语与三平台友好.md`. The full audit, the
per-category strategy, the decisions taken, the deviations from the specification and the
remaining work are in `records/v0.2.2-全面双语与三平台友好记录.md`; the file-by-file audit
table is `records/v0.2.2-审计清单.md` (both internal, **not shipped with releases**).

**Every tracked file was audited, not translated indiscriminately.** `tools/audit_repo.py`
walks `git ls-files` and classifies each of the 252 files by kind, bilingual state and
three-platform state, and emits the audit table. Its `--check` mode is now a gate (registered
in `verify_release.ps1` and in CI): repository-level and nested documentation must either be a
language pair or be registered as deliberately single-file bilingual, paired root files must
cross-reference each other, and every `tools/*.ps1` must be registered in `PS1_ALLOWLIST`
with a reason — i.e. someone has explicitly judged whether it needs a cross-platform
equivalent. Adding a new PowerShell script without that judgement now fails the gate.

**Documentation.** `deploy/models/README.md`, `examples/skills/hello-world/README.md` and
`examples/skills/ofd-reader/README.md` became English-default + `.zh-Hans.md` pairs. The
command/setup documentation (`CONTRIBUTING`, `docs/*/DEVELOPER.md`, `docs/*/COMPLIANCE.md`,
`docs/*/FAQ.md`, `docs/*/AUTOMATION_GUIDE.md`, `docs/*/SKILL_GUIDE.md`, `docs/*/README.md`)
now covers install, build, run, package, test, Git operations and dependency installation on
all three platforms, using **portable syntax first and per-platform blocks only where the
commands genuinely differ** (no `<details>` panels, no triple repetition). Everything that is
Windows-only is still stated as such, with the exact decline message and the workarounds:
WPS/Office COM for encrypted and legacy `.doc`/`.xls` files, and the Playwright/Edge
automation, which is unavailable off Windows and is now flagged at the top of the automation
guide in both languages. Chinese versions that were missing the "the English version
prevails" notice now carry it.

**Platform resources.** `tools/gen_platform_icons.py` derives all three platforms' icons from
the single source `assets/EchoDesk.ico`: `assets/EchoDesk.icns` (a PNG-based icns container
built with the standard library, because `iconutil` only exists on macOS while this project
builds on Windows), the freedesktop tree `assets/icons/hicolor/*/apps/echodesk.png`, plus
`deploy/linux/echodesk.desktop` and AppStream metadata. `deploy/linux/README.md` documents
portable and system-wide installation, the data directory and the CJK/emoji font packages.
The source art tops out at 256×256, so no 512/1024 slots are generated and none are faked.

**Configuration example.** `config.example.json` is no longer a byte copy of
`backend/config.json`: `tools/gen_config_example.py` injects a bilingual `_comment` block (what
the file is, how `config.user.json` overrides it, how to spell paths) and a `_platform_paths`
table for the data, results and models directories. The injection happens on the *copy* — the
runtime configuration, which pydantic parses, is untouched.

**Fixed, found by checking the specification against the code:**

- **The frontend and backend disagreed on the user data directory in the standalone layout.**
  `backend/config.py` resolved `<release>/app/backend/app/` to `<release>/app/backend` while the
  Avalonia frontend resolved `<release>` — so one bundle wrote `snippets.json` at the top level
  and `session.json`, `results/` and caches two levels deeper. Users would see "my history is
  gone" rather than a path error. The resolver now walks up past the `app`/`backend` container
  folders, so all three real layouts agree; `check_standalone_backend.py` asserts this against a
  live backend instead of relying on code review.
- **The backend working directory differed per platform**, which moved the models and skills
  directories: the Avalonia bundle ran with `cwd=backend/app` while the WPF build uses
  `app\backend\`. The Avalonia launch and both `run-backend.*` launchers now use the bundle
  directory, so `app/backend/models/` means the same thing on all three platforms.
- **A CI gate had been silently dead since the version bump**: `release-gates` tested for a
  hard-coded `publish\EchoDesk-v0.2.1\..._internal`, so the packaged-modules comparison never
  ran again and the log merely said "skipped". The version is now read from `backend/version.py`.
  This is the third recurrence of the hard-coded-version defect in this repository.
- **`%APPDATA%\EchoDesk` does not exist.** Four source comments and several documents described
  the Windows data directory that way; no code path writes there. The comments are corrected,
  the unused `PlatformInfo.UserDataDirectoryName` (whose two ternary branches were identical and
  which had no callers) is deleted, and the documents now say plainly that it is *not*
  `%APPDATA%`.
- **`docs/*/COMPLIANCE.md` misdescribed the installer's default directory** as
  `{autopf}\EchoDesk` / `%LOCALAPPDATA%\Programs\EchoDesk`. `tools/installer.iss` actually
  defaults to `ExtractFileDir({srcexe})` — the folder the Setup program sits in — with
  `AppendDefaultDirName=no` and `PrivilegesRequired=lowest`. Corrected in both languages,
  because "which directory is writable after install" decides whether enterprise deployments
  must relocate the artifact paths.
- **The cross-platform bundles were missing `NOTICE`** (the English original — only the Chinese
  translation shipped) along with the `SECURITY`, `CONTRIBUTING` and `CODE_OF_CONDUCT` pairs.
  The file list is now 14 entries, and a missing entry is reported by name instead of being
  skipped in silence, which is how `NOTICE` went unnoticed.
- `docs/en/FAQ.md` still said "Applies to version: v0.2.1" while its Chinese pair said v0.2.2.

**CI.** A new `static-gates-linux` job runs the standard-library-only gates on `ubuntu-latest`
(language packs, dependency declarations, theme, the new audit/resource/config-example gates),
because case sensitivity, path separators and encodings only show up off Windows. Comments are
bilingual and the workflow display name is too; **job names were deliberately left in English**
so that GitHub required-status-check matching cannot break, and each Windows-only job now
explains why it is Windows-only. A stale comment claiming the platform test runs on Linux/macOS
runners was corrected — that job is pinned to `windows-latest` and exercises the injected path.

### Git over SSH only, settings-window shell and General page, cross-platform backend distribution

Task specification: `prompts/EchoDesk-v0.2.1跨平台-2.md`. The full implementation plan,
measurements, deviations from the specification and remaining work are in
`records/v0.2.2-跨平台-2记录.md` (internal, **not shipped with releases**).

**All Git remote operations now use SSH.** HTTPS pushes to GitHub were repeatedly
interrupted on the maintainer's machine (`RPC failed; HTTP 408` /
`Recv failure: Connection was reset`); SSH is unaffected by that network path.
`tools/push_release.ps1` now **asserts** that the remote is SSH: an HTTPS remote fails
the preflight outright and prints the `git remote set-url` fix. Its troubleshooting text
is entirely SSH-based (connectivity test, keys, public key, port 443) and no longer
suggests personal access tokens. A new gate, `tools/check_git_ssh.ps1`, pins all three
places (remote, script default, documentation examples). A genuine conflict was fixed
along the way: the content gate's email rule treated `git@github.com` as a personal
email address, which made documenting SSH URLs fail by construction.

**Cross-platform frontend: every settings tab is now migrated** (`frontend-avalonia/`).
The settings window is built as a shell plus one `UserControl` per tab (the WPF version is a
1321-line XAML file plus six partial classes, roughly 8000 lines in total), and **all seven
tabs are done**: General (theme/emoji/font size/UI language apply immediately and persist;
default mode is synced to the backend via `/mode`), Model (profile chips switch models with
hot reload and download on demand, plus inference parameters — slider bounds are **widened**
rather than clamped so that merely opening the page and pressing Save cannot silently rewrite
a `max_tokens` of 8192 down to 4096), Automation (20 fields, safety protocol, result-folder
preview; **disabled as a whole on non-Windows platforms with a degradation notice** instead of
letting people fill in a form that can never run), Skill market (search/sort/install/uninstall,
keeping the pre-install security notice), Installed (plugin list with in-place confirmed
uninstall), Developer (developer mode plus the "who decided it" distinction, directories, log,
plugin scaffolding) and Agent (tool authorisation with risk levels, **defaults come from the
backend**).

A few sub-features that need a full form or dialog **are not migrated yet** (the model market,
MCP add/edit/remove, the plugin manifest table, visual element picking and the test wizard).
Each of those places says explicitly that it is a known gap and offers a workaround (edit
`config.json` directly, or use the WPF build on Windows) rather than showing an empty panel or
a button that does nothing. Backend discovery gained a "bundled interpreter" candidate, called
directly by the frontend (not through the launcher script, which would add a shell layer that
makes process termination unreliable).

**Cross-platform backend distribution** (`tools/pack_backend.py`). PyInstaller is replaced
by **python-build-standalone plus a generated launcher**: a Windows build machine can now
prepare backends for all three platforms, and the target machine just unpacks them — no
Python installation, no network access (uvbox was rejected because its first run downloads
dependencies, which contradicts the "data never leaves the machine, works offline" promise).
Dependencies are fetched with `pip --target` for the target platform, and environment markers
are evaluated **for that target** (pip evaluates them for the build machine, which in testing
made it require a Linux build of `pywin32` and fail outright). The Windows installer
**still uses PyInstaller**: the WPF launcher path has no "interpreter + main.py" mode, and
standalone brings no benefit on Windows — a deliberate trade-off, not an oversight.

**All three platform backend bundles are now produced and verified.** The earlier attempt in this
round had stalled: the measured download rate from GitHub Releases was only 40–70 KB/s and the
network dropped completely mid-run. Three things then made it possible, and each is worth knowing:

* **The macOS wheel for `llama-cpp-python` is broken upstream.** Upstream publishes **only a source
  distribution on PyPI** — every wheel lives in the author's own GitHub Releases index — and the
  **macOS arm64 wheels in the `cpu` index are corrupt**: the byte count and the official digest both
  match, yet `lib/*.dylib` inside the archive is damaged throughout its compressed streams, so
  `pip install` cannot possibly succeed. Versions measured as corrupt: `cpu` index 0.3.21 / 0.3.30 /
  0.3.35 / 0.3.36, `metal` index 0.3.31–0.3.36. The `metal` index (the `vX.Y.Z-metal` release tags)
  has **0.3.30 as the last intact build**, so macOS pins that single wheel **by direct URL** — a
  direct URL rather than "index plus version" because the two indexes hold a file with the **same
  name but different contents**. The metal build is also simply more correct on macOS: it enables
  the Metal (Apple GPU) backend. A new tool, `tools/fetch_wheel.py`, downloads a target-platform
  wheel with resume **and decompresses every member before accepting it**, which is what caught this
  — "downloaded completely" is not the same as "installable".
* **python-build-standalone publishes a stripped variant.** The backend now uses
  `install_only_stripped` instead of `install_only`. Measured on release 20261003, the interpreter
  archive shrinks from 71.8 MB to **33.5 MB** on Linux, 45.2 MB to **21.0 MB** on Windows, and
  24.2 MB to 24.1 MB on macOS. This is pure weight reduction: debug symbols are only needed to debug
  native crashes with gdb/lldb, while Python's own tracebacks come from the `.py` sources.
  `--keep-symbols` switches back for native debugging. The three backend bundles shrink
  accordingly (archives 128.7 → **102.9 MB** / 188.1 → **150.2 MB** / 120.7 → **120.3 MB**).
* **Dead Tcl/Tk runtime is removed.** With the `tkinter` package already stripped, nothing can load
  `libtcl9*.so` / `libtk9*.so` / `tcl9/` / `tcl*.dll` any more, so those files are provably
  unreachable (about 7 MB per platform) — a reachability argument, not a guess.

Two more tools came out of the same problem: `tools/fetch_runtime.py` pre-warms the interpreter
cache so downloading and packing can be run as separate steps, and `pack_backend.py` reuses a
verified wheel from `artifacts/wheels/` when a dependency is pinned by URL, instead of asking pip to
fetch it again over a link that keeps resetting.

**The Avalonia main window now has all its feature panels**: the command palette (typing `/` opens
ranked candidates, with ↑↓/Tab/Enter/Esc and the rule that an ambiguous match is completed rather
than executed), the snippet palette backed by `snippets.json`, the toast bar (auto-hides after six
seconds), the status strip (search / automation / market / context / KV cache / model capability /
context compaction / capability warnings / update notice / live metrics, with a collapsible state
that persists), and the Agent step rows (`agent_step` / `tool_call` / `tool_result` each become
their own row, with the "not authorised" case distinguished from "the tool failed"). Destructive
commands get a two-step confirmation — Avalonia has no `MessageBox`, and the project's existing
"click twice" pattern does not fit an Enter-driven palette, so the second confirmation happens by
pressing Enter again in the same input box. Sizes and verification counts are in
`records/v0.2.2-跨平台-2记录.md`.

### Cross-platform: Avalonia frontend prototype, platform capability boundaries, cross-platform packaging

Task specification: `prompts/EchoDesk-v0.2.1跨平台.md`. This round removes "Windows-only" as an
*architectural* property: the backend becomes one codebase shared by all three platforms, a new
Avalonia frontend joins the WPF one, and the capabilities that genuinely cannot exist off Windows
are reduced to **two explicit degradation boundaries**, each with an alternative path. The audit,
prototype results, the reuse-versus-rewrite list, packaging routes and unfinished work are in
`records/v0.2.2-跨平台记录.md`.

**New cross-platform frontend prototype** (`frontend-avalonia/`, tasks 1 and 2). Its target framework
is `net10.0` — deliberately without the `-windows` suffix, which is the first hard gate for "can run
elsewhere". The main window is done: message stream (Markdown rendering, code blocks and tables,
copy), streaming output with throttled re-rendering, collapsible reasoning, file upload (button and
drag-and-drop), font zoom, light/dark theming, UI language switching, and a platform capability
banner. Backend process management and the JSON protocol are **carried over** from the already
verified WPF implementation (single in-flight request gate, idle timeouts, two-channel streaming);
only three platform-dependent details changed.

**Backend platform work** (tasks 3 and 5). New `backend/platform_support.py`: platform detection
(`kind`, `rid`, architecture, interpreter) and a capability list (availability, reason, alternative).
Both the `ready` handshake and a new `platform` action return that report; the frontend uses it to
show the degradation banner and to check that the two sides agree about the platform.

| Change | Why |
| :--- | :--- |
| Tolerant `win32com` import | A top-level `import win32com` makes the backend die at startup on Linux/macOS |
| `pywin32` marked `sys_platform == "win32"` in `requirements.txt` | Otherwise `pip install -r` fails on the whole dependency chain off Windows |
| `EchoBackend.spec` branches per platform (`.exe` name, version resource, `.ico`, `win32com`) | Version resources and `.ico` are Windows-only; elsewhere the build fails or emits invalid resources |
| Per-platform user data directories (macOS Application Support, Linux XDG) | Off Windows the program directory is often read-only |

**Only two degradation boundaries** (task 5): reading **encrypted** WPS/Office documents (including
legacy `.doc`/`.xls`, which need COM) and **enterprise intranet automation** (needs a local Edge).
Both explain why they are unavailable and what to do instead, and neither is reported as a missing
dependency. Everything else behaves the same on all three platforms. See
`docs/en/CROSS_PLATFORM.md`.

**Cross-platform packaging** (task 4). New `tools/pack_crossplatform.py`: PyInstaller cannot
cross-compile, so the script itself is cross-platform — it builds for whatever platform it runs on
(publish the frontend → freeze the backend → strip debug symbols (native Skia/HarfBuzz `.pdb` files
account for about 100 MB on Windows) → produce `.tar.gz`/`.zip` → write `SHA256SUMS.txt`,
`manifest.json` and `SIGNING.txt`). CI gained a three-platform matrix job (build, publish per RID,
platform test, headless self-test). macOS signing/notarization and the Linux AppImage are **not
automated** (they need certificates and extra tooling); the commands are written into the artifact
but never run by the build machine.

**New verification**: `EchoDesk --selftest` (headless: platform detection → launch the backend by
platform rules → `ready` handshake → `platform`/`capability` round-trip; measured 11/11 on Windows)
and `tools/test_platform.py` (69 checks; injects a platform through `ECHODESK_FORCE_PLATFORM` to
exercise the non-Windows degradation paths). Stated plainly: real Linux/macOS *run* evidence has to
come from the CI matrix — inside this repository there is currently only build-level evidence.

**UI text**: every new string (frontend and backend) is in both language packs (`zh-Hans` and `en`),
and the two packs stay identical in keys and placeholders (`tools/check_lang.py` gate).

## v0.2.1

> The first version aimed at **open-source publication**. For the complete implementation plan, key decisions,
> deviations from the task specification, unfinished items, and size records, see the internal development
> record (`v0.2.1-研发记录.md`, **not distributed with the release**).
>
> **This version is pre-cross-platform**: it has no `frontend-avalonia/` directory and no platform
> abstraction. The cross-platform work that was originally committed under this heading now lives
> under v0.2.2 above; the tag `v0.2.1` points at the last commit of this state. The installer and
> release artefacts for this version are rebuilt from that commit — see
> `records/v0.2.1-发布记录.md` §30.

### Capability round 1: fewer modes, an agent platform, MCP, plugin ecosystem, model management, retrieval

Task specification: `prompts/EchoDesk-v0.2.1能力扩展-1.md`. This round closes the four capability gaps that
stood out against mainstream local-inference clients (LM Studio / Ollama / Open WebUI / AnythingLLM) and tidies
up two cases of concept sprawl. Audit findings, the trade-off behind each item, deviations from the task
specification, size impact and unfinished work are in `records/v0.2.1-能力扩展-1记录.md`.

**Conversation modes went from six to three** (task 1): `chat` (general conversation, the default),
`contract` (clause review) and `agent` (tool-calling agent). The test is whether a user can say at a glance
which one to use, not which prompt block a task belongs to. No capability was lost — the removed modes simply
stopped occupying a mode slot:

| Old mode | How to use it now |
| :--- | :--- |
| general | it *is* chat (renamed) |
| summary | `/task summary` |
| translate | `/task translate` |
| code | `/task code` |
| creative | `/task creative` (new template this round) |
| guide | `/task guide` (new template this round) |

**The old spellings still work**: `/mode summary` switches to chat, applies the matching template, keeps the
old temperature preset and says what happened — nobody has to relearn anything. **Old numbers are no longer
migrated automatically**: a number means different things under the two schemes (the old `/mode 2` was
summary, the new `/mode 2` is contract), so an old number such as `/mode 4` fails with a message naming the
template to use instead of guessing its way into a state that is neither the old nor the new meaning. Task
templates went from six to eight (adding `creative` and `guide`).

**Agent platform** (task 2): the new `agent` mode lets the model decide which tool to call, in what order,
until it reaches a conclusion. First tool set: calculator (AST-whitelist evaluation, **not** `eval`), date
arithmetic, unit conversion, keyword lookup inside uploaded files, and document summaries. Tools are tiered:
the ones above are pure local computation over content the user already supplied and are enabled by default;
tools that send data out or read arbitrary local files (web search, reading files) are **off by default** and
need `/agent allow <tool>`. **This version ships no tool that writes files or runs commands** (least
privilege). Every step is visible in the conversation (`📍 step 1/4 → 🔧 calling calc → ↩ result …`), a denied
tool shows 🔒 and how to authorise it, Esc stops the loop at any time, and hitting the step limit (4 by
default) converges to "answer with what you have" instead of looping forever. Boundary against automation:
automation runs a predefined batch flow, the agent decides a single task on its own.

**MCP** (task 3): a **stdio MCP client** that connects to user-configured external MCP servers and exposes
their tools to the agent. The dependency decision was to **write it ourselves rather than pull in the official
`mcp` package**: that package drags in anyio/httpx/starlette — all of it server-side — while the release is
held inside a 350–400 MB budget; the protocol itself is JSON-RPC 2.0 over stdin/stdout, about 200 readable and
testable lines. `/mcp` shows status and tools, `/mcp enable|disable <name>` takes effect **immediately**
(no restart), `/mcp connect` starts the process and fetches its tool list, and `examples/mcp/echo_server.py`
(standard library only) is a runnable example. **The security boundary is stated plainly**: an MCP server is a
process the user configured, so its tools are **always treated as sensitive** and must be authorised
explicitly; parent-process environment variables are **not** passed through by default (secrets are not handed
to third-party processes); EchoDesk never discovers or connects to anything on its own. This version supports
stdio only (no HTTP/SSE, no resources/prompts).

**Plugin ecosystem** (task 4): the last gap in "install it and it works" is closed — installing from the
market now **hot-reloads automatically**, so commands work immediately; plugins can declare `dependencies`
(plugin-to-plugin, with `>=1.2` style requirements) and `echodesk.min_version` (minimum host version), and a
missing/disabled/version-mismatched dependency or a too-new host requirement **refuses to load and says why**;
commands declared but not registered (or the reverse) are reported as warnings; "Reload local plugins" now
reloads **only the development directory and the local manifest** instead of reinstalling everything; and a
plugin raising `SystemExit` is isolated instead of taking the backend process down.

**Model management** (task 5): the Model page gains an **inference-parameter panel** (temperature 0–1.5,
top_p, repeat penalty, max tokens as sliders with per-row reset, a generation-preset dropdown, context
strategy, two toggles; saving re-reads the values that actually took effect); **custom model tiers** can now be
created, edited, renamed and deleted (alias, path, repo, context length, temperature, note) without hand-editing
`config.json` — deleting a tier **does not delete the model file**, and a tier in use refuses to be deleted.
Out-of-range values now **error instead of being silently clamped** (previously `temperature=9` was quietly
turned into 1.5 while reporting "saved").

**Retrieval (RAG depth)** (task 6): new **local document retrieval** (BM25 keyword search, standard library
only, no new dependencies). The division of labour is documented and implemented: short documents are **not**
searched — the full text is injected (most accurate); long documents get **summaries for the whole picture plus
retrieval for the details**, with the retrieved passages carrying file name and character range, placed before
the file blocks, and the model is asked to cite `[n]` so the user can check the source. Search spans every
uploaded file (multi-document Q&A without naming files), and indexing is **incremental**: a new file indexes
only itself, a same-named file replaces its own entry, and a removed file stops being searchable. **Why no
vector store**: it plus an embedding model adds tens to hundreds of megabytes against the size budget, and
changing the chat model invalidates the index — keyword retrieval can also explain *why* it matched.

**Pre-existing defects fixed along the way** (all found by this round's audits and tests):

- `config.files.total_chars` was never assigned on the production path, so the multi-file injection budget
  (60000 characters by default) never actually applied — injection was effectively unlimited;
- a plugin command raising `SystemExit` / `sys.exit()` escaped the `except Exception` guard and took the
  backend process with it (the user saw "the program just vanished when I ran that command");
- a plugin's `min_version` (minimum host version) was parsed and displayed but **never checked at runtime**;
- a version requirement written as `v1.2.0` was silently ignored (the pattern did not accept the `v` prefix),
  which bypassed the new version check entirely.

### System-prompt reconstruction: from "fill in the template" back to "exercise judgement"

Task specification: `prompts/EchoDesk-v0.2.1系统提示词重构.md`. This round adds no new prompt content; it fixes
the side effect of the previous round's "six elements": with the constraints piled up, the model lost its
judgement — it listed three points for any question, refused to answer when information was incomplete, and
merely asked the user to clarify when they could not express themselves. The diagnosis (traced to the exact
sentences), the per-mode adjustments, the comparison-test results and the deviations from the task
specification are in `records/v0.2.1-系统提示词重构记录.md`.

**The shared requirements grew from 3 rules to 7, and all four new ones loosen rather than tighten:**

- **decide the detail level**: one sentence when one sentence is enough, points only when genuinely complex,
  never pad for the sake of a format;
- **answer first, ask later** (the old rule 2 with its **order reversed**): when information is incomplete,
  answer with the most likely reading first, state the assumption, and only then ask whether to expand on
  other cases — **never reply with just "please provide more information"**;
- **lay out the speculation**: only when the conclusion really depends on information that was not given,
  write "if A, then X; if B, then Y" and mark it as a guess;
- **guide**: for a vague question (such as "take a look at this"), list 2-3 possible directions with one line
  each about what it would do.

The floor — "say you do not know; never fabricate facts, figures, statutes, clauses or sources" — is untouched.

**Every mode gained a sixth element, "detail level"**, and the format requirements became conditional: the
summarizer adapts to material length (one paragraph **without** points for anything under ~200 characters,
points only for long material), code mode answers API-usage questions in a sentence or two, contract review
lists the usual risks first when no material has been supplied and asks which one applies, and creative mode is
tightened to 2-3 directions. In other words, the judgement call about how much to say went back to the model.
**The length caps were not relaxed** (mode block ≤ 480, whole prompt ≤ 700 characters): the new content was
paid for by compressing existing wording, and the caps are still enforced by assertions.

**New guidance mode** (`/mode guide`, also `/mode 7` or `/mode gd`, temperature preset 0.6): when you cannot
say what you want, it lists 2-3 things you might mean, one line each about what it would do, and you reply with
a number. The fallback wording for vague questions changed from "please supply the object, the output form and
the constraints" to the same list-of-directions idea, and it **picks the directions from your wording** (mention
an error and "diagnose the symptom" comes first; mention a file and "work on the material" comes first).
In guidance mode that step is left to the model so the directions fit your wording more closely. The
machine-readable marker for a vague question is still `clarify: true`; set `Config.clarify_vague` to `false`
to switch the behaviour off.

**Task templates were relaxed to match, except machine-readable ones**: the `extract` JSON template is marked
`relax=False` and still ends with "strictly follow the output format above", while the others now end with
"the format requirements win, but the detail level still follows the complexity of the question — a simple
question may be answered in one sentence". The `reason` template used to say "ask first when information is
missing", which is exactly where the refusal behaviour came from; it now says "give the most likely conclusion
first and label the assumption", with a new `【假设】` (assumptions) field.

**Objective comparison testing** (same model, same question, only the prompt changes):
`tools/prompt_compare.py` takes the pre-reconstruction prompts from git history and runs old against new, and
`tools/test_prompt_compare.py` covers the model-free part of it (now part of CI and the release gate). The
first measurement round found three real defects — the summarizer's "no bullet points for short material" did
not take effect, contract mode invented "verbatim quotes" when no material had been supplied, and guidance
mode parroted the example phrase from its own prompt — all three were fixed in the prompts. Whether a reply
uses points, only asks back, offers directions, quotes the source, or fabricates volume/page/amount figures is
**judged automatically**; the subjective assessment is labelled as such in the record. Measured on both tiers
(the bundled 0.6B default and the optional 1.7B), the objective criteria went from **2/9 to 7/9** on 0.6B and
from **4/9 to 9/9** on 1.7B; the two remaining failures are on 0.6B and fail under both the old and the new
prompt — **the 0.6B tier cannot be trusted to quote a source it was not given, nor to answer questions about
statutes**, which is now a documented limitation.

### Known-issues round 2: extraction prefilter, streaming heartbeat and live metrics, search chain, compaction, multi-file upload, `$$` formulas

Task specification: `prompts/EchoDesk-v0.2.1优化已知问题-2.md` (17 tasks). Every item was audited against the
source first and only the unfinished ones were implemented; per-item findings, decisions, deviations and
everything deliberately left out are in `records/v0.2.1-优化已知问题-2记录.md`.

**Long documents: extraction prefilter (task 4).** New `backend/extractive.py` shrinks every chunk before it
reaches the model, using TextRank plus a **rarity** term and an information-density-aware ratio. Pure standard
library (no jieba/numpy), ~8 ms per 3000-character chunk. Measured on a 47 KB document through the real chunking
path: `50421 → 17880` characters (**2.8× less input**), 136 ms total.

- Plain TextRank points the wrong way here: it picks sentences that *resemble the others*, i.e. boilerplate,
  while the facts that matter (`1,860,000 元`, `2026 年 3 月 31 日`, `按日 0.3%`) appear exactly once and rank
  low. At ratio 0.18 the Chinese contract sample kept **0/6** key figures; with the rarity term and ratio 0.35
  it keeps 3/6 at the same 2.8× compression.
- Two-pass selection (score-first, then "score per character" backfill) stops one long sentence plus its
  neighbour from eating the whole budget while a dozen short clauses get nothing.
- Section titles at the start of a document are never dropped; chunks shorter than 600 characters are left
  alone; chunks above 12000 characters bypass the prefilter entirely (8000 sentences cost 22.9 s — the
  prefilter must not be slower than the model).
- Configurable via `summarize.extractive`, `extractive_ratio`, `extractive_min_chars`.

**Streaming: heartbeat, throttling and live metrics (tasks 5 and 7).** "Generates halfway, stalls, then a burst
appears" was traced to two layers, and neither was stdout buffering (every line was already flushed):

- `llama_cpp`'s generator **blocks for tens of seconds during prefill** — there is no data to flush, so nothing
  could ever be sent. `llm_engine._iter_with_heartbeat` now runs the stream on a reader thread with a bounded
  queue and emits a heartbeat every 1.5 s (`config.stream_heartbeat_seconds`) while the consumer waits.
- The front end rebuilt the thinking region's `FlowDocument` and scrolled the whole page **on every delta**.
  Both are now coalesced into a 50 ms tick (`AppSettings.StreamThrottleMs`); only the text accumulation stays
  per-delta.
- Stop now also reaches the reader thread (so a stop actually stops generation) and the discard boundary is
  pinned to the chunk that was in flight when the stop was noticed — content the user already saw is still
  delivered, content produced after the stop is not.
- **Live metrics from the backend** (never estimated in the UI): time to first token, output tokens (counted
  with the real tokenizer, flagged when estimated), token/s, elapsed time and whether the run was stopped. The
  status strip shows them during generation, keeps the final values afterwards, resets on the next request,
  shows "waiting for first token" before the first token, marks aborted runs as stopped, and hides the whole
  block when no model is loaded.
- Prefill now has its own status text ("processing input…") instead of a single generic "busy".

**Search results are actually used (task 2).** The four stages were checked one by one:

- Failures used to be returned as text (`"搜索失败: …"`), and the caller told success from failure with a
  string check — so failure text could be injected into the prompt as if it were material. Results are now
  structured (`SearchResult` / `SearchOutcome` with `ok` / `error` / `results` / `elapsed_ms`).
- Injection is a numbered list (`[1]`, `[2]`, …) with an explicit instruction to answer from those results and
  cite the number, plus an escape hatch ("say the results are insufficient instead of guessing"). Defaults:
  5 results, 400 characters each, 3000 characters total (~1000 tokens, about 12% of an 8192 window).
- An empty or failed search no longer injects anything and now tells the user which of the two happened
  (new bilingual `sys.search_failed` / `sys.search_empty`).
- `Backend.search_diagnostics()` exposes all four stages (triggered / returned / injected / used) for the
  status strip and for tests.

**Context compaction (task 9).** Trigger is now a configurable **token ratio** (`compact.trigger_ratio`, 0.70;
was a hard-coded 0.8). Added a **grace zone** of the most recent N turns (`grace_turns`, default 3) kept
verbatim, unioned with the token budget — previously a single long message could push out the turn that had
just happened. The summary prompt is customisable and `/compact focus on <what matters>` appends a focus
instead of replacing the prompt. Large tool/file/search outputs are truncated to head 60% + tail 30% plus a
length note before being handed to the compressor (the old `content[:300]` threw whole documents away).
Every compaction writes an event to `compact_log.jsonl` (append-only, redactable, replayable) recording the
trigger, thresholds, token counts before/after, how many messages were dropped and kept, the focus, the prompt
used and the summary text; `Backend.compaction_diagnostics()` and `replay()` answer "which sentences came from
which compaction".

**Multiple files at once (task 8).** The file dialog supports multi-select and drag-and-drop takes every
dropped path; both feed the same code path. Files are parsed one by one with their own status line
(`⏳ [i/n] name…` → `✅/❌`), one failure does not affect the others, and a single "loaded N files" summary
closes the run. Picking more than 10 files or more than 60 MB asks for confirmation first. Each file is given a
label when injected (`【文件内容 N/M：name】`); the total is capped by `files.total_chars` and injection starts
with the **most recently uploaded** file, with the files that did not fit named explicitly instead of being
dropped silently. Files are kept in a registry that can be listed, selected and removed individually, duplicate
names replace rather than accumulate, and `session.json` stores only names/summaries/sizes (never bodies).
Oversized bodies are spilled to `file_cache\` with a pointer kept in context. `/file` accepts several paths.

- Fixed while doing this: command arguments were split with `str.split()`, so **any path containing a space was
  broken** (`/file C:\My Documents\a b.docx` became four arguments). Arguments are now quote-aware:
  `/file "C:\My Documents\a b.docx"`.

**`$$ … $$` formulas in the front end (delivery requirement).** Block formulas (`$$` on their own line, possibly
several lines) render as a centred monospace block; inline `$…$` and mid-line `$$…$$` render as inline
monospace. No LaTeX typesetting: WPF has no native math engine and the alternatives cost a WebView2 dependency
or a third-party renderer, both out of proportion to "recognise `$$`". The risk here is mistaking **US dollar
amounts** for formulas, so the test is deliberately conservative (LaTeX characters, function names, Greek
letters, or a comparison operator between letters/digits); amounts such as `$100`, `$1.5M` and `$1,860,000`,
any content containing CJK characters, and anything longer than 200 characters stay plain text. Verified with a
probe that compiles the **real** renderer source (`artifacts/MarkdownProbe/`, 23 assertions): three formula
shapes, five dollar-amount cases, CJK content, and no regression in headings/code blocks/tables/bold/lists.

**Also verified this round (already working, no changes needed):** thinking content separation (task 1), context
window adaptation from GGUF metadata — `DeepSeek-R1-Distill-Qwen-1.5B` reports a 131,072-token training window
and converges the effective window to the minimum of requested/training/limit (task 3), and session history
storing only final messages rather than streaming chunks (part of task 5).

**Deliberately not in this release** (reasons in the record): conversation-area virtualisation (task 6 — the
front-end throttling already removes the main cost; virtualising variable-height `RichTextBox` items without
losing selection, thinking folds, code highlighting and theming needs a dedicated round), tool-call pipeline
(task 10 — only one call site would actually use it), session JSONL log and crash recovery (task 11 — to be
designed together with the new `compact_log.jsonl` event format), MCP (task 12), multi-model adapters
(task 14 — cloud endpoints conflict with the offline privacy promise), sub-agents (task 15 — serial CPU
inference makes multi-step decomposition slower than it is useful) and the task board (task 16 — needs a task
queue first).


### Default model switched to Qwen3-0.6B (thinking visible out of the box) + a single, model-free installer

> This section supersedes the earlier statements in this v0.2.1 block that described **two** installers
> (with models / `-NoModel`) and a default model that produces no thinking content.

**Why the default model changed.** v0.2.1 shipped `Qwen3.5-0.8B-Q4_K_M.gguf` as the default, but that
checkpoint **never produces thinking content**: its chat template gates the reasoning block on
`enable_thinking is true` and otherwise emits an **empty, already-closed** think block, and
`llama-cpp-python` 0.3.35 cannot pass that variable (`create_chat_completion` has 29 fixed parameters and no
`**kwargs` — read from the installed source). Measured before changing anything (probes; details in
`records/v0.2.1-默认模型切换记录.md`):

- the template baked into the Q6_K files of both `bartowski/Qwen_Qwen3.5-0.8B-GGUF` and
  `lmstudio-community/Qwen3.5-0.8B-GGUF` is **identical in that block** to the one already shipped (read
  straight out of the remote GGUFs' metadata over HTTP range requests, no full download) → swapping the
  repository or the quantisation **cannot** enable thinking;
- injecting `enable_thinking=true` at the load layer does start "thinking", but the model **never emits
  `</think>`** (10,383 characters / 4,096 tokens / 108 s with no closing tag), so the splitter sees a plain
  answer: no thinking region appears and the answer area fills with meta-commentary;
- two prompt-level routes failed as well (an explicit tag instruction is ignored; forcing the template
  branch makes the model loop on the instruction text — 3,573 characters, cut off mid-sentence);
- `Qwen3-0.6B` emits `<think>…</think>` by itself: against the **real backend**, `tools/test_reasoning.py
  --model …` separates 419 thinking / 119 answer characters with 0 resets and clean, correct answers.

**Changes**
- Default model is now `Qwen3-0.6B-Q6_K.gguf` (`unsloth/Qwen3-0.6B-GGUF`, 472 MB), so a default install
  **shows the "🧠 Thinking" region**; replies are also shorter and much faster (measured 5–9 s per reply on
  CPU, versus 21–108 s for the forced-thinking 0.8B, whose answers were unusable).
- Two model profiles are declared under `models`: `qwen3-0.6b` (the default) and `qwen3-1.7b`
  (`Qwen3-1.7B-Q4_K_M.gguf`, 1056 MB), so `/model qwen3-1.7b` switches tiers without editing files. The
  larger tier is not bundled — it is downloaded on demand (model market) or supplied by a model add-on pack.
- Context handling is unchanged: the GGUF metadata (Qwen3-0.6B trains at 40,960 tokens) plus
  `auto_context` converge the effective window to 8192, and the KV-cache estimate follows the new model.

**Model tiers are visible and switchable in Settings**
- The `config.models` registry was previously reachable only through `/model <alias>`, so the Settings window
  never showed which tiers exist or which one is already on disk. Settings → Model now has a
  **"🎚️ Model tiers"** strip: one chip per registered tier showing its parameter count, quantization,
  context window and size, with a marker on the tier in use.
- Clicking a downloaded chip **switches** to that tier (hot-loaded — no restart of the app or the UI, and the
  tier's own `context_length` / `temperature` are applied). Clicking a tier that is not downloaded yet
  **downloads** it: the app resolves the file inside that tier's own repository (`models.<alias>.repo`, which
  until now was a purely decorative field) and then reuses exactly the same streaming download as the model
  market.
- Deliberately a **compact single strip** rather than a tall list: the model tab's vertical space is already
  consumed by the model market (its result list is the `*` row), and a taller block squeezed that list to zero
  height — caught by the GUI probe, whose "list scrolls", "filter panel" and "pager stays put" assertions all
  turned red at once.
- New protocol actions: `model_profiles` (list; reads local files only, never the network) and
  `model_profile_apply` (switch). Covered by 15 new assertions in `tools/test_no_model.py`, plus a GUI
  assertion in `tools/ui_probe.ps1` that the tier data really reaches the window — and, because the first
  version of this feature shipped broken (next paragraph), a second GUI assertion that **clicking a tier
  which is not downloaded yet really reaches the download confirmation dialog**.
- **Fixed: clicking a not-yet-downloaded tier always answered "⚠️ could not read model details".** The new
  code read the response key `hub_detail`, while `_handle_model_hub_detail` returns `detail` — the backend had
  been sending the complete file list all along. Backend protocol tests and the end-to-end suite could not
  catch it (each side looks correct in isolation), so `tools/selftest.py` now cross-checks every `model_*`
  response key that the front end reads against the JSON keys `backend/main.py` actually returns; verified by
  injecting the original bug, where the new check goes red and names the file and the key.
- The same path no longer swallows the backend's real reason when a detail lookup fails, and it pins the
  lookup to the `auto` data source (mirror first, official as fallback) instead of following whatever the
  settings window currently shows: a user whose source was left on "official" would otherwise see every tier
  download fail on networks where `huggingface.co` is unreachable (measured: 21 s timeout versus 0.8 s
  through the mirror).
- Tier chips displayed `Q4K_M` instead of `Q4_K_M`: WPF renders a `Button`'s string content through
  `AccessText`, where `_` marks the access key and is swallowed. The label is now carried by a `TextBlock`.

**Fixed in the same round**
- `download_utils.find_best_model` gave every candidate containing `q4_k_m` an unconditional +0.3 bonus, so an
  explicitly configured Q6_K was resolved to Q4_K_M and the wrong file was downloaded silently (measured
  against both real repositories). The generic-quantisation preference now applies only when the hint itself
  does not name a quantisation, and file-name stemming is idempotent — using `os.path.splitext` truncated
  `qwen3.5-0.8b-q6_k` at the dot inside the version number and lost the quantisation code entirely.
  Regression fixtures use the real repository file lists.
- **`Backend.load_model()` crashed whenever a model was already loaded**: it calls `self.llm.load(...)`, but
  `load()` existed only on `LazyLLMEngine` (the no-model engine); the plain `LLMEngine` had just `reload()`.
  So switching to another model *after* one was loaded — the normal case for the new tier switcher, and
  equally the existing "Load" button in the local model library — failed with
  `AttributeError: 'LLMEngine' object has no attribute 'load'`. `LLMEngine` now exposes the same `load()`.
- **A market search served from the disk cache left the local query dataset empty**: `ModelHub.search()`
  returns early on a cache hit without registering `self._session`, which is what `model_hub_query`
  (paging / sorting / filtering / out-of-range page clamping) reads. The list itself displayed fine, so the
  second time the model market was opened within the cache TTL (6 h) the user saw results but any paging or
  sort answered "search first". The cache-hit path now registers the dataset too (which also re-connects
  "load more" after a cache hit).

**One installer, and it never bundles a model**
- There is now exactly **one** installer, `EchoDesk-v0.2.1-Setup.exe`, and it is always model-free: offline
  users are served by a separate model add-on pack. The `含模型` / `-NoModel` pair is gone, along with the
  `/DMyIncludeModels` build switch, the `-SkipWithModel` / `-SkipNoModel` options, the 850–900 MB size
  window, and the second real install/uninstall round in the acceptance script.
- The uninstaller now also deletes **everything the program creates while running**: `session.json`,
  `models\` (downloaded models incl. the HF cache), `ui_state.json`, `snippets.json`, the
  `*_registry.json` files, the market/summary caches, the automation log and processed list, both crash
  logs, `config.user.json`, the `cache\` folder, installed and in-development skills, and the
  `Start-EchoDesk-*.lnk` shortcut. **`results\` is never touched** (it holds the user's batch output), the
  uninstaller says so up front, and a guard rejects any "delete the whole `{app}`" entry.
- `tools/check_installer_deliverables.ps1` gained two real checks: it parses the `[UninstallDelete]` list
  out of `installer.iss` and fails when that list does not cover the runtime-artifact list, and it
  **creates** those artifacts (plus a file under `results\`) inside the installed copy, uninstalls, and then
  asserts that every runtime file is gone while `results\` survived.

### Model market overhaul (load everything, page through it, filter and sort locally)

**The problem**: searching the model market fetched a single remote page (20 entries) plus the default model,
so a user could see at most 21 of them; searching `GGUF` matches thousands of repositories, the rest were
unreachable and there was no pager at all. The list also showed "downloads in the last 30 days", which does
not match what people mean by popularity.

**Download metric changed to all-time downloads (task 1)**
- Requests now explicitly ask for `expand=downloadsAllTime`; both display and ordering use all-time downloads.
- The server's `sort=downloads` orders by **the last 30 days** (measured: that column is monotonic while the
  all-time column is not), and rejects `sort=downloadsAllTime` with HTTP 400. The remote sort therefore only
  decides *which* entries land in the fetch window; **the displayed order is always re-sorted locally by
  all-time downloads** — otherwise "sorted by downloads" and "showing all-time downloads" contradict each other.
- When an entry has no all-time field it **falls back to the 30-day figure and is labelled per entry**
  (`downloads_source`); the response carries `downloads_metric` and `downloads_fallback_count` and the status
  line says how many entries fell back. It is never silently shown as 0.
- The status line states the metric explicitly: all-time downloads, *not* the "last 30 days" figure the
  Hugging Face website shows.

**All results fetched automatically (task 2)**
- The first search pages through the remote source until every result for that keyword is loaded (the remote
  page size is 1000 — measured: any `limit` above 1000 is **silently truncated**), stopping when there is no
  next page.
- **Three caps** apply at once: total items (default 5000), request count (default 8) and elapsed time
  (default 90 s). Hitting one stops the load, reports "N loaded, more available" and shows a **Load more**
  entry point; an absolute ceiling of 20000 items bounds repeated continuation. Caps are counted **per round**,
  otherwise the second "Load more" would degrade to fetching exactly one item (hit during testing).
- **Progress feedback**: new streaming action `model_hub_load` reports `hub_progress` per page (items fetched /
  pages / elapsed); the UI shows "fetching: N loaded (x s elapsed)" plus a **Stop** button that keeps what was
  already fetched.
- **Retries**: a failed page is retried `download_retries` times with exponential backoff; a failing *first*
  request switches to the other data source first; repeated failure aborts and reports how far it got without
  discarding the results already received.
- **Cache**: in-memory session plus an on-disk cache (`<user data dir>/cache/model_hub/`, default TTL 6 hours,
  configurable). A repeat search for the same keyword hits it directly (measured 15.7 s → 0.24 s); **Refresh**
  bypasses it.

**Paging (task 3)**
- 20 entries per page, switchable to 50 or 100.
- Full control set: first / previous / next / last / jump-to-page (Enter applies) / "page x of y" / total count.
  Boundaries are correct: first and previous are disabled on page 1, next and last on the final page, an
  out-of-range page is clamped to the nearest valid page with a hint, and invalid input shows a hint without
  jumping.
- **The paging bar is fixed below the list** (its own `Auto` row, with the list in a `*` row), so scrolling the
  results does not move it (the UI probe scrolls the list with `ScrollPattern` and asserts the bar stays put);
  in narrow windows it wraps (`WrapPanel`) instead of being clipped.

**Live local filtering (task 4)**
- Filters the **already-loaded** results in real time and **never re-requests the remote source**: it fires
  150 ms after typing stops (debounce).
- Scope: repository name, description, tags. The list endpoint returns no model-card body, so "description" is
  composed from what the endpoint does give (`pipeline_tag` / `library_name` / tags); the full text still lives
  in the details panel (the deviation is recorded in the internal record).
- The status line shows the three numbers: "A after local filter / B loaded / C in total" (truthfully showing
  "N+" when a cap was hit).
- When local filtering matches nothing you get a **Search remote again** entry point instead of a dead end that
  looks like a network failure.
- Filtering recomputes paging from the filtered set and returns to page 1; filters survive paging and sort changes.

**Structured filters (task 5)**
- Five dimensions: parameter range (parsed from the repository name), quantisation (parsed from the name/file
  name), update time (from the API), downloaded state (local scan) and recommended (the existing hardware logic).
- **Within one dimension multiple selections are OR-ed; across dimensions they are AND-ed.** The task
  specification says "multi-select, AND between conditions", but AND within a dimension is necessarily empty
  (a model cannot be in "≤1B" and "20–40B" at the same time), so the standard faceted semantics are used and the
  choice is written up in the record.
- Options carry **facet counts** (e.g. "Q4_K_M (123)") computed on the set *after* the keyword filter but
  *before* the structured filters — otherwise ticking one box would make the others vanish. Zero-count options
  are hidden.
- The quantisation filter compares a **language-independent quantisation code** rather than UI text; entries
  with no quantisation stated go into an "unstated" bucket instead of being silently dropped.

**Sorting (task 6)**
- Four dimensions: all-time downloads / most likes / recently updated / parameter size. Switching the sort
  **re-requests the remote source** (each dimension is cached separately, so switching back hits the cache);
  the displayed order is then recomputed locally, because the server has no parameter-size sort and its
  download sort is not all-time.
- Ties always fall back to the repository name so results are reproducible — without that, paging can show the
  same entry twice.

**Performance and experience (task 7)**
- 20 per page, so no virtual scrolling is needed; local filter + sort + paging over 5000 entries measures around
  40 ms.
- Loading state is visible and non-blocking: progress bar, progress text and a Stop button.
- Error handling: unreachable remote, failed requests and malformed responses each have their own message plus a
  retry/continue entry point; a busy backend returns a structured `busy` flag so the UI can say "the backend is
  busy with another task" instead of "the model market is broken".
- Offline degradation: when the remote is unreachable the **on-disk cache plus locally downloaded models** are
  shown, with the status line marked "📴 Offline mode".
- The default model is **pinned separately above the result list** with its source labelled (the default model
  repository from `config.json`) and is **not counted in the paging totals** (the specification allows this).
- A latent performance bug was fixed along the way: `_is_installed_repo` re-read the model registry file for
  *every* result — 5000 disk reads for 5000 entries. It now computes the "downloaded repositories" set once and
  caches it (invalidated on download / delete / rescan).

**One search is now enough (four defects the real-window probe exposed)**

The UI probe (`tools/ui_probe.ps1`) actually clicks through the model market, which forced out four
problems that only show up under real interaction:

- **The reason a load failed was swallowed**: the status line only showed the loader's message when
  "more available" was true, but "loading interrupted (0 fetched): …" is exactly the case where it is
  false — users saw an empty list with no explanation and had to **click Search or Refresh a second
  time**. That message is now always shown.
- **A round that fetched nothing did not retry**: one network hiccup left an empty list. Now, when the
  round ends in an error and nothing was fetched, it **retries once** after 1.2 s and says
  "retrying automatically" in the status line. A keyword that simply matches nothing
  (`stop_reason=none`) is not retried, because retrying would not help.
- **The facet checkboxes were hooked to the wrong event**: `Click` is not raised by WPF's
  `ToggleButtonAutomationPeer.Toggle()` (which accessibility tools and UI automation use) — it only
  calls `OnToggle()`. Mouse clicks worked; assistive-technology clicks did nothing. Now hooked to
  `Checked`/`Unchecked`.
- **`GetInt` assumed every JSON number is an integer**: `recommend_basis.ram_gb = 31.7` is fractional,
  and reading it as `Int32` threw, so **every render of the recommendation basis reported
  "local filtering failed"**. It now prefers integers and rounds fractions.

**Localisation and documentation (task 8)**
- The paging controls, filters, status messages and error messages all go through the language packs, complete in
  both Chinese and English (80 new front-end keys, 14 new back-end keys), with `tools/check_lang.py` as the gate.
- `USER_GUIDE` (both languages) gains a full "How to use the model market" section; `FAQ` (both languages) gains
  three new questions: "why do I only see some of the results", "why do the download counts differ from the
  website" and "how filtering interacts with paging".
- `AUTOMATION_GUIDE` is not affected: automation never goes through the model market (recorded as a condition
  that does not apply).

**Tests**
- New `tools/test_model_market.py` (134 assertions): a local HTTP stub emulates the HF `api/models` cursor
  paging, `expand` semantics and the "duplicate expand is 400" behaviour, covering the metric fallback, all three
  caps, retry and abort, continuation, the on-disk cache and its expiry, offline degradation and default-model
  pinning, plus the query engine's paging boundaries, sorting, structured filters and three numbers. It then
  drives the real backend through `model_hub_load` + `model_hub_query`.
- `tools/ui_probe.ps1` gains a model-market interaction block (2c): clicking the pager buttons in a real window,
  switching the page size, scrolling the list to prove the paging bar does not move, expanding the filter panel
  and ticking a facet, forcing an empty result to check the escape hatch, and resetting the filters.
- The old "every result must have an update time" assertion in `tools/test_model_hub_e2e.py` is now
  source-aware: remote results must carry downloads and an update time, while local/default entries must carry a
  source label. The old assertion demanded a field the endpoint never returns and had been red (18/19 measured).

### Thinking content separated from the answer

**The problem**: reasoning models (Qwen3 thinking mode, the DeepSeek-R1 family) write their thinking into
`content`, so the "answer" a user sees opens with a long stretch of ` thinking…` reasoning — impossible to
tell apart from the conclusion.

**Facts first (task 1)**: the output structure was measured with probes before any code changed (see
`records/v0.2.1-模型思考内容分离记录.md`):

- `llama-cpp-python` 0.3.35 has a **fixed 29-parameter `create_chat_completion`**: no `reasoning_format`,
  no `chat_template_kwargs`, no `enable_thinking`; `llama_types.py` has **no `reasoning_content` field at
  all**; streamed deltas only ever carry `content` / `role`. **Inference-layer separation is unavailable**
  (task 2 was dropped on this evidence).
- Two model-side shapes were measured:
  - Qwen3-0.6B: with `enable_thinking` undefined the template adds **no prefix**, so the model emits
    `<think>` … `</think>` itself — both tags are in the generated text;
  - DeepSeek-R1-Distill (unsloth GGUF): the template **hard-codes the opening tag** in its
    `add_generation_prompt` branch, so the generated text only has the closing tag — "closing tag without an
    opening tag" must therefore mean "everything so far was thinking", or R1's reasoning lands in the answer.
- Also measured: the shipped default model (Qwen3.5-0.8B) inverts that template condition (`is true`) and the
  binding cannot pass the parameter, so it produces **no thinking at all** — recorded honestly so nobody
  reads it as "the feature does nothing". (**Superseded later in this version**: the shipped default is now
  `Qwen3-0.6B-Q6_K.gguf`, which emits both tags itself — see the default-model section at the top.)

**Application-layer fallback (task 3)**
- New `backend/reasoning.py`: an incremental tag splitter (`ReasoningSplitter`) that turns one text stream
  into `reasoning` / `answer` channels and handles every boundary: tags split across chunks (correct even
  character by character), nested tags (paired by depth), several thinking blocks (merged), an **unclosed**
  tag (everything left goes to thinking, `unclosed` is set), an **orphan closing tag** (text already sent to
  the answer channel is reclassified as thinking, with one `reset` telling the UI to discard the answer
  area), and no tags at all (non-reasoning models are unaffected).
- The tag table is configurable (`reasoning.tags` appends custom pairs); built-ins cover `think`,
  `thinking`, `reasoning`, `thought`, `reflection`, `analysis` plus DeepSeek's special-token forms; longer
  tags win (`<thinking>` is never truncated by `<think>`).
- `LLMEngine.chat_stream` / `chat_complete` keep their **answer-only** semantics (chunked summarisation,
  automation and context compaction therefore never see thinking), with the two channels exposed through the
  new `chat_stream_split` / `chat_complete_split`.

**Protocol**
- Streamed delta lines gained a `channel` field: `reasoning` or `answer`; plus one
  `{"channel":"answer","reset":true}` line that invalidates the answer area.
- The `done` line gained `reasoning` (full thinking) and `reasoning_unclosed` (the tag never closed, usually
  because thinking used up the token budget). Deltas without `channel` keep their old meaning (progress /
  summary text).
- Fixed: the accumulated answer now comes from the splitter's `answer_text`; otherwise `done.content` dragged
  thinking along in the orphan-tag case (caught by unit test C2).

**Frontend (task 4)**
- A collapsed “🧠 Thinking” region (a native WPF `Expander`) sits above the answer: click the header to
  expand, body rendered in italic secondary colour inside a tinted, bordered block so it is unmistakable.
- While streaming it expands automatically so you can watch the model reason, and it **collapses again the
  moment the answer starts**; a manual expansion is never overridden. When there is no thinking, **no region
  is created** (no empty placeholder).
- The copy buttons and `Ctrl+Shift+C` still target only the answer (`Tag="content"`); the thinking region uses
  `Tag="reasoning"` so it is never copied by accident. Ctrl+wheel font scaling applies to it too.
- If thinking ate the whole length limit and no answer started, the region expands and the answer area
  explains why, instead of leaving a blank bubble.

**Judgement calls (recorded with reasons)**
- **Thinking is persisted** in session history (its own `reasoning` field, with a configurable off switch and
  truncation cap): task 5 requires reloading a session that contains thinking and showing it correctly, which
  is impossible without persisting it.
- **Thinking never enters the context**: it is usually longer than the answer, DeepSeek's own API forbids
  sending `reasoning_content` back, and R1's GGUF template itself drops everything before `</think>`. Old
  sessions without the field still load, and ordinary models never grow the field.
- **Export**: `/save` writes thinking as a collapsible `<details>` block placed before its answer.

**Localisation and docs (task 6)**
- New keys: `main.reasoning.title/count/tip/noAnswer` (4 per locale in the frontend) and
  `export.reasoning` / `export.reasoning_truncated` (2 per locale in the backend).

**Tests**
- New `tools/test_reasoning.py`: **95** unit + protocol assertions (splitter boundaries, engine channels,
  streaming protocol channel/reset/done, history persistence, "thinking stays out of context", language
  keys), all driving the real backend code in-process — no network, no model. `--model <gguf>` adds a
  real-model end-to-end run (measured: Qwen3-0.6B 388 chars thinking / 48 chars answer with a clean answer
  area; DeepSeek-R1-Distill-Qwen-1.5B 375 / 127, reclassified by a single `reset` → **104/104**).
- Wired into the pre-release gate `tools/verify_release.ps1`, CI, and `CONTRIBUTING`.
- `tools/ui_probe.ps1` gained a "thinking region" check (`-ThinkingModel <gguf>` points it at a model that
  actually thinks): the answer body must contain no thinking tags, the region starts collapsed, expands and
  collapses again, and shows real thinking text when expanded.

**Defect found by the UI probe: a BOM in a config file killed the backend at startup**
- Symptom: writing the probe's temporary `config.user.json` with Windows PowerShell 5.1's
  `Set-Content -Encoding UTF8` (which emits a BOM) made the backend exit with
  `json.decoder.JSONDecodeError: Unexpected UTF-8 BOM`, taking the whole app down with a bare
  "backend exited unexpectedly" and no hint that a config file was involved.
- Root cause: `Config.load` read config files as plain `utf-8`. Notepad on Windows saves UTF-8 with a BOM
  too, so **a single hand-edit of the config could make the program unopenable** (the protocol layer already
  tolerated a BOM on stdin; the config side had been missed).
- Fix: read config files as `utf-8-sig`; `config.user.json` is an optional overlay, so a malformed one is now
  **warned about and ignored** instead of taking the backend down; the session file is read as `utf-8-sig`
  as well.
- Regression assertions added to `tools/selftest.py` (BOM on the base config, BOM on the overlay, corrupt
  overlay) — **80/80 passing**.

### Studio name finalised (熵寂工作室 / Void Entropia Studio)

- The Studio's Chinese name is now officially **熵寂工作室**; `VoidEntropiaStudio` remains the
  English identifier (legal entity, GitHub account, build metadata).
- **Chinese documents and the Chinese UI use the Chinese name**; English documents and the English
  UI keep `Void Entropia Studio`. The mapping is expressed through the language packs
  (`app.studio` / `app.studio_full` on the backend, `main.studioName` / `main.studioNameFull` in the
  front end), so neither side can display the other language's name by accident.
- First occurrence uses the full form — `熵寂工作室（VoidEntropiaStudio）` in Chinese,
  `Void Entropia Studio (熵寂工作室)` in English (`/about`, the welcome message, the about box);
  later occurrences use the short form.
- Chinese legal translations (EULA / NOTICE / LICENSE) now state the copyright as
  `版权所有 (C) 熵寂工作室（VoidEntropiaStudio）` and note that **the legal entity is governed by the
  English version**.
- Deliberately **unchanged**: the `CompanyName` / `LegalCopyright` fields written by rcedit, the
  `studio` field of the `ready` message, `docs/SBOM.json`, the build and packaging scripts, and the
  GitHub account — all of these stay English.

### No-model ready state, model market listing, and summarisation feedback

**No-model ready state (optimisation task one)**
- When the configured model file is missing, the backend **no longer blocks waiting for an answer and no
  longer exits**: it starts with an empty engine, keeps serving requests, and reports `model_ready: false`
  in `ready` together with a `model_missing` block and a guidance string.
- `need_download` is **kept but demoted to a non-blocking notice** (`blocking: false`), emitted once before
  `ready`. The old "ask, and exit if declined" flow is gone, along with its two helper functions and three
  now-dead language keys. Quitting is only ever user-initiated (the UI sends `exit`).
- Behaviour boundary without a model: `chat` / `chat_stream` return the guidance instead of failing;
  segmented summarisation and batch automation say up front that they need a model; file parsing, settings
  read/write, the skill market, session history and the model market (search / download / registry) all keep
  working. Token counting falls back to a character estimate, so context-usage display still works.
- The `ready` message and every `context` snapshot now carry a machine-readable `model_ready` flag, so the UI
  can tell "the backend is up" apart from "inference is available".

**Getting a model without restarting (optimisation tasks two and five)**
- The guidance lists three concrete paths plus the **real** path of the models folder: the model market,
  copying a `.gguf` file in by hand, or loading an already-downloaded model.
- New action `model_scan` ("Rescan models folder"): re-reads the models folder, registers `.gguf` files that
  were copied in by hand, and **auto-loads** the model when exactly one is found.
- New action `model_load`: hot-loads a model **into the same engine object**, so nothing else holding a
  reference to the engine needs re-wiring. Failures (corrupt file, unsupported format, out of memory) keep
  the no-model state, report the reason, and leave the retry path open.
- Downloading from the market now hot-loads the model when the download finishes; the response carries
  `model_state`, and the UI switches from "no model" to normal by itself.

**The model market shows everything (optimisation task three)**
- Root cause of "the default model never appears in the list": the market lists **repositories**, and the
  list was built purely from one page of download-ranked remote search results, so nothing guaranteed that
  the repository containing the configured default model would show up (a hardware filter, a search keyword
  or a page boundary could each hide it). The fix is not a looser filter but **pinning the default model's
  repository into the list**, independently of the query.
- The list is now merged from three sources and de-duplicated by repository: remote search results, the
  default model, and locally downloaded models — so an unreachable mirror still shows something useful
  instead of an empty list.
- Hardware recommendation (the existing RAM/CPU probing) now only **adds a mark**: recommended entries carry
  `recommended` / `param_b` / `recommend_reasons`, and the response carries a shared `recommend_basis`.
  Nothing is hidden. The UI renders the mark from the language pack — `(推荐)` in Chinese,
  `(Recommended)` in English.
- Ordering: the default model first, then recommended entries, then the requested sort order.

**Long-document summarisation feedback (optimisation task four)**
- Summarisation now emits **structured progress events** after every step (phase, segment index/total,
  status, elapsed time, characters, generated length, plus running counters), and the UI renders them live
  in the chat instead of only writing them to stderr where nobody could see them.
- **Heartbeat**: each segment is generated through the streaming API, so a status line is emitted at least
  every `heartbeat_seconds` (default 5 s) — long silences are now impossible by design.
- **Real interruption**: the same token loop checks the stop flag, so pressing stop aborts the current
  segment immediately instead of after it finishes. Aborting keeps the raw document in context and
  **does not** inject a half-finished summary.
- **Timeout**: `chunk_timeout_seconds` (0 = unlimited) with `chunk_timeout_action` =
  `skip` (default) / `retry` (retry once, then skip) / `abort`.
- **Slow is not broken**: a segment exceeding `slow_chunk_seconds` is reported as slow (with its elapsed
  time) and keeps running; real failures (call exceptions, timeouts, every segment failing) get their own
  error text and recovery advice. A run that had to fall back for some segments says so instead of
  pretending it was complete.
- Logs record segment index, character counts, elapsed time and status only — **never the document body**.

### Automation improvements (login state reuse / href extraction / visual selection semantics)

**Login state reuse (automation improvement task one)**
- Automation now uses Playwright's **persistent user data directory** (`launch_persistent_context`):
  after logging in once, cookies / localStorage are persisted to disk by the browser itself, so
  **later runs no longer require logging in again**.
  The data directory is dedicated to automation (`login_state_dir`, default `browser_profile`)
  and is not shared with the everyday browser — a browser does not allow two instances to share the
  same user data dir.
- **First-run guidance**: when the session is judged not logged in, automation pauses and prompts for
  manual login, then continues automatically once you have finished logging in; on first use the prompt
  states "first use requires you to log in manually".
- **Login state detection**: prefers the "element that only appears after login" (`login_check_selector`,
  which can be picked with visual selection), and falls back to login page URL keywords
  (`login_url_keywords`) when it is not configured; when neither has information it
  **does not guess and does not block**.
- **No forced run when not logged in**: when the session is still not logged in after waiting the full
  `manual_login_wait_seconds`, it explicitly reports "no login state detected" and ends the round,
  instead of letting every task fail once.
- **Clearing login state**: the settings window adds "🗑 Clear automation login state", which deletes the
  data directory (and the old `browser_state.json` backup) so that the next run guides login again;
  it is refused while automation is running.
- **Shared across sites**: one data directory can serve multiple target sites (the browser stores cookies
  per domain).
- **Smooth upgrade for existing users**: `browser_state.json` is still imported once into the new
  directory on first use, so no re-login is needed.
- **Audit**: new `login_state_used` / `login_required` / `login_completed` / `login_timeout` /
  `login_state_cleared` / `login_state_migrated` events record only paths, results, and counts,
  **never cookie contents**.
- Not implemented (advanced option documented only): connecting with CDP to a browser already started in
  debug mode (`connect_over_cdp`).

**Correct flow for href extraction (automation improvement task two)**
- Makes explicit that "a selector is only responsible for **locating the element**; reading attributes is
  a separate step", and implements a four-step flow:
  locate → read `href` (with `data-href` / `data-url` fallbacks) → `urljoin` relative paths into
  absolute addresses → fall back for special cases.
- Handling of the five kinds of values: relative paths are completed, absolute addresses are used as is,
  and `javascript:` pseudo-links / `#` in-page anchors / empty href are all
  **changed to clicking the element to trigger navigation** (catching either a new tab or a navigation in
  the current page), instead of failing outright.
- **Deduplication defect fixed**: deduplication used to be keyed on `href`, so dozens of tasks with
  `href="javascript:void(0)"` on one page were collapsed into 1; now only elements for which a URL was
  obtained are deduplicated by URL.
- The extracted `href` values and the resulting verdict are emitted with progress events to make
  troubleshooting easier.

**Semantics of visual selection (automation improvement task three)**
- **Preview after selection**: a confirmation dialog appears as soon as selection completes, listing
  "the program will extract the following URLs from these elements: …", and the configuration is written
  only after confirmation; cancelling discards the selection (the configuration is unchanged).
- **Non-link element detection**: when an element from which no URL can be read, such as a button / `<div>`,
  is selected, it explicitly reports
  "what you selected is not a link (<button>), so no URL can be extracted; please select again",
  **and it does not corrupt the configuration**;
  if the selected container contains links, the message states how many links the container has and
  suggests selecting a link element inside the container instead.
- **Empty href / pseudo-link notice**: the preview states "no URL could be extracted (JavaScript
  pseudo-link); the program will click the element to trigger navigation instead".
- **Selection semantics map to program logic**: each of the four selection positions has an explicit
  purpose (navigation target / row container / click only / download), and
  **only the two positions that must be links** reject non-link elements.
- A notice is shown when only 1 element matches (selectors produced by visual selection are
  "exact to that one element"), guiding the user to generalize the selector or to select a whole row in
  the "task row container".
- The banner text at the top of the page is no longer hard-coded Chinese and follows the interface language.

**Settings interface and language packs**
- The "Automation" page of the settings window gains a "Login state" section: data directory,
  login check element (with visual selection), login page keywords, the
  "🗑 Clear automation login state" button, and a login state status line (showing only
  path/size/time, **never cookies**).
- New `tools/test_automation_login.py`: runs a real browser against a local loopback HTTP site, covering
  the five tests required by the task specification
  (login state reuse, not-logged-in detection, the five kinds of href values, non-link elements,
  selection preview), with all 52 assertions passing.

### Release preparation (release task specification)

**Deliverables**
- Two new **installers**: `EchoDesk-v0.2.1-Setup.exe` (with models) and
  `EchoDesk-v0.2.1-NoModel-Setup.exe` (without models), compiled by **Inno Setup 6**
  (`tools/installer.iss` + `tools/make_installer.ps1`).
  **By default it installs into the folder where the installer itself resides** (the wizard has a path
  selection page, so it can be changed elsewhere); after installation the `[Icons]` section creates the
  Start menu shortcut (a desktop shortcut can be checked) and registers the uninstall entry;
  the installed directory structure is exactly the same as the portable version. Version resources are
  written at compile time by the `[Setup] VersionInfo*` directives
  (`FileVersion`/`ProductVersion` = `0.2.1.0`).
- New `SHA256SUMS.txt` (SHA256 list of the installers and compliance assets), `USAGE.txt` (structure and
  usage instructions, bilingual Chinese/English), `config.example.json` (copy of the backend default
  configuration), `install-complete.bat`
  (**changed to be for the zip portable version only**, creating shortcuts manually; the installer no
  longer calls it).
- The build script gains a `-MakeInstaller` switch: one command produces the complete deliverable set
  (`tools/make_installer.ps1` performs the actual packaging). The old name `-MakeSfx` is kept as an alias.

**Installation method: changed from a 7-Zip self-extracting package to Inno Setup (measured conclusion, do not revert)**
- **The `7zS.sfx` / `7zSD.sfx` self-extracting approach is dropped.** Measured in a VM: after the user
  double-clicks the self-extracting package it reports extraction complete, yet no EchoDesk file can be
  found anywhere on the disk — because the official 7-Zip SFX module **supports only 7 configuration keys**
  (`Title`/`BeginPrompt`/`Progress`/`RunProgram`/`Directory`/`ExecuteFile`/`ExecuteParameters`),
  **does not support `InstallPath`**, and its default behaviour is exactly "extract to `%TEMP%\7zNNNNNN`,
  run `RunProgram`, then delete that directory". These conclusions were verified by checking the
  strings embedded in the binary in both ASCII and UTF-16 encodings.
- `publish\7zSD.sfx` in the repository and the official 9.20 `7zS.sfx` **are the same file**
  (version resources `InternalName=7zS.sfx`, `OriginalFilename=7zS.sfx.exe`,
  `Copyright (c) 1999-2010 Igor Pavlov`, `FileVersion 9.20`) — **renaming does not get you `InstallPath`**.
  So no `.sfx` file is relied upon any more: `tools/make_sfx.ps1` and `tools/check_sfx_deliverables.ps1`
  have been deleted.
- **Never run `rcedit` on the final `Setup.exe`** (measured to destroy the installer outright): Inno Setup
  **appends the installation data after the PE image** (measured on the smallest installer: the last PE
  section ends at 893,952 bytes while the file is 2,097,164 bytes long), and when `rcedit` rebuilds the PE
  image it discards the appended data — the file is truncated to 893,952 bytes, errors at runtime, and
  installs nothing. This is **the same mechanism** as the
  "603.8 MB self-extracting package truncated by rcedit into a 176 KB empty shell" hit during this
  version's release preparation. Version resources are instead written with Inno's own `VersionInfo*`
  directives; `signtool` is safe (signatures are appended at the end of the file).
- The `Excludes` semantics of `[Files]` in the installer were measured against 7 control groups: patterns
  match **paths relative to the `Source` directory** (not full paths including the source directory
  prefix), and both `app\backend\models\*` and `*\models\*` match, while writing
  `*\app\backend\models\*` fails to match. The no-model build also uses `*.gguf` as a backstop, making
  the assertion "there is not a single `.gguf`" structurally guaranteed.

**Naming conventions: program-generated names are English too** (aimed at a global user base)
- `使用说明.txt` → `USAGE.txt`; the results directory `批量处理结果\` → `results\`;
  the result file `结果.txt` → `result.txt`; the default task directory name `自动化任务` → `auto-task`;
  failure screenshots `失败截图_任务N_时间.png` → `failure-screenshot-taskN-时间.png`;
  the shortcut `EchoDesk-v0.2.1.lnk` → `Start-EchoDesk-v0.2.1.lnk` (the zip portable version keeps this name).
- **Releases no longer ship a pre-made `.lnk` shortcut**: a Windows shortcut stores an **absolute path**,
  and a `.lnk` generated on the build machine carries the maintainer's local path
  (`C:\Users\<user name>\...`), which both violates "global requirement five" of the task specification
  (personal absolute paths are strictly forbidden) and is inevitably a dead link on the user's machine.
  Shortcuts are now always created **on the target machine**: the installer creates the Start menu/desktop
  shortcuts directly with the `[Icons]` section; users of the zip portable version run
  `install-complete.bat` once by hand (or start `app\EchoDesk.exe` directly, in which case the program
  asks on first run whether to create a shortcut).
- **Upgrade note**: the results directory name is user data, so renaming it **does not migrate** the old
  directory automatically. Before upgrading, rename the old `批量处理结果\` to `results\` by hand
  (or move the old directory's contents into the new directory),
  otherwise the new version starts fresh under the new name while historical results stay in the old
  directory (no data is lost, it is simply in two places).

**Compliance documentation bilingualized (7 pairs)**
- `LICENSE` / `NOTICE` / `SECURITY.md` / `CODE_OF_CONDUCT.md` / `docs/PRIVACY.md` /
  `docs/EULA.md` / `docs/THIRD_PARTY_LICENSES.md` are all changed to "English as the default name plus a
  `.zh-Hans.md` Chinese reference translation", and the Chinese versions note at the top that "in case of
  ambiguity with the English version, the English version prevails".
- The body text of `docs/PRIVACY.md`, `docs/EULA.md`, and `docs/THIRD_PARTY_LICENSES.md` was originally
  Chinese; this change makes English the default version and moves the Chinese into `.zh-Hans.md`;
  the authoritative security policy moves to the repository root, and
  `docs/SECURITY.md` becomes a short pointer.
- `CHANGELOG.md` moves to the **repository root** (recognized directly by GitHub); no copy is kept under
  docs\.

**Unified feedback channels**
- Public feedback uniformly points to GitHub Issues and Discussions; security vulnerabilities point to the
  private advisory entry on the repository's Security tab
  (preferred) and to a GitHub noreply address (fallback). All placeholder addresses
  (`security@example.com`, `conduct@example.com`) and the example donation URL have been removed.
- New gate `tools/check_feedback.ps1`: verifies that the above channels exist, and scans for personal
  email addresses, physical addresses, personal absolute paths, plaintext credentials, and AGPL remnants.

**Issues found and fixed during the compliance wrap-up (re-verified in a clean venv)**
- **`backend/requirements.txt` failed to declare 3 directly imported packages**: `requests`, `urllib3`
  (`marketplace.py`), and `tqdm` (`download_utils.py`). On the development and packaging machines they
  happened to exist as transitive dependencies, so the problem stayed hidden; building an environment from
  scratch per this file raises ImportError.
  New `tools/check_requirements.py` (imports extracted via AST + a real import test) serves as the gate.
- **The release contained an undeclared third-party component**: `cryptography` was bundled into the
  backend by PyInstaller (coming from `msoffcrypto-tool`, unrelated to this project, on the development
  machine) yet was absent from the license list and the SBOM.
  It **is genuinely required by features** (`file_parser.py` calls `reader.decrypt("")` for encrypted PDFs
  with an empty password, and pypdf's AES support depends on it), so the choice was to
  **declare it explicitly** rather than exclude it.
- It was also found that `chardet`, `bidi` (both LGPL), and `tzdata` were pulled into the bundle only by
  **optional imports** in dependencies and are unused by this project, so they were added to `EXCLUDES`
  in `backend/EchoBackend.spec`
  (the release therefore no longer contains those two LGPL components and is about 2MB smaller).
  New `tools/check_bundled_packages.py` compares "the packages declared in a clean venv" with
  "the modules actually present in `_internal`", to keep this kind of drift from creeping back in unnoticed.
- **Name normalization defect in `tools/gen_licenses.py`**: distribution names were compared directly with
  `name.lower()`, while the metadata names of `llama-cpp-python`/`duckduckgo-search` use underscores, which
  left those two
  **direct dependencies out of the "backend direct dependencies" table**. It now runs off requirements.txt
  plus PEP 503 normalization.
- **`tools/gen_sbom.py` ignored environment markers in the closure**: cross-platform marker dependencies
  such as `macholib`, `tomli`, and `exceptiongroup` were counted into the release closure, making
  `--check` fail forever in a clean environment (a dead gate).
  It now evaluates `Requires-Dist` markers against the current interpreter (`packaging` is an optional
  dependency, and when it is missing it degrades with a notice).
- Tightened the `EXCLUDES` comments and documentation in `backend/EchoBackend.spec` (during v0.2.1 it was
  found that `publish\7zS.sfx` does not support keys such as `OverwriteMode` that exist only in
  third-party builds; that self-extracting approach was later dropped entirely,
  see "Installation method: changed from a 7-Zip self-extracting package to Inno Setup" above).

**Build pipeline fixes**
- **`tools/build_release.ps1` returned exit code 0 even on failure**: an internal throw did not set an exit
  code, and `& tools\build_release.ps1 -MakeInstaller` once reported success while "not a single installer
  was produced".
  A top-level `trap` (uniform non-zero exit) and post-packaging deliverable assertions were added.
  The assertion was later hardened once more: checking only "the file exists" is not enough — a discarded
  old installer left in `publish\` could make it pass by mistake, so a deliverable must now be
  **regenerated by this build** (timestamp later than the start of packaging),
  and the packaging script deletes old artifacts with the same name before compiling.
- Releases no longer contain process documents such as development/fix records (excluded by name during the
  `-Recurse` copy plus a structural assertion).
- Release documentation no longer contains unfinished phrasings such as "screenshot placeholder / to be
  captured" (v0.2.1 ships no screenshots).

**New/updated automated checks**
- `tools/make_installer.ps1` (Inno Setup availability check + ISCC compilation of both installers
  + integrity assertions
  + optional signing + the full SHA256 flow, runnable standalone), `tools/installer.iss`,
  `tools/check_installer_deliverables.ps1` (**real install and uninstall** acceptance),
  `tools/check_feedback.ps1`, `tools/check_requirements.py`,
  `tools/check_bundled_packages.py`.
- The structural assertions in `tools/verify_release.ps1` expand to 36 required files + all-English naming
  + no process documents +
  no runtime leftovers + installer hashes/version resources/**appended data integrity** + real install and
  uninstall + two release gates.
- `tools/check_lang.py` gains a **shared bilingual document structure check**: the number of second-level
  sections in paired documents under docs,
  the section numbering sequence (supporting both `## 3.` and `## 三、` numbering styles), and the mutual
  declarations at the top of both versions.
- The scan scope of `tools/check_feedback.ps1` expands from "tracked files only" to
  **tracked files plus newly added workspace files (untracked but not ignored)**. Under the old
  implementation, "new files and changes in the same commit" could never be caught by the local gate —
  green all the way locally, red only after committing (hit in practice during v0.2.1).
  The release record was also added to the exclusion list for historical process documents.
- CI gains a `release-gates` job (feedback channels, dependency declarations, SBOM consistency, release
  script syntax);
  the release workflow now prepares **Inno Setup 6** (no longer downloads `7zS.sfx`) and produces both
  installers,
  attaches the SBOM and license list, and saves the Release as a **draft** by default.

**New: behaviour and compliance notes on reading encrypted documents (bilingual)**
- New `docs/ENCRYPTED_DOCUMENTS.md` (English default) and
  `docs/ENCRYPTED_DOCUMENTS.zh-Hans.md` (Chinese translation), with 7 chapters: behaviour, prerequisites,
  a statement that nothing is bypassed, attribution of responsibility, a no-persistence commitment,
  behaviour in unauthorized environments, and consistency with manual operation.
  Aimed at organizations that "protect Office documents with an enterprise document encryption system
  (such as Cobra DocGuard)", it explains that EchoDesk only reads **in memory through the locally
  authorized WPS**, does not touch encryption drivers,
  does not modify encryption policy, holds no keys, and that from the encryption system's point of view the
  read is identical to an employee opening the file manually.
- `docs/en/COMPLIANCE.md` and `docs/zh-Hans/COMPLIANCE.md` gain a dedicated section
  "加密文档读取的合规说明 / Compliance notes on encrypted document reading",
  and cross-reference the new document; a one-section quick-reference checklist gains two rows in step
  (how to enable it, how to forbid this path entirely).
- `README.md` / `README.zh-Hans.md` / `docs/en/README.md` / `docs/zh-Hans/README.md` /
  `docs/EULA.md` / `docs/EULA.zh-Hans.md` have all been wired up with links to and references to that document.

### Prompt optimization (system prompt rewrite)

**Mode prompts upgraded from one sentence to complete six-element prompts**
- The 6 modes in `backend/conversation.py` (general / summary / translate / code / contract /
  creative) previously had one sentence each (30 characters on average). The audit confirmed that
  **all of them were missing 4–6 design dimensions**
  (role specificity / output format / chain-of-thought guidance / few-shot examples / boundary statements /
  tone matching), and 5 of the modes
  were vague even about the role ("expert", "assistant"). They are now rewritten as a labelled
  structure — [Role] [Output format] [Thinking]
  [Examples] [Tone] — with the sixth element, "boundaries", placed together with perspective clarification
  and output discipline in the **shared backbone** `_CORE_RULES`:
  rules unrelated to the scenario are shared in one copy, so they neither waste context nor leave five
  places unupdated when one is changed.
- **Length is bounded and guarded by assertions**: a single mode block ≤ 480 characters (measured 209–313),
  mode block + shared backbone + language instruction ≤ 700 characters (measured 417–521), roughly 8.5% of
  the default 8192-token context,
  leaving headroom for file summaries and conversation history.
- **Measured results** (same model `Qwen3.5-0.8B`, same procurement contract, same question, same generation
  parameters,
  **with only the system prompt changed**): the old prompt produced a 500-token essay, hit the cap and was
  truncated, and invented a
  "lack of punitive damages" requirement that was not in the source; the new prompt outputs item by item in
  the order "source excerpt → risk point → suggested revision", explicitly points out
  the genuine omission that "no acceptance deadline was agreed", writes "requires manual confirmation"
  where it is uncertain, and finishes naturally at 421 tokens.

**Task templates now stack on the mode rather than replacing it (two mechanism defects fixed)**
- Templates applied by `/task` were previously written into `custom_prompt`, and since a custom prompt takes
  precedence in `get_system_prompt()` it skipped the mode prompt — so the template **replaced** the mode,
  directly contradicting the statement in `prompts.py` that it is "appended after the system prompt and
  does not replace the conversation mode". Templates now write into a new **task layer**
  (`ConversationManager.task_prompt`) and stack on top of the mode; when the two layers conflict, a newly
  added `[优先级]` line at the end of the template settles it deterministically, instead of leaving it to
  the model to guess.
- `/task off` previously also wiped the custom instruction the user had set with `/prompt`; it now clears
  only the task layer,
  and explicitly notes when a custom instruction is still in effect. When `/task` and `/prompt` override
  each other, each now says so as well,
  so "you typed a command but cannot tell what happened" no longer occurs.
- The pre-existing "do not ask back" wording in the `/prompt` wrapper directly contradicted vague-question
  detection and the `reason` template's "ask a follow-up first when information is insufficient", so it was
  changed to keep an exception: "when information is insufficient, first ask about the single most critical
  point".

**Reply language now follows the interface language** (previously hard-coded to Simplified Chinese)
- Users who switched the interface to English previously still received Chinese replies only — the
  interface was bilingual, the output was not. This is now decided by
  `_REPLY_LANGUAGE` from the current `i18n` language: a Chinese interface requires Simplified Chinese
  (and rules out Traditional characters),
  an English interface requires English, and a community-added language pack requires "follow the language
  of the question", **with no code change needed**.
- **Prompt bodies stay in Chinese and do not go into language packs** (following the existing convention,
  for unchanged reasons); this change makes it clear in code comments and documentation that "body
  language" and
  "reply language" are two different things.
- The review-comment prompt in `automation.py` is still hard-coded to Simplified Chinese — that is a
  **business decision** (review comments are written into
  `results\` as a long-term record), and it is noted in a comment so that it is not mistaken for an
  inconsistency and changed by mistake next time.

**Other modules that touch prompts updated in step**
- `summarizer.py`: the chunked summary and merge prompts now include output format/thinking/tone, and add
  "when different chunks describe the same matter inconsistently, keep both and label them"; a comment
  notes that this prompt **scales its context usage with the number of chunks**, so it is deliberately kept
  short.
- `automation.py`: the review-comment prompt now covers all six elements and adds "when there is no usable
  attachment content, state directly that the review cannot be performed".
- The language pack keys `mode.desc.*` (shown by `/mode list`) previously **were** the raw mode prompts;
  they are now short user-facing sentences decoupled from the prompt bodies (otherwise the whole prompt
  would be printed in the list); 5 new hint-type keys were added, complete in both Chinese and English.
- Documentation: `USER_GUIDE` (Chinese and English) rewrites the `/task` section and adds two entries,
  "answer language" and "mode prompt completeness";
  `FAQ` (Chinese and English) adds two rows, "changing the answer language" and "custom assistant role";
  `DEVELOPER` adds §1.1
  "System prompt layers and length budget" (a 6-layer structure table + the length rationale + the language
  division of labour).
- Tests: `tools/test_capability.py` adds a ninth section with **39** assertions (structure / length /
  consistency / language),
  taking the suite from 66 to **105** items, all passing.

### Added

**Open-sourcing**
- New `LICENSE` (**Apache-2.0**), `NOTICE`, `README.md` (English primary), `README.zh-Hans.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md`.
- New `.github/`: `workflows/ci.yml` (backend tests / frontend build / size check / documentation check /
  language pack check),
  `workflows/release.yml` (tagging automatically builds packages and creates a Release), 4 issue templates
  (bug / feature request / **skill submission** / **translation contribution**), a PR template, and
  Dependabot configuration.
- Documentation reorganized by language: `docs/zh-Hans/` (user guide, FAQ, skill development guide,
  automation filling guide),
  `docs/en/` (the same set in English).
- **Compliance documentation and bill of materials completed**: `docs/PRIVACY.md` itemizes the 5 network
  features (domain, what is sent,
  what is not sent, where data goes, retention period, how to disable), notes on data leaving the machine,
  and a "network fully disabled" checklist;
  `docs/EULA.md` completes the limitation of liability, third-party open-source license notices,
  attribution of responsibility for automation and skills,
  contact details, and how the agreement is changed; new `docs/SECURITY.md` (the Chinese security policy,
  carrying the same commitments as the root `SECURITY.md`) and `docs/zh-Hans/COMPLIANCE.md`,
  `docs/en/COMPLIANCE.md`
  (enterprise environment adaptation: proxies, certificates, firewall domain allowlists, group policy and
  restricted environments, audit trails);
  new `docs/SBOM.json` (SPDX 2.3-style bill of materials) and generation/verification tool
  `tools/gen_sbom.py`
  (standard library only; `--check` can serve as an inconsistency failure condition in CI).

**Multilingual (Simplified Chinese + English)**
- Backend `backend/i18n.py`, frontend `frontend/Services/Localization.cs`: language pack loading,
  a **fallback chain** (current language → en → zh-Hans → the key name itself), on-demand loading, and
  module sharding
  (`<locale>.<module>.json`).
- The "General" page of the settings window gains an **interface language** dropdown: after switching, the
  interface, error messages, and command help take effect immediately,
  **with no restart**; the choice is recorded in `ui_state.json`; on first launch it is selected
  automatically from the system locale.
- Language packs are **external**, in `lang\` under the program directory: the community can drop in a new
  `*.json` to contribute a language, with no recompilation.
- Command help and the command panel now share the backend's localized command metadata
  (`{"action":"commands"}`),
  and commands registered by skills appear automatically in the candidate list.
- New tools: `tools/check_lang.py` (key and **placeholder** consistency checking, `--scaffold` to generate
  a new language template),
  `tools/extract_strings.py` (inventories interface text not yet collected and generates templates).

**Skill runtime loader and local plugin interface**
- New `backend/skills_runtime.py`: **install it and it works** — a skill's commands are genuinely attached
  to the `/` command table,
  behaving the same as built-in commands; import/registration/execution are isolated against exceptions at
  three layers; uninstalling clears the module cache, `sys.path`,
  commands, parsers, and hooks.
- Six kinds of capability in the skill API: command registration, UI panels (declarative, rendered by the
  frontend with native controls),
  automation hooks (`before_task` / `after_download` / `after_summary` / `after_task`),
  file parser extensions, skill-private configuration, and language packs bundled with the skill.
- Local plugin interface: `backend/skills_dev/` (development, does not write to the registry) plus
  `backend/skills/` (formal installation)
  plus `skills_local.json` (arbitrary paths, aliases, enabled/disabled, load order, trust state);
  priority is `skills_dev` > manifest > `skills`, and on conflict it explicitly reports "the development
  version is currently loaded; it has overridden the installed version".
- The settings window gains an "**Installed**" page (plugin list, load state, commands/formats/hooks/panels,
  source labels
  `🏪 Marketplace`/`📁 Installed`/`🔧 Developer`, error details, open directory, export zip, pre-publish self-check, uninstall)
  and a "**Developer**" page (developer mode switch, plugin directories, hot reload,
  log view/export/clear, new plugin wizard, visual editing of the local plugin manifest, attached hooks,
  plugin panels).
- Security: environment variables whose names contain KEY/SECRET/TOKEN/PASSWORD are **temporarily removed**
  before executing a skill;
  formal loading goes through the `skills_registry.json` whitelist; loading a local plugin of unknown origin
  for the first time raises a confirmation dialog;
  the interface displays the permissions a skill declares.
- Two official example plugins added: `examples/skills/hello-world/` (command + panel + hooks + bilingual),
  `examples/skills/ofd-reader/` (`.ofd` file parser extension, zero third-party dependencies).

**Automation configuration wizard and filling help**
- **🎯 Visual selection**: opens a browser where one click on the target element generates a CSS selector
  automatically and fills it back in;
  hovering highlights and shows the selector and match count in real time; four targets are supported —
  task list element / task row container /
  next page button / detail page attachments (for detail pages it first enters the first task
  automatically); login state and automation share
  `browser_state.json`; press Esc to cancel.
- **Field-level help**: plain-language explanations and examples for all 27 fields come from the language
  pack; core fields have a "?" button beside them
  (hover for the explanation, click for details), and focusing a text box shows the explanation inline
  below it; "📖 Filling help"
  shows every field at once.
- **Configuration template library**: four presets — contract financial review / invoice information
  extraction / report key point summary / tabular data verification — filled in with one click;
  "save as custom template" is supported.
- **Four-step test wizard**: task list element → next page button (actually clicked once, reporting whether
  navigation occurs) →
  detail page attachments (counting suffix matches) → **trial run of one task** (writes no result file and
  records no processing history);
  each step gives the reason for failure and a suggested fix.
- **Configuration export/import** (`.echodesk-auto`): export excludes the login state and temporary
  directories;
  import does not inherit the safety protocol and master switch (they must be checked on this machine in
  person); a language mismatch produces a notice but does not block.
- New documentation `docs/zh-Hans/AUTOMATION_GUIDE.md` and `docs/en/AUTOMATION_GUIDE.md`
  (field overview, field-by-field reference, complete example, wizard flow, 15-row troubleshooting table,
  template descriptions, import/export, security and audit).

**Automatic updates (option A: check and notify only)**
- New `backend/update_check.py`: checks GitHub Releases and performs a semantic version comparison;
  when a new version is found, a clickable notice bar appears in the status area, and clicking it opens the
  download page in the system browser.
  It **does not download, does not replace any file, and does not install silently**; it respects the system
  proxy and certificates; it can be disabled entirely through configuration;
  by default it does not check again within 24 hours; it uses only the standard library `urllib` + `json`
  and introduces no new dependencies.

### Changed

- **AGPL dependency removed**: PDF parsing switches from **PyMuPDF** (AGPL-3.0/commercial dual license) to
  **pypdf**
  (BSD-3-Clause, pure Python). The project itself is Apache-2.0, which is incompatible with AGPL; after the
  replacement the release
  no longer contains any AGPL component, and the only remaining GPL component is the build tool PyInstaller
  (GPLv2 with the official exception).
  A side benefit: the installed size of that dependency drops from **52.9MB to about 1.4MB**.
- The search/mode status in the status bar is now driven by **machine-readable fields** in the backend
  response (it previously relied on matching Chinese text,
  which broke after switching to the English interface).
- The safety protocol text was updated: item 3 changes from "downloaded files are deleted immediately after
  processing" to
  "**downloaded attachments are kept in the results directory** for review" — consistent with the results
  directory design added in v0.2.0 (addition three).
- The "Skill marketplace → Installed" button in the settings window now jumps to the standalone "Installed"
  page.
- Version number `0.2.0` → `0.2.1` (the frontend and backend exe file versions are unified as `0.2.1.0`).

### Fixed

- `tools/test_file_parsers.py` no longer depends on PyMuPDF to create PDF samples (it now hand-generates a
  single-page PDF with an xref table using bare PDF syntax),
  so the test also passes in a clean environment with only `backend/requirements.txt` installed.
- Documentation collection in the packaging script is now recursive: localized documents under
  `docs/zh-Hans/`, `docs/en/` now make it into the release package.
- Corrected outdated descriptions about "downloaded files being deleted" in the safety protocol and in
  PRIVACY/USER_GUIDE/FAQ.

### Fixed (v0.2.1 fix task specification, seven items across P0/P1)

> The complete investigation process, decision rationale, and per-item verification evidence are in the
> internal fix record (`v0.2.1-修复记录.md`, **not distributed with the release**).

**P0 · Developer mode dialog loop and inability to turn it off**
- The per-plugin dialogs are replaced by a **one-shot summary confirmation dialog** (new window
  `frontend/Utilities/TrustDialog.cs`): it lists each plugin awaiting confirmation with its origin, path,
  declared capabilities, and permissions,
  and supports checking them individually / enabling all / disabling all; the backend gains a
  `skills_trust_bulk` action that receives all decisions at once.
- `skills_local.json` gains a `denied` list (schema 1 → 2, compatible with old files): **plugins the user
  explicitly declined are no longer asked about again**,
  and are no longer loaded either (the old implementation recorded only trusted, so clicking "no" counted
  as no answer, and it asked forever).
- **"Once turned on it cannot be turned off" fixed**: a new `dev_mode_explicit` configuration. Once the
  user has explicitly set the switch,
  developer mode follows the configuration entirely and is no longer overridden by the logic that
  "automatically enables it when `skills_dev/` contains plugins".
- Turning developer mode off now **really unloads** dev/local plugins (new `SkillLoader.unload_dev()`) and
  removes them from the command table,
  while also stopping hot reload and clearing the development log.
- **Built-in example plugins are silent**: `builtin_ids()` determines them from the `examples/skills/`
  directory shipped with the package; example plugins are enabled by default and
  raise no confirmation dialog; third-party plugins still require confirmation (the approach of letting a
  plugin declare itself "official" is not used, as that would reduce the dialog to an empty gesture).
- The Developer tab gains "current state + who decided it + the list of enabled development plugins"; after
  switching off, the list is empty, so it can be confirmed at a glance.
- Regression tests: new `tools/test_dev_mode.py` (46 items).

**P0 · Dark mode not working and insufficient contrast**
- **Dark mode never appearing fixed**: `MainWindow_Loaded` previously applied a theme only when the
  configuration was light,
  so with a dark configuration the light dictionary in `App.xaml` was left in place. Startup now applies the
  theme unconditionally through `ThemeService.Apply()`.
- Theme state is consolidated into the new `frontend/Services/ThemeService.cs`: three states
  (**follow system** / light / dark),
  with follow-system implemented via `SystemEvents.UserPreferenceChanged` plus the registry
  `AppsUseLightTheme`; the settings window's checkbox is replaced by `ThemeCombo`, which shows "which one is
  actually in effect right now".
- The semantic palette expands from 10 keys to **47 keys** (background/surface/text/accent/status/button four
  states/selection/tab/scrollbar/overlay/
  role/Markdown), with the light and dark key names entirely identical; control templates were completed
  for `ComboBox`, `ListBox(Item)`, `TabControl(TabItem)`,
  `CheckBox`, `RadioButton`, `ScrollBar`, `ToolTip`, `ContextMenu`, `ProgressBar`, `RichTextBox`,
  all using `{DynamicResource}`.
- **"Stale colours left after switching" fixed**: the old code used `TryFindResource`, which takes a
  **value snapshot**, so already-rendered messages kept the old colours;
  it now uses `SetResourceReference` (a resource reference), and the `FlowDocument` brushes in
  `MarkdownRenderer` were likewise changed to resource references,
  so the theme is followed without rebuilding the document.
- Contrast was reviewed and corrected per WCAG 2.1: input box/button borders 1.53:1 → 3.13:1 (light),
  1.52:1 → 3.6:1 (dark);
  scrollbar thumb 1.76:1 → 3.11:1; three hard-coded colours `Brushes.IndianRed`/`MediumSeaGreen`/`#e06c6c`
  were moved to the palette.
- New `tools/check_theme.py` (palette consistency + **43 forced contrast pairs** + ban on hard-coded colour
  values + verification that all resource references exist)
  and `tools/check_ui_pixels.ps1` (**real rendering** verification: launches the program, screenshots per
  theme, measures the main window's average brightness and
  per-control local contrast; measured 0.076 dark / 0.850 light, worst text 6.24:1 / 5.99:1).

**P0 · Release directory structure**
- Changed to a **three-layer portable structure**: the program proper is entirely under `app\`, with only
  shortcuts, user data, the results directory,
  language packs, examples, and documentation left outside; an assertion added at the end of the build
  script fails the build if any exe/dll/pdb appears in the outer layer.
- New `ShortcutService.UserDataDirectory` (environment variable `ECHODESK_DATA` → parent directory named
  `app` and writable → program directory)
  and backend `config.user_data_root()` (keeping the same decision order); `ui_state.json`, `snippets.json`,
  `results\` all land in the **outer layer**.
- The shortcut is renamed `EchoDesk-v0.2.1.lnk` and placed in the outer layer, with `WorkingDirectory` = the
  outer layer; the broken-shortcut check gains "the working directory is wrong"
  as a further case. The language pack search chain now includes both the outer layer and `app\`, so a user
  can add a language by dropping in a new `lang\ui\<locale>.json`,
  and upgrades do not overwrite it.
- The build script gains `-ReuseBackendFrom` (reuse an existing backend and skip PyInstaller when only the
  frontend changed), and errors out immediately when it points at this build's output
  directory. `tools/installer.iss` is aligned with the installed structure in step.

**P0 · Emoji displayed as boxes**
- Root cause confirmed by measurement: `Segoe UI` / `Consolas` / `Microsoft YaHei UI` have
  **0/20 coverage** of the emoji used in the project,
  and the font chain contained no emoji font at all. Three font resources with fallback chains were added,
  `UiFontFamily` / `MonoFontFamily` /
  `IconFontFamily` (including `Segoe UI Emoji` and `Segoe UI Symbol`), and `MarkdownRenderer` was switched
  over in step.
- New backend `backend/emoji.py` (switches to UTF-8 at startup, decides from the encoding and console code
  page whether emoji can be output,
  and degrades to `[OK]`/`[!]` when they cannot, with `--no-emoji` and `ECHODESK_NO_EMOJI` supported) and
  frontend
  `frontend/Services/Emoji.cs` (the same table for degradation; the settings window gains a three-state
  "use emoji" option);
  any uncovered emoji degrades to `?`, and **a box is never displayed**.
- New `tools/check_emoji.ps1` (scans the 61 emoji in use, checks each against the union of glyph coverage
  across the three font chains,
  and verifies that the frontend and backend ASCII degradation tables are complete).

**P1 · Bilingual translation incomplete**
- User-visible text in the interface and backend was comprehensively collected into the language pack (main
  window, the settings window's six tabs, automation, skill marketplace,
  model hub, developer tab, dialogs, system messages, error messages); a **glossary** was added to unify key
  translations;
  text containing variables was uniformly changed to placeholders.
- The frontend and backend language packs were expanded in step (key counts in §10 of the fix record), with
  `tools/check_lang.py`'s "missing key" and
  "placeholder mismatch" checks as CI gates; the four documents under `docs/<locale>/` are complete.

**P1 · Model capability assistance and context management**
- New `backend/model_profile.py`: parameter count (inferred from metadata/file name, **with the source
  labelled**), quantization format
  (`general.file_type` numeric enum → readable names such as `Q4_K_M`), training window, actually usable
  window, KV Cache estimate,
  measured generation speed; five **actionable** capability hints; hardware recommendations based on
  memory/core count (with the rationale attached).
- New `backend/prompts.py`: 6 task-oriented prompt templates (summary / contract review / structured
  extraction / translation / code troubleshooting / step-by-step reasoning),
  each containing a specific role, output format constraints, chain-of-thought guidance, and few-shot
  examples; applied with `/task`.
- New `/preset light|balanced|quality` with three generation parameter tiers (temperature/top-p/repetition
  penalty/reply length),
  with `llm_engine` supporting `top_p` and `repeat_penalty` accordingly.
- **Context management enhanced**: the status area gains a `🗜 Compact context` button (sharing its source with
  `/compact`), a new `context_mode`
  sliding window mode, and chunked summary parameters (`chunk_chars`/`overlap_chars`/`max_summary_tokens`/…)
  that can be adjusted at runtime.
- The status area gains model capability items (parameter count · quantization · usable window · measured
  speed, refreshed live) and a capability hint button;
  a vague question now triggers **a follow-up question** rather than a forced answer (can be disabled).
- New `SummaryCache`: chunked summaries are cached by "file fingerprint + summary parameters", so a second
  upload of the same file does not recompute them;
  the cache is invalidated automatically after the file changes.
- Regression tests: new `tools/test_capability.py` (58 items).

**P1 · Compliance release matters**
- `docs/PRIVACY.md` was expanded into a "local/networked" convention plus an itemized table of the 5 network
  features (target domain, what is sent, what is not sent,
  where data goes, how long it is kept, how to disable it) plus **a dedicated section on data leaving the
  machine and cross-border transfer** plus an enterprise "network fully disabled" checklist.
- `docs/EULA.md` now includes the license grant (with the Apache-2.0 precedence clause), usage restrictions,
  third-party component notices, attribution of responsibility for automation and skills,
  a standalone limitation of liability clause, governing law, contact details, and how the agreement is
  changed; outdated wording such as pyxtxt/AGPL was corrected.
- New `docs/SECURITY.md` (supported versions, reporting channels, **a response time commitment**,
  out-of-scope items, security design notes, and
  known limitations), with the root `SECURITY.md` aligned to it; new `docs/zh-Hans/COMPLIANCE.md` and
  `docs/en/COMPLIANCE.md` (proxies, certificates, minimal firewall allowlist, group policy/restricted
  environments, audit trails).
- New `tools/gen_sbom.py` (standard library only) and `docs/SBOM.json` (SPDX 2.3, 52 packages);
  `--check` can report discrepancies against `THIRD_PARTY_LICENSES.md`, and it has actually found and
  recorded missing entries in the list.

### Known limitations

- The **per-item progress and deep diagnostic messages** of automation, the skill marketplace, and the model
  hub are still in Chinese (the interface and command help are fully
  bilingual). The list of text still to be collected can be reproduced with
  `python tools/extract_strings.py`.
- Only **Simplified Chinese and English** language packs are provided; the mechanism and
  template generation tooling for `zh-Hant`/`ja`/`ko`/`de`/`fr`/`es`/`pt-BR` are ready and await community
  contributions.
- The example skills have not yet actually been published to the GitHub marketplace (repository credentials
  are needed).
- Automatic updates go only as far as "check and notify", and do not include automatic download-and-replace
  or incremental updates.
- **v0.2.1 ships no interface screenshots**: the README and documentation no longer keep "screenshots to be
  captured" placeholder guidance,
  and instead state that "later versions will place them in `assets/screenshots/`".
- `docs/THIRD_PARTY_LICENSES.md` and `docs/SBOM.json` have now been **regenerated in a clean venv with only
  `backend/requirements.txt` installed and checked item by item as consistent**
  (55 distributions, 0 discrepancies; see "Issues found and fixed during the compliance wrap-up" above).
  The remaining caveat is that the **generation baseline depends on the environment**: the list is defined
  as "the actual installation result of a clean venv",
  so the next release must still re-run `python tools/gen_licenses.py` and
  `python tools/gen_sbom.py --check` in a clean environment (the CI `release-gates` job already runs the
  latter automatically).
- Left over in the fixed build (see §11 of the internal fix record `v0.2.1-修复记录.md`, **not distributed
  with the release**):
  - **Not tested on Windows 10** (the emoji and theme conclusions are based on glyph coverage and palette
    values, not on the system version).
  - ~~**The installed build (Inno Setup) was not actually compiled and verified**: there is no Inno Setup 6
    on this machine, `tools/installer.iss`
    has been aligned with the portable structure, but no `setup.exe` was ever produced.~~ → **Resolved in
    this version**: the installation method was changed throughout to
    compiling both installers with Inno Setup 6 (`tools/installer.iss` + `tools/make_installer.ps1`),
    and `tools/check_installer_deliverables.ps1` was added for "real install and uninstall" acceptance;
    see "Installation method: changed from a 7-Zip self-extracting package to Inno Setup" above.
  - **The automation safety protocol has no separate audit log** (the protocol text and checkbox state live
    in the configuration; there is no tamper-proof trail).
  - The two configuration items `model_hub.enabled` and `automation.start_with_windows` **are not read by the
    code**,
    so do not treat the former as a network kill switch; for start-on-boot, put the shortcut in
    `shell:startup`.
  - Vector retrieval (local RAG) is **not done in this version**: vector database dependencies conflict with
    the global size requirement, and it is suggested that the next version first
    validate the benefit with a zero-dependency keyword retrieval approach.
  - `docs/DEVELOPER.md` is still Chinese only (`PRIVACY.md`/`EULA.md` have been bilingualized).
  - **The results directory rename does not migrate automatically** (`批量处理结果\` → `results\`):
    upgrading users must rename it by hand,
    see the upgrade note under "Naming conventions: program-generated names are English too" above.
  - **The installers are not code-signed**: `-Sign` requires a certificate, and neither `Setup.exe` in this
    version is signed, so the first run shows a SmartScreen prompt
    (`tools\make_installer.ps1 -Sign -CertThumbprint <thumbprint>`
    is ready).
  - **The installer interface is currently English only**: the `Languages\` directory shipped with Inno Setup
    6.7 has no
    `ChineseSimplified.isl` (the official project lists it as a third-party translation that must be
    downloaded separately). `tools\installer.iss`
    already uses ISPP's `FileExists` for conditional inclusion — putting that `.isl` into Inno Setup's
    `Languages\`
    directory enables the Chinese interface automatically, with no code change.
  - **Recompiling the installers requires Inno Setup 6**: `tools\make_installer.ps1` detects it
    automatically
    (registry → common install locations → `%LOCALAPPDATA%\Programs` → PATH), and `-ISCC` can be used to
    point at it.
    The release workflow installs it with Chocolatey in CI.

---

## v0.2.0

### Additions

**Context and long documents**
- The default context window goes from 4096 to **8192**, and a `max_context` ceiling check is added (so a
  configuration cannot exceed the limit); the effective window settles automatically on the smallest of the
  three values "configured value, model training window, ceiling".
- New `gguf_meta.py`: reads GGUF metadata without loading the model, obtaining the model training window, the
  layer count and the number of KV attention heads, and estimating the **KV Cache memory footprint** (fp16).
- Loading/reloading a model now logs the window size and the KV Cache estimate.
- New `summarizer.py`: **map-reduce chunked summarization** for long documents — it splits by chunk length while
  keeping overlap, puts the split points on paragraph/period boundaries, summarizes each chunk and then merges
  recursively under a budget, and keeps an **original-text pointer** in the output (source file + character range
  of each chunk). Documents that are too long are no longer truncated outright.
- New `/summarize` command (triggers a full-text summary by hand); summary progress is shown live in the chat
  area.
- A notice appears when the generation speed falls below a threshold, explaining that a smaller window / more
  threads can improve it.
- `file_max_chars` goes from 10000 to 200000 (0 = unlimited), which together with chunked summarization lets long
  documents through.

**Model download and startup experience**
- **The user is asked before a model is downloaded**: the backend emits `need_download` (with the file name and
  size) and the frontend raises a confirmation dialog; the download only happens on agreement, and on refusal
  the backend exits and gives follow-up instructions.
- **The launcher shortcut is checked at startup**: if it does not exist the program asks whether to create one
  (created through WScript.Shell COM, pointing at the main program, with the program directory as the working
  directory and the EchoDesk icon); once the user has declined it never asks again, and when moving the folder
  has broken the shortcut it offers to create it again.

**Interface and accessibility**
- All message content can be **selected and copied**: plain-text messages become read-only text boxes, and
  assistant messages keep Markdown rendering.
- Every message gains a copy action strip in its bottom-right corner: Copy / Markdown / Code (when there are code
  blocks) / Select all.
- New shortcuts: `Esc` stop, `Ctrl+L` clear, `Ctrl+O` upload, `Ctrl+Shift+C` copy everything,
  `Ctrl+plus/minus/0` font size, `Ctrl+T` theme, `Ctrl+,` Settings, `Ctrl+M` skill marketplace.
- **Window state is remembered** (size/position/maximized), and the theme and font size are persisted to
  `ui_state.json`; with multiple monitors and high DPI it never restores off-screen.
- The status bar gains model, web search, automation and marketplace connection states, plus a bottom
  notification strip (download finished/failed and so on).
- First-run onboarding (welcome page + model download/shortcut/long documents/copying and shortcuts).
- Scrolling inside message controls is disabled, so the wheel always scrolls the conversation list.

**Release engineering**
- The release output becomes a **single folder, multiple files and a single entry point**: the frontend ships as
  multiple files (dependency DLLs sit in the same directory, which starts faster) and the backend uses
  PyInstaller **onedir** (dependencies collected in `backend\_internal`); both are named `EchoDesk.exe` /
  `EchoDeskBackend.exe`, and only one executable is left in the root directory.
- New `tools\build_release.ps1` one-click release script and `tools\installer.iss` (Inno Setup installer
  template); the build script automatically creates the single entry shortcut "EchoDesk 启动.lnk".
- New rcedit metadata writing (`tools\set_metadata.bat`) and six-size icon generation
  (`tools\make_icon.py`: deep indigo → deep purple gradient + warm terracotta echo pattern, 16/32/48/64/128/256).
- **Backend size 976MB → 178MB**: the pyxtxt dependency is removed (see "Changes").

### Changes

- **File parsing no longer depends on pyxtxt**: each format is parsed directly instead (python-docx / openpyxl /
  python-pptx / PyMuPDF / xlrd, plus multi-encoding text detection). The reason is documented in
  `docs/THIRD_PARTY_LICENSES.md`: pyxtxt's parser subpackage has to be packaged as a whole directory, which drags
  in easyocr→torch and roughly 800MB of further dependencies, and its dependency chain contains GPL/LGPL
  components.
- Backend metadata is now written at packaging time by PyInstaller's `version`/`icon` (rcedit would break the PKG
  archive PyInstaller appends); rcedit is still used for the frontend exe.
- Frontend–backend communication gains a **request gate**, so concurrent requests from the main window and the
  Settings window no longer read each other's responses.

### New feature modules

- **Automation (browser)**: `automation.py` + the "Automation" tab of the Settings window. Playwright drives the
  Microsoft Edge installed on this machine and runs **headed**; it scrapes the task list by selector, opens each
  detail page in turn, filters and downloads files by extension, hands them to the local model to produce a
  summary and a review opinion, shows and writes out the results, and deletes the temporary files as soon as it
  is done with them. It supports pausing on a CAPTCHA/MFA to wait for manual handling, task deduplication,
  paging, failure screenshots and skipping, and an audit log (redacted). **The switch is forced off while the
  safety protocol is unticked**; the protocol version and the time of consent are written to the audit log.
- **Skill marketplace**: `marketplace.py` + the "Skill marketplace" tab of the Settings window. It searches by
  the GitHub topic `echodesk-plugin` (sorted by stars / recently updated / best match), keeps a local cache with
  TTL and offline fallback, validates `skill.json` (unverified entries are greyed out), shows details, and
  installs (tarball verification + registry registration + read-back verification after install) and uninstalls.
  **Third-party prepare scripts are never run automatically**, only reported.

### Documentation and compliance

- New `docs/PRIVACY.md` (privacy policy: an item-by-item account of which features run locally and which go
  online), `docs/EULA.md` (end user license agreement), `docs/THIRD_PARTY_LICENSES.md` (auto-generated license
  inventory + compliance conclusion), `docs/FAQ.md` (quick start + troubleshooting), `docs/DEVELOPER.md`
  (architecture and protocol), `docs/SKILL_GUIDE.md` (skill development guide).
- New `tools\gen_licenses.py` (scans the installed distribution packages to generate the license inventory).

### Fixes

- **The backend exited silently on Windows with an English locale**: Python encodes stdout as cp1252 by default,
  so a protocol containing Chinese/emoji raised `UnicodeEncodeError` and crashed outright. stdout and stderr are
  now forced to UTF-8 with `errors="replace"` as a fallback, and the frontend reads explicitly as UTF-8. This is
  the real reason v0.1.0 showed "the backend process just exits by itself after a while" on two machines that
  were not development machines.
- New crash log `backend\backend_crash.log` (`faulthandler` + `sys.excepthook`), so exceptions no longer exit
  silently.
- Download/warm-up progress and backend abnormal-exit notices are now shown in the chat area as well (they no
  longer appear only in the status bar).

### Fixes and hardening (v0.2.0 fix task list)

> Corresponds to the five tasks in `指示.md` in the repository root; the **version number is not raised** and
> stays at v0.2.0 / `0.2.0.0`. The full implementation plan, key decisions, deviations and unfinished items are in
> the internal fix record (`v0.2.0-修复记录.md`, **not distributed with the release**).

**Fix 1: the Settings window hung (P0, fatal)**
- The real root cause was not a deadlock but the slider `<Slider Minimum="10" .../>` in `SettingsWindow.xaml`,
  which forced `Value` from 0 up to 10 during **XAML parsing** and fired `ValueChanged` while `FontSizeLabel` had
  not been created yet → `FontSizeSlider_ValueChanged` threw `NullReferenceException`; there was no global
  exception handler at the time, so the unhandled exception on the UI thread terminated the process outright (a
  29MB WER dump). What you saw was "the interface is unresponsive, the only way out is Task Manager", and it left
  no clue behind.
- Fix: a new `_uiReady` latch (every handler attached from XAML returns immediately until `InitializeComponent`
  has finished), null checks in the slider callback, and default values added in the XAML.
- New **global exception safety net**: `DispatcherUnhandledException` / `AppDomain` / `TaskScheduler` all write
  to `frontend_crash.log` in the program directory and then show a notice, leaving the program usable.
- **The communication layer is rewritten**: stdout is now delivered to a queue by a single pump thread, and the
  requesting side takes lines from that queue — so a settings timeout **cannot leave a dangling read** behind
  (the old "give up reading" would swallow the next response). The request gate waits with a default timeout of
  15 seconds, and the response idle timeout defaults to 10 minutes (45 minutes for heavy tasks).
- The Settings window now appears the moment it is opened: constructing and showing it waits on the backend for
  nothing, the data loads asynchronously after `ContentRendered`, and a loading state stays at the bottom; when
  the backend is busy it retries automatically and shows a waiting notice; closing the window cancels in-flight
  requests.
- The three `MessageBox` calls at startup now get an owner, so a dialog no longer ends up behind the main window
  and looks like "no notice visible, the interface seems stuck".
- Measured: the Settings window opens in **207ms** (requirement <500ms), Responding is True throughout,
  alternating operations in the main and Settings windows do not block each other, and there are no crash logs.

**Fix 2: Enter in the command bar ran the wrong command (P0, data risk)**
- The command list only did prefix filtering and kept the declaration order, so typing `/clear` put
  `/clearcache` first and pressing Enter straight away cleared the KV cache.
- New ordering rules: **exact match first → command names in ascending length (a longer matching prefix is more
  specific) → the original order of the command table**.
- When there is **more than one candidate and the first entry is not an exact match**, Enter **only completes the
  command name** and shows a notice, and the user runs it after confirming.
- The command list shows the **full name and the argument format** (`usage: /model <name|alias|list>`), and the
  list items now carry accessibility names.
- New **second confirmation for dangerous commands**: `/clear` and `/clearcache` explain the consequences before
  running, and cancelling keeps the input; this can be switched off in "Settings → General" (on by default).
- Measured: `/clear`→first entry `/clear`, `/c`→`/clear`, `/cl`→`/clear`, `/sum`→`/summarize`; pressing Enter on
  `/cl` only completes it to `/clear `; and the `/clear` confirmation box points unambiguously at `/clear`.

**Addition 3: the results directory (P1)**
- Results are saved in layers as `program root\results\YYYY-MM-DD\task name\` (the top-level name and the save
  location are both configurable, and an empty top level means the program root directory); several runs on the
  same day merge into one date directory, and same-named tasks get `-1`, `-2` appended automatically.
- A task directory holds **the original files downloaded in that run** (duplicate names get a sequence number
  appended automatically) and `result.txt`; the failure reason and the failure screenshot file name of a failed
  task go into `result.txt` as well, and the screenshot stays inside the task directory.
- `result.txt` is **plain text with no Markdown markup of any kind** (Notepad cannot render it): new
  `backend\plaintext.py` converts Markdown to plain text — headings become underlines, tables become plain text
  **aligned by display width** (CJK counts as 2 columns), lists keep their `-`/numbers, links become
  `text (address)`, and code blocks are indented.
- `result.txt` is rewritten in full as each task finishes or fails, so a crash or a stop midway still loses none
  of the results already processed.
- New self-tests: `tools\test_plaintext.py` (20 items), `tools\test_results_dir.py` (26 items) and
  `tools\test_automation_e2e.py` (21 items over the full chain with the real Playwright/Edge, which in practice
  produced the three-level directory, the plain-text result, the retained original files and the duplicate-name
  `-1`).

**Addition 4: the model selection bar (P1)**
- The Settings window gains a "Model" tab: it connects to **hf-mirror.com (mirror)** and **huggingface.co
  (official)**, supports automatic probing with fallback when one is unreachable, and always states clearly which
  source is in use.
- It supports search (name/author/tag, 400ms debounce), sorting by downloads/update time/likes/name, and
  filtering by minimum downloads and quantization format; results show the author, download count, update time
  and an "already downloaded / sign-in required" badge.
- The detail pane lists **every GGUF quantization of that repository with its exact size**, the quantization
  format, sha256, the license and whether it has been downloaded.
- Before downloading it confirms the file size and the expected disk usage; during a download it shows the
  percentage, speed and time remaining and allows cancelling; it supports **resuming** (`.part` + Range),
  retrying after a failure and **size/hash integrity verification**, and for a gated model returning 401/403 it
  prompts you to configure a Token.
- A finished download is registered automatically in `backend\models_registry.json` (model name/path/repository/
  quantization/size/download time/source) and reconciled against the `models\` directory (a `.gguf` you dropped
  in by hand is registered too).
- The model library list shows the quantization, source, disk usage, download time and **last used time**; the
  model currently in use is marked "in use" and cannot be deleted, and deletion asks for confirmation first.
- It cooperates with `/model`: the download directory is the same `models\` that `/model` uses, so `/model list`
  recognises it directly, and the model library marks the currently loaded model in return.
- New `tools\test_model_hub_e2e.py` (drives the packaged backend over the real instrument protocol; all 19 items
  pass).

**Improvement 5: layering the status bar information (P2)**
- The top status bar **keeps only** the run state, the current model and the current mode (the model and the mode
  become clickable capsules that jump to Settings → Model / Settings → General respectively); the web search,
  automation and marketplace sections move out of the top bar.
- **Under the input box** there is a new lightweight status strip: 🔍 Search · ⚙️ Automation · 🌐 Marketplace ·
  📊 Context percentage · 💾 KV Cache estimate, all of them "icon + short phrase".
- Clicking any item jumps to the matching setting (the search item toggles the switch directly, which is why
  `/search` gains `toggle`); the `▾/▸` on the right collapses it, and the state is written to `ui_state.json` and
  reused on the next launch.
- Accessibility: every item carries HelpText; `AutomationProperties.Name` is **no longer** used to override the
  state text (otherwise a screen reader could only read "run state" and would lose "ready").
- The backend `context` response gains `n_ctx` / `kv_cache_text` / `model` / `search` fields, so the frontend no
  longer has to parse Chinese status text.

### Known limitations

- `.doc` / `.ppt` (legacy binary formats) need WPS/Office installed on this machine; otherwise save them as
  `.docx` / `.pptx` first.
- Scanned PDFs have no text layer, and v0.2.0 has no built-in OCR.
- Automation drives the **Microsoft Edge installed on this machine** (`channel=msedge`); the program does not
  bundle Chromium, and if Edge is unavailable it shows a notice containing `playwright install chromium`
  guidance.
- Automation downloads at most 200MB per file; anything above that is skipped and recorded.
- Automation's piercing of the Shadow DOM is a simplified implementation; in practice Playwright's CSS engine
  already pierces open shadow roots by itself, and complex pages may still need selector adjustments.
- Automation's scheduled triggering relies on the backend `schedule` thread (the program has to stay running);
  starting automatically at boot means putting the shortcut into the system "Startup" folder yourself.
- **Skill marketplace installation does not pip-install dependencies automatically**; it only tells the user to
  handle them by hand (security first).
- The topic `echodesk-plugin` currently has no repositories under it, so the marketplace shows an empty result
  (with an explanation).
- The skill runtime loader (which attaches skill commands to the `/` command list) has its interface defined, but
  the runtime wiring is left for a later version.
- Auto-update and code signing: the build script already reserves a `-Sign` hook, but the repository contains no
  certificate; a self-signed certificate may still trigger a SmartScreen prompt.
- `tools\build_release.ps1` has to run on PowerShell 5.1+; its Chinese messages depend on the script being
  UTF-8 with BOM.
- The deviations and unfinished items of this fix task list are all recorded in section 7 of the internal fix
  record (`v0.2.0-修复记录.md`, **not distributed with the release**), in summary: `/exit` was not included in the
  dangerous-command confirmation (it already has an interruptible countdown); the "task name" of the results
  directory is the run batch name, with one aggregated `result.txt` per run; the model list does not show file
  sizes (the detail pane gives the exact value instead); the model selection bar allows only one download at a
  time; and neither further subdirectories under the results directory nor Windows DPAPI-encrypted tokens were
  implemented.

---

## v0.1.0

- First release: Python backend (local inference with llama.cpp) + C# WPF frontend (JSON line protocol).
- Conversation modes, temperature presets, web search, file upload (Office/TXT/PDF), session persistence, canned
  phrases, Markdown rendering, light and dark themes, font size adjustment.
- Command set: `/help` `/mode` `/file` `/search` `/model` `/clearcache` `/prompt` `/temp`
  `/darkmode` `/stop` `/snippet` `/status` `/clear` `/undo` `/save` `/about` `/exit`.
