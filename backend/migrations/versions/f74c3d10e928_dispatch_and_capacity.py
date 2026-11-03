"""Dispatch admission and platform capacity sentinel."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = 'f74c3d10e928'
down_revision = 'e63b2a09c817'
branch_labels = None
depends_on = None


def upgrade():
    timestamp=sa.DateTime().with_variant(DATETIME(fsp=6),'mysql')
    op.create_table('control_locks', sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('created_at', timestamp, nullable=False))
    op.create_index('ix_control_locks_created_at','control_locks',['created_at'])
    op.add_column('jobs', sa.Column('dispatch_token', sa.String(32)))
    op.add_column('jobs', sa.Column('next_dispatch_at', timestamp))
    op.add_column('jobs', sa.Column('dispatch_attempts', sa.Integer(), nullable=False, server_default='0'))
    op.create_index('ix_jobs_next_dispatch_at', 'jobs', ['next_dispatch_at'])
    op.add_column('uploads',sa.Column('finalize_token',sa.String(32)))
    op.add_column('uploads',sa.Column('finalize_until',timestamp))
    op.add_column('mail_outbox',sa.Column('delivery_token',sa.String(32)))
    op.add_column('mail_outbox',sa.Column('lease_until',timestamp))
    op.add_column('mail_outbox',sa.Column('next_attempt_at',timestamp))
    op.create_index('ix_mail_outbox_next_attempt_at','mail_outbox',['next_attempt_at'])
    op.create_table('space_transfers',sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('created_at',timestamp,nullable=False),sa.Column('space_id',sa.String(32),sa.ForeignKey('spaces.id'),nullable=False),
        sa.Column('requester_id',sa.String(32),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('target_user_id',sa.String(32),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('reason',sa.Text(),nullable=False),sa.Column('status',sa.String(20),nullable=False),
        sa.Column('expires_at',timestamp,nullable=False),sa.Column('decided_at',timestamp))
    for field in ('created_at','space_id','target_user_id'):op.create_index('ix_space_transfers_'+field,'space_transfers',[field])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM uploads WHERE status IN ('uploading','finalizing','aborting')")):
        raise RuntimeError('Drain uploads before removing finalization fences')
    if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM mail_outbox WHERE delivery_token IS NOT NULL')):
        raise RuntimeError('Drain mail delivery before removing receipt leases')
    if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM space_transfers')):
        raise RuntimeError('Ownership receipts exist; downgrade would discard responsibility history')
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')")):
        raise RuntimeError('Drain queued/running jobs before removing dispatch admission')
    op.drop_index('ix_jobs_next_dispatch_at', table_name='jobs')
    op.drop_column('uploads','finalize_token');op.drop_column('uploads','finalize_until')
    op.drop_index('ix_mail_outbox_next_attempt_at',table_name='mail_outbox')
    for name in ('delivery_token','lease_until','next_attempt_at'):op.drop_column('mail_outbox',name)
    for name in ('dispatch_attempts', 'next_dispatch_at', 'dispatch_token'):
        op.drop_column('jobs', name)
    op.drop_table('control_locks')
    op.drop_table('space_transfers')
