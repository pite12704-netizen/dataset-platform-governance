"""Preserve microsecond timestamps when using MySQL."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "d963eb284fa1"
down_revision = "c852da173e90"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    inspector = sa.inspect(bind)
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        for column in inspector.get_columns(table):
            if isinstance(column["type"], sa.DateTime):
                op.alter_column(table, column["name"], existing_type=column["type"],
                                type_=DATETIME(fsp=6), existing_nullable=column["nullable"])


def downgrade():
    # 降级保留已有精度，避免主动截断业务时间。
    pass
