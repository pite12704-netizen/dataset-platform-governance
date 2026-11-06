"""Opt-in real MySQL checks; the fixture refuses non-isolated database names/users."""
from test_review_mysql import mysql_case  # noqa: F401
from test_agent_langgraph import (
    test_simultaneous_confirmation_from_two_api_instances_creates_one_plan as concurrent_case,
    test_confirmation_survives_new_app_and_database_instances as restart_case,
    test_crash_after_business_commit_recovers_without_duplicate_job as crash_case,
    test_shared_lease_fences_old_writer_and_expired_process_can_recover as fencing_case,
    test_two_versions_share_atomic_user_preview_budget as budget_case,
    test_failed_job_waits_for_explicit_retry_then_finishes_same_workflow as retry_case,
)


def test_mysql_langgraph_concurrent_confirm(mysql_case):
    concurrent_case(*mysql_case)


def test_mysql_langgraph_restart_and_replay(mysql_case):
    restart_case(*mysql_case)


def test_mysql_langgraph_commit_before_checkpoint(mysql_case, monkeypatch):
    crash_case(*mysql_case, monkeypatch)


def test_mysql_langgraph_expired_lease_fences_old_process(mysql_case):
    fencing_case(*mysql_case)


def test_mysql_langgraph_per_user_budget_across_versions(mysql_case):
    budget_case(*mysql_case)


def test_mysql_langgraph_waits_for_job_retry(mysql_case):
    retry_case(*mysql_case)
