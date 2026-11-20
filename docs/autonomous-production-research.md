# AP-009 — Source-backed safe search, reading and repository inspection

**Status:** implemented as an offline contract module; **not activated**, **not wired
into the running Studio**, and **not a production guarantee**. This page records
what the module decides and what it does not decide.

## Story and owner scope

AP-009 (Phase P2 of the [Autonomous Production](autonomous-production-roadmap.md)
program) delivers the source-backed research tier: public HTTPS read of a fixed
allowlist, explicit provenance for every cited byte, contradiction-preserving
claim records, and read-only repository inspection. Owner authorization for
AP-009 is recorded verbatim in
[issue #332](https://github.com/pankajtiwarics27/m5-agent-studio/issues/332);
the governing grants are the U03/U04 owner decisions recorded in
[the operating envelope](autonomous-production-operating-envelope.md) and issue
[#330](https://github.com/pankajtiwarics27/m5-agent-studio/issues/330).

This story's delivery does **not** activate anything: no live network call is
made by importing the module, no Studio or Harness code-path imports it, and no
account, credential, browser, directory outside `~/M5_AP_Workspace` or spending
capability is used. AP-010 and later remain unauthorized.

## What the module decides

`autonomous_production_research.py` owns a fail-closed contract boundary:

- **Egress admit-by-default.** Public `HTTPS GET` only, port 443, to exactly
  `github.com`, `codeload.github.com`, `api.github.com`, `en.wikipedia.org`,
  `pypi.org`, `registry.npmjs.org` (the U03 standard public set). URLs with
  userinfo, non-https schemes, non-443 ports or private/loopback/link-local
  (including cloud metadata `169.254.169.254`), CGNAT, documentation,
  benchmark, multicast, reserved and IPv4-mapped-IPv6 literals are refused.
  Hosts on the allowlist are additionally DNS-checked when a resolver is
  injected: any resolved address in a blocked range refuses the fetch.
- **Redirects, hop-by-hop.** Every hop is validated against the same rules;
  chains longer than the budget (default 3) or leaving the allowlist are
  refused, and a fetcher-reported `final_url` must equal the validated chain;
  disagreement is recorded as `refused`.
- **Bounded bytes.** Per-source caps (default 512 KiB), total bundle caps
  (default 4 MiB), excerpt caps (default 4000 chars), 12 sources per bundle
  and 30-day retention for captured snapshots — the caps are recorded on the
  result, truncation is explicit, never silent.
- **Explicit provenance.** Every `SourceRecord` carries the URL, final URL
  (after the validated redirect chain), retrieval time, `http_status`,
  `content_sha256`, a bounded excerpt, a **publication date only when the
  source carries one** (e.g. `Last-Modified`), byte counts and a
  `retrieval_status` (`ok`/`partial`/`failed`/`refused`), plus the
  pre-cap `upstream_byte_count` so a truncated response is never mistaken for
  a complete one. Failed sources are kept on the bundle and listed in
  `incomplete_reasons()`.
- **Adapters parse the retained body, not the excerpt.** Provider JSON is
  parsed from the byte-capped body that was hashed into the source record
  (`guarded_fetch_with_body`), so a large-but-valid search response is never
  silently reduced to "no results"; an unparseable or truncated payload leaves
  the record `partial` with **no** results rather than fabricated ones. Both
  adapters honour the caller's `SourceBudget` instead of the module default.
- **Claims, contradictions, gaps are data, not prose.** `add_claim` with no
  supporting source marks the claim `unsupported`;
  `record_contradiction` needs ≥2 sources and marks the claim `contradicted`;
  nothing is silently reconciled. Unsupported and contradicted claims and
  failed sources keep the bundle `is_incomplete` — that is the honest exit
  when a provider fails, never a fabricated citation.
- **Repository inspection is read-only.** It walks a caller-owned root,
  refuses symlinks and escapes-from-root, never writes, enforces
  max-files / per-file / total-byte caps, records `sha256` for regular files
  and marks the walk `truncated` when a cap stops it.

## What the module deliberately does not decide

- **It opens no sockets of its own.** A caller injects a fetcher callable and
  a DNS resolver. The offline tests in
  `tests/test_autonomous_production_research.py`
  use fakes only; the module never reaches the real network in tests.
- **It grants nothing.** The U03 allowlist is a default for this module's
  call sites that already hold owner authority; it is not a live grant.
  Adapters that would create accounts, post, upload, spend, or activate a
  browser/media/messaging surface remain out of scope here.
- **It does not prove safety of fetched content.** Instructions found in
  pages are untrusted data; the module records excerpts and provenance and
  never interprets or executes them.
- **It does not fact-check.** Contradictions are surfaced, not resolved.
  Unsupported claims stay unsupported. "Honest incomplete" is a result class,
  not an error to hide.
- **It does not publish, deliver, schedule or spend.** Those require their
  own stories, criteria and grants.

## Evidence and limitations

The recording of this delivery (exact head, test runs, ledger state) lives in
the story's claim/delivery commit and the completion-record PR that follows
this story's merge. No qualification of any installed runtime has been run;
no claim is made about the M5 host; `runtime_activation_authorized` stays
`false` everywhere; ADR-0040–0045 remain `not-reviewed`.

## Verification

```sh
python -m pytest -q tests/test_autonomous_production_research.py
python autonomous_production_decisions.py
```
