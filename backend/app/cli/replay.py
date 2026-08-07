"""``tera-replay`` — post a recorded or synthetic session through the real API.

BUILD_SPEC 4.6: "This is the demo fallback for when no phone is available or the venue network is
hostile — it must exercise the same code path as a real device, not a shortcut."

So it does exactly what a handset does, over HTTP, against a running server:

1. obtain an access token
2. request a single-use nonce from ``POST /v1/sessions/nonce``
3. submit the session with ``X-Session-Nonce`` and ``Idempotency-Key``

There is no back door into the database and no bypass of the nonce, the plausibility gate, the
rate limiter or the deviation engine. If the API would reject a real device's payload, it
rejects this one.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import typer

app = typer.Typer(add_completion=False, help="Replay a session file through the real API.")

DEFAULT_BASE_URL = "http://localhost:8000"
TIMEOUT_SECONDS = 30.0


@app.command()
def main(
    file: Path = typer.Argument(..., help="Session JSON to post."),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url", help="Running API root."),
    username: str = typer.Option("demo.patient@tera.invalid", "--username"),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True),
    episode_id: str | None = typer.Option(None, "--episode-id"),
    device_profile_id: str | None = typer.Option(None, "--device-profile-id"),
    reuse_session_id: bool = typer.Option(
        False,
        "--reuse-session-id",
        help="Post the file's session_id verbatim instead of minting a new one. Use this "
        "twice to demonstrate the 409 duplicate path.",
    ),
    keep_started_at: bool = typer.Option(
        False,
        "--keep-started-at",
        help="Use the file's started_at instead of the current time.",
    ),
) -> None:
    """Post one session as if it came from a device."""
    payload = _load(file)

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=TIMEOUT_SECONDS) as client:
        token = _login(client, username, password)
        headers = {"Authorization": f"Bearer {token}"}

        payload = _resolve_ids(client, headers, payload, episode_id, device_profile_id)

        if not reuse_session_id or not payload.get("session_id"):
            payload["session_id"] = str(uuid.uuid4())
        if not keep_started_at or not payload.get("started_at"):
            payload["started_at"] = datetime.now(tz=timezone.utc).isoformat()

        nonce = _request_nonce(client, headers)
        typer.echo(f"nonce obtained (expires {nonce['expires_at']})")

        response = client.post(
            "/v1/sessions",
            json=payload,
            headers={
                **headers,
                "X-Session-Nonce": nonce["nonce"],
                # BUILD_SPEC 4.2 — the idempotency key is the session id.
                "Idempotency-Key": payload["session_id"],
            },
        )

    typer.echo(f"POST /v1/sessions -> {response.status_code}")
    typer.echo(json.dumps(response.json(), indent=2))

    if response.status_code == 409:
        typer.secho(
            "409: this session_id was already submitted; the stored result was returned "
            "unchanged.",
            fg=typer.colors.YELLOW,
        )
    elif response.status_code not in (200, 201):
        raise typer.Exit(code=1)


def _load(file: Path) -> dict[str, Any]:
    """Read the session file, dropping any ``_``-prefixed annotation keys.

    The API forbids unknown fields, so a template cannot carry inline comments through to the
    request; stripping them here keeps the sample files self-documenting.
    """
    if not file.exists():
        typer.secho(f"no such file: {file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    data = json.loads(file.read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if not key.startswith("_")}


def _login(client: httpx.Client, username: str, password: str) -> str:
    response = client.post(
        "/v1/auth/token", data={"username": username, "password": password}
    )
    if response.status_code != 200:
        typer.secho(
            f"login failed ({response.status_code}): {response.text}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return response.json()["access_token"]


def _request_nonce(client: httpx.Client, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post("/v1/sessions/nonce", headers=headers)
    if response.status_code != 201:
        typer.secho(
            f"nonce request failed ({response.status_code}): {response.text}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return response.json()


def _resolve_ids(
    client: httpx.Client,
    headers: dict[str, str],
    payload: dict[str, Any],
    episode_id: str | None,
    device_profile_id: str | None,
) -> dict[str, Any]:
    """Fill in episode and device profile ids.

    Explicit flags win, then whatever the file carries, then discovery through the same public
    endpoints a client would use. Discovery only resolves an unambiguous case — one episode,
    one device profile — because guessing which patient's episode to post into is not a
    decision a demo tool should make quietly.
    """
    payload = dict(payload)

    if episode_id:
        payload["episode_id"] = episode_id
    if device_profile_id:
        payload["device_profile_id"] = device_profile_id

    if not payload.get("episode_id"):
        episodes = client.get("/v1/episodes", headers=headers).json().get("episodes", [])
        if len(episodes) != 1:
            typer.secho(
                f"cannot infer episode_id: {len(episodes)} episodes visible. Pass "
                f"--episode-id explicitly.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        payload["episode_id"] = episodes[0]["episode_id"]
        typer.echo(f"episode resolved: {payload['episode_id']}")

    if not payload.get("device_profile_id"):
        payload["device_profile_id"] = _discover_device_profile(
            client, headers, payload["episode_id"]
        )
        typer.echo(f"device profile resolved: {payload['device_profile_id']}")

    return payload


def _discover_device_profile(
    client: httpx.Client, headers: dict[str, str], episode_id: str
) -> str:
    """Find the device profile used by an existing session in this episode."""
    timeline = client.get(f"/v1/episodes/{episode_id}/timeline", headers=headers).json()
    for item in timeline.get("items", []):
        if item.get("record_type") in ("trend_estimate", "rejected_session"):
            detail = client.get(
                f"/v1/sessions/{item['session_id']}", headers=headers
            ).json()
            return detail["device_profile_id"]

    typer.secho(
        "cannot infer device_profile_id: this episode has no sessions yet. Pass "
        "--device-profile-id explicitly.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


def entrypoint() -> None:
    """Console-script entry point."""
    try:
        app()
    except httpx.ConnectError as exc:  # pragma: no cover - CLI surface
        typer.secho(f"cannot reach the API: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
