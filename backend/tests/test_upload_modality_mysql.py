"""The same admission and metadata transitions on an isolated real MySQL."""
from test_review_mysql import mysql_case  # noqa: F401
from test_upload_modality import (
    test_default_text_rejects_images_before_allocating_storage_or_quota as default_case,
    test_modality_cannot_change_to_bypass_upload_or_conflict_with_existing_assets as change_case,
    test_write_phase_rechecks_type_after_storage_preparation as race_case,
)

def test_mysql_modality_admission(mysql_case,monkeypatch):
    app,client,actors=mysql_case
    default_case(app,client,actors,monkeypatch)

def test_mysql_modality_change(mysql_case):
    app,client,actors=mysql_case
    change_case(app,client,actors)

def test_mysql_modality_revalidation(mysql_case):
    app,client,actors=mysql_case
    race_case(app,client,actors)
