# Phase 13 — Identity verification and protected records

Phase 13 keeps patient access bound to the active call and a patient selected by an authenticated tenant admin. A call starts unverified. Reception checks the tenant's required factors, attests verification, and issues an eight digit code that expires after a short period. The caller says the code by itself; the server verifies it before any patient-specific appointment read or write. Caller ID, names, and model output never establish identity.

Run the new migration before using the API. The migration job also provisions the restricted runtime role:

```powershell
docker compose run --rm --build migrate
```

If using host Python and your existing local PostgreSQL, run the project's migration and provisioning commands from `docs/phase2.md` instead. Phase 11's pgvector server extension must already be installed for the full migration chain.

The default policy requires reception attestation. A tenant admin can require date of birth and an account identifier as additional checks. Configure the policy through the authenticated endpoint:

```http
PUT /api/v1/tenants/current/verification
Authorization: Bearer <tenant-admin-token>
Content-Type: application/json

{"required_factors":["reception_attestation","date_of_birth","account_identifier"],"code_ttl_seconds":300,"access_ttl_minutes":120,"max_code_attempts":3}
```

`required_factors` must include `reception_attestation`. The API rejects unsupported or repeated factors. Tenant policy updates revoke outstanding codes and verified grants on active calls. To set or replace a patient's factors, use `PUT /api/v1/patients/{patient_id}/verification` with `{"date_of_birth":"1990-04-09","account_identifier":"PAT-137"}`. This is a full replacement; send both values when retaining both. Changing a patient's factors revokes that patient's outstanding codes and verified call grants. These fields are accepted only through the tenant admin API and are omitted from patient views. An admin cannot issue access under a policy requiring a factor that is missing or does not match the patient's record.

After checking the caller, reception sends `POST /api/v1/calls/{call_id}/patient-access` with `patient_id`, `verification_attested: true`, and the required factor values. The API returns the short-lived code with `Cache-Control: no-store`; convey it to the caller through the existing reception workflow. A successful redemption persists the verified patient in the call row and conversation state. Protected appointment operations independently check the active call, tenant, patient binding, and access expiry on every database transaction. Patient and appointment reads create audit events with the request and call IDs but no verification values. Access codes and recognizable verification values are removed before caller text reaches the language model or conversation store.

Phone caller ID alone is not accepted as proof of ownership. This phase does not add self-service SMS OTP delivery; the supported path is reception-assisted verification. For a shop requiring a phone ownership check, reception must establish that ownership outside this application before attesting the call. Configure only factors your staff can actually verify.

Run `python -m pytest tests/test_verification_phase13.py -q` for local unit coverage. The database integration case in `tests/integration/test_appointment_agent.py` needs a disposable PostgreSQL with separate migration/runtime credentials and the full migration chain.
