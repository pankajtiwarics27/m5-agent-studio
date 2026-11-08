"""LC-04 fake assumptions only, not installed oMLX/hardware qualification."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import replace
from urllib.parse import unquote

import pytest

import model_lifecycle_coordinator as lc
import omlx_lifecycle as ox
import runtime_persistence as rp
from local_runtime_lifecycle import CapabilityEvidence, ObservationStamp
from test_model_lifecycle_coordinator import A, B, RUNTIMES
from test_shared_memory_admission import load_step, opened, profile, sample

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="coordinator requires POSIX locking"
)


class Clock:
    now = 1000

    def __call__(self):
        return self.now


class Wire:
    def __init__(self, coord, fleet, clock):
        self.coord, self.fleet, self.clock = coord, fleet, clock
        self.catalog = {A.artifact_id: A}
        self.calls = []
        self.reply = None
        self.hook = None
        self.row_changes = {}
        self.get_error = None

    def status(self):
        rows = []
        for name, target in self.catalog.items():
            loaded = self.fleet.models.get(target.runtime_id) == target
            row = dict(
                id=name,
                loaded=loaded,
                is_loading=False,
                pinned=False,
                is_helper=False,
                distributed=False,
                external_engine=None,
                engine_type="llm",
                actual_size=600 if loaded else 0,
                model_context_length=8192,
            )
            row.update(self.row_changes)
            rows.append(row)
        return dict(
            models=rows,
            model_count=len(rows),
            loaded_count=sum(x["loaded"] is True for x in rows),
        )

    @property
    def posts(self):
        return [x for x in self.calls if x.method == "POST"]

    async def __call__(self, request):
        rp.assert_no_active_write_transaction("fake oMLX wire")
        self.calls.append(request)
        if request.method == "GET":
            if self.get_error:
                raise self.get_error
            assert request.path == "/v1/models/status"
            return 200, self.status()
        with sqlite3.connect(self.coord.database) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE event LIKE 'omlx_send_%'"
                ).fetchone()[0]
                > 0
            )
            if request.path.endswith("/load"):
                assert (
                    conn.execute(
                        "SELECT status FROM memory_reservations WHERE kind='model' AND status!='released'"
                    ).fetchone()[0]
                    == "dispatched"
                )
        if self.hook is not None:
            await self.hook(request)
        if self.reply is not None:
            return self.reply
        name = unquote(request.path.split("/")[3])
        target = self.catalog[name]
        if request.path.endswith("/load"):
            assert not self.fleet.models
            self.fleet.models[target.runtime_id] = target
        else:
            self.fleet.models.pop(target.runtime_id, None)
        return 200, {"status": "ok", "model_id": name}


def permit(coord, step, clock):
    target = step.source if step.kind in ("drain", "unload") else step.target
    caps = tuple(
        CapabilityEvidence(
            name,
            "supported",
            "installed",
            target.runtime_id,
            "fake-v1",
            target.profile_fingerprint,
            ("fake-assumption-not-native-evidence",),
        )
        for name in ox._REQUIRED + ("blocking_load",)
    )
    q = ox.OmlxQualification(
        RUNTIMES[0],
        target,
        "fake-v1",
        ox.OMLX_SOURCE_REVISION,
        8192,
        caps,
        ObservationStamp(clock.now / 1000, coord.observer_clock_id),
        "fake-qualified-profile",
    )
    return ox.OmlxPermit(step, q, 100000, "fake-server-authorized-action", True)


def activity(coord, p, clock, **changes):
    return replace(
        ox.OmlxActivity(
            p.step,
            None if p.step.kind == "load" else p.step.source_instance_id,
            0,
            ObservationStamp(clock.now / 1000, coord.observer_clock_id),
            "fake-native-quiescence",
            True,
            True,
        ),
        **changes,
    )


def setup(tmp_path):
    coord, memory, fleet = opened(tmp_path)
    clock = Clock()
    wire = Wire(coord, fleet, clock)
    bridge = ox.OmlxLifecycleBridge(
        coord, memory, wire, clock, authorize=lambda p: p.authorized
    )
    return coord, memory, fleet, clock, wire, bridge


async def start_loaded(coord, memory, fleet, clock, wire, bridge):
    step = load_step(coord)
    p = permit(coord, step, clock)
    bound = profile()
    lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
    assert (
        await bridge.send(
            p,
            activity(coord, p, clock),
            lease=lease,
            sample=sample(memory),
            profile=bound,
        )
        == "verify"
    )
    verify = coord.claim_next("shared", now_ms=clock.now)

    async def produce(observation):
        return fleet.evidence(coord, clock.now, reference=observation.reference)

    assert await bridge.confirm(permit(coord, verify, clock), produce)
    request = coord.claim_next("shared", now_ms=clock.now)
    assert coord.finish_request(
        request, reference="fake-request-finished", now_ms=clock.now
    )
    return lease


async def switch_unload(env):
    coord, memory, fleet, clock, wire, bridge = env
    lease = await start_loaded(*env)
    coord.enqueue("switch-b", "shared", B, deadline_ms=100000, now_ms=clock.now)
    drain = coord.claim_next("shared", now_ms=clock.now)
    p = permit(coord, drain, clock)
    assert bridge.drain(p, activity(coord, p, clock))
    unload = coord.claim_next("shared", now_ms=clock.now)
    return lease, permit(coord, unload, clock)


def test_load_requires_memory_and_current_native_verification_before_request_lease(
    tmp_path,
):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        lease = asyncio.run(start_loaded(*env))
        assert memory.view("shared").committed_bytes == 600
        assert coord.snapshot("shared").resident == A
        assert wire.posts[0].path == "/v1/models/model-a/load"
        assert len(wire.posts) == 1
        assert not hasattr(ox.OmlxWireModel, "instance_id")
        assert not hasattr(ox.OmlxWireModel, "effective_context_tokens")
        assert lease.peak_bytes == 600


@pytest.mark.parametrize("http_status,body_status", [(200, "ok"), (202, "unloading")])
def test_unload_ack_never_releases_domain_or_allows_next_load(
    tmp_path, http_status, body_status
):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        _, p = asyncio.run(switch_unload(env))
        wire.reply = (http_status, {"status": body_status, "model_id": A.artifact_id})
        assert asyncio.run(bridge.send(p, activity(coord, p, clock))) == "pending"
        assert coord.claim_next("shared", now_ms=clock.now) is None
        assert coord.snapshot("shared").resident == A
        assert memory.view("shared").committed_bytes == 600
        with pytest.raises(ox.OmlxError):
            asyncio.run(bridge.send(p, activity(coord, p, clock)))
        assert len(wire.posts) == 2  # Initial load plus exactly one unload.
        assert wire.posts[-1].path.endswith("/unload")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500, 503])
def test_failed_unload_is_not_suppressed_and_causes_zero_target_loads(tmp_path, status):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        _, p = asyncio.run(switch_unload(env))
        wire.reply = (status, {"detail": "sensitive-provider-body"})
        with pytest.raises(ox.OmlxError, match="^native_operation_uncertain$"):
            asyncio.run(bridge.send(p, activity(coord, p, clock)))
        assert coord.snapshot("shared").state == "reconciling"
        assert coord.claim_next("shared", now_ms=clock.now) is None
        assert len([x for x in wire.posts if x.path.endswith("/load")]) == 1
        assert memory.view("shared").committed_bytes == 600


def test_empty_poll_is_insufficient_until_qualified_all_runtime_release_proof(tmp_path):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        lease, p = asyncio.run(switch_unload(env))
        assert asyncio.run(bridge.send(p, activity(coord, p, clock))) == "pending"

        async def not_terminal(obs):
            return fleet.evidence(
                coord, clock.now, reference=obs.reference, operations_terminal=False
            )

        with pytest.raises(lc.CoordinatorError, match="release_unverified"):
            asyncio.run(bridge.confirm(p, not_terminal))
        assert coord.claim_next("shared", now_ms=clock.now) is None

        async def qualified(obs):
            return fleet.evidence(coord, clock.now, reference=obs.reference)

        assert asyncio.run(bridge.confirm(p, qualified))
        next_step = coord.claim_next("shared", now_ms=clock.now)
        assert next_step.kind == "load" and next_step.target == B
        # Physical lifecycle proof still did not silently settle memory.
        assert memory.view("shared").leases == (lease,)


@pytest.mark.parametrize("active", [None, 1, 2])
def test_live_or_unknown_native_streams_do_not_drain_or_force_abort(tmp_path, active):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        asyncio.run(start_loaded(*env))
        coord.enqueue("switch-b", "shared", B, deadline_ms=100000, now_ms=clock.now)
        step = coord.claim_next("shared", now_ms=clock.now)
        p = permit(coord, step, clock)
        proof = activity(coord, p, clock, active_streams=active)
        if active is None:
            with pytest.raises(ox.OmlxError, match="activity_unverified"):
                bridge.drain(p, proof)
        else:
            assert bridge.drain(p, proof) is False
        assert coord.snapshot("shared").operation_stage == "drain"
        assert len(wire.posts) == 1
        assert bridge.drain(p, activity(coord, p, clock))


@pytest.mark.parametrize(
    "changes",
    [
        {"pinned": True},
        {"is_helper": True},
        {"distributed": True},
        {"external_engine": "other-process"},
        {"is_loading": True},
    ],
)
def test_pinned_loading_helpers_and_external_engines_block_before_unload(
    tmp_path, changes
):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        _, p = asyncio.run(switch_unload(env))
        wire.row_changes = changes
        with pytest.raises(ox.OmlxError):
            asyncio.run(bridge.send(p, activity(coord, p, clock)))
        assert len(wire.posts) == 1
        assert memory.view("shared").committed_bytes == 600


@pytest.mark.parametrize(
    "status,payload",
    [
        (400, {}),
        (409, {}),
        (507, {}),
        (200, {}),
        (200, {"status": "loaded", "model_id": "model-a"}),
        (200, {"status": "ok", "model_id": "wrong"}),
        (202, {"status": "unloading", "model_id": "model-a"}),
    ],
)
def test_bad_load_reply_keeps_memory_and_never_issues_inference(
    tmp_path, status, payload
):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        wire.reply = (status, payload)
        with pytest.raises(ox.OmlxError, match="native_operation_uncertain"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
        assert (
            memory.view("shared").state == "paused"
            and memory.view("shared").committed_bytes == 600
        )
        assert coord.claim_next("shared", now_ms=clock.now) is None
        assert len(wire.posts) == 1


@pytest.mark.parametrize(
    "change",
    [
        "unauthorized",
        "expired",
        "offline",
        "unsupported",
        "revision",
        "wrong-version",
        "remote",
        "stale",
    ],
)
def test_unqualified_scopes_fail_without_any_transport(tmp_path, change):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        q = p.qualification
        if change == "unauthorized":
            p = replace(p, authorized=False)
        if change == "expired":
            p = replace(p, expires_ms=1000)
        if change == "offline":
            q = replace(
                q,
                capabilities=tuple(replace(c, basis="offline") for c in q.capabilities),
            )
        if change == "unsupported":
            q = replace(
                q,
                capabilities=tuple(replace(c, state="unknown") for c in q.capabilities),
            )
        if change == "revision":
            q = replace(q, source_revision="unreviewed")
        if change == "wrong-version":
            q = replace(q, runtime_version="another-version")
        if change == "remote":
            q = replace(q, runtime=replace(q.runtime, location="remote"))
        if change == "stale":
            q = replace(q, stamp=ObservationStamp(0, "another-clock"))
        p = replace(p, qualification=q)
        with pytest.raises(ox.OmlxError):
            asyncio.run(bridge.send(p, activity(coord, p, clock)))
        assert not wire.calls


def test_memory_profile_and_exact_reservation_are_checked_before_wire(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        with pytest.raises(ox.OmlxError, match="model_profile_mismatch"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=profile(amount=500),
                )
            )
        assert not wire.calls
        with pytest.raises(ox.OmlxError, match="memory_reservation_required"):
            asyncio.run(bridge.send(p, activity(coord, p, clock), profile=bound))
        assert not wire.calls


def test_transport_and_proof_failures_are_redacted(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        wire.get_error = OSError("secret endpoint credential body")
        with pytest.raises(ox.OmlxError, match="^transport_unavailable$"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
        assert not wire.posts
        wire.get_error = None
        asyncio.run(
            bridge.send(
                p,
                activity(coord, p, clock),
                lease=lease,
                sample=sample(memory),
                profile=bound,
            )
        )
        verify = coord.claim_next("shared", now_ms=clock.now)

        async def broken(obs):
            raise OSError("secret upstream body")

        with pytest.raises(ox.OmlxError, match="^proof_unavailable$"):
            asyncio.run(bridge.confirm(permit(coord, verify, clock), broken))
        assert coord.claim_next("shared", now_ms=clock.now) is None


def test_context_must_be_verified_not_inferred_from_architectural_wire_metadata(
    tmp_path,
):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        asyncio.run(
            bridge.send(
                p,
                activity(coord, p, clock),
                lease=lease,
                sample=sample(memory),
                profile=bound,
            )
        )
        verify = coord.claim_next("shared", now_ms=clock.now)

        async def changed(obs):
            ev = fleet.evidence(coord, clock.now, reference=obs.reference)
            inventories = tuple(
                replace(
                    i,
                    instances=tuple(
                        replace(x, effective_context_tokens=4096) for x in i.instances
                    ),
                )
                for i in ev.inventories
            )
            return replace(ev, inventories=inventories)

        with pytest.raises(ox.OmlxError, match="context_unverified"):
            asyncio.run(bridge.confirm(permit(coord, verify, clock), changed))
        assert coord.snapshot("shared").resident is None
        assert coord.claim_next("shared", now_ms=clock.now) is None


def test_cancellation_and_late_native_effect_keep_holds_and_prevent_replay(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)

        async def run():
            entered = asyncio.Event()

            async def hang(request):
                entered.set()
                await asyncio.Event().wait()

            wire.hook = hang
            task = asyncio.create_task(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
            await entered.wait()
            with pytest.raises(ox.OmlxError):
                await bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        fleet.models[A.runtime_id] = (
            A  # Physical server may finish AFTER client cancellation.
        )
        assert (
            memory.view("shared").committed_bytes == 600
            and memory.view("shared").state == "paused"
        )
        assert coord.snapshot("shared").state == "reconciling"
        assert coord.claim_next("shared", now_ms=clock.now) is None
        assert len(wire.posts) == 1


def test_blocking_load_can_return_after_observation_age_but_still_requires_fresh_verify(
    tmp_path,
):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)

        async def slow(request):
            clock.now = 10000

        wire.hook = slow
        assert (
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
            == "verify"
        )
        assert coord.snapshot("shared").resident is None
        assert coord.snapshot("shared").operation_stage == "verify"
        assert coord.snapshot("shared").active_requests == 0


def test_timeout_and_late_reply_never_promote_target_to_ready(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)

        async def late(request):
            clock.now = 62000

        wire.hook = late
        with pytest.raises(ox.OmlxError, match="native_operation_uncertain"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
        assert coord.snapshot("shared").state == "reconciling"
        assert memory.view("shared").committed_bytes == 600


def test_tokenizer_has_no_cold_load_or_transport_bypass(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord, pytest.raises(ox.OmlxError, match="tokenizer_admission_unavailable"):
        asyncio.run(bridge.tokenize())
    assert not wire.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("loaded", "false"),
        ("is_loading", None),
        ("pinned", 0),
        ("is_helper", "false"),
        ("distributed", None),
        ("actual_size", True),
        ("model_context_length", 0),
        ("source_model_id", "another-model"),
    ],
)
def test_malformed_ambiguous_alias_status_never_defaults_to_safe(
    tmp_path, field, value
):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        raw = wire.status()
        raw["models"][0][field] = value
        with pytest.raises((ox.OmlxError, lc.CoordinatorError)):
            ox.parse_omlx_status(raw)


@pytest.mark.parametrize(
    "fault", ["missing-flag", "missing-count", "duplicate", "wrong-count"]
)
def test_incomplete_catalog_and_duplicate_instances_are_not_hidden(tmp_path, fault):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        raw = wire.status()
        if fault == "missing-flag":
            del raw["models"][0]["is_helper"]
        if fault == "missing-count":
            del raw["loaded_count"]
        if fault == "duplicate":
            raw["models"].append(dict(raw["models"][0]))
            raw["model_count"] = 2
        if fault == "wrong-count":
            raw["model_count"] = 99
        with pytest.raises(ox.OmlxError):
            ox.parse_omlx_status(raw)


def test_real_async_deadline_marks_ambiguous_load_without_releasing_memory(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        bridge = ox.OmlxLifecycleBridge(
            coord,
            memory,
            wire,
            clock,
            request_timeout_s=0.1,
            authorize=lambda p: p.authorized,
        )
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)

        async def hang(request):
            await asyncio.Event().wait()

        wire.hook = hang
        with pytest.raises(ox.OmlxError, match="native_operation_uncertain"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
        assert len(wire.posts) == 1
        assert memory.view("shared").state == "paused"
        assert memory.view("shared").committed_bytes == 600


def test_atomic_storage_failure_occurs_before_native_post_and_rolls_back_both_claims(
    tmp_path, monkeypatch
):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        original = coord._event

        def fail(conn, domain, event, *args, **kwargs):
            if event == "omlx_send_load":
                raise sqlite3.OperationalError("private-storage-detail")
            return original(conn, domain, event, *args, **kwargs)

        monkeypatch.setattr(coord, "_event", fail)
        with pytest.raises(lc.CoordinatorError, match="^journal_unavailable$"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
        assert not wire.posts
        with sqlite3.connect(coord.database) as conn:
            assert (
                conn.execute("SELECT status FROM memory_reservations").fetchone()[0]
                == "reserved"
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE event='omlx_send_load'"
                ).fetchone()[0]
                == 0
            )


def test_process_crash_at_native_send_leaves_durable_hold_and_new_owner_never_replays(
    tmp_path,
):
    import subprocess
    import sys
    from shared_memory_admission import SharedMemoryAdmission, _model_step

    script = """
import sys,os,asyncio
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_omlx_lifecycle_bridge import setup,permit,activity
from test_shared_memory_admission import load_step,profile,sample
coord,memory,fleet,clock,wire,bridge=setup(Path(sys.argv[1]))
step=load_step(coord);p=permit(coord,step,clock);bound=profile()
lease=memory.reserve_model(step,bound,sample(memory),now_ms=clock.now)
async def crash(request):os._exit(0)
wire.hook=crash
asyncio.run(bridge.send(p,activity(coord,p,clock),lease=lease,sample=sample(memory),profile=bound))
raise AssertionError('crash hook not reached')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    with lc.LifecycleCoordinator(tmp_path / "owner") as new:
        memory = SharedMemoryAdmission(new)
        view = memory.view("shared")
        assert view.state == "reconciling" and view.committed_bytes == 600
        old_step = _model_step(view.leases[0].binding)
        clock = Clock()

        async def never(request):
            raise AssertionError("native operation replayed")

        bridge = ox.OmlxLifecycleBridge(
            new, memory, never, clock, authorize=lambda p: p.authorized
        )
        p = permit(new, old_step, clock)
        with pytest.raises(ox.OmlxError, match="intent_stale"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(new, p, clock),
                    lease=view.leases[0],
                    sample=sample(memory),
                    profile=profile(),
                )
            )


def test_activity_proof_for_another_native_instance_does_not_authorize_unload(tmp_path):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        _, p = asyncio.run(switch_unload(env))
        with pytest.raises(ox.OmlxError, match="activity_unverified"):
            asyncio.run(
                bridge.send(p, activity(coord, p, clock, instance_id="someone-else"))
            )
        assert len(wire.posts) == 1


def test_observation_receipt_and_runtime_version_must_match_completion_proof(tmp_path):
    env = setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    with coord:
        _, p = asyncio.run(switch_unload(env))
        asyncio.run(bridge.send(p, activity(coord, p, clock)))

        async def wrong_receipt(obs):
            return fleet.evidence(coord, clock.now, reference="different-receipt")

        with pytest.raises(ox.OmlxError, match="proof_scope_mismatch"):
            asyncio.run(bridge.confirm(p, wrong_receipt))

        async def wrong_version(obs):
            ev = fleet.evidence(coord, clock.now, reference=obs.reference)
            return replace(
                ev,
                inventories=tuple(
                    replace(x, runtime_version="different") for x in ev.inventories
                ),
            )

        with pytest.raises(ox.OmlxError, match="proof_scope_mismatch"):
            asyncio.run(bridge.confirm(p, wrong_version))
        assert coord.claim_next("shared", now_ms=clock.now) is None


def test_model_identifier_is_encoded_as_one_relative_route_not_a_url(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    target = lc.ModelTarget("omlx", "model name?x#y%z", "digest-x", "profile-x")
    with coord:
        wire.catalog = {target.artifact_id: target}
        step = load_step(coord, target)
        p = permit(coord, step, clock)
        bound = profile(target)
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        assert (
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
            == "verify"
        )
        assert wire.posts[0].path == "/v1/models/model%20name%3Fx%23y%25z/load"
        assert all(not x.path.startswith("http") for x in wire.calls)


@pytest.mark.parametrize(
    "timeout", [True, 0, -1, float("inf"), float("nan"), 121, 2**10000]
)
def test_invalid_timeouts_do_not_start_transport(tmp_path, timeout):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord, pytest.raises(ox.OmlxError, match="invalid_timeout"):
        ox.OmlxLifecycleBridge(coord, memory, wire, clock, request_timeout_s=timeout)
    assert not wire.calls


def test_explicit_live_authorizer_is_required_even_for_a_claimed_installed_permit(
    tmp_path,
):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        bridge = ox.OmlxLifecycleBridge(coord, memory, wire, clock)
        p = permit(coord, load_step(coord), clock)
        with pytest.raises(ox.OmlxError, match="authorization_unavailable"):
            asyncio.run(bridge.observe(p))
        assert not wire.calls


def test_revocation_during_preflight_blocks_post_and_keeps_reserved_memory(tmp_path):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        approved = [True]

        async def revoke_on_read(request):
            reply = await wire(request)
            approved[0] = False
            return reply

        bridge = ox.OmlxLifecycleBridge(
            coord, memory, revoke_on_read, clock, authorize=lambda p: approved[0]
        )
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        with pytest.raises(ox.OmlxError, match="authorization_unavailable"):
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
        assert not wire.posts and memory.view("shared").committed_bytes == 600
        with sqlite3.connect(coord.database) as conn:
            assert (
                conn.execute("SELECT status FROM memory_reservations").fetchone()[0]
                == "reserved"
            )


def test_native_status_is_bounded_diagnostic_not_release_authority_or_body_echo(
    tmp_path,
):
    coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
    with coord:
        step = load_step(coord)
        p = permit(coord, step, clock)
        bound = profile()
        lease = memory.reserve_model(step, bound, sample(memory), now_ms=clock.now)
        wire.reply = (401, {"detail": "private auth body"})
        with pytest.raises(ox.OmlxError) as error:
            asyncio.run(
                bridge.send(
                    p,
                    activity(coord, p, clock),
                    lease=lease,
                    sample=sample(memory),
                    profile=bound,
                )
            )
        assert (
            error.value.status_code == 401
            and str(error.value) == "native_operation_uncertain"
        )
        with sqlite3.connect(coord.database) as conn:
            refs = [
                x[0]
                for x in conn.execute(
                    "SELECT reference FROM journal WHERE event='uncertain'"
                )
            ]
        assert "omlx_effect_uncertain_http_401" in refs
        assert all("private" not in (x or "") for x in refs)
        assert memory.view("shared").committed_bytes == 600
