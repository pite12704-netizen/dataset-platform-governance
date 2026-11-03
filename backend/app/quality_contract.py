"""Version-wide evidence. Certification is a server-enforced contract, not a card badge."""
import hashlib
import json

from sqlalchemy import select

from .common import fetch, require
from .models import Asset, Dataset, ImageReviewBatch, ImageReviewImage, ImageReviewItem, Space,Version

TIERS = {'raw': 0, 'curated': 1, 'certified': 2}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def media_manifest(assets):
    from .vision import image_assets
    return digest(sorted((a.id, a.sha256, a.size) for a in image_assets(assets)))


def annotation_snapshot(db, version_id, complete=False):
    """All non-discarded batches participate, including negatives and revisions.

    Ordered rows make the snapshot stable across MySQL/SQLite and worker processes.
    Callers performing writes hold Dataset -> Version locks before reading it.
    """
    batches = db.scalars(select(ImageReviewBatch).where(ImageReviewBatch.version_id == version_id,
        ImageReviewBatch.status != 'discarded').order_by(ImageReviewBatch.id)).all()
    ids = [b.id for b in batches]
    pages = db.scalars(select(ImageReviewImage).where(ImageReviewImage.batch_id.in_(ids))
                       .order_by(ImageReviewImage.id)).all() if ids else []
    items = db.scalars(select(ImageReviewItem).where(ImageReviewItem.batch_id.in_(ids))
                       .order_by(ImageReviewItem.id)).all() if ids else []
    from .vision import image_assets
    images = image_assets(db.scalars(select(Asset).where(Asset.version_id == version_id)).all())
    if complete:
        require(images and batches and all(b.status == 'completed' for b in batches),
                '最终交付需要完成此版本全部人工复核批次', 409)
        approved_ids = [p.asset_id for p in pages if p.status == 'approved']
        require(all(p.status == 'approved' for p in pages) and len(approved_ids) == len(set(approved_ids))
                and set(approved_ids) == {a.id for a in images}, '整版图片须完整且不重复地独立复核', 409)
        require(all(i.status in {'approved', 'excluded'} for i in items), '仍有未复核的人物标注', 409)
    payload = {'schema': 'annotation-snapshot-v1', 'version_id': version_id,
        'media_manifest': media_manifest(images),
        'batches': [(b.id, b.status, b.provenance.get('pipeline_fingerprint'),
                     b.provenance.get('prompt_version')) for b in batches],
        'pages': [(p.id, p.batch_id, p.asset_id, p.revision, p.status, p.snapshot) for p in pages],
        'items': [(i.id, i.batch_id, i.asset_id, i.revision, i.status, i.annotation) for i in items]}
    pipeline=fetch(db,Version,version_id).card.get('_review_pipeline')
    if pipeline and pipeline.get('report_id'):
        from .models import GovernanceReport
        report=fetch(db,GovernanceReport,pipeline['report_id'])
        if complete:require(report.status=='approved','联合去重关系尚未全部独立确认',409)
        if report.status=='approved':
            from .governance import retained_images
            kept=retained_images(db,images,{'dedup_report_id':report.id});identifiers={a.id for a in kept}
            require(not complete or all(not p.snapshot.get('dedup_excluded') for p in pages if p.asset_id in identifiers),'代表图尚未完成标注，请退回补标',409)
            payload['dedup']={'report_id':report.id,'decision_hash':digest(report.decision),'retained_ids':sorted(identifiers),'source_images':len(images)}
            images=kept;pages=[p for p in pages if p.asset_id in identifiers];items=[i for i in items if i.asset_id in identifiers]
    return {'hash': digest(payload), 'payload': payload, 'images': images, 'pages': pages, 'items': items}


def publication_policy(db, version, settings):
    dataset = fetch(db, Dataset, version.dataset_id)
    space = fetch(db, Space, dataset.space_id)
    policy = space.quality_policy or {}
    keys = ('min_truth_images', 'min_positives', 'min_negatives', 'min_attribute_coverage', 'min_precision', 'min_recall')
    thresholds = {key: max(getattr(settings, 'quality_' + key), policy.get(key, 0)) for key in keys}
    return {'schema': 'quality-policy-v1', 'space_id': space.id, 'revision': policy.get('revision', 0),
            'minimum_tier': policy.get('minimum_tier', 'raw'), 'require_blind_evaluation':bool(policy.get('require_blind_evaluation')), **thresholds}


def require_tier(db, version):
    ds = fetch(db, Dataset, version.dataset_id)
    minimum = (fetch(db, Space, ds.space_id, lock=True).quality_policy or {}).get('minimum_tier', 'raw')
    require(version.quality_tier in TIERS and TIERS[version.quality_tier] >= TIERS[minimum],
            '版本等级低于组织发布策略，请提高等级并完成验收', 409)


def effective_required(version):
    return version.quality_tier == 'certified' or bool(version.card.get('quality_gate_required'))


def blind_report_valid(db,report):
    identifier=report.summary.get('evaluation_set_id')
    if not identifier:return True
    from .models import EvaluationSet
    item=db.get(EvaluationSet,identifier)
    truth=db.get(Asset,item.truth_asset_id) if item else None
    return bool(item and item.status=='used' and truth and truth.status=='clean' and
                truth.sha256==item.truth_sha256==report.summary.get('truth_sha256'))


def require_delivery_evidence(db, version, assets):
    if version.quality_tier == 'raw':return
    from .models import GovernanceReport
    reports=db.scalars(select(GovernanceReport).where(GovernanceReport.version_id==version.id,
        GovernanceReport.kind=='dedup',GovernanceReport.status=='approved')).all()
    pipeline=version.card.get('_review_pipeline') or {}
    if pipeline.get('report_id'):
        require(any(r.id==pipeline['report_id'] and r.summary.get('media_manifest')==media_manifest(assets)
                    and r.reviewer_id for r in reports),'高质量交付需要当前联合作业报告完成独立确认',409)
        # Joint review retains raw originals; the frozen training snapshot contains only representatives.
        snapshot=annotation_snapshot(db,version.id,complete=True)
        require(snapshot['payload'].get('dedup',{}).get('report_id')==pipeline['report_id'],
                '正式训练快照未绑定当前去重结论',409)
    else:
        require(any(r.summary.get('media_manifest')==media_manifest(assets) and r.reviewer_id and
                    not r.decision.get('duplicate_pairs') for r in reports),
                '高质量交付需要有效独立去重报告且无已确认重复；请清理到派生版本后重新检查',409)
    from .delivery import current_release
    require(current_release(db,version,assets),'请构建与整版审核快照一致的正式交付包',409)
    if version.quality_tier=='certified':
        source=version.card.get('_source_approval',{})
        require(source.get('approved') and source.get('reviewer_id') and
                source.get('source_hash')==digest({'source':version.source,'license':version.license}),
                '认证交付需完成当前来源与许可的独立审核',409)
