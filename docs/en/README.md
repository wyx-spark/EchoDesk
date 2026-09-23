# EchoDesk Documentation (English)

EchoDesk is a **local, auditable AI desktop assistant**: model inference, document parsing and browser
automation all run on your own machine, and your data never leaves it.
This folder is the entry point for the English documentation.

中文文档入口 / Chinese documentation: [`docs/zh-Hans/README.md`](../zh-Hans/README.md).

---

## Documents in this folder

| Document | What it covers | Who it is for |
| --- | --- | --- |
| [`USER_GUIDE.md`](USER_GUIDE.md) | User guide: install and start, everyday Q&A, long contract review, batch automation, skill marketplace, keyboard shortcuts | Everyone |
| [`FAQ.md`](FAQ.md) | Frequently asked questions and troubleshooting: startup, models, file parsing, automation, marketplace, UI | Check here first when something breaks |
| [`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md) | **Automation configuration guide**: field overview, field-by-field instructions, a complete contract-review example, the selector-test wizard, a troubleshooting table, preset templates, config import/export | Users who want to batch-process web tasks in a browser |
| [`SKILL_GUIDE.md`](SKILL_GUIDE.md) | Skill (plugin) development guide: manifest rules, publishing to the marketplace, security requirements and review process | Skill developers |

## Other languages

| Language | Documentation entry point |
| --- | --- |
| 简体中文 (zh-Hans) | [`docs/zh-Hans/README.md`](../zh-Hans/README.md) |

## Shared documents (repository `docs/` root)

These documents currently exist in Chinese only and live directly under `docs/`:

- [`../CHANGELOG.md`](../CHANGELOG.md) — changelog (additions, changes, fixes and known limitations per release)
- [`../DEVELOPER.md`](../DEVELOPER.md) — developer documentation: architecture, modules, JSON-line protocol, extension points
- [`../PRIVACY.md`](../PRIVACY.md) — privacy statement: network behaviour, where data is stored, how it is cleaned up
- [`../EULA.md`](../EULA.md) — end user license agreement
- [`../THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md) — third-party components and licenses
- [`../v0.2.0-研发记录.md`](../v0.2.0-研发记录.md) / [`../v0.2.0-修复记录.md`](../v0.2.0-修复记录.md) — development and fix records for v0.2.0 (Chinese)

English translations of these shared documents are welcome; see the translation section below.

## Project-level documents (repository root)

- [`../../README.md`](../../README.md) — English project overview and quick start (shown on the GitHub home page)
- [`../../README.zh-Hans.md`](../../README.zh-Hans.md) — Chinese project overview
- [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) — how to file issues, contribute code, and **contribute translations**
- [`../../SECURITY.md`](../../SECURITY.md) — how to report security vulnerabilities privately
- [`../../CODE_OF_CONDUCT.md`](../../CODE_OF_CONDUCT.md) — community code of conduct

---

## Translation contributions

Additional languages are welcome. Documentation for a locale lives in `docs/<locale>/` (for example
`docs/ja/`), and the locale code must match the language pack file names. UI and backend message packs
live in `frontend/Resources/Lang/<locale>.json` and `backend/lang/<locale>.json`.

Claim or submit a translation through the "Translation contribution" issue template, and run the
completeness checker before opening a pull request:

```powershell
python tools/check_lang.py --locale <locale>
```
