# EchoDesk Enterprise Deployment and Compliance Guide

Version: v0.2.1
Publisher: Void Entropia Studio
Audience: IT administrators and compliance reviewers deploying the EchoDesk desktop app
(portable or installed) in a managed enterprise environment

Related documents: [`../PRIVACY.md`](../PRIVACY.md) (itemized network behaviour), [`../EULA.md`](../EULA.md) (license terms),
[`../SECURITY.md`](../SECURITY.md) (security policy), [`../THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md),
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
  2. if those are absent on Windows, read the **system proxy settings** (WinINET, i.e. Settings → Network & Internet → Proxy).
- Browser automation drives the **locally installed Microsoft Edge**, whose proxy and certificate handling follow
  the system and Edge's own settings.
- **There is no in-app proxy setting (not implemented at present).** EchoDesk has no proxy option of its own; a proxy
  can only take effect through the system proxy or environment variables.
- The web-search path goes through a third-party search library whose internal HTTP implementation is **outside this
  project's control, and this document makes no guarantee about it**.

### 2.2 Forcing traffic through a proxy

| Approach | How | Notes |
| --- | --- | --- |
| A. System proxy (recommended) | Configure a manual proxy in Windows Settings, or deliver system proxy settings by Group Policy | Covers `urllib`, `requests` and Edge at once; the least effort |
| B. Environment variables | Deliver `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` via Group Policy "Preferences → Windows Settings → Environment" | Use this when only the Python paths should be affected; environment variables take precedence over the system proxy |
| C. Launch wrapper script | `set HTTPS_PROXY=...` in a `.cmd`/`.ps1` and then `start "" "app\EchoDesk.exe"` | The portable shortcut itself takes no arguments and sets no environment variables, so use a wrapper when you need per-user or per-machine injection |

PowerShell example (for interactive verification):

```powershell
$env:HTTP_PROXY  = "http://proxy.corp.example:8080"
$env:HTTPS_PROXY = "http://proxy.corp.example:8080"
$env:NO_PROXY    = "localhost,127.0.0.1,.corp.example"
& ".\app\EchoDesk.exe"
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

1. Firewall: deny outbound by default and allow only the allow-list (see section 4).
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

### 3.2 Recommended way to install an enterprise root CA (do both)

1. **Install it in the Windows certificate store**: import the enterprise root CA into
   "Local Computer → Trusted Root Certification Authorities". Edge (automation) and standard-library `urllib`
   (model download, update check) then trust it directly.
2. **Prepare a merged PEM for the `requests`-style paths**: export a `.pem` containing the enterprise root **and**
   the public roots, keep it on a managed path (for example `C:\ProgramData\Corp\certs\corp-bundle.pem`), then
   deliver `REQUESTS_CA_BUNDLE=C:\ProgramData\Corp\certs\corp-bundle.pem` (add `SSL_CERT_FILE` pointing at the same
   file when needed). The merge matters because that variable replaces the default root set: enterprise roots alone
   will make certificate validation fail against public sites.
3. If the enterprise uses a **TLS-intercepting (man-in-the-middle) proxy**, both steps above must be completed,
   otherwise you will see "it connects, but certificate validation fails".

### 3.3 Common errors and what they mean

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `CERTIFICATE_VERIFY_FAILED` / `SSLCertVerificationError` | the enterprise root is not in the trust store, or the PEM in `REQUESTS_CA_BUNDLE` lacks the root that issued the site certificate | follow both steps in 3.2 |
| Model download fails while a browser opens the same address fine | that path uses standard-library `urllib` (Windows store) while the browser uses its own chain | confirm the root is installed for "Local Computer", not only for the current user |
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

### 5.2 Directory writability (important)

The portable release uses this three-level layout by default:

```text
EchoDesk-v0.2.1\
├── EchoDesk-v0.2.1.lnk          ← launcher shortcut (working directory = user data directory)
├── ui_state.json                ← UI state
├── snippets.json                ← snippets
├── 批量处理结果\                 ← automation output (batch results; user data directory)
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
- **The installed build behaves the same way**: the installer places the whole release folder under `{app}`
  (by default `{autopf}\EchoDesk`) and its shortcuts use `{app}` as the working directory, so the layout matches the
  portable one. A **non-admin install** (the default "install for me only") lands in
  `%LOCALAPPDATA%\Programs\EchoDesk`, which is writable — recommended. An **admin install** lands in
  `C:\Program Files\EchoDesk`, which standard users cannot write to; that deployment must move the artifact paths
  out per 5.4, otherwise session, model and audit-log writes will fail.

### 5.3 Using `ECHODESK_DATA` to put user data on a managed drive

```powershell
# Deliver per user or per machine (Group Policy "Environment" preference)
[Environment]::SetEnvironmentVariable("ECHODESK_DATA", "D:\CorpData\EchoDesk", "User")
```

- What it affects: the frontend (`ui_state.json`, `snippets.json`) and anything the backend derives from
  `user_data_root()` — chiefly the **default root folder for batch results**. It does **not** move the artifacts
  under `app\backend\` (see 5.2).
- If the location pointed at by `ECHODESK_DATA` is not writable: the frontend ignores the variable and falls back to
  automatic detection; the backend adopts the path as given, and subsequent write failures surface as errors
  (it does not silently write somewhere else).
- Use it together with the table in 5.4: `ECHODESK_DATA` governs the "user data directory", and the settings in that
  table govern the "backend artifacts".

### 5.4 Settings to change for a read-only deployment

Set the following to **absolute paths** (all of them accept a `C:\...` form) to move every runtime write out of `app\`:

| Setting | Default location | Suggested target |
| --- | --- | --- |
| `session_file` | `app\backend\session.json` | a managed per-user folder; set it to an empty string to **disable session persistence** |
| `model_hub.models_dir` | `app\backend\models` | the managed model library |
| `automation.audit_log_file` | `app\backend\automation_log.jsonl` | the managed audit folder (easier central collection) |
| `automation.login_state_file` | `app\backend\browser_state.json` | a writable per-user folder (contains cookies; handle as a credential) |
| `automation.processed_file` | `app\backend\automation_processed.json` | the managed drive |
| `automation.download_dir` | `%TEMP%\echodesk_downloads` | a managed temp folder |
| `automation.results_location` | the user data directory | the managed results folder |
| `marketplace.cache_file` | `app\backend\market_cache.json` | the managed cache folder |
| `marketplace.install_dir` | `app\backend\skills` | the managed skills folder |
| `marketplace.dev_dir` / `marketplace.local_manifest` | `app\backend\skills_dev` / `skills_local.json` | the managed drive |

> `config.json` itself must still be writable (the program writes it back when the search toggle or other settings
> change). If it truly has to be read-only, run the program with `config.json` on a writable path, or accept that
> setting changes will not be persisted.

### 5.5 Rebuilding a broken portable shortcut

1. Launch the program by double-clicking `app\EchoDesk.exe` directly (this always works and does not depend on the
   shortcut).
2. At startup the program checks the outer shortcut:
   - shortcut **missing** → it asks "create `EchoDesk-v<version>.lnk`?"; answer yes to create it;
   - shortcut **broken** (target lost or working directory wrong after the folder was moved or renamed) → it asks
     whether to recreate it; answer yes to rebuild.
   The rebuilt shortcut has target = the current `EchoDesk.exe` and **working directory = the user data directory**
   (this is what keeps batch results at the outer level).
3. If you previously answered "no" (skip creation), the program stops asking: delete `ui_state.json` in the user data
   directory to reset that flag (the file also stores UI state, so deleting it only resets UI preferences).
4. You can also create the shortcut by hand: target `<release folder>\app\EchoDesk.exe`,
   **"Start in" = the user data directory (the outer level)**, icon optionally `app\EchoDesk.ico`.

## 6. Auditing and retained evidence

### 6.1 The results file `结果.txt` (results.txt)

- Location: `<automation.results_location>\<automation.results_root_name>\YYYY-MM-DD\<task name>\结果.txt`
  (by default "user data directory\批量处理结果\date\task name\", with `-1`, `-2` suffixes for same-named runs on
  the same day).
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
  directory (in the portable release, `app\backend\automation_log.jsonl`). Enterprises should point it at a central
  audit folder on a managed drive.
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
| `frontend_crash.log` | program folder | WPF frontend exception stack traces | medium (may contain paths) |
| `session.json` | backend working directory | chat history | **high; contains business content** |

### 6.4 Retention and cleanup

- Apart from the temporary staging folders, the program **never deletes** these files automatically; the retention
  policy is yours to define.
- When audit evidence must be retained, **archive** `automation_log.jsonl` and the `批量处理结果\` folder before
  cleaning anything up.
- Suggested cleanup: delete historical date folders under `批量处理结果\` once the retention period expires; delete
  `browser_state.json` and `session.json` when a machine is handed over or retired.

## 7. One-page quick reference

| Requirement | How to do it |
| --- | --- |
| All traffic through the corporate proxy | configure the system proxy (approach A), or deliver `HTTP_PROXY`/`HTTPS_PROXY` (approach B) |
| Trust the enterprise root CA | install it in the Windows certificate store **and** deliver `REQUESTS_CA_BUNDLE` (merged PEM) |
| Allow model download only | firewall allows only the model download domains + turn search/marketplace/update/automation off (policy A) |
| Fully offline | firewall denies outbound by default + pre-placed offline models + all networked features off (policy B) |
| Put user data on a managed drive | `ECHODESK_DATA` plus the absolute-path settings in the 5.4 table |
| Read-only program folder | copy the release to a writable location and run it there; or move artifacts out per 5.4 (`config.json` must stay writable) |
| Post-incident review | `automation_log.jsonl` (who/when/what ran) + `批量处理结果\...\结果.txt` (what was done and how it turned out) |
| Shortcut lost | run `app\EchoDesk.exe` directly and accept the rebuild prompt (delete `ui_state.json` first if it stopped asking) |
