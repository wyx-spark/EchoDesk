# EchoDesk Frequently Asked Questions and Troubleshooting (FAQ)

Applies to version: v0.2.0
Chinese version: [`docs/zh-Hans/FAQ.md`](../zh-Hans/FAQ.md).

---

## 1. Quick start

| What I want to do | How to do it |
| --- | --- |
| Start the program | Double-click "EchoDesk 启动.lnk" inside the program folder |
| See every command | Type `/help` in the input box |
| Have the AI read a document | Drag the file into the window, or use `/file C:\路径\合同.docx` |
| Very long documents (contracts/reports) | Just upload it: anything beyond the context window is summarized in chunks automatically; you can also trigger it by hand with `/summarize` |
| Use a different model | `/model list` to see the local models, `/model 名称` to switch |
| Check the current status | `/status` (model, mode, temperature, context usage, KV Cache estimate) |
| Copy one reply | "📋 Copy / ⧉ Markdown / </> Code" at the bottom-right of that message |
| Copy the whole conversation | `Ctrl+Shift+C` |
| Open Settings | Click ⚙ in the toolbar, or press `Ctrl+,` |
| Open the skill marketplace | `Ctrl+M` (or Settings window → Skill marketplace) |
| Quit | `/exit` (the program closes itself after a 10-second countdown) |

**Shortcuts at a glance**: `Ctrl+Enter` send · `Enter`/`Shift+Enter` new line · `Esc` stop generation/close pop-ups ·
`Ctrl+L` clear the conversation · `Ctrl+O` upload a file · `Ctrl+Shift+C` copy the whole conversation ·
`Ctrl+加/减/0` adjust font size · `Ctrl+T` toggle light and dark theme · `Ctrl+,` Settings · `Ctrl+M` skill marketplace.

---

## 2. Startup and models

### Double-clicking the shortcut does nothing / "backend failed to start"
1. First look at `backend\backend_crash.log` (the backend creates this file on every start — only a file
   **with content** means it has crashed; an empty file is normal): it holds the full Python stack trace.
2. Send us the log contents and we can pin down the cause.

### "The required model was not found locally"
This is **expected behaviour** (since v0.2.0 the program no longer downloads silently):
- Click "Yes" → it downloads automatically from the configured mirror (default `hf-mirror.com`, about 508 MB);
- Click "No" → the program exits; you can download a `.gguf` yourself, drop it into `backend\models\` and
  restart;
- You can also point `model_repo` / `model_path` / `hf_endpoint` in `backend\config.json` at your own model.

### I want a different model but do not want to download one
1. Put the `.gguf` file into `backend\models\`;
2. Run `/model list` to confirm it has been recognized;
3. Switch with `/model 文件名或前缀`.
   If you define a profile under `models` in `config.json` (alias → path/repo/context_length/temperature),
   then `/model 别名` switches in a single step and applies that profile's context length and temperature
   automatically.

### Answers are very slow
- The larger the context window, the more memory the KV Cache takes and the slower generation becomes.
  `/status` shows the current window and the KV Cache estimate.
- When generation drops below about 3 characters per second, a hint is appended to the end of the reply (you
  can turn it off with `speed_hint` in `config.json`).
- You can lower `context_length` in `config.json` (default 8192), raise `n_threads`, or switch to a smaller
  quantized model (such as Q4_K_M).

---

## 3. Files and long documents

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

### After uploading, the model says it "cannot see the file"
When you ask, **mention the file** (for example "According to this contract…" or "What is clause 3 in the
document?"). To save context, when the file is not mentioned only a filename pointer is injected, not its
contents.

---

## 4. Automation (browser)

### Which browser can be used
**Microsoft Edge** must be installed (Playwright drives it through `channel=msedge`, running in headed mode,
so you can see the browser working). The build script checks whether Edge is present.

### Turning on automation tells me the safety agreement has to be ticked
This is by design: **when the safety agreement is not ticked, the backend forces the automation switch off**,
and the agreement version together with the time you accepted it are recorded in `backend\automation_log.jsonl`
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
批量处理结果\2026-09-23\合同审查\     ← the top-level folder name, date and task name are all configurable
├─ 合同正文.pdf                       ← the original file downloaded in this run (duplicate names get a number appended)
├─ 结果.txt                           ← extract + review opinion (plain text, no Markdown markup)
└─ 失败截图_任务2_20260923_101010.png  ← generated only when a task fails
```

The temporary copies used during downloading are kept in a dedicated subdirectory of the system temporary
directory and are cleaned up as soon as processing finishes.
If you turn off "Save results to local files", no `结果.txt` and no screenshots are written, and the results go
to the log only.
For how to set the save location, the top-level folder name and the task name, see
[`AUTOMATION_GUIDE.md`](AUTOMATION_GUIDE.md).

### Login state
You need to log in manually the first time; the program stores cookies/localStorage in
`backend\browser_state.json` and reuses them next time. Delete that file to sign out of every login state.

---

## 5. Skill marketplace

### No skills found / the list is empty
A skill in the marketplace must carry the **topic `echodesk-plugin`** on its GitHub repository.
No results usually means that no repository carries that topic at the moment, not that the program is broken.
You can click "Refresh" first to see the cached snapshot, or search by keyword.

### "Offline: showing the cached local snapshot"
The anonymous GitHub API is rate-limited (60 requests per hour), or the network is unreachable. What to do:
- Put a GitHub token in `marketplace.github_token` in `backend\config.json` (this raises the limit to 5000
  requests per hour);
- Or try again later; the program keeps using the local cache and does not error out over it.

### The install button is greyed out
The button is greyed out when a card shows "⚠ Unverified" — meaning the repository root has no `skill.json`, or
that file has no `echodesk.bundle` field. The source of such skills cannot be trusted, so installing them is
not allowed by default.

### Does installing run third-party code?
**The `prepare` script is never run automatically.** The install flow only: downloads the source → verifies
`skill.json` → copies it into `backend\skills\` → registers it in `backend\skills_registry.json`.
If a skill declares a `prepare` script or dependencies, they are listed in the prompt and it is up to you to
decide whether to handle them by hand.
Even if prepare is executed in the future, every environment variable whose name contains
`KEY`/`SECRET`/`TOKEN`/`PASSWORD` will be removed first.

### Uninstalling leaves things behind
Uninstalling will: remove the registry entry → delete the skill directory → read the registry back to confirm.
If anything is still left over, you can delete `backend\skills\<技能id>` by hand and edit `skills_registry.json`.

---

## 6. Interface

### Are window size/position/theme/font size remembered?
Yes, they are stored in `ui_state.json` in the program directory. If the window is restored onto a monitor
that has since been unplugged, the program falls back to a position on screen automatically. Delete that file
to reset everything (this also clears the "onboarding already read" marker).

### The mouse wheel does not scroll inside a message
Scrolling inside the message control is disabled; the wheel always scrolls the whole conversation list.
Use `Ctrl+滚轮` to adjust the font size.

### I want to report a problem
Please include: `backend\backend_crash.log` (if it has content), `backend\config.json`, the output of
`/status`, and the steps that reproduce the problem.
