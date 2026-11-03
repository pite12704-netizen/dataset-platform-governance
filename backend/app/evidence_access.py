"""Evaluation answers are internal evidence, not a version-wide training grant.

Classification comes from durable evaluation references rather than filenames.
It also protects historical truth assets without guessing from arbitrary names.
"""
from sqlalchemy import select, or_

from .models import Asset, Dataset, GovernanceReport, Job, Version,EvaluationSet
from .policy import manage_dataset, can_read_content


def truth_condition():
    return or_(select(Job.id).where(Job.version_id==Asset.version_id,Job.kind=='evaluate',
        Job.parameters['governance_options']['truth_asset_id'].as_string()==Asset.id).exists(),
        select(GovernanceReport.id).where(GovernanceReport.version_id==Asset.version_id,GovernanceReport.kind=='evaluate',
            GovernanceReport.summary['truth_asset_id'].as_string()==Asset.id).exists(),
        select(EvaluationSet.id).where(EvaluationSet.truth_asset_id==Asset.id).exists())


def is_truth(db,asset):
    return bool(db.scalar(select(Asset.id).where(Asset.id==asset.id,truth_condition())))


def can_read_asset(db,user,asset,settings):
    version=db.get(Version,asset.version_id)
    return can_read_content(db,user,version,settings) and (manage_dataset(db,user,db.get(Dataset,version.dataset_id)) or not is_truth(db,asset))
