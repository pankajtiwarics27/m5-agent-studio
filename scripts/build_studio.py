#!/usr/bin/env python3
"""Deterministically build the portable single-file Studio from app.py + static/index.html."""

from __future__ import annotations

import pathlib
import re
import py_compile

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
HTML = ROOT / "static" / "index.html"
ORCHESTRATOR = ROOT / "workflow_orchestration.py"
OUT = ROOT / "studio.py"

# Every sibling module app.py imports by name must be inlined here, or the
# standalone artifact only works from a checkout where the sibling is importable.
# A smoke check that runs from the repo root cannot catch that.
INLINED_MODULES = (
    "workspace_paths",
    "runtime_persistence",
    "side_effect_ledger",
    "execution_budget",
    "workflow_checkpoints",
    "recovery_guard",
    "verification_evidence",
    "process_supervision",
    "scoped_grants",
    "child_delegation",
    "workflow_state",
    "mcp_client",
    "context_store",
    "onboarding_preflight",
    "start_command_center",
    "failure_injection",
    "execution_spine",
    "task_events",
    "sandbox_boundary",
    "edit_transactions",
    "workspace_snapshots",
    "artifact_lineage",
    "workspace_quarantine",
    "edit_rollback",
    "resource_admission",
    "execution_profiles",
    "model_admission",
    "runtime_dispatch_boundary",
    "routing_policy",
    "security_release_gate",
    "evidence_lessons",
    "trusted_transformations",
    "routing_experiments",
)

AUTH_HELPER = """<script>(function(){const nativeFetch=window.fetch.bind(window);let promptLock=null;window.fetch=async function(input,init={}){const url=typeof input==='string'?input:input.url;const same=new URL(url,location.href).origin===location.origin;const send=async(token)=>{const headers=new Headers(input instanceof Request?input.headers:undefined);new Headers(init.headers||{}).forEach((v,k)=>headers.set(k,v));if(token&&same)headers.set('Authorization','Bearer '+token);return nativeFetch(input,{...init,headers});};let token=sessionStorage.getItem('m5_api_token')||'';let r=await send(token);if(r.status!==401||!same||!url.includes('/api/')&&!url.includes('/v1/'))return r;if(!promptLock)promptLock=new Promise(resolve=>{const v=window.prompt('Studio API token (saved for this browser session):');if(v)sessionStorage.setItem('m5_api_token',v);resolve(v||'');});token=await promptLock;promptLock=null;if(!token)return r;r=await send(token);return r;};})();</script>"""


# Namespace lifecycle modules rather than flattening their duplicate _gate,
# _integer, profile/dataclass and exception names into the application globals.
NATIVE_MODULES = (
    "local_runtime_lifecycle",
    "model_lifecycle_coordinator",
    "shared_memory_admission",
    "omlx_lifecycle",
    "ollama_protocol",
    "ollama_lifecycle",
    "lmstudio_protocol",
    "lmstudio_lifecycle",
    "request_fit",
    "request_fit_admission",
    "native_dispatch_executor",
    "native_transition_driver",
)


def native_bundle_block() -> str:
    sources = {
        name: (ROOT / (name + ".py")).read_text(encoding="utf-8")
        for name in NATIVE_MODULES
    }
    loader = """
def _load_standalone_native_modules(sources):
    import ast as _bundle_ast
    import types as _bundle_types
    import sys as _bundle_sys
    package_name = '_m5_native_' + __name__.replace('.', '_')
    if package_name in _bundle_sys.modules:
        raise RuntimeError('native_bundle_already_loaded')
    package = _bundle_types.ModuleType(package_name)
    package.__path__ = []
    _bundle_sys.modules[package_name] = package
    # Reuse the actual app transaction ContextVar and dispatch exception identity.
    # No shadow transaction tracker and no replacement of public module names.
    shims = {'runtime_persistence': runtime_persistence, 'runtime_dispatch_boundary': runtime_dispatch_boundary}
    siblings = set(sources) | set(shims)
    class RewriteImports(_bundle_ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.level == 0 and node.module in siblings:
                node.module = package_name + '.' + node.module
            return node
        def visit_Import(self, node):
            result = []
            for alias in node.names:
                if alias.name in siblings:
                    replacement = _bundle_ast.ImportFrom(module=package_name, names=[alias], level=0)
                else:
                    replacement = _bundle_ast.Import(names=[alias])
                result.append(_bundle_ast.copy_location(replacement, node))
            return result
    try:
        for name, module in shims.items():
            _bundle_sys.modules[package_name + '.' + name] = module
            setattr(package, name, module)
        for name, source in sources.items():
            filename = '<embedded-native/' + name + '.py>'
            module = _bundle_types.ModuleType(package_name + '.' + name)
            module.__file__ = filename
            module.__package__ = package_name
            _bundle_sys.modules[module.__name__] = module
            setattr(package, name, module)
            tree = RewriteImports().visit(_bundle_ast.parse(source, filename))
            _bundle_ast.fix_missing_locations(tree)
            exec(compile(tree, filename, 'exec'), module.__dict__)
    except BaseException:
        for name in list(_bundle_sys.modules):
            if name == package_name or name.startswith(package_name + '.'):
                del _bundle_sys.modules[name]
        raise
    return package.native_dispatch_executor

native_dispatch_executor = _load_standalone_native_modules(_STANDALONE_NATIVE_SOURCES)
del _STANDALONE_NATIVE_SOURCES
"""
    return "_STANDALONE_NATIVE_SOURCES = " + repr(sources) + "\n" + loader


def build() -> None:
    app_text = APP.read_text(encoding="utf-8")
    html_text = HTML.read_text(encoding="utf-8")
    orchestrator_text = ORCHESTRATOR.read_text(encoding="utf-8").replace(
        "from __future__ import annotations\n\n", "", 1
    )
    if '"""' in html_text:
        raise SystemExit(
            "static/index.html contains triple double quotes; cannot safely embed as a raw literal"
        )
    if html_text.endswith("\\"):
        raise SystemExit(
            "static/index.html ends with a backslash; cannot safely embed as a raw literal"
        )

    start_marker = (
        '@app.get("/", response_class=HTMLResponse)\nasync def index() -> HTMLResponse:'
    )
    start = app_text.find(start_marker)
    if start < 0:
        raise SystemExit("Could not find canonical index route in app.py")
    end = app_text.find(
        "\n\n# =============================================================================",
        start,
    )
    if end < 0:
        raise SystemExit("Could not find end of canonical index route in app.py")

    replacement = (
        'EMBEDDED_HTML = r"""' + html_text + '"""\n\n\n'
        '@app.get("/", response_class=HTMLResponse)\n'
        "async def index() -> HTMLResponse:\n"
        '    ext = BASE_DIR / "static" / "index.html"\n'
        "    html = EMBEDDED_HTML\n"
        "    if SETTINGS.external_static and ext.is_file():\n"
        '        html = ext.read_text(encoding="utf-8")\n'
        "    if STUDIO_API_TOKEN and not STUDIO_AUTH_DISABLED:\n"
        f'        html = html.replace("</head>", {AUTH_HELPER!r} + "</head>", 1)\n'
        '    return HTMLResponse(html, headers={"Cache-Control": "no-store"})'
    )
    out_text = app_text[:start] + replacement + app_text[end:]
    import_line = (
        "from workflow_orchestration import WorkflowOrchestrator, WorkflowStage\n"
    )
    if import_line not in out_text:
        raise SystemExit("Could not find workflow orchestration import in app.py")
    out_text = out_text.replace(import_line, "", 1)
    privacy_import = "from history_privacy import HistoryPrivacyError, apply_retention, export_history, redact_text\n"
    if privacy_import not in out_text:
        raise SystemExit("Could not find history privacy import in app.py")
    out_text = out_text.replace(privacy_import, "", 1)
    for module_name in INLINED_MODULES:
        # Tolerate a trailing comment (for example a lint suppression on an
        # import that exists only so the builder can inline the module).
        import_pattern = re.compile(
            rf"^import {re.escape(module_name)}(?:\s+#.*)?\n", re.MULTILINE
        )
        if not import_pattern.search(out_text):
            raise SystemExit(f"Could not find {module_name} import in app.py")
        out_text = import_pattern.sub("", out_text, count=1)
    privacy_text = (ROOT / "history_privacy.py").read_text(encoding="utf-8")
    # A mid-file "from __future__ import annotations" is a syntax error; the
    # standalone already declares it at the top of the file.
    future_line = "from __future__ import annotations\n"
    privacy_text = privacy_text.replace(future_line, "", 1)
    orchestrator_text = (
        orchestrator_text.replace(future_line, "", 1)
        if orchestrator_text.startswith(future_line)
        else orchestrator_text
    )
    main_marker = '\nif __name__ == "__main__":'
    main_start = out_text.find(main_marker)
    if main_start < 0:
        raise SystemExit("Could not find app.py main entrypoint")
    # Keep studio.py executable as a single-file artifact while the canonical
    # source remains split into typed modules. Extracted modules are inlined
    # (history_privacy first: the orchestrator and app may not need it, but
    # init_db and the history routes do). The mcp_client/context_store wire and
    # memory boundaries are inlined before the orchestrator and re-aliased so
    # qualified references from the application keep resolving.
    boundary_texts = []
    for module_name in INLINED_MODULES:
        module_text = (
            (ROOT / f"{module_name}.py")
            .read_text(encoding="utf-8")
            .replace(future_line, "", 1)
        )
        for sibling in INLINED_MODULES:
            module_text = module_text.replace(f"import {sibling}\n", "")
        boundary_texts.append(
            f"{module_text.rstrip()}\n\n{module_name} = sys.modules[__name__]"
        )
    # The native executor import is composed only after existing inlined modules
    # have established their shared transaction/dispatch identities.
    native_import = "import native_dispatch_executor  # noqa: F401 -- internal composition, no activation\n"
    if native_import not in out_text:
        raise SystemExit("Could not find native executor import in app.py")
    out_text = out_text.replace(native_import, "", 1)
    # Removal changes main_start; recompute before inserting the module block.
    main_start = out_text.find(main_marker)
    boundary_block = "\n\n".join(boundary_texts) + "\n\n" + native_bundle_block()
    out_text = (
        out_text[:main_start]
        + "\n\n"
        + privacy_text.rstrip()
        + "\n\n"
        + boundary_block
        + "\n\n"
        + orchestrator_text.rstrip()
        + out_text[main_start:]
    )
    OUT.write_text(out_text, encoding="utf-8")
    py_compile.compile(str(APP), doraise=True)
    py_compile.compile(str(OUT), doraise=True)
    print(f"Built {OUT.name}: {OUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    build()
