"""Fake qualified Ollama only; never contact a runtime or infer qualification."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

import model_lifecycle_coordinator as lc
import ollama_lifecycle as ol
import ollama_protocol as op
import runtime_persistence as rp
import shared_memory_admission as sm
from local_runtime_lifecycle import CapabilityEvidence, ObservationStamp
from test_model_lifecycle_coordinator import A
from test_ollama_native_protocol import (
    TARGET,
    TOOL,
    done,
    line,
    native_profile,
    packet,
    tool_packet,
)
from test_shared_memory_admission import opened, sample

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX coordinator ownership"
)


class Wire:
    def __init__(self, bundle):
        self.b = bundle
        self.calls = []
        self.queries = {}
        self.ack = {}
        self.status = 200
        self.error = None
        self.packets = [
            line(
                packet(
                    message=dict(role="assistant", thinking="think", content="READY")
                )
            ),
            line(done()),
        ]
        self.stream_error = None
        self.hook = None
        self.apply_effect = True
        self.closed = False
        self.rows_extra = []

    def rows(self):
        target = self.b.fleet.models.get("ollama")
        return (
            []
            if target is None
            else [
                dict(
                    name=target.artifact_id,
                    model=target.artifact_id,
                    digest=target.artifact_digest,
                    context_length=8192,
                    runner="llama",
                    size=500,
                    size_vram=450,
                    expires_at="9999-01-01T00:00:00Z",
                )
            ]
        ) + self.rows_extra

    def before(self, method, path, body):
        rp.assert_no_active_write_transaction("fake Ollama wire")
        self.calls.append((method, path, op.parse_json(body) if body else None))
        if self.hook:
            self.hook(method, path)
        if method == "POST" and path == "/api/chat":
            b = self.b
            # Durable receipt visible BEFORE external allocation/send.
            assert b.coord._call(
                lambda conn: conn.execute(
                    "SELECT 1 FROM journal WHERE event LIKE 'ollama_sent_%'"
                ).fetchone()
            )
            assert b.memory.view("shared").committed_bytes == 600

    async def request(self, method, path, body, timeout_s):
        self.before(method, path, body)
        if path in self.queries:
            return self.queries[path]
        if path == "/api/version":
            return 200, {"version": "fake-v1"}
        if path == "/api/tags":
            return 200, {
                "models": [
                    {"name": TARGET.artifact_id, "digest": TARGET.artifact_digest},
                    {
                        "name": "remote:cloud",
                        "digest": "remote",
                        "remote_host": "not-contacted",
                    },
                ]
            }
        if path == "/api/show":
            return 200, {
                "model_info": {"architecture.context_length": 999999},
                "capabilities": ["completion", "tools", "thinking"],
            }
        if path == "/api/ps":
            return 200, {"models": self.rows()}
        assert method == "POST" and path == "/api/chat"
        request = op.parse_json(body)
        kind = "unload" if request["keep_alive"] == 0 else "load"
        if self.apply_effect:
            if kind == "load":
                assert not self.b.fleet.models
                self.b.fleet.models["ollama"] = TARGET
            else:
                self.b.fleet.models.pop("ollama", None)
        if self.error:
            raise self.error
        result = dict(
            model=request["model"],
            done=True,
            done_reason=kind,
            message={"role": "assistant", "content": ""},
        )
        result.update(self.ack)
        return self.status, result

    @asynccontextmanager
    async def stream(self, path, body, timeout_s):
        self.before("POST", path, body)
        self.closed = False

        async def chunks():
            for chunk in self.packets:
                yield chunk
            if self.stream_error:
                raise self.stream_error

        try:
            yield self.status, chunks()
        finally:
            self.closed = True


def setup(tmp_path, *, authorize=True):
    coord, memory, fleet = opened(tmp_path)
    b = SimpleNamespace(
        coord=coord, memory=memory, fleet=fleet, now=1000, authorized=True
    )
    b.q = ol.OllamaQualification(
        "ollama",
        "fake-v1",
        op.SOURCE_PIN,
        native_profile(),
        50000,
        "fake-qualification",
        tuple(
            CapabilityEvidence(
                name,
                "supported",
                "installed",
                "ollama",
                "fake-v1",
                TARGET.profile_fingerprint,
                ("fake-assumption-only",),
            )
            for name in ol.REQUIRED_CAPABILITIES
        ),
    )
    b.wire = Wire(b)
    b.bridge = ol.OllamaLifecycleBridge(
        coord,
        memory,
        b.wire,
        clock_ms=lambda: b.now,
        authorize=(lambda permit: b.authorized) if authorize else None,
        admit_request=lambda *args: (
            True
        ),  # explicit fake fit assumption; not a real counter
    )
    return b


def permit(b, intent, **changes):
    return replace(
        ol.OllamaPermit(
            intent,
            b.q,
            50000,
            "fake-live-grant-and-fit",
            changes.pop("request_digest", None),
        ),
        **changes,
    )


def activity(b, intent, **changes):
    instance = (
        intent.instance_id
        if isinstance(intent, lc.RequestLease)
        else intent.source_instance_id
        if intent.kind in ("drain", "unload")
        else None
    )
    return replace(
        ol.OllamaActivity(
            intent,
            TARGET,
            ObservationStamp(b.now / 1000, b.coord.observer_clock_id),
            "fake-current-terminal-instance-proof",
            instance,
            0,
            True,
            True,
        ),
        **changes,
    )


def reserved(b):
    b.coord.enqueue("one", "shared", TARGET, deadline_ms=100000, now_ms=b.now)
    step = b.coord.claim_next("shared", now_ms=b.now)
    assert step.kind == "load"
    lease = b.memory.reserve_model(
        step, b.q.profile.memory, sample(b.memory, now=b.now), now_ms=b.now
    )
    return step, lease


async def proof(b, observation):
    assert observation.runtime_id == "ollama"
    return replace(b.fleet.evidence(b.coord, b.now), reference=observation.reference)


async def warm(b):
    step, lease = reserved(b)
    result = await b.bridge.send(
        permit(b, step),
        activity(b, step),
        lease=lease,
        sample=sample(b.memory, now=b.now),
    )
    assert result == "verify_required"
    verify = b.coord.claim_next("shared", now_ms=b.now)
    assert verify.kind == "verify"
    assert await b.bridge.confirm(
        permit(b, verify), lambda observation: proof(b, observation)
    )
    request = b.coord.claim_next("shared", now_ms=b.now)
    assert isinstance(request, lc.RequestLease)
    return request, lease


async def infer(b, request, lease, *, p=None, terminal=None, activity_changes=None):
    prepared = p or op.prepare_chat(
        b.q.profile, [dict(role="user", content="keep exact anchor")], tools=[TOOL]
    )

    async def produce():
        assert b.wire.closed
        b.now += 1
        return terminal or activity(b, request)

    return [
        event
        async for event in b.bridge.infer(
            permit(b, request, request_digest=prepared.digest),
            prepared,
            activity(b, request, **(activity_changes or {})),
            lease=lease,
            sample=sample(b.memory, now=b.now),
            produce_terminal=produce,
        )
    ]


def mutations(b):
    return [call for call in b.wire.calls if call[0:2] == ("POST", "/api/chat")]


def test_catalog_show_ps_are_separate_and_do_not_promote_catalog_to_residency(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            assert len(await b.bridge.catalog()) == 2
            assert (await b.bridge.describe(TARGET.artifact_id))["model_info"][
                "architecture.context_length"
            ] == 999999
            assert (await b.bridge.observe("ollama")).rows == ()

        asyncio.run(run())
        assert [call[1] for call in b.wire.calls] == [
            "/api/tags",
            "/api/show",
            "/api/version",
            "/api/ps",
        ]
        assert b.wire.calls[1][2] == {"model": "tiny:tag:local"}
        assert b.fleet.models == {} and mutations(b) == []


def test_load_verify_infer_warm_profile_and_usage(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            events = await infer(b, request, lease)
            assert events[:2] == [
                dict(type="reasoning", text="think"),
                dict(type="content", text="READY"),
            ]
            assert (
                events[-1]["completion_tokens"] == 0
                and events[-1]["reasoning_tokens"] is None
            )
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.coord.snapshot("shared").resident == TARGET
            assert b.memory.view("shared").committed_bytes == 600
            load_body, infer_body = [item[2] for item in mutations(b)]
            for key in (
                "options",
                "runner",
                "model",
                "think",
                "truncate",
                "shift",
                "keep_alive",
            ):
                assert load_body[key] == infer_body[key]
            assert load_body["options"]["num_ctx"] == 8192

        asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "no_authorizer",
        "revoked",
        "expired",
        "source_only",
        "unknown_cap",
        "wrong_runtime",
        "wrong_fingerprint",
        "wrong_pin",
        "wrong_version",
        "wrong_target",
        "activity_unknown",
        "late_possible",
        "busy",
        "not_owned",
        "wrong_clock",
        "stale",
        "wrong_instance",
        "missing_memory",
    ],
)
def test_unqualified_or_unauthorized_send_is_zero_effect(tmp_path, case):
    b = setup(tmp_path, authorize=case != "no_authorizer")
    with b.coord:
        step, lease = reserved(b)
        p, a = permit(b, step), activity(b, step)
        if case == "revoked":
            b.authorized = False
        elif case == "expired":
            p = replace(p, expires_ms=1000)
        elif case in (
            "source_only",
            "unknown_cap",
            "wrong_runtime",
            "wrong_fingerprint",
        ):
            changes = {
                "source_only": {"basis": "source"},
                "unknown_cap": {"state": "unknown"},
                "wrong_runtime": {"runtime_id": "other"},
                "wrong_fingerprint": {"profile_fingerprint": "other"},
            }[case]
            p = replace(
                p,
                qualification=replace(
                    b.q,
                    capabilities=(replace(b.q.capabilities[0], **changes),)
                    + b.q.capabilities[1:],
                ),
            )
        elif case == "wrong_pin":
            p = replace(p, qualification=replace(b.q, source_pin="other"))
        elif case == "wrong_version":
            b.wire.queries["/api/version"] = 200, {"version": "changed"}
        elif case == "wrong_target":
            p = replace(
                p, intent=replace(step, target=replace(TARGET, artifact_digest="other"))
            )
        elif case == "activity_unknown":
            a = replace(a, active_requests=None)
        elif case == "late_possible":
            a = replace(a, late_effects_excluded=False)
        elif case == "busy":
            a = replace(a, active_requests=1)
        elif case == "not_owned":
            a = replace(a, ownership_verified=False)
        elif case == "wrong_clock":
            a = replace(a, stamp=ObservationStamp(1.0, "wrong"))
        elif case == "stale":
            b.now = 10000
        elif case == "wrong_instance":
            a = replace(a, instance_id="made-up")
        elif case == "missing_memory":
            lease = None
        with pytest.raises(op.OllamaDenied):
            asyncio.run(
                b.bridge.send(p, a, lease=lease, sample=sample(b.memory, now=b.now))
            )
        assert not mutations(b) and b.fleet.models == {}


@pytest.mark.parametrize("status", [202, 204, 301, 400, 401, 403, 404, 409, 500])
def test_non200_load_ack_is_never_success_or_retried_and_holds(tmp_path, status):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.status = status
        with pytest.raises(op.OllamaDenied, match="http_rejected"):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert b.coord.snapshot("shared").state == "reconciling"
        assert (
            b.memory.view("shared").state == "paused"
            and b.memory.view("shared").committed_bytes == 600
        )
        assert b.coord.claim_next("shared", now_ms=b.now) is None
        with pytest.raises(op.OllamaDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert len(mutations(b)) == 1


@pytest.mark.parametrize(
    "failure", [TimeoutError("secret"), OSError("secret"), asyncio.CancelledError()]
)
def test_late_load_timeout_cancellation_holds_without_replay(tmp_path, failure):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.error = failure
        with pytest.raises((op.OllamaDenied, asyncio.CancelledError)) as error:
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert "secret" not in str(error.value)
        # Effect already happened even though the caller saw failure.
        assert b.fleet.models == {"ollama": TARGET}
        assert b.coord.snapshot("shared").state == "reconciling"
        assert b.memory.view("shared").committed_bytes == 600
        assert len(mutations(b)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        dict(done=False),
        dict(done_reason="stop"),
        dict(model="wrong"),
        dict(remote_host="cloud"),
        dict(message={"role": "assistant", "content": "not lifecycle-only"}),
    ],
)
def test_malformed_load_ack_never_advances_to_verify(tmp_path, changes):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.ack = changes
        with pytest.raises(op.OllamaDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert b.coord.snapshot("shared").operation_stage == "load"
        assert b.coord.snapshot("shared").state == "reconciling"


def test_revocation_during_observation_rechecked_before_atomic_send(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.hook = lambda method, path: (
            setattr(b, "authorized", False) if path == "/api/ps" else None
        )
        with pytest.raises(op.OllamaDenied, match="authority_denied"):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert not mutations(b)
        assert b.memory.view("shared").undispatched_ids == (lease.lease_id,)


def test_memory_pressure_pause_commits_without_send_marker(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        with pytest.raises(sm.MemoryDenied, match="pressure_unsafe"):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory, pressure="critical"),
                )
            )
        assert not mutations(b)
        assert b.memory.view("shared").state == "paused"
        assert (
            b.coord._call(
                lambda conn: conn.execute(
                    "SELECT count(*) FROM journal WHERE event='ollama_sent_load'"
                ).fetchone()[0]
            )
            == 0
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("digest", "wrong"),
        ("context_length", 4096),
        ("runner", "other"),
        ("context_length", None),
        ("size_vram", -1),
        ("name", "alias"),
    ],
)
def test_ps_context_digest_runner_and_unknowns_are_not_architectural_show_facts(
    tmp_path, field, value
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            step, lease = reserved(b)
            await b.bridge.send(
                permit(b, step), activity(b, step), lease=lease, sample=sample(b.memory)
            )
            verify = b.coord.claim_next("shared", now_ms=b.now)
            row = b.wire.rows()[0]
            row[field] = value
            b.wire.queries["/api/ps"] = 200, {"models": [row]}
            with pytest.raises(op.OllamaDenied):
                await b.bridge.confirm(
                    permit(b, verify), lambda observation: proof(b, observation)
                )
            assert b.coord.snapshot("shared").resident is None
            assert b.coord.snapshot("shared").operation_stage == "verify"

        asyncio.run(run())


async def unload_step(b):
    request, lease = await warm(b)
    await infer(b, request, lease)
    b.coord.enqueue("switch", "shared", A, deadline_ms=100000, now_ms=b.now)
    drain = b.coord.claim_next("shared", now_ms=b.now)
    assert drain.kind == "drain"
    assert b.bridge.drain(permit(b, drain), activity(b, drain))
    unload = b.coord.claim_next("shared", now_ms=b.now)
    assert unload.kind == "unload"
    return unload, lease


def test_unload_ack_empty_ps_need_real_all_runtime_physical_proof_then_memory_settlement(
    tmp_path,
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            unload, lease = await unload_step(b)
            assert (
                await b.bridge.send(permit(b, unload), activity(b, unload))
                == "pending_release_proof"
            )
            assert b.fleet.models == {}  # Even empty ps is insufficient.
            assert b.coord.claim_next("shared", now_ms=b.now) is None

            async def bad(observation):
                return replace(await proof(b, observation), operations_terminal=False)

            with pytest.raises(lc.CoordinatorError, match="release_unverified"):
                await b.bridge.confirm(permit(b, unload), bad)
            assert b.coord.claim_next("shared", now_ms=b.now) is None
            assert b.memory.view("shared").committed_bytes == 600
            assert await b.bridge.confirm(
                permit(b, unload), lambda observation: proof(b, observation)
            )
            assert b.memory.view("shared").committed_bytes == 600
            b.memory.settle(
                lease,
                sample(b.memory, now=b.now),
                terminal=True,
                reference="fake-release",
                now_ms=b.now,
            )
            assert b.memory.view("shared").committed_bytes == 0
            assert b.coord.claim_next("shared", now_ms=b.now).kind == "load"

        asyncio.run(run())


def test_unload_ack_is_not_replayed_while_native_release_pending(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            unload, _ = await unload_step(b)
            b.wire.apply_effect = False
            assert (
                await b.bridge.send(permit(b, unload), activity(b, unload))
                == "pending_release_proof"
            )
            with pytest.raises(op.OllamaDenied):
                await b.bridge.confirm(
                    permit(b, unload), lambda observation: proof(b, observation)
                )
            before = len(mutations(b))
            with pytest.raises(op.OllamaDenied, match="already_recorded"):
                await b.bridge.send(permit(b, unload), activity(b, unload))
            assert len(mutations(b)) == before
            assert b.coord.claim_next("shared", now_ms=b.now) is None

        asyncio.run(run())


@pytest.mark.parametrize("change", ["options", "think", "runner"])
def test_load_inference_exact_profile_cannot_change_behind_same_fingerprint(
    tmp_path, change
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            settings = {
                "options": {
                    "options_json": '{"num_ctx":8192,"num_predict":256,"temperature":0.8}'
                },
                "think": {"think": False},
                "runner": {"runner": "other"},
            }[change]
            b.q = replace(b.q, profile=replace(b.q.profile, **settings))
            before = len(mutations(b))
            with pytest.raises(op.OllamaDenied):
                await infer(b, request, lease)
            assert len(mutations(b)) == before

        asyncio.run(run())


@pytest.mark.parametrize(
    "bad",
    [
        "eof",
        "stream_error",
        "http",
        "terminal_unknown",
        "terminal_wrong_instance",
        "terminal_old",
        "revocation",
        "wrong_context",
    ],
)
def test_inference_uncertainty_holds_request_and_memory_never_emits_tools(
    tmp_path, bad
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            b.wire.packets = [line(tool_packet()), line(done())]
            terminal = activity(b, request)
            if bad == "eof":
                b.wire.packets.pop()
            elif bad == "stream_error":
                b.wire.stream_error = OSError("secret prompt body")
            elif bad == "http":
                b.wire.status = 400
            elif bad == "terminal_unknown":
                terminal = replace(terminal, active_requests=None)
            elif bad == "terminal_wrong_instance":
                terminal = replace(terminal, instance_id="other")
            elif bad == "terminal_old":
                b.now += 1
            elif bad == "revocation":
                b.wire.hook = lambda method, path: (
                    setattr(b, "authorized", False) if path == "/api/chat" else None
                )
            else:
                row = b.wire.rows()[0]
                row["context_length"] = 4096
                # Change only AFTER send, not at pre-send observation.
                b.wire.hook = lambda method, path: (
                    b.wire.queries.update({"/api/ps": (200, {"models": [row]})})
                    if path == "/api/chat"
                    else None
                )
            with pytest.raises(op.OllamaDenied, match="inference_uncertain") as error:
                await infer(b, request, lease, terminal=terminal)
            assert "secret" not in str(error.value)
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600
            assert b.coord.claim_next("shared", now_ms=b.now) is None
            assert b.wire.closed

        asyncio.run(run())


def test_inference_success_releases_only_request_and_preserves_native_tool_ids(
    tmp_path,
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            b.wire.packets = [
                line(tool_packet("id-one")),
                line(tool_packet("id-two")),
                line(done()),
            ]
            events = await infer(b, request, lease)
            assert [event["id"] for event in events[:-1]] == ["id-one", "id-two"]
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.memory.view("shared").committed_bytes == 600
            with pytest.raises(op.OllamaDenied):
                await infer(b, request, lease)
            assert len(mutations(b)) == 2  # One load and one inference.

        asyncio.run(run())


def test_consumer_close_mid_stream_holds_native_lease_and_closes_transport(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            p = op.prepare_chat(b.q.profile, [dict(role="user", content="x")])

            async def never():
                pytest.fail("No terminal proof on closed consumer")

            stream = b.bridge.infer(
                permit(b, request, request_digest=p.digest),
                p,
                activity(b, request),
                lease=lease,
                sample=sample(b.memory),
                produce_terminal=never,
            )
            assert (await anext(stream))["type"] == "reasoning"
            await stream.aclose()
            assert b.wire.closed
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").state == "paused"

        asyncio.run(run())


def test_prepared_tampering_refused_before_any_inference_probe(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            p = op.prepare_chat(b.q.profile, [dict(role="user", content="x")])
            body = op.parse_json(p.body)
            body["options"]["num_ctx"] = 16384
            p = replace(p, body=op.encode(body))
            before = len(b.wire.calls)
            with pytest.raises(op.OllamaDenied):
                await infer(b, request, lease, p=p)
            assert len(b.wire.calls) == before

        asyncio.run(run())


def test_restart_fences_permit_and_does_not_replay_dispatched_load(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.error = OSError("disconnect")
        with pytest.raises(op.OllamaDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
    b.coord = lc.LifecycleCoordinator(tmp_path / "owner")
    b.memory = sm.SharedMemoryAdmission(b.coord)
    b.bridge = ol.OllamaLifecycleBridge(
        b.coord, b.memory, b.wire, clock_ms=lambda: b.now, authorize=lambda p: True
    )
    with b.coord:
        with pytest.raises(op.OllamaDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert len(mutations(b)) == 1
        assert b.memory.view("shared").committed_bytes == 600
        assert b.coord.snapshot("shared").state == "reconciling"


def test_no_cold_tokenizer_or_real_app_registration(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        with pytest.raises(op.OllamaDenied, match="tokenizer_unqualified"):
            asyncio.run(b.bridge.tokenize())
        assert b.wire.calls == []
    from pathlib import Path

    source = Path("app.py").read_text()
    assert (
        "import ollama_lifecycle" not in source
        and "import ollama_protocol" not in source
    )


def test_atomic_memory_receipt_rollback_before_native_send(tmp_path, monkeypatch):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        original = b.bridge._event

        def injected(conn, intent, event, reference):
            if event == "profile":
                raise RuntimeError("injected journal failure after first insert")
            original(conn, intent, event, reference)

        monkeypatch.setattr(b.bridge, "_event", injected)
        with pytest.raises(RuntimeError, match="injected"):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert not mutations(b)
        assert b.memory.view("shared").undispatched_ids == (lease.lease_id,)
        assert (
            b.coord._call(
                lambda conn: conn.execute(
                    "SELECT count(*) FROM journal WHERE event='ollama_sent_load'"
                ).fetchone()[0]
            )
            == 0
        )
        monkeypatch.setattr(b.bridge, "_event", original)
        assert (
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
            == "verify_required"
        )
        assert len(mutations(b)) == 1


def test_durable_send_marker_without_wire_effect_is_not_replay_authority(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        p = permit(b, step)
        observation = asyncio.run(b.bridge.observe("ollama"))
        # Crash window: memory/send marker committed; no HTTP call yet.
        b.bridge._commit_send(
            p,
            activity(b, step),
            observation,
            "fake-payload-hash",
            lease,
            sample(b.memory),
        )
        assert not mutations(b) and b.fleet.models == {}
        with pytest.raises(op.OllamaDenied, match="already_recorded"):
            asyncio.run(
                b.bridge.send(
                    p, activity(b, step), lease=lease, sample=sample(b.memory)
                )
            )
        assert not mutations(b)
        assert b.memory.view("shared").committed_bytes == 600
        assert b.coord.claim_next("shared", now_ms=b.now) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "other"),
        ("instance_id", "other"),
        ("owner_epoch", 999),
        ("generation", 999),
    ],
)
def test_forged_request_scope_is_zero_native_queries_or_sends(tmp_path, field, value):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            before = len(b.wire.calls)
            with pytest.raises(op.OllamaDenied, match="intent_stale"):
                await infer(b, replace(request, **{field: value}), lease)
            assert len(b.wire.calls) == before

        asyncio.run(run())


def test_cancel_before_send_denies_but_cancel_during_stream_requires_terminal_proof(
    tmp_path,
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            b.wire.hook = lambda method, path: (
                b.coord.cancel(request.request_id, now_ms=b.now)
                if path == "/api/chat"
                else None
            )
            await infer(b, request, lease)
            assert b.coord.snapshot("shared").active_requests == 0
            assert (
                b.coord._call(
                    lambda conn: conn.execute(
                        "SELECT status FROM requests WHERE id=?", (request.request_id,)
                    ).fetchone()[0]
                )
                == "cancelled"
            )
            assert b.memory.view("shared").committed_bytes == 600

        asyncio.run(run())


def test_cancelled_native_stream_keeps_slot_and_hold(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            b.wire.stream_error = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await infer(b, request, lease)
            assert b.wire.closed
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").state == "paused"

        asyncio.run(run())


def test_consumer_close_after_terminal_tools_does_not_mark_uncertain(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            b.wire.packets = [line(tool_packet()), line(done())]
            p = op.prepare_chat(
                b.q.profile, [dict(role="user", content="x")], tools=[TOOL]
            )

            async def terminal():
                return activity(b, request)

            stream = b.bridge.infer(
                permit(b, request, request_digest=p.digest),
                p,
                activity(b, request),
                lease=lease,
                sample=sample(b.memory),
                produce_terminal=terminal,
            )
            assert (await anext(stream))["type"] == "tool_call"
            assert b.coord.snapshot("shared").active_requests == 0
            await stream.aclose()
            assert b.coord.snapshot("shared").state == "idle"
            assert b.memory.view("shared").state == "ready"

        asyncio.run(run())


def test_verify_cannot_claim_native_load_from_coordinator_only_fake_completion(
    tmp_path,
):
    b = setup(tmp_path)
    with b.coord:
        step, _ = reserved(b)
        b.fleet.apply(b.coord, step, b.now)
        verify = b.coord.claim_next("shared", now_ms=b.now)
        with pytest.raises(op.OllamaDenied, match="ack_missing"):
            asyncio.run(
                b.bridge.confirm(
                    permit(b, verify), lambda observation: proof(b, observation)
                )
            )
        assert not mutations(b)


@pytest.mark.parametrize("bad", ["reference", "version", "clock", "context", "older"])
def test_confirmation_requires_current_observation_bound_native_proof(tmp_path, bad):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            step, lease = reserved(b)
            await b.bridge.send(
                permit(b, step), activity(b, step), lease=lease, sample=sample(b.memory)
            )
            verify = b.coord.claim_next("shared", now_ms=b.now)

            async def mismatched(observation):
                evidence = await proof(b, observation)
                if bad == "reference":
                    return replace(evidence, reference="replayed-other-observation")
                inventories = list(evidence.inventories)
                index = next(
                    i
                    for i, inv in enumerate(inventories)
                    if inv.runtime.runtime_id == "ollama"
                )
                inventory = inventories[index]
                if bad == "version":
                    inventory = replace(inventory, runtime_version="changed")
                elif bad == "clock":
                    inventory = replace(
                        inventory, stamp=ObservationStamp(b.now / 1000, "other")
                    )
                elif bad == "older":
                    inventory = replace(
                        inventory,
                        stamp=ObservationStamp(0.999, b.coord.observer_clock_id),
                    )
                else:
                    inventory = replace(
                        inventory,
                        instances=(
                            replace(
                                inventory.instances[0], effective_context_tokens=4096
                            ),
                        ),
                    )
                inventories[index] = inventory
                return replace(evidence, inventories=tuple(inventories))

            with pytest.raises(op.OllamaDenied):
                await b.bridge.confirm(permit(b, verify), mismatched)
            assert b.coord.snapshot("shared").resident is None

        asyncio.run(run())


def test_request_fit_approval_is_bound_to_exact_prepared_payload(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            request, lease = await warm(b)
            p = op.prepare_chat(
                b.q.profile, [dict(role="user", content="different request")]
            )

            async def terminal():
                return activity(b, request)

            before = len(b.wire.calls)
            with pytest.raises(op.OllamaDenied, match="fit_scope_mismatch"):
                async for _ in b.bridge.infer(
                    permit(b, request, request_digest="0" * 64),
                    p,
                    activity(b, request),
                    lease=lease,
                    sample=sample(b.memory),
                    produce_terminal=terminal,
                ):
                    pytest.fail("No events without exact request fit approval")
            assert len(b.wire.calls) == before

        asyncio.run(run())
