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

## Local HTTP server: DNS-pinned address could not be reached

`Could not connect to any DNS-pinned address for 127.0.0.1` means Studio tried
the validated IP address but could not establish a TCP connection. It is not a
request to disable DNS pinning or permit more private hosts. Common causes are a
server that is not running, the wrong port, or a service running in a different
container/network namespace. The error alone cannot distinguish those causes.

The **Custom Remote / Docker HTTP MCP** preset's `http://127.0.0.1:8080/mcp` is
an example endpoint, not an MCP service bundled with or started by Studio.
Configure the actual MCP server's URL and start that trusted service separately.
`127.0.0.1` means the environment running Studio, not necessarily the browser or
the host outside a container. Do not switch to stdio unless that particular server
supports it, and do not assume every local HTTP API implements MCP.

Use the server's own documented listener/health check to verify its host and port
in the Studio runtime. A reachable HTTP endpoint may still reject an ordinary
GET, so a 400/405 is different from a failed TCP connection. A wrong `/mcp` versus
`/sse` path typically produces an HTTP response, not this connection error.
Do not share authorization headers, credentials or token-bearing URLs when
requesting help; the server name and credential-free scheme/host/port/path suffice.

## Python starts but cannot open a script whose path contains spaces

If Python reports it cannot open `/Users/you/Documents/Test` when the intended
path is under `Test Studio`, it received only part of the script path as its first
argument. The executable was found; installing Python again will not fix this.

The MCP editor sends its Arguments text to the backend's `shlex.split` parser.
An unquoted path containing spaces becomes several arguments. Older Studio
preset/edit flows flattened argument arrays with a plain space join, so this
could happen even when the preset originally contained the correct complete path.

The corrected preset/edit flows quote individual arguments, including spaces,
apostrophes, backslashes and empty values. These quotes are parsed into an argument
array; Studio still launches without a shell, so `$HOME`, `$(...)` and shell
metacharacters are not expanded or executed. Do not put a command and all its
arguments into the Executable Command field.

For an existing broken MoneyPrinter entry, edit **Arguments** to one quoted path:

```text
'/Users/you/Documents/Test Studio/m5-agent-studio/moneyprinter_mcp.py'
```

Leave **Executable Command** as the actual, **unquoted** Python executable path:

```text
/Users/you/Documents/Test Studio/m5-agent-studio/.venv/bin/python
```

Replace the example paths with your own, review the launch command and consent,
then save/test again. This needs no directory rename. Existing arguments already
saved as multiple tokens cannot be safely repaired automatically: some servers
legitimately need multiple positional arguments. Replace the broken script path
manually, or reselect the corrected catalog preset and recheck any custom alias,
environment and policy settings before saving. This path fix does not install the
server's dependencies or remove MoneyPrinter's 45-second Studio tool-call limit.
