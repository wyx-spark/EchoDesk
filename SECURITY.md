# Security Policy

EchoDesk is a fully local desktop application: it runs inference, file parsing and browser
automation on the user's own machine, and it has no server component and no telemetry. That
shapes both the threat model and the way we want vulnerabilities reported.

**Please do not open a public issue for a security problem.** Report it privately so a fix can be
prepared before the details are public.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.2.x (current line, including v0.2.0 and v0.2.1) | Yes — security fixes are made here |
| 0.1.x | No — please upgrade to the latest 0.2.x release |
| Anything older, or a locally modified build | No |

Only the latest release of the supported line receives fixes. If you report an issue against an
older build, expect the first question to be whether it still reproduces on the current release.

## How to report a vulnerability

1. **Preferred: a private GitHub Security Advisory.** Open the repository's
   [Security → Advisories](https://github.com/VoidEntropiaStudio/EchoDesk/security/advisories/new)
   page and use "Report a vulnerability". The report, the discussion and any patch stay private
   until an advisory is published.
2. **Fallback: email.** If you cannot use GitHub advisories, write to
   `security@example.com` — **maintainer note: this address is a placeholder; replace it with a
   monitored mailbox before publishing the repository.**

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
    (next to `EchoDeskBackend.exe`).
  - `backend\automation_log.jsonl` — the automation audit log, useful for automation reports.
    It is redacted by design (file names only, no document text, tokens or cookies), so review it
    before attaching it.
  If relevant, also include the content of `backend\config.json` with personal paths and any
  token removed.

Do not attach real confidential documents. A minimal synthetic file that triggers the same code
path is more useful.

## Response-time commitment

| Stage | Commitment |
| --- | --- |
| Acknowledgement of your report | Within **5 business days** |
| Initial triage: severity assessment, affected versions, whether we can reproduce it | Within **10 business days** of acknowledgement |
| Fix or mitigation plan communicated | Depends on severity; we will tell you the expected timeline after triage |
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

Also in scope: local privilege issues, path traversal in file parsing or skill installation,
memory-safety problems in the bundled native inference library (llama.cpp — those may be better
reported upstream, but tell us as well), and any code path that sends user content off the machine
without the user enabling a feature that is documented as networked.

Out of scope: findings that require physical access to an already-compromised machine, malware
already running with the user's privileges, vulnerabilities in Microsoft Edge, WPS Office,
Microsoft Office or the operating system itself (report those to their vendors), and the accuracy
or safety of model output — that is a quality problem, and belongs in a regular issue.

## Disclosure

We prefer coordinated disclosure. After a fix is available we will publish a GitHub Security
Advisory describing the issue, the affected versions and the mitigation, and we will credit the
reporter unless they ask not to be named. Please give us a reasonable window — at least until the
fix is released — before publishing details yourself.

For non-security problems, use the issue templates in [`.github/`](.github/) instead, and include
the same environment and log information described above.
