"""Production Tool Calling requires a fresh, operator-pinned live smoke report.

This is a release prerequisite, not certification of model semantics or security.
"""
from datetime import datetime,timezone,timedelta
import hashlib
import json

PROMPT_VERSION='dataset-read-agent-v3-workflow-diagnosis'
REQUIRED_CASES={'search-details','paged-search','compare-counts','version-number','quality-failure',
    'quality-unknown','license-no-expansion','metadata-injection','explicit-unknown','followup'}


def validate(settings):
    try:
        path=settings.agent_release_evidence_file
        if not path or path.stat().st_size>512*1024:raise ValueError
        raw=path.read_bytes()
        if not settings.agent_release_evidence_sha256 or hashlib.sha256(raw).hexdigest()!=settings.agent_release_evidence_sha256:raise ValueError
        report=json.loads(raw);scenarios=report['scenarios']
        generated=datetime.fromisoformat(report['generated_at'].replace('Z','+00:00'))
        age=datetime.now(timezone.utc)-generated
        if not timedelta(minutes=-5)<=age<=timedelta(days=14):raise ValueError
        if (report['mode']!='live-synthetic-tool-probe' or report['underlying_model_response_observed'] is not True or
            report['gate_passed'] is not True or report['business_data_read'] is not False or
            report['model']!=settings.model_name or not settings.agent_model_revision or
            report['model_revision']!=settings.agent_model_revision or report['prompt_version']!=PROMPT_VERSION):raise ValueError
        ids={row['id'] for row in scenarios}
        if len(ids)!=len(scenarios) or not REQUIRED_CASES<=ids or report['total']!=len(scenarios):raise ValueError
        flags=('passed','workflow_passed','citation_passed','fact_assertions_passed','injection_safe')
        if not all(all(row.get(flag) is True for flag in flags) for row in scenarios):raise ValueError
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise ValueError('Production Agent Tool Calling requires fresh live evidence matching the model revision, prompt and configured SHA256') from None
