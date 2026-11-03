"""Add review leases and reconcile completed batch status."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "f2b8d527c4e3"
down_revision = "e1a7c416b3f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("image_review_items") as batch:
        batch.add_column(sa.Column("claimed_by_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("claim_token", sa.String(32), nullable=True))
        batch.add_column(sa.Column("claim_expires_at", sa.DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True))
        batch.create_foreign_key("fk_review_item_claimant", "users", ["claimed_by_id"], ["id"])
        batch.create_index("ix_review_item_queue", ["batch_id", "status", "claim_expires_at"])
    # 修复历史批次的并发统计偏差；保留草稿，不改变任何标注和审核事件。
    op.execute("""
        UPDATE image_review_batches
        SET status = CASE
          WHEN NOT EXISTS (SELECT 1 FROM image_review_items i
                           WHERE i.batch_id = image_review_batches.id AND i.status <> 'approved')
          THEN 'completed'
          WHEN status = 'completed' THEN 'in_review'
          ELSE status END
        WHERE status <> 'draft'
          AND EXISTS (SELECT 1 FROM image_review_items i WHERE i.batch_id = image_review_batches.id)
    """)


def downgrade():
    with op.batch_alter_table("image_review_items") as batch:
        batch.drop_index("ix_review_item_queue")
        batch.drop_constraint("fk_review_item_claimant", type_="foreignkey")
        batch.drop_column("claim_expires_at")
        batch.drop_column("claim_token")
        batch.drop_column("claimed_by_id")
