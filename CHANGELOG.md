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
| [v0.2.2](#v022) | Stable Release | 2026-11-04 | Cross-platform frontend, visual consistency, full bilingual coverage |
| [v0.2.1](#v021) | Stable Release | 2026-10-05 | Capability consolidation, Agent platform, MCP, plugin ecosystem |
| [v0.2.0](#v020) | Feature Release | 2026-09-23 | Long context and documents, automation, skill marketplace |
| [v0.1.0](#v010) | Early Preview | 2026-08-29 | First public release: local inference with a desktop client |

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
- The build is not reproducible: rebuilding the same commit does not yield identical hashes.

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
