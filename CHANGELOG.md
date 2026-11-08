# EchoDesk Changelog

> A Simplified Chinese reference translation is available at [`CHANGELOG.zh-Hans.md`](CHANGELOG.zh-Hans.md).
> If the two documents ever disagree, this English version prevails.

This document records the release positioning, key capabilities, known limitations, and shipped artifacts
of each published version. Version numbers follow `MAJOR.MINOR.PATCH`; file version resources are uniformly
`X.Y.Z.0`.

Detailed engineering records — implementation plans, decisions, deviations, and test evidence — are kept in
the repository's `records/` directory and are not distributed with releases. Those records are referenced
for traceability only; this changelog is the release-facing document.

| Version | Type | Release date | Summary |
| :--- | :--- | :--- | :--- |
| [Unreleased](#unreleased) | Development | — | v0.2.5 P0: writable state paths for read-only `.app` / AppImage layouts; Unsloth offline model catalog and `models-qwen-v1` release strategy |
| [v0.2.4](#v024) | Stable Release | 2026-11-08 | Automatic hardware tuning, three inference presets, transparent override and diagnostics; release-time patch and final hotfix for streaming download progress, ignored-version memory, memory warnings, balanced title bar, Chinese installer and upgrade hygiene |
| [v0.2.3](#v023) | Stable Release | 2026-11-07 | Reproducible builds, dependency locks, Avalonia main line, WPF LTS |
| [v0.2.2](#v022) | Stable Release | 2026-11-04 | Cross-platform frontend, visual consistency, full bilingual coverage |
| [v0.2.1](#v021) | Stable Release | 2026-10-05 | Capability consolidation, Agent platform, MCP, plugin ecosystem |
| [v0.2.0](#v020) | Feature Release | 2026-09-23 | Long context and documents, automation, skill marketplace |
| [v0.1.0](#v010) | Early Preview | 2026-08-29 | First public release: local inference with a desktop client |

---

## Unreleased

### Development work (v0.2.5 P0 — writable state paths on read-only mounts)

Preparatory work required before the macOS `.app` bundle and the Linux AppImage can run
from read-only mount points. Both forms previously resolved `config.json` and `models/`
against the current working directory; writing there either failed or invalidated the
signed bundle.

- **Writable state directory.** `backend/config.py` adds `state_dir()`: it honours
  `ECHODESK_STATE`, then a writable current working directory, then falls back to
  `user_data_root()`. A writable CWD is exactly the portable `tar.gz` layout, so existing
  installations keep resolving every path as before; read-only mounts now silently use
  the user data root.
- **macOS `.app` data root.** `user_data_root()` recognises bundles
  (`.../Contents/MacOS/...`) and returns `~/Library/Application Support/EchoDesk`,
  matching the Avalonia `UserPaths.DataRoot` resolution.
- **Configuration persistence.** `Config.load()` and `Config.save()` default to
  `<state_dir>/config.json`. If the located file is a read-only packaged resource,
  saving is redirected to the writable state directory instead of failing.
- **Model repository.** `ModelHub.models_dir` and `registry_path` resolve relative
  configured values against `state_dir()`; absolute paths are untouched.
- **UI language packs.** Avalonia `UserPaths.LangDirectory` falls back to
  `<program directory>/lang/ui` when `<data directory>/lang/ui` does not exist, so
  packaged `.app` / AppImage builds no longer silently degrade the English UI to Chinese
  fallback strings.
- **Verification.** `selftest` 170/170, `check_lang` passes, and
  `tools/verify_reproducible_build.ps1` produced two byte-identical builds. The portable
  path behaviour is unchanged: with a writable CWD `state_dir()` returns the CWD, which
  is the condition the `tar.gz` layout always satisfies.

### Development work (v0.2.5 — Unsloth offline model catalog and external release strategy)

- **Model catalog.** `tools/pack_model_addon.py` now carries a 14-family Unsloth GGUF
  catalog: Qwen3, Qwen2.5, Qwen2.5-Coder, Llama-3.2, SmolLM2, gemma-2, gemma-3,
  Qwen3-VL and Qwen3.5. `Q4_K_M` is required; `Q6_K` and `Q8_0` are fetched when the
  upstream repository publishes them.
- **Size guard.** A single file larger than 2 GiB is skipped with a `WARNING` in every
  mode — local audit, upstream planning, verification and packing — and never aborts the
  script.
- **Projection layers.** Qwen3-VL entries bind `mmproj-Q8_0.gguf` (with `ggml-org`
  fallback and F16/BF16 candidates); Qwen3.5 entries bind their separate
  `mmproj-F16.gguf`. Upstream metadata confirms Qwen3.5 still publishes a standalone
  projection layer, so it is not assumed to be embedded.
- **Graceful missing models.** Missing local files print the manual download pointer for
  GitHub Releases tag `models-qwen-v1` instead of raising; only a proven size/sha256
  mismatch is a hard failure.
- **Documentation.** Both READMEs add an "offline model bundle" section stating that all
  weights live under the dedicated `models-qwen-v1` release tag and only need to be
  extracted into `models`.

---

## v0.2.4

### Release positioning

A quality and transparency release built on the hardware-adaptive runtime introduced in this
cycle. EchoDesk now profiles the host before a model loads and turns those measurements into
concrete recommendations; the user can accept them or override them. The release also adds
three inference presets, structured model metadata awareness, and a diagnostics surface that
makes every automatic decision visible. Reproducible builds remain byte-identical after the new
probes: hardware detection never writes a host identifier or absolute path into a packaged file.

### Key capabilities

**Automatic hardware tuning**

- Before a model loads, the backend probes the CPU (physical and logical cores, AVX2/AVX-512),
  NVIDIA CUDA through `nvidia-smi`, AMD ROCm through the amdgpu sysfs interface, Apple Metal,
  total/available RAM, and free VRAM.
- GPU offload is derived behind a 20% free-VRAM safety watermark after reserving the KV cache
  and a 256 MiB compute buffer. It requests full offload (`-1`) only when the model fits,
  otherwise computes a safe positive layer count; no discrete GPU forces `n_gpu_layers=0`.
- The context window starts at a small allocation (2048 tokens by default) and grows on demand
  up to the model's native window, the configured ceiling or the memory budget, whichever is
  smallest. The thread count follows the detected physical cores.
- Every probe is isolated inside an exception boundary; a failure degrades silently to a
  conservative CPU-only profile and records the reason in the plan.

**Three inference presets**

- `fast`, `balanced` and `quality` control construction-time parameters: initial context,
  the usable share of the native/hardware ceiling, prompt batch size and thread preference.
- `fast` halves the ceiling, starts at 1024 tokens, uses a 1024 batch and every logical thread.
  `balanced` keeps the full ceiling, starts at 2048 and uses physical cores. `quality` keeps
  the full ceiling, starts at 4096 and uses an F16 KV cache.
- Changing a preset re-plans and rebuilds the model so `n_ctx` and `n_batch` really take effect;
  the settings page shows the native window, usable window and the constraint that limited it.

**Transparent override and diagnostics**

- Settings → Model gains a folded hardware diagnostics panel: detected CPU, GPU model and VRAM,
  system memory, the model's native window, the hardware budget and the recommended profile.
- `n_ctx` and `n_gpu_layers` are editable and default to the recommendation. Over-budget values
  show a yellow warning icon with a concrete explanation but do not block saving: the backend
  adopts them verbatim and returns a localized `warnings` list.
- A “Test load” button tries the current values in an isolated child process with a 60-second
  limit. The parent terminates the child on timeout, so model memory is released immediately and
  the live engine is never duplicated.
- If an override cannot load (a 999999-token window, or GPU layers on a machine without an
  accelerator), the backend returns the raw exception plus actionable suggestions and keeps the
  previous engine running. The UI shows the failure in a dialog; it is never swallowed. A
  persisted bad override is caught at startup and enters the no-model state instead of crashing
  the backend, so the user can open Settings and restore the recommendation.

**Model metadata and reproducibility**

- The planner reads the GGUF header for the native context, layer count, attention heads, KV
  heads, embedding width and key/value dimensions; the native window is a hard ceiling and the
  memory budget can only lower it.
- Hardware probing does not affect output determinism: the full release chain was built twice
  from the same commit and every deliverable hash matched. A dedicated gate (`selftest`,
  `test_hardware_profiler`, `test_inference_override`, Avalonia `--probe pages`) covers the new
  override, warning, diagnostics and test-load paths.

### Release-time patch (2026-11-08)

- **Stable input feedback.** The Avalonia `TextBox` theme now treats hover and focus as
  render-only states. `BorderThickness`, `Margin` and `Padding` are constants; `Background`,
  `BorderBrush`, `Opacity` and `BoxShadow` are animated with `BrushTransition` and
  `BoxShadowsTransition`. The unattended probe toggles `:pointerover` and asserts that the
  bounds, margin, padding and border thickness are unchanged, so a future edit cannot silently
  reintroduce layout-driven flicker.
- **Status-strip parity and memory pressure.** The Avalonia status strip now also reports host
  physical-memory pressure from the backend hardware snapshot, alongside search, automation,
  market, context percentage, KV cache, model capability and compaction. Every label is
  bilingual, and `--probe chat` asserts the item count, that each item is non-empty and that
  the memory value is percentage-formatted.
- **User-consented updates.** At startup EchoDesk silently checks GitHub Releases. If a newer
  release exists, it asks before downloading; choosing “no” sends no download request. A
  successful download is written to `<user data>/updates/` and verified against the asset size
  and SHA-256 digest before it is renamed into place. The user is then asked to restart; the
  application exits only after the installer has started. Release asset URLs are re-resolved
  from the GitHub API and restricted to this repository.
- **Installation never destroys user data.** The installer excludes `config.json`, sessions,
  registries, models, user skills and UI preferences from its overwrite set; `config.json` and
  the example skills are copied only when absent. Uninstall keeps user data and model files in
  place and additionally backs up the small configuration/session files to
  `%LOCALAPPDATA%\EchoDesk\uninstall-backup\<timestamp>`. The installer gate was extended to
  prove both behaviours with real install/uninstall cycles.
- **Release-tree hygiene.** The build and full release gate now fail if the release tree
  contains top-level `tools/`, `tests/`, `records/` or `prompts/`, and the installer excludes
  those paths defensively. User-facing `docs/` (EULA, privacy, guides, SBOM) is deliberately
  retained: removing it would strip legally required documents and break the release contract.
- Verification: `selftest` 163/163, the new `test_update_check` 31/31, Avalonia `--probe chat`
  with the new assertions, two byte-identical reproducible builds, and the full
  `verify_release.ps1` gate with no skips.

### Final hotfix (2026-11-08)

- **Streaming update download progress.** `update_download` is now a streaming action. The
  backend emits `update_progress` lines containing downloaded bytes, total bytes and percent,
  throttled to roughly one report per 150 ms; the Avalonia status strip renders a native
  `ProgressBar` plus a live percentage/byte label. The final `done` line still carries the
  verified path, and failure hides and resets the bar.
- **Ignore this version.** The download prompt now offers an “Ignore this version” checkbox.
  Choosing “No” with the box checked persists `update.ignored_version` through the backend; the
  next startup check returns `status=ignored` for that version and does not prompt again. A
  newer release still prompts normally.
- **Critical memory warning.** The status-strip memory item turns red (localised `ErrorBrush`
  class) and changes its icon to `⚠️` when usage exceeds 90%, with the tooltip
  “Memory is critically low and may slow inference”. The class is removed again when pressure
  drops.
- **Balanced title bar.** The Avalonia custom title bar now shows the product mark, the version
  reported by the backend handshake (single source: `backend/version.py`), and the localized
  studio line in a centred identity block. No version is hard-coded in the frontend.
- **Chinese installer and upgrade hygiene.** `tools/InnoSetup/ChineseSimplified.isl` (official
  Inno Setup translation, UTF-8 with BOM) is bundled so every build produces a Chinese-capable
  installer without relying on the compiler machine. `[InstallDelete]` removes stale
  `bin/` and `.cache` paths during an upgrade while explicitly excluding models, config,
  sessions, registries, user skills and results.
- Verification: `selftest` 163/163, `test_update_check` 37/37, Avalonia `--probe chat` with the
  new title/memory/progress assertions, two byte-identical reproducible builds, and the full
  `verify_release.ps1` gate with no skips.

### Post-release documentation correction (2026-11-08)

- **Code-signing status corrected.** Earlier revisions of this changelog, both READMEs and
  `CODE_SIGNING.md` described the Windows deliverables as signed through SignPath. That was the
  intended target state, not the actual state: the free SignPath Foundation application was
  **not approved** this time, because the project is not yet widely known enough. Windows builds
  are unsigned until a certificate is issued. `CODE_SIGNING.md` now leads with the unsigned
  status and keeps the intended scope and workflow in a clearly labelled target-state section;
  both READMEs state the reason next to the SmartScreen note.
- **No binary changed.** This is a documentation-only correction: the v0.2.4 artifacts, their
  SHA-256 digests and the reproducible-build result are unaffected.

### Known limitations

- Windows artifacts are not Authenticode-signed: the free SignPath Foundation signing
  application was not approved this time, so SmartScreen may warn on first run.
- GPU offload and the three presets were validated with synthetic VRAM profiles and a real
  CPU-only host; a long-context stress test on real CUDA / ROCm / Metal hardware has not been
  executed. AMD probing covers the Linux sysfs interface only.
- Quantised KV caches (Q8_0 and similar) need flash-attention support that has not been verified
  on the supported hardware set, so all three presets use F16.
- Changing a preset or applying an override rebuilds the model; hot-swapping without reloading
  the weights is not available in llama.cpp.
- The “Test load” child process uses the same executable as the backend. It is bounded by a
  60-second timeout but adds one full model-load cycle when used.

### Artifacts

`EchoDesk-v0.2.4-Setup.exe`, the portable Windows zip and the Linux/macOS archives are listed
with their SHA-256 digests in `SHA256SUMS.txt` next to the release assets.

---

## v0.2.3

### Release positioning

The first stable release of the v0.2.3 cycle and the first EchoDesk release whose deliverables can be
independently rebuilt byte-for-byte from the tagged commit. The cycle refocuses the project on Avalonia as
the primary interface; the WPF edition completes its functional development here and enters long-term
support (LTS), which means security and crash fixes only and no new features. This release combines the LTS
notices, the interface work delivered during the preview iterations, and the reproducible-build and
supply-chain hardening that closes Issue #6.

### Key capabilities

**WPF moves into long-term support (LTS)**

- The WPF main window carries a permanent notice bar stating that the edition has entered LTS, with
  a button that opens the latest GitHub Releases page. The bar has no close action, because the
  notice is meant to remain visible for as long as the edition is in LTS.
- The Settings window gains a dedicated About page: the LTS statement is presented first and in a
  prominent frame, followed by the version, the interface edition, the project home and the licence.
  The page also links to the latest GitHub Releases page.
- Every new string ships in both Chinese and English through the language packs, and a language
  switch updates both notices immediately without restarting the application.
- Scope of LTS: compatibility and stability are maintained through security and crash fixes; feature
  development continues on the Avalonia edition only.

**Version and lifecycle management**

- `APP_VERSION` is now `v0.2.3`. The four-field form required by Windows version resources,
  `0.2.3.0`, is derived by `app_version_4()`.
- The packaging scripts and the release gate apply the same derivation rule, so a mismatch between
  the display version and the version resource is rejected before packaging.
- The WPF and Avalonia project files now carry the same version numbers.
- The installation package writes the numeric core to its Win32 version fields while keeping the
  full version in the display fields.

**Legacy cleanup**

- Removed twenty-nine unused using directives, three uncalled methods, five write-only fields and
  five unreferenced XAML resources (two named styles and three theme brushes) from the WPF project.
- Removed eight obsolete language keys that no code referenced, in both Chinese and English.
- Corrected a malformed XML documentation comment in the settings window.
- Reviewed `.gitignore`: build outputs, release trees, runtime state files, model caches, screenshot
  directories and development materials are all covered, and the checks in this iteration left no
  untracked files behind.

**Reproducible builds and supply-chain integrity**

- Every packaging script now derives a single canonical `SOURCE_DATE_EPOCH` from the commit time of
  `HEAD`. Packaging aborts when the Git worktree is not clean, so a release can only be produced from
  an exact commit.
- All timestamp formatting uses UTC; no local-time value can affect a shipped byte.
- Both .NET projects enable `Deterministic`, `ContinuousIntegrationBuild`, `DeterministicSourcePaths`
  and an explicit `PathMap`, removing build-machine absolute paths from assembly metadata.
- `backend/requirements.lock` pins every runtime distribution for Windows, Linux and macOS arm64.
  The platform-specific `llama-cpp-python`, `numpy`, `greenlet` and `pillow` versions are expressed
  with environment markers and are evaluated against the target RID, never the build machine.
  `backend/requirements-build.lock` pins the PyInstaller build environment, and
  `tools/check_venv_lock.py` verifies it before a release build.
- Zip and tar.gz writers fix entry timestamps, ownership (uid/gid 0), permission bits, entry order and
  the gzip header. Inno Setup uses `notimestamp` and `TimeStampRounding=1`, and the PE timestamp of
  the final Setup executable is normalised in place without touching the appended installation data.
  PyInstaller's `base_library.zip` metadata is normalised after collection, because PyInstaller
  stamps that archive with the wall-clock time and may emit otherwise identical entries in a
  different order.
- `tools/verify_reproducible_build.ps1` performs two full builds in sequence, clearing compiler caches
  and outputs between them, and compares SHA-256 hashes for the installer, all three platform
  archives, their manifests and every file in the release trees. The two runs are byte-identical.
- The full release gate passes 50/50 without skipping any item.

**Branding and release-size optimisation**

- The organization identifier is unified as `Void-Entropia-Studio` across source, build metadata,
  language packs, legal documents and installers. Organization links point to
  <https://github.com/Void-Entropia-Studio>.
- Release trees are pruned of test suites, demo packages, `__pycache__` directories, debug and
  import libraries, and C extensions that belong to a different platform.
- Playwright's developer-only trace viewer, recorder, dashboard, HTML report assets and TypeScript
  declarations are excluded; the automation feature never loads them.
- The Windows release tree drops from 400.7 MB to approximately 373.6 MB, and the Setup executable
  from 115.19 MB to approximately 114.3 MB. The standalone Linux and macOS backend trees drop by
  approximately 13 MB each.

### Known limitations

- All limitations recorded for v0.2.2 continue to apply.
- The WPF edition receives no new features from this version onwards; requests for new behaviour
  are handled on the Avalonia edition.
- Windows artifacts are not Authenticode-signed; macOS artifacts are neither signed nor notarised,
  because the required certificates are not held by the build machine. `SIGNING.txt` documents the
  remaining steps.

### Delivered artifacts

- `EchoDesk-v0.2.3-Setup.exe` (Windows, no models).
- `EchoDesk-v0.2.3-win-x64.zip`, `EchoDesk-v0.2.3-linux-x64.tar.gz` and
  `EchoDesk-v0.2.3-osx-arm64.tar.gz`.
- `SHA256SUMS.txt` and per-RID manifests. The artifacts are frozen in `publish/archive/v0.2.3/` and
  their hashes are recorded in its `MANIFEST.json`.

---

## v0.2.2

### Release positioning

A cross-platform stable release. This version completes the frontend cross-platform migration, establishes
a delivery system consistent across Windows, macOS, and Linux, and systematically closes out visual
consistency, bilingual coverage, and release gating.

### Key capabilities

**Cross-platform architecture**

- A new Avalonia cross-platform frontend supports Windows, macOS, and Linux with the same interface
  structure and interaction behaviour as the Windows edition.
- The backend gains platform abstraction: capability detection, degradation paths, data-directory
  conventions, and per-platform distribution.
- Three-platform artifacts include the Windows portable package, the Linux x64 archive, and the macOS arm64
  archive, each with a per-file manifest.

**Interface and visual consistency**

- The Avalonia main window and the seven Settings tabs are aligned page by page with the Windows edition,
  unifying palette, control templates, font sizes, spacing, corner radii, and borders.
- The main window replaces bubble-style messages with the Windows edition's structure of timestamp, body,
  and action bar; the input area, status strip, and bottom hint are aligned row by row.
- The main window and the Settings window share a default size of 860×620; previously remembered sizes are
  migrated automatically and constrained to the usable screen area.
- Sliders, tabs, buttons, and text boxes follow a unified control style matching the Windows edition.

**Full bilingual coverage**

- A systematic sweep of user-visible text bypassing the language packs identified 485 sites, remediated and
  reviewed in six batches.
- Machine-readable markers replace Chinese-text sniffing, so the English interface no longer depends on
  Chinese string matching.
- Missing interface keys are supplied; both Chinese and English interfaces are covered by automated scans of
  the main window and all seven tabs.
- Language-pack completeness checks and reverse key audits pass with zero missing keys.

**Reliability and consistency**

- Frontend and backend agree on the data directory, preventing uploaded results and backend-written results
  from landing in different locations.
- Language, theme, and font-size changes take effect immediately and are persisted.
- Accessibility improvements: screen-reader descriptions, control names, and help text cover key
  interactions.

**Release engineering and gates**

- The full release gate, including interface probes, pixel verification, and end-to-end tests, passes 50/50.
- Cross-platform artifact checks pass 28/28.
- Artifacts are frozen in `publish/archive/v0.2.2/`, with all eight hashes in the manifest verified.
- Version resources derive from `backend/version.py`, keeping build artifacts consistent.

**Security and boundaries**

- Automation runs in a visible browser and is forcibly disabled while the safety protocol is unaccepted.
- MCP tools are treated as sensitive by default and require explicit authorisation; environment variables
  are not forwarded to external processes by default.
- Model downloads require confirmation and never start automatically.

### Known limitations

- Neither the installer nor the three-platform artifacts are code-signed; Windows may show a SmartScreen
  prompt on first run, and macOS requires manual approval on first run.
- The installer interface is English only; placing `ChineseSimplified.isl` in Inno Setup's `Languages\`
  directory enables the Chinese interface.
- Not tested on Windows 10; conclusions are based on a Windows 11 environment.
- Automation depends on the locally installed Microsoft Edge; no browser engine is bundled.
- Vector retrieval is not implemented; local retrieval uses a dependency-free keyword approach.
- Automatic updates are limited to checking and notifying; automatic download-and-replace and incremental
  updates are not included.
- The build is not reproducible: rebuilding the same commit does not yield identical hashes. Tracked in [#6](https://github.com/wyx-spark/EchoDesk/issues/6).

### Artifacts

| File | Size | SHA-256 |
| :--- | ---: | :--- |
| `EchoDesk-v0.2.2-Setup.exe` | 113.89 MB | `634c8c874808a46a90de15055bed91a146c9467dfc46cc841895cbe945a62606` |
| `EchoDesk-v0.2.2-win-x64.zip` | 148.47 MB | `7945f614bd28f135bea18b1d5811fdf180f51d9cfd2a8b72305bb00c346be5ff` |
| `EchoDesk-v0.2.2-linux-x64.tar.gz` | 227.88 MB | `0545a1c12c82930cbbabfc4645e86d3f10fb23d31ef855dad5533802a701dff5` |
| `EchoDesk-v0.2.2-osx-arm64.tar.gz` | 178.25 MB | `e02a29e808fd647ebd7d61ecc4802fa7c759e3bc6be21feb031b7a73a6474b4e` |
| `manifest-win-x64.json` | 2.46 KB | `217e557e9ce808b7290b817a7899094a9952b8cab373f45c47a979e967d8935f` |
| `manifest-linux-x64.json` | 2.47 KB | `21dfe62b280fa84c91b5d7f5aa729656858ec9ca525ac766614faafe27b93fff` |
| `manifest-osx-arm64.json` | 2.47 KB | `e942660a5dedd81d9701c23eb8c852d670c63dcea7d5ea885f3f01c59feea62b` |
| `SHA256SUMS.txt` | 659 B | `75be14d6ad4400da21176751ddc3adfbe667a361dc3045a928a69834950ef561` |

---

## v0.2.1

### Release positioning

The first stable release prepared for open-source publication, and the final state before the
cross-platform transition. This version consolidates the capability model, introduces the Agent platform and
MCP support, and brings installation and delivery into the release gate.

### Key capabilities

**Capability consolidation**

- Conversation modes are consolidated from six to three: `chat` (general conversation, default), `contract`
  (clause review), and `agent` (tool-calling agent).
- Task templates expand to eight with the addition of `creative` and `guide`; former modes are invoked
  through `/task`, and previous spellings remain supported.
- Legacy mode numbers are no longer migrated automatically, avoiding ambiguity between the old and new
  schemes.

**Agent platform**

- The model decides which tool to call, in which order, and with which arguments until it reaches a
  conclusion; each step's call, arguments, result, and duration are shown in the conversation area.
- The initial tool set includes a calculator (AST-whitelist evaluation), date arithmetic, unit conversion,
  keyword lookup in uploaded files, and document summarisation.
- Tools are tiered: purely local computation tools are enabled by default; tools that access the network or
  arbitrary local files are disabled by default and require explicit authorisation.
- A step limit converges the loop to an answer based on available information rather than running
  indefinitely; `Esc` stops the loop at any time.

**MCP client**

- A self-implemented stdio MCP client that connects to user-configured external MCP servers and exposes
  their tools to the Agent.
- Security boundaries are explicit: MCP tools are always treated as sensitive and require authorisation;
  parent-process environment variables are not passed to third-party processes by default; the application
  never discovers or connects to servers on its own.

**Plugin ecosystem**

- Plugins hot-reload after installation, so commands are immediately available.
- Plugin-to-plugin dependencies and a minimum host version are supported; missing or mismatched dependencies
  and an insufficient host version cause the plugin to be refused with an explanation.
- Mismatches between declared and registered commands are reported as warnings; plugin exceptions,
  including `SystemExit`, are isolated and do not terminate the backend.

**Model management**

- The Model page gains an inference-parameter panel: temperature, top_p, repeat penalty, maximum generation,
  generation presets, context strategy, and two toggles; saved values are read back after they take effect.
- Custom model tiers can be created, edited, renamed, and deleted without editing configuration files;
  deleting a tier does not delete the model file, and a tier in use cannot be deleted.
- Out-of-range parameters now report an error instead of being silently clamped.

**Local retrieval**

- New BM25-based local document retrieval using only the standard library, with no new dependencies.
- Short documents are injected in full; long documents use a summary-plus-retrieval approach, where
  retrieved passages carry file names and character ranges and the model is asked to cite `[n]`.
- Incremental indexing: a new file indexes only itself, a file with the same name replaces its entry, and a
  deleted file is no longer searchable.

**System prompt and reasoning**

- The system prompt is reconstructed to emphasise judgement and output constraints rather than template
  filling.
- Model reasoning is separated from the final answer and displayed in a collapsible region with word count
  and streaming expansion.

**Model marketplace**

- Full result loading, paginated browsing, and local filtering and sorting; the pager stays fixed below the
  result list.
- Model tiers and the local model library are presented in a coordinated view.

**Default model and delivery**

- The default model changes to Qwen3-0.6B, making reasoning visible out of the box; the installer no longer
  bundles a model.
- Installation is standardised on an Inno Setup installer, with the default install location set to the
  directory containing the installer; an end-to-end install/uninstall acceptance script is added.
- Login-state reuse, link extraction, and visual-selection semantics are improved.

**Bilingual coverage and compliance**

- Interface text, command help, and documentation fully cover Simplified Chinese and English.
- The studio name is finalised as 熵寂工作室 / Void Entropia Studio.

### Known limitations

- Fine-grained progress and deep diagnostics in automation, the skill marketplace, and the model marketplace
  remain Chinese; the interface and command help are fully bilingual.
- Only Simplified Chinese and English language packs are provided; the mechanism and template tooling for
  other languages are ready for community contributions.
- Example skills have not yet been published to the GitHub marketplace.
- Automatic updates are limited to checking and notifying; automatic download-and-replace and incremental
  updates are not included.
- This release does not include interface screenshots.
- The third-party licence inventory and SBOM must be regenerated in a clean environment and re-verified.
- Not tested on Windows 10.
- Automation's safety protocol has no independent audit log.
- The configuration items `model_hub.enabled` and `automation.start_with_windows` are not yet read by the
  code.
- Vector retrieval is not implemented; local retrieval uses a dependency-free keyword approach.
- Developer documentation is available in Chinese only.
- The results directory rename (`批量处理结果\` → `results\`) is not migrated automatically.
- The installers are not code-signed and may trigger a SmartScreen prompt on first run.
- The installer interface is English only; placing `ChineseSimplified.isl` in Inno Setup's `Languages\`
  directory enables the Chinese interface.
- Recompiling the installers requires Inno Setup 6.

### Artifacts

| File | Size | SHA-256 |
| :--- | ---: | :--- |
| `EchoDesk-v0.2.1-Setup.exe` | 113.67 MB | `0665b805925d5043ccb66cbad78f7f6dec202994010aa3985637cc1eef6b724d` |
| `EchoDesk-v0.2.1-portable.zip` | 158.74 MB | `be7b221d4c0087988278fedad4f5172e48f63f10e2d0ccee3f2cc3d615d5eab8` |

---

## v0.2.0

### Release positioning

A feature release. Building on v0.1.0, this version delivers three engineering expansions: long-document
and long-context handling, a startup and delivery experience designed for real-world use, and two new
functional modules — browser automation and a skill marketplace.

### Key capabilities

**Long context and long documents**

- The default context window increases from 4096 to 8192, with a new ceiling check; the effective window is
  the smallest of the configured value, the model's training window, and the ceiling.
- New GGUF metadata reader that obtains the training window, layer count, and KV attention heads without
  loading the model, and estimates KV Cache memory usage.
- New map-reduce chunked summarisation: long documents are split on paragraph and sentence boundaries,
  summarised chunk by chunk, then merged recursively; the output retains pointers to the original text.
- New `/summarize` command that triggers a full-text summary, with progress shown live in the conversation
  area.

**Model and startup experience**

- A confirmation step precedes model download, stating the file name and size; download begins only after
  confirmation, and refusal yields follow-up guidance.
- The launcher shortcut is validated at startup and can be created or recreated when missing or broken.
- New first-run onboarding covering model acquisition, shortcuts, long documents, and common operations.

**Interface and accessibility**

- All message content is selectable and copyable; each message carries an action bar with Copy / Markdown /
  Code / Select all.
- New keyboard shortcuts: `Esc` stop, `Ctrl+L` clear, `Ctrl+O` upload, `Ctrl+Shift+C` copy the whole
  conversation, `Ctrl+plus/minus/0` font size, `Ctrl+T` theme, `Ctrl+,` settings, `Ctrl+M` skill marketplace.
- Window size, position, and maximised state are remembered; theme and font size are persisted; the window
  is never restored off-screen on multi-monitor or high-DPI setups.
- The status bar reports model, web search, automation, and marketplace connection state, with a new bottom
  notification strip.

**Release engineering**

- The release output becomes a single directory with multiple files and a single entry point; the frontend
  ships as multiple files and the backend uses a PyInstaller onedir layout.
- New one-click release script and Inno Setup installer template; new icon generation and version-resource
  tooling.
- Backend size decreases from 976 MB to 178 MB by removing the pyxtxt dependency; each file format is now
  parsed directly.

**New functional modules**

- Browser automation: Playwright drives the locally installed Microsoft Edge in a visible window, scraping
  the task list by selector, opening detail pages, filtering and downloading files by extension, and passing
  them to the local model for summaries and review notes. Supports pause on CAPTCHA/MFA, task deduplication,
  paging, failure screenshots and skipping, and a redacted audit log; the feature is forced off while the
  safety protocol is not accepted.
- Skill marketplace: searches skills by GitHub topic, with sorting, local caching and offline fallback,
  `skill.json` validation, detail display, install and uninstall. Third-party preparation scripts are never
  executed automatically and are reported only.

**Documentation and compliance**

- New privacy policy, end-user license agreement, third-party license inventory, FAQ, developer guide, and
  skill development guide.

**Reliability and fixes**

- Fixed the root cause of backend termination under an English locale: standard output is now uniformly
  UTF-8.
- New backend crash log and frontend global exception guard; download and warm-up progress and backend
  failure notices are shown in the conversation area.
- Fixed the Settings window hanging on open: a slider fired its callback during XAML parsing and
  dereferenced a null field; initialisation gating and null guards were added.
- New request gate in frontend–backend communication, preventing concurrent requests from the main window
  and the Settings window from interleaving.

### Known limitations

- Legacy binary `.doc` and `.ppt` files require WPS or Office on the local machine; scanned PDFs have no
  text layer and no OCR is provided.
- Automation depends on the locally installed Microsoft Edge; no browser engine is bundled.
- Automation downloads at most 200 MB per file; larger files are skipped and recorded.
- Shadow DOM piercing in automation is a simplified implementation; complex pages may still require selector
  adjustments.
- Scheduled automation relies on the backend scheduling thread, so the application must remain running.
- The skill marketplace does not install Python dependencies automatically; it only advises the user.
- The skill runtime loader interface is defined, but runtime wiring is deferred to a later release.
- No code-signing certificate is provided; a SmartScreen prompt may appear on first run.
- The build scripts require PowerShell 5.1 or later.

### Artifacts

| File | Size | SHA-256 |
| :--- | ---: | :--- |
| `EchoDesk-v0.2.0-portable.zip` | 177.52 MB | `a62005c8bea45bc2ee49c2543567663a024a9735232540058bd83ebbbbcb0f92` |

---

## v0.1.0

### Release positioning

The first public release of EchoDesk (Early Preview). This release establishes the product's local-first
posture: model inference and user data remain on the local machine, with no dependency on cloud services. It
is delivered as a two-tier architecture composed of a Python backend and a C# WPF frontend.

### Key capabilities

**Architecture**

- A Python backend provides local inference on top of llama.cpp; a C# WPF frontend provides the user
  interface and interaction; the two communicate over a JSON line protocol.

**Conversation**

- Multiple conversation modes and temperature presets for general Q&A, summarisation, and format
  conversion.
- Session persistence, allowing context to be restored after a restart.
- Canned phrases for frequently reused prompts.

**Input and processing**

- Upload of Office documents, plain text, and PDF files, with the content injected into the conversation
  context.
- Optional web search for time-sensitive information.
- Markdown rendering of conversation content.

**Interface**

- Light and dark themes.
- Adjustable font size.

**Command set**

- `/help`, `/mode`, `/file`, `/search`, `/model`, `/clearcache`, `/prompt`, `/temp`, `/darkmode`, `/stop`,
  `/snippet`, `/status`, `/clear`, `/undo`, `/save`, `/about`, `/exit`.

### Known limitations

- Windows only. The build is not code-signed and may trigger the system SmartScreen prompt on first run.
- Under an English locale, the backend process may terminate due to standard-output encoding; this was fixed
  in v0.2.0.
- Legacy binary `.doc` and `.ppt` files require WPS or Office on the local machine. Scanned PDFs have no text
  layer, and this release provides no OCR.
- No automatic update capability.
- The build is not reproducible: rebuilding the same commit does not yield identical hashes.

### Artifacts

| File | Size | SHA-256 |
| :--- | ---: | :--- |
| `EchoDesk-v0.1.0-setup.exe` | 368.72 MB | `2f3146557127d23f06dbd757d7fd3c480c03a21f95a8ea8ad45347927cf3b5b5` |
| `EchoDesk-v0.1.0-portable.zip` | 386.18 MB | `7a278ee5f7c5867b806cd018d1918bbd10d13c852ea7488dcf5eb3b91f7d5874` |
