# EchoDesk User Guide (v0.2.0)

A hands-on manual for real-world use. For a quick start and troubleshooting, see
[`FAQ.md`](FAQ.md); for field-by-field instructions on the automation settings, see
[`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md).
Chinese version: [`docs/zh-Hans/USER_GUIDE.md`](../zh-Hans/USER_GUIDE.md).

---

## 1. Installation and startup

### Portable version (recommended)
1. Put the whole `EchoDesk-v0.2.0` folder anywhere you like (desktop, D: drive or USB stick all work);
2. Double-click **"EchoDesk 启动.lnk"** inside that folder — this is the only entry point, so you never have
   to work out the difference between `EchoDesk.exe` and `backend\EchoDeskBackend.exe`
   (the latter is launched automatically by the UI; never run it on its own);
3. The first launch handles three things in turn:
   - If there is no model in `backend\models\` → it asks whether to download one (about 508 MB, internet required);
   - If the folder has no shortcut → it asks whether to create one;
   - It shows the onboarding guide once.

### Installed version
Install with the `setup.exe` produced from `tools\installer.iss`, then start it from the Start menu or a
desktop shortcut. The behaviour is exactly the same as the portable version; uninstall it through the
system's "Apps and features".

### Directory layout and the locations you need to know

| Location | Contents | Can I delete it? |
| --- | --- | --- |
| `backend\config.json` | All configuration (model, window, summarization, automation, marketplace) | Yes (recreated with default values) |
| `backend\models\*.gguf` | Local models | Yes (the next launch offers to download them again) |
| `backend\session.json` | The previous conversation (restored automatically) | Yes (clears the history) |
| `backend\browser_state.json` | Automation login state (cookies) | Yes (signs you out everywhere) |
| `backend\automation_log.jsonl` | Automation audit log | Yes (archiving it first is recommended) |
| `backend\skills\` + `skills_registry.json` | Installed skills | Uninstall them from the marketplace instead |
| `ui_state.json` | Window position/size, font size, theme | Yes (resets the interface state) |
| `snippets.json` | Canned phrases | Yes |
| `使用说明.txt` | Structural notes for users | Yes |

**Backup**: if you want to keep your conversation history and settings, just copy the three files
`backend\session.json`, `backend\config.json` and `snippets.json`.

---

## 2. Everyday Q&A

1. Type your question into the input box at the bottom and press `Ctrl+Enter` to send (`Enter` inserts a newline);
2. To interrupt generation: press `Esc` or click ⏹;
3. To copy part of an answer, use the buttons in the bottom-right corner of that message:
   - "📋 Copy" → plain text;
   - "⧉ Markdown" → keeps the original markup (paste it where Markdown is supported and it is re-rendered);
   - "</> Code" → copies code blocks only;
   - You can also simply drag-select the text and press `Ctrl+C`.
4. To copy the whole conversation: `Ctrl+Shift+C`.

### Common commands

```
/help                 all commands
/mode contract        switch to contract-review mode (also /mode 5 or /mode ct)
/file D:\合同.docx     upload a file
/summarize            manually trigger a full-text summary for a long uploaded document
/temp 0.3             adjust the generation temperature
/prompt 你是财务顾问    temporarily change the assistant's role
/search on            turn on web search (searches the web before answering; internet required)
/model list           list the local models / /model 别名 to switch
/status               show the model, window, context usage and KV Cache estimate
/save 会议纪要         export the conversation as Markdown
/undo                 undo the most recent question-and-answer round
/clear                clear the conversation and the file context
/exit                 quit (10-second countdown)
```

---

## 3. Scenario 1: reviewing a long contract

**Goal**: get the local model to answer questions about a contract that runs to dozens of pages, without
losing information because it does not fit in the context window.

1. Press `Ctrl+O`, or just **drag the file into the window** (docx/xlsx/pptx/pdf/txt and more are supported);
2. If the document is very long, EchoDesk first produces a **chunked summary** automatically (you can watch
   "summarizing chunk N/M" in the chat area); when it finishes it reports how many tokens the original text
   was compressed to and gives you an **original-text pointer**;
3. When you ask your question, **mention the file** (for example, "How does this contract provide for payment
   terms?" or "What are the risks of breach of contract?");
4. If the summary is too coarse and you need the details:
   - use `/summarize` to generate the summary again;
   - or split the contract into chapters and upload them one by one;
   - or temporarily raise `context_length` in `backend\config.json` (the larger it is, the slower it runs and
     the more memory it uses; `/status` shows the KV Cache estimate);
5. To keep your conclusions on record: `/save 合同审查意见`.

**Reading the status line**: "context: about X/Y tokens" under `/status` shows current usage and the automatic
summarization threshold (80% of the window).

---

## 4. Scenario 2: batch-reviewing contracts on a website (automation)

**Goal**: a task list in some system holds a number of tasks, each with a contract attachment, and all of them
need to be downloaded one by one with a review opinion generated for each.

### 4.1 One-time configuration (Settings window → Automation)

| Field | What to enter | Example |
| --- | --- | --- |
| Task list URL | The page that lists all tasks | `https://oa.example.com/tasks` |
| Task list element position | CSS or XPath pointing at the task link | `a.task-link` or `//a[@class='task']` |
| Task row container (optional) | Used for paging and row-level targeting | `table.task-table tr` |
| Next page button (optional) | Paging | `a.next-page` |
| File element position on the task detail page | Points at the attachment link | `a.attachment` |
| File extensions to preview | Comma-separated | `pdf, docx, xlsx` |
| Task instruction | The prompt handed to the model | "Extract the key information from the contract and produce a financial review opinion" |

When you are done, click **"Test selectors"**: the program opens a browser and visits that URL, then previews
the text and links of the elements it matched in the log — **confirm that the right elements are being matched
before you start a run**.

### 4.2 Safety agreement
The automation switch only becomes available once you tick "I have read and understood the safety agreement
above" (the backend enforces this). The agreement version and the time you accepted it are recorded in
`backend\automation_log.jsonl`.

### 4.3 Running
- Click "▶ Run once now";
- The browser opens **headed** (you can watch the whole process). For each task it: opens the detail page →
  downloads the files matching your extensions → parses them locally → hands them to the local model to
  produce a summary and a review opinion → **deletes that task's temporary files** → moves on to the next;
- The log refreshes live as it goes (you can also follow it in the chat area of the main window);
- If a **CAPTCHA or two-factor verification** appears, the run pauses and asks you to complete it by hand
  (it waits up to 180 seconds by default);
- To abort: click "⏹ Stop automation" in the Settings window, or return to the main window and press `Esc`;
- A single failing task is **captured in a screenshot as evidence and skipped**, and the batch continues
  (both the screenshot and the failure reason stay in that task's result directory).

### 4.4 Scheduling
Tick "Enable schedule" and enter the times of day it should run (for example `09:00, 18:30`).
Scheduled tasks are fired by a backend thread while the program is running. If you want them to run at
startup, copy "EchoDesk 启动.lnk" into the system Startup folder (`Win+R` → `shell:startup`).

### 4.5 Login state
The first time, you need to log in manually once in the browser; the program stores the login state in
`backend\browser_state.json` and reuses it from then on. Delete that file to sign out of everything.

---

## 5. Scenario 3: installing skills to extend what EchoDesk can do

1. Press `Ctrl+M` to open the **skill marketplace** in the Settings window;
2. Browse the list, or type a keyword to search (the search runs automatically 300 ms after you type); you can
   switch between "Most stars / Recently updated / Best match";
3. Card information:
   - `✔ Verified` = the repository root contains a valid `skill.json` (including `echodesk.bundle`);
   - `⚠ Unverified` = the structure does not comply, and **the install button is greyed out**;
   - `● Installed` = already installed on this machine; the button becomes "Uninstall";
4. Click "Details" to see the README summary, version, dependencies, entry file and whether it ships a
   prepare script;
5. Clicking "Install" opens a confirmation dialog listing the skill name, version, source link and safety
   notes — once you confirm, the source is downloaded, the manifest is verified and the registry is updated.
   **EchoDesk never runs third-party scripts automatically**; if a skill declares extra dependencies they are
   listed in the prompt, and it is your decision whether to install them by hand;
6. The "Installed" button shows the list of skills on this machine.

Where skills come from: **discovery is limited to the GitHub topic `echodesk-plugin`**.
Getting no results usually means that no repository carries that topic at the moment, not that the program is
broken (see the FAQ for details).

---

## 6. Networking and privacy at a glance

| Feature | Network access | What is sent |
| --- | --- | --- |
| Ordinary Q&A, file parsing, long-document summarization | ❌ Fully local | — |
| Model download | ✅ Only after you agree | Downloads only; nothing is uploaded |
| Web search (`/search on`) | ✅ After you turn it on by hand | The keywords of that question → DuckDuckGo |
| Skill marketplace | ✅ When you open the marketplace | Search keywords → GitHub API |
| Automation | ✅ Visits the URLs you enter | Operates only within the sites you configured |

Beyond the above there is **no telemetry, no tracking and no crash reporting** of any kind.
See `docs/PRIVACY.md` for details.

---

## 7. Keyboard and productivity

| Shortcut | Action |
| --- | --- |
| `Ctrl+Enter` | Send |
| `Enter` / `Shift+Enter` | New line |
| `Esc` | Stop generation / close the command list |
| `Ctrl+L` | Clear the conversation |
| `Ctrl+O` | Upload a file |
| `Ctrl+Shift+C` | Copy the whole conversation |
| `Ctrl+滚轮` / `Ctrl+加/减/0` | Adjust font size / reset |
| `Ctrl+T` | Toggle light and dark theme |
| `Ctrl+,` | Open Settings |
| `Ctrl+M` | Open the skill marketplace |

**Command palette**: typing `/` brings up the command list; use `↑↓` to select, `Tab` to complete and `Enter`
to run. The "+" in the bottom-left corner moves the `/xxx` in the input box onto its own command input line.

**Canned phrases**: click "📋 Open canned phrase picker" to insert a template in one click;
add one with `/snippet add 名称 文本`, list them with `/snippet list` and delete one with
`/snippet remove 名称`.
