import sys

import pytest

from agent import __main__ as cli
from agent import main
from agent.tests.stubs import scripted_model
from backend import agent_service
from backend import main as backend_main


def test_one_named_request_is_worked_from_the_terminal(store, monkeypatch, capsys):
    work = main.work_request
    monkeypatch.setattr(
        main, "work_request", lambda request_id: work(request_id, scripted_model())
    )
    monkeypatch.setattr(sys, "argv", ["agent", "REQ-7008"])
    cli.main()
    assert capsys.readouterr().out.startswith("REQ-7008: approve")
    assert store.get_request("REQ-7008")["status"] == "waiting_for_agent_review"


def test_a_capped_run_is_started_and_waited_for(api, monkeypatch, capsys):
    def finish_at_once(payload):
        run_id = payload["run_id"]
        cap = backend_main.app.state.store.get_run(run_id)["cap"]
        backend_main.app.state.store.finish_run(
            run_id, "finished", f"Worked {cap} requests."
        )
        return agent_service.AgentCallResult("accepted")

    monkeypatch.setattr(agent_service, "call_agent", finish_at_once)
    monkeypatch.setattr(sys, "argv", ["agent", "--cap", "3"])
    cli.main()
    assert capsys.readouterr().out.rstrip().endswith("finished: Worked 3 requests.")


def test_the_terminal_needs_a_request_or_a_cap(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent"])
    with pytest.raises(SystemExit):
        cli.main()
