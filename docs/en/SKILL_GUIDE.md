# EchoDesk Skill Development Guide (v0.2.0)

This guide explains how to write, submit and publish an EchoDesk skill (plugin), together with the review process and the publishing rules.
Chinese version: [`docs/zh-Hans/SKILL_GUIDE.md`](../zh-Hans/SKILL_GUIDE.md).
To submit a skill, use the repository's "Skill submission" issue template.

---

## 1. What a Skill Is

A skill is a self-contained local extension package: one directory + a `skill.json` manifest + entry code.
EchoDesk discovers skills through a GitHub topic, and once the user confirms, installs the skill into
`backend\skills\<技能id>\` and registers it in `backend\skills_registry.json`.

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

  "dependencies": {                        // dependency declarations (only reported at install time, never installed automatically)
    "pip": ["python-docx>=1.0"],
    "npm": []
  },

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

```powershell
# 1) 把技能源码放进后端技能目录（模拟已安装）
#    <程序目录>\backend\skills\<技能id>\
# 2) 手工登记到注册表（或直接用市场安装流程装一次）
#    <程序目录>\backend\skills_registry.json

# 3) 检查清单是否合法（等价于市场的验证逻辑）
python -c "import json;d=json.load(open('skill.json',encoding='utf-8'));print('bundle =',d.get('echodesk',{}).get('bundle'))"

# 4) 检查入口是否可加载
python -c "import importlib.util,pathlib;p=pathlib.Path('echodesk_skill/__init__.py');print('存在' if p.exists() else '缺失')"
```

Registry fields for installation and uninstallation:

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
A: Declare them in `dependencies.pip`. For safety reasons EchoDesk only notifies the user at install
time and never installs anything automatically, so document in your README the commands the user needs
to run.

**Q: Which capabilities can a skill hook into?**
A: `capabilities` can currently declare commands, UI panels, automation hooks and file-parser
extensions. For how these extension points are wired up, see the "Extension Points" section of
`docs/DEVELOPER.md`;
what v0.2.0 fully implements is **discovery / verification / installation / uninstallation / registry**,
while the runtime loader (which actually hooks skill commands into the `/` command table) has its
interface defined and will be wired up in a later release.
