from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.config.settings import Settings
from app.main import create_app
from app.platform.bootstrap import BootstrapSettings
from app.platform.passwords import hash_password, verify_password
from app.platform.schemas import LoginCommand
from app.platform.tenants import CreateTenantCommand
from app.tenants.bootstrap import BootstrapTenant
from tests.fakes import FakeDependency


def test_password_hashes_are_salted_and_fail_closed() -> None:
    password = "Test-only secure passphrase!"
    first, second = hash_password(password), hash_password(password)
    assert first != second and password not in first
    assert verify_password(password, first)
    assert not verify_password(password + "x", first)
    assert not verify_password(password, "corrupt")


def test_bootstrap_requires_both_credentials_and_masks_password() -> None:
    empty = BootstrapSettings(super_admin_email="", super_admin_password=SecretStr(""))
    assert empty.command() is None
    with pytest.raises(ValidationError):
        BootstrapSettings(
            super_admin_email="admin@example.test", super_admin_password=SecretStr("")
        ).command()
    with pytest.raises(ValidationError):
        BootstrapSettings(super_admin_email="", super_admin_password=SecretStr("a" * 20)).command()
    configured = BootstrapSettings(
        super_admin_email=" ADMIN@example.test ",
        super_admin_password=SecretStr("private-seed-password"),
    )
    command = configured.command()
    assert command and command.email == "admin@example.test"
    assert "private-seed-password" not in repr(command)
    assert "private-seed-password" not in configured.model_dump_json()
    # Boundary check for min_length=8
    with pytest.raises(ValidationError):
        BootstrapSettings(
            super_admin_email="admin@example.test", super_admin_password=SecretStr("a" * 7)
        ).command()
    valid_8 = BootstrapSettings(
        super_admin_email="admin@example.test", super_admin_password=SecretStr("a" * 8)
    ).command()
    assert valid_8 is not None


@pytest.mark.parametrize("email", [123, None, "bad"])
def test_malformed_login_email_is_validation_error(email: Any) -> None:
    with pytest.raises(ValidationError):
        LoginCommand.model_validate({"email": email, "password": "test"})


def test_platform_and_tenant_tokens_cannot_be_interchanged(settings: Settings) -> None:
    app = create_app(settings, redis_factory=lambda _: FakeDependency())
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform/me",
            headers={"Authorization": f"Bearer fd1.{uuid4()}.{uuid4()}.{'a' * 43}"},
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        response = client.get(
            "/api/v1/patients", headers={"Authorization": f"Bearer fps1.{uuid4()}.{'a' * 43}"}
        )
        assert response.status_code == 401
        # Throttling infrastructure unavailable: login fails closed before DB access.
        response = client.post(
            "/api/v1/platform/auth/login", json={"email": "admin@example.test", "password": "test"}
        )
        assert response.status_code == 503


def test_bootstrap_tenant_tenant_id_is_optional() -> None:
    command = BootstrapTenant.model_validate(
        {"name": "Clinic", "email": "admin@clinic.com", "display_name": "Admin"}
    )
    assert command.tenant_id is None
    assert command.credential_days == 30

    explicit_id = uuid4()
    command_explicit = BootstrapTenant.model_validate(
        {
            "tenant_id": str(explicit_id),
            "name": "Clinic",
            "email": "admin@clinic.com",
            "display_name": "Admin",
        }
    )
    assert command_explicit.tenant_id == explicit_id


def test_create_tenant_command_has_no_tenant_id() -> None:
    command = CreateTenantCommand.model_validate(
        {"name": "Clinic", "email": "admin@clinic.com", "display_name": "Admin"}
    )
    assert not hasattr(command, "tenant_id")
    assert command.name == "Clinic"
    # Attempting to supply tenant_id fails validation (extra='forbid')
    with pytest.raises(ValidationError):
        CreateTenantCommand.model_validate(
            {
                "tenant_id": str(uuid4()),
                "name": "Clinic",
                "email": "admin@clinic.com",
                "display_name": "Admin",
            }
        )
