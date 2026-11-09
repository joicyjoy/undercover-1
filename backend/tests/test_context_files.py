"""Tests for backend.context_files reader used only during seeding."""

from backend import context_files


def test_plans_loaded_without_source():
    plans_data = context_files.plans()
    assert "source" not in plans_data
    assert "plans" in plans_data
    assert len(plans_data["plans"]) >= 3


def test_documents_loaded_with_version_and_no_front_matter():
    manual = context_files.document("claims_manual")
    assert manual.version == "2026.2"
    assert "---" not in manual.text[:10]
    assert "source:" not in manual.text[:100]

    wording = context_files.document("policy_wording")
    assert wording.version == "2026.1"
    assert "---" not in wording.text[:10]
