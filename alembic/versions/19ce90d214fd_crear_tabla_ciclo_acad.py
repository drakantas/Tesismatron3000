"""Crear tabla ciclo acad

Revision ID: 19ce90d214fd
Revises: f1bbcfb7abde
Create Date: 2017-09-11 17:20:57.135768

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '19ce90d214fd'
down_revision = 'f1bbcfb7abde'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ciclo_academico',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('escuela', sa.SmallInteger, nullable=False),
                    sa.Column('fecha_comienzo', sa.DateTime, nullable=False),
                    sa.Column('fecha_fin', sa.DateTime, nullable=False))

    # FK para la tabla proyecto
    op.create_foreign_key('ciclo_acad_id_fk',
                          'proyecto', 'ciclo_academico',
                          ['ciclo_acad_id'], ['id'],
                          ondelete='CASCADE', onupdate='CASCADE')


def downgrade():
    # Dropear la FK de proyecto
    op.drop_constraint('ciclo_acad_id_fk', 'proyecto', type_='foreignkey')

    op.drop_table('ciclo_academico')
