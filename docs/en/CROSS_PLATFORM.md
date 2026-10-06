# EchoDesk Cross-Platform Support and Degradation Reference

EchoDesk is an offline desktop AI assistant: model inference, document parsing and browser automation all
run on your own machine. The **Python backend** is platform-neutral; the **frontend** talks to it over a
JSON-line stdin/stdout protocol, so another toolkit's frontend drives the same backend unchanged.

Windows is the complete, shipped product today; Linux and macOS are served by the **cross-platform
frontend**. This document says what works where, what is Windows-only, what a user on another platform
actually sees, and how far verification goes. Related reading: `USER_GUIDE.md`, `AUTOMATION_GUIDE.md`
(the feature in section 3.2), `FAQ.md`, `SKILL_GUIDE.md`. Chinese: `docs/zh-Hans/CROSS_PLATFORM.md`.

> **Version naming**: **v0.2.1 is pre-cross-platform** (no `frontend-avalonia/`, no platform
> abstraction); **v0.2.2 is the cross-platform version** (frontend migration, platform-isation of the
> backend, three-platform distribution and packaging). The `v0.2.1` tag points at the last
> pre-cross-platform commit, and that version's installer and release artefacts are rebuilt from it —
> see `records/v0.2.1-发布记录.md` §30.

---

## 1. Platform support at a glance

| Platform | Status | Frontend | Packaging |
| --- | --- | --- | --- |
| **Windows** | Complete shipped product | WPF, in `frontend/` | Inno Setup installer (`publish\EchoDesk-v0.2.2-Setup.exe`), plus `…-win-x64.zip` from the three-platform bundle |
| **Linux** | Supported (portable bundle) | Avalonia, in `frontend-avalonia/`; self-contained publish for `linux-x64` | `EchoDesk-v0.2.2-linux-x64.tar.gz` — no installer, no Python needed on the target machine |
| **macOS** | Supported (portable bundle) | Avalonia, in `frontend-avalonia/`; self-contained publish for `osx-arm64` | `EchoDesk-v0.2.2-osx-arm64.tar.gz`, plus signing/notarization (needs the maintainer's Apple credentials) |

The Avalonia frontend now covers **the main window and the whole settings window**: the feature panels
(command palette, snippets, toast, status strip, Agent step rows) and all seven settings tabs are
migrated — see section 8 for what is still missing. Both frontends launch the same Python backend and
speak the same protocol to it, and the only platform-aware backend logic is the capability boundary below.

## 2. Capability matrix

| Capability | Windows | Linux | macOS | Notes |
| --- | --- | --- | --- | --- |
| Chat with streaming output | yes | yes | yes | Identical protocol; the Avalonia frontend covers the main window and the settings window |
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
2. If the **program directory is named `app`**, its parent (portable layout `EchoDesk-v0.2.2\app\`).
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

### 6.0 The backend ships in two forms, each covering a different job (since the cross-platform-2 round)

| Form | Used for | Why |
| :--- | :--- | :--- |
| **PyInstaller onedir** | the Windows installer | the WPF launcher path accepts a single executable only (it has no "interpreter + main.py" mode), and on Windows standalone brings no benefit over it |
| **python-build-standalone** | Linux / macOS and the portable cross-platform bundles | PyInstaller **cannot cross-compile**; standalone is a **relocatable prebuilt interpreter**, so a Windows build machine can prepare backends for all three platforms and the target machine just unpacks them — **no Python installation, no network access** |

The latter is produced by `tools/pack_backend.py`:

```
backend-<rid>/
    python/           bundled CPython (the target machine needs no Python)
    site-packages/    dependencies fetched by cross-platform pip (side by side, injected via PYTHONPATH)
    app/              backend code
    run-backend.bat   Windows launcher (for humans starting/debugging it by hand)
    run-backend.sh    Linux/macOS launcher (exec, so signals and exit codes pass through)
    manifest.json     platform / Python version / every dependency version / sizes
```

The points below are conclusions from things that actually went wrong:

* **The frontend does not go through the launcher**, it calls `python/python[.exe] app/main.py`
  directly. An extra shell layer turns "stop the backend" into "kill the shell while Python
  keeps running", and the frontend manages the backend as a child process.
  The launchers still exist — for **humans starting and debugging it by hand**.
* **Dependencies are not merged into `python/lib/site-packages`** but sit beside the interpreter:
  that keeps "shipped with the interpreter" and "installed by us" clearly separable, and lets
  `manifest.json` record each dependency version.
* **Environment markers are evaluated for the target platform** when fetching dependencies.
  pip itself evaluates them for the **build machine**, so preparing Linux dependencies made it
  demand a Linux build of `pywin32` and fail the whole dependency chain.
* **uvbox was rejected**: its first run downloads dependencies from the network, which
  contradicts the core promise of "data never leaves the machine, works offline".
* **`llama-cpp-python` needs a special fetch route on macOS** (measured during the
  cross-platform-2 round — this one deserves its own entry): upstream publishes **only a source
  distribution on PyPI**; every wheel lives in the author's own GitHub Releases index. Worse, the
  **macOS arm64 wheels in the `cpu` index are corrupt** — the byte count and the official digest both
  match ("fully downloaded, not tampered with"), yet `lib/*.dylib` inside the archive is damaged
  throughout its compressed streams, so `pip install` cannot succeed. Versions measured as corrupt:
  `cpu` index 0.3.21 / 0.3.30 / 0.3.35 / 0.3.36, `metal` index 0.3.31–0.3.36; the `metal` index
  (the `vX.Y.Z-metal` release tags) has **0.3.30 as the last intact build**. macOS therefore
  **pins that single wheel by direct URL** (see `backend/requirements.txt`) — a direct URL rather
  than "index plus version" because the two indexes contain a file with the **same name but
  different contents**, so index selection is non-deterministic. The metal build is also simply the
  right choice on macOS: it enables the Metal (Apple GPU) backend.
  Fetch and verify with `tools/fetch_wheel.py` (curl resume plus per-member decompression check,
  which stops an upstream "downloads fine, cannot install" wheel before it reaches a release).
* **Size** (measured as disk usage, hard links de-duplicated; all three measured on a Windows
  build machine): the standalone backend is **295.9 MB unpacked on Windows, 450.9 MB on Linux,
  352.5 MB on macOS**, and **102.9 / 150.2 / 120.3 MB archived**. Those differences are real
  rather than an artefact of counting: the Linux standalone interpreter is simply larger.
  Two decisions brought these figures down a long way from the first attempt:
  1. **The interpreter archive is `install_only_stripped`, not `install_only`.** Upstream publishes
     both variants for every platform; the stripped one drops native debug symbols, which are only
     needed to debug a native crash with gdb/lldb (Python's own tracebacks come from the `.py`
     sources). Measured, the archive goes from 71.8 MB to **33.5 MB** on Linux and from 45.2 MB to
     **21.0 MB** on Windows; after packing, `python/` drops from 235.2/125.8 MB to **81.1/33.4 MB**.
     `--keep-symbols` switches back for native debugging.
  2. **The unreachable Tcl/Tk runtime is removed**: with the `tkinter` package already stripped,
     nothing can load `libtcl9*.so` / `libtk9*.so` / `tcl9/` / `tcl*.dll` any more (about 7 MB per
     platform). That is a reachability argument, not a guess.
  **Standard-library trimming was deliberately not attempted**: measurement showed the bottleneck is
  not "too many modules" — before stripping, 136.6 MB of the Linux `python/` is `libpython3.13.so`
  and 77.2 MB is `bin/python3`, both native binaries, while the standard library's `.py` files add up
  to a few tens of MB and the parts worth removing (test/idlelib/tkinter/ensurepip/distutils/
  `__pycache__`) were already gone. Trimming the standard library on the impression that "there are
  too many modules" would buy little and risk much (a rarely used module removed here only surfaces
  as an ImportError on the target machine).
  Verify with `python tools/check_standalone_backend.py` (structure / launcher syntax / runnable on
  this platform / protocol round trip **plus a real action set**; for a non-host platform it
  additionally performs a static check that the native dependencies really are valid shared
  libraries).


| Target | Tooling | Output |
| --- | --- | --- |
| Windows | Inno Setup, driven by `tools/build_release.ps1` | `publish\EchoDesk-v0.2.2-Setup.exe` |
| Linux | `tools/pack_crossplatform.py` | `.tar.gz` |
| macOS | `tools/pack_crossplatform.py` + signing/notarization | `.tar.gz` |

**PyInstaller cannot cross-compile**, but the *standalone* route does not need to: its interpreter is a
prebuilt relocatable archive, so `tools/pack_backend.py` can prepare the backends for all three platforms
from a Windows machine. `tools/pack_crossplatform.py` then does the per-RID work: it publishes the Avalonia
frontend for the target RID, wraps the backend alongside it (`--backend-mode standalone`), strips native
`.pdb` files (section 7), writes a top-level `start.sh` (or the Windows `.bat`), produces a `.tar.gz` on
Linux/macOS, and writes `SHA256SUMS.txt` plus `manifest-<rid>.json` so a downloaded archive can be verified.
The two packing scripts have different default output directories (`pack_backend.py` uses `publish/`, this
one uses `dist/`), so it **looks for the standalone backend in both** — running each with its own defaults
in sequence therefore works.

The release directory is deliberately three layers deep (root / `app/` / program files) so that
`UserPaths.DataRoot` recognises a portable layout and keeps user data outside the program. The cost is that
a user who unpacks the archive sees a pile of documents and an `app/` directory, which is why the top-level
`start.sh` exists: it does one thing — hand over to `app/EchoDesk` via `exec`, passing signals and the exit
code straight through.

**macOS distribution additionally needs code signing (Developer ID) and notarization with
`notarytool`.** Those steps need the maintainer's Apple credentials; they are documented but **not
automated** here, so a macOS archive is release-ready only after a human holding them has run them.

**CI.** `.github/workflows/ci.yml` has two cross-platform jobs with different jobs to do:
`cross-platform` builds, publishes and headless-self-tests the Avalonia frontend on the Windows, Ubuntu and
macOS runners (proving the TFM is platform-neutral); `cross-platform-release` actually **produces the
complete release artefact for all three platforms**
(`pack_backend.py` → `check_standalone_backend.py` → `pack_crossplatform.py`), because whether the
cross-prepared dependencies really start on the target platform can only be verified by running them there.

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
| (c) `tools/check_standalone_backend.py` inspects the packaged backend | Structure, launcher syntax, and on Windows the interpreter actually runs and answers a **real action set** (`commands`, `capability`, `context`) |
| (d) The same tool statically checks macOS native dependencies (Mach-O headers of `llama_cpp/lib/*.dylib`) | That a "downloads fine, cannot install" upstream wheel is caught on the build machine — the corrupt macOS wheels in this round were found exactly this way |
| (e) `tools/check_crossplatform_release.py` inspects the finished archives | That a user can actually unpack and start them: archive format matches the target, executable bits survive, parent directory entries precede their contents, the top-level launcher and `SIGNING.txt` are inside, and the digests agree with `SHA256SUMS.txt` and the manifest |
| (f) `tools/test_platform.py` injects a platform via `ECHODESK_FORCE_PLATFORM` | That the non-Windows **degradation branches** are right: the Windows-only message, the alternatives, the suppressed Windows-specific hint, and the `platform` fields including `degraded` and the data root |
| (g) The CI matrix runs the real Linux/macOS branches on real Linux/macOS runners | Run-level evidence on those platforms — obtained in CI, not in this repository's local history |

**No run-level Linux or macOS evidence exists in the repository itself.** The three-platform backends and
release bundles were cross-prepared on a Windows build machine and verified structurally, statically (the
macOS binaries) and — on Windows — at protocol level. Rows (b), (c), (d) and (e) must **not** be read as
"it was run on Linux/macOS". CI is where the real runs are obtained.

## 9. Known frontend differences (WPF vs Avalonia)

None of these change capabilities, but all are user-visible, so they are listed rather than hidden.

- **Rich text**: WPF uses `FlowDocument`; Avalonia renders markdown into a control tree.
- **Link tooltips**: no URL tooltip on inline runs, so the **URL is appended after the link text**.
- **Font zoom**: zooming re-renders messages instead of changing one document-wide font size.
- **Theming**: Avalonia uses `FluentTheme` + `ThemeVariant` instead of two hand-maintained palettes.
- **Copy and selection**: per-message copy/selection is per paragraph.
- **Destructive-command confirmation**: there is no built-in `MessageBox`, and the project's "click
  twice" pattern does not fit an Enter-driven palette, so the second confirmation is "press Enter again
  in the same input box" — behaviourally equivalent, different in form.
- **Popups and tooltips**: Avalonia's `Popup` has **no** `StaysOpen` property, and `ToolTip` is an
  attached property (`ToolTip.SetTip(control, text)`).

## 10. Unfinished and planned work

Listed plainly, with no implied dates:

- **no real Linux/macOS run evidence**: the three-platform artefacts are cross-prepared on Windows and
  verified structurally, statically and (on Windows) at protocol level;
- the Windows installer **still uses PyInstaller**: the WPF launcher path accepts a single executable
  only, and standalone brings no Windows benefit — a deliberate trade-off, not an oversight;
- macOS code signing and notarization, and a Linux AppImage (the commands are written into
  `SIGNING.txt` and are never run by the build script);
- **the build is not reproducible**: three timestamp sources (`USAGE.txt`, rcedit writing `.rsrc`,
  PyInstaller writing the PE header) mean rebuilding the same commit yields a different hash;
  `SOURCE_DATE_EPOCH` is not implemented yet;
- `docs/SBOM.json` and the third-party licence manifest were **not regenerated for v0.2.2** (the
  dependency closure is structurally unchanged);
- branding and icons for non-Windows platforms;
- a real interactive Linux/macOS smoke test by a human or CI runner, not just the headless self-test.

Until those land, the accurate description of Linux and macOS support is what section 1 says: a
cross-platform frontend covering the main window and the whole settings window, a platform-neutral
backend, two explicitly-degrading Windows-only capabilities, and verification that stops honestly at
the boundary described in section 8.
