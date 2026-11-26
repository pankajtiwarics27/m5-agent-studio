"""The JavaScript reader must abstain rather than guess, and must not read flags from the wrong code.

`scripts/js_documented_flags.py` lets the documented-command checker cover `node scripts/*.mjs` lines.
Its Python counterpart had a first version that unioned every `add_argument` in the file, which
validated the very flag the checker existed to reject. The same mistake is available here — a
`"--dark"` compared somewhere in rendering is not an accepted flag — so these tests pin the scope, the
abstentions, and the one case that must *not* be an abstention: a walk that reads no arguments at all
accepts no flags.
"""

from __future__ import annotations

from pathlib import Path

from scripts.js_documented_flags import flags_a_js_script_accepts


def _walk(tmp_path: Path, body: str, name: str = "walk.mjs") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ── scope: only the argument handling may contribute flags ───────────────────────────────────────


def test_a_flag_string_outside_the_argument_handling_is_not_accepted(tmp_path: Path) -> None:
    """A `"--dark"` compared while rendering must not validate `--dark` on the command line."""
    path = _walk(
        tmp_path,
        """
const THEME = process.env.THEME || "light";
if (THEME === "--dark") {
  document.body.dataset.theme = "dark";
}
function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--json") args.json = true;
    else throw new Error("unknown flag: " + argv[i]);
  }
  return args;
}
run(parseArgs(process.argv.slice(2)));
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert shape.static, shape.reason
    assert shape.flags == frozenset({"--json"}), (
        "a flag compared outside the argument handling was read as accepted: " + repr(sorted(shape.flags))
    )


def test_a_commented_out_flag_is_not_accepted(tmp_path: Path) -> None:
    """Commented-out handling is the realistic case: a removed flag left in comments.

    A flag merely *mentioned* in prose cannot reach the flag patterns — they need `case "--x"` or
    `=== "--x"`. Commented-out code can, which is why the reader strips comments before reading.
    """
    path = _walk(
        tmp_path,
        """
function parseArgs(argv) {
  // if (arg === "--screenshots") { args.screenshots = true; }
  /* case "--theme": args.theme = next(); break; */
  const args = {};
  for (const arg of argv) {
    if (arg === "--json") args.json = true;
    else throw new Error("unknown flag: " + arg);
  }
  return args;
}
run(parseArgs(process.argv.slice(2)));
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert shape.static, shape.reason
    assert shape.flags == frozenset({"--json"}), (
        "a flag handled only in commented-out code was read as accepted: " + repr(sorted(shape.flags))
    )


def test_top_level_argv_handling_is_read_but_marked_widened(tmp_path: Path) -> None:
    """Inline handling is readable; a file-wide set is still a weaker result and must say so."""
    path = _walk(
        tmp_path,
        """
for (const arg of process.argv.slice(2)) {
  if (arg === "--json") { json = true; }
  else throw new Error("unknown flag: " + arg);
}
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert shape.static and shape.flags == frozenset({"--json"})
    assert shape.widened, "a file-wide flag set was reported as a clean, scoped pass"


# ── abstention: unknown is not the same answer as "accepts nothing" ──────────────────────────────


def test_a_flag_name_built_from_a_variable_is_not_readable(tmp_path: Path) -> None:
    path = _walk(
        tmp_path,
        """
function parseArgs(argv) {
  const flag = "--" + name;
  if (argv[0] === flag) { return {}; }
  return {};
}
run(parseArgs(process.argv.slice(2)));
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert not shape.static, "a flag assembled at runtime was reported as a closed set"
    assert shape.reason


def test_a_dynamic_switch_case_is_not_readable(tmp_path: Path) -> None:
    """`case FLAG:` accepts a flag no literal names, so the set is open."""
    path = _walk(
        tmp_path,
        """
function parseArgs(argv) {
  switch (argv[0]) {
    case FLAG_NAME: return {};
    case "--json": return { json: true };
    default: throw new Error("unknown");
  }
}
run(parseArgs(process.argv.slice(2)));
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert not shape.static, "a `case <variable>` was treated as a closed set of literal flags"


def test_a_prefix_match_is_not_readable(tmp_path: Path) -> None:
    """`arg.startsWith("--")` accepts every dash flag; the literal set is not the accepted set."""
    path = _walk(
        tmp_path,
        """
for (const arg of process.argv.slice(2)) {
  if (arg.startsWith("--")) { /* anything */ }
}
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert not shape.static, "a prefix match was treated as a closed set"


def test_an_unknown_flag_guard_is_not_a_prefix_match(tmp_path: Path) -> None:
    """`startsWith("-")` *rejects* the unknown, which is what makes the literal set trustworthy."""
    path = _walk(
        tmp_path,
        """
function parseArgs(argv) {
  const args = {};
  for (const arg of argv) {
    if (arg === "--json") args.json = true;
    else if (arg.startsWith("-")) throw new Error("unknown flag: " + arg);
  }
  return args;
}
run(parseArgs(process.argv.slice(2)));
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert shape.static, "an unknown-flag guard was mistaken for a dynamic flag name"
    assert shape.flags == frozenset({"--json"})


def test_a_script_with_no_argument_handling_accepts_no_flags(tmp_path: Path) -> None:
    """This is a closed set of zero, not an abstention — a documented flag against it is a problem."""
    path = _walk(tmp_path, "console.log('no arguments here');\n", name="helper.js")
    shape = flags_a_js_script_accepts(path)
    assert shape.static and shape.flags == frozenset() and shape.shape == "none"


def test_argv_read_but_no_literal_flag_is_not_readable(tmp_path: Path) -> None:
    """Reading argv without naming a flag may mean the flags live somewhere this reader cannot see."""
    path = _walk(tmp_path, "const argv = process.argv.slice(2);\nrun(argv);\n")
    shape = flags_a_js_script_accepts(path)
    assert not shape.static and shape.reason


# ── the shapes the repository actually uses ──────────────────────────────────────────────────────


def test_a_switch_with_a_value_getter_is_read(tmp_path: Path) -> None:
    path = _walk(
        tmp_path,
        """
function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => { i += 1; return argv[i]; };
    switch (arg) {
      case "--base-url": args.baseUrl = next(); break;
      case "--token": args.token = next(); break;
      case "--json": args.json = true; break;
      case "--help": console.log("usage"); process.exit(0);
      default: throw new Error("unknown flag: " + arg);
    }
  }
  return args;
}
run(parseArgs(process.argv.slice(2)));
""",
    )
    shape = flags_a_js_script_accepts(path)
    assert shape.static and shape.shape == "switch"
    assert shape.flags == frozenset({"--base-url", "--token", "--json", "--help"})


def test_an_includes_flag_is_read(tmp_path: Path) -> None:
    path = _walk(tmp_path, "const PROPOSED = process.argv.includes('--proposed');\n")
    shape = flags_a_js_script_accepts(path)
    assert shape.static and shape.flags == frozenset({"--proposed"}) and shape.shape == "includes"


def test_a_non_javascript_file_is_not_readable(tmp_path: Path) -> None:
    path = _walk(tmp_path, "process.argv.push('--x');\n", name="walk.txt")
    shape = flags_a_js_script_accepts(path)
    assert not shape.static, "a non-JavaScript file was read as if it were a walk"
