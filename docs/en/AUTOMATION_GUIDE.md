# EchoDesk Automation Configuration Guide

This guide walks you through setting up browser automation: letting EchoDesk open your organisation's web
pages, click through each task, download the attachments, hand them to the local model to produce a review,
and file the results away **neatly** in folders.

No programming and no web knowledge is required. Wherever a bit of background helps, the guide gives the
concrete "click here, type this" route.

Chinese version: [`docs/zh-Hans/AUTOMATION_GUIDE.md`](../zh-Hans/AUTOMATION_GUIDE.md).
Related documents: [User guide](USER_GUIDE.md) · [FAQ](FAQ.md) · [Skill development guide](SKILL_GUIDE.md).

> **⚠️ Windows-only feature: browser automation is not available on macOS or Linux.**
>
> Automation drives the **Microsoft Edge installation on this machine** through Playwright — a visible window
> and a reused sign-in state — and its configuration page is part of the Windows-only UI. On **macOS and
> Linux the feature is not offered at all**: the automation page is absent, and requesting the capability is
> answered with `Windows-only capability (this is not a missing dependency)` — deliberately **not** a broken
> install, **not** a missing package, and **not** something a `pip install` will fix.
>
> **Alternatives**, in the order most users take them: an **MCP server** that exposes the browser work as
> tools the agent can call; a **skill** that performs the same flow and returns results in the shape the
> built-in automation would; or **running the automation on a Windows machine and exporting the results
> folder** to the machine you actually work on. Everything after the download — parsing, retrieval, review and
> reporting — stays fully portable, so a `results/` folder produced on Windows can be reviewed anywhere.
>
> The rest of this guide therefore documents the **Windows** configuration surface. Where a path genuinely
> differs per platform (the results location, above all), all three are given. Authoritative reference:
> [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) §3.2 and §5.

**How paths are written here.** Paths inside the program folder use forward slashes
(`app/backend/config.json`) — that is exactly how they read on macOS and Linux, and on Windows the same path
uses backslashes (`app\backend\config.json`). Only the **results root** really differs, because each platform
keeps user data in its own place (see 3.9 and 4.3).

---

## 1. Before you start

### 1.1 Three things you need

| Requirement | Why | How to check |
| --- | --- | --- |
| **Windows** — browser automation is Windows-only (see the warning above; on macOS/Linux the feature is not offered at all) | Automation drives the Edge installation that exists on Windows, and its configuration page is part of the Windows-only UI | On macOS/Linux there is nothing to configure — use the alternatives listed above instead |
| Microsoft Edge is installed | Automation opens pages with the Edge on your machine (`browser_channel` defaults to `msedge`); headless mode is never used | If Edge opens normally, you are fine |
| A local model is downloaded | Producing the review needs the local model; without one you get "no local model is loaded, cannot generate a summary or review opinion" | `/status` in the main window shows the loaded model |
| Your machine can reach that web system | Including login, permissions, intranet and VPN; automation only clicks for you, it cannot reach anything you cannot reach | Open the task list page once in Edge by hand and confirm you can see the tasks |

If the attachments are legacy `.doc` / `.ppt` files, or the documents are encrypted, the machine also needs
**WPS or Microsoft Office** installed (used for COM-based reading). That too is **Windows-only**: where the COM
interface does not exist the file gets an explicit "this capability is provided on Windows only" message, and
the alternatives are to save an unencrypted or `.docx` copy with WPS/Office on a Windows machine, to convert
through an MCP server or a skill, or to install a skill that registers its own parser for that format —
never "install WPS and retry", which on those platforms would be a wild goose chase.

### 1.2 What automation actually does

After you click "▶ Run once now", the program:

1. **Opens the browser with a visible window** (you can watch the whole time) and visits the task list URL you
   entered; the **sign-in state is reused automatically** — sign in once in that browser window and later runs
   never ask again (see section 10);
2. Finds every task link using "Task link position" (relative paths are completed into absolute addresses, while
   pseudo-links such as `javascript:` / `#` / an empty href switch to a click automatically — see section 11),
   and turns pages automatically using "Next page button" (if you filled it in);
3. Opens each task in turn, finds the attachments using "Attachment position on the detail page", and downloads only files whose extension is listed in "File types to process";
4. Parses the attachments locally and hands their content, together with the "Task instruction", to the local model to produce a review;
5. Keeps the original attachments and the result (written into the results folder) and moves on to the next task;
6. Appends one record per task to the audit log (see section 9).

When a CAPTCHA or two-factor verification appears, the run **pauses and waits for you** (180 seconds by default).

### 1.3 The five-minute path

1. Open **Settings → Automation**;
2. Fill in, from top to bottom: "Task list URL → Task link position → Attachment position on the detail page → File types to process → Task instruction";
3. Click **"Test selectors"** and walk through the wizard step by step (section 5 explains it in detail);
4. Tick the **safety protocol**, then switch on the **automation master switch**;
5. Click **"▶ Run once now"** and watch the first one or two tasks to confirm the results folder contains what you expect.

> 📷 On screen: the whole Automation tab of the Settings window — the safety protocol box, the
> positions of the fields, the "Test selectors" button on the right, and the
> "▶ Run once now / ⏹ Stop automation / run log" area at the bottom.> 📷 **No screenshots ship with v0.2.1.** The interface previews below will be added to
> `assets/screenshots/` in a future update. The description here tells you what to look at
> on screen in the meantime.


---

## 2. Field overview

### 2.1 Required fields (nothing runs without them)

| Field (UI / config name) | Required? | Purpose | Default |
| --- | --- | --- | --- |
| Task list URL `task_list_url` | **Required** | The address of the page that lists all your tasks | empty |
| Task link position `task_list_selector` | **Required** | Tells the program where the task links are, so it can find each task | empty |
| Attachment position on the detail page `task_detail_file_selector` | **Required** | Tells the program where the attachment names are on a task page | empty |
| File types to process `file_extensions` | **Required** | Only these file types are processed; everything else is skipped | `pdf, docx, doc, xlsx, xls, pptx, ppt, txt` |
| Task instruction `instruction` | **Required** (has a default) | Tells the AI what to do with the files and what kind of opinion to produce | "extract the main information of the contract and produce a financial review opinion" |
| Safety protocol `safety_accepted` | **Required** | Precondition for automation; without it the master switch cannot take effect | `false` |
| Automation master switch `enabled` | Must be on to run | Automation only executes when this is on; it is forced off while the safety protocol is unticked | `false` |

### 2.2 Optional fields (all editable in the Settings window)

| Field (UI / config name) | Required? | Purpose | Default |
| --- | --- | --- | --- |
| Task row container `task_row_selector` | Optional | Fill in when one task occupies a whole row (for example a table row) so the program locates it more accurately; leave empty if unsure | empty |
| Next page button `next_page_selector` | Optional | Fill in when the task list has several pages and the program should turn them by itself; leave empty for a single page | empty |
| Maximum tasks per run `max_tasks` | Optional | How many tasks one run handles at most, so a run cannot get out of hand | `20` (1–500) |
| Page timeout (ms) `page_timeout_ms` | Optional | How long to wait for a page to open; increase it for slow intranet systems | `30000` (3000–300000) |
| Retries on failure `retries` | Optional | How many times a failed task is retried automatically | `1` (0–5) |
| Skip tasks already processed `dedupe` | Optional | When ticked, tasks you already processed are skipped next time | `true` |
| Save result files `save_results` | Optional | Clear it to do a dry run only — no result file is written | `true` |
| Result folder name `task_name` | Optional | The name of the third-level result folder, i.e. what this batch is called | `auto-task` |
| Result root folder name `results_root_name` | Optional | The first-level folder inside the results root | `results` |
| Where to save results `results_location` | Optional | Put results on another drive or a shared folder; empty = the user data folder (Windows: the portable **outer** folder `EchoDesk-v0.2.2/`, not `app/`; macOS: `~/Library/Application Support/EchoDesk/`; Linux: `$XDG_DATA_HOME/EchoDesk/` or `~/.local/share/EchoDesk/` — set the `ECHODESK_DATA` environment variable to move it on any platform) | empty (= the user data folder) |
| Run on a schedule `schedule_enabled` | Optional | Runs a batch automatically at fixed times every day | `false` |
| Scheduled times `schedule_times` | Optional | The times of day, separated by commas | `09:00` |
| Sign-in state data directory `login_state_dir` | Optional | The separate directory automation uses to store the sign-in state in the browser (never shared with your everyday browser); empty = `browser_profile` inside the program folder. Several target sites can share one | empty |
| Element that appears only after signing in `login_check_selector` | Optional | The most reliable way to decide whether you are signed in: an element that exists only once you are. Use "🎯 Pick visually" to click it | empty |
| Sign-in page URL keywords `login_url_keywords` | Optional | The fallback used when the field above is empty: if the address contains one of these words, the program assumes it was redirected to a sign-in page | `login, signin, sign-in, logon, sso, passport, cas, auth` |

### 2.3 Advanced fields (only editable in `app/backend/config.json`)

These fields have no input box in the Settings window yet. **Close EchoDesk before editing**, then save and
restart for the change to take effect (the file is UTF-8; Notepad, VS Code or any editor on any of the three
platforms works — the file sits at the same relative path everywhere, only the separators differ:
`app/backend/config.json` on macOS/Linux, `app\backend\config.json` on Windows).

| Config field | Required? | Purpose | Default |
| --- | --- | --- | --- |
| `llm_temperature` | Optional | Generation temperature: higher is more varied, lower is more conservative; 0.2–0.4 suits review work | `0.3` (0.0–1.0) |
| `llm_max_tokens` | Optional | Maximum length of one review opinion in tokens; longer output is slower | `512` (64–4096) |
| `wait_network_idle` | Optional | Waits until network requests stop before acting: steadier, a little slower | `true` |
| `download_dir` | Optional | Attachments are downloaded into this temporary folder and cleaned up afterwards | empty (= `echodesk_downloads` under the system temp folder) |
| `processed_file` | Optional | Where the processed-task record lives (used by "Skip tasks already processed") | `automation_processed.json` |
| `login_state_file` | Optional | The **backup / migration** file for the sign-in state (cookies / localStorage). The primary mechanism is the browser's own data directory; this JSON file is also used to import cookies once when upgrading from an older version | `browser_state.json` |
| `manual_login_wait_seconds` | Optional | How long the run waits for you when a CAPTCHA or login appears | `180` (0–3600) |
| `headless` | Read-only | Automation **only supports a visible browser**; writing `true` is corrected back to `false` with a warning | `false` |
| `browser_channel` | Optional | Which browser opens the pages; the system Edge is the default | `msedge` |
| `audit_log_file` | Optional | Audit log file: one JSON record per task | `automation_log.jsonl` |
| `start_with_windows` | **Not implemented** | The program does not read this field yet, so setting it to `true` does not start EchoDesk at sign-in; to start with Windows, copy `Start-EchoDesk-v0.2.2.lnk` into the system Startup folder (`Win+R` → `shell:startup`). That route is Windows-only; macOS and Linux have no built-in auto-start in this version (start it yourself with `./start.sh`, or use your desktop's own login-items / autostart mechanism) | `false` |
| `safety_version` / `safety_accepted_at` | Read-only | Safety protocol version and when you accepted it; recorded automatically for auditing | `1.0` / empty |

---

## 3. Field-by-field instructions

> Every field has a "?" icon in the Settings window; hovering shows the same explanation (the text comes from
> the language packs and follows the interface language). The Settings window also has a "📖 Filling-in help"
> button that opens a condensed version of this document.

### 3.1 Task list URL

- **In plain words**: the page address that shows all your tasks. Copy it from the browser address bar and paste it here.
- **Example**: `https://oa.example.com/task/list`
- **Note**: only `http` / `https` / local file (`file:`) addresses are supported; any other protocol reports "the task list URL protocol is not supported".
- Addresses containing `#` or `?` parameters are fine — paste them as they are.

### 3.2 Task link position

- **In plain words**: where the task links you want the program to click are on the page. Click the **"🎯 Pick on page"** button on the right, then click a task name in the page.
- **If you must type it manually**: right-click the task link → Inspect → right-click the highlighted code → Copy → Copy selector → paste it here.
- **Example**: `table.task-list a.title`
- **Two notations are accepted**:
  - CSS selectors (recommended): write them directly, e.g. `a.task-link`, `td.title > a`;
  - XPath: start with `//`, `(` or `xpath=`, e.g. `//a[@class='task']`.

> 📷 On screen: picking the task name with "🎯 Pick on page" on the task list page — a red
> highlight box around the hovered element, a selector preview tooltip following the cursor,
> and a blue banner at the top reading "move the mouse onto the element to capture and click;
> press Esc to cancel".

### 3.3 Task row container (optional)

- **In plain words**: if one task occupies a whole row on the page (for example a table row), put that row's position here so the program can locate it more accurately. Leave it empty if you are not sure.
- **Example**: `table.task-list tr`
- **Tip**: leaving it empty never hurts. You only need it when the task link is mixed together with other links and the wrong ones get picked up.

### 3.4 Next page button (optional)

- **In plain words**: if the task list has several pages, put the position of the "Next page" button here and the program turns pages by itself. Leave it empty when there is only one page.
- **Example**: `a.next-page`
- **Tip**: if the button reads ">", "Next ›" or "下一页", make sure you pick **the clickable element** (usually an `<a>` or `<button>`) rather than the `<div>` wrapping it.

### 3.5 Attachment position on the detail page

- **In plain words**: after you open a task you can see the download links for its attachments. Click "🎯 Pick on page" and choose "attachment on the detail page": the program first opens the first task automatically, and then you click the attachment name.
- **Example**: `div.attachments a`
- **Tip**: if a detail page has several attachments, picking one matches all attachments of the same kind; step 3 of the wizard tells you how many were matched and how many of them satisfy the extension list.

### 3.6 File types to process

- **In plain words**: tells the program which file types to handle. Typical: `pdf, docx` for contracts, `xlsx` for spreadsheets.
- **Example**: `pdf, docx`
- **Tick boxes available**: the UI offers a tick-list (PDF, Word, Excel, PPT, plain text) — tick what you need, or type the list directly.
- **Forgiving input**: `PDF`, `.docx` and even the Chinese comma "，" are all accepted; the program lowercases everything, strips leading dots and splits on commas for you.

### 3.7 Task instruction

- **In plain words**: tells the AI what to do with the files. Write in plain words what to extract and what kind of opinion to produce.
- **Example**: `Extract the contract amount, payment method and terms, penalty ratio, both parties and the signing date; flag anything out of policy`
- **How to write it**:
  - say what to "extract" first, then what to "flag or judge";
  - four to six items at a time is plenty — asking for too much makes the model lose track;
  - no need for pleasantries such as "please read the attachment"; the file content is appended to your instruction automatically.
- **Preset templates**: four common templates can be applied directly from the Settings window (see section 7).
- **Thinking content from reasoning models**: when a thinking model is used (Qwen3 thinking, DeepSeek-R1 and
  so on), the “generate the review opinion” step takes **only the final answer** — the reasoning trace is
  never written into task results or `result.txt` (it only appears in the chat with that model). Result files
  therefore stay clean conclusions.

### 3.8 Result folder name (optional)

- **In plain words**: the name used for the result folder. It defaults to the task name and usually needs no change.
- **Example**: `Contract review`
- **What it does**: whatever you type becomes the third-level folder name. **Duplicate names on the same day get a number** (`合同审查`, `合同审查-1`, `合同审查-2`), so an earlier batch is never overwritten.
- **Left empty**: the default name `auto-task` is used.

### 3.9 Where the results go

| Field | In plain words | Example |
| --- | --- | --- |
| Result root folder name | What the first-level folder under the results root is called | `results` |
| Where to save results | Fill this in to put results on another drive or a shared folder; empty means the user data folder (on Windows the portable **outer** folder, i.e. the one holding the shortcut) | `D:\contract-review-results` (Windows) · `/Users/<user>/Documents/contract-review-results` (macOS) · `/home/<user>/contract-review-results` (Linux) |

Leaving it empty puts the results root in the user data folder, which each platform places differently:

```
# Windows — the program folder itself (portable layout: the outer folder that holds app/);
# %APPDATA%\EchoDesk when that is where EchoDesk was installed or unpacked
C:\Users\<user>\EchoDesk-v0.2.2\results\
%APPDATA%\EchoDesk\results\

# macOS
~/Library/Application Support/EchoDesk/results/

# Linux (~/.local/share/EchoDesk/results/ when XDG_DATA_HOME is unset)
$XDG_DATA_HOME/EchoDesk/results/
```

The window previews the full structure live, for example:
`Result root / YYYY-MM-DD / task name / result.txt + original files`.
Click "Open results folder" to open the results root in the file manager (File Explorer on Windows, Finder on
macOS, your desktop's file manager on Linux); click "Browse…" to pick a folder as the results location.

### 3.10 Run settings

| Field | In plain words | When to change it |
| --- | --- | --- |
| Maximum tasks per run | How many tasks one run handles at most | Set it to `5` for a first small trial |
| Page timeout (ms) | How long to wait for a page to open | Raise it to `60000` when a slow intranet keeps timing out |
| Retries on failure | How many times a failed task is retried | Set it to `2` on an unstable network |
| Skip tasks already processed | Tasks already processed are skipped next time | Clear it to reprocess everything |
| Save result files | Clear it to do a dry run with no result file | Clear it when you only want to watch the flow |

### 3.11 Scheduling

- **In plain words**: turn on "Run on a schedule" and fill in the times of day to run, separated by commas, for example `09:00, 18:30`.
- **Note**: scheduled runs are driven by a background thread, so **EchoDesk must stay running**; invalid times (such as `25:00`) are dropped automatically.
- To start EchoDesk with Windows, put the "Start-EchoDesk-v0.2.2.lnk" shortcut into the system Startup folder
  (`Win+R` → `shell:startup`). The `start_with_windows` config field is **not implemented**: the program does
  not read it, so setting it to `true` has no effect.
- That shortcut route is **Windows-only by name and by mechanism** (a `.lnk` file and the `shell:startup`
  folder). macOS and Linux have no built-in auto-start in this version: start the bundle with `./start.sh`
  yourself, or wire the start command into your desktop's own mechanism (macOS "Login Items", a Linux desktop
  autostart entry or a systemd user unit).

### 3.12 Safety protocol and the automation master switch

- The safety protocol comes first: tick "I have read and understood the safety protocol above and accept responsibility for using it";
- While it is unticked the master switch is **forced off** by the backend, with a message in the log;
- The protocol version and the time you accepted it are written to the audit log for later tracing.

### 3.13 Sign-in state fields (data directory / detection element / page keywords)

All three live under **Settings window → Automation → Sign-in state**, and most users **never have to change
them** (the defaults work). Their full description — plus "what the program does when you are not signed in",
"how to clear the sign-in state" and "how several sites share it" — is in **section 10**. In one line each:

- **Data directory**: where automation stores the sign-in state on its own; sign in once and later runs reuse it;
  leave it empty;
- **Element that appears only after signing in**: fill it in for a smarter "am I signed in?" answer (the user
  name in the corner, for example); "🎯 Pick visually" fills it in for you;
- **Sign-in page URL keywords**: the fallback used when the field above is empty; usually left alone.

---

## 4. A complete example: contract review

Scenario: an OA system lists several purchase contracts as tasks; each one has to be downloaded and turned
into a financial review opinion.

### 4.1 What to type in the UI

| Field | Value |
| --- | --- |
| Task list URL | `https://oa.example.com/task/list` |
| Task link position | `table.task-list a.title` |
| Task row container (optional) | `table.task-list tr` |
| Next page button (optional) | `a.next-page` |
| Attachment position on the detail page | `div.attachments a` |
| File types to process | `pdf, docx` |
| Task instruction | `Extract the contract amount, payment method and terms, penalty ratio, both parties and the signing date; flag anything out of policy` |
| Result folder name | `Contract review` |
| Result root folder name | `results` |
| Where to save results | Empty (= the user data folder, i.e. the platform's own location for it); use `D:\contract-review-results` on Windows, `/Users/<user>/Documents/contract-review-results` on macOS or `/home/<user>/contract-review-results` on Linux to put it elsewhere |
| Maximum tasks / page timeout / retries | `20` / `30000` / `1` |
| Skip tasks already processed, Save result files | both ticked |

### 4.2 The equivalent `config.json` fragment

The real structure inside `app/backend/config.json` (the `automation` section; you can edit it directly, but close
EchoDesk first):

```json
{
  "automation": {
    "enabled": true,
    "safety_accepted": true,
    "safety_version": "1.0",
    "safety_accepted_at": "2026-09-23T09:00:00",
    "task_list_url": "https://oa.example.com/task/list",
    "task_list_selector": "table.task-list a.title",
    "task_row_selector": "table.task-list tr",
    "next_page_selector": "a.next-page",
    "task_detail_file_selector": "div.attachments a",
    "file_extensions": ["pdf", "docx"],
    "instruction": "Extract the contract amount, payment method and terms, penalty ratio, both parties and the signing date; flag anything out of policy",
    "llm_temperature": 0.3,
    "llm_max_tokens": 512,
    "max_tasks": 20,
    "page_timeout_ms": 30000,
    "wait_network_idle": true,
    "retries": 1,
    "download_dir": "",
    "dedupe": true,
    "processed_file": "automation_processed.json",
    "login_state_file": "browser_state.json",
    "manual_login_wait_seconds": 180,
    "headless": false,
    "browser_channel": "msedge",
    "save_results": true,
    "results_root_name": "results",
    "results_location": "",
    "task_name": "Contract review",
    "audit_log_file": "automation_log.jsonl",
    "schedule_enabled": false,
    "schedule_times": ["09:00"],
    "start_with_windows": false
  }
}
```

A note on folder names: the default value of `results_root_name` is `results`, an ASCII name, so that
every language of install produces the same on-disk paths and no encoding trouble can arise from the
folder name. (Before v0.2.1 this default was the Chinese word `批量处理结果`; the name was changed so
that all names the program creates are English — see the release notes for the migration impact.)
You may rename it — for example to `batch-results` — but keep it free of characters the file system forbids
in names (Windows: `\ / : * ? " < > |` and a trailing dot or space; macOS and Linux: `/`), and avoid a
leading `-`, which shells read as an option rather than a name.

### 4.3 What the results folder looks like afterwards

With "Where to save results" left empty, the results land in the **user data folder** — on Windows that is
the outer `EchoDesk-v0.2.2/` folder (the one holding the shortcut), **not** `app/`, and on macOS/Linux it is
the platform directory listed in 3.9:

```
<data folder>/
└── results/                        ← result root folder name (customisable)
    └── 2026-09-23/                     ← the run date (several runs on one day share this level)
        └── Contract review/            ← the result folder name (duplicates get -1, -2)
            ├── 年度采购合同.pdf           ← the original attachments that were downloaded
            ├── 补充协议.docx
            ├── result.txt                  ← every task of this run (plain text)
            └── failure-screenshot-task2-20260923_101010.png   ← only when a task failed
```

Separators inside that tree follow the running platform (`\` on Windows, `/` on macOS and Linux), and the
folder names are identical on all three. `result.txt` is **plain text** — a text editor shows it directly,
with no `#`, `**` or `|` markers. It looks roughly like this:

```text
EchoDesk results
========================================
任务名称：Contract review
运行日期：2026-09-23
开始时间：2026-09-23T09:12:03
任务列表：https://oa.example.com/task/list
任务指令：Extract the contract amount, payment method and terms, penalty ratio, both parties and the signing date; flag anything out of policy
结果目录：<data folder>/results/2026-09-23/Contract review

[1/3] 年度采购合同
--------------------------
任务链接：https://oa.example.com/task/detail/1024
处理时间：2026-09-23T09:12:31
附件：年度采购合同.pdf

审核意见：
(the review opinion in plain language: amount, payment method and terms, penalty ratio, ...)

[2/3] 服务协议（失败）
--------------------------
任务链接：https://oa.example.com/task/detail/1025
附件：服务协议.pdf
失败原因：详情页没有符合后缀要求的文件链接
失败截图：failure-screenshot-task2-20260923_101010.png

自动化完成：成功 1 个，跳过 0 个，失败 1 个（共发现 3 个任务，处理上限 20 个）。
```

The report header and the per-task lines are produced by the backend, so they stay in Chinese
(`任务名称:` = "task name", `运行日期:` = "run date", `附件:` = "attachments", `审核意见:` = "review opinion",
`失败原因:` = "failure reason", `自动化完成:` = "automation finished"). Only the file names and the
instruction text come from your own configuration.

> 📷 On screen: the file manager with the three-level "results/date/task name" folders — the
> original attachments and `result.txt` sit in the same folder.

---

## 5. The correct way to test your selectors

**Test before you run** is the single most important habit with this feature. Clicking "Test selectors" starts a
step-by-step wizard; each step tells you plainly whether it succeeded and what to do next.

### Step 1: test the task link position

- The program opens a browser, visits the task list page and highlights the elements matched by your selector;
- **What success looks like**: the number of matches and the first few task names (for example "matched 8 task links, for example: 年度采购合同, 服务协议, 设备采购单").
  If you also filled in the task row container, it reports how many rows matched as well; **once this step passes you can leave the row container alone**.
- **What failure looks like, and what to do**:
  - "the page opened, but this selector matched no elements" → confirm this page really lists tasks (some systems need a login first), pick the task name again with "🎯 Pick on page", or scroll until the tasks appear when the list is paginated or lazily loaded;
  - "test failed: …" → check that the URL opens normally in a browser and that the selector was not changed by accident.

### Step 2: test the next page button

- The program **really clicks** the "next page" button and then reports whether the page moved.
- **What success looks like**: "found and clicked the next-page button, the page moved. Address after clicking: …".
- **What failure looks like, and what to do**:
  - "no next page button configured" → normal for a single-page list; this step passes anyway and you can continue;
  - the page did not move → if this really was the last page, nothing moving is normal; otherwise the button may be handled by JavaScript, so try a more precise selector, or pick the actual clickable element inside it.

### Step 3: test the attachment position on the detail page

- The wizard **opens the first task for you** and then tests the attachment selector on that detail page;
- **What success looks like**: "on the detail page of task '年度采购合同' 3 attachment elements matched, of which 2 have an extension listed in File types to process."
- **What failure looks like, and what to do**:
  - "no attachment element matched on the task detail page" → pick "attachment on the detail page" again with "🎯 Pick on page", and confirm the first task really has attachments;
  - "0 of them have an extension in …" → the attachments use a format you did not list; add the missing extension (for example `ofd`) as prompted;
  - "the task list selector matched no usable task" → go back and make step 1 pass first.

### Step 4: dry-run one task (no result file is written)

- Only the first task is processed, with the full download and parsing flow, and the generated review opinion is
  shown to you — **but no result file is written** (a rehearsal with `save_results` effectively off).
- **What success looks like**: the log runs through "enter detail → download → parse → generating the review opinion →"
  followed by the opinion itself; the wizard then suggests ticking the safety protocol and switching the master
  switch on for a real batch.
- **What failure looks like, and what to do**:
  - "the configuration has not passed validation: - …" → fix each item listed (for example the safety protocol is
    unticked, the master switch is off, or no model is loaded);
  - an error about downloading or logging in → follow the hints to check the selectors and login state, and confirm
    that the same download works when you do it by hand on this machine.

### Step 5: accept the safety protocol and switch automation on

- Back in the safety protocol box, tick "I have read and understood the safety protocol above and accept responsibility for using it";
- Then switch on "Enable automation" (while the protocol is unticked the switch cannot be turned on, or is turned back off automatically);
- Finally click "▶ Run once now" — for the first round, set **Maximum tasks per run to 3–5** and scale up only after you are satisfied.

> 📷 On screen: step 1 of the "Test selectors" wizard succeeding — a green success message, the
> match count, the sample task names, and the "Next step" button.

---

## 6. Troubleshooting table

| Symptom | Cause | What to do |
| --- | --- | --- |
| The selector test says "matched no elements" | The selector is wrong, the page has not finished loading, or this page genuinely has no tasks | Pick the task name again with "🎯 Pick on page"; confirm the page really lists tasks; wait a few seconds and test again |
| Far too many elements match (dozens or hundreds) and unrelated links are downloaded | The selector is too broad (for example it catches a whole `<ul>` or every `<a>`) | Narrow it to the link that carries the task name, e.g. `table.task-list a.title`; add the task row container to bound the search when needed |
| The page opens on a login screen, or says you are not signed in | The login state is missing or expired | The first run opens a browser window: sign in by hand once and the state is stored in `browser_state.json` for reuse |
| It worked before, now it asks you to sign in again | The login state expired, the site changed its cookies or domain, or `browser_state.json` was deleted | Sign in by hand again; if it expires constantly, check whether your company limits session length |
| "no next-page button matched" | The selector points at an outer container, the button is an icon, or this page is the last one | Pick the "next page" control at the bottom of the list page with "🎯 Pick on page"; switch to the clickable element inside |
| The attachment download times out or hangs | A slow intranet, a large file, or the download opens a new browser tab | Raise "Page timeout (ms)" to `60000`; confirm the attachment downloads when clicked by hand; try "Retries on failure" = `2` |
| "the detail page has no file link matching the required extensions" | The attachment extension is not in "File types to process" | Add the real extension (for example `ofd`, `wps`); use wizard step 3 to confirm the attachments are matched at all |
| The attachment downloaded but parsing returns nothing | A scanned PDF has no text layer, or the file is damaged | Scanned files need OCR (not built in); save them as a text-copyable format with WPS/Office first, or handle that task by hand |
| Parsing `.doc` / `.ppt` fails or asks for WPS | Legacy binary formats (and encrypted documents) are read through COM, and WPS/Office is not installed | Install WPS or Microsoft Office, or save the file as `.docx` / `.pptx` first. Both the COM path and this advice are **Windows-only**; off Windows the capability is reported as platform-unavailable (not as a missing dependency) — see the warning at the top of this guide for the alternatives |
| The model is too slow, or reports "the model returned an empty result" | The task instruction is too long, `llm_max_tokens` is too large, or a long attachment triggered chunked summarisation | Trim the instruction to four to six items; set `llm_max_tokens` to 384–512; switch to a smaller quantised model (such as Q4_K_M) |
| The results folder is not created, or writing fails | "Where to save results" points at a path that does not exist or is not writable (network drive, protected system folder) | Leave "Where to save results" empty to use the user data folder (the platform-specific location in 3.9), or pick a writable local folder such as `D:\contract-review-results` (Windows), `/Users/<user>/Documents/contract-review-results` (macOS) or `/home/<user>/contract-review-results` (Linux); check free disk space |
| The scheduled run never fired | EchoDesk was not running (the schedule is driven by a background thread), the time format was invalid and dropped, or "Run on a schedule" is off | Keep EchoDesk running; on Windows put "Start-EchoDesk-v0.2.2.lnk" into `shell:startup` for auto-start (macOS/Linux: your desktop's own login-items or autostart mechanism, since this version ships none); write times as two-digit hours such as `09:00` |
| You want to see what a task actually did (audit trail) | You need to check afterwards who processed which files and when | Open the audit log `app/backend/automation_log.jsonl`: one JSON record per task (see section 9) |
| "no local model is loaded, cannot generate a summary or review opinion" | No local model is selected or downloaded yet | Select one in the main window with `/model list` and `/model <file name>` |
| "playwright is not installed, run pip install playwright first" | Running from source without the dependencies (the packaged build includes them) | For a source checkout run `pip install -r backend/requirements.txt` and restart (on macOS/Linux use `python3 -m pip install -r backend/requirements.txt` if `python`/`pip` is not on PATH; the separators in the path are `/` on those platforms and `\` on Windows) |

---

## 7. Preset templates

The Settings window offers four templates. One click fills in the "Task instruction" and "File types to
process"; adjust them to your own site afterwards. Template text comes from the language packs, so it follows
the interface language.

| Template | Instruction essentials | Common extensions |
| --- | --- | --- |
| Contract financial review | Extract the contract amount, payment method and terms, penalty ratio, both parties and the signing date; flag anything out of policy | `pdf, docx` |
| Invoice data extraction | Extract the invoice number, issue date, amount, tax, buyer, seller and line items | `pdf, ofd` |
| Report key points | Summarize the core conclusions, key figures and recommended actions | `pdf, docx, pptx` |
| Spreadsheet cross-check | Check whether amounts add up to the totals and flag abnormal rows | `xlsx, xls` |

**Saving your own template**: once the "Task instruction" and "File types to process" match your business,
use "Save as custom template", give it a name (for example "Monthly statement check"), and it appears in the
template list for one-click reuse next time (with the message `✅ Template "Monthly statement check" applied`).

**Templates versus config export**: a template only stores reusable text such as the instruction and the
extension list. To move a whole configuration (URL, selectors, results location, ...) to another computer, use
the export feature in section 8.

---

## 8. Exporting and importing a configuration (`.echodesk-auto`)

### 8.1 Export

- The exported file uses the `.echodesk-auto` extension (it is JSON inside, so a text editor can read it);
- By default it is written to your desktop as `echodesk-automation.echodesk-auto`;
- It contains: a format marker (`format: echodesk-automation`), the schema version, the EchoDesk version, the
  export time, the interface language at export time, and the whole `automation` section;
- It **does not contain**: the login state (the cookies in `browser_state.json`), the processed-task record
  (`processed_file`), or the temporary download folder (`download_dir`) — those are either sensitive or tied to
  one machine, so they are excluded. In other words, **the file holds no cookies and no tokens and is safe to
  pass around inside your organisation**.

### 8.2 Import

- Only fields this version knows about are applied; anything else is ignored;
- **For safety, the safety protocol and the automation master switch are never imported**: after importing you
  must accept the protocol on this machine and switch automation on before it can run (message: "For safety the
  imported file does not carry over the safety protocol or the automation master switch; accept them on this
  machine before running.");
- If the file was authored under a **different interface language**, the program says so — for example
  "Note: this configuration was authored with the interface language \"en\" while you are using \"zh-Hans\".
  Values are imported as-is, so please check whether the selectors and instructions need adjusting." — but it
  **does not block the import**, because selectors and instructions are your own text and are meant to stay as
  they are.

---

## 9. Safety and auditing

- **The safety protocol is a precondition**: only after ticking "I have read and understood the safety protocol
  above and accept responsibility for using it" can the automation master switch take effect; otherwise the
  backend forces it back off and logs a message.
- **Everything stays visible**: automation **only runs with a visible browser window** and never in a silent
  headless mode; when a CAPTCHA or two-factor verification appears the run pauses for you and stops the batch
  when the wait times out.
- **Audit log**: by default `app/backend/automation_log.jsonl`, **one JSON record per task**, with the fields
  time, event type (`run_start` / `task` / `run_end` / `manual_required`), task URL, **file names only**
  (never file contents), result (`ok` / `failed` / `skipped`), the (truncated) failure reason, and the safety
  protocol version plus the time you accepted it. Tokens and passwords inside the log are redacted
  automatically; document text, model output and cookies are never written.
- **Where results are written**: only into the results root you configured
  (`results root/date/task name/`), keeping the original attachments, `result.txt` and failure screenshots in that
  folder. Temporary copies used during processing are cleaned up afterwards. With "Where to save results" left
  empty, that root is the **user data folder** — on Windows the outer `EchoDesk-v0.2.2/` folder next to `app/`,
  on macOS `~/Library/Application Support/EchoDesk/`, on Linux `$XDG_DATA_HOME/EchoDesk/` or
  `~/.local/share/EchoDesk/` (see 3.9); set the `ECHODESK_DATA` environment variable to move all user data
  (`ui_state.json`, `snippets.json`, `results/`) elsewhere on any platform.
- **Deduplication record**: `processed_file` (default `automation_processed.json`) stores only hashes of task
  URLs so processed tasks can be skipped next time; it holds no page content.

---

## 10. Sign-in state: sign in once, reuse it afterwards

Having to sign in again on every run makes automation unusable for non-technical users. Automation therefore
keeps your sign-in state in a **browser data directory of its own** and picks it up again on the next run.

### 10.1 What the program does

| Step | What the program does | What you do |
| --- | --- | --- |
| Data directory | Reserved for automation; `browser_profile` by default (next to `browser_state.json`). Change it under "Sign-in state → Data directory" or via `login_state_dir` | Nothing, normally |
| First run | Opens the browser with a visible window and visits the task list page; when it decides you are not signed in it **pauses** and tells you to sign in | Sign in normally in the browser window that opened |
| After signing in | The browser writes cookies / localStorage into the data directory by itself; once the program detects the signed-in state it **carries on automatically** — no restart needed | Nothing |
| Later runs | Reuses the sign-in state in that directory; it **never asks you to sign in again** | Nothing |
| Sign-in expires | It is treated as "not signed in" again, so the program pauses for you once more | Sign in once again |

The data directory is **never shared with your everyday browser**: a browser refuses to let two instances use
the same user data directory, so automation must have its own copy.

### 10.2 How the program decides whether you are signed in

From most to least reliable:

1. **An element that appears only after signing in** (`login_check_selector`) — the most reliable option. Give an
   element that exists only once you are signed in (for example the user name in the corner); if the program
   cannot find it, it treats you as signed out. Use "🎯 Pick visually" to click it on the page.
   Pick an element that **only appears after signing in** — not something present on the sign-in page too
   (a page frame or a footer, for example).
2. **Sign-in page URL keywords** (`login_url_keywords`, default
   `login, signin, sign-in, logon, sso, passport, cas, auth`) — the fallback: if the current address contains one
   of these words, the program assumes it was redirected to a sign-in page.
3. When neither source has any information, the program **does not guess and does not interrupt you**; it only
   writes an "undetermined" line to the audit log.

### 10.3 What happens when the program detects that you are not signed in

- It pauses and shows: `You are not signed in: please sign in manually in the browser window. The program waits
  up to N seconds and continues automatically once you are signed in.` (on a first run the wording mentions
  that the first run needs a manual sign-in);
- After you sign in successfully the program shows `Sign-in detected; continuing.` and carries on by itself;
- If `manual_login_wait_seconds` (180 by default) elapses without a signed-in state, the program reports
  `No signed-in state was detected (waited N seconds), so this run stops here` and ends the run — it does
  **not** let every task fail one after another. That is deliberate: without a sign-in every task would fail,
  and a pile of failures afterwards helps nobody.

### 10.4 Clearing the sign-in state

Settings window → "Sign-in state" → "🗑 Clear automation sign-in state": deletes the data directory (and the old
`browser_state.json` backup), so the next run guides you through signing in once again.

- It cannot be cleared while automation is running (the program asks you to stop first) — the browser is holding
  those files;
- Your everyday browser is unaffected, and no result file is deleted;
- Use this step when you want to switch to a different account.

### 10.5 Several target sites

**One data directory can serve them all**: the browser stores cookies per domain, so site A's sign-in state never
leaks into site B and there is no need to configure each site separately.

### 10.6 Privacy and auditing

- The data directory and the `browser_state.json` backup **stay on this machine**; the program never uploads
  them;
- `browser_state.json` is still exported (and is also used to import cookies once when upgrading from an older
  version, so existing users do not have to sign in again), which makes backup and machine migration easy;
- The audit log records **events only**: `login_state_used` (a stored sign-in state was reused),
  `login_required` (paused for you to sign in), `login_completed`, `login_timeout`, `login_state_cleared`
  (cleared) and `login_state_migrated` (imported). Those records hold a path, a result and counts —
  **never any cookie content**.

### 10.7 Advanced option (not implemented in this version)

Connecting over CDP to a browser you started in debug mode (`connect_over_cdp`): the upside is that it uses your
everyday browser instance, so the sign-in state is simply there; the downside is that you must start the browser
in debug mode yourself every time. This version **does not implement it**; it is described here only.

---

## 11. How the program gets a URL out of an element (href extraction)

### 11.1 First, a common misunderstanding

"Task link position" and "Attachment position on the detail page" hold **selectors**, and a selector has exactly
one job: **find the element**. It does not read attributes. The correct flow has four steps:

1. **Locate** — use the selector to find the `<a>` element;
2. **Read the attribute** — read `href` from that element (the program also tries `data-href` and `data-url` in
   turn, for sites that carry the address on `<div data-href="...">`);
3. **Normalise** — join a relative path (such as `detail.html?id=1`) with the current page origin to get an
   absolute address;
4. **Fall back for special cases** — a pseudo-link cannot be used to navigate, so the program **clicks the
   element** instead.

### 11.2 How the five kinds of href are handled

| What the page contains | What the program does |
| --- | --- |
| Relative path `href="detail.html?id=1"` | Completes it to an absolute address and opens it |
| Absolute address `href="https://…/detail.html?id=2"` | Opens it as-is |
| `href="javascript:void(0)"` | No address can be extracted → **clicks this element** to trigger navigation |
| `href="#"` (or `#section`) | An in-page anchor is not a task or attachment address → **clicks instead** |
| No `href` attribute at all | **Clicks instead** |

In other words **a pseudo-link never makes the program fail** — it switches to a click automatically. Whether the
target page then opens in the current tab or in a new one, the program catches it and carries on. Every case is
explained in the interface while it happens, for example
`This element has no address that can be opened directly (javascript:void(0)); clicking it instead to navigate…`.

### 11.3 Visual picking tells you what you actually picked

After you click an element with "🎯 Pick visually", the program does not just hand you a selector string:

1. **It previews first**: a dialog lists "The program will extract the following addresses from these
   elements: …" so you confirm the right position before it is written into the configuration (press Cancel and
   the selection is discarded, leaving the configuration untouched);
2. **It recognises non-link elements**: if you clicked a button or a `<div>` that carries no address, it says so
   explicitly — `What you picked is not a link (<button>), so no address can be extracted. Please pick again.` —
   and it **does not damage the configuration**. If the element you clicked is a container that holds links, the
   message goes further: `What you picked is a container <div> that holds N link(s); the container itself is not
   a link … Please click one of the links inside it instead.`;
3. **It flags pseudo-links**: if you picked a JavaScript pseudo-link or an empty href, the preview explains that
   `no address can be extracted (JavaScript pseudo-link); the program will click them instead to trigger
   navigation`;
4. **It warns when only one element matched**: visual picking produces a selector that is exact for the one
   element you clicked. If that position is meant to hold several (several tasks in a task list, several
   attachments on a detail page), the preview tells you to make the selector more general (turn
   `li:nth-of-type(1) > a.task` into `a.task`, for example) or to pick a whole row under "Task row container".

### 11.4 What each of the four picking positions means

| Picking position | What the program uses it for | Must it be a link? |
| --- | --- | --- |
| Task link position | Reads `href` as the **navigation target** to open each task | Yes (a pseudo-link switches to a click automatically) |
| Task row container (optional) | Locates a whole row first, then looks for task links **inside** it | No, it is a container by definition |
| Next page button (optional) | Only **clicks** it to page forward; no address is read | No, a button is not a link |
| Attachment position on the detail page | Reads `href` to **download** the attachment | Yes (a pseudo-link switches to a click automatically) |

That "what you picked is not a link" guard only applies to the two positions that **must** be links; when you
pick "Task row container" or "Next page button", picking a container or a button is perfectly normal.

---

## 12. Related documents

- [`USER_GUIDE.md`](USER_GUIDE.md) — user guide: installation, Q&A, long documents, automation overview
- [`FAQ.md`](FAQ.md) — frequently asked questions: startup, models, file parsing, automation, marketplace
- [`SKILL_GUIDE.md`](SKILL_GUIDE.md) — developing and submitting skills (plugins)
- [`../DEVELOPER.md`](../DEVELOPER.md) — developer documentation: how the automation module works and its
  extension points (English and Simplified Chinese)
- [`../PRIVACY.md`](../PRIVACY.md) — privacy statement: network behaviour and data cleanup (English; Chinese
  translation at [`../PRIVACY.zh-Hans.md`](../PRIVACY.zh-Hans.md))
- Chinese: [`../zh-Hans/AUTOMATION_GUIDE.md`](../zh-Hans/AUTOMATION_GUIDE.md)
