# EchoDesk User Guide (v0.2.1)

A hands-on manual for real-world use. For a quick start and troubleshooting, see
[`FAQ.md`](FAQ.md); for field-by-field instructions on the automation settings, see
[`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md).
Chinese version: [`docs/zh-Hans/USER_GUIDE.md`](../zh-Hans/USER_GUIDE.md).

---

## 1. Installation and startup

### Installed version (recommended)
1. Double-click `EchoDesk-v0.2.1-Setup.exe` (there is only this one installer; it does **not** bundle a
   model, and asks on first launch whether to download one). It needs no administrator rights.
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
   - If there is no model in `app\backend\models\` → it asks whether to download one (about 472 MB, internet required);
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
| `app\backend\config.json` | All configuration (model, window, summarization, **local retrieval**, **agent tool permissions**, **MCP**, automation, marketplace) | Yes (recreated with default values) |
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
3. Get a model in any of four ways — **none of them requires restarting the program**:
   * **Settings → Model → Model tiers** → click a chip to **switch** to that tier (the small
     `Qwen3-0.6B-Q6_K` tier is the default). Clicking a chip you have not downloaded yet downloads it
     first — the app resolves the file inside that tier's own repository — and then you can switch to it.
   * **Settings → Model → Model market** → search and download. See "How to use the model market" below.
   * Copy a `.gguf` file into `app\backend\models\`, then click **Rescan models folder**. If exactly one
     model is found it is loaded automatically.
   * Click **Load** next to an already-downloaded model in the local model library.
4. When the model is loaded the status bar switches from "no model" to the model name and you can chat
   right away. If loading fails you get the reason and can retry — the app keeps running either way.

### How to use the model market

On the **Settings → Model** tab, type a keyword into the search box and press Enter (or click **🔍 Search**).

* **All results are fetched in one go**: the app pages through the remote source itself until every result for
  that keyword is loaded (no more "only 20 shown"), showing progress such as "N loaded". If it takes too long,
  click **⏹ Stop** — whatever has already been fetched is kept.
* **It cannot run away with you**: each load is bounded by three caps (item count, request count, elapsed
  time). When one is hit the app tells you "N loaded, more available" and shows a **⤵ Load more** button.
* **Results are cached**: searching the same keyword again reads the local cache (instant). Use **↻ Refresh**
  to force a fresh fetch.
* **The paging bar is fixed below the list** and does not scroll away with the results: ⏮ First / ◀ Prev /
  Next ▶ / Last ⏭ / jump to page N (Enter to apply, with a hint on invalid input) / 20, 50 or 100 per page.
* **Local filtering**: typing in the filter box narrows the **already-loaded** results in real time
  (repository name / description / tags) and **never touches the network**. You can also multi-select
  parameter size, quantisation format, update time, downloaded state and recommended-only. Within one
  dimension the selected values are OR-ed; across dimensions they are AND-ed. Filters and sorting survive
  page changes.
* **Three numbers, always visible**: the status line reads "A after local filter / B loaded / C in total".
  When local filtering matches nothing you get a **Search remote again** entry instead of a dead end.
* **Sorting**: all-time downloads / most likes / recently updated / parameter size. Switching the sort
  refetches results for that dimension.
* **Download metric**: the list shows **all-time downloads**, which is *not* the same figure as the
  "downloads last 30 days" that the Hugging Face website shows by default — the status line states the metric.
  If an entry has no all-time field, it falls back to "last 30 days" and is labelled individually rather
  than showing 0.
* **The default model is pinned at the top**: the repository of the model configured in `config.json` is
  always shown above the results, labelled with its source, and is **not counted in the paging totals**.
* **Offline**: when the remote source is unreachable the status line shows "📴 Offline mode" and the last
  cached results plus locally downloaded models are shown instead.

### While a long document is being summarised

Long documents are summarised in segments. The chat shows live progress instead of going quiet: the current
phase, which segment of how many is running, how long it has taken, and how much text the current segment
has produced. A segment that is merely slow is labelled slow and keeps going; the stop button aborts the run
immediately and leaves the original text in context (no half-finished summary is injected), so you can run
`/summarize` again whenever you like.

### Thinking models: reasoning and answer are shown separately

If you use a **reasoning model** (Qwen3 thinking mode, the DeepSeek-R1 family, and so on), the model thinks
first and answers afterwards. EchoDesk keeps the two apart:

* the answer area holds **only the final answer** — no more `<think>…</think>` reasoning tags mixed into it;
* above the answer sits a collapsed **“🧠 Thinking”** region; click its header to expand or collapse it;
* while streaming, the thinking region **expands automatically** so you can watch the model reason, and it
  **collapses again as soon as the answer starts**. If you expanded it yourself, it is not forced shut;
* when the model produced no reasoning this time, no empty region appears at all.

Two rules worth knowing:

1. **Thinking is never sent back as context.** It is display and history only. That saves context window
   (thinking is often longer than the answer) and matches how these models are meant to be used —
   DeepSeek-R1's own chat template keeps only what follows `</think>`.
2. **Thinking is saved with the conversation**, so reopening EchoDesk and restoring the session brings the
   thinking regions back (collapsed). Set `reasoning.persist` in `config.json` to change that.

Two related notes:

* The copy buttons (📋 / ⧉ / </> and `Ctrl+Shift+C`) copy **only the answer**; thinking is not dragged along.
  To copy thinking, drag-select inside the expanded region, or use the `/save` export, where thinking is
  written as a collapsible block.
* If a model “thinks too long to answer”, the answer area says that **only thinking was produced**. That
  usually means the generation length limit ran out: switch to the quality preset with `/preset quality`
  (2048 tokens), raise `max_tokens` in `config.json` (default 1024), or ask a more specific question.

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
/mode list            three modes: chat / contract (clause review) / agent (can call tools) — also /mode 1|2|3 or /mode c|ct|ag
/mode agent           switch to agent mode (also /mode 3, /mode ag or /agent on)
/file D:\合同.docx     upload a file (several paths at once are allowed; quote paths containing spaces)
/file "D:\my contract.docx" "D:\annex 1.xlsx"   load several files in one go
/summarize            manually trigger a full-text summary for a long uploaded document
/temp 0.3             adjust the generation temperature
/prompt 你是财务顾问    temporarily change the assistant's role
/search on            turn on web search (searches the web before answering; internet required)
/agent list           see which tools the agent may call and which are authorised
/agent allow web_search   authorise a sensitive tool (written to config.json, persists)
/agent steps 5        force a conclusion after at most 5 tool calls (1–8, default 4)
/mcp                  show MCP server status and tools; /mcp connect fetches the tool list now
/model list           list the local models / /model 别名 to switch
/task list            task-oriented prompt templates (8 in total: summary, contract review, extraction, translation, code debugging, reasoning, creative writing, requirement clarification)
/preset light         generation presets in three steps: light / balanced / quality
/compact              compress the context right away to free up room
/compact focus on 合同金额   compress while keeping the focus you name
/darkmode auto        theme: on = dark, off = light, auto = follow the system
/status               show the model, window, context usage, KV Cache estimate and model capability (size / quantization / measured speed)
/about                version, publisher and the local paths of the compliance documents
/save 会议纪要         export the conversation as Markdown
/undo                 undo the most recent question-and-answer round
/clear                clear the conversation and the file context
/exit                 quit (10-second countdown)
```

### Conversation modes: three modes, and the old spellings still work

There are now **three modes**, numbered 1/2/3 with the short names c/ct/ag:

| Mode | What it is for | Temperature preset |
| --- | --- | --- |
| `chat` (default) | general conversation: office Q&A, explanations, writing, translation, … | 0.7 |
| `contract` | clause review: each item as "verbatim excerpt → risk → suggested change", with a mandatory human-review note on money/liability conclusions | 0.3 |
| `agent` | an agent that decides which tool to call and in what order until it reaches a conclusion (see "6. Agent: letting the model call tools") | 0.5 |

Every mode still spells out the **six elements** (role / output format / level of detail / reasoning /
example / tone) and they all share the same general requirements (say you do not know instead of inventing,
answer in one sentence when one sentence is enough, give the most likely answer first and ask afterwards
when information is missing, never fabricate sources).

**The modes that were merged away lost no capability — `/task` templates carry them now.** The old
spellings still work: the program switches to chat and applies the matching template for you, the reply
says "merged into another mode, template /task xxx applied automatically", and the old temperature preset
is kept (run `/task off` to drop the template):

| Old spelling | Now equivalent to | Old temperature preset kept |
| --- | --- | --- |
| `/mode general` | `chat` (a rename only) | — |
| `/mode summary` | chat + `/task summary` | 0.4 |
| `/mode translate` | chat + `/task translate` | 0.3 |
| `/mode code` | chat + `/task code` | 0.2 |
| `/mode creative` | chat + `/task creative` | 0.9 |
| `/mode guide` | chat + `/task guide` | 0.6 |
| `/mode contract` | unchanged — still its own contract mode | 0.3 |

**The old numbers are no longer migrated automatically — you get a clear error instead**: a number means
different things under the two numbering schemes (the old `/mode 2` was summary, the new `/mode 2` is
contract), so numbers such as 4/5/6/7 are not guessed at. The program replies "the numbering changed: it is
now 1=chat, 2=contract, 3=agent; the old number 4 meant something else, use /task code instead", which keeps
you out of a state that is neither the old nor the new meaning. **Write the name** (`/mode summary`) or
switch first and then apply the template (`/mode chat` followed by `/task code`).

The task templates grew from 6 to **8** (`creative` for creative writing and `guide` for requirement
clarification are new); `/task list` shows them all.

### Live metrics in the status strip

While a reply is being generated the status strip shows the measured metrics for that run. They all come from
the backend; the UI never estimates them:

| Display | Meaning |
| --- | --- |
| `⏳ Waiting for first token` | Generation has started but the model is still processing the input (prefill). On CPU this can take tens of seconds |
| `⚡ First token 0.85s` | Time from sending the request to the first token arriving |
| `🚀 12.3 token/s` | Generation speed (thinking is counted too — a slowdown is reported honestly) |
| `📤 128 tokens` | Tokens generated this run (a `≈` prefix means the tokenizer was unavailable and the count is estimated) |
| `⏱️ 3.40s` | Total elapsed time |
| `⏹ Stopped` | You stopped this run; the metrics stay until the next question resets them |

With no model loaded the whole block stays hidden (no zeros, no placeholders).

### Uploading several files at once

- **File dialog**: `Ctrl`/`Shift` multi-select or rubber-band selection. **Drag and drop**: several files at
  once. Both behave the same way.
- Each file gets its **own status line**: `⏳ [3/8] contract.docx …` → `✅ [3/8] contract.docx (12345 chars)` or
  `❌ [3/8] … could not be parsed`. **One failure does not affect the others**, and a single
  "loaded N files" line closes the batch.
- The limit is 10 files and 60 MB per batch; exceeding it asks first (continuing processes only the first 10).
- At question time **all files are injected**, each with a `【文件内容 N/M：filename】` label. When the total
  exceeds `files.total_chars` in `config.json` (default 60,000 characters) injection starts with the **most
  recently uploaded** file, and the ones that did not fit are named explicitly before the answer — nothing is
  dropped silently.
- Uploading the same file name again **replaces** it instead of accumulating; very large files (over 20,000
  characters by default) have their body spilled to `file_cache\` with only a pointer and summary kept in
  context.
- The session file stores names, sizes and summaries but **not bodies**: after reopening a session, files with a
  summary can still be answered from; files without one prompt you to upload them again.

### Getting more out of a small local model

- **Task templates**: `/task list` shows eight templates — summary (`summary`), contract review
  (`contract`), structured extraction (`extract`), translation (`translate`), code explanation and
  debugging (`code`), step-by-step reasoning (`reason`), creative writing (`creative`) and requirement
  clarification (`guide`). Apply one with `/task contract` (a prefix is enough, e.g. `/task con`),
  and clear it with `/task off`. A template is **layered on top of the current mode**: the mode sets the scene
  and tone, the template sets this task's output format, and where they differ the template wins (the prompt
  states that precedence explicitly). `/prompt <text>` has the highest priority and overrides both — run
  `/prompt reset` to let the template take effect again. `/task off` clears only the template and leaves your
  custom prompt alone.
- **Reply language**: it follows the interface language. A Chinese interface asks for Simplified Chinese, an
  English interface asks for English; if you add another language pack, the model replies in whatever language
  you write in. The prompt text itself stays Chinese, which does not affect this.
- **Every mode carries a complete prompt**: each of the three modes states its role, output format,
  **how much detail to give**, reasoning steps, one input/output example and its tone, and they all share the
  same "general requirements" block (say you do not know instead of inventing, answer in one sentence when one
  sentence is enough, give the most likely answer first and ask afterwards when information is missing, never
  fabricate sources). The detail item is what lets the model decide how long to be: simple questions do not
  get forced into a full template, complex ones are still split into points.
- **When you cannot say what you want, use the requirement-clarification template**: `/task guide` lists
  2-3 things you might mean, each with one line about what it would do, and you reply with a number (or "none
  of these") — you do not have to work out how to phrase the request first. The old spelling `/mode guide`
  works too: it now switches to chat and applies this template for you.
- **Generation presets**: `/preset list` shows them; `light` (shortest, fastest replies), `balanced` (the
  default) and `quality` (longest replies) switch the temperature, top_p, repeat penalty and reply-length cap
  in one step. The choice is written into the configuration and survives a restart; if you later change the
  temperature by hand with `/temp`, your manual value wins.
- **Compressing the context**: `/compact` turns earlier conversation into a summary right away to free up room
  (with `context_mode` set to `slide` it discards the oldest history instead); you no longer have to wait for
  the automatic trigger. The automatic threshold and retention policy live in the `compact` section of
  `config.json`: `trigger_ratio` (default 0.70, i.e. compress at 70% of the window) and `grace_turns`
  (default 3 — the most recent 3 turns are kept **verbatim**). To name what must survive compaction, use
  `/compact focus on 合同金额`: the focus is **appended** to the summary requirements rather than replacing
  them. Every compaction appends an event to `compact_log.jsonl` (trigger, usage before and after, how many
  messages were dropped, the summary text) so it stays auditable; for sensitive content set
  `compact.log_content` to `false` and only lengths are recorded.
- **A vague question gets directions, not a request for material**: for something like "what about this?"
  with neither a file context nor any history, the program lists 2-3 possible meanings ("something to work on",
  "a procedure", "an error to diagnose", …) with one line each about what it would do, and you reply with a
  number — it no longer turns the question back on you by demanding the object, output form and constraints.
  With the requirement-clarification template (`/task guide`) the model produces those directions itself,
  so they fit your wording more closely. It does not trigger once you have chatted a few rounds or uploaded a file (there "this" usually
  refers to the context). To switch it off, set `clarify_vague` to `false` in `app\backend\config.json`.

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
   - or simply ask about the detail: EchoDesk **retrieves the most relevant passages** (with file name and
     character range) and injects them alongside the summary, which lands on a specific clause far more often
     than the summary alone (see "9. Retrieval and citation traceability");
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

## 6. Agent: letting the model call tools

In **agent mode** you no longer dictate the steps: the model decides which tool to call and in what order
until it reaches a conclusion. That is the only difference from ordinary conversation — context, history and
stopping behave exactly the same.

Get there with `/mode agent` (also `/mode 3`, `/mode ag`) or `/agent on` in the chat box; `/agent off`
switches back. The temperature preset for agent mode is **0.5**.

### 6.1 The tool set: two permission tiers

| Tier | Tool | What it can do | Default |
| --- | --- | --- | --- |
| ✅ safe | `calc` | arithmetic with `**`, `%`, parentheses and `sqrt`/`round`/`abs`/`min`/`max`/`log` functions, evaluated through an **AST whitelist, not `eval`** | available |
| ✅ safe | `date` | today's date, date arithmetic, days between two dates, day of the week | available |
| ✅ safe | `convert` | length / mass / area / volume / data / time / speed / pressure / temperature conversion | available |
| ✅ safe | `doc_search` | find relevant passages by keyword inside **your uploaded files** (returns file names and snippets) | available |
| ✅ safe | `doc_summary` | fetch the summary of an uploaded file | available |
| 🔒 sensitive | `web_search` | web search (sends the keywords to the internet) | **off** |
| 🔒 sensitive | `file_read` | read a local **text** file (512 KB per file maximum) | **off** |

- **Sensitive tools must be authorised explicitly**: turn one on with `/agent allow web_search` and revoke it
  with `/agent deny web_search`. The choice goes into `agent.allow_sensitive` in `config.json` and **persists**
  (no confirmation on every question); the Agent page of the Settings window offers the same checkboxes.
- **This version ships no tool that writes files or runs commands** (least privilege).
- An unauthorised tool is **not a silent failure**: the chat shows 🔒 and how to enable it, and the model
  carries on with the information it already has.
- `/agent list` shows the tool list and permission state; `/agent steps 5` changes the step limit to 1–8
  (default 4).

### 6.2 Every step is visible, and you can stop at any time

The conversation shows each step in order:

```
📍 Step 1/4
🔧 Call calc args {"expr": "(128.6*10000)*0.003"}
↩ calc returned: 3858.0 (3 ms)
```

Tool results are fed back into the context (each result has a length cap), so the model can look at what came
back before deciding what to do next. Press `Esc` (or click ⏹) to stop at any time — **the same stop path as
ordinary conversation**. When the step limit is reached the loop converges automatically: it stops calling
tools and instead "answers from what it already knows", so you always get a conclusion.

### 6.3 How this differs from automation

| | Automation | Agent |
| --- | --- | --- |
| Flow | you configure the selectors and the program runs **the same predefined flow** | the model **decides for itself** which tool to use and in what order |
| Shape | batch: list page → task by task → download → review opinion | a single task, solved within one question |
| Driven by | a fixed script (headed Playwright browser) | the model (may differ every time) |

---

## 7. Connecting external tools: MCP

MCP (Model Context Protocol) lets the agent use tools provided by **external processes** (databases, APIs,
file systems, …). EchoDesk ships its own **stdio MCP client** (no new dependencies): declare one command per
server in the configuration and it is connected.

### 7.1 Configuring a server

The `mcp` section of `app\backend\config.json`:

```json
"mcp": {
  "enabled": false,
  "timeout_seconds": 20.0,
  "auto_connect": false,
  "servers": [
    {"name": "echo", "command": "python", "args": ["examples/mcp/echo_server.py"], "enabled": true}
  ]
}
```

A bundled example server lives at `examples\mcp\echo_server.py` (standard library only, exposing the two
tools `echo_text` and `now`) — configure it to verify the plumbing end to end. Once connected, its tools appear
in `/agent list` under names such as `mcp__echo__echo_text`.

### 7.2 Commands

```
/mcp                        show status and tools
/mcp enable echo            enable a server (written to config.json)
/mcp disable echo           disable it (effective immediately, no restart)
/mcp connect                connect now and fetch the tool list
```

The MCP section on the **Agent** page of the Settings window offers the same changes (master switch, connect
on startup, timeout, per-server enable/disable, add or remove a server, and a "connect and fetch tools"
button) — it is equivalent to these commands.

### 7.3 Security boundary (essential reading)

- an MCP server is an **external process you configured yourself**, and EchoDesk cannot control what it does;
- therefore its tools are **always treated as sensitive** and must be authorised with
  `/agent allow mcp__<server>__<tool>` first;
- parent-process environment variables are **not passed through** by default (secrets are not handed to a
  third-party process by accident); name the ones you want in that server's `passthrough_env`;
- EchoDesk **never discovers or connects to anything automatically**; `auto_connect` defaults to `false`, so
  the process is only started when you run `/mcp connect` (or set it to `true`).

### 7.4 How this relates to "your data stays on this machine"

This deserves to be stated plainly: **whether MCP touches the network, and where data goes, depends on the
server process you configured**. EchoDesk itself does not reach out and does not decide who receives your
data — it just starts the command you wrote and collects the tool results. The "data stays on this machine"
promise therefore **still holds under the default configuration** (MCP is off and no server is declared); once
you connect a server of your own, that process defines the data boundary, so only connect servers you trust.

### 7.5 Limits in this version / how it differs from skills

- this version supports **stdio transport only**: no HTTP/SSE, and no resources / prompts / sampling;
- the difference from **skills**: a skill is "adding a feature to EchoDesk" (it can register commands, panels,
  hooks and file parsers, and is installed from the skill marketplace), while MCP is "giving the agent a few
  external tools" (tool calls only, wired up by a command you write yourself).

---

## 8. Model management and inference parameters

### 8.1 The inference parameter panel (Settings → Model)

The Model page now carries an inference parameter panel: four sliders, each with its own "restore default":

| Parameter | Range | Notes |
| --- | --- | --- |
| Temperature | 0–1.5 | higher is more exploratory, lower is more conservative |
| top_p | 0.1–1.0 | candidate range; lower is more focused |
| Repeat penalty | 1.0–1.5 | higher reduces repetition, too high sounds stiff |
| Max generated tokens | 64–4096 | length cap for a single reply |

Three more settings sit alongside them: the **generation preset** dropdown (light / balanced / quality,
effective as soon as you change it), the **context strategy** (segmented summarisation / sliding-window
discard) and two switches (give directions first for vague questions; cache segmented summaries).
**Save** writes everything in one go and reads back the values that are really in effect (it does not pretend
that whatever you dragged is live); **Reload** fetches the current values again.

> The temperature ceiling was raised from 1.0 to 1.5 to match the slider: when dragging to the end and saving
> silently snapped back to 1.0, the interface looked like it was lying.

### 8.2 Custom model tiers

Model tiers can now be **added, edited and deleted** in the Settings window, so editing `config.json` by hand
is no longer required: alias, model file name or path, repository, context length, temperature and a note. A
repeated alias is treated as an edit.

Two guarantees:

- **deleting a tier never deletes the model file on disk** — only the registration for that tier is removed;
- **a tier that is currently in use refuses to be deleted**, and tells you to switch to another one first.

---

## 9. Retrieval and citation traceability

Long documents now get **local retrieval** (keyword BM25, standard library only, no new dependencies): a
question no longer has to live on a single summary, because the most relevant passages come along too.

### 9.1 How retrieval and segmented summarisation divide the work

| Situation | Which one | Why |
| --- | --- | --- |
| Short document (the full text fits the context) | **inject the full text, do not retrieve** | for short material the full text is the most accurate and retrieval would only lose information |
| Long document (spilled to disk or summarised) + a specific question | **summary for the whole picture + retrieval for the detail** | the summary says what the material is about, retrieval finds where "the 30 working days clause" is |
| Long document + a broad question | summary first, retrieval only tops up top-k | a broad question has no keywords to retrieve on, and forcing it would add noise |

In other words: **a summary compresses, retrieval locates** — they complement each other rather than
replace each other. Retrieved passages are placed before the file blocks, so you can see at a glance which
passages the answer was based on.

### 9.2 Citation traceability

Every injected passage carries a **position**, like this:

```
[1] purchase-contract.docx (chars 210-480)
The buyer shall pay 95% of the contract amount within 30 working days of acceptance…
```

and the model is explicitly required to mark its evidence with `[number]` (content without evidence must not
be written as a citation). With the file name and the character range you can go back to the original text and
check, instead of having to take a sentence on trust.

### 9.3 Multi-document Q&A and incremental indexing

- **You do not have to name the file**: retrieval spans every uploaded file, and you can restrict it to a few
  of them when you want to.
- **Incremental indexing**: a newly uploaded file indexes only itself and leaves existing files untouched; a
  file with the same name replaces the previous content; once a file is removed its content is no longer
  retrievable.

### 9.4 Configuration

The `retrieval` section of `app\backend\config.json`:

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | master switch for retrieval |
| `mode` | `keyword` | keyword is the only mode in this version; hybrid / vector are reserved for the future |
| `chunk_chars` | 600 | chunk length (roughly 300 tokens) |
| `overlap_chars` | 100 | overlap between chunks, so an answer spanning a chunk boundary is not missed |
| `top_k` | 4 | how many passages are injected at once |
| `max_context_chars` | 2400 | total length cap for the injected passages |
| `min_score` | 0.0 | BM25 score floor (0 = no threshold) |

### 9.5 Why keyword retrieval rather than a vector store

This is a deliberate trade-off:

1. **Size**: a vector store (faiss / hnswlib / chromadb) plus an embedding model would add tens to hundreds of
   megabytes to the release, which directly conflicts with the "350–400 MB installed" constraint;
2. **A model change would invalidate the index**: embeddings are tied to the chosen model, so switching chat
   models would throw the whole index away; a keyword index does not depend on the model at all;
3. **Explainable and checkable**: a keyword hit can be explained as "because these words appeared", while a
   vector similarity only yields a number — for "citations can be traced and users can check the original
   text", keyword traceability is the more trustworthy foundation.

---

## 10. Networking and privacy at a glance

| Feature | Network access | What is sent |
| --- | --- | --- |
| Ordinary Q&A, file parsing, long-document summarization, local retrieval | ❌ Fully local | — |
| Safe agent tools (calculator / dates / unit conversion / lookup and summary inside uploaded files) | ❌ Fully local | — |
| Sensitive agent tool `web_search` (needs `/agent allow web_search`) | ✅ only callable after authorisation | the keywords of that question → DuckDuckGo |
| Sensitive agent tool `file_read` (needs `/agent allow file_read`) | ❌ no network, but it reads the text file you name (≤512 KB) | that one file only |
| MCP (a server you configured) | ❓ depends on that process | decided by the server you configured; EchoDesk only starts it and never reaches out itself |
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

## 11. Keyboard and productivity

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
