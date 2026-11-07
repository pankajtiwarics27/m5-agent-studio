# Local MCP command troubleshooting

## Saved, but connection test failed: `No such file or directory: 'npx'`

The server entry was saved; Studio could not start its local program, so no MCP
connection was established. This is not an HTTP authentication or protocol error.

Studio deliberately gives local MCP programs a restricted `PATH` (the Python
interpreter directory plus the OS default). It does **not** inherit the Terminal's
full search path, and `PATH` supplied through environment JSON is ignored.
Homebrew, nvm, Volta and other Node installations may therefore be available in
Terminal but unavailable to Studio's MCP child process. Studio running inside a
container needs the dependency inside that container, not just on the host.

Check in the same machine/runtime where Studio runs:

```sh
command -v node
command -v npx
node --version
npx --version
```

If either command is absent, install Node.js with npm through your normal trusted
installation process first. Studio does not install Node or npm packages for you.

### Use a reviewed, server-specific launcher

An absolute `npx` path alone may still fail: its shebang, or the MCP package it
starts, may use `/usr/bin/env node`, which still searches the restricted PATH.
Instead, save a small launcher you control, using the **actual absolute paths**
from the commands above. Replace both placeholders below; do not run as-is:

```sh
#!/bin/sh
PATH="/absolute/directory/containing/node:/usr/bin:/bin"
export PATH
exec "/absolute/path/to/npx" "$@"
```

Save it outside the checkout, for example as `~/bin/studio-npx.sh`, review its
contents, and make it executable (`chmod 700 ~/bin/studio-npx.sh`). On Windows,
use a reviewed platform-appropriate launcher instead of this POSIX shell example.

In **Tools & Integrations → MCP Tools Hub → Connected → Edit**:

1. Set **Executable Command** to the launcher's absolute path. The field is one
   executable path, not a shell command; do not add shell quotes around the field.
2. Keep the MCP server's original **Arguments** unchanged. Quote individual
   arguments containing spaces. Review package/version choices before approving.
3. Review the updated command, retain an appropriate approval policy, and grant
   explicit consent. Save and test the connection again.

Local launch still requires the operator's `STUDIO_ALLOW_MCP_STDIO=1` opt-in.
The launcher explicitly supplies only its reviewed Node search directory; Studio
continues stripping loader/startup injection variables and unrelated credentials.
Do not copy your entire shell environment or credentials into the MCP config.

## MoneyPrinterTurbo is a separate local tool

The catalog entry **MoneyPrinterTurbo AI Video Generation** starts the repository's
`moneyprinter_mcp.py` using Studio's Python interpreter, not `npx`. It appears in
Catalog/Local after the updated Studio server is restarted and the page reloaded.
It requires the full checkout, since the standalone bundle does not embed the
separate wrapper file. Connecting discovers `moneyprinter_video_generate`; it
does not render a video or install MoneyPrinterTurbo.

For rendering, set `MONEYPRINTER_TURBO_DIR` in the server's environment JSON to a
separately installed and configured MoneyPrinterTurbo checkout. That installation
also needs `uv`, `ffmpeg`, and its own provider configuration. The wrapper's child
process also uses a restricted environment; ensure those executables are reachable
there before attempting a render. Never assume a working Terminal PATH proves it.

**Current limitation:** Studio's MCP tool-call budget is 45 seconds. Normal video
renders can exceed this. The catalog card is for discoverability/configuration,
not a claim that long-running video generation is production-ready in Studio.
See [the integration contract](moneyprinter-turbo-integration.md).
