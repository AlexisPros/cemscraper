-- 0001: initial schema for the CEM PES scraper.
-- No CAPTCHA tokens, cookies or other authorization secrets are stored anywhere.

CREATE TABLE exam_sessions (
    id                        BIGSERIAL PRIMARY KEY,
    exam_type                 TEXT        NOT NULL,
    specialization            TEXT        NOT NULL,
    specialization_code       TEXT        NOT NULL,
    year                      SMALLINT    NOT NULL CHECK (year BETWEEN 1990 AND 2100),
    season                    TEXT        NOT NULL CHECK (season IN ('spring', 'autumn')),
    session_code              TEXT        NOT NULL CHECK (session_code ~ '^[0-9]{4}[12]$'),
    expected_question_count   INTEGER     CHECK (expected_question_count > 0),
    discovered_question_count INTEGER     CHECK (discovered_question_count >= 0),
    collection_status         TEXT        NOT NULL DEFAULT 'pending'
        CHECK (collection_status IN ('pending', 'in_progress', 'completed', 'blocked', 'failed')),
    last_error                TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT exam_sessions_identity UNIQUE (exam_type, specialization_code, session_code)
);

CREATE TABLE scrape_runs (
    id               UUID PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'partial', 'interrupted', 'blocked', 'failed')),
    access_provider  TEXT,
    target_sessions  TEXT[]      NOT NULL DEFAULT '{}',
    total_requested  INTEGER     NOT NULL DEFAULT 0,
    total_succeeded  INTEGER     NOT NULL DEFAULT 0,
    total_failed     INTEGER     NOT NULL DEFAULT 0,
    retry_count      INTEGER     NOT NULL DEFAULT 0,
    diagnostics      JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE questions (
    id                BIGSERIAL PRIMARY KEY,
    exam_session_id   BIGINT      NOT NULL REFERENCES exam_sessions (id) ON DELETE CASCADE,
    question_number   INTEGER     NOT NULL CHECK (question_number > 0),
    question_data     JSONB       NOT NULL CHECK (jsonb_typeof(question_data) = 'object'),
    correct_answer    JSONB       CHECK (correct_answer IS NULL OR jsonb_typeof(correct_answer) = 'object'),
    answer_statistics JSONB       CHECK (answer_statistics IS NULL OR jsonb_typeof(answer_statistics) = 'object'),
    extra_statistics  JSONB,
    validation_status TEXT        NOT NULL CHECK (validation_status IN ('valid', 'needs_review')),
    validation_issues JSONB       NOT NULL DEFAULT '[]'::jsonb,
    source_url        TEXT,
    content_hash      TEXT        NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL,
    last_run_id       UUID        REFERENCES scrape_runs (id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT questions_session_number UNIQUE (exam_session_id, question_number)
);

CREATE INDEX questions_needs_review_idx ON questions (exam_session_id) WHERE validation_status <> 'valid';

-- Per-question processing state: drives resume and reprocessing of failures.
CREATE TABLE question_attempts (
    exam_session_id BIGINT      NOT NULL REFERENCES exam_sessions (id) ON DELETE CASCADE,
    question_number INTEGER     NOT NULL CHECK (question_number > 0),
    status          TEXT        NOT NULL CHECK (status IN (
        'stored', 'missing', 'parse_error', 'number_mismatch',
        'unexpected_content', 'auth_error', 'server_error')),
    attempts        INTEGER     NOT NULL DEFAULT 1,
    last_error      TEXT,
    last_run_id     UUID        REFERENCES scrape_runs (id) ON DELETE SET NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (exam_session_id, question_number)
);

CREATE INDEX question_attempts_failed_idx ON question_attempts (exam_session_id) WHERE status NOT IN ('stored', 'missing');
