"""Fenced ready intent; no ACP/network/provider is mocked into success."""

from dataclasses import asdict
import json
import subprocess
from types import SimpleNamespace

import pytest

from agent_dispatch.ordinary_agent_storage import record_created_agent
from agent_dispatch.ordinary_invocation import prepare
from agent_dispatch.queue import TaskQueue, TaskError


@pytest.fixture
def intent(tmp_path):
    queue = TaskQueue(tmp_path / "queue.db")
    task = queue.create(title="fixed work", repo="fixture", require_verification=True)
    reservation, _ = queue.reserve_spawn(task.id, driver="ordinary-bridge")
    info = {
        "session_id": "bridge-a", "ready": True, "status": "awaiting_first_prompt",
        "connection": {"worktree_id": "worktree-a", "repo": "fixture",
                       "native_session_id": "native-a", "target_dir": "/fixture/worktree-a"},
        "creation": {"worktree": {"creation_nonce": "original-nonce"}},
    }
    prompt = 'Unicode café "quoted"\n第二行'
    invocation = prepare(asdict(task), asdict(reservation), info, prompt)
    return queue, task, reservation, info, prompt, invocation


def test_ready_and_frozen_invocation_commit_and_read_back_together(intent):
    queue, task, reservation, info, _, invocation = intent
    written = record_created_agent(queue, reservation.key, task_generation=task.generation,
                                   agent_info=info, startup_invocation=invocation)
    assert written.agent_info["startup_invocation"] == invocation
    assert queue.get_reservation(reservation.key).agent_info == written.agent_info


def test_deferral_keeps_original_key_label_generation_and_prompt(intent):
    queue, task, reservation, info, prompt, invocation = intent
    record_created_agent(queue, reservation.key, task_generation=0,
                         agent_info=info, startup_invocation=invocation)
    queue.defer_spawn(reservation.key)
    retry, admitted = queue.reserve_spawn(task.id, driver="ordinary-bridge")
    assert admitted and retry.key != reservation.key
    retained = retry.agent_info
    assert prepare(asdict(task), asdict(retry), retained, prompt) == invocation
    assert retry.allocation_revision == reservation.allocation_revision


def test_prompt_is_not_identity_but_overwrite_is_conflict(intent):
    queue, task, reservation, info, prompt, invocation = intent
    changed = prepare(asdict(task), asdict(reservation), info, prompt + " changed")
    assert changed["invocation_key"] == invocation["invocation_key"]
    record_created_agent(queue, reservation.key, task_generation=0,
                         agent_info=info, startup_invocation=invocation)
    with pytest.raises(TaskError, match="already bound"):
        record_created_agent(queue, reservation.key, task_generation=0,
                             agent_info=info, startup_invocation=changed)


def test_actual_claim_increments_generation_without_rewriting_admission(intent):
    queue, task, reservation, info, _, invocation = intent
    record_created_agent(queue, reservation.key, task_generation=0,
                         agent_info=info, startup_invocation=invocation)
    claimed = queue.claim_one("worker", task_id=task.id)
    assert claimed.generation == 1 and invocation["binding"]["admission_generation"] == 0
    assert queue.get_reservation(reservation.key).agent_info["startup_invocation"] == invocation
    with pytest.raises(TaskError, match="fence"):
        record_created_agent(queue, reservation.key, task_generation=0,
                             agent_info=info, startup_invocation=invocation)


@pytest.mark.parametrize("change", ["omitted", "unknown-history"])
def test_created_storage_preserves_frozen_intent_transactionally(intent, change):
    queue, _, reservation, info, _, invocation = intent
    record_created_agent(queue, reservation.key, task_generation=0,
                         agent_info=info, startup_invocation=invocation)
    if change == "unknown-history":
        previous = queue.get_reservation(reservation.key).agent_info
        previous["startup_invocation"]["version"] = 2
        with queue._connect() as conn:
            conn.execute("UPDATE spawn_reservations SET agent_info=? WHERE key=?",
                         (json.dumps(previous), reservation.key))
    before = asdict(queue.get_reservation(reservation.key))
    with pytest.raises(TaskError):
        record_created_agent(queue, reservation.key, task_generation=0, agent_info=info,
                             startup_invocation=None if change == "omitted" else invocation)
    assert asdict(queue.get_reservation(reservation.key)) == before


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("invocation_key", "different"),
])
def test_unknown_or_changed_identity_cannot_be_stored(intent, field, value):
    queue, _, reservation, info, _, invocation = intent
    with pytest.raises(TaskError):
        record_created_agent(queue, reservation.key, task_generation=0,
                             agent_info=info, startup_invocation={**invocation, field: value})
    assert queue.get_reservation(reservation.key).agent_info is None


@pytest.mark.parametrize("version", [None, 2, True])
def test_unknown_historical_intent_never_gets_a_new_key(intent, version):
    _, task, reservation, info, prompt, invocation = intent
    previous = {**invocation, "version": version}
    with pytest.raises(TaskError):
        prepare(asdict(task), asdict(reservation), {**info, "startup_invocation": previous}, prompt)


def test_old_storage_ignoring_intent_refuses_before_send(intent, monkeypatch):
    from agent_dispatch import bridge, ordinary_bridge
    queue, task, reservation, info, prompt, _ = intent
    calls = []

    class Client:
        def get_reservation(self, key):
            return asdict(queue.get_reservation(key))

        def record_spawn_allocation(self, key, project):
            return asdict(queue.record_spawn_allocation(key, project))

        def record_created_agent(self, key, **facts):
            facts.pop("startup_invocation")
            return asdict(record_created_agent(queue, key, **facts))

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[2] == "create"
        assert argv[argv.index("--min-daemon-protocol") + 1] == "45"
        return subprocess.CompletedProcess(argv, 0, json.dumps(info), "")

    monkeypatch.setattr(ordinary_bridge, "agent_bridge_launch_prefix", lambda: ["test-bridge"])
    monkeypatch.setattr(ordinary_bridge, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired,
    ))
    monkeypatch.setattr(bridge, "registered_agent_project", lambda *_a, **_kw: "fixture")
    with pytest.raises(ordinary_bridge.CreatedAgentUnrecorded, match="read-back"):
        ordinary_bridge.start(
            {**asdict(task), "spawn_reservation_key": reservation.key},
            client=Client(), agent="mock", project="fixture", prompt=prompt,
        )
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["held", "released", "retired", "superseded"])
def test_existing_fresh_admission_fences_still_refuse(intent, change):
    queue, task, reservation, info, _, invocation = intent
    if change == "held":
        queue.set_hold(task.id, reason="test hold", actor="test")
    elif change == "released":
        queue.request_spawn_release(reservation.key, detail="test release")
    elif change == "retired":
        queue.fail_spawn(reservation.key, detail="test failure")
    else:
        queue.defer_spawn(reservation.key)
        queue.reserve_spawn(task.id, driver="ordinary-bridge")
    with pytest.raises(TaskError):
        record_created_agent(queue, reservation.key, task_generation=0,
                             agent_info=info, startup_invocation=invocation)
