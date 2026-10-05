# EchoDesk Frequently Asked Questions and Troubleshooting (FAQ)

Applies to version: v0.2.1
Chinese version: [`docs/zh-Hans/FAQ.md`](../zh-Hans/FAQ.md).

---

## 1. Quick start

| What I want to do | How to do it |
| --- | --- |
| Start the program | Use the Start Menu (or desktop) shortcut created by the installer; for the portable zip, double-click "Start-EchoDesk-v0.2.1.lnk" inside the program folder (run `install-complete.bat` once if it is missing) |
| See every command | Type `/help` in the input box |
| Have the AI read a document | Drag the file into the window, or use `/file C:\路径\合同.docx` |
| Have the AI do arithmetic or look inside uploaded files | switch to agent mode: `/mode agent`, `/mode 3` or `/agent on` — the model picks the tool (see "3. Modes, agent and MCP") |
| Let the agent search the web | authorise it first: `/agent allow web_search` (sensitive tools are off by default) |
| Very long documents (contracts/reports) | Just upload it: anything beyond the context window is summarized in chunks automatically; you can also trigger it by hand with `/summarize` |
| Use a different model | `/model list` to see the local models, `/model 名称` to switch |
| Get more reliable answers | `/task list` picks a task-oriented prompt template (layered on top of the current mode: the mode sets the scene and tone, the template sets this task's output format) — then state the object and the output you expect |
| Change the reply language | It follows the **interface language**: a Chinese interface replies in Chinese, an English one in English (switch the interface language in Settings) |
| Set a custom assistant role | `/prompt you are a financial adviser` (highest priority — it overrides the mode and any task template; `/prompt reset` restores them) |
| Switch the generation preset | `/preset light`, `/preset balanced` or `/preset quality` in one step |
| The context is almost full | `/compact` compresses the context right away (or click "🗜 Compress context" in the status strip) |
| Follow the system theme | `/darkmode auto`, or pick "Follow the system" on the "General" page in Settings |
| Check the current status | `/status` (model, mode, temperature, context usage, KV Cache estimate and model capability) |
| Copy one reply | "📋 Copy / ⧉ Markdown / </> Code" at the bottom-right of that message |
| Copy the whole conversation | `Ctrl+Shift+C` |
| Open Settings | Click ⚙ in the toolbar, or press `Ctrl+,` |
| Open the skill marketplace | `Ctrl+M` (or Settings window → Skill marketplace) |
| Quit | `/exit` (the program closes itself after a 10-second countdown) |

**Shortcuts at a glance**: `Ctrl+Enter` send · `Enter`/`Shift+Enter` new line · `Esc` stop generation/close pop-ups ·
`Ctrl+L` clear the conversation · `Ctrl+O` upload a file · `Ctrl+Shift+C` copy the whole conversation ·
`Ctrl+加/减/0` adjust font size · `Ctrl+T` switch light/dark theme · `Ctrl+,` Settings · `Ctrl+M` skill marketplace.

**A vague question gets directions first**: for a question with neither a clear object nor any file context
(just "what about this?"), the program lists 2-3 possible meanings with one line each about what it would do,
and you reply with a number — rather than guessing at an answer or demanding that you spell out the object and
output form (it does not do this once you have chatted a few rounds or uploaded a file). For directions that
fit your wording more closely, apply the requirement-clarification template with `/task guide`. To switch the behaviour off, set
`clarify_vague` to `false` in `app\backend\config.json`.

---

## 2. Startup and models

### Double-clicking the shortcut does nothing / "backend failed to start"
1. First look at `app\backend\backend_crash.log` (the backend creates this file on every start — only a file
   **with content** means it has crashed; an empty file is normal): it holds the full Python stack trace.
2. Send us the log contents and we can pin down the cause.

### "The required model was not found locally" / no model at all
Since v0.2.1 the app **keeps running** in this case — nothing exits, nothing blocks, nothing is downloaded
behind your back. The status bar shows "no model", the chat shows guidance, and everything that does not
need inference keeps working (file parsing, settings, skill market, model market, session history).
Three ways to get a model, none of which needs a restart:
1. **Settings → Model → Model market**, search and download. The list shows **all** GGUF repositories
   (nothing is filtered out by hardware); entries that suit this machine are marked "(Recommended)" and the
   repository of the model named in `config.json` is pinned **above** the results (shown separately, not
   counted in the paging totals). The download hot-loads the model.
2. **Copy a `.gguf` into `app\backend\models\`** and click *Rescan models folder*; if exactly one model is
   found it is loaded automatically.
3. **Click *Load*** next to an already-downloaded model in the local model library.
You can also point `model_repo` / `model_path` / `hf_endpoint` in `app\backend\config.json` at your own model.

### The model market list used to hide models — what changed?
Nothing is hidden any more. Results come from three sources: the remote search, the configured default
model, and locally downloaded models. Hardware detection is only used to **mark** entries that fit your
machine, never to filter them out. The default model's repository is pinned **above** the result list with
its source labelled, so it can no longer be missing entirely.

### Searching GGUF returns tens of thousands of entries — why do I only see some?
The app pages through the remote source and fetches everything, but a single load is bounded by three caps
(item count, request count, elapsed time) so that an overly broad keyword cannot swamp the network or your
memory. When a cap is hit the status line says "N loaded, more available" and a **Load more** button appears
to continue. Fetched results are cached locally, so searching the same keyword again hits the cache;
**Refresh** is what goes back to the network.

### The download counts do not match what I see on the Hugging Face website
The website shows **downloads in the last 30 days** by default; EchoDesk shows **all-time downloads** (and
sorts by it). They are genuinely different figures — an older model can have a huge all-time total while
barely being downloaded in the past 30 days. If the API has no all-time field for an entry, the UI falls
back to "last 30 days" and **labels that entry**, instead of showing 0.

### How filtering interacts with paging
Local filtering (the filter box plus the multi-select facets for parameter size, quantisation, update time
and so on) only applies to the results **already fetched** and never touches the network; changing a filter
resets the page to 1. Filters survive paging and sort switches. Within one facet, multiple selections are
OR-ed (e.g. "≤1B" or "1–3B"); across facets they are AND-ed. When local filtering matches nothing you get a
**Search remote again** entry — that is a hint to try another keyword remotely, not a broken app.

### Loading a model failed
The app stays in the no-model state and shows the reason (corrupt file, unsupported format, not enough
memory). Try a smaller quantization, or check that the `.gguf` really is a complete GGUF file. Nothing needs
restarting: fix the file and click *Load* (or *Rescan models folder*) again.

### I want a different model but do not want to download one
1. Put the `.gguf` file into `app\backend\models\`;
2. Run `/model list` to confirm it has been recognized;
3. Switch with `/model 文件名或前缀`.
   If you define a profile under `models` in `config.json` (alias → path/repo/context_length/temperature),
   then `/model 别名` switches in a single step and applies that profile's context length and temperature
   automatically.

### Will deleting a custom tier delete my model files
No. **Deleting a tier only removes the registration for that tier; not a single `.gguf` file on disk is
touched** — a tier is just a note of "this is the one I use often", and deleting files has its own entry point
(with a confirmation) in the model library. **A tier that is currently in use refuses to be deleted** and asks
you to switch to another one first.

Tiers can now be added, edited and deleted on the "Model" page of the Settings window (alias, model file name
or path, repository, context length, temperature, note), so editing `config.json` by hand is no longer needed.
The inference parameter panel on the same page works the same way: four sliders for temperature, top_p, repeat
penalty and max generated tokens, plus **Save**, which writes them and reads back the values really in effect.

### Answers are very slow
- The larger the context window, the more memory the KV Cache takes and the slower generation becomes.
  `/status` shows the current window and the KV Cache estimate.
- When generation drops below about 3 characters per second, a hint is appended to the end of the reply (you
  can turn it off with `speed_hint` in `config.json`).
- You can lower `context_length` in `config.json` (default 8192), raise `n_threads`, or switch to a smaller
  quantized model (such as Q4_K_M).
- Reasoning models (Qwen3 thinking, DeepSeek-R1) think before they answer, and thinking takes time too: as
  long as the “🧠 Thinking” region keeps growing, the model is producing output rather than being stuck.

### The model produced a wall of `<think>…</think>` reasoning — how is it separated now?
That is normal for reasoning models, and EchoDesk already separates the two (thinking/answer split):
- the answer area shows **only the final answer**; reasoning lives in a collapsed “🧠 Thinking” region above
  it — click the header to expand or collapse;
- while streaming the region expands automatically, and it **collapses again when the answer starts**;
- when the model produced no reasoning, no empty region appears;
- reasoning **never goes back into context** (it is not sent to the model), but it **is saved with the
  session**, so it is still there (collapsed) when you reopen EchoDesk;
- the copy buttons and `Ctrl+Shift+C` copy only the answer; to copy reasoning, drag-select inside the
  expanded region or use the `/save` export, where it is written as a collapsible block.

### The model only produced thinking and never answered
The answer area says that only thinking was produced (when the thinking tag was never closed). Nearly always
the **generation length limit** ran out — thinking used up the token budget before the answer began:
- switch to the quality preset with `/preset quality` (2048 tokens; `/preset balanced` is 1024);
- or raise `max_tokens` in `config.json` (default 1024);
- or ask a more specific question (the vaguer the question, the longer the model thinks);
- small models (0.5B–1.5B) are not reliable reasoners anyway; a larger quantized model usually pays off more.

### How do I make EchoDesk start with Windows
Copy "Start-EchoDesk-v0.2.1.lnk" into the system Startup folder (`Win+R` → `shell:startup`).
The `automation.start_with_windows` setting is **not implemented yet**: the program does not read it, so
setting it to `true` will not start EchoDesk at sign-in — use the shortcut above instead (scheduled automation
requires the program to be running).

---

## 3. Modes, agent and MCP

### Why did the six modes lose half of themselves
Nothing was lost — they simply stopped occupying a mode slot. There are now **three modes**
(`chat` / `contract` / `agent`), and the merged `summary` / `translate` / `code` / `creative` / `guide`
live on as `/task` templates (`general` was simply renamed to `chat`).

**The old spellings still work**: `/mode summary` switches to chat and applies `/task summary`, keeping the old
temperature preset (summary 0.4 / translate 0.3 / code 0.2 / creative 0.9 / guide 0.6), and the reply says
"merged into another mode, template /task xxx applied automatically"; run `/task off` to drop the template.
**The old numbers are no longer migrated automatically**: a number means different things under the two
numbering schemes (the old `/mode 2` was summary, the new one is contract), so an old number such as
`/mode 4` produces a clear error telling you which template to use instead of guessing. Write the mode name
(`/mode summary`) for zero ambiguity, or switch first and apply the template (`/mode chat`, then
`/task code`). The full explanation is in the user guide under "Conversation modes: three modes, and the old
spellings still work", and the task templates grew from six to eight (adding `creative` and `guide`).

### Why will the agent not search the web
Because **web search is a sensitive tool and is off by default**: it sends your keywords to the internet, so it
needs your explicit authorisation — `/agent allow web_search` (written to `config.json`, persists), or the
checkbox on the Agent page of the Settings window. For the same reason `file_read`, which reads arbitrary local
text files, is off by default too (`/agent allow file_read`).

An unauthorised tool **never fails silently**: the chat shows 🔒 and how to enable it, and the model answers
from what it already has. The safe tools (`calc`, `date`, `convert`, and `doc_search`/`doc_summary` over your
uploaded files) need no authorisation and work out of the box. `/agent list` shows the authorisation state of
every tool, and `/agent steps <1-8>` changes the step limit (default 4).

### Will MCP send my data out
**That depends on the server process you configured, not on EchoDesk itself.**

- EchoDesk ships a stdio client and **never discovers or connects to anything automatically**;
  `mcp.enabled` defaults to `false` and so does `auto_connect`, so with no configuration no external process is
  ever started.
- An MCP server is a command you wrote yourself (`command` + `args`), and EchoDesk cannot control what it does,
  so its tools are **always treated as sensitive** and must be authorised with
  `/agent allow mcp__<server>__<tool>` first.
- Parent-process environment variables are **not passed through** by default (secrets are not handed to a
  third-party process by accident); name the ones you want in that server's `passthrough_env`.

In short: **the "data stays on this machine" promise still holds under the default configuration**; once you
connect a server of your own, that process defines the data boundary — so only connect servers you trust.

---

## 4. Files and long documents

### Which formats are supported
`.docx` `.xlsx` `.xlsm` `.xls` `.pptx` `.pdf` `.txt` `.md` `.csv` and other plain-text-like formats.
- `.doc` / `.ppt` (legacy binary formats) need WPS/Office installed on the machine (they are read through COM,
  which also lets us read **encrypted documents**); otherwise save them as `.docx` / `.pptx` first.
- Scanned PDFs have no text layer, so parsing returns nothing — those files need OCR, which v0.2.0 does not
  include.

### Why does the answer to a long document not cite details from the original text?
Once a document goes past a certain share of the context window (50% by default), EchoDesk summarizes it in
chunks before injecting it, and **a summary loses detail**. In that case:
- The end of the summary carries an "original-text pointer" (the character range in the original text for each
  chunk), which you can use to narrow the question down;
- To get answers grounded in the full text, raise `context_length` and `summarize.trigger_ratio` in
  `config.json`, or split the document into several parts and upload them separately.

### Why does a question about a long document show only a few passages instead of the full text
Because a long document is **never pushed into the context wholesale**; instead the summary gives the whole
picture and retrieval supplies the detail:

- **short documents (the full text fits the window)**: no retrieval, the full text is injected directly — for
  short material the full text is the most accurate and retrieval would only lose information;
- **long documents (spilled to disk or already summarised)**: the summary plus the retrieved passages, placed
  before the file blocks (`top_k` 4 by default, total length capped at 2400 characters).

Every injected passage carries a position such as `purchase-contract.docx (chars 210-480)`, and the model is
required to mark its evidence with `[number]`, so you can go back to the original text and check. To see more
passages raise `retrieval.top_k` and `retrieval.max_context_chars` in `config.json`; to switch retrieval off
entirely set `retrieval.enabled` to `false`.

### After uploading, the model says it "cannot see the file"
When you ask, **mention the file** (for example "According to this contract…" or "What is clause 3 in the
document?"). To save context, when the file is not mentioned only a filename pointer is injected, not its
contents.

### How do I know chunked summarization is still working and not stuck?
Since v0.2.1 the chat shows live progress while a long document is being summarized: the phase (splitting /
summarizing / merging), which segment of how many is running, how long the whole run and the current
segment have taken, and how much text the current segment has produced so far. Because each segment is
generated in streaming mode, a status update arrives at least every few seconds — a long silence is not
expected, and a segment that is merely slow is labelled as slow rather than reported as an error.
The stop button aborts the run immediately; the original text stays in context and **no half-finished
summary is injected**, so you can retry with `/summarize`. If a segment really fails, that segment falls
back to a raw excerpt of the original text and the result says so.
Timeouts are configurable in `config.json` under `summarize`: `chunk_timeout_seconds` (0 = unlimited) and
`chunk_timeout_action` = `skip` / `retry` / `abort`.

### If I upload the same document again, is it summarized all over again
No. **Chunked summaries are cached** (keyed by the file fingerprint plus the summarization parameters), so a
second upload of the same file reuses the previous result instead of recomputing it; the cache expires
automatically once the file changes (its size or modification time).
To clear the cache, delete `app\backend\summary_cache.json` (it is readable JSON, so you can inspect it too);
to stop using it, set `summary_cache` to `false` in `app\backend\config.json`.

### Why is long-document summarization much faster than before
Since v0.2.1 known-issues round 2, every chunk goes through an **extraction prefilter** before it reaches the
model: a TextRank pass plus a "rarity" statistic picks the sentences that matter, and the model only sees the
extracted text. This step **needs no model and takes milliseconds**; measured on a 47 KB document it cut what
is sent to the model from ~50,000 to ~18,000 characters (64% less), so total summarization time drops clearly.

It is lossy: extraction works on whole sentences, so documents made of many very short clauses (contract
clauses, for example) can lose some detail. To keep everything, raise `extractive_ratio` in the `summarize`
section of `config.json` (default 0.35; 1.0 means no extraction) or set `extractive` to `false`.
The log states how many chunks were prefilted and how many characters were saved
("抽取式预筛 N/M 段生效，送模型 X → Y 字符").

### Can I upload several files at once
Yes. The file dialog supports Ctrl/Shift multi-select and rubber-band selection, and you can drop several
files at once. Each file gets its **own status line** (`⏳ [3/8] contract.docx …` → `✅` or `❌`),
**one failure does not affect the others**, and a single "loaded N files" line closes the run.
The limit is 10 files and 60 MB per batch; anything beyond that asks first, and files over the limit are not
processed (you are told).

At question time **all files are injected** (each with a filename label), capped by `files.total_chars` in
`config.json` (default 60,000 characters). Files that do not fit are dropped starting with the **oldest**
upload, and you are told exactly which ones were not injected — nothing is dropped silently.
`/status` lists the loaded file names; `/file` loads them one by one and `/file` can also remove them.

### "Path does not exist" even though the file is right there
The path contains a space. Quote it:

```
/file "C:\My Documents\contract 2026.docx"
```

(Before v0.2.1 known-issues round 2 a path with spaces was split into several arguments, so it always failed.
The upload button and drag-and-drop never had this problem.)

### Why is the file name still there after reopening a session but the content is gone
The session file (`session.json`) **does not store document bodies** (they run from tens of thousands to
hundreds of thousands of characters — storing them would bloat the session file to tens of MB, and they can
always be re-parsed from the original file). It stores the file name, size and any chunked summary.
So after restoring a session, files with a summary can still be answered from; files without one ask you to
upload them again.

### Web search is on, but the answer shows no trace of search results
Known-issues round 2 fixed three things here. If you still see nothing, check in this order:

1. **The search returned nothing**: you are now told explicitly (failure and "no results" are distinguished),
   instead of silently continuing as before.
2. **The results were not injected**: if the search switch is off (the status strip shows `🔍 搜索：关`),
   nothing is searched — turn it on with `/search on`.
3. **The model ignored the results**: injection is now a numbered list with an instruction to cite the number,
   but a small model may still not comply fully — naming "according to result 1" in your question raises
   compliance noticeably.

---

## 5. Automation (browser)

### Which browser can be used
**Microsoft Edge** must be installed (Playwright drives it through `channel=msedge`, running in headed mode,
so you can see the browser working). The build script checks whether Edge is present.

### Turning on automation tells me the safety agreement has to be ticked
This is by design: **when the safety agreement is not ticked, the backend forces the automation switch off**,
and the agreement version together with the time you accepted it are recorded in `app\backend\automation_log.jsonl`
for auditing.

### I entered a URL but "Test selectors" matches no elements
1. First use the browser's F12 tools to confirm that the selector picks out the target (paste CSS as-is;
   XPath starts with `//`);
2. If the page loads dynamically, wait a moment and test again;
3. When the element sits inside an `<iframe>` or a Shadow DOM, targeting does try to reach through, but success
   is not guaranteed — switch to a selector on a more outer element instead.

### A CAPTCHA or two-factor verification appears
The program pauses and shows "CAPTCHA / two-factor verification detected, please complete it manually"; once
you finish it in the browser window that popped up, the run continues (it waits up to 180 seconds by default;
adjust that with `automation.manual_login_wait_seconds` in `config.json`).

### How do I stop it
Click "⏹ Stop automation" in the Settings window, or go back to the main window and press `Esc` / click ⏹.
Temporary files that were already downloaded are cleaned up and the browser closes.

### Where do the downloaded files go
The original file downloaded for each task is **moved into that task's result directory**, alongside the
review opinion that was generated:

```
results\2026-09-23\合同审查\     ← the top-level folder name, date and task name are all configurable
├─ 合同正文.pdf                       ← the original file downloaded in this run (duplicate names get a number appended)
├─ result.txt                           ← extract + review opinion (plain text, no Markdown markup)
└─ failure-screenshot-task2-20260923_101010.png  ← generated only when a task fails
```

The temporary copies used during downloading are kept in a dedicated subdirectory of the system temporary
directory and are cleaned up as soon as processing finishes.
If you turn off "Save results to local files", no `result.txt` and no screenshots are written, and the results go
to the log only.
With "Where to save results" left empty the results root sits in the portable **outer** folder
(`EchoDesk-v0.2.1\results\`), not inside `app\`; to move all user data (including `ui_state.json` and
`snippets.json`), set the `ECHODESK_DATA` environment variable.
For how to set the save location, the top-level folder name and the task name, see
[`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md).

### Login state
You need to log in manually the first time; the program stores cookies/localStorage in
`app\backend\browser_state.json` and reuses them next time. Delete that file to sign out of every login state.

---

## 6. Skill marketplace

### No skills found / the list is empty
A skill in the marketplace must carry the **topic `echodesk-plugin`** on its GitHub repository.
No results usually means that no repository carries that topic at the moment, not that the program is broken.
You can click "Refresh" first to see the cached snapshot, or search by keyword.

### "Offline: showing the cached local snapshot"
The anonymous GitHub API is rate-limited (60 requests per hour), or the network is unreachable. What to do:
- Put a GitHub token in `marketplace.github_token` in `app\backend\config.json` (this raises the limit to 5000
  requests per hour);
- Or try again later; the program keeps using the local cache and does not error out over it.

### The install button is greyed out
The button is greyed out when a card shows "⚠ Unverified" — meaning the repository root has no `skill.json`, or
that file has no `echodesk.bundle` field. The source of such skills cannot be trusted, so installing them is
not allowed by default.

### Does installing run third-party code?
**The `prepare` script is never run automatically.** The install flow only: downloads the source → verifies
`skill.json` → copies it into `app\backend\skills\` → registers it in `app\backend\skills_registry.json`.
If a skill declares a `prepare` script or dependencies, they are listed in the prompt and it is up to you to
decide whether to handle them by hand.
Even if prepare is executed in the future, every environment variable whose name contains
`KEY`/`SECRET`/`TOKEN`/`PASSWORD` will be removed first.

### Uninstalling leaves things behind
Uninstalling will: remove the registry entry → delete the skill directory → read the registry back to confirm.
If anything is still left over, you can delete `app\backend\skills\<技能id>` by hand and edit
`app\backend\skills_registry.json`.

### Will local plugins keep asking me for confirmation
No. The first time **third-party local plugins** are loaded (they live in `app\backend\skills_dev\`) you get
**one** summary confirmation dialog: tick them one by one, or use "enable all" / "disable all". Your choices
are remembered, so nothing is asked again — refused plugins included.
Bundled example plugins (`examples\skills\`) are enabled by default without asking. The **developer mode**
switch on the "Developer" page of Settings works in both directions: off means off, and it stays off after a
restart, unloading the development plugins and clearing the development log `app\backend\skills_dev.log`.

---

## 7. Interface

### Are window size/position/theme/font size remembered?
Yes, they are stored in `ui_state.json` in the portable **outer** folder (window position and size, font size,
the three-way theme, the emoji display policy, the interface language, ...). If the window is restored onto a
monitor that has since been unplugged, the program falls back to a position on screen automatically. Delete
that file to reset everything (this also clears the "onboarding already read" marker).

### Is the theme just a light/dark switch?
No — there are **three** choices: Follow the system / Light / Dark, on the "General" page of the Settings
window. "Follow the system" tracks the Windows app mode, so the interface follows along the moment you switch
light/dark in Windows. `Ctrl+T` flips between light and dark, and `/darkmode on|off|auto` sets it directly.

### The interface shows plain-text markers such as `[OK]` / `[!]` — is something broken?
No. The **emoji display** setting on the same "General" page also has three choices: Auto / Always / Never.
With "Auto", if this machine has no emoji font (trimmed-down systems, or corporate policies that strip the
font), the interface uses plain-text markers instead of empty boxes; install the emoji font again and restart
to get the coloured icons back.

### What is the capability-hint button in the status strip?
It reports **model capability limits**: it only appears when a limit is actually reached (a usable window well
below the trained window, a very slow measured generation speed, a KV Cache estimate above half of the
machine's memory, and so on), and opening it lists each reason together with the action you can take (lower
`context_length`, raise `n_threads`, switch to a smaller quantized model, ...).
The status strip also shows the model capability item (size · quantization · usable window · measured
generation speed); clicking it opens "Settings → Model" with the full details and the recommended configuration.

### The mouse wheel does not scroll inside a message
Scrolling inside the message control is disabled; the wheel always scrolls the whole conversation list.
Use `Ctrl+滚轮` to adjust the font size.

### I want to report a problem
Please include: `app\backend\backend_crash.log` (if it has content), `app\backend\config.json`, the output of
`/status`, and the steps that reproduce the problem.

---

## 8. Still have a question?

If this FAQ did not answer it, ask on GitHub. The project publishes **no support email address,
no phone number and no postal address** — GitHub is the only channel, and a report filed there
reaches the maintainers directly:

- **Questions, ideas, skill sharing and translation coordination** → post in
  [GitHub Discussions](https://github.com/VoidEntropiaStudio/EchoDesk/discussions).
- **A reproducible problem that is not covered above** → **open an Issue**:
  <https://github.com/VoidEntropiaStudio/EchoDesk/issues>. Include what
  [I want to report a problem](#i-want-to-report-a-problem) lists: the version, your Windows
  build, `app\backend\backend_crash.log` (if it has content), `app\backend\config.json`, the
  output of `/status`, and the exact steps that reproduce it.
- **A security vulnerability** → do **not** open a public issue; follow the private reporting
  process in [`SECURITY.md`](../../SECURITY.md).
- **A change to the code, the documentation or a translation** → open a pull request, see
  [`CONTRIBUTING.md`](../../CONTRIBUTING.md).
