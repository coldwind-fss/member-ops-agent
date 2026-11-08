"""WP02 trusted member bindings, commerce evidence, and private delivery grants."""

from alembic import op

revision = "0003_wp02"
down_revision = "0002_wp01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE member_bindings (
            id uuid PRIMARY KEY, connection_id text NOT NULL, sender_external_id text NOT NULL,
            member_id text, private_external_id text, version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (connection_id, sender_external_id),
            CHECK (member_id IS NOT NULL OR private_external_id IS NULL)
        );
        CREATE UNIQUE INDEX member_private_conversation ON member_bindings (connection_id, private_external_id)
            WHERE private_external_id IS NOT NULL;
        ALTER TABLE matters ADD COLUMN commerce_context jsonb NOT NULL DEFAULT '{}'::jsonb;
        ALTER TABLE deliveries ADD COLUMN binding_id uuid REFERENCES member_bindings(id),
            ADD COLUMN binding_version integer, ADD COLUMN private_body text,
            ADD CONSTRAINT complete_delivery_binding CHECK ((binding_id IS NULL) = (binding_version IS NULL));
        CREATE TABLE commerce_checks (
            id uuid PRIMARY KEY, matter_id uuid NOT NULL REFERENCES matters(id),
            run_id uuid NOT NULL REFERENCES agent_runs(id), operation text NOT NULL,
            result_code text NOT NULL, source text NOT NULL, public_reference text,
            checked_at timestamptz NOT NULL
        );
        CREATE INDEX commerce_check_matter ON commerce_checks (matter_id, checked_at DESC, id DESC);
    """)


def downgrade():
    op.execute("""
        DROP TABLE commerce_checks;
        ALTER TABLE deliveries DROP CONSTRAINT complete_delivery_binding,
            DROP COLUMN binding_id, DROP COLUMN binding_version, DROP COLUMN private_body;
        ALTER TABLE matters DROP COLUMN commerce_context;
        DROP TABLE member_bindings;
    """)
