-- Baseline schema: every table, index, constraint, row-level security policy,
-- function and trigger the application uses. Generated from a migrated database
-- with pg_dump --schema-only; grants are applied by provisioning, not here.

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE FUNCTION public.index_accepted_membership() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_temp'
    AS $$
        BEGIN
            INSERT INTO public.identity_memberships(identity_id,tenant_id,user_id)
            VALUES(NEW.identity_id,NEW.tenant_id,NEW.user_id);
            RETURN NEW;
        END $$;

CREATE FUNCTION public.reject_audit_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
        BEGIN
            RAISE EXCEPTION 'Audit events are append-only' USING ERRCODE = '42501';
        END;
        $$;

CREATE TABLE public.api_credentials (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_digest character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);

ALTER TABLE ONLY public.api_credentials FORCE ROW LEVEL SECURITY;

CREATE TABLE public.appointments (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    provider_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    status character varying(20) NOT NULL,
    version integer NOT NULL,
    CONSTRAINT ck_appointments_positive_duration CHECK ((ends_at > starts_at)),
    CONSTRAINT ck_appointments_positive_version CHECK ((version >= 1)),
    CONSTRAINT ck_appointments_status_allowed CHECK (((status)::text = ANY ((ARRAY['booked'::character varying, 'cancelled'::character varying])::text[])))
);

ALTER TABLE ONLY public.appointments FORCE ROW LEVEL SECURITY;

CREATE TABLE public.audit_events (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    actor_id uuid,
    actor_type character varying(20) NOT NULL,
    action character varying(80) NOT NULL,
    resource_type character varying(80) NOT NULL,
    resource_id uuid,
    request_id uuid NOT NULL,
    call_id uuid,
    platform_user_id uuid,
    support_session_id uuid,
    CONSTRAINT ck_audit_events_actor_type_allowed CHECK (((actor_type)::text = ANY ((ARRAY['user'::character varying, 'system'::character varying, 'platform_support'::character varying])::text[]))),
    CONSTRAINT ck_audit_events_support_actor_required CHECK ((((actor_type)::text <> 'platform_support'::text) OR ((actor_id IS NULL) AND (platform_user_id IS NOT NULL) AND (support_session_id IS NOT NULL)))),
    CONSTRAINT ck_audit_events_user_actor_required CHECK ((((actor_type)::text <> 'user'::text) OR (actor_id IS NOT NULL)))
);

ALTER TABLE ONLY public.audit_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.auth_challenges (
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    purpose character varying(20) NOT NULL,
    key_digest character varying(64) NOT NULL,
    token_digest character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    attempts integer NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    credential_revision integer,
    CONSTRAINT ck_auth_challenges_challenge_attempts CHECK (((attempts >= 0) AND (attempts <= 5))),
    CONSTRAINT ck_auth_challenges_challenge_purpose CHECK (((purpose)::text = ANY ((ARRAY['invitation'::character varying, 'reset'::character varying])::text[])))
);

ALTER TABLE ONLY public.auth_challenges FORCE ROW LEVEL SECURITY;

CREATE TABLE public.auth_mail (
    tenant_id uuid NOT NULL,
    challenge_id uuid NOT NULL,
    key_id character varying(64) NOT NULL,
    ciphertext bytea,
    expires_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    status character varying(20) NOT NULL,
    attempts integer NOT NULL,
    lease_id uuid,
    lease_until timestamp with time zone,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_auth_mail_mail_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'sending'::character varying, 'sent'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);

ALTER TABLE ONLY public.auth_mail FORCE ROW LEVEL SECURITY;

CREATE TABLE public.availability (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    provider_id uuid NOT NULL,
    slot_minutes integer NOT NULL,
    CONSTRAINT ck_availability_bounded_window CHECK ((ends_at <= (starts_at + '24:00:00'::interval))),
    CONSTRAINT ck_availability_fits_slot CHECK ((EXTRACT(epoch FROM (ends_at - starts_at)) >= ((slot_minutes * 60))::numeric)),
    CONSTRAINT ck_availability_positive_window CHECK ((ends_at > starts_at)),
    CONSTRAINT ck_availability_slot_minutes_range CHECK (((slot_minutes >= 5) AND (slot_minutes <= 240)))
);

ALTER TABLE ONLY public.availability FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_directory_mutations (
    request_hash character varying(64) NOT NULL,
    result jsonb NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.business_directory_mutations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_holidays (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    policy_id uuid NOT NULL,
    day date NOT NULL
);

ALTER TABLE ONLY public.business_holidays FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_hours_policies (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    location_id uuid,
    after_hours character varying(20) NOT NULL,
    CONSTRAINT ck_business_hours_policies_after_hours CHECK (((after_hours)::text = ANY ((ARRAY['answer_only'::character varying, 'callback'::character varying, 'transfer'::character varying])::text[])))
);

ALTER TABLE ONLY public.business_hours_policies FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_opening_windows (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    policy_id uuid NOT NULL,
    weekday integer NOT NULL,
    opens time without time zone NOT NULL,
    closes time without time zone NOT NULL,
    CONSTRAINT ck_business_opening_windows_ordered_window CHECK ((opens < closes)),
    CONSTRAINT ck_business_opening_windows_weekday CHECK (((weekday >= 0) AND (weekday <= 6)))
);

ALTER TABLE ONLY public.business_opening_windows FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_resources (
    name character varying(200) NOT NULL,
    resource_type character varying(20) NOT NULL,
    description character varying(2000) NOT NULL,
    location_id uuid NOT NULL,
    capacity integer NOT NULL,
    is_active boolean NOT NULL,
    revision integer NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_business_resources_positive_values CHECK (((revision >= 1) AND ((capacity >= 1) AND (capacity <= 1000)))),
    CONSTRAINT ck_business_resources_resource_type CHECK (((resource_type)::text = ANY ((ARRAY['space'::character varying, 'equipment'::character varying])::text[])))
);

ALTER TABLE ONLY public.business_resources FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_service_resources (
    service_id uuid NOT NULL,
    resource_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.business_service_resources FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_service_staff (
    service_id uuid NOT NULL,
    staff_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.business_service_staff FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_services (
    name character varying(200) NOT NULL,
    description character varying(4000) NOT NULL,
    price_minor integer NOT NULL,
    currency character varying(3) NOT NULL,
    duration_minutes integer NOT NULL,
    buffer_before_minutes integer NOT NULL,
    buffer_after_minutes integer NOT NULL,
    minimum_notice_minutes integer NOT NULL,
    capacity integer NOT NULL,
    location_id uuid,
    is_active boolean NOT NULL,
    revision integer NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_business_services_currency CHECK (((currency)::text = 'INR'::text)),
    CONSTRAINT ck_business_services_duration_values CHECK ((((duration_minutes >= 5) AND (duration_minutes <= 1440)) AND ((buffer_before_minutes >= 0) AND (buffer_before_minutes <= 240)) AND ((buffer_after_minutes >= 0) AND (buffer_after_minutes <= 240)))),
    CONSTRAINT ck_business_services_positive_values CHECK (((revision >= 1) AND ((price_minor >= 0) AND (price_minor <= 1000000000)) AND ((capacity >= 1) AND (capacity <= 1000)) AND ((minimum_notice_minutes >= 0) AND (minimum_notice_minutes <= 43200))))
);

ALTER TABLE ONLY public.business_services FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_staff (
    name character varying(200) NOT NULL,
    job_title character varying(200) NOT NULL,
    email character varying(254) NOT NULL,
    phone character varying(16) NOT NULL,
    transfer_number character varying(16),
    user_id uuid,
    location_id uuid,
    calendar_connection_id uuid,
    capacity integer NOT NULL,
    is_active boolean NOT NULL,
    revision integer NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_business_staff_positive_values CHECK (((revision >= 1) AND ((capacity >= 1) AND (capacity <= 100))))
);

ALTER TABLE ONLY public.business_staff FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_staff_holidays (
    staff_id uuid NOT NULL,
    day date NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.business_staff_holidays FORCE ROW LEVEL SECURITY;

CREATE TABLE public.business_staff_windows (
    staff_id uuid NOT NULL,
    weekday integer NOT NULL,
    opens time without time zone NOT NULL,
    closes time without time zone NOT NULL,
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_business_staff_windows_valid_window CHECK ((((weekday >= 0) AND (weekday <= 6)) AND (opens < closes)))
);

ALTER TABLE ONLY public.business_staff_windows FORCE ROW LEVEL SECURITY;

CREATE TABLE public.call_transcripts (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    call_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    snapshot jsonb NOT NULL
);

ALTER TABLE ONLY public.call_transcripts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.calls (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    phone_number_id uuid NOT NULL,
    provider_call_id character varying(34) NOT NULL,
    stream_sid character varying(34),
    status character varying(20) NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    received_frames integer NOT NULL,
    sent_frames integer NOT NULL,
    end_reason character varying(40),
    access_patient_id uuid,
    verified_patient_id uuid,
    access_digest character varying(128),
    access_salt character varying(32),
    access_expires_at timestamp with time zone,
    access_attempts integer DEFAULT 0 NOT NULL,
    verification_expires_at timestamp with time zone,
    integration_id uuid,
    credential_version_id uuid,
    configuration_version integer,
    location_id uuid,
    agent_id uuid,
    channel_id uuid,
    CONSTRAINT ck_calls_access_attempts_bounded CHECK (((access_attempts >= 0) AND (access_attempts <= 3))),
    CONSTRAINT ck_calls_frame_counts CHECK (((received_frames >= 0) AND (sent_frames >= 0))),
    CONSTRAINT ck_calls_integration_binding CHECK (((integration_id IS NULL) = (credential_version_id IS NULL))),
    CONSTRAINT ck_calls_status_allowed CHECK (((status)::text = ANY ((ARRAY['received'::character varying, 'active'::character varying, 'completed'::character varying, 'disconnected'::character varying, 'failed'::character varying])::text[])))
);

ALTER TABLE ONLY public.calls FORCE ROW LEVEL SECURITY;

CREATE TABLE public.configuration_drafts (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    revision integer NOT NULL,
    document jsonb NOT NULL,
    defaults_version integer NOT NULL,
    published_version integer,
    CONSTRAINT configuration_drafts_revision_check CHECK ((revision > 0))
);

ALTER TABLE ONLY public.configuration_drafts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.configuration_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    configuration_id uuid NOT NULL,
    version integer NOT NULL,
    delivered_at timestamp with time zone,
    available_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.configuration_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.configuration_resources (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    kind character varying(20) NOT NULL,
    channel_kind character varying(20),
    name character varying(200) NOT NULL,
    revision integer NOT NULL,
    is_active boolean NOT NULL,
    creation_hash character varying(64) NOT NULL,
    CONSTRAINT ck_configuration_resources_channel_kind CHECK (((((kind)::text = 'agent'::text) AND (channel_kind IS NULL)) OR (((kind)::text = 'channel'::text) AND (channel_kind IS NOT NULL) AND ((channel_kind)::text = ANY ((ARRAY['voice'::character varying, 'chat'::character varying])::text[]))))),
    CONSTRAINT ck_configuration_resources_resource_kind CHECK (((kind)::text = ANY ((ARRAY['agent'::character varying, 'channel'::character varying])::text[]))),
    CONSTRAINT ck_configuration_resources_resource_revision CHECK ((revision >= 1))
);

ALTER TABLE ONLY public.configuration_resources FORCE ROW LEVEL SECURITY;

CREATE TABLE public.document_chunks (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    document_id uuid NOT NULL,
    document_version_id uuid NOT NULL,
    ordinal integer NOT NULL,
    text text NOT NULL,
    section character varying(300),
    subsection character varying(300),
    page_number integer,
    token_count integer NOT NULL,
    embedding public.vector(1536) NOT NULL,
    checksum character varying(64) NOT NULL,
    source_metadata jsonb NOT NULL,
    CONSTRAINT ck_document_chunks_chunk_token_count CHECK ((token_count > 0))
);

ALTER TABLE ONLY public.document_chunks FORCE ROW LEVEL SECURITY;

CREATE TABLE public.document_versions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    document_id uuid NOT NULL,
    version integer NOT NULL,
    checksum character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    source_type character varying(20) NOT NULL,
    storage_key character varying(200) NOT NULL,
    status character varying(20) NOT NULL,
    attempts integer NOT NULL,
    lease_until timestamp with time zone,
    lease_token uuid,
    error_code character varying(40),
    CONSTRAINT ck_document_versions_version_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'processing'::character varying, 'ready'::character varying, 'failed'::character varying])::text[])))
);

ALTER TABLE ONLY public.document_versions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.documents (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    name character varying(200) NOT NULL,
    source_type character varying(20) NOT NULL,
    status character varying(20) NOT NULL,
    active_version integer NOT NULL,
    CONSTRAINT ck_documents_active_version_positive CHECK ((active_version > 0)),
    CONSTRAINT ck_documents_document_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'approved'::character varying, 'archived'::character varying])::text[])))
);

ALTER TABLE ONLY public.documents FORCE ROW LEVEL SECURITY;

CREATE TABLE public.follow_up_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    call_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind character varying(40) NOT NULL,
    status character varying(20) NOT NULL,
    acknowledged_at timestamp with time zone,
    CONSTRAINT ck_follow_up_events_kind_allowed CHECK (((kind)::text = ANY ((ARRAY['human_review'::character varying, 'appointment_followup'::character varying])::text[]))),
    CONSTRAINT ck_follow_up_events_status_allowed CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'acknowledged'::character varying])::text[])))
);

ALTER TABLE ONLY public.follow_up_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.human_accounts (
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    identity_id uuid NOT NULL
);

ALTER TABLE ONLY public.human_accounts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.human_identities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    email character varying(254) NOT NULL,
    password_hash character varying(256),
    credential_revision integer DEFAULT 1 NOT NULL,
    CONSTRAINT ck_human_identities_credential_revision_positive CHECK ((credential_revision >= 1)),
    CONSTRAINT ck_human_identities_normalized_email CHECK (((email)::text = lower((email)::text)))
);

ALTER TABLE ONLY public.human_identities FORCE ROW LEVEL SECURITY;

CREATE TABLE public.human_sessions (
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_digest character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    identity_id uuid NOT NULL,
    credential_revision integer NOT NULL
);

ALTER TABLE ONLY public.human_sessions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.idempotency_records (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    actor_id uuid,
    key_digest character varying(64) NOT NULL,
    request_digest character varying(64) NOT NULL,
    response jsonb NOT NULL,
    call_id uuid,
    platform_user_id uuid,
    support_session_id uuid,
    CONSTRAINT ck_idempotency_records_idempotency_principal_exactly_one CHECK ((((actor_id IS NOT NULL) AND (call_id IS NULL) AND (support_session_id IS NULL) AND (platform_user_id IS NULL)) OR ((call_id IS NOT NULL) AND (actor_id IS NULL) AND (support_session_id IS NULL) AND (platform_user_id IS NULL)) OR ((support_session_id IS NOT NULL) AND (platform_user_id IS NOT NULL) AND (actor_id IS NULL) AND (call_id IS NULL))))
);

ALTER TABLE ONLY public.idempotency_records FORCE ROW LEVEL SECURITY;

CREATE TABLE public.identity_memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    identity_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL
);

ALTER TABLE ONLY public.identity_memberships FORCE ROW LEVEL SECURITY;

CREATE TABLE public.identity_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    identity_id uuid NOT NULL,
    credential_revision integer NOT NULL,
    token_digest character varying(64) NOT NULL,
    request_id uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);

ALTER TABLE ONLY public.identity_sessions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.integration_credential_versions (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    integration_id uuid NOT NULL,
    version integer NOT NULL,
    state character varying(20) NOT NULL,
    key_id character varying(64) NOT NULL,
    ciphertext bytea,
    display_suffix character varying(8) NOT NULL,
    fingerprint character varying(64) NOT NULL,
    key_digest character varying(64) NOT NULL,
    error_code character varying(40),
    validated_at timestamp with time zone,
    lease_token uuid,
    lease_until timestamp with time zone,
    created_by_user_id uuid,
    created_by_platform_user_id uuid,
    CONSTRAINT ck_integration_credential_versions_positive_version CHECK ((version >= 1)),
    CONSTRAINT ck_integration_credential_versions_secret_present CHECK (((ciphertext IS NOT NULL) OR ((state)::text = ANY ((ARRAY['retired'::character varying, 'invalid'::character varying])::text[])))),
    CONSTRAINT ck_integration_credential_versions_single_author CHECK (((created_by_user_id IS NULL) <> (created_by_platform_user_id IS NULL))),
    CONSTRAINT ck_integration_credential_versions_state_allowed CHECK (((state)::text = ANY ((ARRAY['staged'::character varying, 'verified'::character varying, 'active'::character varying, 'retired'::character varying, 'invalid'::character varying])::text[])))
);

ALTER TABLE ONLY public.integration_credential_versions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.knowledge_jobs (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    document_version_id uuid NOT NULL,
    status character varying(20) NOT NULL,
    attempts integer NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_until timestamp with time zone,
    lease_token uuid,
    error_code character varying(40),
    CONSTRAINT ck_knowledge_jobs_knowledge_job_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'processing'::character varying, 'delivered'::character varying, 'dead'::character varying])::text[])))
);

ALTER TABLE ONLY public.knowledge_jobs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.locations (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    name character varying(200) NOT NULL,
    timezone character varying(100) NOT NULL
);

ALTER TABLE ONLY public.locations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.outbox_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version integer NOT NULL,
    event_type character varying(80) NOT NULL,
    schema_version integer NOT NULL,
    request_id uuid NOT NULL,
    payload jsonb NOT NULL,
    status character varying(20) NOT NULL,
    attempts integer NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_token uuid,
    lease_until timestamp with time zone,
    delivered_at timestamp with time zone,
    last_error character varying(80),
    CONSTRAINT ck_outbox_events_lease_required CHECK ((((status)::text = 'processing'::text) = ((lease_token IS NOT NULL) AND (lease_until IS NOT NULL)))),
    CONSTRAINT ck_outbox_events_positive_versions CHECK (((attempts >= 0) AND (aggregate_version >= 1) AND (schema_version >= 1))),
    CONSTRAINT ck_outbox_events_status_allowed CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'processing'::character varying, 'delivered'::character varying, 'dead'::character varying])::text[])))
);

ALTER TABLE ONLY public.outbox_events FORCE ROW LEVEL SECURITY;

CREATE TABLE public.patients (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    display_name character varying(200) NOT NULL,
    phone character varying(16) NOT NULL,
    date_of_birth date,
    account_identifier character varying(64)
);

ALTER TABLE ONLY public.patients FORCE ROW LEVEL SECURITY;

CREATE TABLE public.phone_numbers (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    number character varying(16) NOT NULL,
    is_active boolean NOT NULL,
    integration_id uuid,
    provider_number_sid character varying(34),
    CONSTRAINT ck_phone_numbers_e164 CHECK (((number)::text ~ '^\+[1-9][0-9]{7,14}$'::text)),
    CONSTRAINT ck_phone_numbers_provider_number_sid CHECK (((provider_number_sid IS NULL) OR ((provider_number_sid)::text ~ '^PN[0-9a-fA-F]{32}$'::text)))
);

ALTER TABLE ONLY public.phone_numbers FORCE ROW LEVEL SECURITY;

CREATE TABLE public.platform_audit_events (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    action character varying(80) NOT NULL,
    request_id uuid NOT NULL,
    resource_id uuid,
    tenant_id uuid,
    support_session_id uuid,
    http_method character varying(10),
    route character varying(200)
);

CREATE TABLE public.platform_authenticators (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    key_id character varying(64) NOT NULL,
    ciphertext bytea NOT NULL,
    enabled_at timestamp with time zone,
    last_counter bigint DEFAULT '-1'::integer NOT NULL,
    failed_attempts integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    recovery_digests jsonb DEFAULT '[]'::jsonb NOT NULL,
    recovery_revision integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_platform_authenticators_recovery_codes_bounded CHECK (((jsonb_typeof(recovery_digests) = 'array'::text) AND (jsonb_array_length(recovery_digests) <= 8))),
    CONSTRAINT ck_platform_authenticators_recovery_revision_nonnegative CHECK ((recovery_revision >= 0)),
    CONSTRAINT platform_authenticators_failed_attempts_check CHECK ((failed_attempts >= 0))
);

CREATE TABLE public.platform_sessions (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    token_digest character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    mfa_verified_at timestamp with time zone
);

CREATE TABLE public.platform_users (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    email character varying(254) NOT NULL,
    display_name character varying(200) NOT NULL,
    password_hash character varying(256) NOT NULL,
    role character varying(20) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT ck_platform_users_role_allowed CHECK (((role)::text = 'super_admin'::text))
);

CREATE TABLE public.post_call_reports (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    call_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    call_ended_at timestamp with time zone NOT NULL,
    summary character varying(1200) NOT NULL,
    outcome jsonb NOT NULL,
    transcript_digest character varying(64) NOT NULL,
    turn_count integer NOT NULL,
    CONSTRAINT ck_post_call_reports_turn_count_bounded CHECK (((turn_count >= 0) AND (turn_count <= 80)))
);

ALTER TABLE ONLY public.post_call_reports FORCE ROW LEVEL SECURITY;

CREATE TABLE public.providers (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    location_id uuid NOT NULL,
    display_name character varying(200) NOT NULL,
    specialty character varying(200)
);

ALTER TABLE ONLY public.providers FORCE ROW LEVEL SECURITY;

CREATE TABLE public.published_configurations (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    version integer NOT NULL,
    source_revision integer NOT NULL,
    document jsonb NOT NULL,
    content_hash character varying(64) NOT NULL,
    published_by uuid,
    platform_user_id uuid,
    CONSTRAINT published_configurations_version_check CHECK ((version > 0))
);

ALTER TABLE ONLY public.published_configurations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.role_permissions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    role_id uuid NOT NULL,
    key character varying(80) NOT NULL,
    allowed boolean DEFAULT false NOT NULL
);

ALTER TABLE ONLY public.role_permissions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.support_sessions (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    platform_user_id uuid NOT NULL,
    platform_session_id uuid NOT NULL,
    token_digest character varying(64) NOT NULL,
    reason character varying(500) NOT NULL,
    capabilities jsonb NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_support_sessions_support_capabilities_array CHECK ((jsonb_typeof(capabilities) = 'array'::text))
);

ALTER TABLE ONLY public.support_sessions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenant_integrations (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    purpose character varying(20) NOT NULL,
    slot character varying(10) NOT NULL,
    provider character varying(30) NOT NULL,
    ownership character varying(10) NOT NULL,
    account_identifier character varying(64),
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(20) NOT NULL,
    revision integer NOT NULL,
    active_version_id uuid,
    last_checked_at timestamp with time zone,
    CONSTRAINT ck_tenant_integrations_ownership_allowed CHECK (((ownership)::text = ANY ((ARRAY['byo'::character varying, 'platform'::character varying])::text[]))),
    CONSTRAINT ck_tenant_integrations_positive_revision CHECK ((revision >= 1)),
    CONSTRAINT ck_tenant_integrations_purpose_allowed CHECK (((purpose)::text = ANY ((ARRAY['telephony'::character varying, 'stt'::character varying, 'tts'::character varying, 'llm'::character varying, 'embeddings'::character varying])::text[]))),
    CONSTRAINT ck_tenant_integrations_slot_allowed CHECK (((slot)::text = 'primary'::text)),
    CONSTRAINT ck_tenant_integrations_status_allowed CHECK (((status)::text = ANY ((ARRAY['not_configured'::character varying, 'saved_unverified'::character varying, 'verified'::character varying, 'invalid'::character varying, 'revoked'::character varying])::text[])))
);

ALTER TABLE ONLY public.tenant_integrations FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenant_onboardings (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    admin_id uuid NOT NULL,
    location_id uuid NOT NULL,
    creator_id uuid NOT NULL,
    key_digest character varying(64) NOT NULL,
    request_digest character varying(64) NOT NULL,
    revision integer NOT NULL,
    status character varying(30) NOT NULL,
    draft jsonb NOT NULL,
    CONSTRAINT ck_tenant_onboardings_onboarding_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'invited'::character varying])::text[]))),
    CONSTRAINT ck_tenant_onboardings_positive_revision CHECK ((revision >= 1))
);

ALTER TABLE ONLY public.tenant_onboardings FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenant_roles (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    name character varying(100) NOT NULL,
    description character varying(500) DEFAULT ''::character varying NOT NULL,
    template_key character varying(30),
    revision integer DEFAULT 1 NOT NULL,
    CONSTRAINT tenant_roles_revision_check CHECK ((revision > 0))
);

ALTER TABLE ONLY public.tenant_roles FORCE ROW LEVEL SECURITY;

CREATE TABLE public.tenants (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    name character varying(200) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    language_policy jsonb DEFAULT '{}'::jsonb NOT NULL,
    human_transfer_number character varying(16),
    verification_policy jsonb DEFAULT '{}'::jsonb NOT NULL,
    service_state character varying(20) DEFAULT 'setup'::character varying NOT NULL,
    requires_saas_activation boolean DEFAULT true NOT NULL,
    CONSTRAINT ck_tenants_human_transfer_e164 CHECK (((human_transfer_number IS NULL) OR ((human_transfer_number)::text ~ '^\+[1-9][0-9]{7,14}$'::text))),
    CONSTRAINT ck_tenants_service_state_allowed CHECK (((service_state)::text = ANY ((ARRAY['setup'::character varying, 'live'::character varying])::text[])))
);

ALTER TABLE ONLY public.tenants FORCE ROW LEVEL SECURITY;

CREATE TABLE public.users (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tenant_id uuid NOT NULL,
    email character varying(254) NOT NULL,
    display_name character varying(200) NOT NULL,
    role character varying(20) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    role_id uuid,
    CONSTRAINT ck_users_role_allowed CHECK (((role)::text = ANY ((ARRAY['admin'::character varying, 'member'::character varying])::text[])))
);

ALTER TABLE ONLY public.users FORCE ROW LEVEL SECURITY;

CREATE TABLE public.workflow_intents (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind character varying(40) NOT NULL,
    resource_id uuid NOT NULL,
    generation integer NOT NULL,
    status character varying(20) NOT NULL,
    attempts integer NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_until timestamp with time zone,
    lease_token uuid,
    dispatched_at timestamp with time zone,
    last_error character varying(40),
    CONSTRAINT ck_workflow_intents_kind_allowed CHECK (((kind)::text = ANY ((ARRAY['document_ingestion'::character varying, 'post_call'::character varying, 'tenant_onboarding'::character varying, 'appointment_reminder'::character varying])::text[]))),
    CONSTRAINT ck_workflow_intents_nonnegative_counts CHECK (((generation >= 1) AND (attempts >= 0))),
    CONSTRAINT ck_workflow_intents_status_allowed CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'dispatching'::character varying, 'dispatched'::character varying, 'dead'::character varying])::text[])))
);

ALTER TABLE ONLY public.workflow_intents FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.business_holidays
    ADD CONSTRAINT business_holidays_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.business_hours_policies
    ADD CONSTRAINT business_hours_policies_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.business_opening_windows
    ADD CONSTRAINT business_opening_windows_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.configuration_drafts
    ADD CONSTRAINT configuration_drafts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.configuration_events
    ADD CONSTRAINT configuration_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT ex_appointments_patient_overlap EXCLUDE USING gist (tenant_id WITH =, patient_id WITH =, tstzrange(starts_at, ends_at, '[)'::text) WITH &&) WHERE (((status)::text = 'booked'::text));

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT ex_appointments_provider_overlap EXCLUDE USING gist (tenant_id WITH =, provider_id WITH =, tstzrange(starts_at, ends_at, '[)'::text) WITH &&) WHERE (((status)::text = 'booked'::text));

ALTER TABLE ONLY public.availability
    ADD CONSTRAINT ex_availability_provider_overlap EXCLUDE USING gist (tenant_id WITH =, provider_id WITH =, tstzrange(starts_at, ends_at, '[)'::text) WITH &&);

ALTER TABLE ONLY public.api_credentials
    ADD CONSTRAINT pk_api_credentials PRIMARY KEY (id);

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT pk_appointments PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT pk_audit_events PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_challenges
    ADD CONSTRAINT pk_auth_challenges PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_mail
    ADD CONSTRAINT pk_auth_mail PRIMARY KEY (id);

ALTER TABLE ONLY public.availability
    ADD CONSTRAINT pk_availability PRIMARY KEY (id);

ALTER TABLE ONLY public.business_directory_mutations
    ADD CONSTRAINT pk_business_directory_mutations PRIMARY KEY (id);

ALTER TABLE ONLY public.business_resources
    ADD CONSTRAINT pk_business_resources PRIMARY KEY (id);

ALTER TABLE ONLY public.business_service_resources
    ADD CONSTRAINT pk_business_service_resources PRIMARY KEY (id);

ALTER TABLE ONLY public.business_service_staff
    ADD CONSTRAINT pk_business_service_staff PRIMARY KEY (id);

ALTER TABLE ONLY public.business_services
    ADD CONSTRAINT pk_business_services PRIMARY KEY (id);

ALTER TABLE ONLY public.business_staff
    ADD CONSTRAINT pk_business_staff PRIMARY KEY (id);

ALTER TABLE ONLY public.business_staff_holidays
    ADD CONSTRAINT pk_business_staff_holidays PRIMARY KEY (id);

ALTER TABLE ONLY public.business_staff_windows
    ADD CONSTRAINT pk_business_staff_windows PRIMARY KEY (id);

ALTER TABLE ONLY public.call_transcripts
    ADD CONSTRAINT pk_call_transcripts PRIMARY KEY (id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT pk_calls PRIMARY KEY (id);

ALTER TABLE ONLY public.configuration_resources
    ADD CONSTRAINT pk_configuration_resources PRIMARY KEY (id);

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT pk_document_chunks PRIMARY KEY (id);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT pk_document_versions PRIMARY KEY (id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT pk_documents PRIMARY KEY (id);

ALTER TABLE ONLY public.follow_up_events
    ADD CONSTRAINT pk_follow_up_events PRIMARY KEY (id);

ALTER TABLE ONLY public.human_accounts
    ADD CONSTRAINT pk_human_accounts PRIMARY KEY (id);

ALTER TABLE ONLY public.human_identities
    ADD CONSTRAINT pk_human_identities PRIMARY KEY (id);

ALTER TABLE ONLY public.human_sessions
    ADD CONSTRAINT pk_human_sessions PRIMARY KEY (id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT pk_idempotency_records PRIMARY KEY (id);

ALTER TABLE ONLY public.identity_memberships
    ADD CONSTRAINT pk_identity_memberships PRIMARY KEY (id);

ALTER TABLE ONLY public.identity_sessions
    ADD CONSTRAINT pk_identity_sessions PRIMARY KEY (id);

ALTER TABLE ONLY public.integration_credential_versions
    ADD CONSTRAINT pk_integration_credential_versions PRIMARY KEY (id);

ALTER TABLE ONLY public.knowledge_jobs
    ADD CONSTRAINT pk_knowledge_jobs PRIMARY KEY (id);

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT pk_locations PRIMARY KEY (id);

ALTER TABLE ONLY public.outbox_events
    ADD CONSTRAINT pk_outbox_events PRIMARY KEY (id);

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT pk_patients PRIMARY KEY (id);

ALTER TABLE ONLY public.phone_numbers
    ADD CONSTRAINT pk_phone_numbers PRIMARY KEY (id);

ALTER TABLE ONLY public.platform_audit_events
    ADD CONSTRAINT pk_platform_audit_events PRIMARY KEY (id);

ALTER TABLE ONLY public.platform_sessions
    ADD CONSTRAINT pk_platform_sessions PRIMARY KEY (id);

ALTER TABLE ONLY public.platform_users
    ADD CONSTRAINT pk_platform_users PRIMARY KEY (id);

ALTER TABLE ONLY public.post_call_reports
    ADD CONSTRAINT pk_post_call_reports PRIMARY KEY (id);

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT pk_providers PRIMARY KEY (id);

ALTER TABLE ONLY public.support_sessions
    ADD CONSTRAINT pk_support_sessions PRIMARY KEY (id);

ALTER TABLE ONLY public.tenant_integrations
    ADD CONSTRAINT pk_tenant_integrations PRIMARY KEY (id);

ALTER TABLE ONLY public.tenant_onboardings
    ADD CONSTRAINT pk_tenant_onboardings PRIMARY KEY (id);

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT pk_tenants PRIMARY KEY (id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);

ALTER TABLE ONLY public.workflow_intents
    ADD CONSTRAINT pk_workflow_intents PRIMARY KEY (id);

ALTER TABLE ONLY public.platform_authenticators
    ADD CONSTRAINT platform_authenticators_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.published_configurations
    ADD CONSTRAINT published_configurations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT tenant_roles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT uq_appointments_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.auth_challenges
    ADD CONSTRAINT uq_auth_challenges_key UNIQUE (tenant_id, key_digest);

ALTER TABLE ONLY public.auth_challenges
    ADD CONSTRAINT uq_auth_challenges_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.auth_mail
    ADD CONSTRAINT uq_auth_mail_challenge UNIQUE (challenge_id);

ALTER TABLE ONLY public.business_holidays
    ADD CONSTRAINT uq_business_holidays_day UNIQUE (tenant_id, policy_id, day);

ALTER TABLE ONLY public.business_hours_policies
    ADD CONSTRAINT uq_business_hours_policies_location UNIQUE NULLS NOT DISTINCT (tenant_id, location_id);

ALTER TABLE ONLY public.business_hours_policies
    ADD CONSTRAINT uq_business_hours_policies_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.business_resources
    ADD CONSTRAINT uq_business_resources_name UNIQUE (tenant_id, name);

ALTER TABLE ONLY public.business_resources
    ADD CONSTRAINT uq_business_resources_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.business_service_resources
    ADD CONSTRAINT uq_business_service_resources_binding UNIQUE (tenant_id, service_id, resource_id);

ALTER TABLE ONLY public.business_service_staff
    ADD CONSTRAINT uq_business_service_staff_binding UNIQUE (tenant_id, service_id, staff_id);

ALTER TABLE ONLY public.business_services
    ADD CONSTRAINT uq_business_services_name UNIQUE (tenant_id, name);

ALTER TABLE ONLY public.business_services
    ADD CONSTRAINT uq_business_services_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.business_staff_holidays
    ADD CONSTRAINT uq_business_staff_holidays_day UNIQUE (tenant_id, staff_id, day);

ALTER TABLE ONLY public.business_staff
    ADD CONSTRAINT uq_business_staff_name UNIQUE (tenant_id, name);

ALTER TABLE ONLY public.business_staff
    ADD CONSTRAINT uq_business_staff_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.call_transcripts
    ADD CONSTRAINT uq_call_transcript UNIQUE (tenant_id, call_id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT uq_calls_provider_call_id UNIQUE (provider_call_id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT uq_calls_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.configuration_drafts
    ADD CONSTRAINT uq_configuration_drafts_tenant UNIQUE (tenant_id);

ALTER TABLE ONLY public.configuration_events
    ADD CONSTRAINT uq_configuration_events_publication UNIQUE (tenant_id, configuration_id);

ALTER TABLE ONLY public.configuration_resources
    ADD CONSTRAINT uq_configuration_resources_name UNIQUE (tenant_id, kind, name);

ALTER TABLE ONLY public.configuration_resources
    ADD CONSTRAINT uq_configuration_resources_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT uq_document_chunks_tenant_id UNIQUE (tenant_id, document_version_id, ordinal);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT uq_document_versions_tenant_document_version UNIQUE (tenant_id, document_id, version);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT uq_document_versions_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_tenant_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.follow_up_events
    ADD CONSTRAINT uq_follow_up_call_kind UNIQUE (tenant_id, call_id, kind);

ALTER TABLE ONLY public.human_accounts
    ADD CONSTRAINT uq_human_accounts_identity UNIQUE (tenant_id, user_id, identity_id);

ALTER TABLE ONLY public.human_accounts
    ADD CONSTRAINT uq_human_accounts_membership UNIQUE (tenant_id, user_id);

ALTER TABLE ONLY public.human_identities
    ADD CONSTRAINT uq_human_identities_email UNIQUE (email);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT uq_idempotency_actor_key UNIQUE (tenant_id, actor_id, key_digest);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT uq_idempotency_call_key UNIQUE (tenant_id, call_id, key_digest);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT uq_idempotency_support_key UNIQUE (tenant_id, support_session_id, key_digest);

ALTER TABLE ONLY public.identity_memberships
    ADD CONSTRAINT uq_identity_memberships_member UNIQUE (tenant_id, user_id);

ALTER TABLE ONLY public.integration_credential_versions
    ADD CONSTRAINT uq_integration_credential_versions_key UNIQUE (integration_id, key_digest);

ALTER TABLE ONLY public.integration_credential_versions
    ADD CONSTRAINT uq_integration_credential_versions_n UNIQUE (integration_id, version);

ALTER TABLE ONLY public.integration_credential_versions
    ADD CONSTRAINT uq_integration_credential_versions_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT uq_locations_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.outbox_events
    ADD CONSTRAINT uq_outbox_aggregate_version UNIQUE (tenant_id, aggregate_id, aggregate_version);

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT uq_patients_account_identifier UNIQUE (tenant_id, account_identifier);

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT uq_patients_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.phone_numbers
    ADD CONSTRAINT uq_phone_numbers_number UNIQUE (number);

ALTER TABLE ONLY public.phone_numbers
    ADD CONSTRAINT uq_phone_numbers_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.platform_authenticators
    ADD CONSTRAINT uq_platform_authenticators_user_id UNIQUE (user_id);

ALTER TABLE ONLY public.platform_users
    ADD CONSTRAINT uq_platform_users_email UNIQUE (email);

ALTER TABLE ONLY public.post_call_reports
    ADD CONSTRAINT uq_post_call_report UNIQUE (tenant_id, call_id);

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT uq_providers_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.published_configurations
    ADD CONSTRAINT uq_published_configurations_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.published_configurations
    ADD CONSTRAINT uq_published_configurations_version UNIQUE (tenant_id, version);

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT uq_role_permissions_key UNIQUE (tenant_id, role_id, key);

ALTER TABLE ONLY public.support_sessions
    ADD CONSTRAINT uq_support_sessions_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.tenant_integrations
    ADD CONSTRAINT uq_tenant_integrations_binding UNIQUE (tenant_id, purpose, slot);

ALTER TABLE ONLY public.tenant_integrations
    ADD CONSTRAINT uq_tenant_integrations_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.tenant_onboardings
    ADD CONSTRAINT uq_tenant_onboardings_creation_key UNIQUE (creator_id, key_digest);

ALTER TABLE ONLY public.tenant_onboardings
    ADD CONSTRAINT uq_tenant_onboardings_tenant_id UNIQUE (tenant_id);

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT uq_tenant_roles_name UNIQUE (tenant_id, name);

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT uq_tenant_roles_template UNIQUE (tenant_id, template_key);

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT uq_tenant_roles_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_tenant_id_email UNIQUE (tenant_id, email);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE ONLY public.workflow_intents
    ADD CONSTRAINT uq_workflow_intent UNIQUE (tenant_id, kind, resource_id, generation);

CREATE INDEX ix_api_credentials_tenant_id ON public.api_credentials USING btree (tenant_id);

CREATE INDEX ix_appointments_tenant_id ON public.appointments USING btree (tenant_id);

CREATE INDEX ix_audit_events_tenant_id ON public.audit_events USING btree (tenant_id);

CREATE INDEX ix_auth_challenges_tenant_id ON public.auth_challenges USING btree (tenant_id);

CREATE INDEX ix_auth_mail_tenant_id ON public.auth_mail USING btree (tenant_id);

CREATE INDEX ix_availability_tenant_id ON public.availability USING btree (tenant_id);

CREATE INDEX ix_business_directory_mutations_tenant_id ON public.business_directory_mutations USING btree (tenant_id);

CREATE INDEX ix_business_holidays_tenant_id ON public.business_holidays USING btree (tenant_id);

CREATE INDEX ix_business_hours_policies_tenant_id ON public.business_hours_policies USING btree (tenant_id);

CREATE INDEX ix_business_opening_windows_tenant_id ON public.business_opening_windows USING btree (tenant_id);

CREATE INDEX ix_business_resources_tenant_id ON public.business_resources USING btree (tenant_id);

CREATE INDEX ix_business_service_resources_tenant_id ON public.business_service_resources USING btree (tenant_id);

CREATE INDEX ix_business_service_staff_tenant_id ON public.business_service_staff USING btree (tenant_id);

CREATE INDEX ix_business_services_tenant_id ON public.business_services USING btree (tenant_id);

CREATE INDEX ix_business_staff_holidays_tenant_id ON public.business_staff_holidays USING btree (tenant_id);

CREATE INDEX ix_business_staff_tenant_id ON public.business_staff USING btree (tenant_id);

CREATE INDEX ix_business_staff_windows_tenant_id ON public.business_staff_windows USING btree (tenant_id);

CREATE INDEX ix_call_transcripts_tenant_created ON public.call_transcripts USING btree (tenant_id, created_at);

CREATE INDEX ix_calls_tenant_id ON public.calls USING btree (tenant_id);

CREATE INDEX ix_configuration_drafts_tenant_id ON public.configuration_drafts USING btree (tenant_id);

CREATE INDEX ix_configuration_events_pending ON public.configuration_events USING btree (available_at, created_at, version) WHERE (delivered_at IS NULL);

CREATE INDEX ix_configuration_events_tenant_id ON public.configuration_events USING btree (tenant_id);

CREATE INDEX ix_configuration_resources_tenant_id ON public.configuration_resources USING btree (tenant_id);

CREATE INDEX ix_document_chunks_embedding ON public.document_chunks USING hnsw (embedding public.vector_cosine_ops);

CREATE INDEX ix_document_chunks_tenant ON public.document_chunks USING btree (tenant_id);

CREATE INDEX ix_document_versions_queue ON public.document_versions USING btree (status, updated_at);

CREATE INDEX ix_documents_tenant ON public.documents USING btree (tenant_id);

CREATE INDEX ix_human_sessions_tenant_id ON public.human_sessions USING btree (tenant_id);

CREATE INDEX ix_idempotency_records_tenant_id ON public.idempotency_records USING btree (tenant_id);

CREATE INDEX ix_identity_memberships_identity_id ON public.identity_memberships USING btree (identity_id);

CREATE INDEX ix_identity_sessions_identity_id ON public.identity_sessions USING btree (identity_id);

CREATE INDEX ix_integration_credential_versions_tenant_id ON public.integration_credential_versions USING btree (tenant_id);

CREATE INDEX ix_knowledge_jobs_claim ON public.knowledge_jobs USING btree (status, available_at);

CREATE INDEX ix_locations_tenant_id ON public.locations USING btree (tenant_id);

CREATE INDEX ix_outbox_delivery ON public.outbox_events USING btree (status, available_at, created_at);

CREATE INDEX ix_outbox_events_tenant_id ON public.outbox_events USING btree (tenant_id);

CREATE INDEX ix_patients_tenant_id ON public.patients USING btree (tenant_id);

CREATE INDEX ix_phone_numbers_tenant_id ON public.phone_numbers USING btree (tenant_id);

CREATE INDEX ix_platform_audit_events_support_session_id ON public.platform_audit_events USING btree (support_session_id);

CREATE INDEX ix_platform_audit_events_tenant_id ON public.platform_audit_events USING btree (tenant_id);

CREATE INDEX ix_platform_audit_events_user_id ON public.platform_audit_events USING btree (user_id);

CREATE INDEX ix_platform_sessions_user_id ON public.platform_sessions USING btree (user_id);

CREATE INDEX ix_post_call_reports_tenant_ended ON public.post_call_reports USING btree (tenant_id, call_ended_at);

CREATE INDEX ix_providers_tenant_id ON public.providers USING btree (tenant_id);

CREATE INDEX ix_published_configurations_tenant_id ON public.published_configurations USING btree (tenant_id);

CREATE INDEX ix_role_permissions_tenant_id ON public.role_permissions USING btree (tenant_id);

CREATE INDEX ix_support_sessions_platform_user_id ON public.support_sessions USING btree (platform_user_id);

CREATE INDEX ix_support_sessions_tenant_id ON public.support_sessions USING btree (tenant_id);

CREATE INDEX ix_tenant_integrations_tenant_id ON public.tenant_integrations USING btree (tenant_id);

CREATE INDEX ix_tenant_roles_tenant_id ON public.tenant_roles USING btree (tenant_id);

CREATE INDEX ix_users_tenant_id ON public.users USING btree (tenant_id);

CREATE INDEX ix_workflow_intents_dispatch ON public.workflow_intents USING btree (status, available_at, created_at);

CREATE TRIGGER accepted_identity_membership AFTER INSERT ON public.human_accounts FOR EACH ROW EXECUTE FUNCTION public.index_accepted_membership();

CREATE TRIGGER audit_no_changes BEFORE DELETE OR UPDATE ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.reject_audit_mutation();

CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON public.audit_events FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_mutation();

CREATE TRIGGER platform_audit_no_changes BEFORE DELETE OR UPDATE ON public.platform_audit_events FOR EACH ROW EXECUTE FUNCTION public.reject_audit_mutation();

CREATE TRIGGER platform_audit_no_truncate BEFORE TRUNCATE ON public.platform_audit_events FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_mutation();

ALTER TABLE ONLY public.business_holidays
    ADD CONSTRAINT business_holidays_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_hours_policies
    ADD CONSTRAINT business_hours_policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_opening_windows
    ADD CONSTRAINT business_opening_windows_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.configuration_drafts
    ADD CONSTRAINT configuration_drafts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.configuration_events
    ADD CONSTRAINT configuration_events_tenant_id_configuration_id_fkey FOREIGN KEY (tenant_id, configuration_id) REFERENCES public.published_configurations(tenant_id, id);

ALTER TABLE ONLY public.configuration_events
    ADD CONSTRAINT configuration_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.configuration_resources
    ADD CONSTRAINT configuration_resources_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.api_credentials
    ADD CONSTRAINT fk_api_credentials_tenant_id_users FOREIGN KEY (tenant_id, user_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT fk_appointments_tenant_id_patients FOREIGN KEY (tenant_id, patient_id) REFERENCES public.patients(tenant_id, id);

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT fk_appointments_tenant_id_providers FOREIGN KEY (tenant_id, provider_id) REFERENCES public.providers(tenant_id, id);

ALTER TABLE ONLY public.appointments
    ADD CONSTRAINT fk_appointments_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT fk_audit_events_platform_user_id FOREIGN KEY (platform_user_id) REFERENCES public.platform_users(id);

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT fk_audit_events_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT fk_audit_events_tenant_id_users FOREIGN KEY (tenant_id, actor_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.auth_challenges
    ADD CONSTRAINT fk_auth_challenges_tenant_id_users FOREIGN KEY (tenant_id, user_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.auth_mail
    ADD CONSTRAINT fk_auth_mail_tenant_id_auth_challenges FOREIGN KEY (tenant_id, challenge_id) REFERENCES public.auth_challenges(tenant_id, id);

ALTER TABLE ONLY public.availability
    ADD CONSTRAINT fk_availability_tenant_id_providers FOREIGN KEY (tenant_id, provider_id) REFERENCES public.providers(tenant_id, id);

ALTER TABLE ONLY public.availability
    ADD CONSTRAINT fk_availability_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_directory_mutations
    ADD CONSTRAINT fk_business_directory_mutations_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_holidays
    ADD CONSTRAINT fk_business_holidays_policy FOREIGN KEY (tenant_id, policy_id) REFERENCES public.business_hours_policies(tenant_id, id);

ALTER TABLE ONLY public.business_hours_policies
    ADD CONSTRAINT fk_business_hours_policies_location FOREIGN KEY (tenant_id, location_id) REFERENCES public.locations(tenant_id, id);

ALTER TABLE ONLY public.business_opening_windows
    ADD CONSTRAINT fk_business_opening_windows_policy FOREIGN KEY (tenant_id, policy_id) REFERENCES public.business_hours_policies(tenant_id, id);

ALTER TABLE ONLY public.business_resources
    ADD CONSTRAINT fk_business_resources_location FOREIGN KEY (tenant_id, location_id) REFERENCES public.locations(tenant_id, id);

ALTER TABLE ONLY public.business_resources
    ADD CONSTRAINT fk_business_resources_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_service_resources
    ADD CONSTRAINT fk_business_service_resources_resource FOREIGN KEY (tenant_id, resource_id) REFERENCES public.business_resources(tenant_id, id);

ALTER TABLE ONLY public.business_service_resources
    ADD CONSTRAINT fk_business_service_resources_service FOREIGN KEY (tenant_id, service_id) REFERENCES public.business_services(tenant_id, id);

ALTER TABLE ONLY public.business_service_resources
    ADD CONSTRAINT fk_business_service_resources_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_service_staff
    ADD CONSTRAINT fk_business_service_staff_service FOREIGN KEY (tenant_id, service_id) REFERENCES public.business_services(tenant_id, id);

ALTER TABLE ONLY public.business_service_staff
    ADD CONSTRAINT fk_business_service_staff_staff FOREIGN KEY (tenant_id, staff_id) REFERENCES public.business_staff(tenant_id, id);

ALTER TABLE ONLY public.business_service_staff
    ADD CONSTRAINT fk_business_service_staff_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_services
    ADD CONSTRAINT fk_business_services_location FOREIGN KEY (tenant_id, location_id) REFERENCES public.locations(tenant_id, id);

ALTER TABLE ONLY public.business_services
    ADD CONSTRAINT fk_business_services_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_staff
    ADD CONSTRAINT fk_business_staff_calendar FOREIGN KEY (tenant_id, calendar_connection_id) REFERENCES public.tenant_integrations(tenant_id, id);

ALTER TABLE ONLY public.business_staff_holidays
    ADD CONSTRAINT fk_business_staff_holidays_staff FOREIGN KEY (tenant_id, staff_id) REFERENCES public.business_staff(tenant_id, id);

ALTER TABLE ONLY public.business_staff_holidays
    ADD CONSTRAINT fk_business_staff_holidays_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_staff
    ADD CONSTRAINT fk_business_staff_location FOREIGN KEY (tenant_id, location_id) REFERENCES public.locations(tenant_id, id);

ALTER TABLE ONLY public.business_staff
    ADD CONSTRAINT fk_business_staff_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.business_staff
    ADD CONSTRAINT fk_business_staff_user FOREIGN KEY (tenant_id, user_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.business_staff_windows
    ADD CONSTRAINT fk_business_staff_windows_staff FOREIGN KEY (tenant_id, staff_id) REFERENCES public.business_staff(tenant_id, id);

ALTER TABLE ONLY public.business_staff_windows
    ADD CONSTRAINT fk_business_staff_windows_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.call_transcripts
    ADD CONSTRAINT fk_call_transcripts_tenant_id_calls FOREIGN KEY (tenant_id, call_id) REFERENCES public.calls(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_access_patient_id_patients FOREIGN KEY (tenant_id, access_patient_id) REFERENCES public.patients(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_configuration_version FOREIGN KEY (tenant_id, configuration_version) REFERENCES public.published_configurations(tenant_id, version);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_credential_version FOREIGN KEY (tenant_id, credential_version_id) REFERENCES public.integration_credential_versions(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_integration FOREIGN KEY (tenant_id, integration_id) REFERENCES public.tenant_integrations(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_scope_agent_id FOREIGN KEY (tenant_id, agent_id) REFERENCES public.configuration_resources(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_scope_channel_id FOREIGN KEY (tenant_id, channel_id) REFERENCES public.configuration_resources(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_scope_location_id FOREIGN KEY (tenant_id, location_id) REFERENCES public.locations(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_tenant_id_phone_numbers FOREIGN KEY (tenant_id, phone_number_id) REFERENCES public.phone_numbers(tenant_id, id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.calls
    ADD CONSTRAINT fk_calls_verified_patient_id_patients FOREIGN KEY (tenant_id, verified_patient_id) REFERENCES public.patients(tenant_id, id);

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT fk_document_chunks_tenant_id_document_versions FOREIGN KEY (tenant_id, document_version_id) REFERENCES public.document_versions(tenant_id, id);

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT fk_document_chunks_tenant_id_documents FOREIGN KEY (tenant_id, document_id) REFERENCES public.documents(tenant_id, id);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT fk_document_versions_tenant_id_documents FOREIGN KEY (tenant_id, document_id) REFERENCES public.documents(tenant_id, id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.follow_up_events
    ADD CONSTRAINT fk_follow_up_events_tenant_id_calls FOREIGN KEY (tenant_id, call_id) REFERENCES public.calls(tenant_id, id);

ALTER TABLE ONLY public.human_accounts
    ADD CONSTRAINT fk_human_accounts_identity_id_human_identities FOREIGN KEY (identity_id) REFERENCES public.human_identities(id);

ALTER TABLE ONLY public.human_accounts
    ADD CONSTRAINT fk_human_accounts_tenant_id_users FOREIGN KEY (tenant_id, user_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.human_sessions
    ADD CONSTRAINT fk_human_sessions_identity_id_human_identities FOREIGN KEY (identity_id) REFERENCES public.human_identities(id);

ALTER TABLE ONLY public.human_sessions
    ADD CONSTRAINT fk_human_sessions_tenant_id_human_accounts FOREIGN KEY (tenant_id, user_id, identity_id) REFERENCES public.human_accounts(tenant_id, user_id, identity_id);

ALTER TABLE ONLY public.human_sessions
    ADD CONSTRAINT fk_human_sessions_tenant_id_users FOREIGN KEY (tenant_id, user_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT fk_idempotency_call FOREIGN KEY (tenant_id, call_id) REFERENCES public.calls(tenant_id, id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT fk_idempotency_platform_user FOREIGN KEY (platform_user_id) REFERENCES public.platform_users(id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT fk_idempotency_records_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT fk_idempotency_records_tenant_id_users FOREIGN KEY (tenant_id, actor_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.idempotency_records
    ADD CONSTRAINT fk_idempotency_support_session FOREIGN KEY (tenant_id, support_session_id) REFERENCES public.support_sessions(tenant_id, id);

ALTER TABLE ONLY public.identity_memberships
    ADD CONSTRAINT fk_identity_memberships_tenant_id_human_accounts FOREIGN KEY (tenant_id, user_id, identity_id) REFERENCES public.human_accounts(tenant_id, user_id, identity_id);

ALTER TABLE ONLY public.integration_credential_versions
    ADD CONSTRAINT fk_integration_credential_versions_integration FOREIGN KEY (tenant_id, integration_id) REFERENCES public.tenant_integrations(tenant_id, id);

ALTER TABLE ONLY public.knowledge_jobs
    ADD CONSTRAINT fk_knowledge_jobs_tenant_id_document_versions FOREIGN KEY (tenant_id, document_version_id) REFERENCES public.document_versions(tenant_id, id);

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT fk_locations_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.outbox_events
    ADD CONSTRAINT fk_outbox_events_tenant_id_appointments FOREIGN KEY (tenant_id, aggregate_id) REFERENCES public.appointments(tenant_id, id);

ALTER TABLE ONLY public.outbox_events
    ADD CONSTRAINT fk_outbox_events_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT fk_patients_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.phone_numbers
    ADD CONSTRAINT fk_phone_numbers_integration FOREIGN KEY (tenant_id, integration_id) REFERENCES public.tenant_integrations(tenant_id, id);

ALTER TABLE ONLY public.phone_numbers
    ADD CONSTRAINT fk_phone_numbers_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.platform_audit_events
    ADD CONSTRAINT fk_platform_audit_events_user_id_platform_users FOREIGN KEY (user_id) REFERENCES public.platform_users(id);

ALTER TABLE ONLY public.platform_sessions
    ADD CONSTRAINT fk_platform_sessions_user_id_platform_users FOREIGN KEY (user_id) REFERENCES public.platform_users(id);

ALTER TABLE ONLY public.post_call_reports
    ADD CONSTRAINT fk_post_call_reports_tenant_id_calls FOREIGN KEY (tenant_id, call_id) REFERENCES public.calls(tenant_id, id);

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT fk_providers_tenant_id_locations FOREIGN KEY (tenant_id, location_id) REFERENCES public.locations(tenant_id, id);

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT fk_providers_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.support_sessions
    ADD CONSTRAINT fk_support_sessions_platform_session_id_platform_sessions FOREIGN KEY (platform_session_id) REFERENCES public.platform_sessions(id);

ALTER TABLE ONLY public.support_sessions
    ADD CONSTRAINT fk_support_sessions_platform_user_id_platform_users FOREIGN KEY (platform_user_id) REFERENCES public.platform_users(id);

ALTER TABLE ONLY public.support_sessions
    ADD CONSTRAINT fk_support_sessions_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.tenant_integrations
    ADD CONSTRAINT fk_tenant_integrations_active_version FOREIGN KEY (tenant_id, active_version_id) REFERENCES public.integration_credential_versions(tenant_id, id);

ALTER TABLE ONLY public.tenant_integrations
    ADD CONSTRAINT fk_tenant_integrations_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.tenant_onboardings
    ADD CONSTRAINT fk_tenant_onboardings_creator_id_platform_users FOREIGN KEY (creator_id) REFERENCES public.platform_users(id);

ALTER TABLE ONLY public.tenant_onboardings
    ADD CONSTRAINT fk_tenant_onboardings_tenant_id_locations FOREIGN KEY (tenant_id, location_id) REFERENCES public.locations(tenant_id, id);

ALTER TABLE ONLY public.tenant_onboardings
    ADD CONSTRAINT fk_tenant_onboardings_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.tenant_onboardings
    ADD CONSTRAINT fk_tenant_onboardings_tenant_id_users FOREIGN KEY (tenant_id, admin_id) REFERENCES public.users(tenant_id, id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT fk_users_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT fk_users_tenant_role FOREIGN KEY (tenant_id, role_id) REFERENCES public.tenant_roles(tenant_id, id);

ALTER TABLE ONLY public.workflow_intents
    ADD CONSTRAINT fk_workflow_intents_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.identity_memberships
    ADD CONSTRAINT identity_memberships_identity_id_fkey FOREIGN KEY (identity_id) REFERENCES public.human_identities(id);

ALTER TABLE ONLY public.identity_sessions
    ADD CONSTRAINT identity_sessions_identity_id_fkey FOREIGN KEY (identity_id) REFERENCES public.human_identities(id);

ALTER TABLE ONLY public.platform_authenticators
    ADD CONSTRAINT platform_authenticators_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.platform_users(id);

ALTER TABLE ONLY public.published_configurations
    ADD CONSTRAINT published_configurations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_tenant_id_role_id_fkey FOREIGN KEY (tenant_id, role_id) REFERENCES public.tenant_roles(tenant_id, id);

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT tenant_roles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);

ALTER TABLE public.api_credentials ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.appointments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.auth_challenges ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.auth_mail ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.availability ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_directory_mutations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_holidays ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_hours_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_opening_windows ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_resources ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_service_resources ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_service_staff ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_services ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_staff ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_staff_holidays ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.business_staff_windows ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.call_transcripts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.calls ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.configuration_drafts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.configuration_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.configuration_resources ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.document_chunks ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.document_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.follow_up_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.human_accounts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.human_identities ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.human_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.idempotency_records ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.identity_memberships ENABLE ROW LEVEL SECURITY;

CREATE POLICY identity_scope ON public.human_identities USING (((email)::text = NULLIF(current_setting('app.identity_email'::text, true), ''::text))) WITH CHECK (((email)::text = NULLIF(current_setting('app.identity_email'::text, true), ''::text)));

CREATE POLICY identity_scope ON public.identity_memberships USING ((EXISTS ( SELECT 1
   FROM public.human_identities i
  WHERE ((i.id = identity_memberships.identity_id) AND ((i.email)::text = NULLIF(current_setting('app.identity_email'::text, true), ''::text)))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.human_identities i
  WHERE ((i.id = identity_memberships.identity_id) AND ((i.email)::text = NULLIF(current_setting('app.identity_email'::text, true), ''::text))))));

CREATE POLICY identity_scope ON public.identity_sessions USING ((EXISTS ( SELECT 1
   FROM public.human_identities i
  WHERE ((i.id = identity_sessions.identity_id) AND ((i.email)::text = NULLIF(current_setting('app.identity_email'::text, true), ''::text)))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.human_identities i
  WHERE ((i.id = identity_sessions.identity_id) AND ((i.email)::text = NULLIF(current_setting('app.identity_email'::text, true), ''::text))))));

ALTER TABLE public.identity_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY inbound_phone_lookup ON public.phone_numbers FOR SELECT USING (((number)::text = NULLIF(current_setting('app.inbound_number'::text, true), ''::text)));

ALTER TABLE public.integration_credential_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.knowledge_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.locations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.patients ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.phone_numbers ENABLE ROW LEVEL SECURITY;

CREATE POLICY platform_onboarding_create ON public.tenant_onboardings FOR INSERT WITH CHECK (((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text)))) AND (creator_id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid)));

CREATE POLICY platform_onboarding_read ON public.tenant_onboardings FOR SELECT USING ((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text)))));

CREATE POLICY platform_onboarding_update ON public.tenant_onboardings FOR UPDATE USING ((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text)))));

CREATE POLICY platform_phone_management ON public.phone_numbers USING ((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text)))));

CREATE POLICY platform_tenant_management ON public.tenants USING ((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.platform_users p
  WHERE ((p.id = (NULLIF(current_setting('app.platform_user_id'::text, true), ''::text))::uuid) AND p.is_active AND ((p.role)::text = 'super_admin'::text)))));

ALTER TABLE public.post_call_reports ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.providers ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.published_configurations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.role_permissions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.support_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenant_integrations ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON public.api_credentials USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.appointments USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.audit_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.auth_challenges USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.auth_mail USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.availability USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_directory_mutations USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_holidays USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_hours_policies USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_opening_windows USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_resources USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_service_resources USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_service_staff USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_services USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_staff USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_staff_holidays USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.business_staff_windows USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.call_transcripts USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.calls USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.configuration_drafts USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.configuration_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.configuration_resources USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.document_chunks USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.document_versions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.documents USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.follow_up_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.human_accounts USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.human_sessions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.idempotency_records USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.integration_credential_versions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.knowledge_jobs USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.locations USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.outbox_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.patients USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.post_call_reports USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.providers USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.published_configurations USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.role_permissions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.support_sessions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.tenant_integrations USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.tenant_roles USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.tenants USING ((id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.users USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_isolation ON public.workflow_intents USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

ALTER TABLE public.tenant_onboardings ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_phone_bind ON public.phone_numbers FOR INSERT WITH CHECK (((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid) AND (integration_id IS NOT NULL)));

CREATE POLICY tenant_phone_read ON public.phone_numbers FOR SELECT USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));

CREATE POLICY tenant_phone_update ON public.phone_numbers FOR UPDATE USING (((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid) AND (integration_id IS NOT NULL))) WITH CHECK (((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid) AND (integration_id IS NOT NULL)));

ALTER TABLE public.tenant_roles ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.workflow_intents ENABLE ROW LEVEL SECURITY;
