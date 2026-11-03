"""Add the owner-to-review-team batch workflow."""
from alembic import op
import sqlalchemy as sa

revision = "e1a7c416b3f2"
down_revision = "d963eb284fa1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("image_review_batches") as batch:
        batch.add_column(sa.Column("status", sa.String(20), nullable=False, server_default="draft"))
        batch.add_column(sa.Column("submitted_by_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("submitted_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_image_review_batches_status", ["status"])
        batch.create_foreign_key("fk_review_batch_submitter", "users", ["submitted_by_id"], ["id"])
    # Existing completed work remains completed; untouched model output stays in the owner's draft queue.
    op.execute("""
        UPDATE image_review_batches
        SET status = CASE
          WHEN NOT EXISTS (
            SELECT 1 FROM image_review_items i
            WHERE i.batch_id = image_review_batches.id AND i.status <> 'approved'
          ) THEN 'completed'
          WHEN EXISTS (
            SELECT 1 FROM image_review_items i
            WHERE i.batch_id = image_review_batches.id AND i.status <> 'pending'
          ) THEN 'in_review'
          ELSE 'draft' END
    """)


def downgrade():
    with op.batch_alter_table("image_review_batches") as batch:
        batch.drop_constraint("fk_review_batch_submitter", type_="foreignkey")
        batch.drop_index("ix_image_review_batches_status")
        batch.drop_column("submitted_at")
        batch.drop_column("submitted_by_id")
        batch.drop_column("status")
