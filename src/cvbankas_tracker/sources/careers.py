from __future__ import annotations

import html
import http.client
import http.cookiejar
import ipaddress
import json
import os
import re
import socket
import string
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ..models import Vacancy
from .base import CollectionCancelledError
from .career_budget import (
    DEFAULT_MAX_JOBS,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_TOTAL_BYTES,
    CareerBudgetExceeded,
    RunBudget,
)
from .career_feeds import SUPPORTED_EXTRA_ATS, collect_feed

SUPPORTED_ATS_TYPES = {"ashby", "greenhouse", "lever", "html", "workday", "elastic_custom", "astro_server_island", "seb"} | SUPPORTED_EXTRA_ATS
TOKEN_OPTIONAL_PROVIDERS = {"html", "astro_server_island"}
DEFAULT_LEVER_PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
ALLOWED_API_HOSTS = {
    "api.ashbyhq.com",
    "boards-api.greenhouse.io",
    "api.lever.co",
    "api.eu.lever.co",
}


class _BudgetedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, budget: RunBudget | None = None):
        self.budget = budget

    def http_error_302(self, req, fp, code, msg, headers):
        """Keep urllib's method/loop rules, close redirect bodies without draining them."""
        try:
            location = headers.get("location") or headers.get("uri")
            if not location:
                return None
            target = urljoin(req.full_url, quote(location, encoding="iso-8859-1", safe=string.punctuation))
            redirected = self.redirect_request(req, fp, code, msg, headers, target)
            if redirected is None:
                return None
            visited = getattr(req, "redirect_dict", {})
            if visited.get(target, 0) >= self.max_repeats or len(visited) >= self.max_redirections:
                raise HTTPError(req.full_url, code, "Career redirect limit exceeded.", headers, fp)
            visited[target] = visited.get(target, 0) + 1
            redirected.redirect_dict = visited
            req.redirect_dict = visited
            if self.budget is not None:
                self.budget.before_request()
        finally:
            fp.close()
        return self.parent.open(redirected, timeout=req.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class _AllowedHostRedirectHandler(_BudgetedRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.netloc not in ALLOWED_API_HOSTS:
            raise HTTPError(req.full_url, code, f"redirect to unsupported host: {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Career page must be a public HTTP or HTTPS URL without credentials.")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Career page must use a standard HTTP port.")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Career page resolves to a non-public network address.")


class _PublicRedirectHandler(_BudgetedRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Resolve once and connect to the checked numeric address, preserving TLS host validation."""
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Career connection resolves to a non-public network address.")
    last_error = None
    for item in addresses:
        try:
            return socket.create_connection((item[4][0], port), timeout, source_address)
        except OSError as error:
            last_error = error
    raise last_error or OSError("No public career server address was reachable.")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


def _read_response(response, budget: RunBudget, max_response_bytes: int) -> bytes:
    """Check cumulative bytes and elapsed time while a response is streaming."""
    reader = getattr(response, "read1", None)
    if not callable(reader):
        content = response.read(budget.read_limit(max_response_bytes) + 1)
        budget.consume_bytes(len(content))
        if len(content) > max_response_bytes:
            raise ValueError("Career response exceeds the configured byte limit.")
        return content
    chunks = []
    size = 0
    while True:
        budget.check()
        remaining = max_response_bytes - size
        byte_remaining = budget.max_total_bytes - budget.total_bytes
        read_size = min(64 * 1024, budget.read_limit(max(1, remaining)) + 1) if byte_remaining else 1
        chunk = reader(read_size)
        budget.consume_bytes(len(chunk))
        size += len(chunk)
        if size > max_response_bytes:
            raise ValueError("Career response exceeds the configured byte limit.")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


@dataclass(frozen=True, slots=True)
class _CareerJob:
    company_id: str
    company_name: str
    provider: str
    source_id: str
    source_url: str
    title: str
    location: str
    salary_text: str
    description: str
    requirements: tuple[str, ...]
    raw: dict


class CareerPagesSource:
    name = "careers"
    uses_search_keywords = False
    supports_newest_first_stop = False
    requires_complete_processing = True

    def __init__(
        self,
        *,
        db_path: str = "",
        company_registry: object | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        lever_page_size: int = DEFAULT_LEVER_PAGE_SIZE,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_collection_seconds: float = DEFAULT_MAX_SECONDS,
        max_jobs: int = DEFAULT_MAX_JOBS,
    ) -> None:
        self.db_path = str(db_path or "")
        self.company_registry = company_registry
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
        self.lever_page_size = max(1, int(lever_page_size or DEFAULT_LEVER_PAGE_SIZE))
        self.max_response_bytes = max(1024, int(max_response_bytes or DEFAULT_MAX_RESPONSE_BYTES))
        self.collection_errors: list[str] = []
        self.collection_incomplete_reasons: list[str] = []
        self._job_cache: dict[str, _CareerJob] = {}
        self._scan_revisions: dict[str, int] = {}
        self._pending_jobs: dict[str, _CareerJob] = {}
        self._budget = RunBudget(max_requests=max_requests, max_total_bytes=max_total_bytes,
                                 max_seconds=max_collection_seconds, max_jobs=max_jobs)
        self._public_cookies = http.cookiejar.CookieJar()

    @classmethod
    def from_options(cls, options: Mapping[str, object] | None = None) -> CareerPagesSource:
        options = options or {}
        return cls(
            db_path=str(options.get("db_path", "")),
            company_registry=options.get("company_registry"),
            timeout_seconds=int(options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            lever_page_size=int(options.get("lever_page_size", DEFAULT_LEVER_PAGE_SIZE)),
            max_response_bytes=int(options.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)),
            max_requests=options.get("max_requests", DEFAULT_MAX_REQUESTS),
            max_total_bytes=options.get("max_total_bytes", DEFAULT_MAX_TOTAL_BYTES),
            max_collection_seconds=options.get("max_collection_seconds", DEFAULT_MAX_SECONDS),
            max_jobs=options.get("max_jobs", DEFAULT_MAX_JOBS),
        )

    def collect_vacancy_urls(
        self,
        *,
        keyword: str | None = None,
        listing_url: str = "",
        max_pages: int = 1,
        before_listing_fetch: Callable[[str], None] | None = None,
        stop_at_vacancy: Callable[[str], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        del keyword, listing_url, stop_at_vacancy
        max_pages = max(1, int(max_pages or 1))
        self.collection_errors = []
        self.collection_incomplete_reasons = []
        self._job_cache = {}
        self._budget.reset()

        companies = self._list_collectable_companies()
        self._scan_revisions = {str(company["company_id"]): int(company.get("source_revision", 0)) for company in companies}
        if not companies:
            raise ValueError("No enabled career sources are available for collection.")
        collected_urls: list[str] = []
        page_urls: list[str] = []
        seen: set[str] = set()
        companies_by_id = {str(company["company_id"]): company for company in companies}
        company_counts: dict[str, int] = {}

        for company in companies:
            company_id = self._text(company.get("company_id"))
            company_name = self._text(company.get("name")) or company_id
            provider = self._text(company.get("ats_type")).lower()
            token = self._text(company.get("ats_token"))
            if provider not in SUPPORTED_ATS_TYPES or (not token and provider not in TOKEN_OPTIONAL_PROVIDERS):
                continue

            try:
                self._pending_jobs = {}
                self._budget.check()
                jobs, fetched_urls, incomplete = self._collect_company_jobs(
                    company=company,
                    provider=provider,
                    token=token,
                    max_pages=max_pages,
                    before_listing_fetch=before_listing_fetch,
                )
                if not self._source_is_current(company_id):
                    self.collection_incomplete_reasons.append(f"{company_name}: source changed during collection; old results were discarded.")
                    continue
                page_urls.extend(fetched_urls)
                company_counts[company_id] = len(jobs)
                for job in jobs:
                    if job.source_url in seen:
                        existing = self._job_cache[job.source_url]
                        previous_company = companies_by_id[existing.company_id]
                        if (existing.company_name == job.company_name and previous_company.get("name") != existing.company_name
                                and company.get("name") == job.company_name):
                            self._job_cache[job.source_url] = job
                        elif existing.company_id != job.company_id and existing.company_name != job.company_name:
                            reason = f"Shared career job URL has conflicting employers: {existing.company_name}, {job.company_name}; check board ownership."
                            if reason not in self.collection_incomplete_reasons:
                                self.collection_incomplete_reasons.append(reason)
                            self._record_scan(existing.company_id, "partial", jobs_count=company_counts.get(existing.company_id), error=reason)
                            incomplete = True
                        continue
                    seen.add(job.source_url)
                    self._job_cache[job.source_url] = job
                    collected_urls.append(job.source_url)
                if incomplete:
                    reason = f"{company_name} {provider} collection is incomplete; inspect the career page or increase max_pages={max_pages}"
                    self.collection_incomplete_reasons.append(reason)
                    self._record_scan(company_id, "partial", jobs_count=len(jobs), error=reason)
                else:
                    self._record_scan(company_id, "completed", jobs_count=len(jobs))
            except CollectionCancelledError:
                raise
            except CareerBudgetExceeded as error:
                reason = self._compact_error(error)
                self.collection_incomplete_reasons.append(reason)
                pending_jobs = self._pending_jobs if self._source_is_current(company_id) else {}
                for job in pending_jobs.values():
                    if job.source_url not in seen:
                        seen.add(job.source_url)
                        self._job_cache[job.source_url] = job
                        collected_urls.append(job.source_url)
                remaining_index = companies.index(company)
                for unchecked in companies[remaining_index:]:
                    self._record_scan(str(unchecked["company_id"]), "partial", error=reason,
                                      jobs_count=len(pending_jobs) if unchecked is company and pending_jobs else None)
                break
            except Exception as error:  # noqa: BLE001 - isolate one company board from the rest
                message = f"{company_name} ({provider}): {self._compact_error(error)}"
                self.collection_errors.append(message)
                self._record_scan(company_id, "failed", error=message)

        return collected_urls, page_urls

    def before_vacancy_processing(self) -> None:
        self._budget.check()

    def fetch_vacancy_page(self, url: str) -> str:
        self.validate_vacancy_url(url)
        job = self._job_cache.get(url)

        return json.dumps(
            {
                "company_id": job.company_id,
                "company_name": job.company_name,
                "provider": job.provider,
                "source_id": job.source_id,
                "source_url": job.source_url,
                "title": job.title,
                "location": job.location,
                "salary_text": job.salary_text,
                "description": job.description,
                "requirements": list(job.requirements),
                "raw": job.raw,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def validate_vacancy_url(self, url: str) -> None:
        job = self._job_cache.get(url)
        if job is None:
            raise ValueError(f"Career job was not found in the listing cache: {url}")
        if not self._source_is_current(job.company_id):
            raise ValueError("Career source changed after discovery; run collection again before processing this job.")
    def career_source_revision_for(self, url: str) -> tuple[str, int] | None:
        from ..companies import CompanyRegistry
        job = self._job_cache.get(url)
        if isinstance(self.company_registry, CompanyRegistry) and job and job.company_id in self._scan_revisions:
            return job.company_id, self._scan_revisions[job.company_id]
        return None

    def _source_is_current(self, company_id: str) -> bool:
        from ..companies import CompanyRegistry
        if not isinstance(self.company_registry, CompanyRegistry) or company_id not in self._scan_revisions:
            return True
        company = self.company_registry.get(company_id)
        return bool(company and company.get("source_revision", 0) == self._scan_revisions[company_id])

    def parse_vacancy(self, html_text: str, source_url: str) -> Vacancy:
        try:
            payload = json.loads(html_text)
        except json.JSONDecodeError as error:
            raise ValueError("Cached career job payload is not valid JSON.") from error

        description = self._text(payload.get("description"))
        return Vacancy(
            source_name=self.name,
            source_id=self._text(payload.get("source_id")) or source_url,
            source_url=self._text(payload.get("source_url")) or source_url,
            title=self._text(payload.get("title")),
            company=self._text(payload.get("company_name")),
            location=self._text(payload.get("location")),
            salary_text=self._text(payload.get("salary_text")),
            requirements=[self._text(item) for item in payload.get("requirements", []) if self._text(item)],
            responsibilities=[description] if description else [],
            raw_text=description,
        )

    def can_handle_url(self, url: str) -> bool:
        return url in self._job_cache

    def _list_collectable_companies(self) -> list[dict]:
        registry = self.company_registry or self._build_company_registry()
        self.company_registry = registry
        list_companies = getattr(registry, "list_companies", None)
        if not callable(list_companies):
            raise ValueError("Career company registry does not provide list_companies().")
        companies = list_companies(enabled_only=True)
        return [
            dict(company)
            for company in companies
            if isinstance(company, Mapping)
            and self._text(company.get("ats_type")).lower() in SUPPORTED_ATS_TYPES
            and (self._text(company.get("ats_token")) or company.get("ats_type") in TOKEN_OPTIONAL_PROVIDERS)
        ]

    def _build_company_registry(self) -> object:
        if not self.db_path:
            raise ValueError("Career source requires a db_path option.")
        try:
            from ..companies import CompanyRegistry
            from ..storage import DatabaseManager
        except ImportError as error:
            raise ValueError("Career source requires CompanyRegistry in cvbankas_tracker.storage.") from error
        database = DatabaseManager(self.db_path)
        database.initialize()
        return CompanyRegistry(database)

    def _collect_company_jobs(
        self,
        *,
        company: Mapping[str, object],
        provider: str,
        token: str,
        max_pages: int,
        before_listing_fetch: Callable[[str], None] | None,
    ) -> tuple[list[_CareerJob], list[str], bool]:
        if company.get("ats_sources"):
            jobs, pages, incomplete = [], [], False
            failures = []
            seen_boards = set()
            primary = {**{key: value for key, value in company.items() if key != "ats_sources"}, "ats_type": provider, "ats_token": token}
            for board in [*company["ats_sources"], primary]:
                base = {key: value for key, value in company.items() if key not in {
                    "ats_type", "ats_token", "ats_url", "career_url", "api_url", "fallback_url",
                    "greenhouse_api_url", "greenhouse_metadata_filter", "vacancy_company_name",
                }}
                merged = {**base, **board}
                merged.pop("ats_sources", None)
                board_provider = str(merged["ats_type"])
                board_key = (
                    board_provider, str(merged.get("ats_token") or ""),
                    self._uses_eu_lever(merged) if board_provider == "lever" else None,
                    (merged.get("ats_url") or merged.get("career_url")) if board_provider not in {"ashby", "lever", "greenhouse"} else None,
                    str(merged.get("greenhouse_api_url") or ""),
                    json.dumps(merged.get("greenhouse_metadata_filter"), sort_keys=True),
                    str(merged.get("language") or merged.get("locale") or ""),
                )
                if board_key in seen_boards:
                    continue
                seen_boards.add(board_key)
                try:
                    board_jobs, board_pages, partial = self._collect_company_jobs(
                        company=merged, provider=str(merged["ats_type"]), token=str(merged.get("ats_token") or ""),
                        max_pages=max_pages, before_listing_fetch=before_listing_fetch,
                    )
                    jobs.extend(board_jobs)
                    pages.extend(board_pages)
                    incomplete |= partial
                except (CollectionCancelledError, CareerBudgetExceeded):
                    raise
                except Exception as error:  # noqa: BLE001 - retain successful boards
                    failures.append(self._compact_error(error))
                    incomplete = True
            if failures and not pages:
                raise ValueError("All company boards failed: " + "; ".join(failures))
            return jobs, pages, incomplete
        if provider == "ashby":
            return self._collect_ashby(company, token, before_listing_fetch)
        if provider == "greenhouse":
            return self._collect_greenhouse(company, token, before_listing_fetch)
        if provider == "lever":
            return self._collect_lever(company, token, max_pages, before_listing_fetch)
        if provider in SUPPORTED_EXTRA_ATS or provider in {"html", "workday", "elastic_custom", "astro_server_island", "seb"}:
            def fetch_html(url):
                if company.get("company_id") == "epam":
                    return self._fetch_public_text(url, before_listing_fetch, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                return self._fetch_public_text(url, before_listing_fetch)

            def fetch_json(url, payload=None, headers=None):
                return json.loads(self._fetch_public_text(url, before_listing_fetch, payload, headers))
            if provider == "html":
                from .career_html import collect_html
                primary_error = None
                try:
                    items, pages, incomplete = collect_html(dict(company), max_pages, fetch_html, max_detail_pages=min(5000, max_pages * 100))
                    if not items and company.get("career_url") and company.get("career_url") != company.get("ats_url"):
                        fallback_items, fallback_pages, fallback_incomplete = collect_html({**company, "ats_url": company["career_url"]}, max_pages, fetch_html, max_detail_pages=min(5000, max_pages * 100))
                        items, pages, incomplete = fallback_items, pages + fallback_pages, incomplete or fallback_incomplete
                except CollectionCancelledError:
                    raise
                except (ValueError, OSError) as error:
                    if not company.get("career_url") or company.get("career_url") == company.get("ats_url"):
                        raise
                    items, pages, incomplete = collect_html({**company, "ats_url": company["career_url"]}, max_pages, fetch_html, max_detail_pages=min(5000, max_pages * 100))
                    incomplete = True
                    primary_error = self._compact_error(error)
                if primary_error:
                    self.collection_incomplete_reasons.append(f"{company.get('name')} primary career page failed; fallback used: {primary_error}")
            elif provider == "workday":
                from .career_workday import collect_workday
                items, pages, incomplete = collect_workday(dict(company), max_pages, fetch_json)
            elif provider == "seb":
                from .career_seb import collect_seb
                items, pages, incomplete = collect_seb(dict(company), max_pages, fetch_json, fetch_html)
            elif provider in {"elastic_custom", "astro_server_island"}:
                from .career_custom import collect_custom
                items, pages, incomplete = collect_custom(provider, dict(company), max_pages, fetch_json, fetch_html, self._public_cookie_value)
            else:
                items, pages, incomplete = collect_feed(provider, dict(company), max_pages, fetch_json, fetch_html)
            jobs = [self._job(
                company=company, provider=provider,
                source_id=self._text(item.get("id") or item.get("source_id") or item.get("url")),
                source_url=self._text(item.get("url")), title=self._text(item.get("title")),
                location=self._text(item.get("location")), salary_text=self._text(item.get("salary_text")),
                description=self._text(item.get("description")),
                requirements=tuple(item.get("requirements") or ()), raw=item,
            ) for item in items]
            return jobs, pages, incomplete
        raise ValueError(f"Unsupported career ATS type: {provider}")

    def _public_cookie_value(self, name: str, domain: str = "") -> str:
        return next((cookie.value for cookie in self._public_cookies
                     if cookie.name == name and (not domain or cookie.domain.lstrip(".") == domain.lstrip("."))), "")

    def _fetch_public_text(self, url: str, before_listing_fetch: Callable[[str], None] | None, payload: dict | None = None, headers: dict | None = None) -> str:
        if before_listing_fetch is not None:
            before_listing_fetch(url)
        self._budget.before_request()
        self._budget.read_limit(self.max_response_bytes)
        _validate_public_url(url)
        request = Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers={
            "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": os.getenv("JOB_SEEKER_USER_AGENT") or "JobSeekerCareerCollector/1.0",
            **({"Content-Type": "application/json"} if payload is not None else {}),
            **(headers or {}),
        })
        with build_opener(ProxyHandler({}), _PublicRedirectHandler(self._budget), _PublicHTTPHandler, _PublicHTTPSHandler, HTTPCookieProcessor(self._public_cookies)).open(request, timeout=self.timeout_seconds) as response:
            content = _read_response(response, self._budget, self.max_response_bytes)
            return content.decode(response.headers.get_content_charset() or "utf-8", errors="replace")

    def _collect_ashby(
        self,
        company: Mapping[str, object],
        token: str,
        before_listing_fetch: Callable[[str], None] | None,
    ) -> tuple[list[_CareerJob], list[str], bool]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        payload = self._fetch_json(url, before_listing_fetch)
        items = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("Ashby response did not contain a jobs list.")
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("Ashby response contained malformed job entries.")
        jobs = [
            self._ashby_job(company, item)
            for item in items
            if isinstance(item, dict) and item.get("isListed", True) is True
        ]
        return jobs, [url], False

    def _collect_greenhouse(
        self,
        company: Mapping[str, object],
        token: str,
        before_listing_fetch: Callable[[str], None] | None,
    ) -> tuple[list[_CareerJob], list[str], bool]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        if company.get("greenhouse_api_url"):
            url = str(company["greenhouse_api_url"])
            payload = json.loads(self._fetch_public_text(url, before_listing_fetch))
            if isinstance(payload, list):
                payload = {"jobs": payload, "meta": {"total": len(payload)}}
        else:
            payload = self._fetch_json(url, before_listing_fetch)
        items = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("Greenhouse response did not contain a jobs list.")
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("Greenhouse response contained malformed job entries.")
        meta_total = payload.get("meta", {}).get("total") if isinstance(payload.get("meta"), dict) else None
        if isinstance(meta_total, int) and meta_total != len(items):
            raise ValueError("Greenhouse response was incomplete.")
        metadata_filter = company.get("greenhouse_metadata_filter")
        if metadata_filter:
            if not isinstance(metadata_filter, Mapping) or not metadata_filter.get("id") or not metadata_filter.get("value"):
                raise ValueError("Greenhouse metadata filter requires an id and value.")
            items = [item for item in items if any(
                isinstance(meta, Mapping)
                and str(meta.get("id")) == str(metadata_filter["id"])
                and str(meta.get("value")) == str(metadata_filter["value"])
                for meta in (item.get("metadata") or [])
            )]
        return [self._greenhouse_job(company, item) for item in items if isinstance(item, dict)], [url], False

    def _collect_lever(
        self,
        company: Mapping[str, object],
        token: str,
        max_pages: int,
        before_listing_fetch: Callable[[str], None] | None,
    ) -> tuple[list[_CareerJob], list[str], bool]:
        host = (
            "api.eu.lever.co"
            if self._uses_eu_lever(company)
            else "api.lever.co"
        )
        jobs: list[_CareerJob] = []
        urls: list[str] = []
        incomplete = False
        for page in range(max_pages):
            query = urlencode(
                {
                    "mode": "json",
                    "skip": page * self.lever_page_size,
                    "limit": self.lever_page_size,
                }
            )
            url = f"https://{host}/v0/postings/{token}?{query}"
            payload = self._fetch_json(url, before_listing_fetch)
            if not isinstance(payload, list):
                raise ValueError("Lever response was not a postings list.")
            if not all(isinstance(item, dict) for item in payload):
                raise ValueError("Lever response contained malformed job entries.")
            urls.append(url)
            jobs.extend(self._lever_job(company, item) for item in payload if isinstance(item, dict))
            if len(payload) < self.lever_page_size:
                return jobs, urls, False
        if jobs and len(jobs) % self.lever_page_size == 0:
            incomplete = True
        return jobs, urls, incomplete

    def _fetch_json(
        self,
        url: str,
        before_listing_fetch: Callable[[str], None] | None,
    ) -> object:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in ALLOWED_API_HOSTS:
            raise ValueError(f"Unsupported career API URL: {url}")
        if before_listing_fetch is not None:
            before_listing_fetch(url)
        self._budget.before_request()
        self._budget.read_limit(self.max_response_bytes)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": os.getenv("JOB_SEEKER_USER_AGENT")
                or "JobSeekerCareerCollector/1.0",
            },
        )
        opener = build_opener(_AllowedHostRedirectHandler(self._budget))
        with opener.open(request, timeout=self.timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            content = _read_response(response, self._budget, self.max_response_bytes)
            return json.loads(content.decode(charset, errors="replace"))

    def _ashby_job(self, company: Mapping[str, object], item: Mapping[str, object]) -> _CareerJob:
        job_id = self._text(item.get("id")) or self._text(item.get("externalLink")) or self._text(item.get("title"))
        url = (
            self._text(item.get("jobUrl"))
            or self._text(item.get("externalLink"))
            or f"https://jobs.ashbyhq.com/{self._text(company.get('ats_token'))}/{job_id}"
        )
        location = self._join_values(
            [
                item.get("location"),
                item.get("locationName"),
                "Remote" if item.get("isRemote") is True or item.get("workplaceType") == "Remote" else "",
            ]
        )
        compensation = item.get("compensation")
        salary = self._format_compensation(compensation if isinstance(compensation, Mapping) else None)
        return self._job(
            company=company,
            provider="ashby",
            source_id=f"{self._text(company.get('company_id'))}:{job_id}",
            source_url=url,
            title=self._text(item.get("title")),
            location=location,
            salary_text=salary,
            description=self._clean_html(
                self._text(item.get("descriptionHtml"))
                or self._text(item.get("description"))
                or self._text(item.get("descriptionPlain"))
            ),
            requirements=(),
            raw=dict(item),
        )

    def _greenhouse_job(self, company: Mapping[str, object], item: Mapping[str, object]) -> _CareerJob:
        location = ""
        if isinstance(item.get("location"), Mapping):
            location = self._text(item["location"].get("name"))
        metadata = item.get("metadata") if isinstance(item.get("metadata"), list) else []
        salary = self._join_values(
            meta.get("value") for meta in metadata if isinstance(meta, Mapping) and "salary" in self._text(meta.get("name")).lower()
        )
        return self._job(
            company=company,
            provider="greenhouse",
            source_id=f"{self._text(company.get('company_id'))}:{self._text(item.get('id'))}",
            source_url=self._text(item.get("absolute_url")),
            title=self._text(item.get("title")),
            location=location,
            salary_text=salary,
            description=self._clean_html(self._text(item.get("content"))),
            requirements=(),
            raw=dict(item),
        )

    def _lever_job(self, company: Mapping[str, object], item: Mapping[str, object]) -> _CareerJob:
        urls = item.get("hostedUrl")
        if isinstance(item.get("urls"), Mapping):
            urls = item["urls"].get("show") or item["urls"].get("list")
        lists = item.get("lists") if isinstance(item.get("lists"), list) else []
        requirements = tuple(
            self._clean_html(section.get("content", ""))
            for section in lists
            if isinstance(section, Mapping) and self._text(section.get("content"))
        )
        categories = item.get("categories") if isinstance(item.get("categories"), Mapping) else {}
        location = self._join_values([categories.get("location"), item.get("workplaceType")])
        description_parts = [
            self._text(item.get("descriptionPlain")),
            self._clean_html(self._text(item.get("description"))),
            self._text(item.get("additionalPlain")),
        ]
        return self._job(
            company=company,
            provider="lever",
            source_id=f"{self._text(company.get('company_id'))}:{self._text(item.get('id'))}",
            source_url=self._text(urls),
            title=self._text(item.get("text")),
            location=location,
            salary_text=self._text(item.get("salaryDescription")),
            description=self._join_values(description_parts),
            requirements=requirements,
            raw=dict(item),
        )

    def _job(
        self,
        *,
        company: Mapping[str, object],
        provider: str,
        source_id: str,
        source_url: str,
        title: str,
        location: str,
        salary_text: str,
        description: str,
        requirements: tuple[str, ...],
        raw: dict,
    ) -> _CareerJob:
        if not source_url:
            raise ValueError(f"{provider} job is missing a public URL.")
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"{provider} job has an invalid public URL.")
        if not title:
            raise ValueError(f"{provider} job is missing a title.")
        job = _CareerJob(
            company_id=self._text(company.get("company_id")),
            company_name=self._text(company.get("vacancy_company_name") or company.get("name")),
            provider=provider,
            source_id=source_id,
            source_url=source_url,
            title=title,
            location=location,
            salary_text=salary_text,
            description=description,
            requirements=requirements,
            raw=raw,
        )
        self._budget.add_job(source_url)
        self._pending_jobs[source_url] = job
        return job

    def _record_scan(
        self,
        company_id: str,
        status: str,
        *,
        jobs_count: int | None = None,
        error: str = "",
    ) -> None:
        registry = self.company_registry
        if registry is None:
            try:
                registry = self._build_company_registry()
                self.company_registry = registry
            except ValueError:
                return
        record_scan = getattr(registry, "record_scan", None)
        if callable(record_scan):
            from ..companies import CompanyRegistry
            options = {"expected_revision": self._scan_revisions[company_id]} if isinstance(registry, CompanyRegistry) and company_id in self._scan_revisions else {}
            try:
                accepted = record_scan(company_id, status, error=error, jobs_count=jobs_count, **options)
                if accepted is False:
                    self.collection_incomplete_reasons.append(f"{company_id}: source changed during collection; run again to check the current configuration.")
            except Exception as scan_error:  # noqa: BLE001 - status persistence must not stop other companies
                self.collection_errors.append(f"{company_id}: could not save career check status: {self._compact_error(scan_error)}")

    def _uses_eu_lever(self, company: Mapping[str, object]) -> bool:
        for field in ("ats_url", "career_url", "api_url"):
            host = urlparse(self._text(company.get(field))).hostname
            if host in {"jobs.lever.co", "api.lever.co", "jobs.eu.lever.co", "api.eu.lever.co"}:
                return host in {"jobs.eu.lever.co", "api.eu.lever.co"}
        return self._text(company.get("region")).lower() in {"eu", "europe"}

    @staticmethod
    def _text(value: object) -> str:
        if value is None:
            return ""
        return " ".join(str(value).split())

    def _join_values(self, values: Iterable[object]) -> str:
        parts: list[str] = []
        for value in values:
            text = self._clean_html(self._text(value))
            if text and text not in parts:
                parts.append(text)
        return ", ".join(parts)

    def _format_compensation(self, compensation: Mapping[str, object] | None) -> str:
        if not compensation:
            return ""
        text = self._text(compensation.get("compensationTierSummary"))
        if text:
            return text
        currency = self._text(compensation.get("currencyCode") or compensation.get("currency"))
        interval = self._text(compensation.get("interval"))
        minimum = self._text(compensation.get("minValue") or compensation.get("minimum"))
        maximum = self._text(compensation.get("maxValue") or compensation.get("maximum"))
        if minimum and maximum:
            return self._join_values([f"{minimum}-{maximum}", currency, interval])
        return self._join_values([minimum or maximum, currency, interval])

    @staticmethod
    def _clean_html(value: str) -> str:
        value = html.unescape(value or "")
        value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def _compact_error(error: Exception) -> str:
        return (" ".join(str(error).split()) or type(error).__name__)[:240]
