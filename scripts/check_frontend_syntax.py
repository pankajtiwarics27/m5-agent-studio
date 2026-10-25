#!/usr/bin/env python3
"""Syntax-check inline JavaScript with Node.js without changing the generated artifacts."""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = ROOT / "static" / "index.html"


def main() -> None:
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required for the frontend syntax check.")
    html = HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", html, flags=re.IGNORECASE)
    if not scripts:
        raise SystemExit("No inline JavaScript blocks found to validate.")
    with tempfile.TemporaryDirectory(prefix="m5-studio-js-") as td:
        for idx, script in enumerate(scripts, start=1):
            target = pathlib.Path(td) / f"inline-{idx}.js"
            target.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, check=False)
            if result.returncode:
                raise SystemExit(f"Inline script {idx} failed Node syntax check:\n{result.stderr or result.stdout}")
    print(f"JavaScript syntax OK ({len(scripts)} inline block(s))")


if __name__ == "__main__":
    main()
