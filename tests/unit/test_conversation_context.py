from app.conversation.context import update_state
from app.query.schemas import ConversationState, Filter, QueryPlan


def test_filters_drop_after_six_unreferenced_turns():
    state = ConversationState(session_id="session", owner_id="owner")
    filtered_plan = QueryPlan(
        intent="aggregate",
        filters=[Filter(column_id="stores.region", op="eq", value="West")],
    )
    state = update_state(state, filtered_plan, None, "only stores in the West region")

    for turn in range(6):
        state = update_state(state, None, None, f"unrelated question {turn}")

    assert [f.column_id for f in state.active_filters] == ["stores.region"]
    state = update_state(state, None, None, "another unrelated question")
    assert state.active_filters == []
    assert state.previous_plan is not None
    assert state.previous_plan.filters == []


def test_explicit_carry_refreshes_filter_reference_age():
    state = ConversationState(session_id="session", owner_id="owner")
    filtered_plan = QueryPlan(
        intent="aggregate",
        filters=[Filter(column_id="stores.region", op="eq", value="West")],
    )
    state = update_state(state, filtered_plan, None, "only stores in the West region")

    for turn in range(6):
        state = update_state(state, filtered_plan, None, f"among those, question {turn}")

    assert state.active_filters
    assert state.filter_last_referenced_turn["stores.region"] == state.turn_count
