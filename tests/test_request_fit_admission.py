"""LC-07 fake installed counters/bindings only; never load/count a real model."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

import request_fit as rf
import request_fit_admission as ra
import runtime_persistence as rp
import model_lifecycle_coordinator as lc
import shared_memory_admission as sm
from local_runtime_lifecycle import CapabilityEvidence, ObservationStamp
from test_model_lifecycle_coordinator import A
from test_shared_memory_admission import opened, profile, sample, load_step

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner locking")


def harness(b, req, memory_lease, prepared, *, version="fake-v1"):
    b.prepared, b.request, b.mem = prepared, req, memory_lease
    b.count_calls, b.observe_calls = 0, 0
    b.input_tokens = 7000
    b.obs_changes, b.sample_changes, b.receipt_changes = {}, {}, {}
    b.count_error, b.counter_hook = None, None
    b.service = ra.RequestFitAdmission(
        b.coord, b.memory, clock_ms=lambda: b.now, authorize=lambda permit: b.authorized
    )
    b.counter_q = ra.CounterQualification(
        version,
        "fake-process",
        prepared.profile.native_profile_digest,
        prepared.profile.tokenizer_revision,
        prepared.profile.renderer_revision,
        "exact",
        prepared.features,
        50000,
        "fake-only-counter-qualified",
        tuple(
            CapabilityEvidence(
                name,
                "supported",
                "installed",
                req.target.runtime_id,
                version,
                req.target.profile_fingerprint,
                ("fake-assumption-not-native-qualification",),
            )
            for name in ra.CAPABILITIES
        ),
        False,
        True,
        True,
        "included",
    )

    async def observe():
        rp.assert_no_active_write_transaction("fake fit binding producer")
        b.observe_calls += 1
        binding = replace(
            ra.ResidentBinding(
                req,
                version,
                "fake-process",
                prepared.profile.native_profile_digest,
                prepared.profile.tokenizer_revision,
                prepared.profile.renderer_revision,
                prepared.profile.memory.context_tokens,
                ObservationStamp(b.now / 1000, b.coord.observer_clock_id),
                "fake-binding-" + str(b.observe_calls),
                True,
                True,
                True,
                0,
                True,
            ),
            **b.obs_changes,
        )
        b.last_binding = binding
        return ra.FitObservation(
            binding, sample(b.memory, now=b.now, **b.sample_changes)
        )

    async def counter(p, q):
        rp.assert_no_active_write_transaction("fake exact full-wire counter")
        assert (
            b.coord._call(
                lambda conn: conn.execute(
                    "SELECT reference FROM journal WHERE event='request_fit_count_sent'"
                ).fetchone()[0]
            )
            == p.body_digest
        )
        assert p.body == prepared.body and q == b.counter_q
        b.count_calls += 1
        b.now += 1
        receipt = replace(
            ra.CountReceipt(
                b.last_binding,
                p.body_digest,
                b.input_tokens,
                q.confidence,
                p.features,
                ObservationStamp(b.now / 1000, b.coord.observer_clock_id),
                "fake-full-wire-inclusive-receipt",
                True,
                True,
            ),
            **b.receipt_changes,
        )
        if b.counter_hook:
            b.counter_hook()
        if b.count_error:
            raise b.count_error
        return receipt

    b.observe, b.counter = observe, counter
    b.permit = ra.FitPermit(
        req, prepared.body_digest, 50000, "fake-live-count-authority"
    )
    return b


def setup(tmp_path, *, context=8192):
    coord, memory, fleet = opened(tmp_path)
    b = SimpleNamespace(
        coord=coord, memory=memory, fleet=fleet, now=1000, authorized=True
    )
    mp = replace(profile(A), context_tokens=context)
    original_evidence = fleet.evidence

    def evidence(*args, **kwargs):
        result = original_evidence(*args, **kwargs)
        return replace(
            result,
            inventories=tuple(
                replace(
                    inv,
                    instances=tuple(
                        replace(i, effective_context_tokens=context)
                        for i in inv.instances
                    ),
                )
                for inv in result.inventories
            ),
        )

    fleet.evidence = evidence
    step = load_step(coord)
    ml = memory.reserve_model(step, mp, sample(memory), now_ms=b.now)
    assert memory.dispatch(ml, sample(memory), now_ms=b.now)
    fleet.apply(coord, step, b.now)
    verify = coord.claim_next("shared", now_ms=b.now)
    fleet.apply(coord, verify, b.now)
    req = coord.claim_next("shared", now_ms=b.now)
    fp = rf.FitProfile(mp, "omlx_openai", "a" * 64, "fake-tokenizer", "fake-renderer")
    prepared = rf.prepare_request(
        req,
        fp,
        rf.OutputBudget(512, 128),
        rf.encode(
            dict(
                model=A.artifact_id,
                stream=True,
                max_tokens=512,
                messages=[
                    dict(role="system", content="KEEP SECURITY SECRET"),
                    dict(role="user", content="keep latest ಕರ್ನಾಟಕ 😀"),
                ],
            )
        ),
        origin="harness",
    )
    return harness(b, req, ml, prepared)


async def evaluate(b):
    return await b.service.evaluate(
        b.permit,
        b.prepared,
        lease=b.mem,
        qualification=b.counter_q,
        counter=b.counter,
        observe=b.observe,
    )


def test_positive_full_wire_count_approval_is_bound_to_lease_body_profile_and_expiry(
    tmp_path,
):
    b = setup(tmp_path)
    with b.coord:
        approval = asyncio.run(evaluate(b))
        assert (
            approval.diagnostic.input_tokens == 7000
            and b.count_calls == 1
            and b.observe_calls == 2
        )
        assert b.service.authorizes(
            b.request, b.prepared.body_digest, b.prepared.profile.native_profile_digest
        )
        assert not b.service.authorizes(
            b.request, "0" * 64, b.prepared.profile.native_profile_digest
        )
        assert not b.service.authorizes(
            replace(b.request, instance_id="other"),
            b.prepared.body_digest,
            b.prepared.profile.native_profile_digest,
        )
        assert not b.service.authorizes(b.request, b.prepared.body_digest, "0" * 64)
        assert (
            b.coord.snapshot("shared").active_requests == 1
            and b.memory.view("shared").committed_bytes == 600
        )
        with b.service._mutex:
            assert b.service._issued[b.request.lease_id] is approval
        b.now = approval.expires_ms
        assert not b.service.authorizes(
            b.request, b.prepared.body_digest, b.prepared.profile.native_profile_digest
        )


def test_7326_4096_refusal_keeps_anchors_and_does_not_infer_resize_release_or_retry(
    tmp_path,
):
    b = setup(tmp_path, context=4096)
    with b.coord:
        b.input_tokens = 7326
        original = b.prepared.body
        with pytest.raises(rf.RequestFitDenied, match="context_exceeded") as error:
            asyncio.run(evaluate(b))
        assert error.value.to_mapping()["fit"]["input_tokens"] == 7326
        assert "SECRET" not in str(error.value.to_mapping())
        assert b.prepared.body == original and b.count_calls == 1
        assert not b.service.authorizes(
            b.request, b.prepared.body_digest, b.prepared.profile.native_profile_digest
        )
        assert (
            b.coord.snapshot("shared").active_requests == 1
            and b.coord.snapshot("shared").state == "idle"
        )
        assert b.memory.view("shared").committed_bytes == 600
        with pytest.raises(rf.RequestFitDenied, match="counter_already_dispatched"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 1


@pytest.mark.parametrize(
    "case",
    [
        "no_authorizer",
        "revoked",
        "no_caps",
        "offline_caps",
        "wrong_scope",
        "may_load",
        "unknown_load",
        "workspace_unknown",
        "request_changed",
        "tokenizer",
        "renderer",
        "accounting",
        "expired",
        "missing_features",
        "estimated",
    ],
)
def test_unqualified_counter_or_authority_is_zero_counter_effect(tmp_path, case):
    b = setup(tmp_path)
    with b.coord:
        if case == "no_authorizer":
            b.service.authorize = None
        elif case == "revoked":
            b.authorized = False
        elif case == "no_caps":
            b.counter_q = replace(b.counter_q, capabilities=())
        elif case in ("offline_caps", "wrong_scope"):
            cap = replace(
                b.counter_q.capabilities[0],
                **(
                    {"basis": "offline"}
                    if case == "offline_caps"
                    else {"runtime_id": "other"}
                ),
            )
            b.counter_q = replace(
                b.counter_q, capabilities=(cap,) + b.counter_q.capabilities[1:]
            )
        else:
            changes = {
                "may_load": {"may_load": True},
                "unknown_load": {"may_load": None},
                "workspace_unknown": {"workspace_within_model_peak": False},
                "request_changed": {"preserves_request": False},
                "tokenizer": {"tokenizer_revision": "wrong"},
                "renderer": {"renderer_revision": "wrong"},
                "accounting": {"output_accounting": "additional"},
                "expired": {"expires_ms": 1000},
                "missing_features": {"supported_features": frozenset({"messages"})},
                "estimated": {"confidence": "estimated"},
            }[case]
            b.counter_q = replace(b.counter_q, **changes)
        with pytest.raises(rf.RequestFitDenied):
            asyncio.run(evaluate(b))
        assert b.count_calls == 0 and b.observe_calls == 0
        assert b.fleet.models == {"omlx": A}


def test_missing_counter_is_not_an_implicit_native_tokenizer_probe(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        with pytest.raises(rf.RequestFitDenied, match="counter_unqualified"):
            asyncio.run(
                b.service.evaluate(
                    b.permit,
                    b.prepared,
                    lease=b.mem,
                    qualification=b.counter_q,
                    observe=b.observe,
                )
            )
        assert b.count_calls == 0 and b.observe_calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        dict(effective_context_tokens=4096),
        dict(active_requests=None),
        dict(active_requests=1),
        dict(native_controls_verified=False),
        dict(exclusive_resident_verified=False),
        dict(ownership_verified=False),
        dict(late_effects_excluded=False),
        dict(server_identity="restarted"),
        dict(renderer_revision="changed"),
        dict(tokenizer_revision="changed"),
    ],
)
def test_unknown_context_native_controls_or_instance_never_runs_counter(
    tmp_path, changes
):
    b = setup(tmp_path)
    with b.coord:
        b.obs_changes.update(changes)
        with pytest.raises(rf.RequestFitDenied):
            asyncio.run(evaluate(b))
        assert b.count_calls == 0


@pytest.mark.parametrize(
    "bad", ["context", "instance", "server", "renderer", "revoked", "pressure"]
)
def test_context_template_identity_or_pressure_change_during_count_never_authorizes_inference(
    tmp_path, bad
):
    b = setup(tmp_path)
    with b.coord:

        def change():
            if bad == "context":
                b.obs_changes["effective_context_tokens"] = 4096
            elif bad == "instance":
                b.obs_changes["lease"] = replace(b.request, instance_id="changed")
            elif bad == "server":
                b.obs_changes["server_identity"] = "restarted"
            elif bad == "renderer":
                b.obs_changes["renderer_revision"] = "changed"
            elif bad == "revoked":
                b.authorized = False
            else:
                b.sample_changes["pressure"] = "critical"

        b.counter_hook = change
        with pytest.raises(rf.RequestFitDenied, match="count_uncertain"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 1
        assert b.coord.snapshot("shared").state == "reconciling"
        assert (
            b.memory.view("shared").committed_bytes == 600
            and b.memory.view("shared").state == "paused"
        )
        assert not b.service.authorizes(
            b.request, b.prepared.body_digest, b.prepared.profile.native_profile_digest
        )


@pytest.mark.parametrize(
    "changes",
    [
        dict(body_digest="0" * 64),
        dict(input_tokens=True),
        dict(input_tokens=-1),
        dict(confidence="estimated"),
        dict(covered_features=frozenset({"messages"})),
        dict(template_included=False),
        dict(terminal=False),
        dict(component_tokens=(("template", 99999),)),
    ],
)
def test_count_receipt_unknowns_or_partial_template_message_only_counts_cannot_fit(
    tmp_path, changes
):
    b = setup(tmp_path)
    with b.coord:
        b.receipt_changes = changes
        with pytest.raises(rf.RequestFitDenied, match="count_uncertain"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 1 and b.coord.snapshot("shared").active_requests == 1


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("SECRET raw body"),
        OSError("SECRET raw body"),
        asyncio.CancelledError(),
    ],
)
def test_timeout_cancel_disconnect_holds_counter_effect_and_never_replays(
    tmp_path, error
):
    b = setup(tmp_path)
    with b.coord:
        b.count_error = error
        with pytest.raises((rf.RequestFitDenied, asyncio.CancelledError)) as caught:
            asyncio.run(evaluate(b))
        assert "SECRET" not in str(caught.value)
        assert b.coord.snapshot("shared").state == "reconciling"
        assert b.memory.view("shared").committed_bytes == 600
        with pytest.raises(rf.RequestFitDenied):
            asyncio.run(evaluate(b))
        assert b.count_calls == 1


def test_forged_approval_object_or_another_service_is_not_authority(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        approval = asyncio.run(evaluate(b))
        other = ra.RequestFitAdmission(
            b.coord, b.memory, clock_ms=lambda: b.now, authorize=lambda _: True
        )
        assert not other.authorizes(
            approval.lease, approval.body_digest, approval.native_profile_digest
        )
        b.service.revoke(b.request)
        assert not b.service.authorizes(
            approval.lease, approval.body_digest, approval.native_profile_digest
        )


def test_memory_pressure_pause_commits_before_any_counter_call(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        b.sample_changes["pressure"] = "critical"
        with pytest.raises(sm.MemoryDenied):
            asyncio.run(evaluate(b))
        assert b.count_calls == 0 and b.memory.view("shared").state == "paused"
        assert (
            b.coord._call(
                lambda conn: conn.execute(
                    "SELECT count(*) FROM journal WHERE event='request_fit_count_sent'"
                ).fetchone()[0]
            )
            == 0
        )


def test_restart_or_owner_close_invalidates_ephemeral_fit_authority(tmp_path):
    b = setup(tmp_path)
    approval = asyncio.run(evaluate(b))
    b.coord.close()
    assert not b.service.authorizes(
        approval.lease, approval.body_digest, approval.native_profile_digest
    )
    with lc.LifecycleCoordinator(tmp_path / "owner") as new:
        assert new.snapshot("shared").state == "reconciling"
        assert sm.SharedMemoryAdmission(new).view("shared").committed_bytes == 600


def test_current_service_import_has_no_runtime_client_or_app_activation():
    from pathlib import Path

    assert "import request_fit" not in Path("app.py").read_text()


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "mode", ["missing_fit", "approved", "changed_body", "expired", "long_stream"]
)
def test_real_dormant_adapter_dispatch_requires_exact_fit_not_a_generic_authorized_flag(
    tmp_path, backend, mode
):
    if backend == "ollama":
        import test_ollama_lifecycle_bridge as driver
        from ollama_protocol import prepare_chat
        from ollama_lifecycle import _profile_digest
    else:
        import test_lmstudio_lifecycle_bridge as driver
        from lmstudio_protocol import prepare_inference
    b = driver.setup(tmp_path)
    with b.coord:

        async def run():
            req, ml = await driver.warm(b)
            if backend == "ollama":
                native = prepare_chat(
                    b.q.profile, [dict(role="user", content="FULL ORIGINAL ಕರ್ನಾಟಕ 😀")]
                )
                fp = rf.FitProfile(
                    b.q.profile.memory,
                    "ollama_native",
                    _profile_digest(b.q.profile),
                    "fake-tokenizer",
                    "fake-renderer",
                )
                version = b.q.server_version
            else:
                native = prepare_inference(
                    req.instance_id,
                    b.q.profile,
                    dict(
                        model=req.instance_id,
                        stream=True,
                        messages=[dict(role="user", content="FULL ORIGINAL ಕರ್ನಾಟಕ 😀")],
                        max_tokens=512,
                    ),
                )
                fp = rf.FitProfile(
                    b.q.profile.memory,
                    "lmstudio_openai",
                    b.q.profile.digest,
                    "fake-tokenizer",
                    "fake-renderer",
                )
                version = b.q.version
            prepared = rf.prepare_request(
                req, fp, rf.OutputBudget(512), native.body, origin="tool_followup"
            )
            harness(b, req, ml, prepared, version=version)
            b.bridge.admit_request = (
                None if mode == "missing_fit" else b.service.authorizes
            )
            before_posts = len(driver.mutations(b))
            if mode != "missing_fit":
                await evaluate(b)
            if mode == "changed_body":
                if backend == "ollama":
                    native = prepare_chat(
                        b.q.profile, [dict(role="user", content="CHANGED AFTER COUNT")]
                    )
                else:
                    native = prepare_inference(
                        req.instance_id,
                        b.q.profile,
                        dict(
                            model=req.instance_id,
                            stream=True,
                            messages=[dict(role="user", content="CHANGED AFTER COUNT")],
                            max_tokens=512,
                        ),
                    )
            elif mode == "expired":
                b.now += 5000
            elif mode == "long_stream":
                b.wire.hook = lambda method, path: (
                    setattr(b, "now", 10000)
                    if path in ("/api/chat", "/v1/chat/completions")
                    else None
                )
            if mode in ("missing_fit", "changed_body", "expired"):
                # The old policy-only authorizer is still True, but no native
                # inference may occur without the distinct exact fit ticket.
                with pytest.raises(RuntimeError, match="request_fit_required"):
                    await driver.infer(
                        b,
                        req,
                        ml,
                        **(
                            {"p": native}
                            if backend == "ollama"
                            else {"prepared": native}
                        ),
                    )
                assert len(driver.mutations(b)) == before_posts
            else:
                result = await driver.infer(
                    b,
                    req,
                    ml,
                    **({"p": native} if backend == "ollama" else {"prepared": native}),
                )
                assert result and len(driver.mutations(b)) == before_posts + 1
                assert b.coord.snapshot("shared").active_requests == 0
                assert b.memory.view("shared").committed_bytes == 600

        asyncio.run(run())


def test_qualified_media_upper_bound_and_missing_media_coverage(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        body = rf.decode(b.prepared.body)
        body["messages"][-1]["content"] = [
            {"type": "text", "text": "owned anchor"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        prepared = rf.prepare_request(
            b.request,
            b.prepared.profile,
            b.prepared.budget,
            rf.encode(body),
            origin="chat",
        )
        harness(b, b.request, b.mem, prepared)
        b.counter_q = replace(
            b.counter_q, supported_features=b.counter_q.supported_features - {"media"}
        )
        with pytest.raises(rf.RequestFitDenied, match="counter_unqualified"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 0
        b.counter_q = replace(
            b.counter_q,
            supported_features=prepared.features,
            confidence="qualified_upper_bound",
        )
        assert asyncio.run(evaluate(b)).diagnostic.confidence == "qualified_upper_bound"
        assert b.count_calls == 1
        # This positive bound is an invented matching-local-counter assumption,
        # not oMLX /tokenize media support (that route explicitly rejects media).


def test_atomic_counter_receipt_failure_sends_no_count_and_can_retry_before_commit(
    tmp_path, monkeypatch
):
    b = setup(tmp_path)
    with b.coord:
        original = b.coord._event

        def fail(conn, domain, event, *args, **kwargs):
            if event == "request_fit_count_sent":
                raise RuntimeError("injected write failure")
            return original(conn, domain, event, *args, **kwargs)

        monkeypatch.setattr(b.coord, "_event", fail)
        with pytest.raises(RuntimeError, match="injected"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 0
        assert (
            b.coord._call(
                lambda conn: conn.execute(
                    "SELECT count(*) FROM journal WHERE event='request_fit_count_sent'"
                ).fetchone()[0]
            )
            == 0
        )
        monkeypatch.setattr(b.coord, "_event", original)
        asyncio.run(evaluate(b))
        assert b.count_calls == 1


def test_same_lease_cannot_repeat_a_counter_even_after_success(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        asyncio.run(evaluate(b))
        with pytest.raises(rf.RequestFitDenied, match="counter_already_dispatched"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_epoch", 999),
        ("generation", 999),
        ("instance_id", "wrong"),
        ("request_id", "queued-not-admitted"),
    ],
)
def test_no_cold_or_stale_counting_lease_even_with_a_claimed_matching_counter(
    tmp_path, field, value
):
    b = setup(tmp_path)
    with b.coord:
        forged = replace(b.request, **{field: value})
        b.prepared = replace(b.prepared, lease=forged)
        b.permit = replace(b.permit, lease=forged)
        with pytest.raises(rf.RequestFitDenied, match="lease_stale"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 0 and b.observe_calls == 0


def test_unknown_counter_error_reports_safe_limits_but_not_fabricated_counts(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        b.counter_q = replace(b.counter_q, may_load=True)
        with pytest.raises(rf.RequestFitDenied) as error:
            asyncio.run(evaluate(b))
        info = error.value.to_mapping()
        assert info["fit"]["approved_context_tokens"] == 8192
        assert info["fit"]["input_tokens"] is None and info["fit"]["confidence"] is None
        assert info["fit"]["generation_reserve_tokens"] == 512
        assert "SECRET" not in str(info)


def test_journal_contains_receipt_metadata_not_assembled_messages_or_tool_results(
    tmp_path,
):
    b = setup(tmp_path)
    with b.coord:
        asyncio.run(evaluate(b))
        rows = b.coord._call(
            lambda conn: tuple(
                tuple(row)
                for row in conn.execute("SELECT event,reference FROM journal")
            )
        )
        assert all(
            "SECRET" not in str(row) and "ಕರ್ನಾಟಕ" not in str(row) for row in rows
        )


def test_bounded_ticket_cache_refuses_without_effects_and_prunes_expired(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        approval = asyncio.run(evaluate(b))
        b.service._issued = {str(i): approval for i in range(256)}
        with pytest.raises(rf.RequestFitDenied, match="approval_stale"):
            asyncio.run(evaluate(b))
        assert b.count_calls == 1 and b.observe_calls == 2
        b.now = approval.expires_ms
        with pytest.raises(rf.RequestFitDenied, match="counter_already_dispatched"):
            asyncio.run(evaluate(b))
        assert not b.service._issued and b.count_calls == 1
