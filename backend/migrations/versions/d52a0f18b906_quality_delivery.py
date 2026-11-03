"""Quality tiers, organization publication policy and explicit partial acceptance."""
from alembic import op
import sqlalchemy as sa

revision = 'd52a0f18b906'
down_revision = 'c41f8d29a705'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_job_space_kind_status','jobs',['space_id','kind','status','created_at'])
    op.create_index('ix_job_version_kind_status','jobs',['version_id','kind','status'])
    op.create_index('ix_review_asset_batch_status','image_review_items',['asset_id','batch_id','status'])
    op.add_column('spaces', sa.Column('quality_policy', sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE spaces SET quality_policy='{}' WHERE quality_policy IS NULL"))
    with op.batch_alter_table('spaces') as batch:
        batch.alter_column('quality_policy', existing_type=sa.JSON(), nullable=False)
    op.add_column('versions', sa.Column('quality_tier', sa.String(20), nullable=False, server_default='raw'))
    op.add_column('import_batches', sa.Column('resolution', sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE import_batches SET resolution='{}' WHERE resolution IS NULL"))
    with op.batch_alter_table('import_batches') as batch:
        batch.alter_column('resolution', existing_type=sa.JSON(), nullable=False)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM versions WHERE quality_tier <> 'raw'")):
        raise RuntimeError('Quality-certified versions exist; downgrade would discard policy semantics')
    for name,column in [('spaces','quality_policy'),('import_batches','resolution')]:
        table=sa.table(name,sa.column(column,sa.JSON()))
        if any(value for value, in op.get_bind().execute(sa.select(table.c[column]))):
            raise RuntimeError('Quality policy or partial acceptance exists; downgrade would discard governance state')
    op.drop_column('import_batches', 'resolution')
    op.drop_column('versions', 'quality_tier')
    op.drop_column('spaces', 'quality_policy')
    op.drop_index('ix_review_asset_batch_status',table_name='image_review_items')
    op.drop_index('ix_job_version_kind_status',table_name='jobs')
    op.drop_index('ix_job_space_kind_status',table_name='jobs')
