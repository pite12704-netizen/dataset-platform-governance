"""【学习标注：领域数据模型】平台所有持久化对象都在这里。

可按四组理解：身份(User/Session/Identity)、数据资产(Dataset/Version/Asset)、
业务流程(Publication/Application/Grant/Job)、治理与 Agent(Audit/AgentAction/AgentRun)。
"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column as col, relationship
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT

# MySQL 保留微秒，迁移后审批/租约/修订时间不丢失精度。
DateTime = DateTime().with_variant(DATETIME(fsp=6), "mysql")

from .db import Base, now, uid


class Identified:
    id: Mapped[str] = col(String(32), primary_key=True, default=uid)
    created_at: Mapped[datetime] = col(DateTime, default=now, index=True)


class ControlLock(Identified, Base):
    """Stable transactional sentinels; no credentials or runtime ownership."""
    __tablename__ = "control_locks"


class KnowledgeDocument(Identified, Base):
    """受控知识正文；发布编辑有 revision，引用始终指向确切文档版本。"""
    __tablename__ = "knowledge_documents"
    space_id: Mapped[str | None] = col(ForeignKey("spaces.id"), index=True)
    author_id: Mapped[str] = col(ForeignKey("users.id"))
    title: Mapped[str] = col(String(200))
    body: Mapped[str] = col(Text)
    revision: Mapped[int] = col(Integer, default=1)
    status: Mapped[str] = col(String(20), default="draft")


class RetrievalEntry(Identified, Base):
    """一条检索单元。正文/来源在 SQL，向量只在外部索引；不保存可下载 URL。"""
    __tablename__ = "retrieval_entries"
    __table_args__ = (Index("ix_retrieval_source", "kind", "source_id"),)
    kind: Mapped[str] = col(String(20), index=True)
    source_id: Mapped[str] = col(String(32))
    version_id: Mapped[str | None] = col(String(32), index=True)
    space_id: Mapped[str | None] = col(String(32), index=True)
    fingerprint: Mapped[str] = col(String(64))
    model_revision: Mapped[str] = col(String(128))
    title: Mapped[str] = col(String(240))
    section: Mapped[str] = col(String(200), default="")
    body: Mapped[str] = col(Text, default="")
    source_revision: Mapped[int] = col(Integer, default=0)


class RetrievalEvent(Identified, Base):
    """小行事务 outbox。generation + lease token 防止旧 Worker 覆盖新修改。"""
    __tablename__ = "retrieval_events"
    __table_args__ = (UniqueConstraint("kind", "source_id"),
                     Index("ix_retrieval_dispatch", "status", "available_at"))
    kind: Mapped[str] = col(String(20))
    source_id: Mapped[str] = col(String(32))
    generation: Mapped[int] = col(Integer, default=1)
    status: Mapped[str] = col(String(20), default="queued")
    cursor: Mapped[str] = col(String(32), default="")
    processed: Mapped[int] = col(Integer, default=0)
    attempts: Mapped[int] = col(Integer, default=0)
    available_at: Mapped[datetime] = col(DateTime, default=now)
    lease_until: Mapped[datetime | None] = col(DateTime)
    lease_token: Mapped[str | None] = col(String(32))
    error_code: Mapped[str | None] = col(String(80))


class SpaceTransfer(Identified, Base):
    __tablename__ = 'space_transfers'
    space_id: Mapped[str] = col(ForeignKey('spaces.id'),index=True)
    requester_id: Mapped[str] = col(ForeignKey('users.id'))
    target_user_id: Mapped[str] = col(ForeignKey('users.id'),index=True)
    reason: Mapped[str] = col(Text)
    status: Mapped[str] = col(String(20),default='pending')
    expires_at: Mapped[datetime] = col(DateTime)
    decided_at: Mapped[datetime | None] = col(DateTime)


class User(Identified, Base):
    """账号；平台管理员只是平台角色，组织角色保存在 Membership。"""
    __tablename__ = "users"
    email: Mapped[str | None] = col(String(254), unique=True)
    username: Mapped[str | None] = col(String(32), unique=True)
    must_change_password: Mapped[bool] = col(Boolean, default=False, server_default='0')
    name: Mapped[str] = col(String(100))
    password_hash: Mapped[str] = col(String(256))
    active: Mapped[bool] = col(Boolean, default=True)
    verified: Mapped[bool] = col(Boolean, default=False)
    platform_admin: Mapped[bool] = col(Boolean, default=False)
    mfa_secret: Mapped[str | None] = col(String(512))
    mfa_enabled: Mapped[bool] = col(Boolean, default=False)
    mfa_last_counter: Mapped[int] = col(BigInteger, default=-1)
    mfa_recovery_hashes: Mapped[list] = col(JSON, default=list)


class LoginSession(Identified, Base):
    __tablename__ = "login_sessions"
    user_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = col(DateTime)
    revoked: Mapped[bool] = col(Boolean, default=False)
    mfa: Mapped[bool] = col(Boolean, default=False)
    recovery_only: Mapped[bool] = col(Boolean, default=False, server_default='0')


class Challenge(Identified, Base):
    __tablename__ = "challenges"
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    kind: Mapped[str] = col(String(30))
    token_hash: Mapped[str] = col(String(64), unique=True)
    expires_at: Mapped[datetime] = col(DateTime)
    consumed: Mapped[bool] = col(Boolean, default=False)
    payload: Mapped[dict] = col(JSON, default=dict)


class Identity(Identified, Base):
    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    issuer: Mapped[str] = col(String(255))
    subject: Mapped[str] = col(String(255))
    user_id: Mapped[str] = col(ForeignKey("users.id"))


class Space(Identified, Base):
    __tablename__ = "spaces"
    name: Mapped[str] = col(String(150))
    kind: Mapped[str] = col(String(20), default="organization")
    owner_id: Mapped[str] = col(ForeignKey("users.id"))
    quota_bytes: Mapped[int] = col(BigInteger, default=10 * 1024**3)
    used_bytes: Mapped[int] = col(BigInteger, default=0)
    reserved_bytes: Mapped[int] = col(BigInteger, default=0)
    quality_policy: Mapped[dict] = col(JSON, default=dict)


class Membership(Identified, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("space_id", "user_id"),)
    space_id: Mapped[str] = col(ForeignKey("spaces.id"), index=True)
    user_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    role: Mapped[str] = col(String(30), default="member")
    active: Mapped[bool] = col(Boolean, default=True)


class Project(Identified, Base):
    __tablename__ = "projects"
    space_id: Mapped[str] = col(ForeignKey("spaces.id"), index=True)
    name: Mapped[str] = col(String(150))


class ProjectMember(Identified, Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    project_id: Mapped[str] = col(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    can_apply: Mapped[bool] = col(Boolean, default=False)
    active: Mapped[bool] = col(Boolean, default=True)


class Dataset(Identified, Base):
    """长期数据资产；介绍、可见范围和获取方式属于数据集。"""
    __tablename__ = "datasets"
    space_id: Mapped[str] = col(ForeignKey("spaces.id"), index=True)
    owner_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    name: Mapped[str] = col(String(200), index=True)
    description: Mapped[str] = col(Text, default="")
    modality: Mapped[str] = col(String(30), default="text")
    category: Mapped[str] = col(String(80), default="通用")
    tags: Mapped[list] = col(JSON, default=list)
    visibility: Mapped[str] = col(String(20), default="private")
    access_mode: Mapped[str] = col(String(20), default="application")
    audience_users: Mapped[list] = col(JSON, default=list)
    audience_projects: Mapped[list] = col(JSON, default=list)
    status: Mapped[str] = col(String(20), default="active")


class Version(Identified, Base):
    """不可变发布快照；发布后不能覆盖，变更必须创建新版本。"""
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("dataset_id", "number"),)
    dataset_id: Mapped[str] = col(ForeignKey("datasets.id"), index=True)
    number: Mapped[int] = col(Integer)
    creator_id: Mapped[str] = col(ForeignKey("users.id"))
    status: Mapped[str] = col(String(30), default="draft")
    quality_tier: Mapped[str] = col(String(20), default='raw', server_default='raw')
    license: Mapped[str] = col(String(150))
    source: Mapped[str] = col(Text)
    card: Mapped[dict] = col(JSON, default=dict)
    changelog: Mapped[str] = col(Text, default="")
    parent_version_id: Mapped[str | None] = col(ForeignKey("versions.id"))
    lineage: Mapped[dict] = col(JSON, default=dict)
    manifest_hash: Mapped[str | None] = col(String(64))
    published_at: Mapped[datetime | None] = col(DateTime)


class Asset(Identified, Base):
    """版本中的文件记录；object_key 只在服务端使用，不返回客户端。"""
    __tablename__ = "assets"
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    filename: Mapped[str] = col(String(255))
    object_key: Mapped[str] = col(String(512), unique=True)
    size: Mapped[int] = col(BigInteger)
    sha256: Mapped[str] = col(String(64), index=True)
    media_type: Mapped[str] = col(String(100), default="application/octet-stream")
    status: Mapped[str] = col(String(30), default="quarantine")
    preview_allowed: Mapped[bool] = col(Boolean, default=False)
    checks: Mapped[dict] = col(JSON, default=dict)


class Thumbnail(Identified, Base):
    __tablename__='thumbnails'
    __table_args__=(UniqueConstraint('asset_id','profile'),)
    asset_id: Mapped[str]=col(ForeignKey('assets.id'),index=True)
    profile: Mapped[str]=col(String(40))
    source_sha256: Mapped[str]=col(String(64))
    object_key: Mapped[str]=col(String(512),unique=True)
    sha256: Mapped[str]=col(String(64))
    size: Mapped[int]=col(BigInteger)


class EvaluationSet(Identified, Base):
    __tablename__='evaluation_sets'
    target_version_id: Mapped[str]=col(ForeignKey('versions.id'),index=True)
    creator_id: Mapped[str]=col(ForeignKey('users.id'))
    reviewer_id: Mapped[str]=col(ForeignKey('users.id'))
    status: Mapped[str]=col(String(20),default='draft')
    sample_ids: Mapped[list]=col(JSON)
    population_manifest: Mapped[str]=col(String(64))
    truth_asset_id: Mapped[str|None]=col(ForeignKey('assets.id'))
    truth_sha256: Mapped[str|None]=col(String(64))
    annotation_manifest: Mapped[str|None]=col(String(64))


class Upload(Identified, Base):
    __tablename__ = "uploads"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    filename: Mapped[str] = col(String(255))
    size: Mapped[int] = col(BigInteger)
    sha256: Mapped[str] = col(String(64))
    object_key: Mapped[str] = col(String(512))
    storage_upload_id: Mapped[str] = col(Text)
    part_size: Mapped[int] = col(Integer)
    parts: Mapped[dict] = col(JSON, default=dict)
    status: Mapped[str] = col(String(30), default="uploading")
    idempotency_key: Mapped[str] = col(String(100))
    preview_allowed: Mapped[bool] = col(Boolean, default=False)
    asset_id: Mapped[str | None] = col(ForeignKey("assets.id"))
    expires_at: Mapped[datetime] = col(DateTime)
    finalize_token: Mapped[str | None] = col(String(32))
    finalize_until: Mapped[datetime | None] = col(DateTime)


class Publication(Identified, Base):
    """发布审核记录，与 Version.status 配合形成发布状态机。"""
    __tablename__ = "publications"
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    submitter_id: Mapped[str] = col(ForeignKey("users.id"))
    reviewer_id: Mapped[str | None] = col(ForeignKey("users.id"))
    status: Mapped[str] = col(String(20), default="pending")
    comment: Mapped[str] = col(Text, default="")
    decided_at: Mapped[datetime | None] = col(DateTime)


class Application(Identified, Base):
    """数据使用申请；审批通过后才创建独立 Grant。"""
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("applicant_id", "idempotency_key"),)
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    applicant_id: Mapped[str] = col(ForeignKey("users.id"))
    project_id: Mapped[str | None] = col(ForeignKey("projects.id"))
    purpose: Mapped[str] = col(Text)
    duration_days: Mapped[int] = col(Integer)
    status: Mapped[str] = col(String(20), default="pending")
    reviewer_id: Mapped[str | None] = col(ForeignKey("users.id"))
    comment: Mapped[str] = col(Text, default="")
    decided_at: Mapped[datetime | None] = col(DateTime)
    idempotency_key: Mapped[str] = col(String(100))


class Grant(Identified, Base):
    """绑定具体版本、个人或项目的访问授权，可到期和撤销。"""
    __tablename__ = "grants"
    application_id: Mapped[str] = col(ForeignKey("applications.id"), unique=True)
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    user_id: Mapped[str | None] = col(ForeignKey("users.id"))
    project_id: Mapped[str | None] = col(ForeignKey("projects.id"))
    expires_at: Mapped[datetime] = col(DateTime)
    revoked_at: Mapped[datetime | None] = col(DateTime)


class Acceptance(Identified, Base):
    __tablename__ = "acceptances"
    __table_args__ = (UniqueConstraint("user_id", "version_id"),)
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    version_id: Mapped[str] = col(ForeignKey("versions.id"))
    license: Mapped[str] = col(String(150))


class Job(Identified, Base):
    """可恢复的后台任务；包含幂等键、租约、尝试次数、进度与结果。"""
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),
        Index('ix_job_space_kind_status','space_id','kind','status','created_at'),
        Index('ix_job_version_kind_status','version_id','kind','status'))
    space_id: Mapped[str] = col(ForeignKey("spaces.id"), index=True)
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    kind: Mapped[str] = col(String(30))
    parameters: Mapped[dict] = col(JSON, default=dict)
    status: Mapped[str] = col(String(30), default="queued", index=True)
    progress: Mapped[int] = col(Integer, default=0)
    attempts: Mapped[int] = col(Integer, default=0)
    idempotency_key: Mapped[str] = col(String(100))
    lease_until: Mapped[datetime | None] = col(DateTime)
    run_token: Mapped[str | None] = col(String(32))
    result: Mapped[dict] = col(JSON, default=dict)
    error: Mapped[str | None] = col(Text)
    finished_at: Mapped[datetime | None] = col(DateTime)
    reserved_bytes: Mapped[int] = col(BigInteger, default=0)
    artifact_bytes: Mapped[int] = col(BigInteger, default=0)
    artifact_expires_at: Mapped[datetime | None] = col(DateTime)
    dispatch_token: Mapped[str | None] = col(String(32))
    next_dispatch_at: Mapped[datetime | None] = col(DateTime, index=True)
    dispatch_attempts: Mapped[int] = col(Integer, default=0, server_default='0')
    payloads: Mapped[list['JobPayload']] = relationship(back_populates='job',cascade='all, delete-orphan',passive_deletes=True)


class JobPayload(Identified, Base):
    """不可变冷载荷：独立于高频更新的任务行；仅按任务权限读取。"""
    __tablename__='job_payloads'
    __table_args__=(UniqueConstraint('job_id','field','key','sha256',name='uq_job_payload_content'),)
    job_id: Mapped[str] = col(ForeignKey('jobs.id',ondelete='CASCADE'),index=True)
    field: Mapped[str] = col(String(16))
    key: Mapped[str] = col(String(100))
    sha256: Mapped[str] = col(String(64))
    raw_bytes: Mapped[int] = col(BigInteger)
    content: Mapped[str] = col(Text().with_variant(LONGTEXT(),'mysql'))
    job: Mapped['Job'] = relationship(back_populates='payloads')


class QualityReport(Identified, Base):
    __tablename__ = "quality_reports"
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    job_id: Mapped[str] = col(ForeignKey("jobs.id"), unique=True)
    summary: Mapped[dict] = col(JSON)


class Audit(Identified, Base):
    __tablename__ = "audits"
    actor_id: Mapped[str | None] = col(ForeignKey("users.id"), index=True)
    space_id: Mapped[str | None] = col(ForeignKey("spaces.id"), index=True)
    action: Mapped[str] = col(String(100), index=True)
    resource_id: Mapped[str | None] = col(String(32))
    details: Mapped[dict] = col(JSON, default=dict)


class Notification(Identified, Base):
    __tablename__ = "notifications"
    user_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    title: Mapped[str] = col(String(200))
    body: Mapped[str] = col(Text)
    read: Mapped[bool] = col(Boolean, default=False)


class Favorite(Identified, Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "dataset_id"),)
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    dataset_id: Mapped[str] = col(ForeignKey("datasets.id"))


class Report(Identified, Base):
    __tablename__ = "reports"
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    dataset_id: Mapped[str] = col(ForeignKey("datasets.id"))
    reason: Mapped[str] = col(Text)
    status: Mapped[str] = col(String(20), default="pending")
    resolution: Mapped[str] = col(Text, default="")


class ServiceCredential(Identified, Base):
    __tablename__ = "service_credentials"
    user_id: Mapped[str] = col(ForeignKey("users.id"))
    name: Mapped[str] = col(String(100))
    token_hash: Mapped[str] = col(String(64), unique=True)
    scopes: Mapped[list] = col(JSON)
    expires_at: Mapped[datetime] = col(DateTime)
    revoked: Mapped[bool] = col(Boolean, default=False)


class AgentConversation(Identified, Base):
    __tablename__ = "agent_conversations"
    user_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    title: Mapped[str] = col(String(100), default="新对话")
    updated_at: Mapped[datetime] = col(DateTime, default=now, index=True)
    active_run_id: Mapped[str | None] = col(String(32))
    active_until: Mapped[datetime | None] = col(DateTime)
    revision: Mapped[int] = col(Integer, default=0)


class AgentAction(Identified, Base):
    """Agent 写操作的确认单；先预览，确认后再转换为申请或任务。"""
    __tablename__ = "agent_actions"
    user_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str | None] = col(ForeignKey("agent_conversations.id"), index=True)
    version_id: Mapped[str] = col(ForeignKey("versions.id"))
    kind: Mapped[str] = col(String(30))
    payload: Mapped[dict] = col(JSON)
    summary: Mapped[dict] = col(JSON)
    status: Mapped[str] = col(String(20), default="pending")
    expires_at: Mapped[datetime] = col(DateTime)
    result: Mapped[dict] = col(JSON, default=dict)


class AgentRun(Identified, Base):
    """Agent/MCP 调用轨迹；记录输入、输出、模型、耗时和失败状态。"""
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("user_id", "request_id", name="uq_agent_request"),)
    user_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str | None] = col(ForeignKey("agent_conversations.id"), index=True)
    source: Mapped[str] = col(String(20))
    tool: Mapped[str] = col(String(80))
    model: Mapped[str] = col(String(100), default="catalog-search")
    inputs: Mapped[dict] = col(JSON, default=dict)
    outputs: Mapped[dict] = col(JSON, default=dict)
    status: Mapped[str] = col(String(20), default="completed")
    duration_ms: Mapped[int] = col(Integer, default=0)
    request_id: Mapped[str | None] = col(String(64))


class AgentGraphWorkflow(Identified, Base):
    """LangGraph 的执行锁与小型状态；业务确认和授权仍以 AgentAction 为准。"""
    __tablename__ = "agent_graph_workflows"
    action_id: Mapped[str] = col(ForeignKey("agent_actions.id"), unique=True)
    user_id: Mapped[str] = col(ForeignKey("users.id"), index=True)
    graph_version: Mapped[str] = col(String(80))
    status: Mapped[str] = col(String(30), default="created")
    lease_token: Mapped[str | None] = col(String(32))
    lease_until: Mapped[datetime | None] = col(DateTime)
    failure_code: Mapped[str | None] = col(String(60))


class AgentGraphCheckpoint(Identified, Base):
    """有界 JSON 检查点；不保存图片清单、文件内容、模型凭据或 pickle。"""
    __tablename__ = "agent_graph_checkpoints"
    __table_args__ = (UniqueConstraint("workflow_id", "checkpoint_id", name="uq_agent_graph_checkpoint"),)
    workflow_id: Mapped[str] = col(ForeignKey("agent_graph_workflows.id"), index=True)
    checkpoint_id: Mapped[str] = col(String(64))
    parent_id: Mapped[str | None] = col(String(64))
    checkpoint: Mapped[dict] = col(JSON)
    checkpoint_metadata: Mapped[dict] = col(JSON)


class AgentGraphWrite(Identified, Base):
    """节点写入及 interrupt/resume 回执，与检查点分开保留以恢复半途执行。"""
    __tablename__ = "agent_graph_writes"
    __table_args__ = (UniqueConstraint("workflow_id", "checkpoint_id", "task_id", "write_index",
                                      name="uq_agent_graph_write"),)
    workflow_id: Mapped[str] = col(ForeignKey("agent_graph_workflows.id"), index=True)
    checkpoint_id: Mapped[str] = col(String(64))
    task_id: Mapped[str] = col(String(64))
    task_path: Mapped[str] = col(String(200), default="")
    write_index: Mapped[int] = col(Integer)
    channel: Mapped[str] = col(String(100))
    value: Mapped[dict] = col(JSON)


class VisionCheckpoint(Identified, Base):
    """逐图提交的推理检查点；旧执行不能覆盖新执行，不跨用户共享内容。"""
    __tablename__ = "vision_checkpoints"
    __table_args__ = (UniqueConstraint("job_id", "asset_id"),)
    job_id: Mapped[str] = col(ForeignKey("jobs.id"), index=True)
    asset_id: Mapped[str] = col(ForeignKey("assets.id"))
    fingerprint: Mapped[str] = col(String(64))
    result: Mapped[dict] = col(JSON)


class ImageReviewImage(Identified, Base):
    """整图完整性复核，包含没有候选人物的图片。"""
    __tablename__ = "image_review_images"
    __table_args__ = (UniqueConstraint("batch_id", "asset_id"),
                     Index("ix_review_image_queue", "batch_id", "status", "claim_expires_at"))
    batch_id: Mapped[str] = col(ForeignKey("image_review_batches.id"), index=True)
    asset_id: Mapped[str] = col(ForeignKey("assets.id"))
    width: Mapped[int] = col(Integer)
    height: Mapped[int] = col(Integer)
    status: Mapped[str] = col(String(20), default="pending")
    revision: Mapped[int] = col(Integer, default=0)
    snapshot: Mapped[dict] = col(JSON, default=dict)
    editor_id: Mapped[str | None] = col(ForeignKey("users.id"))
    reviewer_id: Mapped[str | None] = col(ForeignKey("users.id"))
    claimed_by_id: Mapped[str | None] = col(ForeignKey("users.id"))
    claim_token: Mapped[str | None] = col(String(32))
    claim_expires_at: Mapped[datetime | None] = col(DateTime)


class ImageReviewImageEvent(Identified, Base):
    __tablename__ = "image_review_image_events"
    __table_args__ = (UniqueConstraint("image_id", "revision"),)
    image_id: Mapped[str] = col(ForeignKey("image_review_images.id"), index=True)
    actor_id: Mapped[str] = col(ForeignKey("users.id"))
    revision: Mapped[int] = col(Integer)
    action: Mapped[str] = col(String(30))
    before: Mapped[dict] = col(JSON)
    after: Mapped[dict] = col(JSON)


class GovernanceReport(Identified, Base):
    """版本绑定的去重、划分和真值评测证据；内容由受控任务生成。"""
    __tablename__ = "governance_reports"
    job_id: Mapped[str] = col(ForeignKey("jobs.id"), unique=True)
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    kind: Mapped[str] = col(String(30))
    summary: Mapped[dict] = col(JSON)
    status: Mapped[str] = col(String(20), default="pending")
    reviewer_id: Mapped[str | None] = col(ForeignKey("users.id"))
    decision: Mapped[dict] = col(JSON, default=dict)


class DuplicatePairReview(Identified, Base):
    """Small mutable per-pair state; never renew/rewrite the full candidate report."""
    __tablename__='duplicate_pair_reviews'
    __table_args__=(UniqueConstraint('report_id','pair_id',name='uq_duplicate_pair_review'),
        Index('ix_duplicate_pair_right','report_id','right_asset_id'),
        Index('ix_duplicate_pair_pending','report_id','reviewer_id'))
    report_id: Mapped[str]=col(ForeignKey('governance_reports.id'))
    pair_id: Mapped[str]=col(String(24))
    left_asset_id: Mapped[str]=col(ForeignKey('assets.id'))
    right_asset_id: Mapped[str]=col(ForeignKey('assets.id'))
    proposed_duplicate: Mapped[bool|None]=col(Boolean)
    annotator_id: Mapped[str|None]=col(ForeignKey('users.id'))
    approved_duplicate: Mapped[bool|None]=col(Boolean)
    reviewer_id: Mapped[str|None]=col(ForeignKey('users.id'))
    revision: Mapped[int]=col(Integer,default=0)


class ImageReviewBatch(Identified, Base):
    """【学习标注】发布版本之外的标注工作区；不覆盖已发布文件。"""
    __tablename__ = "image_review_batches"
    version_id: Mapped[str] = col(ForeignKey("versions.id"), index=True)
    creator_id: Mapped[str] = col(ForeignKey("users.id"))
    job_id: Mapped[str | None] = col(ForeignKey("jobs.id"), unique=True)
    report_asset_id: Mapped[str | None] = col(ForeignKey("assets.id"), unique=True)
    provenance: Mapped[dict] = col(JSON, default=dict)
    status: Mapped[str] = col(String(20), default="draft", index=True)
    submitted_by_id: Mapped[str | None] = col(ForeignKey("users.id"))
    submitted_at: Mapped[datetime | None] = col(DateTime)


class ImageReviewItem(Identified, Base):
    __tablename__ = "image_review_items"
    __table_args__ = (UniqueConstraint("batch_id", "asset_id", "person_key"),
                      Index("ix_review_item_queue", "batch_id", "status", "claim_expires_at"),
                      Index('ix_review_asset_batch_status','asset_id','batch_id','status'))
    batch_id: Mapped[str] = col(ForeignKey("image_review_batches.id"), index=True)
    asset_id: Mapped[str] = col(ForeignKey("assets.id"))
    person_key: Mapped[str] = col(String(64))
    width: Mapped[int] = col(Integer)
    height: Mapped[int] = col(Integer)
    predicted: Mapped[dict] = col(JSON)
    annotation: Mapped[dict] = col(JSON)
    status: Mapped[str] = col(String(20), default="pending")
    revision: Mapped[int] = col(Integer, default=0)
    editor_id: Mapped[str | None] = col(ForeignKey("users.id"))
    reviewer_id: Mapped[str | None] = col(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = col(DateTime)
    claimed_by_id: Mapped[str | None] = col(ForeignKey("users.id"))
    claim_token: Mapped[str | None] = col(String(32))
    claim_expires_at: Mapped[datetime | None] = col(DateTime)


class ImageReviewEvent(Identified, Base):
    """只追加纠错和复核事件，revision 用于防止并发覆盖。"""
    __tablename__ = "image_review_events"
    __table_args__ = (UniqueConstraint("item_id", "revision"),)
    item_id: Mapped[str] = col(ForeignKey("image_review_items.id"), index=True)
    actor_id: Mapped[str] = col(ForeignKey("users.id"))
    revision: Mapped[int] = col(Integer)
    action: Mapped[str] = col(String(20))
    before: Mapped[dict] = col(JSON)
    after: Mapped[dict] = col(JSON)
    note: Mapped[str] = col(Text, default="")


class MailOutbox(Identified, Base):
    __tablename__ = "mail_outbox"
    recipient: Mapped[str] = col(String(254))
    subject: Mapped[str] = col(String(200))
    body: Mapped[str] = col(Text)
    sent_at: Mapped[datetime | None] = col(DateTime)
    attempts: Mapped[int] = col(Integer, default=0)
    delivery_mode: Mapped[str | None] = col(String(20))
    last_error: Mapped[str | None] = col(String(80))
    delivery_token: Mapped[str | None] = col(String(32))
    lease_until: Mapped[datetime | None] = col(DateTime)
    next_attempt_at: Mapped[datetime | None] = col(DateTime, index=True)


class ImportBatch(Identified, Base):
    """客户侧推送的清单及专用短期凭据；不保存源数据库或对象存储密钥。"""
    __tablename__ = "import_batches"
    __table_args__ = (UniqueConstraint('user_id', 'idempotency_key'),)
    version_id: Mapped[str] = col(ForeignKey('versions.id'), index=True)
    user_id: Mapped[str] = col(ForeignKey('users.id'), index=True)
    source_id: Mapped[str] = col(String(100))
    expected_files: Mapped[int] = col(Integer)
    expected_bytes: Mapped[int] = col(BigInteger)
    status: Mapped[str] = col(String(30), default='uploading')
    sealed: Mapped[bool] = col(Boolean, default=False)
    manifest_hash: Mapped[str | None] = col(String(64))
    idempotency_key: Mapped[str] = col(String(100))
    token_hash: Mapped[str] = col(String(64), unique=True)
    expires_at: Mapped[datetime] = col(DateTime)
    revoked: Mapped[bool] = col(Boolean, default=False)
    resolution: Mapped[dict] = col(JSON, default=dict)


class ImportItem(Identified, Base):
    """外部记录到受控 Upload/Asset 的稳定映射；来源标签不是人工批准结果。"""
    __tablename__ = 'import_items'
    __table_args__ = (UniqueConstraint('batch_id', 'external_id'),
                     Index('ix_import_item_batch_created', 'batch_id', 'created_at', 'id'))
    batch_id: Mapped[str] = col(ForeignKey('import_batches.id'), index=True)
    external_id: Mapped[str] = col(String(200))
    source_revision: Mapped[str] = col(String(200), default='')
    filename: Mapped[str] = col(String(240))
    size: Mapped[int] = col(BigInteger)
    sha256: Mapped[str] = col(String(64))
    source_group: Mapped[str] = col(String(200), default='')
    source: Mapped[str] = col(String(2000), default='')
    license: Mapped[str] = col(String(1000), default='')
    source_annotation: Mapped[dict] = col(JSON, default=dict)
    upload_id: Mapped[str | None] = col(ForeignKey('uploads.id'), unique=True)
    asset_id: Mapped[str | None] = col(ForeignKey('assets.id'), unique=True)
    scan_job_id: Mapped[str | None] = col(ForeignKey('jobs.id'))
