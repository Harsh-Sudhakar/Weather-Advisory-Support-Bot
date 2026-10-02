"""Tests for the parts that never call a model, so CI can run them without a key.

The suite in `suite.py` exercises the whole graph and therefore needs OPENROUTER_API_KEY. Everything
checked here is pure: the condition engine, the window arithmetic, ranking, grounding and the lint.
That is deliberately most of the logic this system is trusted for.
"""

import pytest

from app import graph, grounding, llm
from app.facts import POLICY_FACTS, build_facts, comfort_score, timeline
from app.sops import OPS, Sop, evaluate, lint, lint_all, load_sops, match_all, rank
from evals.fixtures import SCENARIOS


def question_facts(payload, window="now", **overrides):
    """A fact table as the graph would build it, with the question half filled in."""
    facts, _ = build_facts(payload, window)
    facts.update(activity_category=["outdoor_exercise"], audience=["general"],
                 is_outdoor_question=True, location="Testville")
    facts.update(overrides)
    return facts


# --- the condition engine -------------------------------------------------------------------

@pytest.mark.parametrize("op,actual,expected,want", [
    ("gte", 50, 50, True), ("gte", 49.9, 50, False),
    ("gt", 50, 50, False), ("lte", 5, 5, True), ("lt", 5, 5, False),
    ("between", 40, [40, 72], True), ("between", 72.1, [40, 72], False),
    ("includes_any", ["children"], ["children", "elderly"], True),
    ("includes_any", ["general"], ["children"], False),
    ("in", "evening", ["evening", "night"], True),
    ("is_true", True, True, True), ("is_true", False, True, False),
])
def test_operators(op, actual, expected, want):
    assert OPS[op](actual, expected) is want or OPS[op](actual, expected) == want


def test_missing_fact_never_matches_on_a_guess():
    """A reading we do not have must make the condition false, not raise and not be assumed."""
    sop = Sop(id="test_rule", title="Test rule", category="testing", severity="high", guidance="x" * 50, verdict="Test",
              when=[{"fact": "gust_kmh", "op": "gte", "value": 50}], requires_facts=["gust_kmh"])
    result = evaluate(sop, {"gust_kmh": None})
    assert result["matched"] is False
    assert result["missing_facts"] == ["gust_kmh"]


def test_any_of_needs_only_one_branch():
    sop = Sop(id="test_rule", title="Test rule", category="testing", severity="high", guidance="x" * 50, verdict="Test",
              when=[{"any_of": [{"fact": "a", "op": "gte", "value": 10},
                                {"fact": "b", "op": "gte", "value": 10}]}])
    assert evaluate(sop, {"a": 1, "b": 99})["matched"] is True
    assert evaluate(sop, {"a": 1, "b": 1})["matched"] is False


# --- ranking, which is the conflict policy ---------------------------------------------------

def test_override_beats_a_higher_priority_non_override():
    facts = question_facts(SCENARIOS["thunderstorm"])
    ordered = rank(match_all(facts))
    assert ordered[0].override is True
    assert len(ordered) > 1, "expected a genuine conflict in this scenario"


def test_ranking_is_a_total_order_not_a_file_listing():
    import random
    facts = question_facts(SCENARIOS["hot_for_children"], audience=["children"])
    baseline = [s.id for s in rank(match_all(facts))]
    for _ in range(8):
        pool = load_sops()[:]
        random.shuffle(pool)
        assert [s.id for s in rank(match_all(facts, pool))] == baseline


# --- window arithmetic ------------------------------------------------------------------------

@pytest.mark.parametrize("window", ["now", "morning", "afternoon", "evening", "night", "today", "tomorrow"])
def test_every_window_resolves_with_no_missing_readings(window):
    facts, provenance = build_facts(SCENARIOS["pleasant"], window)
    for key in ("temp_c", "wind_kmh", "gust_kmh", "precip_prob_pct", "comfort_score"):
        assert facts[key] is not None, f"{key} unavailable for window {window}"
    assert provenance["temp_c"]


def test_a_window_already_past_rolls_forward_rather_than_answering_about_the_past():
    from evals.fixtures import payload
    late = payload(now_hour=23)
    facts, _ = build_facts(late, "morning")
    assert facts["window_date"] > late["current"]["time"][:10]


def test_timeline_window_matches_the_window_the_facts_used():
    series = timeline(SCENARIOS["pleasant"], "evening")
    assert len(series["hours"]) == 24
    assert any(series["in_window"]), "the shaded band must cover something"


def test_comfort_score_stays_in_range_and_moves_the_right_way():
    perfect = comfort_score(24, 0, 5, 3, 50)
    grim = comfort_score(41, 95, 60, 11, 95)
    assert 0 <= grim < perfect <= 100


# --- grounding --------------------------------------------------------------------------------

def test_a_number_from_nowhere_is_rejected():
    ok, problems = grounding.check("Winds are 61.0 km/h.", {"wind_kmh": 12.0}, [], claims=[])
    assert not ok and problems


def test_a_real_number_under_the_wrong_reading_is_rejected():
    """The case provenance alone cannot catch: 38.0 is real, but it is the gust, not the wind."""
    facts = {"wind_kmh": 22.0, "gust_kmh": 38.0}
    ok, problems = grounding.check("Winds are steady at 38.0 km/h.", facts, [],
                                   claims=[{"value": 38.0, "fact": "wind_kmh"}])
    assert not ok
    assert "wind_kmh" in problems[0] and "22.0" in problems[0]


def test_a_correctly_attributed_number_passes():
    facts = {"wind_kmh": 22.0, "gust_kmh": 38.0}
    ok, problems = grounding.check("Gusts reach 38.0 km/h.", facts, [],
                                   claims=[{"value": 38.0, "fact": "gust_kmh"}])
    assert ok, problems


def test_a_negative_reading_is_grounded():
    """Cold-exposure replies quote sub-zero temperatures; reading "-3.5" as 3.5 failed every one."""
    facts = {"apparent_temp_c": -3.5}
    ok, problems = grounding.check("It feels like -3.5°C out there.", facts, [],
                                   claims=[{"value": -3.5, "fact": "apparent_temp_c"}])
    assert ok, problems


def test_a_hyphenated_range_is_not_read_as_negative():
    assert grounding._numbers_in("Rest for 5-10 minutes.") == [5.0, 10.0]
    assert grounding._numbers_in("observed 2026-10-02") == [2026.0, 10.0, 2.0]


def test_a_thousands_separator_stays_one_number():
    facts = {"visibility_m": 1200.0}
    ok, problems = grounding.check("Visibility is down to 1,200 m.", facts, [],
                                   claims=[{"value": 1200, "fact": "visibility_m"}])
    assert ok, problems


def test_a_null_current_reading_is_missing_not_a_crash():
    payload = SCENARIOS["pleasant"]
    patched = {**payload, "current": {**payload["current"], "wind_gusts_10m": None}}
    facts, _ = build_facts(patched, "now")
    assert facts["gust_kmh"] is None


def test_a_former_city_name_is_searched_under_its_current_one():
    from app.weather import _modern_name
    assert _modern_name("Bangalore") == "Bengaluru"
    assert _modern_name("bombay, Maharashtra") == "Mumbai, Maharashtra"
    assert _modern_name("Bhopal") == "Bhopal"


# --- session state, which the checkpointer carries between turns --------------------------------

def _intent(**overrides):
    base = dict(location="Bhopal", activity_category=["outdoor_exercise"], audience=["general"],
                time_window="now", is_outdoor_question=True, is_smalltalk=False,
                is_weather_question=True, restated="")
    return {**base, **overrides}


def test_a_new_turn_clears_the_last_turns_results(monkeypatch):
    """A follow-up that never reaches the weather nodes must not report the previous turn's place,
    readings or rejected policies as its own."""
    monkeypatch.setattr(llm, "extract_intent", lambda m, h: _intent(
        location=None, activity_category=[], is_outdoor_question=False, is_weather_question=False))
    previous = {"question": "what should I cook?", "messages": [], "facts": {"temp_c": 30.0},
                "evaluations": [{"sop": None}], "place": {"name": "Bhopal"}, "series": {"hours": []}}
    patch = graph.parse_request(previous)
    assert patch["facts"] is None and patch["evaluations"] == [] and patch["place"] is None


def test_a_failed_parse_also_clears_the_last_turns_results(monkeypatch):
    def down(message, history):
        raise llm.LLMUnavailable("model call failed (APIConnectionError)")

    monkeypatch.setattr(llm, "extract_intent", down)
    patch = graph.parse_request({"question": "and now?", "messages": [], "facts": {"temp_c": 30.0}})
    assert patch["failure"]["stage"] == "intent" and patch["facts"] is None


def test_small_talk_does_not_erase_the_activity_a_follow_up_inherits(monkeypatch):
    carried = {"activity_category": ["outdoor_exercise"], "audience": ["general"]}
    monkeypatch.setattr(llm, "extract_intent", lambda m, h: _intent(
        location=None, activity_category=[], is_outdoor_question=False,
        is_weather_question=False, is_smalltalk=True))
    patch = graph.parse_request({"question": "thanks!", "messages": [], "session_intent": carried})
    assert patch["session_intent"] == carried


# --- routing, which decides what kind of answer a question gets ---------------------------------

def test_a_greeting_skips_the_policy_path():
    state = {"intent": {"is_weather_question": False, "is_smalltalk": True}}
    assert graph.route_after_parse(state) == "general_answer"


def test_a_non_weather_question_is_still_refused():
    """Small talk is the only thing that bypasses the refusal. "What should I cook tonight?" is
    still a request for advice no policy covers, and must not reach a free-form reply."""
    state = {"intent": {"is_weather_question": False, "is_smalltalk": False}}
    assert graph.route_after_parse(state) == "no_policy_answer"


def test_a_greeting_cites_nothing(monkeypatch):
    monkeypatch.setattr(llm, "general_reply", lambda message, history: "Hello. Ask me about conditions.")
    patch = graph.general_answer({"question": "hi", "messages": [{"role": "user", "content": "hi"}]})
    assert patch["answer"] == "Hello. Ask me about conditions."
    assert patch["primary"] is None and patch["secondary"] == []


def test_a_greeting_with_no_model_takes_the_failure_branch(monkeypatch):
    """Every other node that calls the model degrades into honest_failure. This one must too,
    rather than raising out of the graph and 500ing the request."""
    def down(message, history):
        raise llm.LLMUnavailable("model call failed (APIConnectionError)")

    monkeypatch.setattr(llm, "general_reply", down)
    patch = graph.general_answer({"question": "hi", "messages": [{"role": "user", "content": "hi"}]})
    assert patch["failure"]["stage"] == "general"
    assert graph.route_after_general(patch) == "honest_failure"


def test_a_failed_greeting_is_worded_rather_than_crashing():
    """honest_failure indexes FAILURE_TEXT by stage, so a stage with no entry reaches the user
    as a KeyError."""
    patch = graph.honest_failure({"failure": {"stage": "general", "reason": "model call failed"}})
    assert patch["answer"].strip()
    assert patch["primary"] is None


# --- the policy set itself ----------------------------------------------------------------------

def test_shipped_policies_pass_their_own_lint():
    assert lint_all() == {}


def test_policy_set_meets_the_shape_the_brief_asks_for():
    sops = load_sops()
    assert len(sops) >= 10
    assert len({s.category for s in sops}) >= 3
    assert len({s.severity for s in sops}) >= 3
    assert all(s.verdict.strip() for s in sops), "every policy needs a decision, not just an explanation"


def test_lint_catches_a_fact_that_does_not_exist():
    broken = Sop(id="test_rule", title="Test rule", category="testing", severity="low", guidance="x" * 50,
                 when=[{"fact": "uv_indx", "op": "gte", "value": 3}])
    assert any("unknown fact" in p for p in lint(broken))


def test_lint_catches_a_hard_requirement_that_was_not_declared():
    broken = Sop(id="test_rule", title="Test rule", category="testing", severity="low", guidance="x" * 50, verdict="Go",
                 when=[{"fact": "uv_index", "op": "gte", "value": 3}])
    assert any("requires_facts" in p for p in lint(broken))


def test_every_policy_condition_uses_a_known_fact():
    for sop in load_sops():
        for problem in lint(sop):
            assert "unknown fact" not in problem
    assert "comfort_score" in POLICY_FACTS


# --- the schema itself, which is what stops a bad file entering the rule set -------------------

@pytest.mark.parametrize("bad,because", [
    ({"severity": "apocalyptic"}, "severity outside the allowed set"),
    ({"id": "Has Capitals And Spaces"}, "id is used as a citation, so it is constrained"),
    ({"guidance": "be careful"}, "guidance too short to be actionable advice"),
    ({"when": [{"fact": "uv_index", "op": "exceeds", "value": 8}]}, "unknown operator"),
    ({"cite_as": "SOP-1"}, "unknown key, most likely a typo for an existing one"),
    ({"guidance": "What the bot should tell the user when this policy applies."},
     "editor template saved unedited, so the model would invent the advice"),
    ({"title": "What this policy is called"}, "editor template title cited to the user"),
])
def test_a_malformed_policy_is_rejected_with_the_reason(bad, because):
    from pydantic import ValidationError
    from app.sops import Sop
    good = dict(id="test_rule", title="Test rule", category="testing", severity="low",
                verdict="Go", guidance="x" * 50, when=[{"fact": "uv_index", "op": "gte", "value": 8}])
    with pytest.raises(ValidationError):
        Sop(**{**good, **bad})


def test_a_bad_file_names_itself(tmp_path):
    """The app refuses to start on a malformed policy, and the message says which file."""
    import os
    from app import sops as policy
    original = policy.SOP_DIR
    (tmp_path / "77_wrong.yaml").write_text("id: x\ntitle: y\n", encoding="utf-8")
    policy.SOP_DIR = tmp_path
    try:
        with pytest.raises(policy.SopError) as caught:
            policy.load_sops(force=True)
        assert "77_wrong.yaml" in str(caught.value)
    finally:
        policy.SOP_DIR = original
        policy.load_sops(force=True)


def test_an_unusable_weather_payload_is_rejected_before_it_is_used():
    """Open-Meteo answers a request with no field list using 200 and no readings."""
    from pydantic import ValidationError
    from app.weather import ForecastPayload
    with pytest.raises(ValidationError):
        ForecastPayload(current={"time": "2026-09-17T10:00"}, hourly={"time": ["2026-09-17T10:00"]})
