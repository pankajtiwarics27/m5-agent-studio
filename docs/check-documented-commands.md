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
value the reader cannot know, so the line is **skipped and counted**, never checked and passed. The
skip count also includes lines with no arguments, shell text the reader cannot parse, incomplete
continuations, and opted-out counter-examples; it is not a count of placeholders alone. Complete simple
backslash continuations are joined before parsing, preserving their starting line number. A placeholder
on any continuation line skips the whole command. An unfinished continuation at a Markdown boundary or
before another command is skipped, without swallowing the next command. This is not a general shell
parser: skipped commands have not been validated. Inline shell comments are excluded from arguments;
commands containing `;` or `&` are skipped rather than checked as one invocation, even when those
characters are quoted. Continuations with single quotes before a join are conservatively skipped,
because the reader does not determine shell quote state. The output lists each skipped command's
repository-relative path and line, and names scripts it could not read statically — a checker that
silently reads nothing would pass forever.

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
tell", the second says "the page is wrong", and only one of them is evidence. In particular, a script
without the text `sys.argv` may still read `from sys import argv`, so absence of that text is not
proof that it accepts no options. A comment mentioning `argparse` is not proof of a readable parser.
The Python reader also abstains on non-Python files, invalid Python syntax and files it cannot decode.

These cases have regression tests: each was checked with a mutation that removed the relevant
abstention or guard, and the restored test files passed afterward. This does not make arbitrary Python
argument handling statically readable; the scripts in the unreadable list remain unchecked.

Each invocation is read using its documented interpreter. Caching by path alone allowed a valid
`python` invocation to lend its result to a `node` invocation of the same Python file, or an unreadable
invocation to hide a readable one, depending on order. Mixed-interpreter tests cover both orders for
Python and JavaScript files; mutations restoring the path-only cache and the first-invocation-only
unresolved list were caught, and the restored tests passed. An interpreter/file mismatch remains
unchecked, not validated by the other interpreter's reader.

## The node half, added after the python half shipped

The first version of this checker read `python scripts/*.py` lines and nothing else. That covered the
prose it was written for and left the other half of the documentation unchecked: every
`node scripts/*.mjs ...` invocation in these pages, including the two in `ap032-pilot-harness.md` that
an operator is told to run before a pilot. A page can be wrong about a `node` flag exactly as it was
wrong about `--root`.

`scripts/js_documented_flags.py` reads `process.argv` handling the way this module reads `argparse`:
from the source, without running anything. It understands the three shapes these walks use — a
`switch` with a `next()` value getter, an `if`-chain ending in an unknown-flag `throw`, and a
`process.argv.includes("--flag")` boolean — and abstains on anything else.

It makes the same mistake the Python half made, and avoids it the same way. A flag name is read only
from the code that handles `process.argv`, found by brace-matching the function that mentions it, not
from every string in the file: a `"--dark"` compared while rendering is not an accepted flag. When
handling happens inline at top level, where a line scan sees only the `for` header, the reader falls
back to the whole file and marks the result **widened**, because a file-wide set is a union over code
that need not all run at once.

Two abstentions are load-bearing:

- a flag built from a variable (`case FLAG:`, `` `--${name}` ``) or matched by prefix
  (`arg.startsWith("--")`) means the accepted set is not closed, so it is *not readable*;
- a walk without a literal `process.argv` reference is **not readable**. The absence of that spelling
  does not prove zero options: `import { argv } from "node:process"`, `process["argv"]`, or an imported
  argument handler can read them indirectly. This conservative rule may also abstain on a script that
  truly takes no options; it does not attempt to resolve arbitrary imported code. A mutation restoring
  the zero-options inference was caught separately by the plain-script test, all three indirect-access
  variants, and the end-to-end unchecked-report test; the restored checker tests passed.

An unknown-flag guard — `else if (arg.startsWith("-")) throw` — is the opposite of a dynamic name: it
*rejects* everything unrecognised, which is what makes the literal set trustworthy. The reader
distinguishes the two; an early version of it did not and abstained on the pilot walk itself.

Current result: **162 documented command starts: 55 skipped, 101 checked, 6 unchecked across 2
unreadable scripts, and 0 problems**. The counts changed because shell operators are now skipped and
comments no longer make a no-argument command look like an invocation with arguments. Previously this page reported 107 checked: the summary subtracted
six distinct script names from 113 literal invocations, even though those scripts occur on nine lines.
The count now subtracts invocations, not names. A JavaScript file-wide or multi-region flag union is
also named as a weaker check in the repository report, rather than only inside the JavaScript reader.

Regression tests were mutation-checked against the old count, a missing unchecked list, an inaccurate
runtime-parser diagnosis, omitted or over-broad JavaScript widening, and a suppressed warning. All six
mutations were caught, and the restored Python and JavaScript command-checker tests passed. Seven more
mutations tested Python and Node continuations, exact shell joining, escaped backslashes, placeholders,
boundaries, and malformed counter-examples; each was caught and the restored tests passed. Seven further
mutations covered shell operators, comment handling, continuation boundaries, quote uncertainty and
skipped-location reporting; each was caught and the restored tests passed.

## Verification

`tests/test_check_documented_commands.py` (25 tests) and `tests/test_js_documented_flags.py` (12).
Eight mutations, each caught: dropping subcommand scope, reporting an abstention as a finding,
checking placeholder lines, not resolving loop-built subparsers, letting an unknown parser leak to the
root, dropping the widened report, routing every command to the Python reader, and ignoring `node`
lines again. The widened test originally asserted `static or reason`, which passes either way — it now
asserts the specific failure, because a vacuous test is not a guard.

The JavaScript reader was mutation-tested separately, and its first test suite failed the check that
matters: mutating *strip comments* and *scope to the argv region* both survived. The comment test had
used a flag merely *mentioned* in prose, which cannot reach the flag patterns at all — the realistic
case is commented-out code, and with that the mutation is caught. The scope test did not exist; it
does now, and it fails when flags are read from the whole file. A third survivor — falling back to a
file-wide scan instead of abstaining — is now covered by the widened assertion, so a weaker result
cannot be reported as a clean one.

The end-to-end check is a test rather than a claim: `--out` in the pilot runbook's own documented
command is changed to `--output`, and the gate must fail.
