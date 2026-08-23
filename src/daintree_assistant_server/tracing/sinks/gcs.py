"""Google Cloud Storage sink, written over the existing httpx client.

**No new dependency.** ``google-cloud-storage`` would pull a large transitive tree
into a runtime image for what is, at this size, four HTTP calls against a
documented JSON API. ``httpx`` is already a pinned runtime dependency and already
the way this service talks to every other host, so the sink is written the same
way the provider adapters are.

**The live path is UNVERIFIED.** Nothing here has run against a real bucket: there
is no bucket, and CI has no credentials. What IS pinned offline is the part that
is ours to get wrong — object paths, content types, the auth header, and above all
that the completion marker is written LAST (``tests/unit/test_tracing_gcs.py``).
What Cloud Storage does with these requests is not pinned, and
``TODO.md`` carries that the same way it carries the Together contract gap. Treat
a first deployment as a smoke test, not a rollout.

**Write-only by design.** The sink never reads, lists or deletes. The Cloud Run
runtime identity should hold ``storage.objects.create`` and nothing else, so a
compromised serving instance cannot enumerate or destroy the corpus it writes to;
reading is a separate offline export identity. That is an IAM decision this code
cannot enforce, only avoid contradicting — see ``infra/gcp/bootstrap.sh``.
"""

from __future__ import annotations

import gzip
import time
from typing import Any
from urllib.parse import quote

import httpx

from ...observability import get_logger
from ..canonical import canonical_bytes

logger = get_logger(__name__)

#: The metadata server, which is how a Cloud Run instance gets a token without
#: any credential existing in the image or the environment.
_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)

#: Refresh this far before the stated expiry; a token that dies mid-flight
#: fails the request it was attached to.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0
#: Used only when the metadata server omits `expires_in`.
_ASSUMED_TOKEN_LIFETIME_SECONDS = 300.0
#: Floor, so a very short stated lifetime still caches briefly rather than making
#: the token endpoint part of every upload.
_MIN_TOKEN_CACHE_SECONDS = 30.0

COMPLETION_MARKER = "complete.json"

_EXTENSIONS = {
    "application/json": ".json.gz",
    "text/plain": ".txt.gz",
    "application/octet-stream": ".bin",
}


class GcsTraceSink:
    """Uploads a trace's objects under ``<prefix>/<trace_id>/``."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        client: httpx.AsyncClient,
        access_token: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client
        self._static_token = access_token
        self._cached_token: str | None = None
        self._token_expires_at = 0.0

    async def _token(self, *, force_refresh: bool = False) -> str | None:
        """A metadata-server token, refreshed before it expires.

        Caching one forever is the obvious shortcut and the wrong one: these
        expire in about an hour, so a long-lived Cloud Run instance would upload
        happily and then fail EVERY write for the rest of its life. The expiry is
        honoured with a safety margin, and `force_refresh` covers the case where
        the server's clock and ours disagree.
        """
        if self._static_token:
            return self._static_token
        now = time.monotonic()
        if self._cached_token and not force_refresh and now < self._token_expires_at:
            return self._cached_token
        try:
            response = await self._client.get(
                _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5.0
            )
            response.raise_for_status()
            payload = response.json()
            token = str(payload.get("access_token") or "")
            # Refresh a minute early rather than at the boundary; a token that
            # expires mid-flight fails the request it was attached to.
            #
            # A MISSING `expires_in` falls back to a short conservative life, not
            # to zero: zero means "already stale", which would refetch on every
            # single upload and turn the metadata server into a hot dependency of
            # the write path. Short-but-nonzero degrades to more refreshes, which
            # is the safe direction.
            lifetime = float(payload.get("expires_in") or _ASSUMED_TOKEN_LIFETIME_SECONDS)
            self._token_expires_at = now + max(
                _MIN_TOKEN_CACHE_SECONDS, lifetime - _TOKEN_REFRESH_MARGIN_SECONDS
            )
        except Exception:
            # Content-free, and non-fatal: an unauthenticated sink simply stores
            # nothing, which is the same failure mode as a storage outage and is
            # handled identically by the scope above.
            logger.warning("trace_write_failed", reason="token_unavailable")
            return None
        self._cached_token = token
        return token

    def _object_name(self, trace_id: str, leaf: str) -> str:
        parts = [p for p in (self._prefix, trace_id, leaf) if p]
        return "/".join(parts)

    async def _upload(
        self, trace_id: str, leaf: str, payload: bytes, content_type: str
    ) -> None:
        name = self._object_name(trace_id, leaf)
        for attempt in (0, 1):
            token = await self._token(force_refresh=attempt == 1)
            if token is None:
                return
            response = await self._client.post(
                f"https://storage.googleapis.com/upload/storage/v1/b/{quote(self._bucket)}/o",
                params={"uploadType": "media", "name": name},
                content=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                },
            )
            # Exactly one retry, and only for a credential the server rejected.
            # A 401 against a token we believed was live means our expiry estimate
            # was wrong; anything else is a real failure and retrying it would
            # just spend the flush budget twice.
            if response.status_code == 401 and attempt == 0:
                continue
            # Raise so the scope's guard counts it as a write failure; it must
            # never propagate past that, and the scope guarantees it does not.
            response.raise_for_status()
            return

    async def open(self, trace_id: str, header: dict[str, Any]) -> None:
        await self._upload(trace_id, "header.json", canonical_bytes(header), "application/json")

    async def put_blob(self, trace_id: str, digest: str, raw: bytes, media_type: str) -> None:
        suffix = _EXTENSIONS.get(media_type, ".bin")
        payload = raw if suffix.endswith(".bin") else gzip.compress(raw)
        await self._upload(
            trace_id,
            f"blobs/{digest}{suffix}",
            payload,
            "application/octet-stream" if suffix.endswith(".bin") else "application/gzip",
        )

    async def put_events(self, trace_id: str, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        body = b"\n".join(canonical_bytes(event) for event in events) + b"\n"
        await self._upload(
            trace_id, "events.jsonl.gz", gzip.compress(body), "application/gzip"
        )

    async def finish(self, trace_id: str, manifest: dict[str, Any]) -> None:
        # Manifest FIRST, marker SECOND, and never concurrently. The marker's whole
        # meaning is "everything the manifest names is stored"; uploading them in
        # parallel would let the marker land first after a crash, and an exporter
        # would then export a trace whose blobs are missing.
        await self._upload(
            trace_id, "manifest.json", canonical_bytes(manifest), "application/json"
        )
        await self._upload(
            trace_id,
            COMPLETION_MARKER,
            canonical_bytes({"trace_id": trace_id, "blobs": len(manifest.get("blobs", []))}),
            "application/json",
        )

    async def aclose(self) -> None:
        # The client is owned by the container, which closes it in its own
        # teardown order. Closing it here would tear down a shared client while
        # another component still holds it.
        return None
