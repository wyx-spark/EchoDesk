# models directory

> A Chinese reference translation is available at [README.zh-Hans.md](README.zh-Hans.md); if the two ever disagree, the English version prevails.

Model files (such as `Qwen3-0.6B-Q6_K.gguf`) go in this directory.

If the program finds no local model at start-up, it downloads one from Hugging Face and loads it
automatically:
- The mirror is configured through `hf_endpoint` in `config.json`; the default is
  `https://hf-mirror.com` (reachable from mainland China)
- `model_repo` defaults to `unsloth/Qwen3-0.6B-GGUF`
- Before downloading it does a fuzzy match in the repository to resolve the file name that actually
  exists (such as `Qwen3-0.6B-Q6_K.gguf`), then writes the match back to `model_path` /
  `models.*.path` in `config.json`, so the next start uses it directly

You can also use the `/model <name>` command to list, switch or manually download models; or put a
model file into this directory yourself and point `model_path` in `config.json` at it.

## Which directory the program actually loads models from

`deploy/models/` is a **build-time** source of models, not the directory the running program reads.
Treating the two as the same thing is the usual reason a model "was put in place but does not take
effect":

| Directory | Who uses it | How it is resolved |
| --- | --- | --- |
| `deploy/models/` | `tools/build_release.ps1 -IncludeModels` | build time only: the `.gguf` files here are copied into the release's `app/backend/models/` |
| `models/` under the backend working directory | the running backend | `model_hub.models_dir` in `config.json` (default `models`) is resolved to an absolute path against the **backend's own working directory** |

The frontend sets that working directory to the folder the backend lives in, so the real location
depends on how EchoDesk was installed:

| How EchoDesk is installed | Backend working directory | Put the `.gguf` here |
| --- | --- | --- |
| Windows installer / portable zip | `<release directory>\app\backend\` | `<release directory>\app\backend\models\` |
| macOS / Linux archive | `<release directory>/app/backend/app/` | `<release directory>/app/backend/app/models/` |
| Source checkout (backend run from `backend/`) | `backend/` | `backend/models/` |

You never have to guess: when no model is available the program's guidance message prints the
resolved models folder ("Models folder: …"), and `model_hub.models_dir` in `config.json` can point
it somewhere else.

## Three-platform commands

The release directory layout is the same on all three platforms; only the copy command and the path
separator differ:

```
# Windows (PowerShell)
Copy-Item model.gguf "<release directory>\app\backend\models\"

# macOS / Linux
cp model.gguf "<release directory>/app/backend/models/"
```

In a source checkout the target becomes `backend/models/` (`backend\models\` on Windows).

Two platform differences are worth spelling out:

- **Windows**: the installer's default target is **the folder the installer itself sits in**
  (`GetDefaultDir` in `tools/installer.iss`), so the "release directory" is wherever you put
  `EchoDesk-<version>-Setup.exe`. The installer **does not contain models**: it excludes `*.gguf`,
  so either download one on first start or drop your own file in.
- **macOS / Linux**: the release is a plain archive with no installer — unpack it into any writable
  directory. If the executable bit was lost while extracting, see the
  [user guide](../../docs/en/USER_GUIDE.md).

The model-hub search cache is the only model-related path in this document that is not inside the
release directory: with `model_hub.search_cache_dir` left empty it lands in `cache/model_hub` under
the user data directory, which per platform is `~/Library/Application Support/EchoDesk` (macOS),
`$XDG_DATA_HOME/EchoDesk` or `~/.local/share/EchoDesk` (Linux), and on Windows the portable outer
directory, or whatever `ECHODESK_DATA` points at.
