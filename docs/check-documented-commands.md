# Checking that the documented commands exist

<!-- doc-command-check: counter-example — this page quotes a wrong command to explain it, which is the
     opposite of instructing anyone to run it. The checker prints this exemption by name. -->

`scripts/check_documented_commands.py` reads every `python scripts/<tool>.py ...` line in the docs and
checks each flag against the script's `argparse` wiring. It runs in CI after `validate_docs.py`.

## The defect it answers

`docs/packaging-rollback.md` documented:

```text
python scripts/release.py package --version 9.0.1 --root ~/.local/share/m5-agent-studio
```

The page above carries the `doc-command-check: counter-example` marker, because a page that shows a
wrong command to explain it is not telling anyone to run it. The checker prints every such exemption by
name, so the opt-out is visible rather than silent.

`package` does not accept `--root`. It stages into `--output`, and `install`/`update` read the staging
directory through `--from-dir`. Following that page produced `unrecognized arguments: --root` and no
release. The page was wrong for as long as it existed and nothing noticed, because prose about a
command is not tied to the command.

## What it checks, and what it deliberately does not

| It checks | It does not check |
|---|---|
| every `--flag` on a literal documented line exists in that subcommand's scope | whether the flags shown are *enough* to do the job |
| a subcommand's flags are its own, not the file's | whether the command does what the prose says |
| an unresolvable parser is reported as unchecked | anything after an angle-bracketed stand-in, `$VAR`, `\|` or `...` |

The last row is the important one. A line like `--root` followed by an angle-bracketed stand-in describes a
value the reader cannot know, so the line is **skipped and counted**, never checked and passed. The output prints how many
lines were checked, how many had placeholders, and which scripts could not be read statically — a
checker that silently reads nothing would pass forever.

## Static, not executed

The scripts are read, not run. Several documented scripts are generators that write files and one
starts a server: a documentation check that executed them would edit the tree it is checking. During
development of this module an "execute `--help` on each documented script" prototype ran
`generate_route_matrix.py`, which writes its output before failing. The tree was clean afterwards,
because the file it rewrote was already current — that is luck, not safety.

## Scope, which is where the first version of this checker failed

The obvious implementation collects every `add_argument` in the file and checks each documented flag
against the union. That version was written first and **reported `package --root` as valid**, by
borrowing `--root` from `install` — the original defect reproduced inside its own detector.

It now keeps the root parser's flags apart from each subparser's, and checks a command against the
union of the flags in its own scope. A subparser built in a loop (`for name in ("install", "update")`)
is resolved when the names are literals, and those commands are reported as **widened**: one iteration
may add a flag another does not, so a pass there is weaker than a pass elsewhere. The report says so
rather than implying precision it does not have.

## Abstention

A parser this reader cannot follow — no `argparse`, non-literal flags, or a parser variable it never
saw created — is `static: False`, and every command against it is reported as *not checked*.

It is never reported as *accepting nothing*. Those two are different: the first says "I could not
tell", the second says "the page is wrong", and only one of them is evidence.

## Verification

`tests/test_check_documented_commands.py` (18 tests). Six mutations, each caught: dropping subcommand
scope, reporting an abstention as a finding, checking placeholder lines, not resolving loop-built
subparsers, letting an unknown parser leak to the root, and dropping the widened report. The widened
test originally asserted `static or reason`, which passes either way — it now asserts the specific
failure, because a vacuous test is not a guard.
