"""Durable, bounded checkpoints for human-confirmed LangGraph workflows.

Revision ID: e24b7c91d602
Revises: d91e7a62b430
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "e24b7c91d602"
down_revision = "d91e7a62b430"
branch_labels = None
depends_on = None


def upgrade():
    dt = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")

    def identity():
        return [sa.Column("id", sa.String(32), primary_key=True), sa.Column("created_at", dt, nullable=False)]

    op.create_table("agent_graph_workflows", *identity(),
        sa.Column("action_id", sa.String(32), sa.ForeignKey("agent_actions.id"), nullable=False, unique=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("graph_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("lease_token", sa.String(32)),
        sa.Column("lease_until", dt), sa.Column("failure_code", sa.String(60)))
    op.create_table("agent_graph_checkpoints", *identity(),
        sa.Column("workflow_id", sa.String(32), sa.ForeignKey("agent_graph_workflows.id"), nullable=False),
        sa.Column("checkpoint_id", sa.String(64), nullable=False), sa.Column("parent_id", sa.String(64)),
        sa.Column("checkpoint", sa.JSON(), nullable=False), sa.Column("checkpoint_metadata", sa.JSON(), nullable=False),
        sa.UniqueConstraint("workflow_id", "checkpoint_id", name="uq_agent_graph_checkpoint"))
    op.create_table("agent_graph_writes", *identity(),
        sa.Column("workflow_id", sa.String(32), sa.ForeignKey("agent_graph_workflows.id"), nullable=False),
        sa.Column("checkpoint_id", sa.String(64), nullable=False), sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("task_path", sa.String(200), nullable=False),
        sa.Column("write_index", sa.Integer(), nullable=False), sa.Column("channel", sa.String(100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.UniqueConstraint("workflow_id", "checkpoint_id", "task_id", "write_index", name="uq_agent_graph_write"))
    for name in ("agent_graph_workflows", "agent_graph_checkpoints", "agent_graph_writes"):
        op.create_index(f"ix_{name}_created_at", name, ["created_at"])
    op.create_index("ix_agent_graph_workflows_user_id", "agent_graph_workflows", ["user_id"])
    for name in ("agent_graph_checkpoints", "agent_graph_writes"):
        op.create_index(f"ix_{name}_workflow_id", name, ["workflow_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM agent_graph_workflows")):
        raise RuntimeError("Workflow receipts exist; backup and review before downgrade")
    for name in ("agent_graph_writes", "agent_graph_checkpoints", "agent_graph_workflows"):
        op.drop_table(name)
