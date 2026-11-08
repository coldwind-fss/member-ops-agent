"""WP03 member knowledge, participation, group signals and editorial records."""

from alembic import op

revision = "0004_wp03"
down_revision = "0003_wp02"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE member_profiles (
            id uuid PRIMARY KEY, merchant_member text NOT NULL UNIQUE,
            version integer NOT NULL DEFAULT 1, deleted_at timestamptz
        );
        CREATE TABLE group_memberships (
            connection_id text NOT NULL, group_id text NOT NULL, sender_external_id text NOT NULL,
            active boolean NOT NULL DEFAULT true, updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (connection_id,group_id,sender_external_id)
        );
        CREATE TABLE member_timeline (
            profile_id uuid NOT NULL REFERENCES member_profiles(id), event_id uuid NOT NULL REFERENCES inbox_events(id),
            kind text NOT NULL, topic text, occurred_at timestamptz NOT NULL,
            PRIMARY KEY (profile_id,event_id,kind)
        );
        CREATE INDEX member_timeline_recent ON member_timeline (profile_id,occurred_at DESC);
        CREATE TABLE member_memories (
            id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES member_profiles(id),
            kind text NOT NULL, topic text NOT NULL, value text, status text NOT NULL,
            version integer NOT NULL DEFAULT 1, event_id uuid REFERENCES inbox_events(id),
            source_at timestamptz NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz,
            UNIQUE (profile_id,kind,topic)
        );
        CREATE TABLE audiences (
            id uuid PRIMARY KEY, name text NOT NULL, filters jsonb NOT NULL,
            confirmed_member_ids uuid[] NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE activities (
            id uuid PRIMARY KEY, title text NOT NULL UNIQUE, group_id text,
            starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
            status text NOT NULL, version integer NOT NULL DEFAULT 1, config jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(), CHECK (starts_at < ends_at)
        );
        CREATE TABLE activity_versions (
            activity_id uuid NOT NULL REFERENCES activities(id), version integer NOT NULL,
            snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (activity_id,version)
        );
        CREATE TABLE participation (
            id uuid PRIMARY KEY, activity_id uuid NOT NULL, activity_version integer NOT NULL,
            profile_id uuid NOT NULL REFERENCES member_profiles(id), action text NOT NULL, period text NOT NULL,
            event_id uuid NOT NULL REFERENCES inbox_events(id), answer text,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (activity_id,profile_id,action,period), UNIQUE (event_id,action),
            FOREIGN KEY (activity_id,activity_version) REFERENCES activity_versions(activity_id,version)
        );
        CREATE TABLE analysis_groups (
            connection_id text NOT NULL, group_id text NOT NULL, enabled boolean NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (connection_id,group_id)
        );
        CREATE TABLE group_signals (
            id uuid PRIMARY KEY, event_id uuid NOT NULL UNIQUE REFERENCES inbox_events(id),
            connection_id text NOT NULL, group_id text NOT NULL, topic text NOT NULL,
            category text NOT NULL, status text NOT NULL DEFAULT 'open', version integer NOT NULL DEFAULT 1,
            occurred_at timestamptz NOT NULL, matter_id uuid REFERENCES matters(id),
            profile_id uuid REFERENCES member_profiles(id)
        );
        CREATE INDEX group_signal_day ON group_signals (connection_id,group_id,occurred_at);
        CREATE TABLE knowledge_drafts (
            id uuid PRIMARY KEY, knowledge_id uuid NOT NULL, expected_version integer NOT NULL,
            matter_id uuid REFERENCES matters(id), signal_id uuid REFERENCES group_signals(id),
            category text NOT NULL, config jsonb NOT NULL, published_version integer,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE knowledge_verifications (
            id uuid PRIMARY KEY, draft_id uuid NOT NULL REFERENCES knowledge_drafts(id),
            cases jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
        );
        ALTER TABLE inbox_events ADD COLUMN redacted_at timestamptz;
        ALTER TABLE deliveries ADD COLUMN profile_id uuid REFERENCES member_profiles(id), ADD COLUMN profile_version integer;
        INSERT INTO member_profiles (id,merchant_member)
        SELECT gen_random_uuid(), member_id FROM (SELECT DISTINCT member_id FROM member_bindings WHERE member_id IS NOT NULL) AS bound;
        INSERT INTO group_memberships (connection_id,group_id,sender_external_id,updated_at)
        SELECT connection_id,payload#>>'{conversation,external_id}',payload->>'sender_external_id',max(received_at)
        FROM inbox_events WHERE payload#>>'{conversation,kind}'='group' GROUP BY 1,2,3;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE deliveries DROP COLUMN profile_id, DROP COLUMN profile_version;
        ALTER TABLE inbox_events DROP COLUMN redacted_at;
        DROP TABLE knowledge_verifications, knowledge_drafts, group_signals, analysis_groups,
            participation, activity_versions, activities, audiences, member_memories, member_timeline,
            group_memberships, member_profiles;
    """)
