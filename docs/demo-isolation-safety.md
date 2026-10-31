# Demo isolation follow-up

The demo runner uses a throwaway root for its database, API token, workspace and
harness session store. The default root is created under the system temporary
directory; `--root` explicitly reuses a caller-selected directory, so select a
**dedicated disposable root** rather than a real workspace or a shared folder.
The runner rejects roots that overlap the source checkout and rejects demo paths
that resolve outside the root. Existing data in an explicitly reused root is not
cleared or treated as private from the authenticated demo user.

Both the in-process transcript runner and the loopback server configure the
modular app's harness store before startup, without changing the app's normal
production store or the generated standalone. They fail if the app was already
imported in the same process, rather than reusing an app with unknown settings.
The demo also removes the app's known provider and MCP preset credential variables
from its process before startup. Its bearer token remains required and host
execution remains disabled. These measures do not make the demo a sandbox for an
untrusted authenticated user: explicit workspace selections and actions can still
access paths permitted by the app and operating system.

`tests/test_demo_isolation.py` uses a disposable copy with a synthetic source-store
sentinel. It checks the transcript mode and a real authenticated loopback server,
including a workspace preference update, without reading or changing that source
sentinel. The test also checks a synthetic inherited provider credential, an
already-imported app, and root/symlink escapes. It does not exercise the generated
`studio.py`, because the demo runner invokes the modular app only.

This is a proposed fix, not acceptance of the UI/UX baseline. Until it is reviewed
and merged, continue to use disposable source copies for demo/screenshot
investigations ([#108](https://github.com/pankajtiwarics27/m5-agent-studio/issues/108)).
The broader test-store isolation issue ([#117](https://github.com/pankajtiwarics27/m5-agent-studio/issues/117))
and route-map review hold ([#110](https://github.com/pankajtiwarics27/m5-agent-studio/issues/110))
are separate and remain open. The screenshot story and P0 sign-off have their own
owners and acceptance criteria.
