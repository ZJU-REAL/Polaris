"""Versioned journal and conference metrics for literature discovery."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.base import utcnow
from app.models.literature_discovery import LiteratureVenueMetricCache

METRIC_SNAPSHOT_VERSION = "venue-metrics-v1"
_ISSN_RE = re.compile(r"\b\d{4}-[\dXx]{4}\b")
_UNKNOWN_VENUES = {
    "unknown",
    "unknown journal",
    "unknown conference",
    "unknown venue",
    "n/a",
    "na",
    "none",
    "未知",
    "未知期刊",
    "未知会议",
}


class VenueMetricProviderError(RuntimeError):
    """A stable provider error that never contains a request URL or credential."""

    def __init__(self, provider: str, code: str) -> None:
        super().__init__(f"{provider} venue metric lookup failed ({code})")
        self.provider = provider
        self.code = code


@dataclass(frozen=True, slots=True)
class VenueIdentity:
    key: str
    name: str
    issn_l: str | None
    issns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VenueMetricResult:
    provider: str
    venue_name: str | None
    issn_l: str | None
    metrics: dict[str, Any]


class VenueMetricProvider(Protocol):
    name: str

    async def lookup(self, identity: VenueIdentity) -> VenueMetricResult | None: ...


def normalize_venue(value: Any) -> str | None:
    text = " ".join(str(value or "").split()).strip(" .")
    return None if not text or text.casefold() in _UNKNOWN_VENUES else text[:512]


def _canonical_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", str(value or "").casefold())


def _issns(value: Any) -> list[str]:
    output: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, Mapping):
            for nested in item.values():
                collect(nested)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                collect(nested)
            return
        for match in _ISSN_RE.finditer(str(item or "")):
            normalized = match.group(0).upper()
            if normalized not in output:
                output.append(normalized)

    collect(value)
    return output


def venue_identity(candidate: Mapping[str, Any]) -> VenueIdentity | None:
    venue = normalize_venue(candidate.get("venue"))
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    issn_l = next(
        iter(
            _issns(
                metadata.get("issn_l") or metadata.get("issnL")
            )
        ),
        None,
    )
    issns = _issns(
        [
            issn_l,
            *(
                metadata.get("issns")
                if isinstance(metadata.get("issns"), list)
                else [
                    metadata.get("issn"),
                    metadata.get("ISSN"),
                    metadata.get("issn-type"),
                ]
            ),
        ]
    )
    if not venue and not issns:
        return None
    key = f"issn:{issn_l or issns[0]}" if issns else f"name:{_canonical_name(venue)}"
    return VenueIdentity(key=key, name=venue or issns[0], issn_l=issn_l, issns=tuple(issns))


def _provider_error_code(error: Exception) -> str:
    if isinstance(error, VenueMetricProviderError):
        return error.code
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP_{error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "TIMEOUT"
    if isinstance(error, httpx.RequestError):
        return "NETWORK_ERROR"
    if isinstance(error, ValueError):
        return "INVALID_RESPONSE"
    return type(error).__name__.upper()


class _KeyRotator:
    def __init__(self, keys: Sequence[str]) -> None:
        self._keys = tuple(dict.fromkeys(key.strip() for key in keys if key.strip()))
        self._index = 0
        self._lock = asyncio.Lock()

    async def next(self) -> str | None:
        if not self._keys:
            return None
        async with self._lock:
            key = self._keys[self._index % len(self._keys)]
            self._index += 1
            return key


class OpenAlexVenueMetricProvider:
    name = "openalex"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        keys: Sequence[str] = (),
        mailto: str = "",
    ) -> None:
        self._client = client
        self._keys = _KeyRotator(keys)
        self._mailto = mailto.strip()

    async def _get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        if self._mailto:
            query["mailto"] = self._mailto
        if key := await self._keys.next():
            query["api_key"] = key
        try:
            response = await self._client.get(url, params=query)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise VenueMetricProviderError(self.name, _provider_error_code(exc)) from exc

    @staticmethod
    def _result(source: Mapping[str, Any]) -> VenueMetricResult | None:
        source_type = str(source.get("type") or "").casefold()
        if source_type and source_type not in {"journal", "conference", "proceedings"}:
            return None
        stats = source.get("summary_stats")
        stats = stats if isinstance(stats, Mapping) else {}
        metrics = {
            "two_year_mean_citedness": _number(stats.get("2yr_mean_citedness")),
            "h_index": _integer(stats.get("h_index")),
            "i10_index": _integer(stats.get("i10_index")),
            "works_count": _integer(source.get("works_count")),
            "cited_by_count": _integer(source.get("cited_by_count")),
            "is_oa": bool(source.get("is_oa")),
            "is_in_doaj": bool(source.get("is_in_doaj")),
            "openalex_source_id": source.get("id"),
        }
        metrics = {key: value for key, value in metrics.items() if value is not None}
        return VenueMetricResult(
            provider="openalex",
            venue_name=normalize_venue(source.get("display_name")),
            issn_l=next(iter(_issns(source.get("issn_l"))), None),
            metrics=metrics,
        )

    async def lookup(self, identity: VenueIdentity) -> VenueMetricResult | None:
        for issn in identity.issns:
            source = await self._get(f"https://api.openalex.org/sources/issn:{issn}")
            if isinstance(source, Mapping) and (result := self._result(source)):
                return result
        payload = await self._get(
            "https://api.openalex.org/sources",
            params={"search": identity.name, "filter": "type:journal", "per_page": 5},
        )
        expected = _canonical_name(identity.name)
        for source in payload.get("results") or [] if isinstance(payload, Mapping) else []:
            if not isinstance(source, Mapping):
                continue
            aliases = [source.get("display_name"), *(source.get("alternate_titles") or [])]
            if expected and expected in {_canonical_name(alias) for alias in aliases}:
                return self._result(source)
        return None


class EasyScholarVenueMetricProvider:
    name = "easyscholar"

    def __init__(self, client: httpx.AsyncClient, *, keys: Sequence[str], base_url: str) -> None:
        self._client = client
        self._keys = _KeyRotator(keys)
        self._base_url = base_url

    async def lookup(self, identity: VenueIdentity) -> VenueMetricResult | None:
        key = await self._keys.next()
        if not key:
            return None
        try:
            response = await self._client.get(
                self._base_url,
                params={"secretKey": key, "publicationName": identity.name},
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise VenueMetricProviderError(self.name, _provider_error_code(exc)) from exc
        if not isinstance(payload, Mapping):
            raise VenueMetricProviderError(self.name, "INVALID_RESPONSE")
        code = _integer(payload.get("code"))
        if code != 200:
            raise VenueMetricProviderError(self.name, f"API_{code or 'ERROR'}")
        data = payload.get("data")
        data = data if isinstance(data, Mapping) else {}
        official_group = data.get("officialRank")
        official_group = official_group if isinstance(official_group, Mapping) else {}
        official = official_group.get("all")
        official = official if isinstance(official, Mapping) else {}
        metrics = {
            "impact_factor": _number(official.get("sciif")),
            "five_year_impact_factor": _number(official.get("sciif5")),
            "jcr_quartile": _quartile(official.get("sci") or official.get("ssci")),
            "jci": _number(official.get("jci")),
            "cas_base_zone": _zone(official.get("sciBase")),
            "cas_upgraded_zone": _zone(official.get("sciUp")),
        }
        metrics = {name: value for name, value in metrics.items() if value is not None}
        if not metrics:
            return None
        return VenueMetricResult(
            provider="easyscholar",
            venue_name=identity.name,
            issn_l=identity.issn_l,
            metrics=metrics,
        )


def _number(value: Any) -> float | None:
    try:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        return float(match.group(0)) if match else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _quartile(value: Any) -> str | None:
    match = re.search(r"(?<![A-Z0-9])Q\s*([1-4])(?!\d)", str(value or ""), re.I)
    return f"Q{match.group(1)}" if match else None


def _zone(value: Any) -> int | None:
    match = re.search(r"(?<!\d)([1-4])\s*(?:区|zone)?(?!\d)", str(value or ""), re.I)
    return int(match.group(1)) if match else None


def metric_impact_score(metrics: Mapping[str, Any]) -> float | None:
    values: list[float] = []
    if (impact_factor := _number(metrics.get("impact_factor"))) is not None:
        values.append(1 - math.exp(-max(0.0, impact_factor) / 8))
    if (citedness := _number(metrics.get("two_year_mean_citedness"))) is not None:
        values.append(1 - math.exp(-max(0.0, citedness) / 8))
    if (h_index := _number(metrics.get("h_index"))) is not None:
        values.append(min(1.0, math.log1p(max(0.0, h_index)) / math.log(201)))
    quartile = str(metrics.get("jcr_quartile") or "").upper()
    if quartile in {"Q1", "Q2", "Q3", "Q4"}:
        values.append({"Q1": 1.0, "Q2": 0.75, "Q3": 0.5, "Q4": 0.25}[quartile])
    return round(sum(values) / len(values), 6) if values else None


class VenueMetricService:
    def __init__(
        self,
        providers: Sequence[VenueMetricProvider],
        *,
        ttl_days: int = 30,
        concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.providers = tuple(providers)
        self.ttl = timedelta(days=max(1, ttl_days))
        self.concurrency = max(1, concurrency)
        self._client = client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def enrich_candidates(
        self, session: AsyncSession, candidates: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        output = [dict(candidate) for candidate in candidates]
        identities: dict[str, VenueIdentity] = {}
        for candidate in output:
            candidate["venue"] = normalize_venue(candidate.get("venue"))
            if identity := venue_identity(candidate):
                identities.setdefault(identity.key, identity)
        if not identities or not self.providers:
            return output

        now = datetime.now(UTC)
        cache_rows = list(
            (
                await session.execute(
                    select(LiteratureVenueMetricCache).where(
                        LiteratureVenueMetricCache.provider.in_(
                            [provider.name for provider in self.providers]
                        ),
                        LiteratureVenueMetricCache.identity_key.in_(identities),
                    )
                )
            )
            .scalars()
            .all()
        )
        cached = {
            (row.provider, row.identity_key): row
            for row in cache_rows
            if _aware(row.expires_at) > now
        }
        semaphore = asyncio.Semaphore(self.concurrency)

        async def resolve(provider: VenueMetricProvider, identity: VenueIdentity):
            row = cached.get((provider.name, identity.key))
            if row is not None:
                result = (
                    VenueMetricResult(provider.name, row.venue_name, row.issn_l, dict(row.metrics))
                    if row.status == "resolved" and isinstance(row.metrics, Mapping)
                    else None
                )
                return provider.name, identity, result, None, _aware(row.fetched_at), _aware(
                    row.expires_at
                )
            async with semaphore:
                try:
                    result = await provider.lookup(identity)
                    return provider.name, identity, result, None, now, now + self.ttl
                except Exception as exc:  # provider isolation is intentional
                    return provider.name, identity, None, _provider_error_code(exc), now, now

        resolutions = await asyncio.gather(
            *(
                resolve(provider, identity)
                for identity in identities.values()
                for provider in self.providers
            )
        )
        by_identity: dict[str, list[tuple[Any, ...]]] = {}
        for row in resolutions:
            by_identity.setdefault(row[1].key, []).append(row)
            provider, identity, result, error, fetched_at, expires_at = row
            if error is None and (provider, identity.key) not in cached:
                await _upsert_cache(
                    session,
                    provider=provider,
                    identity=identity,
                    result=result,
                    fetched_at=fetched_at,
                    expires_at=expires_at,
                )
        await session.flush()

        for candidate in output:
            identity = venue_identity(candidate)
            if identity is None:
                continue
            metrics: dict[str, Any] = {}
            providers: list[dict[str, Any]] = []
            errors: dict[str, str] = {}
            venue_name = candidate.get("venue")
            issn_l = identity.issn_l
            for provider, _, result, error, fetched_at, expires_at in by_identity[identity.key]:
                if error:
                    errors[provider] = error
                    continue
                providers.append(
                    {
                        "provider": provider,
                        "status": "resolved" if result else "not_found",
                        "fetched_at": fetched_at.isoformat(),
                        "expires_at": expires_at.isoformat(),
                    }
                )
                if result:
                    metrics.update(result.metrics)
                    venue_name = result.venue_name or venue_name
                    issn_l = result.issn_l or issn_l
            impact_score = metric_impact_score(metrics)
            snapshot = {
                "version": METRIC_SNAPSHOT_VERSION,
                "identity": identity.key,
                "venue_name": venue_name,
                "issn_l": issn_l,
                "providers": providers,
                "metrics": metrics or None,
                "provider_errors": errors or None,
                "impact_score": impact_score,
            }
            candidate["venue_metric_snapshot"] = snapshot
            if impact_score is not None:
                candidate["impact_score"] = impact_score
        return output


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _upsert_cache(
    session: AsyncSession,
    *,
    provider: str,
    identity: VenueIdentity,
    result: VenueMetricResult | None,
    fetched_at: datetime,
    expires_at: datetime,
) -> None:
    values = {
        "provider": provider,
        "identity_key": identity.key,
        "status": "resolved" if result else "not_found",
        "venue_name": result.venue_name if result else identity.name,
        "issn_l": result.issn_l if result else identity.issn_l,
        "metrics": result.metrics if result else None,
        "fetched_at": fetched_at,
        "expires_at": expires_at,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    table = LiteratureVenueMetricCache.__table__
    immutable = {"provider", "identity_key", "created_at"}
    update_values = {key: value for key, value in values.items() if key not in immutable}
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(table).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.provider, table.c.identity_key], set_=update_values
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(table).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.provider, table.c.identity_key], set_=update_values
        )
    else:
        existing = await session.get(LiteratureVenueMetricCache, (provider, identity.key))
        if existing is None:
            session.add(LiteratureVenueMetricCache(**values))
        else:
            for key, value in update_values.items():
                setattr(existing, key, value)
        return
    await session.execute(statement)


def _pool(settings: Mapping[str, Any], source: str, fallback: str = "") -> list[str]:
    pools = settings.get("provider_keys")
    declared = isinstance(pools, Mapping) and source in pools
    values = pools.get(source) if declared else None
    if declared:
        return [str(value).strip() for value in values or [] if str(value).strip()]
    return [value.strip() for value in re.split(r"[,;\r\n]+", fallback) if value.strip()]


def build_venue_metric_service(runtime_settings: Mapping[str, Any]) -> VenueMetricService:
    settings = get_settings()
    client = httpx.AsyncClient(
        proxy=settings.outbound_proxy or None,
        timeout=settings.literature_source_timeout_seconds,
    )
    providers: list[VenueMetricProvider] = [
        OpenAlexVenueMetricProvider(
            client,
            keys=_pool(runtime_settings, "openalex"),
            mailto=settings.openalex_mailto,
        )
    ]
    easyscholar_keys = _pool(
        runtime_settings, "easyscholar", settings.easyscholar_secret_keys
    )
    if easyscholar_keys:
        providers.append(
            EasyScholarVenueMetricProvider(
                client,
                keys=easyscholar_keys,
                base_url=settings.easyscholar_base_url,
            )
        )
    return VenueMetricService(
        providers,
        ttl_days=settings.venue_metrics_cache_ttl_days,
        concurrency=settings.venue_metrics_concurrency,
        client=client,
    )


async def probe_venue_metric_provider(
    runtime_settings: Mapping[str, Any], *, source: str, venue_name: str
) -> bool:
    """Probe one configured metric provider without exposing its credential."""

    service = build_venue_metric_service(runtime_settings)
    identity = venue_identity({"venue": venue_name})
    provider = next((item for item in service.providers if item.name == source), None)
    try:
        if provider is None or identity is None:
            raise VenueMetricProviderError(source, "NOT_CONFIGURED")
        return await provider.lookup(identity) is not None
    finally:
        await service.aclose()
