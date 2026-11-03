"""Independent per-pair proposals and final review without rewriting large reports."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME
revision='c63f8a29d105'
down_revision='a85d4e21fa39'
branch_labels=None
depends_on=None
def upgrade():
    op.create_table('duplicate_pair_reviews',sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('created_at',sa.DateTime().with_variant(DATETIME(fsp=6),'mysql'),nullable=False),
        sa.Column('report_id',sa.String(32),sa.ForeignKey('governance_reports.id'),nullable=False),
        sa.Column('pair_id',sa.String(24),nullable=False),
        sa.Column('left_asset_id',sa.String(32),sa.ForeignKey('assets.id'),nullable=False),
        sa.Column('right_asset_id',sa.String(32),sa.ForeignKey('assets.id'),nullable=False),
        sa.Column('proposed_duplicate',sa.Boolean()),sa.Column('annotator_id',sa.String(32),sa.ForeignKey('users.id')),
        sa.Column('approved_duplicate',sa.Boolean()),sa.Column('reviewer_id',sa.String(32),sa.ForeignKey('users.id')),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.UniqueConstraint('report_id','pair_id',name='uq_duplicate_pair_review'))
    op.create_index('ix_duplicate_pair_right','duplicate_pair_reviews',['report_id','right_asset_id'])
    op.create_index('ix_duplicate_pair_pending','duplicate_pair_reviews',['report_id','reviewer_id'])
    op.create_index('ix_duplicate_pair_reviews_created_at','duplicate_pair_reviews',['created_at'])
def downgrade():
    if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM duplicate_pair_reviews')):
        raise RuntimeError('Pair review evidence exists; do not drop it during rollback')
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM versions WHERE CAST(card AS CHAR) LIKE '%_review_pipeline%'")):
        raise RuntimeError('Version review assignments exist; old code would weaken role separation')
    op.drop_table('duplicate_pair_reviews')
