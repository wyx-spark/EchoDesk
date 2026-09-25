# Screenshots

**v0.2.1 ships without screenshots.** This folder exists so that the convention is unambiguous and
no document has to link to an image that is not there yet: README and the user guides describe
*what to look at on screen* instead of embedding pictures.

Future releases will place interface previews here, for example:

| File | What it should show |
| --- | --- |
| `main-window.png` | the main window (both themes) |
| `long-document-summary.png` | a long document being summarised in chunks |
| `automation-tab.png` | the Settings → Automation tab, including the safety protocol area |
| `selector-test.png` | the "Test selectors" wizard succeeding |
| `automation-run.png` | a headed automation run in progress |
| `skill-marketplace.png` | the skill marketplace |

## Contributing a screenshot

1. Run the portable build, set the language and theme you want to document, and resize the window
   to a sensible size (about 1280×800).
2. **Redact everything personal first**: file paths, user names, document content, URLs, mail
   addresses, tokens. Screenshots are published — treat them like any other release asset
   (see [`../../SECURITY.md`](../../SECURITY.md) for what must never be committed).
3. Use PNG, keep each file under ~500 KB, and add the file to the table above in a PR.

Because personal data leaks easily through screenshots, they are reviewed like code.
