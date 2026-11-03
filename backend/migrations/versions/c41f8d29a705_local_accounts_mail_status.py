"""Optional email, local username, first-login password change and mail delivery status."""
from alembic import op
import sqlalchemy as sa

revision = 'c41f8d29a705'
down_revision = 'b9e81a7302fd'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as batch:
        batch.alter_column('email', existing_type=sa.String(254), nullable=True)
        batch.add_column(sa.Column('username', sa.String(32), nullable=True))
        batch.add_column(sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default='0'))
        batch.create_unique_constraint('uq_users_username', ['username'])
    op.add_column('mail_outbox', sa.Column('delivery_mode', sa.String(20), nullable=True))
    op.add_column('mail_outbox', sa.Column('last_error', sa.String(80), nullable=True))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM users WHERE email IS NULL')):
        raise RuntimeError('Local accounts without email exist; cannot downgrade without an explicit account mapping')
    op.drop_column('mail_outbox', 'last_error')
    op.drop_column('mail_outbox', 'delivery_mode')
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('uq_users_username', type_='unique')
        batch.drop_column('must_change_password')
        batch.drop_column('username')
        batch.alter_column('email', existing_type=sa.String(254), nullable=False)
