"""Invariant 5 — clinical records are append-only.

"No update or delete endpoint on clinical rows. Corrections are new rows referencing the
original. The audit log is append-only."
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.main import app
from app.models import CLINICAL_TABLES, AuditLog, CuffReading
from tests.conftest import make_session_payload, post_session

MUTATING_METHODS = {"PUT", "PATCH", "DELETE"}


@pytest.mark.invariant
def test_clinical_rows_have_no_update_or_delete_route() -> None:
    """Walk the OpenAPI schema: no PUT, PATCH or DELETE anywhere in the API.

    Read from the generated schema rather than the router objects, because that is the
    contract clients see, and because FastAPI's route table nests included routers.
    """
    spec = app.openapi()

    offenders = [
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() in MUTATING_METHODS
    ]

    assert not offenders, (
        f"mutating route(s) found: {offenders}. Invariant 5: clinical records are append-only "
        f"and corrections are new rows referencing the original."
    )


#: One mutable column per clinical table, used to attempt a real UPDATE. A row-level
#: ``BEFORE UPDATE`` trigger only fires when a row matches, so an UPDATE against an empty table
#: would pass vacuously — every table below is populated first.
_UPDATE_TARGET_COLUMN = {
    "measurement_session": ("model_version", "'rewritten'"),
    "trend_estimate": ("confidence", "0.5"),
    "cuff_reading": ("systolic_mmhg", "199"),
    "medication_event": ("payload", "'{}'::jsonb"),
    "symptom_event": ("payload", "'{}'::jsonb"),
    "red_flag_event": ("payload", "'{}'::jsonb"),
    "clinician_summary": ("viewed_at", "now()"),
    "audit_log": ("actor", "'someone-else'"),
    "calibration_source_session": ("session_ptt_ms", "999.0"),
    # Rewriting a pregnancy answer is exactly the mutation this table exists to prevent.
    "patient_context": ("known_arrhythmia", "true"),
    # Rewriting what a patient reported around a past measurement is exactly the mutation this
    # table exists to prevent. phr_profile is deliberately absent: it is mutable by design.
    "session_context": ("feeling_unwell", "true"),
}


@pytest.mark.invariant
@pytest.mark.parametrize("table", CLINICAL_TABLES)
def test_clinical_tables_reject_update_and_delete(
    populated_clinical_tables, db, table: str
) -> None:
    """The database refuses a real UPDATE and a real DELETE on every clinical table.

    Parametrised over ``CLINICAL_TABLES`` so a table added to that list without a trigger fails
    here rather than silently becoming mutable. The fixture guarantees each table has a row, so
    the row-level trigger actually fires — asserting the trigger merely *exists* in
    ``pg_trigger`` would not prove it does anything.
    """
    row_count = db.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
    assert row_count > 0, (
        f"{table} is empty, so an UPDATE would not fire a row-level trigger and this test "
        f"would pass without proving anything"
    )

    column, value = _UPDATE_TARGET_COLUMN[table]

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(sa.text(f"UPDATE {table} SET {column} = {value}"))  # noqa: S608
    assert "append-only" in str(excinfo.value).lower(), f"{table} allowed an UPDATE"
    db.rollback()

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(sa.text(f"DELETE FROM {table}"))  # noqa: S608
    assert "append-only" in str(excinfo.value).lower(), f"{table} allowed a DELETE"
    db.rollback()

    # The row survived both attempts.
    assert db.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one() == row_count  # noqa: S608


@pytest.mark.invariant
def test_every_clinical_table_is_covered_by_the_update_delete_test() -> None:
    """A clinical table with no UPDATE target would be skipped silently above."""
    assert set(CLINICAL_TABLES) == set(_UPDATE_TARGET_COLUMN), (
        "CLINICAL_TABLES and _UPDATE_TARGET_COLUMN have drifted apart"
    )


@pytest.mark.invariant
def test_clinical_tables_match_the_migrations_trigger_list(db) -> None:
    """The application's idea of "clinical" and the migration's must agree.

    A table in the migration but not in ``CLINICAL_TABLES`` goes untested; a table in
    ``CLINICAL_TABLES`` but not in the migration has no trigger at all. Both have happened.
    """
    # Asks the database which triggers exist rather than parsing one migration file. Clinical
    # tables can arrive in later migrations — patient_context did, in 0008 — and a check pinned to
    # 0001 would have to be edited every time, which is exactly how the two lists drifted before.
    installed = {
        row[0]
        for row in db.execute(
            sa.text(
                "SELECT c.relname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal AND t.tgname LIKE 'trg\_%\_append\_only'"
            )
        ).all()
    }

    assert installed == set(CLINICAL_TABLES)


@pytest.mark.invariant
def test_every_clinical_table_actually_has_its_trigger_installed(db) -> None:
    """Belt and braces: the trigger exists in the live database, by name, for each table."""
    installed = {
        name
        for (name,) in db.execute(
            sa.text(
                "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal AND t.tgname LIKE 'trg\\_%\\_append\\_only'"
            )
        ).all()
    }
    assert installed == set(CLINICAL_TABLES), (
        f"missing trigger(s): {set(CLINICAL_TABLES) - installed}"
    )


@pytest.mark.invariant
def test_audit_log_is_append_only(db) -> None:
    """UPDATE and DELETE on audit_log raise, not merely 'are not exposed'."""
    row = AuditLog(actor="test-actor", role="patient", action="session_submitted", target="x")
    db.add(row)
    db.commit()

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(
            sa.text("UPDATE audit_log SET actor = 'someone-else' WHERE id = :id"),
            {"id": row.id},
        )
    assert "append-only" in str(excinfo.value).lower()
    db.rollback()

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(sa.text("DELETE FROM audit_log WHERE id = :id"), {"id": row.id})
    assert "append-only" in str(excinfo.value).lower()
    db.rollback()

    db.expire_all()
    assert db.get(AuditLog, row.id).actor == "test-actor"


@pytest.mark.invariant
def test_measurement_session_cannot_be_rewritten(
    client: TestClient, auth, db, episode, device_profile
) -> None:
    """A stored session is immutable once ingested."""
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    assert post_session(client, auth, payload).status_code == 201

    with pytest.raises(sa.exc.DatabaseError):
        db.execute(
            sa.text("UPDATE measurement_session SET status = 'rejected' WHERE id = :id"),
            {"id": uuid.UUID(payload["session_id"])},
        )
    db.rollback()


@pytest.mark.invariant
def test_correction_is_a_new_row_referencing_the_original(
    client: TestClient, auth, db, episode, cuff_reading
) -> None:
    """"Corrections are new rows referencing the original."" """
    taken_at = datetime.now(tz=timezone.utc) - timedelta(hours=2)

    response = client.post(
        "/v1/cuff-readings",
        headers=auth,
        json={
            "episode_id": str(episode.id),
            "systolic_mmhg": 148,
            "diastolic_mmhg": 92,
            "pulse_bpm": 71,
            "source": "manual_entry",
            "taken_at": taken_at.isoformat(),
            "user_confirmed_at": taken_at.isoformat(),
            "corrects_id": str(cuff_reading.id),
        },
    )
    assert response.status_code == 201, response.text
    correction = response.json()
    assert correction["corrects_id"] == str(cuff_reading.id)
    assert correction["id"] != str(cuff_reading.id)

    # The original is untouched and still present.
    db.expire_all()
    original = db.get(CuffReading, cuff_reading.id)
    assert original is not None
    assert original.systolic_mmhg == 152

    # Both rows appear on the timeline; nothing was replaced.
    timeline = client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).json()
    readings = [i for i in timeline["items"] if i["record_type"] == "cuff_reading"]
    assert len(readings) == 2


@pytest.mark.invariant
def test_clinician_summary_generation_appends_rather_than_updates(
    client: TestClient, clinician_auth, db, episode
) -> None:
    """Each generation is a new row, so the record shows what was seen and when."""
    for _ in range(3):
        assert (
            client.get(
                f"/v1/episodes/{episode.id}/summary", headers=clinician_auth
            ).status_code
            == 200
        )

    count = db.execute(
        sa.text("SELECT count(*) FROM clinician_summary WHERE episode_id = :id"),
        {"id": episode.id},
    ).scalar_one()
    assert count == 3

    viewed = db.execute(
        sa.text(
            "SELECT count(*) FROM clinician_summary WHERE episode_id = :id "
            "AND viewed_at IS NOT NULL"
        ),
        {"id": episode.id},
    ).scalar_one()
    assert viewed == 3, "a clinician fetching the summary is a view"
