# EchoDesk Automation Configuration Guide

This guide walks you through setting up browser automation: letting EchoDesk open your organisation's web
pages, click through each task, download the attachments, hand them to the local model to produce a review,
and file the results away **neatly** in folders.

No programming and no web knowledge is required. Wherever a bit of background helps, the guide gives the
concrete "click here, type this" route.

Chinese version: [`docs/zh-Hans/AUTOMATION_GUIDE.md`](../zh-Hans/AUTOMATION_GUIDE.md).
Related documents: [User guide](USER_GUIDE.md) · [FAQ](FAQ.md) · [Skill development guide](SKILL_GUIDE.md).

---

## 1. Before you start

### 1.1 Three things you need

| Requirement | Why | How to check |
| --- | --- | --- |
| Microsoft Edge is installed | Automation opens pages with the Edge on your machine (`browser_channel` defaults to `msedge`); headless mode is never used | If Edge opens normally, you are fine |
| A local model is downloaded | Producing the review needs the local model; without one you get "no local model is loaded, cannot generate a summary or review opinion" | `/status` in the main window shows the loaded model |
| Your machine can reach that web system | Including login, permissions, intranet and VPN; automation only clicks for you, it cannot reach anything you cannot reach | Open the task list page once in Edge by hand and confirm you can see the tasks |

If the attachments are legacy `.doc` / `.ppt` files, or the documents are encrypted, the machine also needs
**WPS or Microsoft Office** installed (used for COM-based reading).

### 1.2 What automation actually does

After you click "▶ Run once now", the program:

1. **Opens the browser with a visible window** (you can watch the whole time) and visits the task list URL you entered;
2. Finds every task link using "Task link position", and turns pages automatically using "Next page button" (if you filled it in);
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

> 📷 Screenshot placeholder: the whole Automation tab of the Settings window, showing the safety protocol box, the positions of the fields, the "Test selectors" button on the right, and the "▶ Run once now / ⏹ Stop automation / run log" area at the bottom.

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
| Result folder name `task_name` | Optional | The name of the third-level result folder, i.e. what this batch is called | `自动化任务` ("automation task") |
| Result root folder name `results_root_name` | Optional | The first-level folder inside the results root | `批量处理结果` ("batch results") |
| Where to save results `results_location` | Optional | Put results on another drive or a shared folder; empty = the program folder | empty (= program folder) |
| Run on a schedule `schedule_enabled` | Optional | Runs a batch automatically at fixed times every day | `false` |
| Scheduled times `schedule_times` | Optional | The times of day, separated by commas | `09:00` |

### 2.3 Advanced fields (only editable in `backend\config.json`)

These fields have no input box in the Settings window yet. **Close EchoDesk before editing**, then save and
restart for the change to take effect (the file is UTF-8; Notepad or VS Code both work).

| Config field | Required? | Purpose | Default |
| --- | --- | --- | --- |
| `llm_temperature` | Optional | Generation temperature: higher is more varied, lower is more conservative; 0.2–0.4 suits review work | `0.3` (0.0–1.0) |
| `llm_max_tokens` | Optional | Maximum length of one review opinion in tokens; longer output is slower | `512` (64–4096) |
| `wait_network_idle` | Optional | Waits until network requests stop before acting: steadier, a little slower | `true` |
| `download_dir` | Optional | Attachments are downloaded into this temporary folder and cleaned up afterwards | empty (= `echodesk_downloads` under the system temp folder) |
| `processed_file` | Optional | Where the processed-task record lives (used by "Skip tasks already processed") | `automation_processed.json` |
| `login_state_file` | Optional | Where the login state is stored (cookies / localStorage) | `browser_state.json` |
| `manual_login_wait_seconds` | Optional | How long the run waits for you when a CAPTCHA or login appears | `180` (0–3600) |
| `headless` | Read-only | Automation **only supports a visible browser**; writing `true` is corrected back to `false` with a warning | `false` |
| `browser_channel` | Optional | Which browser opens the pages; the system Edge is the default | `msedge` |
| `audit_log_file` | Optional | Audit log file: one JSON record per task | `automation_log.jsonl` |
| `start_with_windows` | Optional | Launch EchoDesk automatically at sign-in (scheduled runs need the program running) | `false` |
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

> 📷 Screenshot placeholder: picking the task name with "🎯 Pick on page" on the task list page — capture the red highlight box around the hovered element, the selector preview tooltip following the cursor, and the blue banner at the top reading "move the mouse onto the element to capture and click; press Esc to cancel".

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

### 3.8 Result folder name (optional)

- **In plain words**: the name used for the result folder. It defaults to the task name and usually needs no change.
- **Example**: `Contract review`
- **What it does**: whatever you type becomes the third-level folder name. **Duplicate names on the same day get a number** (`合同审查`, `合同审查-1`, `合同审查-2`), so an earlier batch is never overwritten.
- **Left empty**: the default name `自动化任务` is used.

### 3.9 Where the results go

| Field | In plain words | Example |
| --- | --- | --- |
| Result root folder name | What the first-level folder under the results root is called | `批量处理结果` |
| Where to save results | Fill this in to put results on another drive or a shared folder; empty means the program folder | `D:\contract-review-results` |

The window previews the full structure live, for example:
`Result root / YYYY-MM-DD / task name / 结果.txt + original files`.
Click "Open results folder" to open the results root in File Explorer; click "Browse…" to pick a folder as the results location.

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
- To start EchoDesk with Windows, put the "EchoDesk 启动.lnk" shortcut into the system Startup folder (`Win+R` → `shell:startup`), or set `start_with_windows` to `true` in `backend\config.json`.

### 3.12 Safety protocol and the automation master switch

- The safety protocol comes first: tick "I have read and understood the safety protocol above and accept responsibility for using it";
- While it is unticked the master switch is **forced off** by the backend, with a message in the log;
- The protocol version and the time you accepted it are written to the audit log for later tracing.

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
| Result root folder name | `批量处理结果` |
| Where to save results | Empty (= the program folder); use `D:\contract-review-results` to put it on drive D |
| Maximum tasks / page timeout / retries | `20` / `30000` / `1` |
| Skip tasks already processed, Save result files | both ticked |

### 4.2 The equivalent `config.json` fragment

The real structure inside `backend\config.json` (the `automation` section; you can edit it directly, but close
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
    "results_root_name": "批量处理结果",
    "results_location": "",
    "task_name": "Contract review",
    "audit_log_file": "automation_log.jsonl",
    "schedule_enabled": false,
    "schedule_times": ["09:00"],
    "start_with_windows": false
  }
}
```

A note on folder names: the default value of `results_root_name` is the Chinese word `批量处理结果`
("batch results") so that Chinese and English installs produce the same paths. You may rename it — for
example to `batch-results` — but keep it free of characters Windows forbids in file names.

### 4.3 What the results folder looks like afterwards

With "Where to save results" left empty, the results land in the program folder:

```
<program folder>\
└── 批量处理结果\                        ← result root folder name (customisable)
    └── 2026-09-23\                     ← the run date (several runs on one day share this level)
        └── Contract review\            ← the result folder name (duplicates get -1, -2)
            ├── 年度采购合同.pdf           ← the original attachments that were downloaded
            ├── 补充协议.docx
            ├── 结果.txt                  ← every task of this run (plain text)
            └── 失败截图_任务2_20260923_101010.png   ← only when a task failed
```

`结果.txt` ("results.txt") is **plain text** — Notepad shows it directly, with no `#`, `**` or `|` markers.
It looks roughly like this:

```text
EchoDesk 批量处理结果
========================================
任务名称：Contract review
运行日期：2026-09-23
开始时间：2026-09-23T09:12:03
任务列表：https://oa.example.com/task/list
任务指令：Extract the contract amount, payment method and terms, penalty ratio, both parties and the signing date; flag anything out of policy
结果目录：<program folder>\批量处理结果\2026-09-23\Contract review

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
失败截图：失败截图_任务2_20260923_101010.png

自动化完成：成功 1 个，跳过 0 个，失败 1 个（共发现 3 个任务，处理上限 20 个）。
```

The report header and the per-task lines are produced by the backend, so they stay in Chinese
(`任务名称:` = "task name", `运行日期:` = "run date", `附件:` = "attachments", `审核意见:` = "review opinion",
`失败原因:` = "failure reason", `自动化完成:` = "automation finished"). Only the file names and the
instruction text come from your own configuration.

> 📷 Screenshot placeholder: File Explorer showing the three-level "批量处理结果\date\task name" folders, making clear that the original attachments and `结果.txt` sit in the same folder.

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

> 📷 Screenshot placeholder: step 1 of the "Test selectors" wizard succeeding — capture the green success message, the match count and the sample task names, plus the "Next step" button.

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
| Parsing `.doc` / `.ppt` fails or asks for WPS | Legacy binary formats (and encrypted documents) are read through COM, and WPS/Office is not installed | Install WPS or Microsoft Office, or save the file as `.docx` / `.pptx` first |
| The model is too slow, or reports "the model returned an empty result" | The task instruction is too long, `llm_max_tokens` is too large, or a long attachment triggered chunked summarisation | Trim the instruction to four to six items; set `llm_max_tokens` to 384–512; switch to a smaller quantised model (such as Q4_K_M) |
| The results folder is not created, or writing fails | "Where to save results" points at a path that does not exist or is not writable (network drive, protected system folder) | Leave "Where to save results" empty to use the program folder, or pick a writable local folder such as `D:\contract-review-results`; check free disk space |
| The scheduled run never fired | EchoDesk was not running (the schedule is driven by a background thread), the time format was invalid and dropped, or "Run on a schedule" is off | Keep EchoDesk running; put "EchoDesk 启动.lnk" into `shell:startup` for auto-start; write times as two-digit hours such as `09:00` |
| You want to see what a task actually did (audit trail) | You need to check afterwards who processed which files and when | Open the audit log `backend\automation_log.jsonl`: one JSON record per task (see section 9) |
| "no local model is loaded, cannot generate a summary or review opinion" | No local model is selected or downloaded yet | Select one in the main window with `/model list` and `/model <file name>` |
| "playwright is not installed, run pip install playwright first" | Running from source without the dependencies (the packaged build includes them) | For a source checkout run `pip install -r backend\requirements.txt` and restart |

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
- **Audit log**: by default `backend\automation_log.jsonl`, **one JSON record per task**, with the fields
  time, event type (`run_start` / `task` / `run_end` / `manual_required`), task URL, **file names only**
  (never file contents), result (`ok` / `failed` / `skipped`), the (truncated) failure reason, and the safety
  protocol version plus the time you accepted it. Tokens and passwords inside the log are redacted
  automatically; document text, model output and cookies are never written.
- **Where results are written**: only into the results root you configured
  (`results root\date\task name\`), keeping the original attachments, `结果.txt` and failure screenshots in that
  folder. Temporary copies used during processing are cleaned up afterwards.
- **Deduplication record**: `processed_file` (default `automation_processed.json`) stores only hashes of task
  URLs so processed tasks can be skipped next time; it holds no page content.

---

## 10. Related documents

- [`USER_GUIDE.md`](USER_GUIDE.md) — user guide: installation, Q&A, long documents, automation overview
- [`FAQ.md`](FAQ.md) — frequently asked questions: startup, models, file parsing, automation, marketplace
- [`SKILL_GUIDE.md`](SKILL_GUIDE.md) — developing and submitting skills (plugins)
- [`../DEVELOPER.md`](../DEVELOPER.md) — developer documentation: how the automation module works and its extension points (Chinese)
- [`../PRIVACY.md`](../PRIVACY.md) — privacy statement: network behaviour and data cleanup (Chinese)
- Chinese: [`../zh-Hans/AUTOMATION_GUIDE.md`](../zh-Hans/AUTOMATION_GUIDE.md)
