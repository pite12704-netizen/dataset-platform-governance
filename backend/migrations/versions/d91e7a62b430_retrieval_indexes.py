"""Knowledge documents and durable retrieval index outbox.

Revision ID: d91e7a62b430
Revises: c63f8a29d105
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "d91e7a62b430"
down_revision = "c63f8a29d105"
branch_labels = None
depends_on = None


def upgrade():
    # 固化定义，不导入随业务演进的 ORM 模型，保证未来重放迁移仍得到同一结构。
    dt = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")
    def identity():
        return [sa.Column("id", sa.String(32), primary_key=True), sa.Column("created_at", dt, nullable=False)]
    op.create_table("knowledge_documents", *identity(),
        sa.Column("space_id", sa.String(32), sa.ForeignKey("spaces.id")),
        sa.Column("author_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("body", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("status", sa.String(20), nullable=False))
    op.create_table("retrieval_entries", *identity(),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("version_id", sa.String(32)), sa.Column("space_id", sa.String(32)),
        sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("model_revision", sa.String(128), nullable=False),
        sa.Column("title", sa.String(240), nullable=False), sa.Column("section", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("source_revision", sa.Integer(), nullable=False))
    op.create_table("retrieval_events", *identity(),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cursor", sa.String(32), nullable=False), sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("available_at", dt, nullable=False),
        sa.Column("lease_until", dt), sa.Column("lease_token", sa.String(32)), sa.Column("error_code", sa.String(80)),
        sa.UniqueConstraint("kind", "source_id", name="uq_retrieval_event_source"))
    for table, fields in {
        "knowledge_documents": ["created_at", "space_id"],
        "retrieval_entries": ["created_at", "kind", "space_id", "version_id"],
        "retrieval_events": ["created_at"],
    }.items():
        for field in fields:
            op.create_index(f"ix_{table}_{field}", table, [field])
    op.create_index("ix_retrieval_source", "retrieval_entries", ["kind", "source_id"])
    op.create_index("ix_retrieval_dispatch", "retrieval_events", ["status", "available_at"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM knowledge_documents")):
        raise RuntimeError("Knowledge documents exist; export/backup before a reviewed downgrade")
    for name in ("retrieval_events", "retrieval_entries", "knowledge_documents"):
        op.drop_table(name)
