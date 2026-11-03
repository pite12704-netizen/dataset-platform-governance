"""Version-bound image annotations and independent review history."""
from alembic import op
import sqlalchemy as sa

revision = "c852da173e90"
down_revision = "b7319a2f0c84"
branch_labels = None
depends_on = None


def identity():
    return [sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False)]


def ref(name, table, nullable=False, **kw):
    return sa.Column(name, sa.String(32), sa.ForeignKey(table + ".id"), nullable=nullable, **kw)


def upgrade():
    op.create_table("image_review_batches", *identity(),
        ref("version_id", "versions"), ref("creator_id", "users"),
        ref("job_id", "jobs", True, unique=True), ref("report_asset_id", "assets", True, unique=True),
        sa.Column("provenance", sa.JSON(), nullable=False))
    op.create_table("image_review_items", *identity(),
        ref("batch_id", "image_review_batches"), ref("asset_id", "assets"),
        sa.Column("person_key", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False), sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("predicted", sa.JSON(), nullable=False), sa.Column("annotation", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        ref("editor_id", "users", True), ref("reviewer_id", "users", True),
        sa.Column("reviewed_at", sa.DateTime()), sa.UniqueConstraint("batch_id", "asset_id", "person_key"))
    op.create_table("image_review_events", *identity(),
        ref("item_id", "image_review_items"), ref("actor_id", "users"),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("action", sa.String(20), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False), sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False), sa.UniqueConstraint("item_id", "revision"))
    for table, field in [("image_review_batches", "version_id"), ("image_review_items", "batch_id"),
                         ("image_review_events", "item_id")]:
        for column in ("created_at", field):
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in ("image_review_events", "image_review_items", "image_review_batches"):
        op.drop_table(table)
