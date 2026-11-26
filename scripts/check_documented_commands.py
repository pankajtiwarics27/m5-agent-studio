"""Check that the CLI flags in the documentation exist on the scripts they claim to drive.

`docs/packaging-rollback.md` documented `release.py package --version X --root <root>`. The script does
not accept `--root` on `package`; an operator following the page got `unrecognized arguments` and no
release. The page was wrong for as long as it existed and nothing noticed, because prose about a
command is not tied to the command.

This module ties them: it reads the documentation, finds every `python scripts/<tool>.py ...` and
`node scripts/<walk>.mjs ...` line, reads the tool's argument wiring **statically** and reports a flag
the tool does not accept.

The Python side reads `argparse` with `ast`. The JavaScript side reads `process.argv` handling with
`js_documented_flags`, which abstains rather than guessing when a walk builds a flag name at runtime.
It was added after the Python half shipped: 43 documented `node` invocations, including the pilot walk
that this programme's own runbook tells an operator to run, were unchecked by a page that claimed to
check documented commands.

Static parsing is deliberate. Running the scripts would be the stronger check and is the wrong method
here: several are generators that write files and one starts a server, so a documentation check that
executed them would edit the tree it is checking. The parser is read, not run.

What it cannot see, and does not claim to:

* whether the flags shown are *sufficient* -- a documented command may be incomplete;
* flags accepted through `**kwargs` or a parser built at runtime, which are reported as unchecked
  rather than as errors;
* whether the command does what the prose says. That is what the runbook drills are for.

Deliberately narrow: a check that guesses is worse than a check that abstains, so anything it cannot
resolve is skipped and counted, never passed silently.
"""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

try:  # imported as scripts.check_documented_commands (tests) or run as a script (CI)
    from .js_documented_flags import flags_a_js_script_accepts
except ImportError:  # pragma: no cover - the script entry point
    from js_documented_flags import flags_a_js_script_accepts

__all__ = [
    "DocCommand",
    "DocFlagProblem",
    "DocFlagReport",
    "check_repository",
    "documented_commands",
    "ParserShape",
    "flags_a_script_accepts",
    "unresolved_scripts",
]

_ROOT = Path(__file__).resolve().parent
#: A documented invocation, with the interpreter kept so the right reader is used for the script.
#: `python scripts/x.py` and `node scripts/x.mjs` are both instructions; only the first was checked.
_COMMAND = re.compile(
    r"^\s*\$?\s*(?P<interpreter>python3?|node)\s+(?P<script>scripts/[\w./-]+\.(?:py|m?js|cjs))\b(.*)$"
)
#: Lines carrying shell metacharacters describe a shape, not an invocation. `<...>`, `$VAR`, `|` and
#: `...` all mean "substitute something here", so the flags after them are not checkable.
_NOT_A_LITERAL = re.compile(r"[<>$|`]|\.\.\.")
#: A document that quotes a wrong command in order to explain why it is wrong is not instructing
#: anyone to run it. Rewriting such an example to look correct would destroy its point, so the document
#: opts out with this marker, and the report prints every opt-out by name so the exemption is visible
#: rather than silent.
COUNTER_EXAMPLE_MARKER = "doc-command-check: counter-example"


@dataclass(frozen=True)
class DocCommand:
    """One `python scripts/tool.py ...` or `node scripts/walk.mjs ...` line in the documentation."""

    document: str
    line: int
    script: str
    tokens: tuple[str, ...]
    interpreter: str = "python"

    @property
    def text(self) -> str:
        binary = "python" if self.interpreter.startswith("python") else "node"
        return " ".join((f"{binary} {self.script}", *self.tokens))


@dataclass(frozen=True)
class DocFlagProblem:
    document: str
    line: int
    script: str
    flag: str
    accepted: tuple[str, ...]
    subcommand: str = ""

    def describe(self) -> str:
        near = ", ".join(self.accepted[:8]) or "no options at all"
        scope = f"`{self.script} {self.subcommand}`" if self.subcommand else self.script
        return (
            f"{self.document}:{self.line} documents `{self.flag}` for {scope}, which does not "
            f"accept it (accepts there: {near})"
        )


@dataclass
class DocFlagReport:
    checked: list[DocCommand] = field(default_factory=list)
    skipped: list[DocCommand] = field(default_factory=list)
    problems: list[DocFlagProblem] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    #: script -> subcommands whose flags were widened across loop iterations, so a pass there is
    #: weaker than a pass elsewhere. Reported, never hidden.
    widened: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: documents exempted because they quote a wrong command as a counter-example.
    counter_examples: list[str] = field(default_factory=list)
    #: scripts the reader could confirm take no options at all, without `argparse`. Zero flags is an
    #: answer, not an abstention -- and naming them makes it visible that a documented flag there would
    #: be caught, rather than silently exempted.
    accepts_nothing: list[str] = field(default_factory=list)
    #: scripts whose flags were read from a hand-rolled argv check rather than `argparse`. Readable, but
    #: weaker, because the patterns cover only what the source spells out. Reported, never hidden.
    hand_rolled: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _markdown_files(root: Path) -> Iterator[Path]:
    yield from sorted(root.glob("docs/*.md"))
    readme = root / "README.md"
    if readme.is_file():
        yield readme


def documented_commands(root: Path) -> tuple[list[DocCommand], list[DocCommand]]:
    """Return (checkable, skipped) documented invocations.

    A line is skipped when it carries a shell metacharacter: the documentation uses `<root>` and
    `$STUDIO_DB_PATH` to stand for values, and a flag next to a placeholder cannot be checked without
    guessing what the placeholder becomes.
    """
    checkable: list[DocCommand] = []
    skipped: list[DocCommand] = []
    for document in _markdown_files(root):
        if COUNTER_EXAMPLE_MARKER in document.read_text(encoding="utf-8"):
            skipped.extend(
                DocCommand(
                    document.name,
                    number,
                    match.group("script"),
                    tuple(shlex.split(match.group(3))),
                    interpreter=match.group("interpreter"),
                )
                for number, match in (
                    (number, _COMMAND.match(text))
                    for number, text in enumerate(document.read_text(encoding="utf-8").splitlines(), 1)
                )
                if match is not None
            )
            continue
        for number, text in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            match = _COMMAND.match(text)
            if match is None:
                continue
            interpreter, script, rest = match.group("interpreter"), match.group("script"), match.group(3).strip()
            if _NOT_A_LITERAL.search(rest) or not rest:
                skipped.append(
                    DocCommand(
                        document.name,
                        number,
                        script,
                        tuple(shlex.split(rest) if rest and not _NOT_A_LITERAL.search(rest) else ()),
                        interpreter=interpreter,
                    )
                )
                continue
            try:
                tokens = tuple(shlex.split(rest))
            except ValueError:
                skipped.append(DocCommand(document.name, number, script, (), interpreter=interpreter))
                continue
            command = DocCommand(document.name, number, script, tokens, interpreter=interpreter)
            (skipped if not tokens else checkable).append(command)
    return checkable, skipped


@dataclass(frozen=True)
class ParserShape:
    """The flags a script accepts, kept apart by subcommand.

    Scope is the whole point. `release.py` accepts `--root` on `install`, `update`, `rollback`,
    `verify`, `status` and `prune`, and *not* on `package`. A reader that unions every `add_argument`
    in the file reports `package --root` as valid -- which is the exact defect this module was written
    to catch, reproduced inside the module. So the shape keeps the root flags and one set per
    subparser, and a command is checked against the union of the flags in its own scope.
    """

    root_flags: frozenset[str]
    subcommand_flags: Mapping[str, frozenset[str]]
    static: bool
    reason: str = ""
    #: Subcommands whose flags were read from a loop body, where one iteration may add a flag another
    #: does not ("if name == 'update'"). Their flag sets are a union over the iterations, so they can
    #: accept a flag in this report that the real subparser rejects. Recorded so the report can say so
    #: instead of implying a precision it does not have.
    widened: frozenset[str] = frozenset()


def _add_argument_flags(call: ast.Call) -> set[str] | None:
    """Flags from one `add_argument` call, or None when they are not literals."""
    flags: set[str] = set()
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if arg.value.startswith("-"):
                flags.add(arg.value.split("=")[0])
        elif isinstance(arg, (ast.Name, ast.JoinedStr, ast.BinOp, ast.Attribute)):
            return None
    for keyword in call.keywords:
        if keyword.arg and keyword.arg.startswith("-"):
            flags.add(keyword.arg)
    return flags


def _receiver_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return ""


def _loop_bound_names(tree: ast.Module) -> dict[str, set[str]]:
    """`for name, ... in (("install", ...), ("update", ...)):` binds `name` to a known finite set.

    `scripts/release.py` builds `install` and `update` in one loop body, so their `add_argument` calls
    attach to a variable, not to a literal parser. Reading the loop's literal tuple recovers the names
    without executing anything, which is what keeps those two subcommands checkable instead of unknown.
    """
    bound: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        elements = node.iter.elts
        if not elements:
            continue
        # `for name in ("a", "b")` binds every element; `for name, handler in (("a", f), ("b", g))`
        # binds the first element of each literal tuple.
        names: set[str] = set()
        for element in elements:
            first = element.elts[0] if isinstance(element, (ast.Tuple, ast.List)) and element.elts else element
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
            else:
                names = set()
                break
        if not names:
            continue
        targets = node.target.elts if isinstance(node.target, (ast.Tuple, ast.List)) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                bound[target.id] = names
    return bound


def flags_a_script_accepts(path: Path) -> ParserShape:
    """Read a script's parser without running it.

    ``static`` is False when the shape cannot be resolved: a subcommand whose name is not resolvable
    from literals, or an `add_argument` whose flags are not literals. A caller must treat that as
    "unknown", never as "accepts nothing" -- and, just as important, an unresolvable parser's flags
    must never be attributed to the root, or they would validate a flag the subcommand does not take.

    A script with no `argparse` is not necessarily unknown. Five documented scripts take no options at
    all, and one reads argv by hand:

    * `validate_docs.py`, `validate_security_signoff.py`, `check_frontend_syntax.py` and
      `build_studio.py` take no arguments whatsoever. "Accepts nothing" is a real answer for them, and
      a documented flag against one is a real problem. Reporting them as unreadable left six
      documented invocations unchecked because of a property nothing could go wrong with.
    * `smoke_local.py` reads `"--standalone" in sys.argv[1:]`. That is a closed set of literal flags
      in a hand-rolled parser: readable, and marked `hand-rolled` so the weaker result is visible.

    Anything else that touches `sys.argv` or `os.environ`-driven flags without `argparse` still
    abstains, because a wrong guess there is worse than an abstention.
    """
    if path.suffix != ".py":
        # The Python reader must never answer for a JavaScript walk: a `.mjs` file has no `argparse`,
        # which this reader would otherwise report as "takes no options". That is the wrong answer,
        # confidently given. `accepts_for` routes by interpreter; this guard makes a mis-route an
        # explicit abstention instead of a silently empty flag set.
        return ParserShape(frozenset(), {}, False, f"{path.suffix or 'no extension'} is not a Python script")
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return ParserShape(frozenset(), {}, False, "unreadable")
    if "argparse" not in source:
        return _parser_shape_without_argparse(source)
    tree = ast.parse(source)
    loop_names = _loop_bound_names(tree)

    # Which variable holds a parser, and for which subcommand names.
    subcommands: dict[str, set[str]] = {}
    root_parsers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not targets:
            continue
        if isinstance(call.func, ast.Attribute) and call.func.attr == "add_parser":
            if not call.args:
                return ParserShape(frozenset(), {}, False, "add_parser with no name")
            name_node = call.args[0]
            if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                resolved = {name_node.value}
            elif isinstance(name_node, ast.Name) and name_node.id in loop_names:
                resolved = loop_names[name_node.id]
            else:
                return ParserShape(frozenset(), {}, False, "subcommand name is not resolvable from literals")
            for target in targets:
                subcommands.setdefault(target, set()).update(resolved)
        elif isinstance(call.func, ast.Attribute) and call.func.attr in {"ArgumentParser", "add_subparsers"}:
            root_parsers.update(targets)

    root_flags: set[str] = set()
    per_subcommand: dict[str, set[str]] = {name: set() for names in subcommands.values() for name in names}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
            continue
        flags = _add_argument_flags(node)
        receiver = _receiver_name(node)
        if flags is None:
            return ParserShape(frozenset(), {}, False, "an add_argument call uses non-literal flags")
        if receiver in subcommands:
            for name in subcommands[receiver]:
                per_subcommand[name] |= flags
        elif receiver in root_parsers:
            root_flags |= flags
        elif not receiver:
            return ParserShape(frozenset(), {}, False, "add_argument called on an expression this reader cannot name")
        else:
            # A parser variable we never saw created: attributing its flags to the root is how
            # `package --root` would be reported as valid. Abstain instead.
            return ParserShape(frozenset(), {}, False, f"parser variable `{receiver}` was not created here")

    widened = frozenset(name for names in subcommands.values() if len(names) > 1 for name in names)
    # An `argparse` script that declares no flag still accepts no flags; that is a closed set, not an
    # unresolvable one. Reading it as unknown would abstain on a script that cannot accept anything.
    return ParserShape(
        root_flags=frozenset(root_flags),
        subcommand_flags={name: frozenset(flags) for name, flags in per_subcommand.items()},
        static=True,
        reason="",
        widened=widened,
    )


#: Hand-rolled membership tests that name a flag literally: `"--standalone" in sys.argv[1:]`.
_ARGV_MEMBERSHIP = re.compile(r"""['"](--?[A-Za-z][\w-]*)['"]\s+in\s+(?:sys\.argv|argv)""")
#: A flag compared against an argv element: `sys.argv[1] == "--json"`.
_ARGV_COMPARE = re.compile(r"""(?:sys\.argv\[\d+\]|argv\[\d+\])\s*==\s*['"](--?[A-Za-z][\w-]*)['"]""")


def _parser_shape_without_argparse(source: str) -> ParserShape:
    """Read a hand-rolled parser, or say why it cannot be read.

    Only three answers are honest here, and they are different answers:

    * the script never reads `sys.argv` -- it accepts nothing, which is a closed set of zero flags, so
      a documented flag against it is a problem rather than something to abstain on;
    * it reads `sys.argv` by comparing literal flags -- readable, but `hand-rolled`, and the report
      names that as a weaker check than an `argparse` wiring;
    * it reaches `sys.argv` in any way this reader cannot close over -- unreadable, because a flag may
      be accepted under a name the source does not spell out.

    `reason` is only ever set on the third answer. It means "why this is unknown", so filling it in on
    a readable script would label a known result as an unknown one.
    """
    if "sys.argv" not in source:
        return ParserShape(frozenset(), {}, True, "none")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ParserShape(frozenset(), {}, False, "not parseable as Python")
    flags = frozenset(_ARGV_MEMBERSHIP.findall(source)) | frozenset(_ARGV_COMPARE.findall(source))
    if not flags:
        return ParserShape(
            frozenset(), {}, False, "reads sys.argv but names no literal flag, so the set is not closed"
        )
    # `sys.argv[0]` is the program path and `sys.argv[1:]` is how these scripts reach their arguments;
    # neither can hide a flag name. An argv element selected by a concrete index and *not* compared
    # with a literal in the patterns above is evidence of a name this reader did not see.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Attribute):
            continue
        if node.value.attr != "argv":
            continue
        index = node.slice
        if isinstance(index, ast.Slice):
            continue
        if isinstance(index, ast.Constant) and index.value == 0:
            continue
        return ParserShape(
            frozenset(), {}, False, "selects an argv element the literal comparisons do not cover"
        )
    return ParserShape(root_flags=flags, subcommand_flags={}, static=True, reason="hand-rolled")


def accepts_for(command: DocCommand, root: Path) -> tuple[frozenset[str], bool]:
    """The flags a documented command's script accepts, and whether they were readable.

    Dispatch is on the interpreter, not the file extension: the reader decides whether a `.py` script's
    `argparse` or a `.mjs` walk's `process.argv` handling is the source of truth. When the two disagree
    about readability — one says static, the other does not — the answer is "not readable", because a
    flag must never be validated by a reader that did not understand the script.
    """
    path = root / command.script
    if command.interpreter.startswith("python"):
        shape = flags_a_script_accepts(path)
        return frozenset(shape.root_flags) | frozenset().union(*shape.subcommand_flags.values()) if shape.static else frozenset(), shape.static
    js = flags_a_js_script_accepts(path)
    return js.flags, js.static


def unresolved_scripts(root: Path) -> tuple[str, ...]:
    """Scripts referenced by the docs whose argument wiring this module cannot read."""
    commands, _ = documented_commands(root)
    seen: dict[str, bool] = {}
    for command in commands:
        if command.script in seen:
            continue
        _, static = accepts_for(command, root)
        seen[command.script] = static
    return tuple(sorted(name for name, static in seen.items() if not static))


def check_flags(commands: Iterable[DocCommand], root: Path) -> tuple[list[DocFlagProblem], list[DocCommand]]:
    """Return (problems, commands whose script could not be read statically)."""
    problems: list[DocFlagProblem] = []
    unchecked: list[DocCommand] = []
    cache: dict[str, tuple[frozenset[str], bool]] = {}
    shapes: dict[str, ParserShape] = {}
    for command in commands:
        path = root / command.script
        if command.script not in cache:
            cache[command.script] = accepts_for(command, root)
            shapes[command.script] = flags_a_script_accepts(path)
        accepted_flags, static = cache[command.script]
        if not static:
            unchecked.append(command)
            continue
        shape = shapes[command.script]
        tokens = [token for token in command.tokens if not token.startswith("-")]
        # Subcommand scope only means something for a Python script with subparsers; a walk has none.
        subcommand = (
            tokens[0] if command.interpreter.startswith("python") and tokens and tokens[0] in shape.subcommand_flags else ""
        )
        accepted = (
            set(shape.root_flags) | set(shape.subcommand_flags.get(subcommand, ()))
            if command.interpreter.startswith("python")
            else set(accepted_flags)
        )
        for token in command.tokens:
            if not token.startswith("--"):
                continue
            flag = token.split("=")[0]
            if flag in accepted:
                continue
            problems.append(
                DocFlagProblem(
                    command.document,
                    command.line,
                    command.script,
                    flag,
                    tuple(sorted(accepted)),
                    subcommand=subcommand,
                )
            )
    return problems, unchecked


def check_repository(root: Path = _ROOT) -> DocFlagReport:
    """Check every documentation line this module can read, and count what it could not."""
    commands, skipped = documented_commands(root)
    problems, unchecked = check_flags(commands, root)
    report = DocFlagReport(checked=list(commands), skipped=list(skipped), problems=problems)
    report.unresolved = sorted({command.script for command in unchecked})
    widened: dict[str, tuple[str, ...]] = {}
    for script in sorted({command.script for command in commands}):
        shape = flags_a_script_accepts(root / script)
        if shape.static and shape.widened:
            widened[script] = tuple(sorted(shape.widened))
    report.widened = widened
    shapes = {script: flags_a_script_accepts(root / script) for script in {command.script for command in commands}}
    report.hand_rolled = sorted(script for script, shape in shapes.items() if shape.reason == "hand-rolled")
    report.accepts_nothing = sorted(
        script for script, shape in shapes.items() if shape.static and shape.reason == "none"
    )
    report.counter_examples = sorted(
        document.name
        for document in _markdown_files(root)
        if COUNTER_EXAMPLE_MARKER in document.read_text(encoding="utf-8")
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: exit 0 when every readable documented flag exists, 1 on a problem, 0 with a note otherwise."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else _ROOT.parent
    report = check_repository(root)
    for problem in report.problems:
        print(f"doc flag: {problem.describe()}", file=sys.stderr)
    readable = len(report.checked) - len(list(report.unresolved))
    print(
        f"documented commands: {len(report.checked) + len(report.skipped)} lines, "
        f"{len(report.checked)} literal, {len(report.skipped)} with placeholders; "
        f"{readable} checked against a static parser, {len(report.unresolved)} not statically readable, "
        f"{len(report.problems)} problem(s)"
    )
    if report.unresolved:
        print(f"not checked (parser built at runtime): {', '.join(report.unresolved)}", file=sys.stderr)
    if report.counter_examples:
        print(
            f"exempt as counter-examples (not instructions): {', '.join(report.counter_examples)}",
            file=sys.stderr,
        )
    if report.accepts_nothing:
        print(
            "takes no options at all, checked as such: " + ", ".join(report.accepts_nothing), file=sys.stderr
        )
    if report.hand_rolled:
        print(
            "weaker check (flags read from a hand-rolled argv test, not argparse): "
            + ", ".join(report.hand_rolled),
            file=sys.stderr,
        )
    for script, names in report.widened.items():
        print(f"weaker check (subcommand flags unioned across a loop): {script} {', '.join(names)}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
