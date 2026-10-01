# EchoDesk Changelog

> A Chinese reference translation of this file is available at
> [`CHANGELOG.zh-Hans.md`](CHANGELOG.zh-Hans.md); if the two ever disagree, this English version prevails.

This file records the feature changes, fixes, and known limitations of each version.
Version numbers follow `MAJOR.MINOR.PATCH`; file version resources are uniformly `X.Y.Z.0`.

---

## v0.2.1

> The first version aimed at **open-source publication**. For the complete implementation plan, key decisions,
> deviations from the task specification, unfinished items, and size records, see the internal development
> record (`v0.2.1-研发记录.md`, **not distributed with the release**).

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
