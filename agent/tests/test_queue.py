from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import update

from agent import main
from agent.queue import run_queue, work_run
from agent.tests.stubs import StubModel, call, scripted_model
from backend import main as backend_main
from backend.store import support_requests


def queue_model(*replies: AIMessage) -> StubModel:
    return StubModel(messages=iter(replies))


def work(request_id: str) -> AIMessage:
    return call("work_request", {"request_id": request_id})


def todos(*request_ids: str) -> AIMessage:
    return call(
        "write_todos",
        {
            "todos": [
                {"request_id": rid, "title": f"{rid} request", "status": "to_do"}
                for rid in request_ids
            ]
        },
    )


def finish(text: str = "Summary for the support team.") -> AIMessage:
    return AIMessage(content=text)


def request_models(*failing: str) -> Callable[[], StubModel]:
    """Give each request a fresh stub; the listed attempts claim an action and fail."""
    plan = list(failing)

    def make() -> StubModel:
        if plan:
            plan.pop(0)
            return scripted_model(rationale="The order has been cancelled.")
        return scripted_model()

    return make


def start(store, cap: int) -> str:
    run_id = store.create_run(cap)
    assert run_id is not None
    return run_id


def statuses(store) -> dict[str, str]:
    return {item["request_id"]: item["status"] for item in store.list_requests()}


def todo_statuses(store) -> dict[str, str]:
    run = store.current_run()
    return {todo["request_id"]: todo["status"] for todo in run["todos"]}


def test_a_capped_run_plans_works_its_cap_and_leaves_the_rest_waiting(store):
    run_id = start(store, 2)
    llm = queue_model(
        call("list_waiting_requests"),
        todos("REQ-7007", "REQ-7002"),
        work("REQ-7007"),
        work("REQ-7002"),
        work("REQ-7006"),
        finish("Two requests are waiting for support review."),
    )

    summary = run_queue(run_id, 2, llm, request_models())

    assert summary == "Two requests are waiting for support review."
    queue = statuses(store)
    assert queue["REQ-7007"] == "waiting_for_agent_review"
    assert queue["REQ-7002"] == "waiting_for_agent_review"
    assert list(queue.values()).count("waiting") == 7
    assert todo_statuses(store) == {"REQ-7007": "done", "REQ-7002": "done"}
    assert store.get_request("REQ-7006")["steps"] == []


def test_a_failed_request_does_not_stop_the_run_and_is_tried_again(store):
    run_id = start(store, 2)
    llm = queue_model(
        todos("REQ-7007", "REQ-7002"),
        work("REQ-7002"),
        work("REQ-7007"),
        work("REQ-7002"),
        finish(),
    )

    run_queue(run_id, 2, llm, request_models("REQ-7002 first attempt"))

    request = store.get_request("REQ-7002")
    stages = [step["stage"] for step in request["steps"]]
    assert statuses(store)["REQ-7007"] == "waiting_for_agent_review"
    assert request["status"] == "waiting_for_agent_review"
    assert request["recommendation"]["outcome"] == "approve"
    assert stages.count("started") == 2
    assert "error" in stages
    assert todo_statuses(store) == {"REQ-7007": "done", "REQ-7002": "done"}


def test_a_request_that_fails_twice_is_left_for_support(store):
    run_id = start(store, 1)
    llm = queue_model(
        todos("REQ-7007"),
        work("REQ-7007"),
        work("REQ-7007"),
        finish("REQ-7007 was left for a support agent."),
    )

    summary = run_queue(run_id, 1, llm, request_models("first", "second"))

    request = store.get_request("REQ-7007")
    assert summary == "REQ-7007 was left for a support agent."
    assert request["status"] == "waiting_for_agent_review"
    assert request["recommendation"] is None
    assert request["steps"][-1]["stage"] == "left_for_support"
    assert todo_statuses(store) == {"REQ-7007": "left_for_support"}


def test_the_agent_is_sent_back_when_it_misses_a_waiting_request(store):
    run_id = start(store, 2)
    llm = queue_model(
        work("REQ-7007"),
        finish("Done early."),
        work("REQ-7002"),
        finish("Both requests are ready for review."),
    )

    summary = run_queue(run_id, 2, llm, request_models())

    assert summary == "Both requests are ready for review."
    assert statuses(store)["REQ-7002"] == "waiting_for_agent_review"
    assert list(statuses(store).values()).count("waiting") == 7


def test_a_run_that_keeps_missing_requests_fails(store):
    run_id = start(store, 1)
    llm = queue_model(*(finish() for _ in range(4)))
    with pytest.raises(RuntimeError, match="left requests unworked: REQ-7007"):
        run_queue(run_id, 1, llm, request_models())


def test_a_request_that_is_not_waiting_does_not_use_the_cap(store):
    run_id = start(store, 1)
    llm = queue_model(work("REQ-0000"), work("REQ-7007"), finish())

    run_queue(run_id, 1, llm, request_models())

    assert statuses(store)["REQ-7007"] == "waiting_for_agent_review"
    assert todo_statuses(store) == {"REQ-7007": "done"}


def test_an_error_outside_the_helper_does_not_stop_the_run(store, monkeypatch):
    run_id = start(store, 2)
    llm = queue_model(
        work("REQ-7007"), work("REQ-7002"), finish(), work("REQ-7007"), finish()
    )
    calls = []

    def broken_then_working() -> StubModel:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("model service unreachable")
        return scripted_model()

    run_queue(run_id, 2, llm, broken_then_working)

    queue = statuses(store)
    assert queue["REQ-7002"] == "waiting_for_agent_review"
    assert queue["REQ-7007"] == "waiting_for_agent_review"
    assert todo_statuses(store) == {"REQ-7007": "done", "REQ-7002": "done"}


def test_run_finishes_signed_off_and_stopped_requests_when_agent_was_unreachable(
    api, store
):
    main.work_request("REQ-7008", scripted_model())
    signed_off = api.post(
        "/api/requests/REQ-7008/review",
        json={
            "outcome": "approve",
            "notes": "Signed off while agent service was offline.",
            "reviewed_by": "Priya Nair",
        },
    )
    assert signed_off.status_code == 200
    assert store.get_request("REQ-7008")["status"] == "decided"

    store.start_request("REQ-7002")
    assert store.get_request("REQ-7002")["status"] == "in_progress"

    run_id = start(store, 2)
    llm = queue_model(
        todos("REQ-7007"),
        work("REQ-7007"),
        finish("Finished REQ-7008, resumed REQ-7002, and worked REQ-7007."),
    )

    summary = run_queue(run_id, 2, llm, request_models())

    assert "Finished REQ-7008" in summary
    queue = statuses(store)
    assert queue["REQ-7008"] == "decided"
    assert [step["stage"] for step in store.get_request("REQ-7008")["steps"]].count(
        "completed"
    ) == 1
    assert queue["REQ-7002"] == "waiting_for_agent_review"
    assert queue["REQ-7007"] == "waiting_for_agent_review"
    assert todo_statuses(store) == {"REQ-7002": "done", "REQ-7007": "done"}


def test_work_run_reports_a_finished_run_to_the_backend(store):
    run_id = start(store, 1)
    llm = queue_model(todos("REQ-7007"), work("REQ-7007"), finish("Worked one."))

    assert work_run(run_id, 1, llm, request_models()) == "finished"

    run = store.current_run()
    assert run["status"] == "finished"
    assert run["summary"] == "Worked one."


def test_work_run_reports_a_failed_run_to_the_backend(store):
    run_id = start(store, 1)
    llm = queue_model(*(finish() for _ in range(4)))

    assert work_run(run_id, 1, llm, request_models()) == "failed"

    run = store.current_run()
    assert run["status"] == "failed"
    assert "left requests unworked" in run["summary"]


def test_dispatch_leaves_active_resume_alone_and_works_missed_send_back(
    api, store, monkeypatch
):
    monkeypatch.setattr(backend_main, "notify_agent_resume", lambda rid: None)
    main.work_request("REQ-7008", scripted_model())
    main.work_request("REQ-7007", scripted_model())

    # REQ-7008 is sent back while agent is offline -> stays sent_back for next run
    sent_missed = api.post(
        "/api/requests/REQ-7008/send_back",
        json={"note": "Verify carrier scan.", "sent_back_by": "Priya Nair"},
    )
    assert sent_missed.json()["status"] == "sent_back"

    # REQ-7007 is sent back and actively claimed by resume with fresh heartbeat
    api.post(
        "/api/requests/REQ-7007/send_back",
        json={"note": "Check expiry date.", "sent_back_by": "Priya Nair"},
    )
    assert store.take_sent_back("REQ-7007", "resume") == 1
    assert store.get_request("REQ-7007")["resume_heartbeat_at"] is not None

    # A queue run picks up REQ-7008 (resuming its paused thread with the note) while
    # leaving REQ-7007 in progress untouched because its resume heartbeat is recent.
    run_id = start(store, 1)
    llm = queue_model(
        call("list_waiting_requests"),
        todos("REQ-7008"),
        work("REQ-7008"),
        finish("Worked REQ-7008 after missed send-back."),
    )
    redecide = scripted_model(
        outcome="decline",
        rationale="Carrier scan shows dispatched before request.",
        include_context=False,
    )
    summary = run_queue(run_id, 1, llm, lambda: redecide)
    store.finish_run(run_id, "finished", summary)

    assert "Worked REQ-7008" in summary
    assert store.get_request("REQ-7008")["status"] == "waiting_for_agent_review"
    assert store.get_request("REQ-7008")["recommendation"]["outcome"] == "decline"
    assert len(store.get_request("REQ-7008")["sent_back"]) == 1
    assert store.get_request("REQ-7007")["status"] == "in_progress"
    assert store.get_request("REQ-7007")["recommendation"] is None

    # If REQ-7007's resume goes silent (> 5 minutes), the next run recovers it too.
    stale_time = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    with store.engine.begin() as connection:
        connection.execute(
            update(support_requests)
            .where(support_requests.c.request_id == "REQ-7007")
            .values(resume_heartbeat_at=stale_time)
        )
    run2 = start(store, 1)
    redecide_7007 = scripted_model(
        outcome="decline",
        rationale="Recovered after silent resume.",
        include_context=False,
    )
    run_queue(
        run2, 1, queue_model(finish("Recovered REQ-7007.")), lambda: redecide_7007
    )
    assert store.get_request("REQ-7007")["status"] == "waiting_for_agent_review"
    assert store.get_request("REQ-7007")["recommendation"]["outcome"] == "decline"
