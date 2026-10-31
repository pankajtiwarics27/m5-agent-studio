# Test-store isolation follow-up

Issue [#117](https://github.com/pankajtiwarics27/m5-agent-studio/issues/117)
reports that tests can modify the source checkout's harness session data even
when HOME and the database are isolated. A scoped investigation is being
coordinated on branch `arena/21d0f2f5-m5-agent-studio`; it does not claim the
UI/UX screenshot story or alter either program's story tracking.

Until a broader fix has been reviewed and verified, run the full test suite in a
**disposable clone or detached worktree with an empty harness store**. Tests and
reproductions must use synthetic sentinel data, not a real checkout's state.
Demo isolation ([#108](https://github.com/pankajtiwarics27/m5-agent-studio/issues/108)),
the P0 baseline, and screenshot pipeline review are separate concerns.
