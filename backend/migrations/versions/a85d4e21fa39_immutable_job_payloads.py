"""Immutable large task fields outside hot status/lease rows."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME,LONGTEXT

revision='a85d4e21fa39'
down_revision='f74c3d10e928'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('job_payloads',
        sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('created_at',sa.DateTime().with_variant(DATETIME(fsp=6),'mysql'),nullable=False),
        sa.Column('job_id',sa.String(32),sa.ForeignKey('jobs.id',ondelete='CASCADE'),nullable=False),
        sa.Column('field',sa.String(16),nullable=False),sa.Column('key',sa.String(100),nullable=False),
        sa.Column('sha256',sa.String(64),nullable=False),sa.Column('raw_bytes',sa.BigInteger(),nullable=False),
        sa.Column('content',sa.Text().with_variant(LONGTEXT(),'mysql'),nullable=False),
        sa.UniqueConstraint('job_id','field','key','sha256',name='uq_job_payload_content'))
    op.create_index('ix_job_payloads_created_at','job_payloads',['created_at'])
    op.create_index('ix_job_payloads_job_id','job_payloads',['job_id'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM job_payloads')):
        raise RuntimeError('Task payloads exist; dropping them would invalidate frozen task inputs/results')
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM jobs WHERE CAST(parameters AS CHAR) LIKE '%payload_refs_v1%' OR CAST(result AS CHAR) LIKE '%payload_refs_v1%'")):
        raise RuntimeError('Task payload references remain; review recovery rather than dropping payload support')
    op.drop_table('job_payloads')
