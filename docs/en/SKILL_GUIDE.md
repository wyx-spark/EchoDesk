# EchoDesk Skill Development Guide (v0.2.0)

This guide explains how to write, submit and publish an EchoDesk skill (plugin), together with the review process and the publishing rules.
Chinese version: [`docs/zh-Hans/SKILL_GUIDE.md`](../zh-Hans/SKILL_GUIDE.md).
To submit a skill, use the repository's "Skill submission" issue template.

**Platforms.** A skill is pure Python, so it runs on **Windows, macOS and Linux** alike, and nothing in this
guide is platform-specific: the paths below are written with forward slashes (`app/backend/skills/`), which is
how they read on macOS and Linux — on Windows the same path uses backslashes (`app\backend\skills\`), and the
program folder is wherever you installed or unpacked the bundle (`EchoDesk-v0.2.2/app/`). Two EchoDesk
capabilities a skill can run into *are* platform-specific: reading WPS/Office **encrypted** documents through
COM (legacy `.doc` / `.xls` included) and the enterprise-intranet browser automation are **Windows-only**.
If your skill itself depends on something Windows-only — calling WPS through COM, say — say
so plainly in the README (the marketplace shows it), because EchoDesk cannot make that component available on
the other platforms. The full matrix is in [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md).

---

## 1. What a Skill Is

A skill is a self-contained local extension package: one directory + a `skill.json` manifest + entry code.
EchoDesk discovers skills through a GitHub topic, and once the user confirms, installs the skill into
**`app/backend/skills/<技能id>/`** (inside `backend/` under the program directory) and registers it in
`app/backend/skills_registry.json`.

**It works as soon as it is installed**: the runtime loader **hot-reloads automatically** after a successful
install, so the commands the skill declares appear in the command table and the command panel immediately —
no restart and no manual "reload local plugins" step.

**Skills run with the privileges of the local user.** EchoDesk is not responsible for the safety of
third-party skills, so a manifest must declare its dependencies and permissions truthfully: they are
shown to the user verbatim before installation.

---

## 2. Directory Structure

```
my-skill/                     ← repository root
├─ skill.json                 ← required: the skill manifest
├─ README.md                  ← recommended: the marketplace shows the first 800 characters as a summary
├─ requirements.txt           ← optional: dependencies (never installed automatically, the user is only notified)
└─ echodesk_skill/            ← entry package (any name, matching echodesk.bundle in skill.json)
   ├─ __init__.py             ← required (the entry must be a package directory containing __init__.py)
   └─ main.py
```

---

## 3. The skill.json Specification

```jsonc
{
  "// 说明": "EchoDesk 技能清单；echodesk.bundle 是市场验证与安装的必要字段",

  "id": "contract-audit",                  // unique skill identifier (matching the repository name is recommended)
  "name": "合同财务审核",                   // display name (Chinese recommended; value kept as-is)
  "version": "1.0.0",                      // semantic version
  "description": "读取合同并生成财务审核意见",  // one-line description (shown on the marketplace card; value kept as-is)
  "author": "your-github-name",
  "license": "MIT",                        // license (the user is warned when it is missing)
  "homepage": "https://github.com/you/my-skill",

  "echodesk": {
    "bundle": "echodesk_skill",            // [required] entry package directory name, or path to the entry file
    "entry": "echodesk_skill/__init__.py", // optional explicit entry file
    "min_version": "0.2.0",                // oldest compatible EchoDesk version
    "api_version": "1"                     // skill API version
  },

  "dependencies": ["contract-core>=1.2"],  // **other plugins** this skill needs (list or dict form; validated at load time)
  "requirements": ["python-docx>=1.0"],    // Python package requirements: reported at install time, never installed automatically

  "publish": {                             // optional: other distribution channels
    "npm": "",                             // e.g. "@you/my-skill"
    "pypi": ""                             // e.g. "my-skill"
  },

  "capabilities": {                        // declared capabilities (so users can judge the permissions)
    "commands": ["/audit"],                // commands the skill can register
    "ui_panels": ["audit-panel"],          // UI panels the skill can register
    "automation_hooks": ["after_task"],    // automation hooks the skill can attach to
    "file_parsers": [".ofd"]               // file formats the skill can extend parsing for
  },

  "prepare": ""                            // post-install script (EchoDesk never runs it automatically)
}
```

**Hard field requirements**

| Field | Required | Notes |
| --- | --- | --- |
| `echodesk.bundle` | ✅ | Without it the skill is marked "unverified" in the marketplace and the install button is greyed out |
| `id` or `name` | ✅ | Used to derive the installation directory name (sanitized to `[A-Za-z0-9._-]`) |
| `version` | Recommended | The marketplace shows "unknown" when it is missing |
| `license` | Recommended | The user is warned in the install prompt when it is missing |
| `dependencies` | Optional | **Other plugins** this skill needs (list or dict form), validated at load time — see 3.1 |
| `echodesk.min_version` | Recommended | Oldest compatible program version; anything older refuses to load |

### 3.1 Dependency declarations and version compatibility (load-time semantics)

`dependencies` names **other plugins**, and the two spellings are equivalent (the loader normalises them to
`{plugin id: version requirement}`):

```jsonc
"dependencies": ["contract-core", "audit-utils>=1.2"]              // list
"dependencies": { "contract-core": "", "audit-utils": ">=1.2" }    // dict
```

Requirements support `>=` / `>` / `<=` / `<` / `==`, may be just a version number (read as "at least this
version"), and tolerate a `v1.2.0` prefix. The loader decides once, after discovery and before the per-plugin
load:

| Situation | Result |
| --- | --- |
| the dependency is not installed (absent from this round's list) | ❌ refuses to load, reporting the missing dependency |
| the dependency is installed but disabled | ❌ refuses to load, reporting it as missing (disabled) |
| the dependency's version does not satisfy the requirement | ❌ refuses to load, reporting "needs X, found Y" |
| the program version is below `echodesk.min_version` | ❌ refuses to load, asking for a newer EchoDesk |
| the dependency is present but declares no `version` | ⚠️ warning only, and it loads (development plugins often omit it) |

**Python package requirements** (such as `python-docx`) belong in `requirements` (or a bundled
`requirements.txt`): EchoDesk only reports them at install time and **never installs them automatically**.

After a successful load the loader also compares `capabilities.commands` with the commands actually
registered: **declared but not registered**, or **registered but not declared**, both go to the warnings and to
`app/backend/skills_dev.log` — this kind of silent inconsistency is the hardest thing to track down.

---

## 4. Publishing to the Skill Marketplace

### 4.1 Required Steps

1. **Add the topic `echodesk-plugin` to the GitHub repository**
   (repository page → the gear icon ⚙ in the top-right corner → About → Topics).
   **EchoDesk only searches for this topic**; any other label will not be discovered.
2. Make sure the repository root contains a `skill.json` with an `echodesk.bundle` field.
3. Write a clear `README.md`: what the skill does, which permissions it needs, and how to uninstall it.
4. Push the code, then press `Ctrl+M` in EchoDesk to open the skill marketplace and click "Refresh" to
   confirm that your skill can be found.

### 4.2 Naming and Versioning Rules

- Name the repository `echodesk-skill-<功能>` (easier to search for and recognize).
- Follow semantic versioning: `MAJOR.MINOR.PATCH`;
  breaking changes must bump `MAJOR` and update `echodesk.min_version` at the same time.
- Do not stuff large binary files into the repository, and do not include model weights.

### 4.3 Security Requirements (the focus of review)

- **Do not request unnecessary permissions**: declare in `capabilities` only the capabilities you truly use.
- **No network access** unless it is essential to the feature; any network access must be prominently
  documented in the README, including where the data goes.
- **Do not read users' private data**: do not scan user directories, and do not read files other than
  `session.json`/`snippets.json`.
- **Do not write to the registry, do not install services, do not modify system settings.**
- No obfuscated code, and `prepare` must not do anything that contradicts what you declared.
- Never write tokens or passwords to logs or files; if credentials are needed, the user must configure them explicitly.

> EchoDesk does **not** run the `prepare` script automatically at install time. If that capability is
> enabled in the future, every environment variable whose name contains `KEY`/`SECRET`/`TOKEN`/`PASSWORD`
> will be stripped before it runs.

---

## 5. Review Process (Proposed)

The skill marketplace currently lists every repository that carries the GitHub topic, and distinguishes
between "verified" and "unverified". The planned review process:

1. **Automated check** (already implemented): `skill.json` exists and contains `echodesk.bundle` → marked "verified".
2. **Manual review** (still to be established): maintainers check the skill against the security
   requirements in 4.3, looking mainly at whether the declared permissions are minimal, whether there is
   hidden network access or data collection, and whether the `license` is clear.
3. **Flagging and delisting**: when a malicious or rule-breaking skill is found, it is marked as
   untrusted in the verification cache (the button is greyed out in the frontend) and announced in the
   release notes.

---

## 6. Local Development and Debugging

### 6.1 Directories and priority

A plugin can come from three sources, **in descending order of priority**:

| Source | Location | Purpose |
| --- | --- | --- |
| Development directory | `app/backend/skills_dev/<plugin id>/` | day-to-day development: drop the directory in and it is picked up |
| Local manifest | `app/backend/skills_local.json` | explicitly registered local plugins (including trust / refusal records) |
| Installed | `app/backend/skills/<plugin id>/` + `skills_registry.json` | plugins installed from the skill marketplace |

**Development flow**: put the plugin directory into `skills_dev/`, turn on developer mode on the "Developer"
page of the Settings window, and click **🔄 Reload local plugins** after each code change. A plugin with the
same id resolves to the copy in the development directory, so you can debug locally without first uninstalling
the released version.

### 6.2 Hot reload and log-based troubleshooting

- **Automatic hot reload**: after a successful **install or uninstall** from the skill marketplace the backend
  hot-reloads and reports the result back to the UI — the commands are usable immediately, with no restart and
  no manual reload.
- **"Reload local plugins" reloads local sources only**: it unloads and reloads the plugins in `skills_dev/`
  and in the local manifest, while **installed plugins are left untouched** rather than being taken down and
  reinstalled wholesale (their runtime state survives too).
- **Log**: loading, unloading, warnings and errors all go to `app/backend/skills_dev.log`; the "Developer" page
  of the Settings window lets you **view it (with a configurable last-N-lines limit), 🗑 clear it and export
  it**. A sensible troubleshooting order is: the log → is the manifest valid → are the dependencies present
  and are the versions satisfied.
- **Isolation**: import, registration and command-execution failures affect only that one plugin; a plugin
  raising `BaseException` (a bare `sys.exit()`, say) is isolated the same way and does not take the backend
  process down.

### 6.3 Manual validation and registry fields

Run these from the skill's own directory. They are **plain Python**, so the same command line works in
PowerShell or `cmd` on Windows, in Terminal on macOS and in a Linux shell — only the interpreter name can
differ: use `python3` in place of `python` if your macOS/Linux system has no `python` on `PATH`. A portable
bundle carries its own interpreter, so there you can call it directly:
`app/backend/python/bin/python3` on macOS/Linux, `app\backend\python\python.exe` on Windows.

```bash
# 1) check that the manifest is valid (equivalent to the marketplace validation logic)
python -c "import json;d=json.load(open('skill.json',encoding='utf-8'));print('bundle =',d.get('echodesk',{}).get('bundle'))"

# 2) check that the entry point can be loaded
python -c "import importlib.util,pathlib;p=pathlib.Path('echodesk_skill/__init__.py');print('存在' if p.exists() else '缺失')"
```

Registry fields for installation and uninstallation (`app/backend/skills_registry.json`):

```jsonc
{
  "id": "contract-audit",
  "version": "1.0.0",
  "path": "skills/contract-audit",
  "loader": "python",
  "repo": "you/my-skill",
  "installed_at": "2026-01-01T10:00:00",
  "dependencies": ["python-docx>=1.0"],
  "signature": "<tarball 的 sha256>",
  "license": "MIT"
}
```

---

## 7. Frequently Asked Questions

**Q: Why can't I find my skill in the marketplace?**
A: Check that the topic is spelled `echodesk-plugin` (and not written in any other form, such as
`echodesk-plugin, ai`), that the repository is public, and that it has been pushed at least once.
GitHub's search index lags by a few minutes.

**Q: Why is the install button greyed out?**
A: A card showing "unverified" means the repository root is missing `skill.json`, or that file has no
`echodesk.bundle` in it.

**Q: What if my skill needs extra dependencies?**
A: Python package requirements go in `requirements` (or in a bundled `requirements.txt`). For safety reasons
EchoDesk only notifies the user at install time and never installs anything automatically, so document in your
README the commands the user needs to run. Dependencies on **other plugins** go in `dependencies`, which the
loader validates at load time (see 3.1).

**Q: Which capabilities can a skill hook into?**
A: `capabilities` can declare commands, UI panels, automation hooks and file-parser extensions.
**All of these extension points are wired up at runtime**: `backend/skills_runtime.py` imports the entry
dynamically and calls its `register(api)`, which puts the declared commands into the `/` command table, so they
are usable as soon as the skill is installed (or reloaded after being dropped into `skills_dev/`). The list of
extension points and how they are wired up is in the "Extension points" section of `docs/en/DEVELOPER.md`.

**Q: The plugin is installed — why is its command missing?**
A: Look at `app/backend/skills_dev.log` first. The most common cause is a dependency blocking it: a dependency
that is not installed or is disabled, an unsatisfied version, or a program version below
`echodesk.min_version` — each of those **refuses to load and states the reason**. Next, check that
`capabilities.commands` matches the commands actually registered (a mismatch is warned about); note also that
plugins in `skills_dev/` are not loaded while developer mode is off.

**Q: What if a plugin's dependency declares no version number?**
A: It is warned about and let through rather than blocking the load (development plugins routinely omit it).
What genuinely blocks a load is "the dependency is not installed / is disabled / its version does not
satisfy the requirement".

**Q: Can a plugin crash the whole program?**
A: No. Plugin loading, unloading, registration and command execution all catch `BaseException`: raising
`SystemExit` (a bare `sys.exit()` in a plugin, say) only aborts that step and does not take the backend
process down. If the backend really did exit, please attach `app/backend/backend_crash.log` and
`app/backend/skills_dev.log`.
