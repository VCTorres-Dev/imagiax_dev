"""
app.ai.orthanc_client — Orthanc / PACS HTTP helpers.

Extracted from servidor_api_orthanc_v1.py (God Object refactor, Phase 5).

All functions accept an ``httpx.AsyncClient`` as their first argument so
they can be used with the shared client managed by the lifespan
(``request.app.state.orthanc``).

The DICOMweb proxy helper (``build_dicomweb_proxy_request``) is also here.

Usage:
    from app.ai.orthanc_client import (
        orthanc_get,
        orthanc_post,
        orthanc_put,
        resolve_orthanc_instance_id,
        find_instances_by_query,
        find_study_id_by_uid,
        save_study_result_metadata,
        get_study_result_metadata,
        get_instance_dicom_bytes,
        get_instance_tags,
    )
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger("imagiax.orthanc")

# ── Metadata key used to persist IMAGIAX analysis results in Orthanc ─────────
IMAGIAX_LAST_RESULT_KEY = "2048"

# ── DICOMweb path whitelist regex  (SEC-BE-003 / AD-3) ───────────────────────
_DICOMWEB_PATH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:-]*$")


# ============================================================
# Low-level HTTP helpers
# ============================================================


async def orthanc_get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    """GET request to Orthanc using the shared client."""
    return await client.get(path)


async def orthanc_post(
    client: httpx.AsyncClient,
    path: str,
    json_body: dict[str, Any],
) -> httpx.Response:
    """POST request to Orthanc using the shared client."""
    return await client.post(path, json=json_body)


async def orthanc_put(
    client: httpx.AsyncClient,
    path: str,
    text_body: str,
) -> httpx.Response:
    """PUT request to Orthanc using the shared client."""
    return await client.put(
        path,
        content=text_body.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )


# ============================================================
# Query helpers
# ============================================================


async def resolve_orthanc_instance_id(
    client: httpx.AsyncClient,
    sop_instance_uid: str,
) -> str:
    """Resolve a SOPInstanceUID to an Orthanc internal instance ID."""
    payload: dict[str, Any] = {
        "Level": "Instance",
        "Query": {"SOPInstanceUID": sop_instance_uid},
        "Expand": False,
    }
    response = await orthanc_post(client, "/tools/find", payload)
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Orthanc /tools/find failed ({response.status_code}): {response.text}"
            ),
        )
    ids: list[str] = response.json()
    if not ids:
        raise HTTPException(
            status_code=404,
            detail="No instance found for the given SOPInstanceUID",
        )
    return ids[0]


async def find_instances_by_query(
    client: httpx.AsyncClient,
    query: dict[str, str],
) -> list[str]:
    """Return a list of Orthanc instance IDs matching ``query``."""
    payload: dict[str, Any] = {
        "Level": "Instance",
        "Query": query,
        "Expand": False,
    }
    response = await orthanc_post(client, "/tools/find", payload)
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Orthanc /tools/find failed ({response.status_code}): {response.text}"
            ),
        )
    ids: list[str] = response.json()
    if not ids:
        raise HTTPException(
            status_code=404,
            detail="No instances found for the given query",
        )
    return ids


async def find_study_id_by_uid(
    client: httpx.AsyncClient,
    study_instance_uid: str,
) -> str:
    """Return the Orthanc internal study ID for a StudyInstanceUID."""
    payload: dict[str, Any] = {
        "Level": "Study",
        "Query": {"StudyInstanceUID": study_instance_uid},
        "Expand": False,
    }
    response = await orthanc_post(client, "/tools/find", payload)
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Orthanc /tools/find (study) failed ({response.status_code}): "
                f"{response.text}"
            ),
        )
    ids: list[str] = response.json()
    if not ids:
        raise HTTPException(
            status_code=404,
            detail="No study found for the given StudyInstanceUID",
        )
    return ids[0]


# ============================================================
# Metadata persistence
# ============================================================


async def save_study_result_metadata(
    client: httpx.AsyncClient,
    study_instance_uid: str,
    payload: dict[str, Any],
) -> None:
    """Persist an IMAGIAX analysis result as Orthanc study metadata."""
    study_id = await find_study_id_by_uid(client, study_instance_uid)
    response = await orthanc_put(
        client,
        f"/studies/{study_id}/metadata/{IMAGIAX_LAST_RESULT_KEY}",
        json.dumps(payload, ensure_ascii=False),
    )
    if response.status_code not in (200, 204):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not persist metadata in Orthanc "
                f"({response.status_code}): {response.text}"
            ),
        )


async def get_study_result_metadata(
    client: httpx.AsyncClient,
    study_instance_uid: str,
) -> dict[str, Any] | None:
    """Retrieve the IMAGIAX analysis result stored in Orthanc study metadata.

    Returns ``None`` if no result is stored yet (404 from Orthanc).
    """
    study_id = await find_study_id_by_uid(client, study_instance_uid)
    response = await orthanc_get(
        client,
        f"/studies/{study_id}/metadata/{IMAGIAX_LAST_RESULT_KEY}",
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not read metadata from Orthanc "
                f"({response.status_code}): {response.text}"
            ),
        )
    try:
        return json.loads(response.text)
    except json.JSONDecodeError as error:
        logger.exception(
            "Corrupt IMAGIAX metadata in Orthanc for UID %s",
            study_instance_uid,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error. Please try again.",
        ) from error


# ============================================================
# Instance data retrieval
# ============================================================


async def get_instance_dicom_bytes(
    client: httpx.AsyncClient,
    orthanc_instance_id: str,
) -> bytes:
    """Download raw DICOM bytes for an instance."""
    response = await orthanc_get(
        client,
        f"/instances/{orthanc_instance_id}/file",
    )
    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Instance not found in Orthanc",
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not download DICOM "
                f"({response.status_code}): {response.text}"
            ),
        )
    return response.content


async def get_instance_tags(
    client: httpx.AsyncClient,
    orthanc_instance_id: str,
) -> dict[str, Any]:
    """Return simplified DICOM tags for an instance (best-effort, empty dict on failure)."""
    response = await orthanc_get(
        client,
        f"/instances/{orthanc_instance_id}/simplified-tags",
    )
    if response.status_code != 200:
        return {}
    return response.json()


# ============================================================
# DICOMweb proxy helper
# ============================================================


def sanitize_dicomweb_path(path: str) -> str:
    """Validate a DICOMweb proxy path against a strict whitelist regex.

    Raises HTTPException(400) on path traversal attempts, absolute paths,
    or any character outside the whitelist.

    Valid examples: ``studies/X/series/Y/instances/Z``
    """
    if ".." in path or path.startswith("/") or not _DICOMWEB_PATH_RE.match(path):
        raise HTTPException(status_code=400, detail="Invalid DICOMweb path")
    return path


# ============================================================
# Client factory
# ============================================================


def build_orthanc_client() -> httpx.AsyncClient:
    """Create and return a new shared ``httpx.AsyncClient`` for Orthanc.

    Called once from the app lifespan; the resulting client should be
    stored in ``app.state.orthanc`` and closed on shutdown.

    Credentials are read from ``settings`` (ORTHANC_USER / ORTHANC_PASSWORD).
    """
    headers: dict[str, str] = {}
    _user = settings.ORTHANC_USER
    _pass = settings.ORTHANC_PASSWORD.get_secret_value()
    if _user and _pass:
        import base64
        _cred = base64.b64encode(f"{_user}:{_pass}".encode()).decode()
        headers["Authorization"] = f"Basic {_cred}"

    return httpx.AsyncClient(
        base_url=settings.ORTHANC_BASE_URL,
        timeout=settings.IMAGIAX_HTTP_TIMEOUT,
        headers=headers,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )


def build_pacs_client() -> httpx.AsyncClient:
    """Create and return a new shared ``httpx.AsyncClient`` for DICOMweb proxy.

    Uses the pre-computed ``ORTHANC_AUTH_HEADER`` env var (Basic <base64>).
    Stored in ``app.state.pacs`` and closed on shutdown.
    """
    _auth_header = settings.ORTHANC_AUTH_HEADER
    pacs_headers: dict[str, str] = {}
    if _auth_header:
        pacs_headers["Authorization"] = f"Basic {_auth_header}"

    return httpx.AsyncClient(
        base_url=settings.ORTHANC_BASE_URL,
        timeout=httpx.Timeout(60.0),
        headers=pacs_headers,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )
