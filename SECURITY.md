# Security Policy

> **Chinese version:** the same policy, translated section for section, is available at
> [`SECURITY.zh-Hans.md`](SECURITY.zh-Hans.md). The two documents must stay consistent; if they ever
> disagree, this English version prevails — please open an issue.

EchoDesk is a fully local desktop application: it runs inference, file parsing and browser
automation on the user's own machine, and it has no server component, no telemetry and no listening
port. That shapes both the threat model and the way we want vulnerabilities reported.

**Please do not open a public issue for a security problem.** Report it privately so a fix can be
prepared before the details are public.

## Related documents

- [`docs/PRIVACY.md`](docs/PRIVACY.md) — networked behaviour and data handling.
- [`docs/EULA.md`](docs/EULA.md) — licence terms.
- [`docs/THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md) — third-party components and their
  licences.
- [`docs/SBOM.json`](docs/SBOM.json) — software bill of materials.

## Supported versions

| Version | Supported |
| --- | --- |
| **v0.2.3** — current release | **Yes** — security fixes are made here |
| 0.2.x earlier than v0.2.3 (including v0.2.0) | Upgrade recommended — fixes land only in the latest release of the line |
| 0.1.x | No — please upgrade to the latest 0.2.x release |
| Anything older, a locally modified build, or a third-party repackaged build | No |

Only the latest release of the supported line receives fixes. If you report an issue against an
older build, expect the first question to be whether it still reproduces on the current release.

## How to report a vulnerability

1. **Preferred: a private GitHub Security Advisory.** Open the repository's
   [Security → Advisories](https://github.com/wyx-spark/EchoDesk/security/advisories/new)
   page and use "Report a vulnerability". The report, the discussion and any patch stay private
   until an advisory is published.
2. **Fallback: email.** If you cannot use GitHub advisories, write to
   `Void-Entropia-Studio@users.noreply.github.com`. That is the project's GitHub noreply address and
   the only email address we publish; it reaches the maintainers' GitHub notifications.

### Encryption

- Reporting through a private GitHub Security Advisory runs entirely over **HTTPS/TLS** and is
  visible only to the maintainers — this is the **recommended encrypted channel** today.
- **No PGP/GPG key is published yet.** The repository contains no PGP public key, no `security.txt`
  and no `.well-known/` directory, so this policy does not list a key fingerprint or a fingerprint
  link (publishing a fingerprint that does not exist would be worse than publishing none). If you
  need to send encrypted material, say so in the private advisory and we will first provide a
  verifiable key or agree on another channel both sides accept.
- The email fallback (`Void-Entropia-Studio@users.noreply.github.com`) is plain SMTP and is **not**
  guaranteed to be end-to-end encrypted; please do not put full exploit code or real confidential
  data in the first message.

Please do not include exploit details in public issues, discussions or pull requests. If you have
already disclosed something publicly by accident, tell us anyway; we will still work on a fix.

## What to include

A report that lets us reproduce the problem is worth ten that only describe it. Please include:

- **Version**: the version reported by `/status` or the release folder name (for example
  `v0.2.1`), and whether you run the portable release or a build from source.
- **Operating system**: Windows edition, version and build, plus whether WPS Office /
  Microsoft Office and Microsoft Edge are installed.
- **Steps to reproduce**: precise, minimal, in order. For automation issues, include the relevant
  configuration (URL pattern, selectors, file extensions) with confidential values redacted.
- **Impact**: what an attacker gains — local code execution, reading files outside the intended
  scope, network access, credential exposure, and so on.
- **Logs**: both crash logs exist and are appended to when something fails:
  - `frontend_crash.log` — written by the WPF frontend, in the **program folder**
    (next to `EchoDesk.exe`).
  - `backend_crash.log` — written by the Python backend, inside the **`backend\`** folder
    (next to `EchoDeskBackend.exe`; in the portable release that folder is `app\backend\`).
  - `backend\automation_log.jsonl` — the automation audit log (in the backend working directory by
    default), useful for automation reports. It is redacted by design (file names only, no document
    text, tokens or cookies), so review it before attaching it.
  If relevant, also include the content of `backend\config.json` with personal paths and any
  token removed.

Do not attach real confidential documents. A minimal synthetic file that triggers the same code
path is more useful.

## Response-time commitment

| Stage | Commitment |
| --- | --- |
| Acknowledgement of your report | Within **3 business days** |
| Initial triage: severity assessment, affected versions, whether we can reproduce it | Within **10 business days** of acknowledgement |
| Fix released, or an explicit mitigation communicated | Within **30 days** of acknowledgement; if that is genuinely not possible, we will give you a concrete fix timeline and an interim mitigation within those 30 days |
| Credit in the advisory | Offered by default, unless you prefer to stay anonymous |

This is a small project maintained in spare time, so these are commitments about *communication*,
not guaranteed fix dates. If you have not heard from us within the windows above, please send a
follow-up — mail filters and holidays happen.

## Scope notes

Because EchoDesk is local-only, the classic server-side issues (authentication bypass, remote
request forgery against our API, data at rest on our servers) do not apply. The areas we consider
highest risk are:

- **Skill execution.** Skills run with the user's own privileges. A skill that can escape the
  intended permission model, read files it never declared, silently reach the network, or bypass
  the manifest verification and environment-sanitization rules is a valid and important report.
- **The automation browser profile.** Automation drives the locally installed Microsoft Edge and
  stores its session in `backend\browser_state.json`. Reports about leaked cookies or credentials,
  automation reaching sites or files the user did not configure, downloaded files surviving after
  a task, or the audit log recording sensitive data are in scope. (EchoDesk never uploads run
  data itself.)
- **The model download path.** Downloads come from the configured Hugging Face endpoint
  (`hf-mirror.com` by default, changeable to the official site or an internal mirror). Reports
  about missing or bypassable integrity verification of downloaded files, or a malicious
  repository/model file leading to code execution or path traversal, are in scope. Note that the
  endpoint is user-configurable, so "the mirror served the wrong file" is a configuration and
  trust question, while "EchoDesk wrote a file outside its own directories" is a vulnerability.

## Out of scope

The following are not security vulnerabilities in this project; please do not file them as security
reports (some of them belong in a regular issue):

1. **Reports that require physical access** — anything that needs the machine already logged in, a
   device plugged in, or the local disk to be read.
2. **Unsupported versions** (0.1.x and older), and builds that have been modified, repackaged or
   redistributed by third parties.
3. **Vulnerabilities in third-party dependencies themselves** — please **report those upstream**
   (for example llama.cpp, PyInstaller, Playwright, `requests`, `pypdf`, the Python standard library
   and so on). You are welcome to copy us in; we will upgrade the dependency in the next release, but
   the fix and its timeline belong upstream. The current dependency list is in
   [`docs/THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md) and
   [`docs/SBOM.json`](docs/SBOM.json).
4. **Malware already running on the machine**, and vulnerabilities in Microsoft Edge, WPS Office,
   Microsoft Office or the operating system itself (report those to the relevant vendor).
5. **The accuracy or safety of model output** — that is a quality problem and belongs in a regular
   issue.
6. **Social engineering** — for example "the user was tricked into manually downloading a malicious
   model file and putting it in `models\`", which depends entirely on the user acting deliberately.
7. **Third-party site behaviour** — a mirror or the official site returning a different model file
   than you expected is a configuration and trust question (the download source is configurable and
   you can verify the hash yourself); "EchoDesk wrote a file outside its own directories" is a
   vulnerability.
8. **The absence of in-app hardening features by itself** (for example "there is no plugin
   sandbox"): if it is only a known limitation, please raise it as a feature request; if it leads to
   privilege escalation, out-of-bounds reads or writes, or data exfiltration, then it **is** a
   vulnerability and should be reported.

## Security design notes (current state and known limitations)

This section describes the security boundary EchoDesk actually implements today, **including what is
not implemented**, so that reporters can tell which behaviour is by design.

### Process and network boundary

- **No listening port**: the frontend and the backend talk over standard input/output (line-delimited
  JSON), so there is no local HTTP service, no authentication surface and no firewall inbound prompt.
- **No registry writes, no service**: the portable release is uninstalled by deleting its folder.
  The only registry access is a **read-only** read of `AppsUseLightTheme` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize`, used for "follow the system
  theme"; nothing is written and no keys are created.
- **Only the backend reaches the network**: the frontend (.NET WPF) makes no network requests of its
  own. The five features that do reach the network, and the domains they use, are listed in section 2
  of [`docs/PRIVACY.md`](docs/PRIVACY.md).

### Skills (plugins)

- **They run with your privileges**: skill code is loaded and executed by the backend process
  **in-process**. It has the same privileges as the EchoDesk backend — that is, your Windows account
  — and can read and write files you can access and make network requests.
- **First load needs user confirmation**: when a local plugin is first discovered, the UI shows a
  **batch confirmation dialog** listing the capabilities the plugin declares (commands, UI panels,
  hooks and so on) for you to review; your choices are recorded in a local plugin manifest
  (trusted / denied) so that you are not asked again. The example plugins shipped with the package
  are enabled by default and do not prompt; an author who **overwrites an example plugin under the
  same name** cannot use that to skip confirmation.
- **Secret sanitisation**: when a subprocess related to a third-party skill is invoked or prepared, a
  copy of the environment has every variable whose name contains `KEY`, `SECRET`, `TOKEN`,
  `PASSWORD`, `PASSWD` or `CREDENTIAL` **temporarily removed**, so your API keys, cloud credentials
  and access tokens are not handed to third-party code.
- **`prepare` scripts are not run automatically**: a skill's declared `prepare` script is **not**
  executed automatically; the current implementation only reports that it exists and that it would
  run with the sanitised environment.
- **Fixed install source**: marketplace installation downloads a source tarball from
  `codeload.github.com` only (50 MB per package, counted while downloading, sha256 recorded) and does
  **not** run npm / pip network installs.
- **Known limitations (deliberately unimplemented, disclosed here as attack surface)**:
  - **No process-level sandbox**: skills run in the same process as the main program, so the
    permission declaration is **advance notice and review material**, not enforced runtime
    isolation; there are also no CPU, memory or execution-time limits.
  - "Only run skills you trust" is therefore the only effective defence today; a skill that steps
    outside its intended scope — escaping the intended range, reading files it never declared,
    silently reaching the network, or bypassing manifest verification or secret sanitisation — is a
    **valid and important** vulnerability report.

### The automation browser

- Automation drives the **locally installed Microsoft Edge** (headed, so you can watch it at any
  time) and stores its session in `browser_state.json` in the backend folder.
- Enabling it for the first time requires ticking the **safety agreement** (multilingual text, key
  `settings.automation.safetyBody`); until you agree, the configuration layer forces the master
  switch off, and the agreement version and the time of consent are written to the audit log.
- Target URLs are restricted to the `http`, `https` and `file` schemes and **must be configured
  explicitly by the user**; the program ships with no built-in sites.
- Downloaded files land in the system temporary directory first and are cleaned up when the task
  finishes; retained originals and `result.txt` go under `results\` in the user data directory (for
  auditing).
- Reports about automation reaching sites or files the user did not configure, downloaded files
  surviving after a task, the audit log recording sensitive data, or leaked cookies or credentials
  are all in scope. EchoDesk never uploads run data itself.

### Model downloads

- The download source is configurable: the third-party mirror `hf-mirror.com` by default, switchable
  to the official `huggingface.co`.
- Integrity verification is optional: after a download completes the file size is compared; when a
  sha256 is available and the file is no more than 2 GB, the hash is compared as well
  (`model_hub.verify_sha256`, on by default).
- In scope: integrity verification being missing or bypassable, a malicious repository or model file
  leading to code execution or path traversal, and **files being written outside EchoDesk's own
  directories**.

### Other areas still in scope

Also in scope: local privilege issues, path traversal in file parsing or skill installation,
memory-safety problems in the bundled native inference library (llama.cpp — those may be better
reported upstream, but tell us as well), and any code path that sends user content off the machine
without the user enabling a feature that is documented as networked.

## Disclosure

We prefer coordinated disclosure. After a fix is available we will publish a GitHub Security
Advisory describing the issue, the affected versions and the mitigation, and we will credit the
reporter unless they ask not to be named. Please give us a reasonable window — at least until the
fix is released — before publishing details yourself.

For non-security problems, use the issue templates in [`.github/`](.github/) instead, and include
the same environment and log information described above.
