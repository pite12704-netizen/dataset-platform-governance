"""Account recovery, internal blind evaluation and private persistent thumbnails."""
from alembic import op
import sqlalchemy as sa

revision='e63b2a09c817'
down_revision='d52a0f18b906'
branch_labels=None
depends_on=None


def upgrade():
    for table,name,empty in [('users','mfa_recovery_hashes','[]'),('challenges','payload','{}')]:
        op.add_column(table,sa.Column(name,sa.JSON(),nullable=True))
        ref=sa.table(table,sa.column(name,sa.JSON()))
        op.get_bind().execute(ref.update().values({name:[] if empty=='[]' else {}}))
        with op.batch_alter_table(table) as batch:batch.alter_column(name,existing_type=sa.JSON(),nullable=False)
    op.add_column('login_sessions',sa.Column('recovery_only',sa.Boolean(),nullable=False,server_default='0'))
    op.create_table('thumbnails',sa.Column('id',sa.String(32),primary_key=True),sa.Column('created_at',sa.DateTime(),nullable=False),
        sa.Column('asset_id',sa.String(32),sa.ForeignKey('assets.id'),nullable=False),sa.Column('profile',sa.String(40),nullable=False),
        sa.Column('source_sha256',sa.String(64),nullable=False),sa.Column('object_key',sa.String(512),unique=True,nullable=False),
        sa.Column('sha256',sa.String(64),nullable=False),sa.Column('size',sa.BigInteger(),nullable=False),
        sa.UniqueConstraint('asset_id','profile'))
    op.create_index('ix_thumbnails_asset_id','thumbnails',['asset_id'])
    op.create_index('ix_thumbnails_created_at','thumbnails',['created_at'])
    op.create_table('evaluation_sets',sa.Column('id',sa.String(32),primary_key=True),sa.Column('created_at',sa.DateTime(),nullable=False),
        sa.Column('target_version_id',sa.String(32),sa.ForeignKey('versions.id'),nullable=False),
        sa.Column('creator_id',sa.String(32),sa.ForeignKey('users.id'),nullable=False),sa.Column('reviewer_id',sa.String(32),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('status',sa.String(20),nullable=False),sa.Column('sample_ids',sa.JSON(),nullable=False),
        sa.Column('population_manifest',sa.String(64),nullable=False),sa.Column('truth_asset_id',sa.String(32),sa.ForeignKey('assets.id')),
        sa.Column('truth_sha256',sa.String(64)),sa.Column('annotation_manifest',sa.String(64)))
    op.create_index('ix_evaluation_sets_target_version_id','evaluation_sets',['target_version_id'])
    op.create_index('ix_evaluation_sets_created_at','evaluation_sets',['created_at'])


def downgrade():
    if any(op.get_bind().scalar(sa.text(f'SELECT COUNT(*) FROM {table}')) for table in ['evaluation_sets','thumbnails']):
        raise RuntimeError('Evidence or thumbnail records exist; downgrade would discard protected records')
    user=sa.table('users',sa.column('mfa_recovery_hashes',sa.JSON()))
    if any(value for value, in op.get_bind().execute(sa.select(user.c.mfa_recovery_hashes))):
        raise RuntimeError('Recovery codes exist; downgrade would discard active security state')
    op.execute(sa.text('UPDATE login_sessions SET revoked=1 WHERE recovery_only=1'))
    op.drop_table('evaluation_sets');op.drop_table('thumbnails')
    op.drop_column('login_sessions','recovery_only');op.drop_column('challenges','payload');op.drop_column('users','mfa_recovery_hashes')
