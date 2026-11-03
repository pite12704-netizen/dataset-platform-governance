from sqlalchemy import select

import pytest

from app.image_reviews import create_batch
from app.models import (Asset, ImageReviewBatch, ImageReviewImage, ImageReviewItem, Job,
                        Space, VisionCheckpoint)
from test_image_reviews import import_batch, setup_images, submitted_review, transition, finish_images
from test_lifecycle import publish


def test_other_organizations_cannot_push_reviewers_batches_out_of_queue(app, client, actors):
    from datetime import timedelta
    from app.db import now
    from test_lifecycle import create_dataset
    owner, reviewer, _, batch, version = submitted_review(app, client, actors, 1)
    _, foreign_version = create_dataset(client, actors['stranger'], visibility='organization')
    with app.state.db.Session() as db:
        for index in range(205):
            db.add(ImageReviewBatch(version_id=foreign_version['id'], creator_id=actors['stranger']['id'],
                status='submitted', provenance={'origin': 'isolated-foreign-queue'},
                created_at=now() + timedelta(seconds=index + 1)))
        db.commit()
    for actor in [owner, reviewer]:
        response = client.get('/api/v1/image-reviews', headers=actor['headers'])
        assert response.status_code == 200
        assert batch['id'] in {row['id'] for row in response.json()}
        assert all(row['version_id'] != foreign_version['id'] for row in response.json())


def test_duplicate_draft_can_be_discarded_without_bypassing_remaining_reviews(app, client, actors):
    owner, reviewer, _, batch, version = submitted_review(app, client, actors, 1)
    row = batch['items'][0]
    with app.state.db.Session() as db:
        duplicate = create_batch(db, version['id'], owner['id'], [{
            'asset_id': row['asset_id'], 'person_key': '1', 'width': row['width'], 'height': row['height'],
            'annotation': row['annotation']}], {'origin': 'test-repeat-model'})
        db.commit(); duplicate_id = duplicate.id
    root = f'/api/v1/image-reviews/{duplicate_id}'
    assert client.post(root + '/discard', headers=reviewer['headers']).status_code == 403
    assert client.post(f"/api/v1/image-reviews/{batch['id']}/discard", headers=owner['headers']).status_code == 409
    assert transition(client, reviewer, row, 'approve', 1).status_code == 200
    assert client.post(f"/api/v1/versions/{version['id']}/submit", headers=owner['headers']).status_code == 409
    assert client.post(root + '/discard', headers=owner['headers']).json()['status'] == 'discarded'
    assert client.post(root + '/discard', headers=owner['headers']).status_code == 200
    assert client.get(root, headers=reviewer['headers']).status_code == 403
    listing = client.get(f"/api/v1/image-reviews?version_id={version['id']}", headers=owner['headers']).json()
    assert all(not entry['overlaps_other_batch'] for entry in listing)
    assert not client.get(root, headers=owner['headers']).json()['can_review']
    finish_images(client, reviewer, actors["reader"], batch)
    publish(client, owner, reviewer, version)
    with app.state.db.Session() as db:
        assert db.get(ImageReviewBatch, duplicate_id).status == 'discarded'
        assert db.scalar(select(ImageReviewItem).where(ImageReviewItem.batch_id == duplicate_id)) is not None
        assert db.get(Asset, row['asset_id']) is not None


def test_only_draft_covering_images_cannot_be_discarded_or_deleted(app, client, actors):
    owner, _, _, images, report = setup_images(app, client, actors)
    batch = import_batch(client, owner, report)
    assert client.post(f"/api/v1/image-reviews/{batch['id']}/discard", headers=owner['headers']).status_code == 409
    for identifier in [images[0], report]:
        response = client.delete(f'/api/v1/assets/{identifier}', headers=owner['headers'])
        assert response.status_code == 409
        with app.state.db.Session() as db:
            asset = db.get(Asset, identifier)
            assert asset.status == 'clean'
            assert b''.join(app.state.storage.chunks(asset.object_key))


@pytest.mark.parametrize('reference', ['empty_image', 'checkpoint'])
def test_delete_referenced_original_keeps_bytes_and_active_job(app, client, actors, reference):
    from test_upload_task_resilience import package
    owner, _, version, images, _ = setup_images(app, client, actors)
    identifier = images[0]
    with app.state.db.Session() as db:
        asset = db.get(Asset, identifier)
        original = b''.join(app.state.storage.chunks(asset.object_key))
        if reference == 'empty_image':
            batch = create_batch(db, version['id'], owner['id'], [], {'scans': [
                {'asset_id': identifier, 'width': 64, 'height': 80, 'persons': 0}]})
            assert db.scalar(select(ImageReviewImage.id).where(ImageReviewImage.batch_id == batch.id))
            assert not db.scalar(select(ImageReviewItem.id).where(ImageReviewItem.batch_id == batch.id))
        else:
            space_id = db.scalar(select(Job.space_id).where(Job.version_id == version['id']).limit(1))
            job = Job(space_id=space_id, user_id=owner['id'], version_id=version['id'],
                      kind='vision', status='failed', parameters={}, idempotency_key='isolated-checkpoint')
            db.add(job); db.flush()
            db.add(VisionCheckpoint(job_id=job.id, asset_id=identifier, fingerprint='0' * 64,
                                    result={'persons': []}))
        db.commit()
    app.state.settings.task_mode = 'manual'
    queued = package(client, owner, version)
    with app.state.db.Session() as db:
        space_id = db.get(Job, queued['id']).space_id
        reserved = db.get(Space, space_id).reserved_bytes
    response = client.delete('/api/v1/assets/' + identifier, headers=owner['headers'])
    assert response.status_code == 409, response.text
    with app.state.db.Session() as db:
        asset = db.get(Asset, identifier)
        assert asset.status == 'clean'
        assert b''.join(app.state.storage.chunks(asset.object_key)) == original
        assert db.get(Job, queued['id']).status == 'queued'
        assert db.get(Space, space_id).reserved_bytes == reserved


@pytest.mark.parametrize('changed_input', ['image', 'report'])
def test_import_rechecks_inputs_after_reading_before_commit(app, client, actors, monkeypatch, changed_input):
    from sqlalchemy import update
    from app import workflows
    owner, _, _, images, report = setup_images(app, client, actors)
    identifier = images[0] if changed_input == 'image' else report
    original_lock = workflows.locked_version
    def remove_before_lock(db, version_id):
        # Simulate another transaction marking the asset removing after file reads.
        db.execute(update(Asset).where(Asset.id == identifier).values(status='removing')
                   .execution_options(synchronize_session=False))
        db.commit()
        return original_lock(db, version_id)
    monkeypatch.setattr(workflows, 'locked_version', remove_before_lock)
    response = client.post('/api/v1/image-reviews/import', headers=owner['headers'],
                           json={'report_asset_id': report})
    assert response.status_code == 409, response.text
    with app.state.db.Session() as db:
        assert not db.scalar(select(ImageReviewBatch.id))
        assert b''.join(app.state.storage.chunks(db.get(Asset, identifier).object_key))
