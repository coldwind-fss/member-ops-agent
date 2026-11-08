"""WP01 operator access, inbox, conversation, outbox, knowledge and handoff."""

from alembic import op

revision = "0002_wp01"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE operators (
            id uuid PRIMARY KEY, username text UNIQUE NOT NULL, display_name text NOT NULL,
            password_hash text NOT NULL, singleton boolean UNIQUE NOT NULL DEFAULT true CHECK (singleton)
        );
        CREATE TABLE operator_sessions (
            token_digest text PRIMARY KEY, operator_id uuid NOT NULL REFERENCES operators(id),
            csrf_token text NOT NULL, expires_at timestamptz NOT NULL
        );
        CREATE TABLE matters (
            id uuid PRIMARY KEY, connection_id text NOT NULL, conversation_kind text NOT NULL,
            conversation_external_id text NOT NULL, participant_label text NOT NULL,
            subject text NOT NULL, public_subject text, status text NOT NULL, version integer NOT NULL DEFAULT 1,
            clarification_count integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX matter_scope ON matters (connection_id, conversation_external_id, participant_label, updated_at DESC);
        CREATE TABLE inbox_events (
            id uuid PRIMARY KEY, connection_id text NOT NULL, external_event_id text NOT NULL,
            payload jsonb NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (connection_id, external_event_id)
        );
        CREATE TABLE messages (
            id uuid PRIMARY KEY, matter_id uuid REFERENCES matters(id), event_id uuid REFERENCES inbox_events(id),
            kind text NOT NULL, connection_id text NOT NULL, conversation_kind text NOT NULL, conversation_external_id text NOT NULL,
            external_id text, in_reply_to text, preview text NOT NULL, occurred_at timestamptz NOT NULL,
            delivery_state text,
            UNIQUE (connection_id, conversation_kind, conversation_external_id, external_id)
        );
        CREATE INDEX message_matter ON messages (matter_id, occurred_at);
        CREATE TABLE matter_inputs (
            matter_id uuid NOT NULL REFERENCES matters(id), message_id uuid NOT NULL REFERENCES messages(id),
            item_index integer NOT NULL, PRIMARY KEY (message_id, item_index)
        );
        CREATE INDEX matter_input_lookup ON matter_inputs (matter_id);
        CREATE TABLE knowledge (
            id uuid NOT NULL, version integer NOT NULL, title text NOT NULL, body text NOT NULL,
            product text NOT NULL, topic text NOT NULL, aliases text[] NOT NULL, keywords text[] NOT NULL,
            valid_from timestamptz NOT NULL, valid_until timestamptz,
            PRIMARY KEY (id, version)
        );
        CREATE TABLE agent_runs (
            id uuid PRIMARY KEY, operation_key text UNIQUE NOT NULL, matter_id uuid NOT NULL REFERENCES matters(id),
            intents text[] NOT NULL, outcome text NOT NULL, error_code text, recorded_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX run_matter ON agent_runs (matter_id, recorded_at);
        CREATE TABLE answer_evidence (
            run_id uuid NOT NULL REFERENCES agent_runs(id), knowledge_id uuid NOT NULL,
            version integer NOT NULL, PRIMARY KEY (run_id, knowledge_id, version),
            FOREIGN KEY (knowledge_id, version) REFERENCES knowledge(id, version)
        );
        CREATE TABLE handoffs (
            id uuid PRIMARY KEY, matter_id uuid UNIQUE NOT NULL REFERENCES matters(id),
            version integer NOT NULL DEFAULT 1, status text NOT NULL,
            summary text NOT NULL, reason text NOT NULL, notification_state text, notification_delivery_id uuid, resolution text,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE jobs (
            id uuid PRIMARY KEY, operation_key text UNIQUE NOT NULL, kind text NOT NULL,
            lane text NOT NULL, payload jsonb NOT NULL, state text NOT NULL DEFAULT 'pending',
            run_after timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
            generation integer NOT NULL DEFAULT 0, lease_until timestamptz, last_error text
        );
        CREATE INDEX job_claim ON jobs (state, run_after, created_at);
        CREATE INDEX job_lane ON jobs (lane, created_at, id) WHERE state IN ('pending', 'running');
        CREATE TABLE deliveries (
            id uuid PRIMARY KEY, operation_key text UNIQUE NOT NULL, kind text NOT NULL,
            matter_id uuid NOT NULL REFERENCES matters(id), expected_version integer NOT NULL,
            message_id uuid REFERENCES messages(id), handoff_id uuid REFERENCES handoffs(id),
            state text NOT NULL DEFAULT 'pending', provider_ref text, attempt_started_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE api_writes (
            operator_id uuid NOT NULL REFERENCES operators(id), action text NOT NULL,
            operation_key text NOT NULL, request jsonb NOT NULL, response jsonb,
            PRIMARY KEY (operator_id, action, operation_key)
        );
    """)


def downgrade():
    op.execute("""
        DROP TABLE api_writes, deliveries, jobs, handoffs, answer_evidence, agent_runs,
            knowledge, matter_inputs, messages, inbox_events, matters, operator_sessions, operators;
    """)
