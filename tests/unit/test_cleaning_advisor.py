import pandas as pd

from app.cleaning.advisor import CleaningAdvisor
from app.cleaning.plan_generator import build_plan
from app.llm.mock_provider import MockProvider
from app.profiling.profiler import profile_frame
from app.schemas import PipelineOptions, RoleBinding


def test_advisor_applies_only_a_high_confidence_fuzzy_mapping():
    provider = MockProvider(['{"mappings":[{"from":"Shooes","to":"Shoes","confidence":0.95}]}'])

    result = CleaningAdvisor(provider).suggest({"Shooes": 2, "Shoes": 8})

    assert result.applied["Shooes"]["to"] == "Shoes"
    assert result.review == []
    assert provider.requests[0][0]["role"] == "system"


def test_advisor_keeps_low_confidence_mapping_for_review():
    provider = MockProvider(['{"mappings":[{"from":"Shooes","to":"Shoes","confidence":0.89}]}'])

    result = CleaningAdvisor(provider).suggest({"Shooes": 2, "Shoes": 8})

    assert result.applied == {}
    assert result.review[0]["target"] == "Shoes"


def test_advisor_rejects_unknown_targets_and_is_not_called_when_disabled():
    provider = MockProvider(['{"mappings":[{"from":"Shose","to":"Secret","confidence":1.0}]}'])
    result = CleaningAdvisor(provider).suggest({"Shose": 2, "Shoes": 8})

    assert result.applied == {} and result.review == []
    provider.requests.clear()
    frame = pd.DataFrame({"category": ["Shose", "Shoes"]})
    profile = profile_frame(frame, dataset="products", version_id="v", stage="raw")
    binding = RoleBinding(
        role="category", dataset_version_id="v", column_id="products.category",
        confidence=1.0, inference_source=["alias"], status="verified",
    )
    build_plan("products", "v", frame, profile, [], [binding], PipelineOptions(use_advisor=False),
               provider=provider)
    assert provider.requests == []


def test_advisor_mapping_is_written_to_the_cleaning_step():
    provider = MockProvider(['{"mappings":[{"from":"Shooes","to":"Shoes","confidence":0.95}]}'])
    frame = pd.DataFrame({"category": ["Shooes", "Shoes"]})
    profile = profile_frame(frame, dataset="products", version_id="v", stage="raw")
    binding = RoleBinding(
        role="category", dataset_version_id="v", column_id="products.category",
        confidence=1.0, inference_source=["alias"], status="verified",
    )

    plan = build_plan("products", "v", frame, profile, [], [binding], PipelineOptions(), provider=provider)

    advisor_steps = [step for step in plan.steps if step.source == "rule" and step.parameters.get("mapping")]
    assert advisor_steps and advisor_steps[0].parameters["mapping"]["Shooes"] == "Shoes"
