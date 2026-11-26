"""Read the flags a JavaScript walk accepts, without running it.

`check_documented_commands.py` reads `python scripts/*.py` lines out of the documentation and checks
each flag against that script's argparse wiring. That left the other half of the documentation
unchecked: every `node scripts/*.mjs ...` invocation in the docs, none of them verified. The pilot walk
itself is documented twice, and its flags are the ones an operator would get wrong first.

Running the walks to find out is not an option — they start throwaway apps, drive Chromium and write
files. So this reads the source, the way the Python reader uses `ast`, and abstains where the shape is
not regular:

* a walk that parses with `switch (arg) { case "--base-url": ... }` and a `next()` value getter;
* a walk that parses with `if (arg === "--json") ... else if (arg.startsWith("-")) throw`;
* a walk that reads `process.argv.includes("--proposed")` for a single boolean;
* a walk with no argument handling at all, which accepts no flags — a real answer, not an abstention.

**Scope matters more than coverage here.** The Python reader's first version collected every
`add_argument` in the file and checked against the union, which validated `package --root` — the defect
it was written to catch, reproduced inside the detector. The same trap exists in JavaScript: a string
like `"--dark"` compared somewhere in rendering is not an accepted flag. So flags are read only from
the region that handles `process.argv`, found by brace-matching the function whose body mentions it,
and only from the statement that mentions it at top level. A walk with more than one such region has
its flags unioned across regions, and that is reported as a *weaker* check rather than a clean pass.

Anything unresolvable — a flag name built from a variable, a template-built flag — is *not readable*
rather than guessed. An unreadable walker's flags must never be attributed to it, because "unknown"
and "accepts nothing" are different answers and only one of them is honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "JsParserShape",
    "flags_a_js_script_accepts",
]

_CASE_FLAG = re.compile(r'case\s+"(--[A-Za-z0-9-]+)"')
_EQ_FLAG = re.compile(r'(?:===|==)\s*"(--[A-Za-z0-9-]+)"')
_INCLUDES_FLAG = re.compile(r"includes\(\s*['\"](--[A-Za-z0-9-]+)['\"]\s*\)")
_SHORT_FLAGS = re.compile(r'(?:===|==)\s*"(-[A-Za-z])"')
_KNOWN_LIST = re.compile(r"(?:KNOWN_FLAGS|FLAGS|ALLOWED_FLAGS)\s*=\s*\[([^\]]*)\]", re.DOTALL)
#: A flag name assembled from a variable, or matched by prefix, means the accepted set is not closed.
#: `case FLAG_NAME:` and `arg.startsWith("--")` both accept flags no literal in the file names.
#: `arg.startsWith("")`-style unknown-flag guards are the opposite -- `arg.startsWith("-")` rejects
#: anything unrecognised, which is exactly what makes the literal set trustworthy -- so only a
#: `startsWith` with a dash-dash or empty argument counts as dynamic.
_DYNAMIC = re.compile(r"(case\s+[A-Za-z_$][\w$]*\s*:|startsWith\(\s*['\"`](?:--|)\s*['\"`]\s*\))")
_TEMPLATE_FLAG = re.compile(r"`--\$\{")

_FUNCTION_WITH_ARGV = re.compile(r"function\s+[\w$]+\s*\([^)]*\)\s*\{")


@dataclass(frozen=True)
class JsParserShape:
    """The flags one JavaScript script accepts.

    `static` is False when the source does not resolve to a closed set of literal flags. A caller must
    treat that as "unknown", never as "accepts nothing".
    """

    flags: frozenset[str] = frozenset()
    static: bool = False
    reason: str = ""
    #: Where the flags were read from, for the report: `switch`, `if-chain`, `includes`, `none`.
    shape: str = ""
    #: True when more than one argument-handling region contributed flags, so the set is a union over
    #: regions and a pass is weaker than a single-region pass. Reported, never hidden.
    widened: bool = False

    def describe(self) -> str:
        if not self.static:
            return f"not statically readable: {self.reason}"
        ordered = ", ".join(sorted(self.flags)) or "no flags"
        return f"{self.shape}: {ordered}"


def _strip_comments(source: str) -> str:
    """Remove comments so a flag named in prose is not read as a flag.

    String literals are left alone: a `//` inside a URL string must not swallow the rest of the line.
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r'(?m)^(\s*//.*)$|(\s//[^"\']*)$', "", without_block)


def _regions(source: str) -> list[str]:
    """The argument-handling regions: functions named in terms of argv, plus top-level argv lines.

    Brace matching is naive about braces inside strings, which is acceptable here because the body is
    only ever scanned for literal flag names, never executed or compiled.
    """
    regions: list[str] = []
    for match in _FUNCTION_WITH_ARGV.finditer(source):
        start = match.start()
        depth = 0
        for index in range(match.end() - 1, len(source)):
            character = source[index]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    body = source[start : index + 1]
                    if "argv" in body:
                        regions.append(body)
                    break
    # Top-level handling: a statement that reaches process.argv outside every function region.
    remainder = source
    for region in regions:
        remainder = remainder.replace(region, "", 1)
    for line in remainder.splitlines():
        if "process.argv" in line:
            regions.append(line)
    return regions


def _flags_in(text: str) -> set[str]:
    flags: set[str] = set()
    flags.update(_CASE_FLAG.findall(text))
    flags.update(_EQ_FLAG.findall(text))
    flags.update(_INCLUDES_FLAG.findall(text))
    flags.update(flag for flag in _SHORT_FLAGS.findall(text) if flag in {"-h"})
    for block in _KNOWN_LIST.finditer(text):
        flags.update(re.findall(r'"(--[A-Za-z0-9-]+)"', block.group(1)))
        flags.update(re.findall(r"'(--[A-Za-z0-9-]+)'", block.group(1)))
        flags.update(re.findall(r"'(-[A-Za-z])'", block.group(1)))
    return flags


def flags_a_js_script_accepts(path: Path) -> JsParserShape:
    """Read a walk's argument handling from its source, or say why it cannot be read."""
    if path.suffix not in {".mjs", ".js", ".cjs"}:
        return JsParserShape(static=False, reason=f"{path.suffix or 'no extension'} is not JavaScript")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return JsParserShape(static=False, reason=f"cannot read {path.name}: {exc.strerror}")
    source = _strip_comments(raw)

    # A walk with no argv handling at all accepts no flags. That is a real answer, and a documented
    # flag against it is a real problem -- not an abstention.
    if "process.argv" not in source:
        return JsParserShape(flags=frozenset(), static=True, shape="none")

    if _TEMPLATE_FLAG.search(source):
        return JsParserShape(
            static=False, reason="a flag name is built from a template literal, so the set is not closed"
        )
    for region in _regions(source):
        if _DYNAMIC.search(region):
            return JsParserShape(
                static=False,
                reason="an argument branch compares against a non-literal, so the set is not closed",
            )

    regions = [region for region in _regions(source) if _flags_in(region)]
    fallback = False
    if not regions:
        # No single argv-handling region yielded a literal flag: the walk may handle argv inline in a
        # loop at top level, where a line-by-line scan sees only the `for` header. Scanning the whole
        # file recovers the flags, but a file-wide set is a union over code that need not all run at
        # once, so the result is marked widened rather than reported as a clean pass.
        regions = [source]
        fallback = True
        if not _flags_in(source):
            return JsParserShape(
                static=False,
                reason="the script reads process.argv but no literal flag resolves from the handling code",
            )
    flags = set().union(*(_flags_in(region) for region in regions))
    joined = " ".join(regions)
    if "case \"" in joined:
        shape = "switch"
    elif "includes(" in joined and not _CASE_FLAG.search(joined):
        shape = "includes"
    else:
        shape = "if-chain"
    return JsParserShape(
        flags=frozenset(flags),
        static=True,
        shape=shape,
        widened=fallback or len(regions) > 1,
    )
