"""WP04 plans, verified business events, subscriptions and outreach outbox."""

from alembic import op

revision = "0005_wp04"
down_revision = "0004_wp03"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE outreach_groups (
            connection_id text NOT NULL, group_id text NOT NULL,
            reply_enabled boolean NOT NULL, outreach_enabled boolean NOT NULL,
            welcome_text text, care_text text, outreach_since timestamptz NOT NULL DEFAULT now(), reply_since timestamptz,
            PRIMARY KEY (connection_id,group_id)
        );
        CREATE TABLE outreach_plans (
            id uuid PRIMARY KEY, title text NOT NULL, config jsonb NOT NULL,
            status text NOT NULL DEFAULT 'draft', version integer NOT NULL DEFAULT 1,
            approved_members uuid[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE outreach_plan_versions (
            plan_id uuid NOT NULL REFERENCES outreach_plans(id), version integer NOT NULL,
            snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (plan_id,version)
        );
        CREATE TABLE subscriptions (
            id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES member_profiles(id),
            binding_id uuid NOT NULL REFERENCES member_bindings(id), binding_version integer NOT NULL,
            connection_id text NOT NULL, group_id text, product_id text NOT NULL, product_name text NOT NULL,
            color text NOT NULL, size text NOT NULL, event_id uuid NOT NULL REFERENCES inbox_events(id),
            status text NOT NULL DEFAULT 'active', version integer NOT NULL DEFAULT 1,
            expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX active_subscription ON subscriptions (profile_id,product_id,color,size) WHERE status='active';
        CREATE INDEX subscription_spec ON subscriptions (connection_id,product_id,color,size) WHERE status='active';
        CREATE TABLE business_events (
            id uuid PRIMARY KEY, connection_id text NOT NULL, external_event_id text NOT NULL,
            event_type text NOT NULL, payload jsonb NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (connection_id,external_event_id)
        );
        CREATE TABLE outreach_stock (
            connection_id text NOT NULL, product_id text NOT NULL, color text NOT NULL, size text NOT NULL,
            quantity integer NOT NULL, occurred_at timestamptz NOT NULL, PRIMARY KEY (connection_id,product_id,color,size)
        );
        CREATE TABLE outreach_orders (
            connection_id text NOT NULL, order_id text NOT NULL, sender_external_id text NOT NULL,
            state text NOT NULL, occurred_at timestamptz NOT NULL, PRIMARY KEY (connection_id,order_id)
        );
        CREATE TABLE care_optouts (
            profile_id uuid PRIMARY KEY REFERENCES member_profiles(id), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE outreach_suggestions (
            memory_id uuid PRIMARY KEY REFERENCES member_memories(id), version integer NOT NULL DEFAULT 1,
            status text NOT NULL, result text, updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE outreach_records (
            id uuid PRIMARY KEY, operation_key text NOT NULL UNIQUE, kind text NOT NULL,
            connection_id text NOT NULL, group_id text, sender_external_id text,
            profile_id uuid REFERENCES member_profiles(id), binding_id uuid REFERENCES member_bindings(id),
            binding_version integer, plan_id uuid REFERENCES outreach_plans(id), plan_version integer,
            subscription_id uuid REFERENCES subscriptions(id), memory_id uuid REFERENCES member_memories(id),
            event_id uuid REFERENCES business_events(id), order_id text, body text NOT NULL,
            state text NOT NULL DEFAULT 'pending', reason text, version integer NOT NULL DEFAULT 1,
            attempts integer NOT NULL DEFAULT 0, provider_ref text, evidence text, dispatch_at timestamptz,
            scheduled_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX outreach_recent ON outreach_records (connection_id,group_id,kind,sender_external_id,updated_at DESC);
        CREATE TABLE daily_reports (
            connection_id text NOT NULL, group_id text NOT NULL, day date NOT NULL,
            generated_at timestamptz NOT NULL DEFAULT now(), summary jsonb NOT NULL,
            PRIMARY KEY (connection_id,group_id,day)
        );
        ALTER TABLE group_memberships ADD COLUMN source_at timestamptz;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE group_memberships DROP COLUMN source_at;
        DROP TABLE daily_reports, outreach_records, outreach_suggestions, care_optouts, outreach_orders, outreach_stock,
            business_events, subscriptions, outreach_plan_versions, outreach_plans, outreach_groups;
    """)
