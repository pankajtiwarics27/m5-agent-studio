"""LC-06 fake assumptions only; no LM Studio/server/SDK/network is used."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

import lmstudio_lifecycle as lm
import lmstudio_protocol as p
import model_lifecycle_coordinator as lc
import runtime_persistence as rp
import shared_memory_admission as sm
from local_runtime_lifecycle import CapabilityEvidence, ObservationStamp
from test_model_lifecycle_coordinator import A
from test_shared_memory_admission import opened, sample
from test_lmstudio_native_protocol import (
    CONFIG,
    INSTANCE,
    TARGET,
    final,
    model,
    native_profile,
    packet,
    request,
    sse,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner locking")


class Wire:
    def __init__(self, bundle):
        self.b = bundle
        self.calls = []
        self.status = 200
        self.error = None
        self.ack = {}
        self.list_override = None
        self.catalog_changes = {}
        self.extra_instances = []
        self.extra_models = []
        self.hook = None
        self.apply = True
        self.instance = INSTANCE
        self.closed = False
        self.stream_error = None
        self.chunks = [sse(packet()), final()]

    def rows(self):
        instances = (
            [dict(id=self.instance, config=CONFIG.copy())]
            if "lmstudio" in self.b.fleet.models
            else []
        )
        return [
            model(
                loaded_instances=instances + self.extra_instances,
                **self.catalog_changes,
            )
        ] + self.extra_models

    def before(self, method, path, body):
        rp.assert_no_active_write_transaction("fake LM Studio wire")
        self.calls.append((method, path, p.decode(body) if body else None))
        if self.hook:
            self.hook(method, path)
        if method == "POST":
            assert self.b.coord._call(
                lambda conn: conn.execute(
                    "SELECT 1 FROM journal WHERE event LIKE 'lmstudio_sent_%'"
                ).fetchone()
            )
            assert self.b.memory.view("shared").committed_bytes == 600

    async def request(self, method, path, body, timeout_s):
        self.before(method, path, body)
        if path == "/api/v1/models":
            return 200, self.list_override if self.list_override is not None else {
                "models": self.rows()
            }
        assert method == "POST"
        if path == "/api/v1/models/load":
            if self.apply:
                assert not self.b.fleet.models
                self.b.fleet.models["lmstudio"] = TARGET
            result = dict(
                type="llm",
                status="loaded",
                instance_id=self.instance,
                load_config={k: v for k, v in CONFIG.items() if k != "parallel"},
            )
        else:
            assert path == "/api/v1/models/unload" and p.decode(body) == {
                "instance_id": self.instance
            }
            if self.apply:
                self.b.fleet.models.pop("lmstudio", None)
            result = dict(instance_id=self.instance)
        result.update(self.ack)
        if self.error:
            raise self.error
        return self.status, result

    @asynccontextmanager
    async def stream(self, path, body, timeout_s):
        self.before("POST", path, body)
        self.closed = False

        async def chunks():
            for chunk in self.chunks:
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
        coord=coord,
        memory=memory,
        fleet=fleet,
        now=1000,
        authorized=True,
        env_changes={},
    )
    b.q = lm.LmQualification(
        "lmstudio",
        "0.4.0",
        "fake-process-identity",
        p.SOURCE_PIN,
        native_profile(),
        50000,
        "fake-qualified-profile",
        tuple(
            CapabilityEvidence(
                name,
                "supported",
                "installed",
                "lmstudio",
                "0.4.0",
                TARGET.profile_fingerprint,
                ("fake-only-not-installed",),
            )
            for name in lm.CAPABILITIES
        ),
    )
    b.wire = Wire(b)

    async def environment():
        rp.assert_no_active_write_transaction("fake environment producer")
        return replace(
            lm.LmEnvironment(
                "lmstudio",
                "0.4.0",
                "fake-process-identity",
                ObservationStamp(b.now / 1000, coord.observer_clock_id),
                "fake-current-settings-digest-authority",
                tuple(
                    (
                        row["key"],
                        TARGET.artifact_digest
                        if row["key"] == TARGET.artifact_id
                        else "extra-digest",
                    )
                    for row in b.wire.rows()
                ),
                False,
                False,
                True,
                True,
                authentication_required=True,
            ),
            **b.env_changes,
        )

    b.environment = environment
    b.bridge = lm.LmStudioLifecycleBridge(
        coord,
        memory,
        b.wire,
        clock_ms=lambda: b.now,
        environment=environment,
        authorize=(lambda grant: b.authorized) if authorize else None,
        admit_request=lambda *args: (
            True
        ),  # explicit fake fit assumption; not a real counter
    )
    return b


def permit(b, intent, **changes):
    return replace(
        lm.LmPermit(
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
        lm.LmActivity(
            intent,
            TARGET,
            ObservationStamp(b.now / 1000, b.coord.observer_clock_id),
            "fake-exact-stream-ownership-proof",
            instance,
            0,
            True,
            True,
        ),
        **changes,
    )


def reserved(b):
    b.coord.enqueue("first", "shared", TARGET, deadline_ms=100000, now_ms=b.now)
    step = b.coord.claim_next("shared", now_ms=b.now)
    assert step.kind == "load"
    lease = b.memory.reserve_model(
        step, b.q.profile.memory, sample(b.memory, now=b.now), now_ms=b.now
    )
    return step, lease


async def proof(b, observation):
    evidence = b.fleet.evidence(b.coord, b.now)
    inventories = tuple(
        replace(
            inv,
            runtime_version="0.4.0",
            instances=tuple(
                replace(x, instance_id=b.wire.instance) for x in inv.instances
            ),
        )
        if inv.runtime.runtime_id == "lmstudio"
        else inv
        for inv in evidence.inventories
    )
    return replace(evidence, reference=observation.reference, inventories=inventories)


async def warm(b):
    step, lease = reserved(b)
    assert (
        await b.bridge.send(
            permit(b, step),
            activity(b, step),
            lease=lease,
            sample=sample(b.memory, now=b.now),
        )
        == "verify_required"
    )
    verify = b.coord.claim_next("shared", now_ms=b.now)
    assert verify.kind == "verify"
    assert await b.bridge.confirm(
        permit(b, verify), lambda observed: proof(b, observed)
    )
    request_lease = b.coord.claim_next("shared", now_ms=b.now)
    assert isinstance(request_lease, lc.RequestLease)
    return request_lease, lease


async def infer(
    b, intent, lease, *, terminal_changes=None, prepared=None, permit_changes=None
):
    prepared = prepared or p.prepare_inference(
        intent.instance_id, b.q.profile, request(model=intent.instance_id)
    )

    async def terminal():
        assert b.wire.closed
        b.now += 1
        return activity(
            b,
            intent,
            executed_request_digest=prepared.digest,
            **(terminal_changes or {}),
        )

    return [
        raw
        async for raw in b.bridge.infer(
            permit(b, intent, request_digest=prepared.digest, **(permit_changes or {})),
            prepared,
            activity(b, intent),
            lease=lease,
            sample=sample(b.memory, now=b.now),
            produce_terminal=terminal,
        )
    ]


def mutations(b):
    return [call for call in b.wire.calls if call[0] == "POST"]


def test_positive_native_load_verify_exact_instance_inference_warm_hold(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            intent, lease = await warm(b)
            assert intent.instance_id == INSTANCE
            result = await infer(b, intent, lease)
            assert [p.decode(raw) for raw in result] == [
                packet(),
                packet(choices=[dict(index=0, delta={}, finish_reason="stop")]),
            ]
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.coord.snapshot("shared").resident_instance_id == INSTANCE
            assert b.memory.view("shared").committed_bytes == 600
            posts = mutations(b)
            assert (
                posts[0][1] == "/api/v1/models/load"
                and posts[0][2]["echo_load_config"] is True
            )
            assert (
                posts[1][1] == "/v1/chat/completions"
                and posts[1][2]["model"] == INSTANCE
            )
            assert "context_length" not in posts[1][2]

        asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        "no_authorizer",
        "revoked",
        "expired",
        "source_basis",
        "unknown_cap",
        "wrong_version",
        "old_version",
        "unknown_version",
        "wrong_pin",
        "wrong_digest",
        "busy",
        "unknown_activity",
        "wrong_clock",
        "late_possible",
        "unowned",
        "wrong_epoch",
        "wrong_generation",
        "no_memory",
    ],
)
def test_default_unknown_authority_and_scope_deny_without_mutation(tmp_path, change):
    b = setup(tmp_path, authorize=change != "no_authorizer")
    with b.coord:
        step, lease = reserved(b)
        grant, live = permit(b, step), activity(b, step)
        if change == "revoked":
            b.authorized = False
        elif change == "expired":
            grant = replace(grant, expires_ms=b.now)
        elif change in ("source_basis", "unknown_cap"):
            cap = replace(
                b.q.capabilities[0],
                **(
                    {"basis": "source"}
                    if change == "source_basis"
                    else {"state": "unknown"}
                ),
            )
            grant = replace(
                grant,
                qualification=replace(b.q, capabilities=(cap,) + b.q.capabilities[1:]),
            )
        elif change == "wrong_version":
            b.env_changes["version"] = "0.4.1"
        elif change in ("old_version", "unknown_version"):
            grant = replace(
                grant,
                qualification=replace(
                    b.q, version="0.3.9" if change == "old_version" else "unknown"
                ),
            )
        elif change == "wrong_pin":
            grant = replace(grant, qualification=replace(b.q, source_pin="different"))
        elif change == "wrong_digest":
            b.env_changes["artifact_digests"] = ((TARGET.artifact_id, "wrong"),)
        elif change == "busy":
            live = replace(live, active_requests=1)
        elif change == "unknown_activity":
            live = replace(live, active_requests=None)
        elif change == "wrong_clock":
            live = replace(live, stamp=ObservationStamp(1.0, "other"))
        elif change == "late_possible":
            live = replace(live, late_effects_excluded=False)
        elif change == "unowned":
            live = replace(live, ownership_verified=False)
        elif change in ("wrong_epoch", "wrong_generation"):
            grant = replace(
                grant,
                intent=replace(
                    step,
                    **{"owner_epoch" if change == "wrong_epoch" else "generation": 999},
                ),
            )
        else:
            lease = None
        with pytest.raises(p.LmStudioDenied):
            asyncio.run(
                b.bridge.send(
                    grant, live, lease=lease, sample=sample(b.memory, now=b.now)
                )
            )
        assert not mutations(b) and not b.fleet.models


@pytest.mark.parametrize(
    "changes",
    [
        dict(jit_enabled=True),
        dict(jit_enabled=None),
        dict(auto_evict_enabled=True),
        dict(auto_evict_enabled=None),
        dict(expiry_excluded=False),
        dict(exclusive_control=False),
        dict(server_identity="restarted-server"),
        dict(artifact_digests=()),
    ],
)
def test_automatic_loading_eviction_expiry_or_unknown_server_cannot_supply_exclusivity(
    tmp_path, changes
):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.env_changes.update(changes)
        with pytest.raises(p.LmStudioDenied):
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


@pytest.mark.parametrize(
    "case",
    ["manual_same", "manual_other", "helper", "wrong_format", "wrong_type", "variant"],
)
def test_manual_multiple_helper_and_unapproved_artifact_state_blocks_load(
    tmp_path, case
):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        if case == "manual_same":
            b.wire.extra_instances.append(dict(id="manual-one", config=CONFIG))
        elif case in ("manual_other", "helper"):
            b.wire.extra_models.append(
                model(
                    key="other",
                    type="embedding" if case == "helper" else "llm",
                    loaded_instances=[dict(id="external", config=CONFIG)],
                )
            )
        elif case == "wrong_format":
            b.wire.catalog_changes["format"] = "mlx"
        elif case == "wrong_type":
            b.wire.catalog_changes["type"] = "embedding"
        else:
            b.wire.catalog_changes.update(variants=["v1"], selected_variant="v1")
        with pytest.raises(p.LmStudioDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert not mutations(b)


@pytest.mark.parametrize("status", [202, 204, 301, 400, 401, 403, 404, 409, 500])
def test_error_auth_redirect_unsupported_api_ack_never_falls_back_or_retries(
    tmp_path, status
):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.status = status
        with pytest.raises(p.LmStudioDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert len(mutations(b)) == 1
        assert b.coord.snapshot("shared").state == "reconciling"
        assert (
            b.memory.view("shared").committed_bytes == 600
            and b.memory.view("shared").state == "paused"
        )
        assert b.coord.claim_next("shared", now_ms=b.now) is None
        with pytest.raises(p.LmStudioDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert len(mutations(b)) == 1
        assert all("/api/v0" not in call[1] for call in b.wire.calls)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("secret native body"),
        OSError("secret native body"),
        asyncio.CancelledError(),
    ],
)
def test_late_load_timeout_or_cancel_keeps_hold_even_if_list_empty(tmp_path, error):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.apply = False
        b.wire.error = error
        with pytest.raises((p.LmStudioDenied, asyncio.CancelledError)) as caught:
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert "secret" not in str(caught.value)
        assert not b.fleet.models
        # A delayed native load is still possible after an empty list.
        b.fleet.models["lmstudio"] = TARGET
        assert b.coord.claim_next("shared", now_ms=b.now) is None
        assert b.memory.view("shared").committed_bytes == 600
        assert len(mutations(b)) == 1


@pytest.mark.parametrize(
    "ack",
    [
        dict(status="loading"),
        dict(type="embedding"),
        dict(instance_id=""),
        dict(load_config=None),
        dict(load_config={"context_length": 4096}),
        dict(load_config={"context_length": 8192}),
        dict(load_config=dict(CONFIG, parallel=4)),
    ],
)
def test_load_requires_final_echo_no_ignored_or_changed_options(tmp_path, ack):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.ack.update(ack)
        with pytest.raises(p.LmStudioDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert b.coord.snapshot("shared").state == "reconciling"
        assert b.coord.snapshot("shared").operation_stage == "load"
        assert b.memory.view("shared").committed_bytes == 600


@pytest.mark.parametrize(
    "case", ["context", "instance", "parallel", "extra_instance", "format", "variant"]
)
def test_verification_uses_allocated_context_config_and_exact_returned_instance(
    tmp_path, case
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            step, lease = reserved(b)
            await b.bridge.send(
                permit(b, step), activity(b, step), lease=lease, sample=sample(b.memory)
            )
            verify = b.coord.claim_next("shared", now_ms=b.now)
            rows = b.wire.rows()
            if case == "context":
                rows[0]["loaded_instances"][0]["config"]["context_length"] = 4096
            elif case == "parallel":
                rows[0]["loaded_instances"][0]["config"]["parallel"] = 8
            elif case == "instance":
                rows[0]["loaded_instances"][0]["id"] = "other-instance"
            elif case == "extra_instance":
                rows[0]["loaded_instances"].append(dict(id="manual-two", config=CONFIG))
            elif case == "format":
                rows[0]["format"] = "mlx"
            else:
                rows[0].update(variants=["changed"], selected_variant="changed")
            b.wire.list_override = {"models": rows}
            with pytest.raises(p.LmStudioDenied):
                await b.bridge.confirm(
                    permit(b, verify), lambda observed: proof(b, observed)
                )
            assert b.coord.snapshot("shared").resident is None
            assert b.memory.view("shared").committed_bytes == 600

        asyncio.run(run())


async def unloading(b):
    intent, lease = await warm(b)
    await infer(b, intent, lease)
    b.coord.enqueue("switch", "shared", A, deadline_ms=100000, now_ms=b.now)
    drain = b.coord.claim_next("shared", now_ms=b.now)
    assert drain.kind == "drain" and b.bridge.drain(
        permit(b, drain), activity(b, drain)
    )
    unload = b.coord.claim_next("shared", now_ms=b.now)
    assert unload.kind == "unload"
    return unload, lease


def test_unload_exact_instance_ack_is_pending_until_all_runtime_physical_proof_and_memory_settlement(
    tmp_path,
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            unload, lease = await unloading(b)
            assert (
                await b.bridge.send(permit(b, unload), activity(b, unload))
                == "pending_release_proof"
            )
            assert mutations(b)[-1][2] == {"instance_id": INSTANCE}
            assert b.coord.claim_next("shared", now_ms=b.now) is None

            async def bad(observed):
                return replace(await proof(b, observed), operations_terminal=False)

            with pytest.raises(lc.CoordinatorError):
                await b.bridge.confirm(permit(b, unload), bad)
            assert b.memory.view("shared").committed_bytes == 600
            assert await b.bridge.confirm(
                permit(b, unload), lambda observed: proof(b, observed)
            )
            assert b.memory.view("shared").committed_bytes == 600
            b.memory.settle(
                lease,
                sample(b.memory, now=b.now),
                terminal=True,
                reference="fake-qualified-release",
                now_ms=b.now,
            )
            assert b.memory.view("shared").committed_bytes == 0
            assert b.coord.claim_next("shared", now_ms=b.now).kind == "load"

        asyncio.run(run())


def test_pending_unload_cannot_be_replayed_or_treated_as_release(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            unload, _ = await unloading(b)
            b.wire.apply = False
            await b.bridge.send(permit(b, unload), activity(b, unload))
            before = len(mutations(b))
            with pytest.raises(p.LmStudioDenied):
                await b.bridge.confirm(
                    permit(b, unload), lambda observed: proof(b, observed)
                )
            with pytest.raises(p.LmStudioDenied, match="already_recorded"):
                await b.bridge.send(permit(b, unload), activity(b, unload))
            assert len(mutations(b)) == before
            assert b.coord.claim_next("shared", now_ms=b.now) is None

        asyncio.run(run())


@pytest.mark.parametrize(
    "bad",
    [
        "instance",
        "context",
        "server_restart",
        "jit",
        "disconnect",
        "cancel",
        "eof",
        "execution_digest",
        "terminal_busy",
        "terminal_instance",
        "terminal_old",
        "revoked",
    ],
)
def test_inference_uncertainty_preserves_active_slot_and_warm_memory(tmp_path, bad):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            intent, lease = await warm(b)
            terminal_changes = {}
            if bad == "disconnect":
                b.wire.stream_error = OSError("secret prompt and token")
            elif bad == "cancel":
                b.wire.stream_error = asyncio.CancelledError()
            elif bad == "eof":
                b.wire.chunks.pop()
            elif bad == "terminal_busy":
                terminal_changes["active_requests"] = 1
            elif bad == "terminal_instance":
                terminal_changes["instance_id"] = "wrong"
            elif bad == "terminal_old":
                terminal_changes["stamp"] = ObservationStamp(
                    0.999, b.coord.observer_clock_id
                )
            elif bad == "execution_digest":
                # Replace the receipt producer rather than duplicating a keyword.
                pass
            else:

                def hook(method, path):
                    if path != "/v1/chat/completions":
                        return
                    if bad == "instance":
                        b.wire.instance = "wrong-instance"
                    elif bad == "context":
                        rows = b.wire.rows()
                        rows[0]["loaded_instances"][0]["config"]["context_length"] = (
                            4096
                        )
                        b.wire.list_override = {"models": rows}
                    elif bad == "server_restart":
                        b.env_changes["server_identity"] = "new-process"
                    elif bad == "jit":
                        b.env_changes["jit_enabled"] = True
                    else:
                        b.authorized = False

                b.wire.hook = hook
            if bad == "execution_digest":
                prepared = p.prepare_inference(
                    intent.instance_id, b.q.profile, request()
                )

                async def wrong_receipt():
                    return activity(b, intent, executed_request_digest="0" * 64)

                async def run_wrong():
                    return [
                        raw
                        async for raw in b.bridge.infer(
                            permit(b, intent, request_digest=prepared.digest),
                            prepared,
                            activity(b, intent),
                            lease=lease,
                            sample=sample(b.memory),
                            produce_terminal=wrong_receipt,
                        )
                    ]

                operation = run_wrong()
            else:
                operation = infer(b, intent, lease, terminal_changes=terminal_changes)
            with pytest.raises((p.LmStudioDenied, asyncio.CancelledError)) as caught:
                await operation
            assert "secret" not in str(caught.value)
            assert b.wire.closed
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == "paused"

        asyncio.run(run())


def test_close_mid_stream_holds_and_closes_transport(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            intent, lease = await warm(b)
            prepared = p.prepare_inference(intent.instance_id, b.q.profile, request())

            async def terminal():
                return activity(b, intent, executed_request_digest=prepared.digest)

            stream = b.bridge.infer(
                permit(b, intent, request_digest=prepared.digest),
                prepared,
                activity(b, intent),
                lease=lease,
                sample=sample(b.memory),
                produce_terminal=terminal,
            )
            assert p.decode(await anext(stream)) == packet()
            await stream.aclose()
            assert b.wire.closed
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.coord.snapshot("shared").state == "reconciling"

        asyncio.run(run())


def test_atomic_memory_send_marker_rollback_then_no_replay_crash_window(
    tmp_path, monkeypatch
):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        original = b.bridge._event

        def injected(conn, permit_, kind, reference):
            if kind == "profile":
                raise RuntimeError("injected journal failure")
            original(conn, permit_, kind, reference)

        monkeypatch.setattr(b.bridge, "_event", injected)
        with pytest.raises(RuntimeError):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert not mutations(b) and b.memory.view("shared").undispatched_ids == (
            lease.lease_id,
        )
        assert (
            b.coord._call(
                lambda conn: conn.execute(
                    "SELECT count(*) FROM journal WHERE event='lmstudio_sent_load'"
                ).fetchone()[0]
            )
            == 0
        )
        monkeypatch.setattr(b.bridge, "_event", original)
        observed = asyncio.run(b.bridge.observe())
        b.bridge._send_marker(
            permit(b, step),
            activity(b, step),
            observed,
            "fake-digest",
            lease,
            sample(b.memory),
        )
        with pytest.raises(p.LmStudioDenied, match="already_recorded"):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert not mutations(b)
        assert b.memory.view("shared").committed_bytes == 600


def test_memory_pressure_pause_commits_without_native_send(tmp_path):
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
        assert b.memory.view("shared").state == "paused" and not mutations(b)


def test_restart_fences_old_intent_and_keeps_uncertain_memory(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.error = TimeoutError("uncertain")
        with pytest.raises(p.LmStudioDenied):
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
    b.bridge = lm.LmStudioLifecycleBridge(
        b.coord,
        b.memory,
        b.wire,
        clock_ms=lambda: b.now,
        environment=b.environment,
        authorize=lambda grant: True,
    )
    with b.coord:
        with pytest.raises(p.LmStudioDenied):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert len(mutations(b)) == 1 and b.memory.view("shared").committed_bytes == 600
        assert b.coord.snapshot("shared").state == "reconciling"


def test_no_sdk_cold_counter_cancellation_or_app_registration(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        for operation in (b.bridge.cancel_native, b.bridge.tokenize):
            with pytest.raises(p.LmStudioDenied, match="unqualified"):
                asyncio.run(operation())
        assert not b.wire.calls
    from pathlib import Path

    source = Path("app.py").read_text()
    assert (
        "import lmstudio_lifecycle" not in source
        and "import lmstudio_protocol" not in source
    )


@pytest.mark.parametrize("required", [False, None])
def test_authentication_disabled_or_unknown_does_not_authorize_native_io(
    tmp_path, required
):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.env_changes["authentication_required"] = required
        with pytest.raises(p.LmStudioDenied, match="environment_unqualified"):
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert not mutations(b)


def test_revoked_grant_after_inventory_is_rechecked_at_atomic_send(tmp_path):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)
        b.wire.hook = lambda method, path: (
            setattr(b, "authorized", False) if path == "/api/v1/models" else None
        )
        with pytest.raises(p.LmStudioDenied, match="authority_denied"):
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


@pytest.mark.parametrize(
    "case", ["id", "context", "version", "clock", "old", "reference"]
)
def test_confirmation_requires_current_observation_bound_instance_config_proof(
    tmp_path, case
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            step, lease = reserved(b)
            await b.bridge.send(
                permit(b, step), activity(b, step), lease=lease, sample=sample(b.memory)
            )
            verify = b.coord.claim_next("shared", now_ms=b.now)

            async def bad(observed):
                evidence = await proof(b, observed)
                if case == "reference":
                    return replace(evidence, reference="replayed-other-reference")
                inventories = list(evidence.inventories)
                i = next(
                    i
                    for i, inv in enumerate(inventories)
                    if inv.runtime.runtime_id == "lmstudio"
                )
                inv = inventories[i]
                if case == "id":
                    inv = replace(
                        inv, instances=(replace(inv.instances[0], instance_id="wrong"),)
                    )
                elif case == "context":
                    inv = replace(
                        inv,
                        instances=(
                            replace(inv.instances[0], effective_context_tokens=4096),
                        ),
                    )
                elif case == "version":
                    inv = replace(inv, runtime_version="0.4.1")
                elif case == "clock":
                    inv = replace(inv, stamp=ObservationStamp(1.0, "wrong"))
                else:
                    inv = replace(
                        inv, stamp=ObservationStamp(0.999, b.coord.observer_clock_id)
                    )
                inventories[i] = inv
                return replace(evidence, inventories=tuple(inventories))

            with pytest.raises(p.LmStudioDenied):
                await b.bridge.confirm(permit(b, verify), bad)
            assert b.coord.snapshot("shared").resident is None

        asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "profile_swap",
        "request_digest",
        "instance_scope",
        "no_routing_cap",
        "cancelled_before_send",
    ],
)
def test_inference_preflight_no_effect_for_changed_profile_payload_scope_or_unsupported_route(
    tmp_path, case
):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            intent, lease = await warm(b)
            if case == "profile_swap":
                values = dict(CONFIG, parallel=8)
                b.q = replace(
                    b.q,
                    profile=replace(b.q.profile, config_json=p.encode(values).decode()),
                )
                # Keep the returned list identical to the new profile: the
                # durable original load profile must still reject the swap.
                rows = b.wire.rows()
                rows[0]["loaded_instances"][0]["config"] = values
                b.wire.list_override = {"models": rows}
            elif case == "instance_scope":
                intent = replace(intent, instance_id="other")
            elif case == "no_routing_cap":
                caps = tuple(
                    replace(cap, state="unknown")
                    if cap.name == "exact_instance_routing"
                    else cap
                    for cap in b.q.capabilities
                )
                b.q = replace(b.q, capabilities=caps)
            elif case == "cancelled_before_send":
                b.coord.cancel(intent.request_id, now_ms=b.now)
            prepared = p.prepare_inference(
                intent.instance_id, b.q.profile, request(model=intent.instance_id)
            )
            grant = permit(
                b,
                intent,
                request_digest="0" * 64
                if case == "request_digest"
                else prepared.digest,
            )
            before = len(mutations(b))

            async def terminal():
                return activity(b, intent, executed_request_digest=prepared.digest)

            with pytest.raises(p.LmStudioDenied):
                async for _ in b.bridge.infer(
                    grant,
                    prepared,
                    activity(b, intent),
                    lease=lease,
                    sample=sample(b.memory),
                    produce_terminal=terminal,
                ):
                    pytest.fail("No native inference events")
            assert len(mutations(b)) == before

        asyncio.run(run())


def test_unavailable_native_list_does_not_try_v0_or_openai_catalog(
    tmp_path, monkeypatch
):
    b = setup(tmp_path)
    with b.coord:
        step, lease = reserved(b)

        async def unavailable(method, path, body, timeout):
            b.wire.calls.append((method, path, None))
            return 404, {"error": "secret server body"}

        monkeypatch.setattr(b.wire, "request", unavailable)
        with pytest.raises(p.LmStudioDenied) as error:
            asyncio.run(
                b.bridge.send(
                    permit(b, step),
                    activity(b, step),
                    lease=lease,
                    sample=sample(b.memory),
                )
            )
        assert "secret" not in str(error.value)
        assert b.wire.calls == [("GET", "/api/v1/models", None)]


def test_wrong_unload_ack_id_retains_hold_and_never_loads_target(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            unload, _ = await unloading(b)
            b.wire.ack = {"instance_id": "wrong-instance"}
            with pytest.raises(p.LmStudioDenied, match="unload_instance_mismatch"):
                await b.bridge.send(permit(b, unload), activity(b, unload))
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.coord.claim_next("shared", now_ms=b.now) is None
            assert b.memory.view("shared").committed_bytes == 600

        asyncio.run(run())


def test_tools_wait_for_terminal_routing_receipt_then_keep_native_ids(tmp_path):
    b = setup(tmp_path)
    with b.coord:

        async def run():
            intent, lease = await warm(b)
            tool = packet(
                choices=[
                    dict(
                        index=0,
                        delta={
                            "tool_calls": [
                                dict(
                                    index=0,
                                    id="original-tool-id",
                                    type="function",
                                    function={"name": "read", "arguments": "{}"},
                                )
                            ]
                        },
                        finish_reason=None,
                    )
                ]
            )
            end = packet(choices=[dict(index=0, delta={}, finish_reason="tool_calls")])
            b.wire.chunks = [sse(tool), sse(end), b"data: [DONE]\n\n"]
            prepared = p.prepare_inference(intent.instance_id, b.q.profile, request())

            async def terminal():
                assert b.wire.closed
                assert b.coord.snapshot("shared").active_requests == 1
                return activity(b, intent, executed_request_digest=prepared.digest)

            stream = b.bridge.infer(
                permit(b, intent, request_digest=prepared.digest),
                prepared,
                activity(b, intent),
                lease=lease,
                sample=sample(b.memory),
                produce_terminal=terminal,
            )
            assert p.decode(await anext(stream)) == tool
            assert b.coord.snapshot("shared").active_requests == 0
            await stream.aclose()
            assert b.coord.snapshot("shared").state == "idle"
            assert b.memory.view("shared").state == "ready"

        asyncio.run(run())
