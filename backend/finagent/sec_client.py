"""Bounded official SEC JSON transport with a process-shared 2 requests/s gate."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

import httpx

from .contracts import FinanceError

DEFAULT_USER_AGENT = "FinanceAgent/0.1"
CONTACT_WARNING = "SEC_USER_AGENT is using FinanceAgent/0.1; configure an honest application name and real contact email for SEC fair-access identification."
MAX_RESPONSE_BYTES = 40 * 1024 * 1024


def contact_configured(user_agent: str | None = None) -> bool:
    value = user_agent if user_agent is not None else os.getenv("SEC_USER_AGENT", "")
    match = re.search(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", value)
    if not match:
        return False
    domain = match.group(1).lower()
    return not (domain in {"example.com", "example.org", "example.net"}
                or domain.endswith((".invalid", ".test", ".example", ".localhost")))


def filing_url(cik: str, accession: str, primary_document: str) -> str:
    if not re.fullmatch(r"[0-9]{10}", cik) or int(cik) == 0:
        raise FinanceError("invalid_cik", "A configured ten-digit SEC CIK is required.")
    if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
        raise FinanceError("invalid_accession", "SEC filing accession is invalid.")
    # primaryDocument is a filename, never a URL, directory, encoded path or ixviewer link.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:htm|html)", primary_document, re.IGNORECASE) or ".." in primary_document:
        raise FinanceError("unsupported_document", "SEC primaryDocument must be a safe HTML/HTM filename; no alternate document was substituted.")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary_document}"


class RateGate:
    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    async def wait(self):
        while True:
            with self._lock:
                now = time.monotonic()
                delay = self._next - now
                if delay <= 0:
                    self._next = now + self.interval
                    return
            await asyncio.sleep(delay)


_RATE_GATE = RateGate()


@dataclass(frozen=True)
class SecPayload:
    data: dict
    url: str
    retrieved_at: datetime
    cached: bool = False


@dataclass(frozen=True)
class SecDocument:
    path: Path
    url: str
    sha256: str
    byte_size: int
    retrieved_at: datetime
    cached: bool = False
    media_type: str = "text/html"


class SECClient:
    def __init__(self, *, cache_dir: Path | None = None, filing_dir: Path | None = None,
                 transport=None, rate_gate=None, ttl: float = 21600):
        self.cache_dir = cache_dir or Path(os.environ.get("FIN_DATA_DIR", "/data/workspaces/.finance")) / "cache"
        self.filing_dir = filing_dir or Path(os.environ.get("FIN_DATA_DIR", "/data/workspaces/.finance")) / "filings"
        self.ttl = ttl
        self.rate_gate = rate_gate or _RATE_GATE
        self.user_agent = os.environ.get("SEC_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
        self.http = httpx.AsyncClient(
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=httpx.Timeout(25.0, connect=10.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            transport=transport, follow_redirects=False, trust_env=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def submissions(self, cik: str, *, refresh: bool = False) -> SecPayload:
        return await self._get(self._url(cik, False), refresh)

    async def companyfacts(self, cik: str, *, refresh: bool = False) -> SecPayload:
        return await self._get(self._url(cik, True), refresh)

    async def older_submissions(self, cik: str, name: str, *, refresh: bool = False) -> SecPayload:
        self._url(cik, False)
        if not re.fullmatch(r"CIK" + re.escape(cik) + r"-submissions-[0-9]{3,}\.json", name):
            raise FinanceError("invalid_submissions_path", "SEC older submissions filename is invalid for this company.")
        return await self._get(f"https://data.sec.gov/submissions/{name}", refresh)

    def require_contact(self) -> None:
        if not contact_configured(self.user_agent):
            raise FinanceError("sec_configuration_required", "Original filing retrieval requires SEC_USER_AGENT with an honest application name and real contact email; placeholder contacts are not accepted.")

    def _read_document_cache(self, key: str, url: str) -> SecDocument | None:
        try:
            index = json.loads((self.cache_dir / f"{key}.index").read_text(encoding="utf-8"))
            retrieved = datetime.fromisoformat(index["retrieved_at"])
            digest = index["sha256"]
            # A specific accession/primary-document URL is a pinned original,
            # not a changing JSON feed. Reuse verified local bytes until refresh.
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                return None
            path = (self.filing_dir / f"{digest}.html").resolve()
            if not path.is_relative_to(self.filing_dir.resolve()):
                return None
            size = path.stat().st_size
            if not 0 < size <= MAX_RESPONSE_BYTES or size != index["byte_size"]:
                return None
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return None
            media_type = index.get("media_type", "text/html")
            if media_type not in {"text/html", "application/xhtml+xml"}:
                return None
            return SecDocument(path, url, digest, size, retrieved, True, media_type)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_document(self, key: str, url: str, raw: bytes, retrieved: datetime, media_type: str) -> SecDocument:
        self.filing_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.filing_dir / f"{digest}.html"
        # A staged hard link publishes a fully written file without ever overwriting
        # a previous immutable version (including concurrent downloads).
        temporary = self.filing_dir / f".{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(raw)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.stat().st_size != len(raw) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise FinanceError("document_integrity", "The stored immutable filing failed its checksum; it was not overwritten.")
        finally:
            temporary.unlink(missing_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_dir / f"{key}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps({"sha256": digest, "byte_size": len(raw), "retrieved_at": retrieved.isoformat(), "media_type": media_type}), encoding="utf-8")
            os.replace(temporary, self.cache_dir / f"{key}.index")
        finally:
            temporary.unlink(missing_ok=True)
        return SecDocument(path.resolve(), url, digest, len(raw), retrieved, media_type=media_type)

    async def original_document(self, cik: str, accession: str, primary_document: str, *, refresh: bool = False) -> SecDocument:
        url = filing_url(cik, accession, primary_document)
        key = "document-" + hashlib.sha256(url.encode()).hexdigest()
        if not refresh:
            cached = await asyncio.to_thread(self._read_document_cache, key, url)
            if cached:
                return cached
        self.require_contact()
        raw, media_type = await self._request_bytes(url, accept="text/html, application/xhtml+xml")
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise FinanceError("unsupported_document", "SEC primary document did not return an HTML media type.")
        if not raw:
            raise FinanceError("sec_invalid_response", "SEC returned an empty primary filing document.")
        try:
            return await asyncio.to_thread(self._write_document, key, url, raw, datetime.now(timezone.utc), media_type)
        except OSError as exc:
            raise FinanceError("document_storage_unavailable", "The original filing could not be saved to persistent storage.") from exc

    @staticmethod
    def _url(cik: str, facts: bool) -> str:
        if not re.fullmatch(r"[0-9]{10}", cik):
            raise FinanceError("invalid_cik", "A configured ten-digit SEC CIK is required.")
        return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json" if facts else f"https://data.sec.gov/submissions/CIK{cik}.json"

    def _read_cache(self, key: str, url: str) -> SecPayload | None:
        try:
            index = json.loads((self.cache_dir / f"{key}.index").read_text(encoding="utf-8"))
            retrieved = datetime.fromisoformat(index["retrieved_at"])
            age = (datetime.now(timezone.utc) - retrieved).total_seconds()
            if not 0 <= age < self.ttl or not re.fullmatch(r"[0-9a-f]{64}", index["sha256"]):
                return None
            raw = (self.cache_dir / f"{index['sha256']}.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != index["sha256"]:
                return None
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return SecPayload(data, url, retrieved, True)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, key: str, raw: bytes, retrieved: datetime):
        # Hash-addressed immutable payloads preserve the bytes behind prior snapshots.
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        payload = self.cache_dir / f"{digest}.json"
        try:
            with payload.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            pass
        temporary = self.cache_dir / f"{key}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps({"sha256": digest, "retrieved_at": retrieved.isoformat()}), encoding="utf-8")
            os.replace(temporary, self.cache_dir / f"{key}.index")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After", "")
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = 2.0 ** attempt
        if delay > 30:
            raise FinanceError("sec_rate_limited", "SEC Retry-After exceeds the bounded retry budget; try again later.")
        return max(0.0, delay)

    async def _get(self, url: str, refresh: bool) -> SecPayload:
        key = hashlib.sha256(url.encode()).hexdigest()
        if not refresh:
            cached = await asyncio.to_thread(self._read_cache, key, url)
            if cached:
                return cached
        raw, _ = await self._request_bytes(url, accept="application/json")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
        except ValueError as exc:
            raise FinanceError("sec_invalid_response", "SEC did not return a JSON object.") from exc
        retrieved = datetime.now(timezone.utc)
        try:
            await asyncio.to_thread(self._write_cache, key, raw, retrieved)
        except OSError:
            # JSON cache failure does not invalidate official structured facts.
            pass
        return SecPayload(data, url, retrieved)

    async def _request_bytes(self, url: str, *, accept: str) -> tuple[bytes, str]:
        for attempt in range(3):
            await self.rate_gate.wait()
            try:
                async with self.http.stream("GET", url, headers={"Accept": accept, "Accept-Encoding": "identity"}) as response:
                    if response.status_code == 403:
                        raise FinanceError("sec_forbidden", "SEC returned 403. No bypass or document/search fallback was attempted. Check SEC_USER_AGENT contact information and try later.")
                    if response.status_code == 429 or response.status_code in {500, 502, 503, 504}:
                        if attempt == 2:
                            raise FinanceError("sec_unavailable", f"SEC returned HTTP {response.status_code} after bounded retries.")
                        delay = self._retry_delay(response, attempt)
                    elif response.status_code != 200:
                        raise FinanceError("sec_http_error", f"SEC returned HTTP {response.status_code}; no alternative source was substituted.")
                    else:
                        length = response.headers.get("Content-Length", "")
                        if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                            raise FinanceError("sec_response_too_large", "SEC response exceeds the 40 MiB byte limit.")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise FinanceError("sec_response_too_large", "SEC response exceeds the 40 MiB streaming byte limit.")
                            raw.extend(chunk)
                        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        return bytes(raw), media_type
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 2:
                    raise FinanceError("sec_unavailable", "SEC request timed out or failed after bounded retries.") from exc
                delay = 2.0 ** attempt
            await asyncio.sleep(delay)
        raise FinanceError("sec_unavailable", "SEC retry budget exhausted.")
