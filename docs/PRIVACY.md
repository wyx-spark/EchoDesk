# EchoDesk Privacy Policy

> **Chinese version:** a reference translation of this policy is available at
> [`PRIVACY.zh-Hans.md`](PRIVACY.zh-Hans.md). If the two ever disagree, this English version prevails.

Version: v0.2.1
Effective date: the release date of this version
Publisher: Void Entropia Studio
Applies to: EchoDesk desktop edition (Python backend `EchoDeskBackend.exe` + .NET WPF frontend `EchoDesk.exe`)

Related documents: [`SECURITY.md`](../SECURITY.md) (security policy and vulnerability reporting), [`EULA.md`](EULA.md) (license agreement),
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) (third-party component inventory),
[`SBOM.json`](SBOM.json) (software bill of materials),
[`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md) / [`en/COMPLIANCE.md`](en/COMPLIANCE.md) (enterprise deployment and compliance).

## In one sentence

**EchoDesk is a local AI desktop assistant: model inference, document parsing and conversation records are
all completed on this machine, and your documents, conversations or any personal data are never uploaded.**
Only features you switch on yourself produce network requests, and what those requests contain is itemized
below, one by one.

---

## Conventions: what counts as "this machine" and what counts as "going online"

- **This machine**: the Windows computer on which you run EchoDesk. All model weights, conversation history,
  parsing results and batch-processing output are stored on this computer's disk.
- **Going online**: HTTP(S) requests actively initiated by the **backend process** (`EchoDeskBackend.exe`).
  The frontend (`EchoDesk.exe`, .NET WPF) **initiates no network requests of its own**; it communicates with
  the backend only through standard input/output (stdio, line-delimited JSON) and **listens on no port**, so
  there is no local service that could be reached from the outside either.
- EchoDesk **has no server side**: there is no EchoDesk cloud API, account system or data-receiving endpoint,
  so under this architecture the statement "EchoDesk has collected your data" has nowhere to attach.
- Apart from the **five features** listed in section 2, EchoDesk contains no other networked code path.

## 1. Behaviour that does not go online by default

The following operations **happen entirely on this machine and produce no network request whatsoever**:

| Operation | Description |
| --- | --- |
| AI chat and inference | Performed by llama.cpp loading a local `.gguf` model on this machine; neither the input nor the output leaves this machine |
| File upload and parsing | Word/Excel/PPT/PDF/TXT are parsed on this machine, and long documents are summarized in chunks on this machine |
| Long-document chunked summarization | Each chunk is summarized with the local model; no cloud service is called |
| Conversation history saving | Saved to `session.json` under the backend working directory (`app\backend\session.json` in the portable build), readable on this machine only |
| Snippets and UI state | Saved to `snippets.json` and `ui_state.json` in the user data directory, readable on this machine only |
| Skill (plugin) loading and execution | Plugin code is loaded from this machine's disk and executed inside the backend process; **nothing is downloaded over the network and nothing is reported back** |
| Skill marketplace cache | The marketplace list is cached locally at `app\backend\market_cache.json`; **browsing without keywords** reads the local cache directly while that cache is valid (no network), and refreshes automatically once it expires; **entering keywords to search** always goes online |
| Automation task execution | The Microsoft Edge already installed on this machine is used to open the URLs you entered; all browser operations are completed on this machine |
| Crash and runtime logs | Written into the program directory and into local files, never uploaded automatically (see section 5) |

## 2. Features that go online (5 in total, every one of them switched on by you)

### 2.1 Model download

| Item | Content |
| --- | --- |
| Trigger condition | On first launch the program asks "do you want to download a model" and you click **Yes**; or you click download in the model hub; or switching with `/model` triggers a prompt and you confirm it |
| Target domains | `hf-mirror.com` (the default mirror site), `huggingface.co` (the official site, switchable in the configuration); a download link may be redirected to that site's CDN / storage domain (of the form `cdn-lfs.<site domain>`; **the exact host name is decided upstream and this project makes no fixed promise about it**) |
| **What is sent** | ① the **model repository name and file name** you want to download (for example `unsloth/Qwen3.5-0.8B-GGUF`, `Qwen3.5-0.8B-Q4_K_M.gguf`); ② the **search keywords together with the sorting and pagination parameters** used when searching for models; ③ a `User-Agent` stating that it is EchoDesk; ④ if you filled in a Hugging Face Token (or set the `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` environment variables), the request header carries `Authorization: Bearer <your token>`. **Note**: when a download link is redirected to that site's CDN, the standard-library `urllib` implementation carries that request header along with it; if an enterprise does not accept this, **do not fill in a Token** (when it is left empty the request header is not sent at all) |
| **What is not sent** | Your conversation content, the documents you have uploaded/parsed, your local file paths, your question history, a machine unique identifier, or any hardware fingerprint or statistical information |
| Data uploaded | None (a pure download; nothing is written apart from the request headers and file names above) |
| Where the data goes | Model files are **downloaded to this machine** (by default `app\backend\models\`, changeable to a managed drive with `model_hub.models_dir`); once the download is complete they can be verified by size and sha256 (`model_hub.verify_sha256`, on by default) |
| How long it is kept | Model files stay on your disk and **only disappear when you delete them by hand** (the model hub can delete them, or you can delete the files directly). The **server access logs of the third-party sites (mirror site/official site) are retained by that third party under its own policy**; EchoDesk cannot query, export or delete those logs |
| How to switch it off | ① Choose "No" when asked about downloading and the program will not download by itself; ② **place the `.gguf` file into the `models\` directory by hand** — the program then loads from local storage only and never goes online at all; ③ do not fill in a Token and do not set `HF_TOKEN` (a Token only affects authentication for private/restricted models and has nothing to do with local data); ④ block the domains above at the firewall/proxy layer (see [`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md)) |

> **Note (not implemented at present)**: although there is an `enabled` field under the `model_hub`
> configuration section, **it does not take effect on the download/search path in the current version**;
> do not rely on it as a "one-click network kill switch" — the only controllable points for model downloads
> are "whether you agree to the download" and blocking the domains at the network layer.

### 2.2 Web search

| Item | Content |
| --- | --- |
| Trigger condition | You run `/search on` (or use the toggle in the status area below the input box) and **then ask a question**; after `/search off` questions are answered fully locally |
| Target domains | `duckduckgo.com` (including subdomains such as `html.duckduckgo.com` and `lite.duckduckgo.com`; the specific endpoint is decided by the upstream search library) |
| **What is sent** | The **keyword text** of this one question of yours, plus the maximum number of results (`search_results`, 3 by default) |
| **What is not sent** | Your document content, the files you uploaded (the body of a file is never stuffed into the search keywords), conversation history, local paths, or account information (this feature neither needs nor uses any account or API Key) |
| Where the data goes | The keywords go to DuckDuckGo; the returned titles/snippets/links are **injected into the context of this question** and fed to the local model |
| How long it is kept | Search results are **not written** into the local conversation history; but the original text of this question of yours is written into the local `session.json` according to the session persistence setting (automatic saving can be switched off by setting `session_file` to empty). Retention of logs on DuckDuckGo's side is decided by its own policy and EchoDesk cannot delete them |
| How to switch it off | `/search off` (this also writes `search_default` back to `false`, so it is still off after a restart); in an enterprise environment it is recommended to block `duckduckgo.com` at the firewall layer as well |

> **Tip**: once search is on, the keywords leave this machine. Do not ask about sensitive content while search is on.

### 2.3 Skill marketplace (including plugin installation)

| Item | Content |
| --- | --- |
| Trigger condition | You open the "Skill marketplace" page of the settings window and refresh/search; or you click "Install/Update" on a skill |
| Target domains | `api.github.com` (repository search and details), `raw.githubusercontent.com` (reading `skill.json` / `README.md`, preferred), `codeload.github.com` (downloading the source tarball) |
| **What is sent** | ① the fixed search condition `topic:echodesk-plugin` plus the **search keywords** you entered and the sorting and pagination parameters; ② the **full repository name** (`owner/name`) of the repository you are viewing/installing; ③ a `User-Agent` stating that it is EchoDesk; ④ if you filled in a GitHub Token, the request header carries `Authorization: Bearer <your token>` (used to raise the API rate limit). **Note**: that request header is added uniformly by the same HTTP client, so all three GitHub domains in the table above receive it; it is **not** sent to any address outside GitHub (a standard client automatically drops that request header on a cross-domain redirect). If an enterprise does not want the credential to appear under several domains, **do not fill in a Token** — when it is left empty the request header is not sent at all |
| **What is not sent** | Your conversation content, document content, the list of skills installed on this machine, local paths, or a machine identifier |
| Data uploaded | None (the marketplace performs read-only queries and downloads only) |
| Where the data goes | The marketplace list is cached locally at `app\backend\market_cache.json`; the source of an installed skill is extracted into the local skill directory (`marketplace.install_dir`, `skills\` by default) |
| How long it is kept | The cache is kept until you refresh it, clear it or delete the file (expiry is controlled by `marketplace.cache_ttl_hours`, 6 hours by default); installed skills stay on the disk until you uninstall them |
| How to switch it off | ① `"marketplace": { "enabled": false }` (written into `config.json` or the user override file `config.user.json`) — marketplace search/installation/status queries then all return "disabled"; ② simply **do not open** the skill marketplace page; ③ do not fill in a GitHub Token; ④ block the domains above at the firewall layer |

> **Note (an important difference from how older versions were described)**: installing a skill **only
> downloads a source tarball from GitHub** (`codeload.github.com`, with a 50MB per-package limit and a
> sha256 record). npm / PyPI dependencies declared in the skill manifest are **only displayed and never
> installed over the network**, and `prepare` scripts are **never executed automatically** — so "installing
> a skill" does not incidentally visit npm or PyPI.

### 2.4 Automatic update check

| Item | Content |
| --- | --- |
| Trigger condition | At startup (`update.check_on_startup`, on by default) or when `check_interval_hours` (24 hours by default) falls due; it can also be triggered manually |
| Target domains | `api.github.com` (reading the Releases list of the repository named by `update.repo`); when a new version exists the program only provides a link in the conversation window, and **you yourself** open a browser to visit `github.com` |
| **What is sent** | One `Accept: application/vnd.github+json` header and `User-Agent: EchoDesk/<current version number> (+update-check)` — that is, the **current version number** is sent with the request |
| **What is not sent** | Conversations, documents, paths, a machine identifier, usage statistics; there is **no** installation ID, device number or anonymous statistics field |
| Data uploaded | None |
| Where the data goes | Only the tags and description text of the Releases are read, to compare versions |
| How long it is kept | On EchoDesk's side only the two time and version strings `last_check` / `last_seen_version` are recorded in the configuration; **no file is downloaded, replaced or installed silently** (this version only "checks and prompts") |
| How to switch it off | `"update": { "enabled": false }`; or set `update.repo` to empty (skipping the check); or turn off `check_on_startup` and lengthen `check_interval_hours` |

### 2.5 Browser automation

| Item | Content |
| --- | --- |
| Trigger condition | After you **accept the safety protocol** on the "Automation" page of the settings window and turn on the master switch, a run starts; or you click "Run once now"; or it runs automatically when the scheduled time arrives (`schedule_enabled`) |
| Target domains | **Only the one task-list URL you entered yourself** (plus the file download links and pagination links you specify on that page); the protocol is limited to `http` / `https` / `file`. EchoDesk **bundles no default site of any kind** |
| **What is sent** | The requests that the target site **normally receives** when the browser visits it: the URL, Cookies (including the login state in this browser profile), form field values, and the click/download actions you ask for on the page |
| **What is not sent** | EchoDesk **sends nothing back to any third party** — neither the task content, nor the downloaded files, nor the generated review opinions or results; there is **no** step of "uploading the results to an EchoDesk server" (no such server exists). In addition, files downloaded while a task is being processed **do not** leave this machine |
| Where the data goes | Everything stays on this machine: the browser login state in `app\backend\browser_state.json`; download staging in the system temporary directory `%TEMP%\echodesk_downloads` (changeable with `automation.download_dir`); the results and the originals under `results\` in the user data directory |
| How long it is kept | Staged download files are cleaned up once the task has been processed (a temporary directory left behind by an abnormal exit is cleaned up on the next start); the **result files and originals under `results\` are kept until you delete them by hand** — this is the audit trail, and the program never clears it automatically |
| How to switch it off | ① Do not accept the safety protocol (while `automation.safety_accepted=false` the master switch cannot be turned on); ② `"automation": { "enabled": false }`; ③ turn `schedule_enabled` off to cancel the scheduled task; ④ delete `browser_state.json` to log out of all signed-in sites. In an enterprise environment it is recommended to **disable this automation as a whole**, because it will reach the external network according to your configuration |

### 2.6 Summary table of the five features

| Feature | Domains | What the request carries | How to switch it off |
| --- | --- | --- | --- |
| Model download | `hf-mirror.com` / `huggingface.co` (+ possible CDN domains) | repository name, file name, search keywords, User-Agent, optional HF Token | do not agree to the download / place the model file by hand / block the domains |
| Web search | `duckduckgo.com` | this question's keywords, maximum number of results | `/search off` / block the domain |
| Skill marketplace | `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com` | topic and search keywords, full repository name, User-Agent, optional GitHub Token | `marketplace.enabled=false` / do not open the marketplace / block the domains |
| Automatic update check | `api.github.com` | the **current version number** in the User-Agent | `update.enabled=false` / set `update.repo` to empty |
| Browser automation | **the URL the user entered themselves** (no default site) | the requests the target site normally receives (including your login Cookie) | do not accept the safety protocol / `automation.enabled=false` |

> Apart from the five categories above, EchoDesk **contains no telemetry, tracking, crash reporting or usage statistics of any kind**.

## 3. Cross-border data transfer

**Please read this section especially carefully during enterprise compliance review.**

1. **Only "requests" leave this machine, not "content"**:
   - What the five networked features send is **metadata required by the feature** (model repository name/file name,
     search keywords, topic and repository name, version number), and it **does not include** the body of your
     conversations, the content of the documents you uploaded or parsed, or the results and downloaded originals
     from batch processing.
2. **Cross-border situation**:
   - Model download: the default mirror `hf-mirror.com` is operated by a third party and its servers are
     **usually located inside the country** (the actual location is as published by the operator, and this project
     makes no guarantee); when you switch to the official site `huggingface.co`, requests go to servers
     **outside the country**.
   - Web search: DuckDuckGo's servers are **outside the country**, so keywords do cross the border (that is what
     the search feature itself means).
   - Skill marketplace / automatic update check: `api.github.com`, `raw.githubusercontent.com` and
     `codeload.github.com` are all GitHub services, with servers **outside the country**.
   - Browser automation: the data flows to **the target site you entered yourself**; whether it crosses the border
     depends on where that site is.
3. **Conversations and documents do not cross the border**: model inference, file parsing, chunked summarization
   and automation result generation are all completed on this machine; there is no path by which "conversations or
   documents are sent to a model vendor" — because EchoDesk calls no cloud large-model API at all.
4. **No account, no telemetry**: EchoDesk requires no registration, collects no device fingerprint and sends no
   installation ID; the only information in the requests above that has anything to do with the "machine" is the
   **current version number** (the User-Agent of the update check).
5. **Where the compliance judgement lands**: if your organisation's rule is "business data must not leave the
   country", then as long as you **do not use web search**, **point model downloads at an internal mirror or use
   offline models instead**, and **disable the skill marketplace and the automatic update check**, EchoDesk is a
   purely local tool; conversely, the moment you switch search on or point downloads at the official site, the
   corresponding metadata crosses the border, so carry out the assessment and notification your organisation requires.

## 4. Enterprise/restricted environments: the checklist for banning network access entirely

Work through the checklist below item by item and EchoDesk can be narrowed down to a **fully offline** way of
running (for more detailed procedures and the domain allow-list see [`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md) / [`en/COMPLIANCE.md`](en/COMPLIANCE.md)):

1. **Take the model offline**: download the `.gguf` on a machine with network access, copy it into the `models\`
   directory on the intranet machine, and use `/model` to select the local file. Do not click "download" in any
   prompt.
2. **Turn search off**: `/search off`; and confirm that the configuration file contains `"search_default": false`.
3. **Turn the marketplace off**: `"marketplace": { "enabled": false }`.
4. **Turn the update check off**: `"update": { "enabled": false }` (or `"repo": ""`).
5. **Turn automation off** (if you do not need it): `"automation": { "enabled": false }`, and do not accept the
   safety protocol. If you genuinely need automation but no external connectivity is allowed, confirm with your
   network administrator that the target site is on your allow-list.
6. **Fill in no Token of any kind**: do not fill in a Hugging Face Token, do not set `HF_TOKEN` /
   `HUGGING_FACE_HUB_TOKEN`, and do not fill in a GitHub Token.
7. **Network-layer fallback**: block the domains in the table in section 2.6 at the firewall/proxy layer (this is
   the only measure that covers "code paths added in the future").
8. **Proxy evidence trail**: if your enterprise already requires all outbound traffic to be audited through a
   proxy, you can keep the system proxy settings — on the Python side `requests` / `urllib` go through the proxy
   by default, and the outbound records can be checked in the proxy logs.

## 5. Logs and redaction

- Crash logs: frontend `frontend_crash.log` (program directory), backend `backend_crash.log` (backend directory);
  they record nothing but exception stack traces, used for diagnosing startup/runtime problems.
- Automation audit log `automation_log.jsonl`: append-only JSON Lines, recording
  "time / event / task link / **file name only** / result / truncated error / safety protocol version and consent time".
- **Redaction rules**: the logs never contain file body content, model output body, GitHub/Hugging Face Tokens,
  Cookies, passwords or any form field values; over-long fields are always truncated.
- All logs are stored on this machine and are **never uploaded automatically**. For enterprise auditing, rely on
  `automation_log.jsonl` and `result.txt` under `results\` (for their locations and purposes see the "Auditing and
  retained evidence" section of the compliance document).

## 6. Temporary files and memory cleanup

- Staged files for automation downloads are all placed in a dedicated subdirectory of the system temporary
  directory (by default `%TEMP%\echodesk_downloads`) and are cleaned up once the task has been processed; a
  temporary directory left behind when the program exits abnormally is cleaned up on the next start. Originals
  that **need to be retained** after processing, along with `result.txt`, are stored under `results\` and managed
  by you.
- The temporary directory for skill installation (`%TEMP%\echodesk-market-*`) is cleaned up when installation
  finishes (including on failure).
- The browser login state (Cookie / localStorage) is stored in `browser_state.json` in the backend directory; you
  can delete that file at any time to log out of all signed-in sites, and it is deleted along with the program
  directory when you uninstall.
- The KV cache used during inference lives in memory and is released as soon as the process exits.

## 7. Your control

- Delete `session.json` in the backend directory → clears the conversation history.
- Delete `snippets.json` in the user data directory → clears the snippets.
- Delete `ui_state.json` in the user data directory → resets the window state and the first-run guide flag.
- Delete `models\*.gguf` → removes the local models (using them again will ask about downloading once more).
- Delete `browser_state.json` → logs out of every automation browser session.
- Delete `market_cache.json` → clears the skill marketplace cache.
- Delete `results\` → clears the automation output (**before deleting, confirm whether the audit trail still needs to be kept**).
- Delete the entire program directory → equivalent to a complete uninstall (the portable build writes no registry
  entries; the installed build does it through the uninstaller).

## 8. Children and compliance

EchoDesk is designed for adult professional users. When you use the automation feature to access third-party sites
or install third-party skills, you must confirm for yourself that doing so complies with the target site's terms
of service and with the rules and regulations of the organisation you belong to.

## 9. Policy changes

If this policy changes, the change will be updated in step in `docs/PRIVACY.md` in the new version and in the
explanatory files inside the distribution, and will be noted in [`CHANGELOG.md`](../CHANGELOG.md).

## 10. Contact

- Security vulnerabilities and privacy questions: the public feedback channels are
  [Issues](https://github.com/VoidEntropiaStudio/EchoDesk/issues) and
  [Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions); if a privacy question must be
  handled privately, use the private reporting channel in [`SECURITY.md`](../SECURITY.md) — this repository's
  **Security → Report a vulnerability** private advisory form,
  <https://github.com/VoidEntropiaStudio/EchoDesk/security/advisories/new> — and do not open a public Issue.
- General questions and usage enquiries: see [`zh-Hans/FAQ.md`](zh-Hans/FAQ.md) / [`en/FAQ.md`](en/FAQ.md),
  and the explanatory files inside the distribution.
