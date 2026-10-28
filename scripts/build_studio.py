#!/usr/bin/env python3
"""Deterministically build the portable single-file Studio from app.py + static/index.html."""
from __future__ import annotations

import pathlib
import py_compile

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
HTML = ROOT / "static" / "index.html"
ORCHESTRATOR = ROOT / "workflow_orchestration.py"
OUT = ROOT / "studio.py"

AUTH_HELPER = '''<script>(function(){const nativeFetch=window.fetch.bind(window);let promptLock=null;window.fetch=async function(input,init={}){const url=typeof input==='string'?input:input.url;const same=new URL(url,location.href).origin===location.origin;const send=async(token)=>{const headers=new Headers(input instanceof Request?input.headers:undefined);new Headers(init.headers||{}).forEach((v,k)=>headers.set(k,v));if(token&&same)headers.set('Authorization','Bearer '+token);return nativeFetch(input,{...init,headers});};let token=sessionStorage.getItem('m5_api_token')||'';let r=await send(token);if(r.status!==401||!same||!url.includes('/api/')&&!url.includes('/v1/'))return r;if(!promptLock)promptLock=new Promise(resolve=>{const v=window.prompt('Studio API token (saved for this browser session):');if(v)sessionStorage.setItem('m5_api_token',v);resolve(v||'');});token=await promptLock;promptLock=null;if(!token)return r;r=await send(token);return r;};})();</script>'''


def build() -> None:
    app_text = APP.read_text(encoding="utf-8")
    html_text = HTML.read_text(encoding="utf-8")
    orchestrator_text = ORCHESTRATOR.read_text(encoding="utf-8").replace("from __future__ import annotations\n\n", "", 1)
    if '\"\"\"' in html_text:
        raise SystemExit('static/index.html contains triple double quotes; cannot safely embed as a raw literal')
    if html_text.endswith("\\"):
        raise SystemExit("static/index.html ends with a backslash; cannot safely embed as a raw literal")

    start_marker = '@app.get("/", response_class=HTMLResponse)\nasync def index() -> HTMLResponse:'
    start = app_text.find(start_marker)
    if start < 0:
        raise SystemExit("Could not find canonical index route in app.py")
    end = app_text.find("\n\n# =============================================================================", start)
    if end < 0:
        raise SystemExit("Could not find end of canonical index route in app.py")

    replacement = (
        'EMBEDDED_HTML = r"""' + html_text + '"""\n\n\n'
        '@app.get("/", response_class=HTMLResponse)\n'
        'async def index() -> HTMLResponse:\n'
        '    ext = BASE_DIR / "static" / "index.html"\n'
        '    html = EMBEDDED_HTML\n'
        '    if SETTINGS.external_static and ext.is_file():\n'
        '        html = ext.read_text(encoding="utf-8")\n'
        '    if STUDIO_API_TOKEN and not STUDIO_AUTH_DISABLED:\n'
        f'        html = html.replace("</head>", {AUTH_HELPER!r} + "</head>", 1)\n'
        '    return HTMLResponse(html, headers={"Cache-Control": "no-store"})'
    )
    out_text = app_text[:start] + replacement + app_text[end:]
    import_line = "from workflow_orchestration import WorkflowOrchestrator, WorkflowStage\n"
    if import_line not in out_text:
        raise SystemExit("Could not find workflow orchestration import in app.py")
    out_text = out_text.replace(import_line, "", 1)
    privacy_import = "from history_privacy import HistoryPrivacyError, apply_retention, export_history\n"
    if privacy_import not in out_text:
        raise SystemExit("Could not find history privacy import in app.py")
    out_text = out_text.replace(privacy_import, "", 1)
    privacy_text = (ROOT / "history_privacy.py").read_text(encoding="utf-8")
    # A mid-file "from __future__ import annotations" is a syntax error; the
    # standalone already declares it at the top of the file.
    future_line = "from __future__ import annotations\n"
    privacy_text = privacy_text.replace(future_line, "", 1)
    orchestrator_text = orchestrator_text.replace(future_line, "", 1) if orchestrator_text.startswith(future_line) else orchestrator_text
    main_marker = "\nif __name__ == \"__main__\":"
    main_start = out_text.find(main_marker)
    if main_start < 0:
        raise SystemExit("Could not find app.py main entrypoint")
    # Keep studio.py executable as a single-file artifact while the canonical
    # source remains split into typed modules. Extracted modules are inlined
    # (history_privacy first: the orchestrator and app may not need it, but
    # init_db and the history routes do).
    out_text = out_text[:main_start] + "\n\n" + privacy_text.rstrip() + "\n\n" + orchestrator_text.rstrip() + out_text[main_start:]
    OUT.write_text(out_text, encoding="utf-8")
    py_compile.compile(str(APP), doraise=True)
    py_compile.compile(str(OUT), doraise=True)
    print(f"Built {OUT.name}: {OUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    build()
