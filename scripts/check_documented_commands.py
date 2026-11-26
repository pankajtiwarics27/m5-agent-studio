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
    #: Literal invocations whose parser was unreadable; unlike unresolved, this counts each line.
    unchecked: list[DocCommand] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    #: JavaScript scripts whose flags are unioned across regions or a file-wide fallback.
    js_widened: list[str] = field(default_factory=list)
    #: script -> subcommands whose flags were widened across loop iterations, so a pass there is
    #: weaker than a pass elsewhere. Reported, never hidden.
    widened: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: documents exempted because they quote a wrong command as a counter-example.
    counter_examples: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _markdown_files(root: Path) -> Iterator[Path]:
    yield from sorted(root.glob("docs/*.md"))
    readme = root / "README.md"
    if readme.is_file():
        yield readme


def _command_lines(text: str) -> Iterator[tuple[int, str, bool]]:
    """Join simple backslash continuations; never consume another command or a Markdown boundary."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        number = index + 1
        line = lines[index]
        index += 1
        if _COMMAND.match(line) is None:
            continue
        complete = True
        while line.endswith("\\") and (len(line) - len(line.rstrip("\\"))) % 2:
            if index >= len(lines):
                complete = False
                break
            following = lines[index]
            stripped = following.strip()
            if not stripped or stripped.startswith(("```", "~~~")) or _COMMAND.match(following):
                complete = False
                break
            # Shell removes the backslash and newline, but preserves indentation on the next line.
            line = line[:-1] + following
            index += 1
        yield number, line, complete


def documented_commands(root: Path) -> tuple[list[DocCommand], list[DocCommand]]:
    """Return (checkable, skipped) documented invocations.

    A line is skipped when it carries a shell metacharacter: the documentation uses `<root>` and
    `$STUDIO_DB_PATH` to stand for values, and a flag next to a placeholder cannot be checked without
    guessing what the placeholder becomes.
    """
    checkable: list[DocCommand] = []
    skipped: list[DocCommand] = []
    for document in _markdown_files(root):
        source = document.read_text(encoding="utf-8")
        for number, text, complete in _command_lines(source):
            match = _COMMAND.match(text)
            assert match is not None
            interpreter, script, rest = match.group("interpreter"), match.group("script"), match.group(3).strip()
            if COUNTER_EXAMPLE_MARKER in source or not complete or _NOT_A_LITERAL.search(rest) or not rest:
                skipped.append(DocCommand(document.name, number, script, (), interpreter=interpreter))
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

    ``static`` is False when the shape cannot be resolved: no `argparse`, a subcommand whose name is
    not resolvable from literals, or an `add_argument` whose flags are not literals. A caller must
    treat that as "unknown", never as "accepts nothing" -- and, just as important, an unresolvable
    parser's flags must never be attributed to the root, or they would validate a flag the subcommand
    does not take.
    """
    if path.suffix != ".py":
        return ParserShape(frozenset(), {}, False, "not a Python script")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ParserShape(frozenset(), {}, False, "unreadable")
    if "argparse" not in source:
        return ParserShape(frozenset(), {}, False, "no argparse")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ParserShape(frozenset(), {}, False, "not parseable as Python")
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
    static = bool(root_flags or per_subcommand)
    return ParserShape(
        root_flags=frozenset(root_flags),
        subcommand_flags={name: frozenset(flags) for name, flags in per_subcommand.items()},
        static=static,
        reason="" if static else "no flags found",
        widened=widened,
    )


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
    unresolved: set[str] = set()
    for command in commands:
        _, static = accepts_for(command, root)
        if not static:
            unresolved.add(command.script)
    return tuple(sorted(unresolved))


def check_flags(commands: Iterable[DocCommand], root: Path) -> tuple[list[DocFlagProblem], list[DocCommand]]:
    """Return (problems, commands whose script could not be read statically)."""
    problems: list[DocFlagProblem] = []
    unchecked: list[DocCommand] = []
    cache: dict[tuple[str, str], tuple[frozenset[str], bool]] = {}
    shapes: dict[str, ParserShape] = {}
    for command in commands:
        path = root / command.script
        interpreter = "python" if command.interpreter.startswith("python") else command.interpreter
        key = (command.script, interpreter)
        if key not in cache:
            cache[key] = accepts_for(command, root)
        if command.script not in shapes:
            shapes[command.script] = flags_a_script_accepts(path)
        accepted_flags, static = cache[key]
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
    report = DocFlagReport(checked=list(commands), skipped=list(skipped), problems=problems, unchecked=unchecked)
    report.unresolved = sorted({command.script for command in unchecked})
    report.js_widened = sorted(
        script
        for script in {command.script for command in commands if command.interpreter == "node"}
        if (shape := flags_a_js_script_accepts(root / script)).static and shape.widened
    )
    widened: dict[str, tuple[str, ...]] = {}
    for script in sorted({command.script for command in commands}):
        shape = flags_a_script_accepts(root / script)
        if shape.static and shape.widened:
            widened[script] = tuple(sorted(shape.widened))
    report.widened = widened
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
    readable = len(report.checked) - len(report.unchecked)
    print(
        f"documented commands: {len(report.checked) + len(report.skipped)} lines, "
        f"{len(report.checked)} literal candidates, {len(report.skipped)} skipped lines; "
        f"{readable} checked against a static parser, {len(report.unchecked)} unchecked "
        f"across {len(report.unresolved)} unreadable scripts, {len(report.problems)} problem(s)"
    )
    if report.unresolved:
        print(f"not checked (parser not statically readable): {', '.join(report.unresolved)}", file=sys.stderr)
    if report.js_widened:
        print(
            "weaker check (JavaScript flags unioned across regions or file): " + ", ".join(report.js_widened),
            file=sys.stderr,
        )
    if report.counter_examples:
        print(
            f"exempt as counter-examples (not instructions): {', '.join(report.counter_examples)}",
            file=sys.stderr,
        )
    for script, names in report.widened.items():
        print(f"weaker check (subcommand flags unioned across a loop): {script} {', '.join(names)}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
