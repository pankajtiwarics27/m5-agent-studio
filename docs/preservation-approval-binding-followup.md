# Preservation approval binding follow-up

This branch follows up [#126](https://github.com/pankajtiwarics27/m5-agent-studio/issues/126)
without changing the recorded owner decision or taking ownership of a UI/UX story.
The existing signature records a historical decision, but does not identify the
exact input hashes approved. The generator currently places it next to hashes of
current artifacts, even after those artifacts change.

The investigation will keep historical approval distinct from evidence that
current inputs match an explicitly approved snapshot. It will not retroactively
invent a binding for the existing signature: migration of that record and any new
approval require an owner decision. It will test legacy, unsigned, matching and
drifted cases and retain the separate screenshot provenance hold in
[#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122).
