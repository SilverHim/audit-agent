"""Create financial and audit storage with mandatory project isolation."""

from alembic import op

from backend.persistence.models import SCOPED, metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())
    for table in SCOPED:
        name = table.name
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY engagement_isolation ON {name} "
            "USING (engagement_id = current_setting('app.engagement_id', true)) "
            "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
        )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO audit_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO audit_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_trail FROM audit_app")
    op.execute(
        """CREATE FUNCTION reject_trail_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'audit_trail is append only'; END; $$"""
    )
    op.execute(
        "CREATE TRIGGER audit_trail_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_trail "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_trail_mutation()"
    )


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
    op.execute("DROP FUNCTION reject_trail_mutation()")
