"""Persistent assistant conversations and confirmed actions."""
from alembic import op
import sqlalchemy as sa

revision = "b7319a2f0c84"
down_revision = "31556066940d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_conversations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    for field in ("created_at", "user_id", "updated_at"):
        op.create_index(f"ix_agent_conversations_{field}", "agent_conversations", [field])
    op.create_table("agent_actions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("agent_conversations.id")),
        sa.Column("version_id", sa.String(32), sa.ForeignKey("versions.id"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False))
    for field in ("created_at", "user_id", "conversation_id"):
        op.create_index(f"ix_agent_actions_{field}", "agent_actions", [field])
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("conversation_id", sa.String(32), nullable=True))
        batch.create_foreign_key("fk_agent_run_conversation", "agent_conversations", ["conversation_id"], ["id"])
        batch.create_index("ix_agent_runs_conversation_id", ["conversation_id"])


def downgrade():
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_index("ix_agent_runs_conversation_id")
        batch.drop_constraint("fk_agent_run_conversation", type_="foreignkey")
        batch.drop_column("conversation_id")
    op.drop_table("agent_actions")
    op.drop_table("agent_conversations")
