# EchoDesk Documentation (English)

EchoDesk is a **local, auditable AI desktop assistant**: model inference, document parsing and browser
automation all run on your own machine, and your data never leaves it.
This folder is the entry point for the English documentation.

中文文档入口 / Chinese documentation: [`docs/zh-Hans/README.md`](../zh-Hans/README.md).

---

## Platforms and installation

EchoDesk runs on **Windows, macOS and Linux**. The Windows build is the complete shipped product; Linux and
macOS are served by the **cross-platform frontend** on the same Python backend. **Two capabilities are
Windows-only** — reading WPS/Office **encrypted** documents through COM (legacy `.doc` / `.xls` included) and
the **enterprise-intranet browser automation**. Both degrade explicitly, naming the platform reason and
offering alternatives, instead of failing silently: [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) has the matrix and
the alternatives. Everything else (chat and streaming, file parsing, retrieval, Agent, MCP, skills, model
management) behaves the same on all three platforms.

| Platform | What you download | How you start it |
| --- | --- | --- |
| Windows | `EchoDesk-v0.2.2-Setup.exe` (installer) or the portable `…-win-x64.zip` | Start Menu / desktop shortcut, or the portable launcher inside the program folder |
| Linux x86-64 | `EchoDesk-v0.2.2-linux-x64.tar.gz` | `./start.sh` inside the unpacked directory |
| macOS (Apple silicon) | `EchoDesk-v0.2.2-osx-arm64.tar.gz` | `./start.sh` inside the unpacked directory |

The Linux and macOS builds are self-contained archives with **no installer**: the UI runtime and the backend
interpreter are both inside, so the target machine needs neither .NET nor Python.

```bash
# Linux / macOS: verify, extract, start
sha256sum -c SHA256SUMS.txt                    # macOS: shasum -a 256 -c SHA256SUMS.txt
tar -xzf EchoDesk-v0.2.2-linux-x64.tar.gz
cd EchoDesk-v0.2.2-linux-x64 && ./start.sh     # chmod +x start.sh app/EchoDesk first if the bits were lost
```

User data (conversation history, settings, `results/`) lives in the **user data folder**, which each platform
resolves its own way:

```
# Windows — the program folder itself (%APPDATA%\EchoDesk when that is where you installed or unpacked it)
C:\Users\<user>\EchoDesk-v0.2.2\
%APPDATA%\EchoDesk\

# macOS
~/Library/Application Support/EchoDesk/

# Linux (~/.local/share/EchoDesk when XDG_DATA_HOME is unset)
$XDG_DATA_HOME/EchoDesk/
```

`ECHODESK_DATA` overrides that location on every platform. Step-by-step installation and startup — including
the macOS Gatekeeper note and running without a graphical environment — is in
[`USER_GUIDE.md`](USER_GUIDE.md).

## Documents in this folder

| Document | What it covers | Who it is for |
| --- | --- | --- |
| [`USER_GUIDE.md`](USER_GUIDE.md) | User guide: install and start, everyday Q&A, long contract review, batch automation, skill marketplace, keyboard shortcuts | Everyone |
| [`FAQ.md`](FAQ.md) | Frequently asked questions and troubleshooting: startup, models, file parsing, automation, marketplace, UI | Check here first when something breaks |
| [`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md) | **Automation configuration guide**: field overview, field-by-field instructions, a complete contract-review example, the selector-test wizard, a troubleshooting table, preset templates, config import/export | Users who want to batch-process web tasks in a browser |
| [`SKILL_GUIDE.md`](SKILL_GUIDE.md) | Skill (plugin) development guide: manifest rules, publishing to the marketplace, security requirements and review process | Skill developers |
| [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) | **Cross-platform support and degradation reference**: what works on Windows / macOS / Linux, the two Windows-only capabilities and their alternatives, data-directory conventions, packaging and verification evidence | Everyone; essential on macOS or Linux |
| [`COMPLIANCE.md`](COMPLIANCE.md) | **Enterprise deployment and compliance guide**: proxy, certificates, firewall domain allow-list, Group Policy and restricted environments, auditing and retained evidence | IT administrators, compliance and security reviewers |

## Other languages

| Language | Documentation entry point |
| --- | --- |
| 简体中文 (zh-Hans) | [`docs/zh-Hans/README.md`](../zh-Hans/README.md) |

## Shared documents (language-neutral)

These documents are not language-specific: each one ships as an English default plus a Chinese
reference translation (`*.zh-Hans.md`), and the English file always prevails.

- [`../../CHANGELOG.md`](../../CHANGELOG.md) — changelog (additions, changes, fixes and known limitations per release). Lives at the **repository root** so GitHub links to it directly; Chinese at [`../../CHANGELOG.zh-Hans.md`](../../CHANGELOG.zh-Hans.md).
- [`../DEVELOPER.md`](../DEVELOPER.md) — developer documentation: architecture, modules, JSON-line protocol, extension points. A short pointer: the full text is [`DEVELOPER.md`](DEVELOPER.md) here and [`../zh-Hans/DEVELOPER.md`](../zh-Hans/DEVELOPER.md)
- [`../PRIVACY.md`](../PRIVACY.md) — privacy statement: network behaviour, where data is stored, how it is cleaned up (Chinese: [`../PRIVACY.zh-Hans.md`](../PRIVACY.zh-Hans.md))
- [`../EULA.md`](../EULA.md) — end user license agreement (Chinese: [`../EULA.zh-Hans.md`](../EULA.zh-Hans.md))
- [`../ENCRYPTED_DOCUMENTS.md`](../ENCRYPTED_DOCUMENTS.md) — reading documents protected by an enterprise encryption system: prerequisites, the no-circumvention statement, responsibility, and what stays in memory (Chinese: [`../ENCRYPTED_DOCUMENTS.zh-Hans.md`](../ENCRYPTED_DOCUMENTS.zh-Hans.md))
- [`../THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md) — third-party components and licenses (Chinese: [`../THIRD_PARTY_LICENSES.zh-Hans.md`](../THIRD_PARTY_LICENSES.zh-Hans.md))
- [`../SBOM.json`](../SBOM.json) — software bill of materials (SPDX-style, generated by `python tools/gen_sbom.py`)
- [`../../SECURITY.md`](../../SECURITY.md) — security policy: supported versions, private reporting channels, response commitments, security design and accepted scope (Chinese: [`../../SECURITY.zh-Hans.md`](../../SECURITY.zh-Hans.md))

`docs/SECURITY.md` is a short pointer to the root policy, and `docs/DEVELOPER.md` is a short pointer
to the developer documentation in this folder and in `docs/zh-Hans/`. Translations of the shared
documents are welcome; see the translation section below.

## Project-level documents (repository root)

- [`../../README.md`](../../README.md) — English project overview and quick start (shown on the GitHub home page)
- [`../../README.zh-Hans.md`](../../README.zh-Hans.md) — Chinese project overview
- [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) — how to file issues, contribute code, and **contribute translations** (Chinese: [`../../CONTRIBUTING.zh-Hans.md`](../../CONTRIBUTING.zh-Hans.md))
- [`../../SECURITY.md`](../../SECURITY.md) — how to report security vulnerabilities privately
- [`../../CODE_OF_CONDUCT.md`](../../CODE_OF_CONDUCT.md) — community code of conduct

---

## Translation contributions

Additional languages are welcome. Documentation for a locale lives in `docs/<locale>/` (for example
`docs/ja/`), and the locale code must match the language pack file names. UI and backend message packs
live in `frontend/Resources/Lang/<locale>.json` and `backend/lang/<locale>.json`.

Claim or submit a translation through the "Translation contribution" issue template, and run the
completeness checker before opening a pull request (the same command line works in PowerShell or `cmd` on
Windows, in Terminal on macOS and in a Linux shell; use `python3` if `python` is not on `PATH`):

```bash
python tools/check_lang.py --locale <locale>
```
