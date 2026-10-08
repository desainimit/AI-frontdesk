"""Relational business drafts and immutable mutation receipts; frozen PostgreSQL DDL."""

from alembic import op

revision = "0028_business_directory"
down_revision = "0027_business_hours"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE business_staff (
        name VARCHAR(200) NOT NULL,
        job_title VARCHAR(200) NOT NULL,
        email VARCHAR(254) NOT NULL,
        phone VARCHAR(16) NOT NULL,
        transfer_number VARCHAR(16),
        user_id UUID,
        location_id UUID,
        calendar_connection_id UUID,
        capacity INTEGER NOT NULL,
        is_active BOOLEAN NOT NULL,
        revision INTEGER NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_staff PRIMARY KEY (id),
        CONSTRAINT uq_business_staff_tenant_id_id UNIQUE (tenant_id, id),
        CONSTRAINT uq_business_staff_name UNIQUE (tenant_id, name),
        CONSTRAINT fk_business_staff_location FOREIGN KEY(tenant_id, location_id)
        REFERENCES locations (tenant_id, id),
        CONSTRAINT fk_business_staff_user FOREIGN KEY(tenant_id, user_id) REFERENCES
        users (tenant_id, id),
        CONSTRAINT fk_business_staff_calendar FOREIGN KEY(tenant_id,
        calendar_connection_id) REFERENCES tenant_integrations (tenant_id, id),
        CONSTRAINT ck_business_staff_positive_values CHECK (revision >= 1 AND capacity
        BETWEEN 1 AND 100),
        CONSTRAINT fk_business_staff_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES
        tenants (id)
)""")
    op.execute("""CREATE INDEX ix_business_staff_tenant_id ON business_staff (tenant_id)""")
    op.execute("""CREATE TABLE business_resources (
        name VARCHAR(200) NOT NULL,
        resource_type VARCHAR(20) NOT NULL,
        description VARCHAR(2000) NOT NULL,
        location_id UUID NOT NULL,
        capacity INTEGER NOT NULL,
        is_active BOOLEAN NOT NULL,
        revision INTEGER NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_resources PRIMARY KEY (id),
        CONSTRAINT uq_business_resources_tenant_id_id UNIQUE (tenant_id, id),
        CONSTRAINT uq_business_resources_name UNIQUE (tenant_id, name),
        CONSTRAINT fk_business_resources_location FOREIGN KEY(tenant_id, location_id)
        REFERENCES locations (tenant_id, id),
        CONSTRAINT ck_business_resources_positive_values CHECK (revision >= 1 AND
        capacity BETWEEN 1 AND 1000),
        CONSTRAINT ck_business_resources_resource_type CHECK (resource_type IN
        ('space','equipment')),
        CONSTRAINT fk_business_resources_tenant_id_tenants FOREIGN KEY(tenant_id)
        REFERENCES tenants (id)
)""")
    op.execute("""CREATE INDEX ix_business_resources_tenant_id ON business_resources (tenant_id)""")
    op.execute("""CREATE TABLE business_services (
        name VARCHAR(200) NOT NULL,
        description VARCHAR(4000) NOT NULL,
        price_minor INTEGER NOT NULL,
        currency VARCHAR(3) NOT NULL,
        duration_minutes INTEGER NOT NULL,
        buffer_before_minutes INTEGER NOT NULL,
        buffer_after_minutes INTEGER NOT NULL,
        minimum_notice_minutes INTEGER NOT NULL,
        capacity INTEGER NOT NULL,
        location_id UUID,
        is_active BOOLEAN NOT NULL,
        revision INTEGER NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_services PRIMARY KEY (id),
        CONSTRAINT uq_business_services_tenant_id_id UNIQUE (tenant_id, id),
        CONSTRAINT uq_business_services_name UNIQUE (tenant_id, name),
        CONSTRAINT fk_business_services_location FOREIGN KEY(tenant_id, location_id)
        REFERENCES locations (tenant_id, id),
        CONSTRAINT ck_business_services_positive_values CHECK (revision >= 1 AND
        price_minor BETWEEN 0 AND 1000000000 AND capacity BETWEEN 1 AND 1000 AND
        minimum_notice_minutes BETWEEN 0 AND 43200),
        CONSTRAINT ck_business_services_duration_values CHECK (duration_minutes BETWEEN
        5 AND 1440 AND buffer_before_minutes BETWEEN 0 AND 240 AND buffer_after_minutes
        BETWEEN 0 AND 240),
        CONSTRAINT ck_business_services_currency CHECK (currency = 'INR'),
        CONSTRAINT fk_business_services_tenant_id_tenants FOREIGN KEY(tenant_id)
        REFERENCES tenants (id)
)""")
    op.execute("""CREATE INDEX ix_business_services_tenant_id ON business_services (tenant_id)""")
    op.execute("""CREATE TABLE business_service_staff (
        service_id UUID NOT NULL,
        staff_id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_service_staff PRIMARY KEY (id),
        CONSTRAINT fk_business_service_staff_service FOREIGN KEY(tenant_id, service_id)
        REFERENCES business_services (tenant_id, id),
        CONSTRAINT fk_business_service_staff_staff FOREIGN KEY(tenant_id, staff_id)
        REFERENCES business_staff (tenant_id, id),
        CONSTRAINT uq_business_service_staff_binding UNIQUE (tenant_id, service_id,
        staff_id),
        CONSTRAINT fk_business_service_staff_tenant_id_tenants FOREIGN KEY(tenant_id)
        REFERENCES tenants (id)
)""")
    op.execute(
        """CREATE INDEX ix_business_service_staff_tenant_id ON business_service_staff (tenant_id)"""
    )
    op.execute("""CREATE TABLE business_service_resources (
        service_id UUID NOT NULL,
        resource_id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_service_resources PRIMARY KEY (id),
        CONSTRAINT fk_business_service_resources_service FOREIGN KEY(tenant_id,
        service_id) REFERENCES business_services (tenant_id, id),
        CONSTRAINT fk_business_service_resources_resource FOREIGN KEY(tenant_id,
        resource_id) REFERENCES business_resources (tenant_id, id),
        CONSTRAINT uq_business_service_resources_binding UNIQUE (tenant_id, service_id,
        resource_id),
        CONSTRAINT fk_business_service_resources_tenant_id_tenants FOREIGN
        KEY(tenant_id) REFERENCES tenants (id)
)""")
    op.execute("""CREATE INDEX ix_business_service_resources_tenant_id ON business_service_resources
        (tenant_id)""")
    op.execute("""CREATE TABLE business_staff_windows (
        staff_id UUID NOT NULL,
        weekday INTEGER NOT NULL,
        opens TIME WITHOUT TIME ZONE NOT NULL,
        closes TIME WITHOUT TIME ZONE NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_staff_windows PRIMARY KEY (id),
        CONSTRAINT fk_business_staff_windows_staff FOREIGN KEY(tenant_id, staff_id)
        REFERENCES business_staff (tenant_id, id),
        CONSTRAINT ck_business_staff_windows_valid_window CHECK (weekday BETWEEN 0 AND 6
        AND opens < closes),
        CONSTRAINT fk_business_staff_windows_tenant_id_tenants FOREIGN KEY(tenant_id)
        REFERENCES tenants (id)
)""")
    op.execute(
        """CREATE INDEX ix_business_staff_windows_tenant_id ON business_staff_windows (tenant_id)"""
    )
    op.execute("""CREATE TABLE business_staff_holidays (
        staff_id UUID NOT NULL,
        day DATE NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_staff_holidays PRIMARY KEY (id),
        CONSTRAINT fk_business_staff_holidays_staff FOREIGN KEY(tenant_id, staff_id)
        REFERENCES business_staff (tenant_id, id),
        CONSTRAINT uq_business_staff_holidays_day UNIQUE (tenant_id, staff_id, day),
        CONSTRAINT fk_business_staff_holidays_tenant_id_tenants FOREIGN KEY(tenant_id)
        REFERENCES tenants (id)
)""")
    op.execute(
        """CREATE INDEX ix_business_staff_holidays_tenant_id
        ON business_staff_holidays (tenant_id)"""
    )
    op.execute("""CREATE TABLE business_directory_mutations (
        request_hash VARCHAR(64) NOT NULL,
        result JSONB NOT NULL,
        tenant_id UUID NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_business_directory_mutations PRIMARY KEY (id),
        CONSTRAINT fk_business_directory_mutations_tenant_id_tenants FOREIGN
        KEY(tenant_id) REFERENCES tenants (id)
)""")
    op.execute("""CREATE INDEX ix_business_directory_mutations_tenant_id
        ON business_directory_mutations
        (tenant_id)""")
    tenant = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for name in (
        "business_staff",
        "business_resources",
        "business_services",
        "business_service_staff",
        "business_service_resources",
        "business_staff_windows",
        "business_staff_holidays",
        "business_directory_mutations",
    ):
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {name} USING ({tenant}) WITH CHECK ({tenant})"
        )


def downgrade() -> None:
    for name in (
        "business_directory_mutations",
        "business_staff_holidays",
        "business_staff_windows",
        "business_service_resources",
        "business_service_staff",
        "business_services",
        "business_resources",
        "business_staff",
    ):
        op.drop_table(name)
