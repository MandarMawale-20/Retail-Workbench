from app.services.chat import ask, reset_session


def test_reset_session_clears_context_and_turns(session):
    svc, session_id = session
    first = ask(svc, "tester", session_id, "Revenue by category")
    assert first.status == "ok"
    reset_session(svc, "tester", session_id)
    assert svc.repo.list_turns("tester", session_id) == []
    second = ask(svc, "tester", session_id, "Revenue by category")
    assert second.status == "ok"
