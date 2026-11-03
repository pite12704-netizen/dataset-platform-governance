"""Controlled import batches and external record to asset mappings."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'b9e81a7302fd'
down_revision = 'a6c9e341d508'
branch_labels = None
depends_on = None


def identity():
    return [sa.Column('id', sa.String(32), primary_key=True),
            sa.Column('created_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False)]


def upgrade():
    op.create_table('import_batches', *identity(),
        sa.Column('version_id', sa.String(32), sa.ForeignKey('versions.id'), nullable=False),
        sa.Column('user_id', sa.String(32), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source_id', sa.String(100), nullable=False),
        sa.Column('expected_files', sa.Integer(), nullable=False),
        sa.Column('expected_bytes', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('sealed', sa.Boolean(), nullable=False),
        sa.Column('manifest_hash', sa.String(64)),
        sa.Column('idempotency_key', sa.String(100), nullable=False),
        sa.Column('token_hash', sa.String(64), unique=True, nullable=False),
        sa.Column('expires_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('user_id', 'idempotency_key'))
    for column in ('created_at', 'version_id', 'user_id'):
        op.create_index('ix_import_batches_' + column, 'import_batches', [column])
    op.create_table('import_items', *identity(),
        sa.Column('batch_id', sa.String(32), sa.ForeignKey('import_batches.id'), nullable=False),
        sa.Column('external_id', sa.String(200), nullable=False),
        sa.Column('source_revision', sa.String(200), nullable=False),
        sa.Column('filename', sa.String(240), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('source_group', sa.String(200), nullable=False),
        sa.Column('source', sa.String(2000), nullable=False),
        sa.Column('license', sa.String(1000), nullable=False),
        sa.Column('source_annotation', sa.JSON(), nullable=False),
        sa.Column('upload_id', sa.String(32), sa.ForeignKey('uploads.id'), unique=True),
        sa.Column('asset_id', sa.String(32), sa.ForeignKey('assets.id'), unique=True),
        sa.Column('scan_job_id', sa.String(32), sa.ForeignKey('jobs.id')),
        sa.UniqueConstraint('batch_id', 'external_id'))
    for column in ('created_at', 'batch_id'):
        op.create_index('ix_import_items_' + column, 'import_items', [column])
    op.create_index('ix_import_item_batch_created', 'import_items', ['batch_id', 'created_at', 'id'])


def downgrade():
    op.drop_table('import_items')
    op.drop_table('import_batches')
