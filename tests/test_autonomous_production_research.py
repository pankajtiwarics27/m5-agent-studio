"""AP-009 offline contract tests: safe search, reading and repo inspection.

Every network call in this file is injected via a fake fetcher; DNS answers
come from callables. No test opens a socket, and none touches the real
public allowlist at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autonomous_production_research import (
    PUBLIC_READ_ALLOWLIST,
    ContradictionRecord,
    FetchRequest,
    FetchResponse,
    ResearchBundle,
    ResearchContractError,
    SourceBudget,
    SourceRecord,
    github_search_repositories,
    guarded_fetch,
    inspect_repository,
    is_blocked_ip,
    validate_public_url,
    validate_redirect_chain,
    wikipedia_raw_article,
)


FIXED_CLOCK = "2026-11-18T04:00:00Z"


def _repo_body() -> bytes:
    return json.dumps(
        {
            "items": [
                {
                    "full_name": "acme/demo",
                    "html_url": "https://github.com/acme/demo",
                    "updated_at": "2026-11-01T00:00:00Z",
                    "stargazers_count": 42,
                }
            ],
            "total_count": 1,
        }
    ).encode()


def _ok_response(url: str, body: bytes = b"ok", *, headers: dict | None = None, redirects: tuple = ()) -> FetchResponse:
    return FetchResponse(
        status=200,
        final_url=url,
        redirects=tuple(redirects),
        body=body,
        headers=headers or {},
        retrieved_at=FIXED_CLOCK,
    )


def _fetcher_from(mapping: dict[str, FetchResponse | list[FetchResponse]]):
    calls: list[FetchRequest] = []

    def fake(request: FetchRequest) -> FetchResponse:
        calls.append(request)
        response = mapping[request.url]
        if isinstance(response, list):
            response = response.pop(0)
        return response

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


# --------------------------------------------------------------- policy helpers

def test_allowlist_contains_exactly_the_u03_standard_public_set():
    assert PUBLIC_READ_ALLOWLIST == frozenset(
        {
            "github.com",
            "codeload.github.com",
            "api.github.com",
            "en.wikipedia.org",
            "pypi.org",
            "registry.npmjs.org",
        }
    )


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.9",
        "172.16.0.9",
        "100.64.10.2",
        "198.51.100.7",
        "203.0.113.9",
        "224.0.0.5",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
def test_blocked_ranges_are_blocked(ip):
    assert is_blocked_ip(ip) is True


def test_public_addresses_are_not_blocked():
    assert is_blocked_ip("93.184.216.34") is False
    assert is_blocked_ip("2606:4700:4700::1111") is False


def test_plain_public_url_accepted():
    host, path, port = validate_public_url("https://github.com/owner/repo")
    assert (host, path, port) == ("github.com", "/owner/repo", 443)


def test_non_allowlist_host_is_refused():
    with pytest.raises(ResearchContractError):
        validate_public_url("https://example.com/")


def test_http_scheme_is_refused():
    with pytest.raises(ResearchContractError):
        validate_public_url("http://github.com/")


def test_literal_loopback_host_is_refused():
    with pytest.raises(ResearchContractError):
        validate_public_url("https://127.0.0.1:443/")


def test_literal_metadata_host_is_refused():
    with pytest.raises(ResearchContractError):
        validate_public_url("https://169.254.169.254/latest/meta-data")


def test_url_with_userinfo_is_refused():
    with pytest.raises(ResearchContractError):
        validate_public_url("https://user:pass@github.com/")


def test_non_443_port_is_refused():
    with pytest.raises(ResearchContractError):
        validate_public_url("https://github.com:8080/")


def test_allowlisted_host_resolving_to_private_dns_is_refused():
    with pytest.raises(ResearchContractError, match="blocked address"):
        validate_public_url("https://github.com/", dns_resolver=lambda host: ["10.1.2.3"])


def test_allowlisted_host_resolving_to_public_dns_is_accepted():
    validate_public_url("https://github.com/", dns_resolver=lambda host: ["140.82.114.3"])


def test_dns_failure_is_refused_not_crashed():
    with pytest.raises(ResearchContractError, match="DNS"):
        validate_public_url("https://github.com/", dns_resolver=lambda host: [""])


def test_redirect_crossing_to_private_ip_is_refused():
    with pytest.raises(ResearchContractError):
        validate_redirect_chain(
            "https://github.com/",
            ["https://169.254.169.254/latest/meta-data"],
        )


def test_redirect_crossing_allowlist_is_refused():
    with pytest.raises(ResearchContractError):
        validate_redirect_chain("https://github.com/", ["https://evil.example.com/"])


def test_redirect_chain_length_cap():
    with pytest.raises(ResearchContractError):
        validate_redirect_chain(
            "https://github.com/",
            [f"https://github.com/r{i}" for i in range(4)],
            max_redirects=3,
        )


# --------------------------------------------------------------- source records

def test_ok_fetch_records_provenance_and_publication_date():
    response = _ok_response(
        "https://github.com/owner/repo",
        body=b"<html>Repository documentation</html>",
        headers={"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )
    fetcher = _fetcher_from({"https://github.com/owner/repo": response})
    record = guarded_fetch(
        fetcher,
        FetchRequest(url="https://github.com/owner/repo"),
        dns_resolver=lambda host: ["140.82.114.3"],
        clock=lambda: FIXED_CLOCK,
    )
    assert record.retrieval_status == "ok"
    assert record.url == "https://github.com/owner/repo"
    assert record.final_url == "https://github.com/owner/repo"
    assert record.retrieved_at == FIXED_CLOCK
    assert record.publication_date == "2015-10-21"
    assert record.content_sha256
    assert "Repository documentation" in (record.excerpt or "")


def test_missing_publication_date_stays_none():
    fetcher = _fetcher_from({"https://github.com/owner/repo": _ok_response("https://github.com/owner/repo")})
    record = guarded_fetch(
        fetcher,
        FetchRequest(url="https://github.com/owner/repo"),
        clock=lambda: FIXED_CLOCK,
    )
    assert record.publication_date is None


def test_oversize_body_is_marked_partial_with_reason():
    body = b"x" * 4096
    fetcher = _fetcher_from({"https://github.com/owner/repo": _ok_response("https://github.com/owner/repo", body)})
    record = guarded_fetch(
        fetcher,
        FetchRequest(url="https://github.com/owner/repo", max_body_bytes=4096),
        budget=SourceBudget(max_bytes_per_source=100, max_sources=1),
        clock=lambda: FIXED_CLOCK,
    )
    assert record.retrieval_status == "partial"
    assert "truncated" in (record.reason or "")


def test_truncated_source_record_reports_upstream_and_retained_bytes():
    body = b"x" * 4096
    url = "https://github.com/owner/repo"
    fetcher = _fetcher_from({url: _ok_response(url, body=body)})
    record = guarded_fetch(
        fetcher,
        FetchRequest(url=url, max_body_bytes=4096),
        budget=SourceBudget(max_bytes_per_source=100),
        clock=lambda: FIXED_CLOCK,
    )
    assert record.retrieval_status == "partial"
    assert record.byte_count == 100
    assert record.upstream_byte_count == 4096
    assert record.content_sha256 is not None


def test_untruncated_source_record_has_equal_byte_counts():
    url = "https://github.com/owner/repo"
    fetcher = _fetcher_from({url: _ok_response(url, body=b"payload")})
    record = guarded_fetch(fetcher, FetchRequest(url=url), clock=lambda: FIXED_CLOCK)
    assert record.byte_count == record.upstream_byte_count == len(b"payload")


def test_http_500_is_failed_and_marks_bundle_incomplete():
    bundle = ResearchBundle()
    fetcher = _fetcher_from(
        {"https://api.github.com/search/repositories?q=x&per_page=1&sort=updated": FetchResponse(500, "https://api.github.com/search/repositories?q=x&per_page=1&sort=updated", (), b"", {}, FIXED_CLOCK)}
    )
    record, repos = github_search_repositories(
        "x", bundle=bundle, fetcher=fetcher, limit=1, clock=lambda: FIXED_CLOCK
    )
    assert record.retrieval_status == "failed"
    assert repos == ()
    assert bundle.is_incomplete
    assert any("failed" in r for r in bundle.incomplete_reason())


def test_fetcher_exception_is_failed_not_raised():
    def exploding(req):
        raise RuntimeError("socket closed mid-read")

    bundle = ResearchBundle()
    fetcher = exploding
    record, repos = github_search_repositories(
        "anything", bundle=bundle, fetcher=fetcher, limit=1, clock=lambda: FIXED_CLOCK
    )
    assert record.retrieval_status == "failed"
    assert repos == ()
    assert bundle.is_incomplete


def test_refusal_is_recorded_not_raised():
    record = guarded_fetch(lambda req: _ok_response(req.url), FetchRequest(url="http://github.com/"), clock=lambda: FIXED_CLOCK)
    assert record.retrieval_status == "refused"
    assert record.reason


def test_dns_resolver_failure_refuses_not_raises():
    record = guarded_fetch(
        lambda req: _ok_response(req.url),
        FetchRequest(url="https://github.com/"),
        dns_resolver=lambda host: (_ for _ in ()).throw(RuntimeError("resolver exploded")),
        clock=lambda: FIXED_CLOCK,
    )
    assert record.retrieval_status == "refused"
    assert "DNS" in (record.reason or "")


def test_final_url_mismatch_with_chain_is_refused():
    fetcher = _fetcher_from(
        {"https://github.com/a": FetchResponse(200, "https://evil.example.com/a", (), b"x", {}, FIXED_CLOCK)}
    )
    record = guarded_fetch(fetcher, FetchRequest(url="https://github.com/a"), clock=lambda: FIXED_CLOCK)
    assert record.retrieval_status == "refused"


# --------------------------------------------------------------- claims & contradictions

def _bundle_with_source(url: str = "https://github.com/owner/repo") -> tuple[ResearchBundle, SourceRecord]:
    bundle = ResearchBundle()
    fetcher = _fetcher_from({url: _ok_response(url, body=b"evidence bytes")})
    record = guarded_fetch(fetcher, FetchRequest(url=url), clock=lambda: FIXED_CLOCK)
    bundle.add_source(record)
    return bundle, record


def test_claim_without_supporting_source_is_unsupported():
    bundle, _ = _bundle_with_source()
    claim = bundle.add_claim("The repo uses pytest.")
    assert claim.status == "unsupported"
    assert [c.claim_id for c in bundle.unsupported_claims()] == [claim.claim_id]
    assert bundle.is_incomplete
    assert any("no supporting source" in r for r in bundle.incomplete_reason())


def test_claim_with_source_is_supported():
    bundle, record = _bundle_with_source()
    claim = bundle.add_claim("The repo uses pytest.", supporting=[record.source_id])
    assert claim.status == "supported"
    assert bundle.unsupported_claims() == ()
    assert bundle.contradicted_claims() == ()


def test_claim_with_unknown_source_raises():
    bundle, _ = _bundle_with_source()
    with pytest.raises(ResearchContractError):
        bundle.add_claim("unsupported reference", supporting=["src-nope"])


def test_empty_claim_raises():
    bundle, _ = _bundle_with_source()
    with pytest.raises(ResearchContractError):
        bundle.add_claim("   ")


def test_contradiction_requires_two_sources_and_marks_claim():
    bundle, record = _bundle_with_source()
    other_url = "https://en.wikipedia.org/w/index.php?title=X&action=raw"
    bundle.add_source(
        guarded_fetch(
            _fetcher_from({other_url: _ok_response(other_url, body=b"other")}),
            FetchRequest(url=other_url),
            clock=lambda: FIXED_CLOCK,
        )
    )
    sources = [s.source_id for s in bundle.sources]
    claim = bundle.add_claim("Contradictory property.", supporting=[sources[0]])
    record_c = bundle.record_contradiction(claim.claim_id, sources[:2], note="sources disagree")
    assert isinstance(record_c, ContradictionRecord)
    assert bundle.contradicted_claims()[0].claim_id == claim.claim_id
    assert len(bundle.contradictions) == 1


def test_contradiction_with_single_source_raises():
    bundle, record = _bundle_with_source()
    claim = bundle.add_claim("Contra test.", supporting=[record.source_id])
    with pytest.raises(ResearchContractError):
        bundle.record_contradiction(claim.claim_id, [record.source_id])


def test_contradiction_with_unknown_claim_raises():
    bundle, record = _bundle_with_source()
    with pytest.raises(ResearchContractError):
        bundle.record_contradiction("claim-nope", [record.source_id, "src-x"])


def test_provider_failure_never_invents_a_citation():
    bundle = ResearchBundle()
    record = guarded_fetch(
        lambda req: (_ for _ in ()).throw(RuntimeError("boom")),
        FetchRequest(url="https://github.com/a"),
        clock=lambda: FIXED_CLOCK,
    )
    bundle.add_source(record)
    # The provider gave nothing; every claim stays unsupported on purpose.
    claim = bundle.add_claim("GitHub recovered.")
    assert claim.status == "unsupported"
    assert bundle.failed_sources()
    assert bundle.is_incomplete


# --------------------------------------------------------------- adapters

def test_github_search_parses_results_and_records_source():
    body = _repo_body()
    url = "https://api.github.com/search/repositories?q=demo&per_page=5&sort=updated"
    fetcher = _fetcher_from({url: _ok_response(url, body=body)})
    bundle = ResearchBundle()
    record, repos = github_search_repositories("demo", bundle=bundle, fetcher=fetcher, limit=5, clock=lambda: FIXED_CLOCK)
    assert record.retrieval_status == "ok"
    assert len(repos) == 1
    assert repos[0]["full_name"] == "acme/demo"
    assert repos[0]["search_source_id"] == record.source_id
    assert bundle.sources[0].source_id == record.source_id


def test_github_search_bad_json_is_partial_and_lists_no_repos():
    url = "https://api.github.com/search/repositories?q=demo&per_page=1&sort=updated"
    fetcher = _fetcher_from({url: _ok_response(url, body=b"<html>not json</html>")})
    record, repos = github_search_repositories(
        "demo", bundle=ResearchBundle(), fetcher=fetcher, limit=1, clock=lambda: FIXED_CLOCK
    )
    assert record.retrieval_status == "partial"
    assert "no repositories listed" in (record.reason or "")
    assert repos == ()


def _realistic_search_payload(count: int) -> bytes:
    """A payload shaped like a real api.github.com search response.

    Deliberately larger than the excerpt budget: this is the regression guard
    for adapters that tried to parse the excerpt instead of the retained body.
    """
    items = [
        {
            "id": index,
            "full_name": f"acme/repo-{index}",
            "html_url": f"https://github.com/acme/repo-{index}",
            "updated_at": "2026-11-01T00:00:00Z",
            "stargazers_count": 10 * index,
            "forks_count": 3,
            "language": "Python",
            "topics": ["alpha", "beta", "gamma"],
            "owner": {"login": "acme", "html_url": "https://github.com/acme"},
            "description": (
                "A reasonably long description of what this repository actually does, "
                "which real API responses always include. "
            )
            * 2,
        }
        for index in range(count)
    ]
    return json.dumps({"total_count": count, "items": items}).encode()


def test_github_search_parses_payload_larger_than_the_excerpt_budget():
    body = _realistic_search_payload(10)
    url = "https://api.github.com/search/repositories?q=demo&per_page=10&sort=updated"
    fetcher = _fetcher_from({url: _ok_response(url, body=body)})
    record, repos = github_search_repositories(
        "demo", bundle=ResearchBundle(), fetcher=fetcher, limit=10, clock=lambda: FIXED_CLOCK
    )
    assert len(body) > SourceBudget().max_excerpt_chars
    assert len(record.excerpt or "") <= SourceBudget().max_excerpt_chars + 1  # + ellipsis
    assert record.retrieval_status == "ok"
    assert len(repos) == 10
    assert [r["full_name"] for r in repos][:2] == ["acme/repo-0", "acme/repo-1"]


def test_github_search_parsing_does_not_depend_on_content_type():
    body = _repo_body()
    url = "https://api.github.com/search/repositories?q=demo&per_page=5&sort=updated"
    # No Content-Type header at all: the adapter must still behave.
    fetcher = _fetcher_from({url: _ok_response(url, body=body, headers={})})
    record, repos = github_search_repositories(
        "demo", bundle=ResearchBundle(), fetcher=fetcher, limit=5, clock=lambda: FIXED_CLOCK
    )
    assert record.retrieval_status == "ok"
    assert len(repos) == 1


def test_github_search_on_source_cap_parses_nothing_and_says_so():
    url = "https://api.github.com/search/repositories?q=demo&per_page=10&sort=updated"
    fetcher = _fetcher_from({url: _ok_response(url, body=_realistic_search_payload(50))})
    bundle = ResearchBundle(budget=SourceBudget(max_bytes_per_source=1024))
    record, repos = github_search_repositories(
        "demo", bundle=bundle, fetcher=fetcher, limit=10, clock=lambda: FIXED_CLOCK
    )
    # The adapter must honour the bundle budget rather than the 512 KiB default.
    assert record.byte_count <= 1024
    assert record.upstream_byte_count is not None
    assert record.upstream_byte_count > record.byte_count
    assert record.retrieval_status == "partial"
    assert "truncated" in (record.reason or "")
    # A truncated JSON payload cannot parse, so no result set may be presented.
    assert repos == ()


def test_github_search_json_array_payload_is_partial_with_no_repos():
    url = "https://api.github.com/search/repositories?q=demo&per_page=1&sort=updated"
    fetcher = _fetcher_from({url: _ok_response(url, body=b'[1, 2, 3]')})
    record, repos = github_search_repositories(
        "demo", bundle=ResearchBundle(), fetcher=fetcher, limit=1, clock=lambda: FIXED_CLOCK
    )
    assert record.retrieval_status == "partial"
    assert repos == ()


def test_github_search_requires_query():
    with pytest.raises(ResearchContractError):
        github_search_repositories("  ", bundle=ResearchBundle(), fetcher=lambda req: _ok_response(req.url))


def test_wikipedia_read_is_bounded_and_recorded():
    url = "https://en.wikipedia.org/w/index.php?title=Model&action=raw"
    fetcher = _fetcher_from({url: _ok_response(url, body=b"== Demo ==\narticle body")})
    bundle = ResearchBundle()
    record = wikipedia_raw_article("Model", bundle=bundle, fetcher=fetcher, clock=lambda: FIXED_CLOCK)
    assert record.retrieval_status == "ok"
    assert "== Demo ==" in (record.excerpt or "")
    assert bundle.sources[0].source_id == record.source_id


# --------------------------------------------------------------- repo inspection

def test_repo_inspection_is_read_only_and_bounded(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('a')\n")
    (tmp_path / "notes.txt").write_text("notes\n")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    result = inspect_repository(tmp_path, max_files=10, max_file_bytes=1024, max_total_bytes=4096)

    assert result.truncated is False
    assert len(result.files) == 2
    assert all(f.sha256 for f in result.files if f.skipped_reason is None)
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after


def test_repo_inspection_refuses_symlinks_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n")
    (tmp_path / "link.txt").symlink_to(outside)
    result = inspect_repository(tmp_path)
    skipped = [f for f in result.files if f.skipped_reason == "symlink_refused"]
    assert len(skipped) == 1
    assert not any(f.sha256 for f in result.files)


def test_repo_inspection_enforces_per_file_and_total_budgets(tmp_path: Path):
    """The total budget stops the walk; the per-file cap is not what stopped it.

    This asserted that a file literally named `c.bin` was never included, which
    is only true when the filesystem happens to enumerate `c.bin` last. With
    three equal 5000-byte files and a 9000-byte budget, exactly one file fits, so
    whichever name is enumerated first is the included one -- and that order is
    not the caller's to decide. The failure was observed in CI on shard 1 (3.13
    and 3.11) while passing on other interpreters and machines.

    The intent is order-independent and is now asserted directly: the walk
    truncates, nothing was skipped for being too large per file, and exactly one
    complete file's worth of bytes was admitted.
    """
    for name, size in (("a.bin", 5000), ("b.bin", 5000), ("c.bin", 5000)):
        (tmp_path / name).write_bytes(b"x" * size)
    result = inspect_repository(tmp_path, max_files=10, max_file_bytes=10000, max_total_bytes=9000)

    skipped = [f for f in result.files if f.skipped_reason]
    included = [f for f in result.files if f.skipped_reason is None]

    assert result.truncated is True
    assert skipped == []  # per-file cap not hit; the total budget stops the walk
    assert len(included) == 1, f"exactly one file fits in the budget, admitted {[f.path for f in included]}"
    assert included[0].sha256 is not None and included[0].size_bytes == 5000
    assert result.total_bytes == 5000


def test_repo_inspection_per_file_cap(tmp_path: Path):
    (tmp_path / "big.bin").write_bytes(b"y" * 10_000)
    result = inspect_repository(tmp_path, max_file_bytes=100)
    assert result.files[0].skipped_reason == "file_too_large"
    assert result.files[0].sha256 is None


def test_repo_inspection_rejects_missing_root(tmp_path: Path):
    with pytest.raises(ResearchContractError):
        inspect_repository(tmp_path / "nope")


# --------------------------------------------------------------- integrity

def test_source_budget_rejects_negative_limits():
    with pytest.raises(ResearchContractError):
        SourceBudget(max_sources=-1)


def test_bundle_to_dict_is_json_serializable_and_honest():
    bundle = ResearchBundle()
    url = "https://github.com/owner/repo"
    bundle.add_source(
        guarded_fetch(_fetcher_from({url: _ok_response(url, body=b"payload")}), FetchRequest(url=url), clock=lambda: FIXED_CLOCK)
    )
    bundle.add_claim("claim without support")
    payload = bundle.to_dict()
    json.dumps(payload)
    assert payload["is_incomplete"] is True
    assert payload["schema_version"] == 1
    assert payload["claims"][0]["status"] == "unsupported"


def test_bundle_source_cap(tmp_path: Path):
    url = "https://github.com/owner/repo"
    fetcher = _fetcher_from({url: _ok_response(url, body=b"payload")})
    bundle = ResearchBundle(budget=SourceBudget(max_sources=1))
    guarded = guarded_fetch(fetcher, FetchRequest(url=url), clock=lambda: FIXED_CLOCK)
    bundle.add_source(guarded)
    with pytest.raises(ResearchContractError):
        bundle.add_source(guarded)
