# EchoDesk User Guide (v0.2.1)

A hands-on manual for real-world use. For a quick start and troubleshooting, see
[`FAQ.md`](FAQ.md); for field-by-field instructions on the automation settings, see
[`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md).
Chinese version: [`docs/zh-Hans/USER_GUIDE.md`](../zh-Hans/USER_GUIDE.md).

---

## 1. Installation and startup

### Installed version (recommended)
1. Double-click `EchoDesk-v0.2.1-Setup.exe` (the `-NoModel-` build is the same program without the bundled
   model). It needs no administrator rights.
2. **By default it installs into the folder the installer itself sits in** — so a copy of the installer
   dropped into `D:\Tools\EchoDesk\` installs into `D:\Tools\EchoDesk\`. The wizard shows the path page, so
   change it there if you would rather install somewhere else.
3. The wizard creates the Start Menu shortcut, and a desktop shortcut if you tick that box, then offers to
   launch the program. Uninstall later through the system's "Apps and features" (or the Start Menu entry).

The installed layout is **exactly the same** as the portable build: the program lives in `app\`, and your
data and `results\` stay in the install folder.

### Portable version (plain zip)
1. Unzip the whole `EchoDesk-v0.2.1` folder anywhere you like (desktop, D: drive or USB stick all work) —
   nothing is written outside it, and deleting the folder uninstalls it;
2. Double-click **"Start-EchoDesk-v0.2.1.lnk"** inside that folder — this is the only entry point, so you never have
   to work out the difference between `app\EchoDesk.exe` and `app\backend\EchoDeskBackend.exe`
   (the latter is launched automatically by the UI; never run it on its own).
   The zip contains no shortcut (a shortcut stores an absolute path, so one created on the maintainer's
   machine would be dead on yours) — run `install-complete.bat` once, or launch `app\EchoDesk.exe` and
   accept the offer to create a shortcut;
3. The first launch handles three things in turn:
   - If there is no model in `app\backend\models\` → it asks whether to download one (about 508 MB, internet required);
   - If the folder has no shortcut → it asks whether to create one;
   - It shows the onboarding guide once.

**Two layers**: the outer folder (`EchoDesk-v0.2.1\`) holds only the shortcut, your user data and the docs,
while **the program itself lives entirely in `app\`** — no exe/dll ever shows up in the outer folder, and
uninstalling simply means deleting the whole folder.

### Directory layout and the locations you need to know

The paths below are relative to the portable **outer** folder (`EchoDesk-v0.2.1\`). **`app\` is the program
itself and is not meant for your own files; the outer folder is your data directory**, so back up and clean up
from there.

| Location | Contents | Can I delete it? |
| --- | --- | --- |
| `app\backend\config.json` | All configuration (model, window, summarization, automation, marketplace) | Yes (recreated with default values) |
| `app\backend\models\*.gguf` | Local models | Yes (the next launch offers to download them again) |
| `app\backend\session.json` | The previous conversation (restored automatically) | Yes (clears the history) |
| `app\backend\browser_state.json` | Automation login state (cookies) | Yes (signs you out everywhere) |
| `app\backend\automation_log.jsonl` | Automation audit log | Yes (archiving it first is recommended) |
| `app\backend\skills\` + `app\backend\skills_registry.json` | Installed skills | Uninstall them from the marketplace instead |
| `app\backend\summary_cache.json` | Chunked-summary cache for long documents (readable JSON) | Yes (recomputed on the next upload) |
| `results\` | Automation results (layered as `date\task name\`) | Yes (archiving them first is recommended) |
| `lang\` + `app\lang\ui\` | Backend / UI language packs (add your own languages here) | Better not (deleting them falls back to the built-in text) |
| `ui_state.json` | Window position/size, font size, theme | Yes (resets the interface state) |
| `snippets.json` | Canned phrases | Yes |
| `USAGE.txt` | Structural notes for users | Yes |

**Backup**: if you want to keep your conversation history and settings, just copy the three files
`app\backend\session.json`, `app\backend\config.json` and `snippets.json`.

**Where your data lives**: `ui_state.json`, `snippets.json` and `results\` all sit in the portable
**outer** folder (not inside `app\`). To put them somewhere else instead (a USB stick or a user folder, say),
set the `ECHODESK_DATA` environment variable to the target directory before starting; the program writes user
data there.

### Starting without a model

EchoDesk does not ship model weights. If none is present the app still starts and stays usable — nothing
exits, nothing blocks, and nothing is downloaded without you asking:

1. The status bar shows **"no model"** and the chat shows guidance with the real path of the models folder.
2. Everything that does not need inference still works: file parsing, settings, the skill market, the model
   market and your session history. Asking a question returns the guidance instead of an error.
3. Get a model in any of three ways — **none of them requires restarting the program**:
   * **Settings → Model → Model market** → search and download. The list shows all GGUF repositories
     (nothing is hidden by hardware filtering); entries that suit this machine are marked "(Recommended)"
     and the repository of the model named in `config.json` is pinned first.
   * Copy a `.gguf` file into `app\backend\models\`, then click **Rescan models folder**. If exactly one
     model is found it is loaded automatically.
   * Click **Load** next to an already-downloaded model in the local model library.
4. When the model is loaded the status bar switches from "no model" to the model name and you can chat
   right away. If loading fails you get the reason and can retry — the app keeps running either way.

### While a long document is being summarised

Long documents are summarised in segments. The chat shows live progress instead of going quiet: the current
phase, which segment of how many is running, how long it has taken, and how much text the current segment
has produced. A segment that is merely slow is labelled slow and keeps going; the stop button aborts the run
immediately and leaves the original text in context (no half-finished summary is injected), so you can run
`/summarize` again whenever you like.

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
/task list            task-oriented prompt templates (summary, contract review, extraction, translation, code debugging, reasoning)
/preset light         generation presets in three steps: light / balanced / quality
/compact              compress the context right away to free up room
/darkmode auto        theme: on = dark, off = light, auto = follow the system
/status               show the model, window, context usage, KV Cache estimate and model capability (size / quantization / measured speed)
/about                version, publisher and the local paths of the compliance documents
/save 会议纪要         export the conversation as Markdown
/undo                 undo the most recent question-and-answer round
/clear                clear the conversation and the file context
/exit                 quit (10-second countdown)
```

### Getting more out of a small local model

- **Task templates**: `/task list` shows six templates — summary (`summary`), contract review (`contract`),
  structured extraction (`extract`), translation (`translate`), code explanation and debugging (`code`) and
  step-by-step reasoning (`reason`). Apply one with `/task contract` (a prefix is enough, e.g. `/task con`),
  and clear it with `/task off`. A template is **layered on top of the current mode**: the mode sets the scene
  and tone, the template sets this task's output format, and where they differ the template wins (the prompt
  states that precedence explicitly). `/prompt <text>` has the highest priority and overrides both — run
  `/prompt reset` to let the template take effect again. `/task off` clears only the template and leaves your
  custom prompt alone.
- **Reply language**: it follows the interface language. A Chinese interface asks for Simplified Chinese, an
  English interface asks for English; if you add another language pack, the model replies in whatever language
  you write in. The prompt text itself stays Chinese, which does not affect this.
- **Every mode carries a complete prompt**: each of the six modes states its role, output format, reasoning
  steps, one input/output example and its tone, and they all share the same "boundaries and output discipline"
  block (say you do not know instead of inventing, ask one clarifying question when the request is too thin,
  never fabricate sources).
- **Generation presets**: `/preset list` shows them; `light` (shortest, fastest replies), `balanced` (the
  default) and `quality` (longest replies) switch the temperature, top_p, repeat penalty and reply-length cap
  in one step. The choice is written into the configuration and survives a restart; if you later change the
  temperature by hand with `/temp`, your manual value wins.
- **Compressing the context**: `/compact` turns earlier conversation into a summary right away to free up room
  (with `context_mode` set to `slide` it discards the oldest history instead); you no longer have to wait for
  the automatic trigger above 80%.
- **Being asked a follow-up question is normal**: when a question is too vague and there is neither a file
  context nor any history (just "what about this?"), the program asks you to clarify the object, the expected
  output form and any constraints instead of guessing at an answer. To switch that off, set `clarify_vague` to
  `false` in `app\backend\config.json`.

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
   - or temporarily raise `context_length` in `app\backend\config.json` (the larger it is, the slower it runs and
     the more memory it uses; `/status` shows the KV Cache estimate);
5. To keep your conclusions on record: `/save 合同审查意见`.

**Reading the status line**: "context: about X/Y tokens" under `/status` shows current usage and the automatic
summarization threshold (80% of the window).

**Summaries are cached**: uploading the same document a second time reuses the previous chunked summary
instead of recomputing it, and the cache expires automatically once the file changes. The cache lives in
`app\backend\summary_cache.json` (readable JSON — delete it to clear it), and you can switch caching off with
`summary_cache: false` in `app\backend\config.json`.

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
`app\backend\automation_log.jsonl`.

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
startup, copy "Start-EchoDesk-v0.2.1.lnk" into the system Startup folder (`Win+R` → `shell:startup`).

### 4.5 Login state
The first time, you need to log in manually once in the browser; the program stores the login state in
`app\backend\browser_state.json` and reuses it from then on. Delete that file to sign out of everything.

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

### Local plugins and developer mode

The **developer mode switch** on the "Developer" page of the Settings window works in both directions: off
means off, and it stays off after a restart.

- Turning developer mode off **unloads the development plugins** and removes them from the command list, and
  clears the development log `app\backend\skills_dev.log`;
- The first time **third-party local plugins** are loaded (they live in `app\backend\skills_dev\`) you get
  **one** summary confirmation dialog: it lists each plugin's source, path, declared capabilities and
  permissions, and lets you **tick them one by one, enable all or disable all**. Your choices are remembered,
  so a plugin you refused is never asked about again;
- **Bundled example plugins** (Hello World and the OFD reader under `examples\skills\`) are enabled by default
  and are never asked about.

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

**Compliance documents**: `/about` shows the version, the publisher and the **actual local paths** of these
files — `LICENSE` and `NOTICE` in the outer folder, plus `docs\PRIVACY.md`, `docs\EULA.md`,
`docs\THIRD_PARTY_LICENSES.md` and `docs\SBOM.json`. The full set also includes `docs\SECURITY.md`,
`docs\zh-Hans\COMPLIANCE.md` and `docs\en\COMPLIANCE.md`.

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
| `Ctrl+mouse wheel` / `Ctrl+plus/minus/0` | Adjust font size / reset |
| `Ctrl+T` | Switch between the light and dark themes (the three-way choice lives in Settings) |
| `Ctrl+,` | Open Settings |
| `Ctrl+M` | Open the skill marketplace |

**Theme (three-way)**: the "General" page of the Settings window offers **Follow the system / Light / Dark**.
"Follow the system" tracks the Windows app mode, so the interface changes the moment you switch light/dark in
Windows. `Ctrl+T` flips between light and dark, and `/darkmode on|off|auto` sets it directly.

**Emoji display (three-way)**: the same "General" page offers **Auto / Always / Never**. With "Auto", if this
machine has no emoji font the interface falls back to plain-text markers (`[OK]` / `[!]`) instead of showing
empty boxes.

**Status strip**: the row under the input box refreshes live — web search, automation, the skill marketplace,
context usage, the KV Cache estimate, and the **model capability item** (size · quantization · usable window ·
measured generation speed):

- Clicking the capability item opens "Settings → Model" with the full capability details and the recommended
  configuration;
- The capability-hint button appears only when a model capability limit is actually hit (a small usable window,
  very slow generation, a KV Cache estimate above half of the machine's memory, and so on); opening it lists
  each reason together with the action you can take;
- "🗜 Compress context" is the same as running `/compact` once.

**Command palette**: typing `/` brings up the command list; use `↑↓` to select, `Tab` to complete and `Enter`
to run. The "+" in the bottom-left corner moves the `/xxx` in the input box onto its own command input line.

**Canned phrases**: click "📋 Open canned phrase picker" to insert a template in one click;
add one with `/snippet add 名称 文本`, list them with `/snippet list` and delete one with
`/snippet remove 名称`.
