from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import quote, urljoin, urlparse

from .career_html import collect_html

SUPPORTED_EXTRA_ATS = {"personio", "recruitee", "smartrecruiters", "teamtailor", "workable", "paylocity"}
_DEFAULT_PAGE_SIZE = 100
_DESCRIPTION_MIN_CHARS = 120


def collect_feed(
    provider: str,
    company: dict,
    max_pages: int,
    fetch_json: Callable[[str], object],
    fetch_html: Callable[[str], str],
) -> tuple[list[dict], list[str], bool]:
    normalized = _text(provider).lower()
    max_pages = max(1, int(max_pages or 1))
    if normalized == "workable":
        return _collect_workable(company, fetch_json)
    if normalized == "paylocity":
        token = _provider_token(company, "paylocity")
        url = f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{quote(token)}"
        payload = fetch_json(url)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Paylocity response did not contain a jobs list.")
        jobs = []
        for item in payload["jobs"]:
            if not isinstance(item, Mapping):
                raise ValueError("Paylocity response contained malformed postings.")
            jobs.append(_job(item, source_id=f"paylocity:{token}:{item.get('jobId')}",
                title=_text(item.get("title")), url=_valid_url(_text(item.get("applyUrl"))),
                location=_text(item.get("location")), description=_clean_html(_text(item.get("description")) + " " + _text(item.get("requirements"))),
                salary_text="", company_name=_text(item.get("companyName") or company.get("name"))))
        return jobs, [url], False
    if normalized == "recruitee":
        return _collect_recruitee(company, fetch_json)
    if normalized == "smartrecruiters":
        return _collect_smartrecruiters(company, max_pages, fetch_json)
    if normalized == "personio":
        return _collect_personio(company, fetch_html)
    if normalized == "teamtailor":
        return _collect_teamtailor(company, max_pages, fetch_html)
    raise ValueError(f"Unsupported career feed provider: {provider}")


def _collect_workable(
    company: Mapping[str, object],
    fetch_json: Callable[[str], object],
) -> tuple[list[dict], list[str], bool]:
    token = _provider_token(company, "workable")
    url = f"https://apply.workable.com/api/v1/widget/accounts/{quote(token)}?details=true"
    payload = fetch_json(url)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        raise ValueError("Workable response did not contain a jobs list.")
    company_name = _text(payload.get("name")) or _text(company.get("name"))
    jobs = [_workable_job(company, item, company_name, token) for item in payload["jobs"] if isinstance(item, Mapping)]
    if len(jobs) != len(payload["jobs"]):
        raise ValueError("Workable response contained malformed job entries.")
    return jobs, [url], False


def _collect_recruitee(
    company: Mapping[str, object],
    fetch_json: Callable[[str], object],
) -> tuple[list[dict], list[str], bool]:
    host = _recruitee_host(company)
    url = f"https://{host}/api/offers"
    payload = fetch_json(url)
    items = payload.get("offers") if isinstance(payload, Mapping) else None
    if items is None and isinstance(payload, list):
        items = payload
    if not isinstance(items, list):
        raise ValueError("Recruitee response did not contain an offers list.")
    jobs = [_recruitee_job(company, item, host) for item in items if isinstance(item, Mapping)]
    if len(jobs) != len(items):
        raise ValueError("Recruitee response contained malformed offer entries.")
    return jobs, [url], False


def _collect_smartrecruiters(
    company: Mapping[str, object],
    max_pages: int,
    fetch_json: Callable[[str], object],
) -> tuple[list[dict], list[str], bool]:
    token = _provider_token(company, "smartrecruiters")
    urls: list[str] = []
    jobs: list[dict] = []
    incomplete = False
    for page in range(max_pages):
        offset = page * _DEFAULT_PAGE_SIZE
        list_url = (
            f"https://api.smartrecruiters.com/v1/companies/{quote(token)}/postings"
            f"?limit={_DEFAULT_PAGE_SIZE}&offset={offset}"
        )
        payload = fetch_json(list_url)
        items = payload.get("content") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            raise ValueError("SmartRecruiters response did not contain a postings list.")
        urls.append(list_url)
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("SmartRecruiters response contained malformed posting entries.")
            posting_id = _text(item.get("id") or item.get("uuid"))
            if not posting_id:
                raise ValueError("SmartRecruiters posting is missing an id.")
            detail_url = f"https://api.smartrecruiters.com/v1/companies/{quote(token)}/postings/{quote(posting_id)}"
            detail = fetch_json(detail_url)
            urls.append(detail_url)
            if not isinstance(detail, Mapping):
                raise ValueError("SmartRecruiters posting detail was not an object.")
            jobs.append(_smartrecruiters_job(company, detail, token))
        total = payload.get("totalFound") or payload.get("total")
        if isinstance(total, int):
            if offset + len(items) >= total:
                return jobs, urls, False
            continue
        if len(items) < _DEFAULT_PAGE_SIZE:
            return jobs, urls, False
    if jobs:
        incomplete = True
    return jobs, urls, incomplete


def _collect_personio(
    company: Mapping[str, object],
    fetch_html: Callable[[str], str],
) -> tuple[list[dict], list[str], bool]:
    token = _provider_token(company, "personio")
    language = _text(company.get("language") or company.get("locale"))[:2].lower() or "en"
    if language not in {"de", "en", "fr", "es", "nl", "it", "pt"}:
        language = "en"
    url = _personio_xml_url(company, token, language)
    xml_text = fetch_html(url)
    root = _parse_xml(xml_text, "Personio")
    if _local_name(root.tag) != "workzag-jobs":
        raise ValueError("Personio XML response did not contain a workzag-jobs feed.")
    positions = [element for element in root.iter() if _local_name(element.tag) == "position"]
    if not positions:
        return [], [url], False
    return [_personio_job(company, item, token) for item in positions], [url], False


def _collect_teamtailor(
    company: Mapping[str, object],
    max_pages: int,
    fetch_html: Callable[[str], str],
) -> tuple[list[dict], list[str], bool]:
    feed_url = _teamtailor_feed_url(company)
    xml_text = fetch_html(feed_url)
    root = _parse_xml(xml_text, "Teamtailor RSS")
    if _local_name(root.tag) != "rss":
        raise ValueError("Teamtailor response was not an RSS feed.")
    channel = next((item for item in root if _local_name(item.tag) == "channel"), None)
    if channel is None:
        raise ValueError("Teamtailor RSS response did not contain a channel.")
    items = [element for element in channel if _local_name(element.tag) == "item"]
    if not items:
        return [], [feed_url], False

    pages = [feed_url]
    jobs: list[dict] = []
    incomplete = False
    for item in items:
        try:
            job = _teamtailor_item_job(company, item)
            if len(job["description"]) < _DESCRIPTION_MIN_CHARS:
                detail_url = job["url"]
                if len(pages) >= max_pages:
                    incomplete = True
                    continue
                detail_jobs, detail_pages, detail_incomplete = collect_html(
                    {**dict(company), "career_url": detail_url, "ats_url": detail_url},
                    1,
                    fetch_html,
                )
                pages.extend(page for page in detail_pages if page not in pages)
                incomplete = incomplete or detail_incomplete
                if detail_jobs:
                    job = {**detail_jobs[0], "id": job["id"], "raw": job["raw"]}
            jobs.append(job)
        except (ValueError, OSError):
            incomplete = True
    return jobs, pages, incomplete


def _workable_job(company: Mapping[str, object], item: Mapping[str, object], company_name: str, token: str) -> dict:
    shortcode = _text(item.get("shortcode") or item.get("id"))
    url = _valid_url(
        _text(item.get("url"))
        or _text(item.get("application_url"))
        or _text(item.get("shortlink"))
        or f"https://apply.workable.com/{token}/j/{shortcode}/"
    )
    location = _join_values(
        [
            _location_from_mapping(item.get("location")),
            item.get("city"),
            item.get("state"),
            item.get("country"),
            "Remote" if item.get("telecommuting") is True else "",
        ]
    )
    description = _clean_html(
        _text(item.get("full_description"))
        or _text(item.get("description"))
        or _text(item.get("requirements"))
    )
    return _job(
        item,
        source_id=f"workable:{token}:{shortcode or url}",
        title=_text(item.get("title") or item.get("full_title")),
        url=url,
        location=location,
        description=description,
        salary_text=_salary_from_mapping(item.get("salary")),
        company_name=company_name or _text(company.get("name")),
    )


def _recruitee_job(company: Mapping[str, object], item: Mapping[str, object], host: str) -> dict:
    slug = _text(item.get("slug") or item.get("offer_slug"))
    job_id = _text(item.get("id") or slug)
    url = _valid_url(
        _text(item.get("careers_url"))
        or _text(item.get("url"))
        or _text(item.get("offer_url"))
        or f"https://{host}/o/{slug or job_id}"
    )
    location = _join_values(
        [
            _location_from_mapping(item.get("location")),
            item.get("city"),
            item.get("state_name"),
            item.get("country"),
            item.get("remote") or item.get("remote_status"),
        ]
    )
    description = _clean_html(
        _text(item.get("description"))
        or _text(item.get("description_html"))
        or _text(item.get("requirements"))
    )
    requirements = _requirements([item.get("requirements"), item.get("requirements_html")])
    return _job(
        item,
        source_id=f"recruitee:{host}:{job_id or url}",
        title=_text(item.get("title")),
        url=url,
        location=location,
        description=description,
        salary_text=_salary_from_mapping(item.get("salary")),
        company_name=_text(company.get("name")),
        requirements=requirements,
    )


def _smartrecruiters_job(company: Mapping[str, object], item: Mapping[str, object], token: str) -> dict:
    posting_id = _text(item.get("id") or item.get("uuid"))
    job_ad = item.get("jobAd") if isinstance(item.get("jobAd"), Mapping) else {}
    sections = job_ad.get("sections") if isinstance(job_ad.get("sections"), Mapping) else {}
    description = _join_values(
        _clean_html(_text(section.get("text") if isinstance(section, Mapping) else section))
        for section in sections.values()
    )
    url = _valid_url(
        _text(item.get("applyUrl"))
        or _text(item.get("url"))
        or f"https://jobs.smartrecruiters.com/{token}/{posting_id}"
    )
    location = _join_values([_location_from_mapping(item.get("location")), item.get("remote")])
    return _job(
        item,
        source_id=f"smartrecruiters:{token}:{posting_id or url}",
        title=_text(item.get("name") or item.get("title")),
        url=url,
        location=location,
        description=description,
        salary_text=_salary_from_mapping(item.get("salary")),
        company_name=_text(company.get("name")),
        requirements=_requirements(
            section.get("text")
            for key, section in sections.items()
            if isinstance(section, Mapping) and "qualif" in _text(key).lower()
        ),
    )


def _personio_job(company: Mapping[str, object], item: ET.Element, token: str) -> dict:
    job_id = _child_text(item, "id") or _child_text(item, "recruitingCategory") or _child_text(item, "name")
    url = _valid_url(_child_text(item, "jobUrl") or f"https://{token}.jobs.personio.de/job/{job_id}")
    description = _join_values(
        _child_text(item, name)
        for name in (
            "jobDescriptions",
            "jobDescription",
            "description",
            "tasks",
            "profile",
            "benefits",
        )
    )
    location = _join_values([_child_text(item, "office"), _child_text(item, "city"), _child_text(item, "country")])
    return _job(
        {},
        source_id=f"personio:{token}:{job_id or url}",
        title=_child_text(item, "name"),
        url=url,
        location=location,
        description=_clean_html(description),
        salary_text="",
        company_name=_text(company.get("name")),
    )


def _teamtailor_item_job(company: Mapping[str, object], item: ET.Element) -> dict:
    title = _child_direct_text(item, "title")
    url = _valid_url(_child_direct_text(item, "link") or _child_direct_text(item, "guid"))
    description = _clean_html(
        _child_direct_text(item, "description")
        or _child_direct_text(item, "encoded")
        or _child_direct_text(item, "content")
    )
    location = _join_values(
        [
            _child_direct_text(item, "location"),
            _child_direct_text(item, "category"),
        ]
    )
    return _job(
        {"source": "rss"},
        source_id=f"teamtailor:{url}",
        title=title,
        url=url,
        location=location,
        description=description,
        salary_text="",
        company_name=_text(company.get("name")),
    )


def _parse_xml(xml_text: str, label: str) -> ET.Element:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE):
        raise ValueError(f"{label} XML response cannot contain DTD or entity declarations.")
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise ValueError(f"{label} XML response was malformed.") from error


def _personio_xml_url(company: Mapping[str, object], token: str, language: str) -> str:
    for field in ("ats_url", "career_url"):
        value = _text(company.get(field))
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname:
            host = parsed.hostname.lower()
            if host.endswith(".jobs.personio.de") or host.endswith(".jobs.personio.com"):
                return f"https://{host}/xml?language={language}"
    return f"https://{quote(token)}.jobs.personio.de/xml?language={language}"


def _teamtailor_feed_url(company: Mapping[str, object]) -> str:
    for field in ("ats_url", "career_url"):
        value = _text(company.get(field))
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname:
            return urljoin(f"https://{parsed.hostname.lower()}", "/jobs.rss")
    token = _text(company.get("ats_token"))
    if token:
        return f"https://{quote(token)}.teamtailor.com/jobs.rss"
    raise ValueError("teamtailor company is missing a career URL or ATS token.")


def _provider_token(company: Mapping[str, object], provider: str) -> str:
    token = _text(company.get("ats_token"))
    if token:
        return token
    inferred = _infer_token_from_url(provider, _text(company.get("ats_url")) or _text(company.get("career_url")))
    if inferred:
        return inferred
    raise ValueError(f"{provider} company is missing an ATS token.")


def _recruitee_host(company: Mapping[str, object]) -> str:
    for field in ("ats_url", "career_url"):
        value = _text(company.get(field))
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".recruitee.com"):
            return parsed.hostname.lower()
    return f"{_provider_token(company, 'recruitee')}.recruitee.com"


def _infer_token_from_url(provider: str, url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = [part for part in parsed.path.split("/") if part]
    if provider == "workable" and host == "apply.workable.com" and path:
        return path[0]
    if provider == "smartrecruiters" and host == "careers.smartrecruiters.com" and path:
        return path[0]
    if provider == "personio" and host.endswith(".jobs.personio.de"):
        return host.removesuffix(".jobs.personio.de")
    if provider == "personio" and host.endswith(".jobs.personio.com"):
        return host.removesuffix(".jobs.personio.com")
    if provider == "teamtailor" and (
        host.endswith(".teamtailor.com")
        or host.endswith(".na.teamtailor.com")
        or host.endswith(".au.teamtailor.com")
    ):
        return host.split(".", 1)[0]
    if provider == "recruitee" and host.endswith(".recruitee.com"):
        return host.removesuffix(".recruitee.com")
    return ""


def _job(
    raw: Mapping[str, object],
    *,
    source_id: str,
    title: str,
    url: str,
    location: str,
    description: str,
    salary_text: str,
    company_name: str,
    requirements: list[str] | None = None,
) -> dict:
    if not title:
        raise ValueError("Career feed job is missing a title.")
    if not description:
        raise ValueError(f"Career feed job is missing a full description: {title}")
    return {
        "id": source_id,
        "title": title,
        "url": url,
        "location": location,
        "description": description,
        "salary_text": salary_text,
        "requirements": requirements or [],
        "company": company_name,
        "raw": dict(raw),
    }


def _valid_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Career feed job is missing a valid public URL.")
    return value


def _location_from_mapping(value: object) -> str:
    if not isinstance(value, Mapping):
        return _text(value)
    return _join_values(
        value.get(key)
        for key in (
            "location_str",
            "city",
            "region",
            "state",
            "country",
            "country_code",
            "address",
        )
    )


def _salary_from_mapping(value: object) -> str:
    if not isinstance(value, Mapping):
        return _text(value)
    return _join_values(
        value.get(key)
        for key in (
            "salary_from",
            "salary_to",
            "salary_currency",
            "minValue",
            "maxValue",
            "currency",
            "unitText",
            "value",
        )
    )


def _requirements(values: Iterable[object]) -> list[str]:
    requirements: list[str] = []
    for value in values:
        text = _clean_html(_text(value))
        if text and text not in requirements:
            requirements.append(text)
    return requirements


def _child_text(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if _local_name(child.tag) == name:
            return _clean_html("".join(child.itertext()))
    return ""


def _child_direct_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return _clean_html("".join(child.itertext()))
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _join_values(values: Iterable[object]) -> str:
    parts: list[str] = []
    for value in values:
        text = _clean_html(_text(value))
        if text and text not in parts:
            parts.append(text)
    return ", ".join(parts)


def _clean_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()
