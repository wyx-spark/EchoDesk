# EchoDesk Enterprise Deployment and Compliance Guide

Version: v0.2.2
Publisher: Void Entropia Studio
Audience: IT administrators and compliance reviewers deploying the EchoDesk desktop app
(portable or installed) in a managed enterprise environment

Related documents: [`../PRIVACY.md`](../PRIVACY.md) (itemized network behaviour), [`../EULA.md`](../EULA.md) (license terms),
[`../SECURITY.md`](../../SECURITY.md) (security policy), [`../ENCRYPTED_DOCUMENTS.md`](../ENCRYPTED_DOCUMENTS.md) (reading encrypted documents),
[`../THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md),
[`../SBOM.json`](../SBOM.json).
中文版 / Chinese version: [`../zh-Hans/COMPLIANCE.md`](../zh-Hans/COMPLIANCE.md).

---

## 1. Scope and assumptions

1. This document covers **managed enterprise environments**: proxy, certificates, firewall, Group Policy and
   audit trails. Individual users do not need it — see [`USER_GUIDE.md`](USER_GUIDE.md) and [`FAQ.md`](FAQ.md) instead.
2. Assumptions (consistent with [`../PRIVACY.md`](../PRIVACY.md)): EchoDesk is a purely local application with
   **no server side, no telemetry and no listening ports**; only five features ever use the network, and every one
   of them can be switched off.
3. The global configuration file is `config.json` in the backend folder. A sibling `config.user.json` acts as a
   **user override layer** (its fields win, and the program never rewrites it), so enterprise policy is best
   delivered through `config.user.json`.

## 2. Proxy

### 2.1 How the program picks up a proxy

- Every outbound request is made by the **backend**, through ordinary Python HTTP clients: standard-library
  `urllib` for model downloads and update checks, `requests` for the skill marketplace, `huggingface_hub` for
  model repository queries, and a third-party search library for web search.
- The **default behaviour** of `requests` and `urllib` is, in order:
  1. read the `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` environment variables (lowercase forms included);
  2. if those are absent, read the **system proxy settings** where the platform has them: WinINET on Windows
     (Settings → Network & Internet → Proxy) and the network service proxies on macOS
     (System Settings → Network → Proxies). **On Linux there is no system proxy store on this path**, so only
     the environment variables apply — see 2.2.
- Browser automation drives the **locally installed Microsoft Edge**, whose proxy and certificate handling follow
  the system and Edge's own settings.
- **There is no in-app proxy setting (not implemented at present).** EchoDesk has no proxy option of its own; a proxy
  can only take effect through the system proxy or environment variables.
- The web-search path goes through a third-party search library whose internal HTTP implementation is **outside this
  project's control, and this document makes no guarantee about it**.

### 2.2 Forcing traffic through a proxy

| Approach | How | Notes |
| --- | --- | --- |
| A. System proxy (recommended) | Configure a manual proxy in Windows Settings or macOS System Settings, or deliver system proxy settings by Group Policy (Windows) or a configuration profile (macOS) | Covers `urllib`, `requests` and Edge at once on Windows and macOS; on Linux this path does not exist, so use B |
| B. Environment variables | Deliver `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` via Group Policy "Preferences → Windows Settings → Environment" on Windows, or through the shell profile (`~/.profile`, `~/.zshrc`) on macOS/Linux | Works on all three platforms; use it when only the Python paths should be affected, and it is the only option on Linux; environment variables take precedence over the system proxy |
| C. Launch wrapper script | A wrapper that exports the variables and then starts the program: `.cmd`/`.ps1` with `set HTTPS_PROXY=...` and `start "" "app\EchoDesk.exe"` on Windows, `export HTTPS_PROXY=...` and `./start.sh` on macOS/Linux | The portable shortcut itself takes no arguments and sets no environment variables, so use a wrapper when you need per-user or per-machine injection |

Windows PowerShell example (for interactive verification):

```powershell
$env:HTTP_PROXY  = "http://proxy.corp.example:8080"
$env:HTTPS_PROXY = "http://proxy.corp.example:8080"
$env:NO_PROXY    = "localhost,127.0.0.1,.corp.example"
& ".\app\EchoDesk.exe"
```

macOS / Linux example (same three variables, POSIX shell):

```bash
export HTTP_PROXY="http://proxy.corp.example:8080"
export HTTPS_PROXY="http://proxy.corp.example:8080"
export NO_PROXY="localhost,127.0.0.1,.corp.example"
./start.sh
```

- When the proxy needs authentication, `http://user:password@proxy.corp.example:8080` also works, but the
  **plaintext credential stays in the environment variable**; enterprise deployments should prefer the system
  proxy's credential mechanism.
- Use `NO_PROXY` to let loopback addresses and internal mirrors bypass the proxy (for example
  `localhost,127.0.0.1,.corp.example`).
- To verify: search the proxy logs for the destination domains (see section 4), or trigger a model search / update
  check on a machine without a model and watch for certificate or connection errors.

### 2.3 Cutting the network off entirely (harder than pointing at a dead proxy)

Pointing the proxy at an unreachable address is only a **soft** block (and it relies on every third-party library
honouring the proxy setting). When the requirement is "provably no egress", use firewall outbound rules and switch
the features off as well:

1. Firewall: deny outbound by default and allow only the allow-list (see section 4). Use the platform's own
   filter — Windows Defender Firewall, the macOS `pf` packet filter (or a configuration profile), or
   `nftables`/`ufw` on Linux.
2. Configuration: `/search off`, `"marketplace": {"enabled": false}`, `"update": {"enabled": false}`,
   `"automation": {"enabled": false}`.
3. Models: pre-place the `.gguf` files in the model folder and never trigger a download from the UI
   (**model download has no "disable networking" switch**, see [`../PRIVACY.md`](../PRIVACY.md) section 2.1).

## 3. Certificates

### 3.1 Which trust store each path uses

| Path | HTTP client | Default trust store | How an enterprise root CA takes effect |
| --- | --- | --- | --- |
| Model download, update check | standard-library `urllib` + `ssl` | the **Windows certificate store** (`CA`, `ROOT`) | installing the enterprise root CA in the Windows certificate store is enough |
| Skill marketplace | `requests` | the CA set bundled with **certifi** | set `REQUESTS_CA_BUNDLE` to a PEM that contains the enterprise root |
| Model repository queries | `huggingface_hub` (internally a `requests`/`httpx`-style client) | a certifi-style trust store | as above; set both `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` if needed |
| Web search | third-party search library | determined by the upstream implementation | not guaranteed; enterprises with strict trust-chain rules should disable search |
| Browser automation | the locally installed Microsoft Edge | the Windows/Edge certificate store | installing the enterprise root CA in the Windows certificate store is enough |

> Why this is stated this way (no guesswork): Python's `ssl.SSLContext.load_default_certs()` on the `win32`
> platform **loads only the Windows `CA` and `ROOT` stores**; only the non-Windows branch goes through OpenSSL's
> default paths — which is where `SSL_CERT_FILE` / `SSL_CERT_DIR` apply. So on Windows, setting only
> `SSL_CERT_FILE` is **not enough** for the standard-library paths to trust an enterprise root.
> `requests` uses the bundle shipped with `certifi` unless `REQUESTS_CA_BUNDLE` (or `CURL_CA_BUNDLE`) is set — and
> note that variable **replaces** the default root set rather than adding to it.

**On macOS and Linux** the same two layers exist, but the standard-library layer follows the non-Windows branch
above, so the sequence is:

- install the enterprise root in the **system trust store**: macOS
  `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain corp-root.crt`;
  Debian/Ubuntu `sudo cp corp-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`;
  RHEL/Fedora `sudo cp corp-root.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust`;
- **and** deliver `SSL_CERT_FILE` (a merged PEM) as well. The release ships a **bundled interpreter**, whose
  OpenSSL may look at its own default path rather than the OS store, and `SSL_CERT_FILE` is the setting that
  overrides that path on every build;
- `REQUESTS_CA_BUNDLE` is still required for the `requests`-style paths, exactly as on Windows.
  Managed-path examples: `/etc/pki/corp/corp-bundle.pem` (Linux),
  `/Library/Application Support/Corp/certs/corp-bundle.pem` (macOS),
  `C:\ProgramData\Corp\certs\corp-bundle.pem` (Windows, as in 3.2).

### 3.2 Recommended way to install an enterprise root CA (do both)

1. **Install it in the Windows certificate store**: import the enterprise root CA into
   "Local Computer → Trusted Root Certification Authorities". Edge (automation) and standard-library `urllib`
   (model download, update check) then trust it directly.
2. **Prepare a merged PEM for the `requests`-style paths**: export a `.pem` containing the enterprise root **and**
   the public roots, keep it on a managed path (`C:\ProgramData\Corp\certs\corp-bundle.pem` on Windows,
   `/etc/pki/corp/corp-bundle.pem` on Linux, `/Library/Application Support/Corp/certs/corp-bundle.pem` on macOS),
   then deliver `REQUESTS_CA_BUNDLE` with that path (add `SSL_CERT_FILE` pointing at the same
   file when needed). The merge matters because that variable replaces the default root set: enterprise roots alone
   will make certificate validation fail against public sites.
3. If the enterprise uses a **TLS-intercepting (man-in-the-middle) proxy**, both steps above must be completed,
   otherwise you will see "it connects, but certificate validation fails".

### 3.3 Common errors and what they mean

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `CERTIFICATE_VERIFY_FAILED` / `SSLCertVerificationError` | the enterprise root is not in the trust store, or the PEM in `REQUESTS_CA_BUNDLE` lacks the root that issued the site certificate | follow both steps in 3.2 |
| Model download fails while a browser opens the same address fine | that path uses standard-library `urllib` (Windows store on Windows; OpenSSL's default path elsewhere) while the browser uses its own chain | Windows: confirm the root is installed for "Local Computer", not only for the current user. macOS/Linux: deliver `SSL_CERT_FILE` pointing at the merged PEM |
| Marketplace search reports an SSL error while other features work | `requests` uses certifi | set `REQUESTS_CA_BUNDLE` |
| The proxy needs authentication and requests return 407 | no proxy credential was supplied | use the system proxy's credential mechanism, or configure per 2.2 |

## 4. Firewall and domain allow-list

### 4.1 Domains to allow, by feature

| Feature | Domains | Port | Notes |
| --- | --- | --- | --- |
| Model download (default mirror) | `hf-mirror.com` | 443 | the default source; operated by a third party, servers usually located in mainland China |
| Model download (official site) | `huggingface.co` | 443 | used when the source is switched to official; servers are outside the country |
| Model download (CDN redirects) | the upstream site's CDN/storage hosts (of the form `cdn-lfs.<site domain>`; **not fixed anywhere in this project's code — verify with a packet capture or your proxy logs**) | 443 | download URLs may redirect to the upstream site's CDN/storage hosts; the exact host names are decided upstream and this project **makes no fixed promise** about them; for strict allow-lists prefer an internal mirror |
| Web search | `duckduckgo.com` (including the `html.` and `lite.` subdomains) | 443 | used only after `/search on`; servers are outside the country |
| Skill marketplace | `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com` | 443 | search/detail reads, manifest reads, source archive download |
| Update check | `api.github.com` | 443 | reads the Releases list only; **downloads nothing and replaces no files** |
| Browser automation | **the URLs you configure yourself** | 80 / 443 | the program ships with no built-in site; allow per business need |
| Manual "view release notes" | `github.com` | 443 | only when you click the link the program shows, opened by your system browser |

### 4.2 Minimum allow-list policies

| Policy | Allow | Configuration (in `config.json` or `config.user.json`) |
| --- | --- | --- |
| **A. Model download only** (recommended for teams that must pull models) | `hf-mirror.com` (or your internal mirror) | `/search off`; `"search_default": false`; `"marketplace": {"enabled": false}`; `"update": {"enabled": false}`; `"automation": {"enabled": false}` |
| **B. Fully offline** | nothing | same as A, plus deny outbound by default in the firewall; supply models as pre-placed `.gguf` files |
| **C. Marketplace allowed, search and automation forbidden** | `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com`, the model download domains | `/search off`; `"automation": {"enabled": false}`; `"marketplace": {"enabled": true}` |
| **D. Internal mirror and intranet sites only** | the internal mirror plus your intranet business sites | see 4.3; also switch search, marketplace and update check off |

Configuration example (`config.user.json`, used as the override layer that the program never rewrites):

```json
{
    "search_default": false,
    "marketplace": { "enabled": false },
    "update": { "enabled": false },
    "automation": { "enabled": false }
}
```

### 4.3 Caveats when only an internal mirror is allowed

- The `hf_endpoint` field in `config.json` takes effect as the `HF_ENDPOINT` environment variable for the
  `huggingface_hub`-based model download and repository query paths, and may point at your internal mirror
  (for example `"hf_endpoint": "https://hf.corp.example"`).
- **Limitation (stated plainly)**: the "Model Center" browsing/search endpoints in the settings window are fixed to
  the two addresses `hf-mirror.com` and `huggingface.co` (`model_hub.source` only accepts `auto` / `mirror` /
  `official`) and **do not read `hf_endpoint`**. In a network with no internet access the Model Center page will be
  unusable (it reports that the data source is unreachable), but placing `.gguf` files into the model folder by hand
  leaves local inference and chat completely unaffected.

## 5. Group Policy and restricted environments

### 5.1 Behavioural boundaries of the program

| Item | Current behaviour |
| --- | --- |
| Registry writes | **None.** The only registry access is a **read-only** lookup of `AppsUseLightTheme` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` (used for "follow the system theme") |
| System services | **Installs none** |
| Listening ports | **None.** Frontend and backend talk over standard input/output (line-delimited JSON over stdio), so there is no inbound firewall prompt |
| Scheduled tasks | **Creates none.** Automation's "run at these times" is scheduled by the program while it is running; closing the program stops it |
| Start at sign-in | The `automation.start_with_windows` setting is **not implemented at present** (changing it has no effect). If you need start-at-sign-in, copy the outer shortcut into the Startup folder (`Win+R` → `shell:startup`); that is a per-user Windows startup entry and EchoDesk itself writes no registry keys |
| Self-update | **Downloads nothing and replaces no files**; it checks and notifies only |

Two rows above are Windows-specific in their **mechanism** only; the equivalents on the other platforms are:

- **Start at sign-in**: Windows — copy the outer shortcut into the Startup folder (`Win+R` → `shell:startup`);
  macOS — System Settings → General → Login Items; Linux — an XDG autostart entry
  (`~/.config/autostart/echodesk.desktop`, based on the desktop entry the project generates into
  `deploy/linux/echodesk.desktop`, with `Exec=` pointing at your unpacked `start.sh`).
- **Registry / configuration**: macOS and Linux have no registry. A policy is delivered as files instead —
  a pre-created `config.user.json`, the settings in 5.4, and the `ECHODESK_DATA` / `ECHODESK_LANG_DIR`
  environment variables, set through the platform's profile mechanism.

### 5.2 Directory writability (important)

The portable release uses this three-level layout by default:

```text
EchoDesk-v0.2.2\
├── install-complete.bat         ← post-extraction step (creates the shortcut below)
├── Start-EchoDesk-v0.2.2.lnk          ← launcher shortcut, created on YOUR machine by the above
│                                  (working directory = user data directory)
├── ui_state.json                ← UI state
├── snippets.json                ← snippets
├── results\                 ← automation output (batch results; user data directory)
└── app\                         ← the program itself
    ├── EchoDesk.exe
    └── backend\
        ├── EchoDeskBackend.exe
        ├── config.json          ← main configuration (the program writes it back)
        ├── session.json         ← chat history
        ├── models\              ← local models
        ├── browser_state.json   ← automation login state
        ├── automation_log.jsonl ← audit log
        ├── market_cache.json    ← marketplace cache
        └── skills\              ← installed skills
```

- The **user data directory** (the outer level) is chosen by the `ECHODESK_DATA` environment variable; when it is
  unset, the rule is "if the program folder is named `app`, take its parent, otherwise take the program folder".
- **Backend artifacts that use relative paths land in the backend working directory** (in the portable release that
  is `app\backend\`) and are **not** affected by `ECHODESK_DATA`: chat history, models, the audit log, login state,
  the marketplace cache and installed skills all live there.
- Therefore: **in a default deployment `app\` must be writable** (`config.json` is written back too). If policy
  requires the program folder to be read-only, move the artifact paths out per the table in 5.4; the simpler option is
  to copy the whole release folder to a writable managed location (for example `%LOCALAPPDATA%` or a managed drive)
  and run it from there.
- **The installed build behaves the same way**: the installer places the whole release folder into the directory you
  choose, and by default that is **the folder the Setup program itself sits in** — `DefaultDirName` comes from
  `ExtractFileDir({srcexe})` with `AppendDefaultDirName=no` and `UsePreviousAppDir=no`, and the wizard runs with
  `PrivilegesRequired=lowest`. The shortcuts use that directory as their working directory, so the layout matches the
  portable one. (An earlier revision of this document claimed the default was `{autopf}\EchoDesk` /
  `%LOCALAPPDATA%\Programs\EchoDesk`; that is **not** what `tools/installer.iss` does — corrected in v0.2.2.)
  Because the default location is wherever the user unpacked the Setup program, it is writable by that user, which is
  why the default deployment works without moving anything. If an administrator redirects the install to
  `C:\Program Files\EchoDesk` instead, standard users cannot write there, and that deployment **must** move the
  artifact paths out per 5.4 — otherwise session, model and audit-log writes fail.

**Linux and macOS bundles** use the same three-level layout with POSIX names, and create no shortcut at all:

```text
EchoDesk-v0.2.2/
├── start.sh                     ← top-level launcher (hands over to app/EchoDesk)
├── SIGNING.txt                  ← what macOS signing/notarization still needs
├── docs/  README*.md  LICENSE*
└── app/
    ├── EchoDesk
    └── backend/                 ← config.json, session.json, models/, audit log, skills/
```

- The outer level is **not** the user data directory on macOS/Linux unless `ECHODESK_DATA` points there: the
  default per-platform directory is `~/Library/Application Support/EchoDesk` on macOS and
  `$XDG_DATA_HOME/EchoDesk` (or `~/.local/share/EchoDesk`) on Linux, as in the resolution order of
  [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) section 5.1.
- The writability rule is unchanged: `app/backend/` holds every relative-path backend artifact, so it must be
  writable, or those paths must be moved per 5.4. Unpacking a `.tar.gz` into your home directory or a managed
  drive therefore needs no extra step — unlike a system-wide install under `/opt`.

### 5.3 Using `ECHODESK_DATA` to put user data on a managed drive

Windows (PowerShell, per user or per machine — the Group Policy "Environment" preference):

```powershell
[Environment]::SetEnvironmentVariable("ECHODESK_DATA", "D:\CorpData\EchoDesk", "User")
```

macOS / Linux (POSIX shell; the program reads the variable from its environment, so a profile entry is the
managed delivery mechanism):

```bash
export ECHODESK_DATA="$HOME/CorpData/EchoDesk"   # persist by adding this line to ~/.profile or ~/.zshrc
./start.sh
```

- What it affects: the frontend (`ui_state.json`, `snippets.json`) and anything the backend derives from
  `user_data_root()` — chiefly the **default root folder for batch results**. It does **not** move the artifacts
  under `app/backend/` (see 5.2).
- If the location pointed at by `ECHODESK_DATA` is not writable: the frontend ignores the variable and falls back to
  automatic detection; the backend adopts the path as given, and subsequent write failures surface as errors
  (it does not silently write somewhere else).
- Use it together with the table in 5.4: `ECHODESK_DATA` governs the "user data directory", and the settings in that
  table govern the "backend artifacts".

### 5.4 Settings to change for a read-only deployment

Set the following to **absolute paths** to move every runtime write out of `app/backend/` —
`C:\Corp\EchoDesk\...` on Windows, `/var/lib/echodesk/...` on macOS/Linux. Every one of them accepts forward
slashes on all three platforms and `~` is expanded to the home directory, so the same JSON can be delivered
everywhere:

| Setting | Default location | Suggested target |
| --- | --- | --- |
| `session_file` | `app/backend/session.json` | a managed per-user folder; set it to an empty string to **disable session persistence** |
| `model_hub.models_dir` | `app/backend/models` | the managed model library |
| `automation.audit_log_file` | `app/backend/automation_log.jsonl` | the managed audit folder (easier central collection) |
| `automation.login_state_file` | `app/backend/browser_state.json` | a writable per-user folder (contains cookies; handle as a credential) |
| `automation.processed_file` | `app/backend/automation_processed.json` | the managed drive |
| `automation.download_dir` | the system temp directory + `/echodesk_downloads` (`%TEMP%` on Windows, `/tmp` on macOS/Linux) | a managed temp folder |
| `automation.results_location` | the user data directory | the managed results folder |
| `marketplace.cache_file` | `app/backend/market_cache.json` | the managed cache folder |
| `marketplace.install_dir` | `app/backend/skills` | the managed skills folder |
| `marketplace.dev_dir` / `marketplace.local_manifest` | `app/backend/skills_dev` / `skills_local.json` | the managed drive |

> `config.json` itself must still be writable (the program writes it back when the search toggle or other settings
> change). If it truly has to be read-only, run the program with `config.json` on a writable path, or accept that
> setting changes will not be persisted.

### 5.5 Rebuilding a broken portable shortcut

**Windows only.** This section is about the portable Windows layout: the `.lnk` shortcut and
`install-complete.bat` exist there and nowhere else. On Linux and macOS the bundle has no shortcut to repair —
start it with `./start.sh`. For a desktop icon, base one on `deploy/linux/echodesk.desktop` (regenerate with
`python tools/gen_platform_icons.py`) and fix `Exec=` to the unpacked `start.sh`, or drag `app/EchoDesk` to the
Dock on macOS.

1. Launch the program by double-clicking `app\EchoDesk.exe` directly (this always works and does not depend on the
   shortcut).
2. At startup the program checks the outer shortcut:
   - shortcut **missing** → it asks "create `Start-EchoDesk-v<version>.lnk`?"; answer yes to create it;
   - shortcut **broken** (target lost or working directory wrong after the folder was moved or renamed) → it asks
     whether to recreate it; answer yes to rebuild.
   The rebuilt shortcut has target = the current `EchoDesk.exe` and **working directory = the user data directory**
   (this is what keeps batch results at the outer level).
3. If you previously answered "no" (skip creation), the program stops asking: delete `ui_state.json` in the user data
   directory to reset that flag (the file also stores UI state, so deleting it only resets UI preferences).
4. You can also create the shortcut by hand: target `<release folder>\app\EchoDesk.exe`,
   **"Start in" = the user data directory (the outer level)**, icon optionally `app\EchoDesk.ico`.

## 6. Auditing and retained evidence

### 6.1 The results file `result.txt` (results.txt)

- Location: `<automation.results_location>\<automation.results_root_name>\YYYY-MM-DD\<task name>\result.txt`
  (by default "user data directory\results\date\task name\", with `-1`, `-2` suffixes for same-named runs on
  the same day). Path separators follow the platform: `\` on Windows, `/` on macOS/Linux, where the same file is
  `<automation.results_location>/<automation.results_root_name>/YYYY-MM-DD/<task name>/result.txt`.
- Contents: **plain text, with no Markdown markup**. The header lists the task name, run date, start time, task list
  URL, task instruction and the results folder; the body holds one result block per task (review opinions and so on),
  and failures record the reason plus the file name of the failure screenshot.
- Write behaviour: the whole file is **rewritten after each task completes**, so stopping or crashing mid-run never
  loses results that were already produced.
- The same folder also keeps the **originals** downloaded for that task and any failure screenshots
  (when `automation.save_results` is true).
- Purpose: to review, after the fact, which links a batch processed, what conclusion each produced and which failed.

### 6.2 The audit log `automation_log.jsonl`

- Location: the path in `automation.audit_log_file`, by default `automation_log.jsonl` in the backend working
  directory (in the portable release, `app/backend/automation_log.jsonl` on every platform). Enterprises should
  point it at a central audit folder on a managed drive.
- Format: JSON Lines, one record per line, with these fixed fields:
  `time`, `event`, `url` (task link, truncated to 300 characters), `files` (**file names only**), `result`,
  `error` (truncated to 200 characters), `safety_version` (automation safety-agreement version) and
  `safety_accepted_at` (when consent was given), plus a few scalar counters.
- **Redaction**: document text, model output text, cookies, tokens and form values are never written.
- Purpose: to review who consented to which version of the automation safety agreement and when, which links were
  processed, which files were downloaded and how each turned out.

### 6.3 Other local traces useful for review

| File | Location | Contents | Sensitivity |
| --- | --- | --- | --- |
| `automation_processed.json` | backend working directory | hashes of already-processed task URLs (deduplication record) | low |
| `browser_state.json` | backend working directory | cookies / localStorage login state of the automation browser | **high; treat as a credential** |
| `market_cache.json` | backend working directory | snapshot of the skill marketplace listing | low |
| `backend_crash.log` | backend folder | Python/native exception stack traces | medium (may contain paths) |
| `frontend_crash.log` | program folder | frontend exception stack traces (WPF on Windows, Avalonia elsewhere) | medium (may contain paths) |
| `session.json` | backend working directory | chat history | **high; contains business content** |

"Backend working directory" is `app/backend/` in a release on all three platforms (and `backend/` in a source
check-out); "program folder" is `app/`. Note that the automation artefacts (`automation_processed.json`,
`browser_state.json`) only appear on Windows, because automation itself is Windows-only.

### 6.4 Retention and cleanup

- Apart from the temporary staging folders, the program **never deletes** these files automatically; the retention
  policy is yours to define.
- When audit evidence must be retained, **archive** `automation_log.jsonl` and the `results/` folder before
  cleaning anything up.
- Suggested cleanup: delete historical date folders under `results/` once the retention period expires; delete
  `browser_state.json` and `session.json` when a machine is handed over or retired.

## 7. Compliance notes on encrypted document reading

Some organisations protect Office documents with a document-encryption system (for example Cobra
DocGuard). This section is the compliance summary; the full behaviour statement — prerequisites,
responsibility, what is stored where — is
[`../ENCRYPTED_DOCUMENTS.md`](../ENCRYPTED_DOCUMENTS.md) (Chinese:
[`../ENCRYPTED_DOCUMENTS.zh-Hans.md`](../ENCRYPTED_DOCUMENTS.zh-Hans.md)).

> **Windows only.** The path described here goes through the local WPS/Office COM interface, which does not
> exist on macOS or Linux. On those platforms a protected file cannot be opened at all: the program reports the
> capability as Windows-only rather than as a parse failure, and the same is true of legacy binary `.doc`/`.xls`.
> See [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) section 3.1.

- **EchoDesk does not touch the encryption driver, does not modify encryption policy and holds no
  keys.** It does not install, register, patch or unload the encryption client, and it reads no key
  or licence material belonging to it.
- **It only calls WPS.** Whether a protected file can be opened is decided entirely by the encryption
  client already installed on the machine, together with the access rights your organisation has
  granted the current Windows account. From the encryption system's point of view the requesting
  application is the same authorised office application an employee would use by hand.
- **The encryption system's audit log records the file access by WPS**, making no distinction between
  a manual open and a read initiated by EchoDesk. Your existing review, approval and traceability
  processes therefore continue to apply unchanged — there is nothing extra to monitor and nothing
  extra that goes unrecorded.
- **EchoDesk does not change your existing data-loss-prevention boundary.** It neither widens nor
  narrows what may be opened: it introduces no privileged route, no separate credential and no hidden
  interface of its own.
- **On a machine without the enterprise encryption client**, a protected file cannot be decrypted:
  what EchoDesk reads is ciphertext, the generic parser cannot interpret it, and the program reports
  a parsing failure. The file remains ciphertext on that machine and cannot be opened there.
- **Plaintext read through this path stays in memory.** It is not written to disk, not written to
  logs (logs record file names only) and not sent to any network location; it is released when the
  program exits.
- **Deployment control.** If your policy forbids this path, do not install WPS on the machine:
  EchoDesk then has no in-memory decryption route at all, falls back to the generic parser, and
  protected files fail to parse. That is the intended behaviour, and it can be enforced with the same
  software-distribution rules you already use.

## 8. One-page quick reference

| Requirement | How to do it |
| --- | --- |
| All traffic through the corporate proxy | configure the system proxy (approach A) on Windows/macOS, or deliver `HTTP_PROXY`/`HTTPS_PROXY` (approach B — the only option on Linux) |
| Trust the enterprise root CA | Windows: install it in the Windows certificate store **and** deliver `REQUESTS_CA_BUNDLE` (merged PEM). macOS/Linux: install it in the system trust store **and** deliver `SSL_CERT_FILE` + `REQUESTS_CA_BUNDLE` |
| Allow model download only | firewall allows only the model download domains + turn search/marketplace/update/automation off (policy A) |
| Fully offline | firewall denies outbound by default + pre-placed offline models + all networked features off (policy B) |
| Put user data on a managed drive | `ECHODESK_DATA` plus the absolute-path settings in the 5.4 table |
| Read-only program folder | copy the release to a writable location and run it there; or move artifacts out per 5.4 (`config.json` must stay writable) |
| Post-incident review | `automation_log.jsonl` (who/when/what ran) + `results/.../result.txt` (what was done and how it turned out) |
| Shortcut lost (Windows portable layout) | run `app\EchoDesk.exe` directly and accept the rebuild prompt (delete `ui_state.json` first if it stopped asking) |
| Linux / macOS bundle | unpack the `.tar.gz` and run `./start.sh`; user data goes to `~/Library/Application Support/EchoDesk` (macOS) or `$XDG_DATA_HOME/EchoDesk` / `~/.local/share/EchoDesk` (Linux), or wherever `ECHODESK_DATA` points |
| Read a document protected by the enterprise encryption system | install WPS plus the matching encryption client and grant the account access rights; the read then goes through WPS exactly like a manual open — see [`../ENCRYPTED_DOCUMENTS.md`](../ENCRYPTED_DOCUMENTS.md) (Windows only) |
| Forbid that path entirely | do not install WPS on the machine; protected files then fail to parse (intended) |
