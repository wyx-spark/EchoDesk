# EchoDesk Cross-Platform Support and Degradation Reference

EchoDesk is an offline desktop AI assistant: model inference, document parsing and browser automation all
run on your own machine. The **Python backend** is platform-neutral; the **frontend** talks to it over a
JSON-line stdin/stdout protocol, so another toolkit's frontend drives the same backend unchanged.

Windows is the complete, shipped product today; Linux and macOS have a **prototype** frontend. This
document says what works where, what is Windows-only, what a user on another platform actually sees, and
how far verification goes. Related reading: `USER_GUIDE.md`, `AUTOMATION_GUIDE.md` (the feature in
section 3.2), `FAQ.md`, `SKILL_GUIDE.md`. Chinese: `docs/zh-Hans/CROSS_PLATFORM.md`.

---

## 1. Platform support at a glance

| Platform | Status | Frontend | Packaging |
| --- | --- | --- | --- |
| **Windows** | Complete shipped product | WPF, in `frontend/` | Inno Setup installer (`publish\EchoDesk-v0.2.1-Setup.exe`) |
| **Linux** | Prototype frontend | Avalonia, in `frontend-avalonia/`; builds and publishes for `linux-x64` | `.tar.gz` built on Linux |
| **macOS** | Prototype frontend | Avalonia, in `frontend-avalonia/`; builds and publishes for `osx-arm64` | `.tar.gz` built on macOS, plus signing/notarization |

The Avalonia frontend's **main window is prototyped**: chat, streaming output, the thinking fold, file
upload, and theme / font / language switching all work; the remaining WPF windows (settings, panels) are
not migrated yet — see section 10. Both frontends launch the same Python backend and speak the same
protocol to it, and the only platform-aware backend logic is the capability boundary below.

## 2. Capability matrix

| Capability | Windows | Linux | macOS | Notes |
| --- | --- | --- | --- | --- |
| Chat with streaming output | yes | yes | yes | Identical protocol; the Avalonia main window is the prototype surface |
| File parsing (docx, xlsx, pptx, pdf, txt, csv, md) | yes | yes | yes | Generic pure-Python parsers, no Office installation required |
| Reading WPS/Office **encrypted** documents via COM | yes | no | no | Needs a local WPS/Office COM automation object — Windows-only by nature |
| Legacy binary `.doc` / `.xls` reading | yes | no | no | Also needs COM; pure-Python parsers do not cover the old binary formats |
| Local retrieval (RAG, BM25 + citations) | yes | yes | yes | Pure Python |
| Agent platform | yes | yes | yes | Pure Python |
| MCP client | yes | yes | yes | Pure Python |
| Plugins / skills | yes | yes | yes | Pure Python; the documented escape hatch for the rows above |
| Model management & local inference | yes | yes | yes | Pure Python |
| Enterprise intranet automation (Playwright driving a headed local Edge) | yes | no | no | Depends on the Windows Edge installation and the Windows-only automation UI |

The two "no" pairs are one boundary, not four gaps: **formats needing a real Office application, and the
browser-automation feature, are Windows-only.**

## 3. The degradation boundary

There are exactly **two** Windows-only capabilities, and both degrade explicitly rather than silently:
availability is decided from the **detected platform**, not from an import that happened to fail; an
unavailable capability names its reason and offers alternatives; and **Windows-specific advice is never
shown on other platforms**, since telling a Linux user to "install WPS and try again" is a wild goose
chase.

### 3.1 Reading encrypted WPS/Office documents via COM

**What is unavailable.** On Linux and macOS, EchoDesk cannot open documents protected by an enterprise
encryption system, and cannot read legacy binary `.doc` / `.xls` files.

**Why.** Both paths go through a local WPS/Office COM automation object that exists only on Windows, and
no cross-platform equivalent would decrypt an enterprise-protected document with the credentials the
logged-in user already has — doing it in pure Python would mean circumventing the protection.

**What you see.** Uploading such a file on a non-Windows platform returns an explicit message: the
capability is **Windows-only**, it is **not** a parse failure and **not** a missing dependency. It
carries three concrete alternatives:

- (a) **produce an unencrypted copy on a Windows machine** — save it unencrypted or as `.docx`, then upload that copy here;
- (b) **delegate the conversion** — use an MCP server, or a plugin/skill, that calls an external converter and returns plain text;
- (c) **register your own parser** — install a skill that registers a parser for that extension, so the document never reaches the Windows-only path.

You will not be told the file is corrupt, and you will not be told to **"install WPS then retry"** — that
advice is not shown to Linux or macOS users. Encrypted files keep working fully on Windows.

### 3.2 Enterprise intranet automation

**What is unavailable.** On Linux and macOS the automation page/feature is unavailable: you cannot run a
batch browser task that drives a headed local Edge window.

**Why.** The automation stack drives the Edge installation on the machine, with a visible window and a
reused sign-in state, and its configuration surface is part of the Windows-only UI. No equivalent is
wired up yet elsewhere.

**What you see.** Requesting the capability returns an unavailable answer, and the platform check
`validate()` reports exactly: `Windows-only capability (this is not a missing dependency)`. That wording
is deliberate — "not a missing dependency" says this is a platform decision, not a broken install, not a
missing package, and not something a `pip install` will fix.

**Alternative path**, in the order most users take them: through an **MCP server** that exposes the
browser work as tools the agent can call; through a **skill** that performs the flow and returns results
in the shape the built-in automation would; or by **running the automation on a Windows machine and
exporting the results**, then bringing those files here for review, retrieval and reporting.

## 4. Platform detection and reporting

Nothing is guessed at the UI layer: the backend detects the platform once and reports it, so a frontend
and a backend that disagree can be caught. The `platform` block arrives in the **`ready` handshake** and
on demand through a **`platform` action**:

| Field | Meaning |
| --- | --- |
| `kind` | `windows` / `macos` / `linux` / `unknown` |
| `name` | Human-readable platform name |
| `os` | Operating-system detail as the runtime reports it |
| `architecture` | CPU architecture |
| `rid` | .NET RID-style identifier: `win-x64`, `linux-x64`, `osx-arm64`, … |
| `python` | Interpreter details: version, implementation, and whether it is a frozen (PyInstaller) build |
| `forced` | `true` **only** when a test injected the platform (see `ECHODESK_FORCE_PLATFORM`) |
| `capabilities` | Per-feature record with `available`, `reason`, `alternative` and `windows_only` |
| `degraded` | The list of unavailable Windows-only features for this platform |
| `user_data_root` | The resolved data directory the process is actually using (section 5) |

`forced` matters for honesty: such a run is a test exercising a degradation path, not evidence gathered
on that platform.

The Avalonia frontend detects the same facts locally and **compares its own `kind` with the backend's
`kind`**. If they disagree the user is warned about **platform drift** — say a Linux frontend talking to a
Windows-built backend, or a stale backend process from another platform — so the matrix shown describes
the backend that is actually answering.

## 5. Directory conventions

### 5.1 Data root resolution order

The data root is resolved in this order; the first rule that applies wins.

1. The `ECHODESK_DATA` environment variable, if set.
2. If the **program directory is named `app`**, its parent (portable layout `EchoDesk-v0.2.1\app\`).
3. **Windows**: the program directory itself.
4. **macOS**: `~/Library/Application Support/EchoDesk`.
5. **Linux**: `$XDG_DATA_HOME/EchoDesk`, or `~/.local/share/EchoDesk` when `XDG_DATA_HOME` is unset.

Rule 2 precedes the per-platform rules because a portable or installed layout keeps its data next to the
program, exactly as `USER_GUIDE.md` describes.

### 5.2 Language packs

Two kinds of language pack exist and they **must not share a directory**: UI language packs live in
`<data root>/lang/ui`, backend language packs in `backend/lang/`. The split lets you add a UI language
without touching backend messages, and vice versa; the completeness checker (`tools/check_lang.py`)
expects it.

### 5.3 Environment variables

`ECHODESK_DATA` overrides the data root (rule 1 above); `ECHODESK_BACKEND` is the path to the backend
executable the frontend should launch; `ECHODESK_LANG_DIR` overrides the language-pack directory; and
`ECHODESK_FORCE_PLATFORM` is **test-only**, accepting `windows` / `macos` / `linux` / `unknown` to inject
a platform so degradation paths can be exercised on a Windows development machine.

## 6. Packaging and release

| Target | Tooling | Output |
| --- | --- | --- |
| Windows | Inno Setup, driven by `tools/build_release.ps1` | `publish\EchoDesk-v0.2.1-Setup.exe` |
| Linux | `tools/pack_crossplatform.py` | `.tar.gz` |
| macOS | `tools/pack_crossplatform.py` + signing/notarization | `.tar.gz` |

**PyInstaller cannot cross-compile**, so a Linux backend binary must be built on Linux and a macOS one on
macOS; there is no supported way to emit them from Windows. `tools/pack_crossplatform.py` does the
per-RID work: it publishes the Avalonia frontend for the target RID, wraps the Python backend alongside
it, strips native `.pdb` files (section 7), produces a `.tar.gz` on Linux/macOS, and writes
`SHA256SUMS.txt` plus `manifest.json` so a downloaded archive can be verified.

**macOS distribution additionally needs code signing (Developer ID) and notarization with
`notarytool`.** Those steps need the maintainer's Apple credentials; they are documented but **not
automated** here, so a macOS archive is release-ready only after a human holding them has run them.

**CI.** A GitHub Actions matrix in `.github/workflows/ci.yml`, job `cross-platform`, builds and publishes
all three RIDs and runs the headless self-test on each runner — where real Linux/macOS runs happen.

## 7. Measured artifact sizes

Avalonia frontend only, self-contained publish (`PublishSingleFile=false`); backend and models extra.

| RID | Files | Size | Notes |
| --- | --- | --- | --- |
| `win-x64` | 223 | 205.8 MB | about 100 MB is native `libSkiaSharp.pdb` + `libHarfBuzzSharp.pdb`; removed during packaging → roughly **106 MB** |
| `linux-x64` | 220 | 102.9 MB | — |
| `osx-arm64` | 220 | 112.2 MB | — |

Those two debug symbol files dominate the Windows number, which is why packaging strips them: elsewhere
the same libraries ship without that overhead, so the stripped Windows archive lands in the same class.

## 8. How this was verified — and what was not

A capability matrix is only as good as its evidence, and this development machine is **Windows-only**:
no WSL, no Docker, no macOS. Everything below follows from that.

| Verification | What it proves |
| --- | --- |
| (a) The Avalonia frontend and the real backend protocol ran for real on Windows, via headless `EchoDesk --selftest` | Platform detection, backend launch by platform rules, the `ready` handshake and the `platform` / `capability` round-trip — **run-level Windows evidence** |
| (b) Cross-RID builds for `linux-x64` and `osx-arm64` were produced from Windows | That the frontend **compiles and publishes** for those RIDs — **build-level evidence only**, not run-level |
| (c) `tools/test_platform.py` injects a platform via `ECHODESK_FORCE_PLATFORM` | That the non-Windows **degradation branches** are right: the Windows-only message, the alternatives, the suppressed Windows-specific hint, and the `platform` fields including `degraded` and the data root |
| (d) The CI matrix runs the real Linux/macOS branches on real Linux/macOS runners | Run-level evidence on those platforms — obtained in CI, not in this repository's local history |

**No run-level Linux or macOS evidence exists in the repository itself.** Locally there is build-level
plus injected-platform evidence; CI is where the real runs are obtained. For a non-Windows deployment
that is the honest summary: the code paths are tested, the binaries build, and CI is where they run.

## 9. Known frontend differences (WPF vs Avalonia)

None of these change capabilities, but all are user-visible, so they are listed rather than hidden.

- **Rich text**: WPF uses `FlowDocument`; Avalonia renders markdown into a control tree.
- **Link tooltips**: no URL tooltip on inline runs, so the **URL is appended after the link text**.
- **Font zoom**: zooming re-renders messages instead of changing one document-wide font size.
- **Theming**: Avalonia uses `FluentTheme` + `ThemeVariant` instead of two hand-maintained palettes.
- **Copy and selection**: per-message copy/selection is per paragraph.

## 10. Unfinished and planned work

Listed plainly, with no implied dates:

- migrating the remaining WPF windows (settings, panels) to Avalonia;
- per-platform PyInstaller builds and release archives produced in CI rather than by hand;
- macOS code signing and notarization, and a Linux AppImage;
- branding and icons for non-Windows platforms;
- a real interactive Linux/macOS smoke test by a human or CI runner, not just the headless self-test.

Until those land, the accurate description of Linux and macOS support is what section 1 says: a
prototyped frontend, a platform-neutral backend, two explicitly-degrading Windows-only capabilities, and
verification that stops honestly at the boundary described in section 8.
