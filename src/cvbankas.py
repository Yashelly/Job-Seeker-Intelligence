from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .extractor import extract_job_structured
from .utils import dedupe_preserve_order, normalize_spaces

BASE_URL = "https://www.cvbankas.lt/"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
}

JOB_PATH_RE = re.compile(r"/[^?#]+/\d+-\d+/?$")
SALARY_RANGE_RE = re.compile(
    r"(?P<min>\d[\d\s.,]{2,})\s*[-–]\s*(?P<max>\d[\d\s.,]{2,})\s*€?\s*/\s*(?P<period>mėn\.|val\.)?",
    re.IGNORECASE,
)
SALARY_FROM_RE = re.compile(
    r"(?:nuo|from)\s*(?P<min>\d[\d\s.,]{2,})\s*€?\s*/\s*(?P<period>mėn\.|val\.)?",
    re.IGNORECASE,
)

TOP_STOP_WORDS = {
    "cvbankas.lt skaičiuoklės duomenys. redaguoti »",
    "priimame ukrainiečius",
    "приймаємо українців",
}

SECTION_STOP_HEADINGS = [
    "reikalavimai",
    "verta kandidatuoti, nes",
    "mes jums siūlome",
    "įmonė siūlo",
    "privalumai",
    "atlyginimas",
    "kontaktai",
    "kontaktinis asmuo",
    "apie įmonę",
    "įmonė",
    "darbo vieta",
    "darbo laikas",
]

CITY_HINTS = [
    "vilnius",
    "vilniuje",
    "kaunas",
    "kaune",
    "klaipėda",
    "klaipedoje",
    "klaipėdoje",
    "šiauliai",
    "šiauliuose",
    "panevėžys",
    "panevėžyje",
    "jonava",
    "jonavoje",
    "kaišiadorys",
    "kaišiadoryse",
    "lentvaris",
    "lentvaryje",
    "trakai",
    "trakuose",
    "užsienis",
    "vokietijoje",
    "darbas namuose",
    "nuotol",
]


class CVBankasError(RuntimeError):
    pass


class CVBankasHTTPError(CVBankasError):
    pass


class CVBankasParseError(CVBankasError):
    pass


def _build_page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _clean_number(value: str) -> int:
    cleaned = re.sub(r"[^\d]", "", value)
    return int(cleaned)


def _clean_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw_line in normalize_spaces(text).splitlines():
        line = raw_line.strip(" \t•*-–")
        if not line:
            continue
        if line.lower() in TOP_STOP_WORDS:
            continue
        lines.append(line)
    return lines


def _extract_section(lines: List[str], heading: str) -> List[str]:
    heading_lower = heading.lower()
    active = False
    results: List[str] = []
    for line in lines:
        lowered = line.lower()
        if lowered == heading_lower:
            active = True
            continue
        if active and lowered in SECTION_STOP_HEADINGS:
            break
        if active:
            results.append(line)
    return dedupe_preserve_order(results)


def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        title = normalize_spaces(h1.get_text(" ", strip=True))
        if title:
            return title

    meta = soup.find("meta", attrs={"property": "og:title"}) or soup.find("title")
    if meta:
        text = meta.get("content") if meta.name == "meta" else meta.get_text(" ", strip=True)
        if text:
            return normalize_spaces(text.split("| CVbankas.lt", 1)[0])
    return ""


def _looks_like_location_line(value: str) -> bool:
    lowered = value.lower()
    if any(hint in lowered for hint in CITY_HINTS):
        return True
    if "," in value and len(value) <= 120:
        return True
    return False


def _extract_company_and_location(lines: List[str], role_title: str, page_title: str) -> Tuple[str, str]:
    role_title_lower = role_title.lower().strip()
    for line in lines[:25]:
        if " - " not in line:
            continue
        left, right = [normalize_spaces(x) for x in line.split(" - ", 1)]
        if not left or not right or len(right) > 120:
            continue
        if role_title_lower and line.lower().startswith(role_title_lower):
            continue
        if _looks_like_location_line(left):
            return right, left

    clean_page_title = page_title.split("| CVbankas.lt", 1)[0].strip()
    if role_title and clean_page_title.lower().startswith(role_title_lower):
        remainder = clean_page_title[len(role_title):].strip(" ,-–")
        if "," in remainder:
            left, right = [normalize_spaces(x) for x in remainder.rsplit(",", 1)]
            if left and right:
                return right, left
    return "", ""


def _extract_salary(text: str) -> Dict[str, Any]:
    lowered = text.lower()
    salary_type = ""
    if "neatskaičius mokesčių" in lowered or "bruto" in lowered:
        salary_type = "gross"
    elif "į rankas" in lowered or "i rankas" in lowered or "neto" in lowered:
        salary_type = "net"

    m = SALARY_RANGE_RE.search(text)
    if m:
        period_raw = (m.group("period") or "mėn.").lower()
        period = "hour" if "val" in period_raw else "month"
        return {
            "min": _clean_number(m.group("min")),
            "max": _clean_number(m.group("max")),
            "currency": "EUR",
            "gross_or_net": salary_type,
            "period": period,
        }

    m = SALARY_FROM_RE.search(text)
    if m:
        period_raw = (m.group("period") or "mėn.").lower()
        period = "hour" if "val" in period_raw else "month"
        return {
            "min": _clean_number(m.group("min")),
            "max": None,
            "currency": "EUR",
            "gross_or_net": salary_type,
            "period": period,
        }

    return {"min": None, "max": None, "currency": "", "gross_or_net": "", "period": ""}


def _extract_employment_type(text: str) -> str:
    lowered = text.lower()
    if "visa darbo diena" in lowered or "full-time" in lowered or "full time" in lowered:
        return "full-time"
    if "ne visa darbo diena" in lowered or "part-time" in lowered or "part time" in lowered:
        return "part-time"
    if "lankstus grafikas" in lowered:
        return "flexible"
    return ""


def _extract_page_title(soup: BeautifulSoup) -> str:
    title_tag = soup.find("title")
    if title_tag:
        return normalize_spaces(title_tag.get_text(" ", strip=True))
    return ""


def _find_job_links(soup: BeautifulSoup, page_url: str) -> List[str]:
    links: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, anchor["href"])
        parts = urlsplit(href)
        normalized = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        if parts.netloc not in {"www.cvbankas.lt", "cvbankas.lt"}:
            continue
        if not JOB_PATH_RE.search(parts.path):
            continue
        links.append(normalized)
    return dedupe_preserve_order(links)


def fetch_html(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    headers: Dict[str, str] | None = None,
) -> str:
    sess = session or requests.Session()
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    response = sess.get(url, timeout=timeout, headers=merged_headers)
    if response.status_code >= 400:
        raise CVBankasHTTPError(f"HTTP {response.status_code} for {url}")
    return response.text


def collect_listing_links(
    *,
    start_url: str = BASE_URL,
    max_pages: int = 3,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    delay_seconds: float = 0.0,
) -> List[str]:
    if max_pages < 1:
        return []

    all_links: List[str] = []
    seen = set()

    for page in range(1, max_pages + 1):
        page_url = _build_page_url(start_url, page)
        html = fetch_html(page_url, session=session, timeout=timeout)
        soup = BeautifulSoup(html, "html.parser")
        page_links = _find_job_links(soup, page_url)

        if not page_links:
            break

        new_count = 0
        for link in page_links:
            if link in seen:
                continue
            seen.add(link)
            all_links.append(link)
            new_count += 1

        if new_count == 0:
            break

        if delay_seconds > 0 and page < max_pages:
            time.sleep(delay_seconds)

    return all_links


def parse_detail_html(html: str, *, url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    raw_text = normalize_spaces(soup.get_text("\n", strip=True))
    lines = _clean_lines(raw_text)
    if not lines:
        raise CVBankasParseError(f"Could not extract readable text from {url}")

    role_title = _extract_title(soup)
    if not role_title:
        raise CVBankasParseError(f"Could not determine vacancy title from {url}")

    page_title = _extract_page_title(soup)
    company, location = _extract_company_and_location(lines, role_title, page_title)

    responsibilities = _extract_section(lines, "Darbo pobūdis")
    requirements = _extract_section(lines, "Reikalavimai")
    benefits = (
        _extract_section(lines, "Verta kandidatuoti, nes")
        or _extract_section(lines, "Mes jums siūlome")
        or _extract_section(lines, "Įmonė siūlo")
        or _extract_section(lines, "Privalumai")
    )

    job = extract_job_structured(raw_text)
    job.update(
        {
            "source": "cvbankas",
            "source_url": url,
            "external_id": url.rstrip("/").rsplit("/", 1)[-1],
            "company": company or str(job.get("company", "")),
            "role_title": role_title,
            "location": location or str(job.get("location", "")),
            "salary": _extract_salary(raw_text),
            "employment_type": _extract_employment_type(raw_text) or str(job.get("employment_type", "")),
            "responsibilities": responsibilities or list(job.get("responsibilities", [])),
            "required_skills": list(job.get("required_skills", [])),
            "preferred_skills": list(job.get("preferred_skills", [])),
            "tools_and_platforms": list(job.get("tools_and_platforms", [])),
            "notes": (str(job.get("notes", "")).strip() + " | parsed_from=cvbankas").strip(" |"),
            "raw_text": raw_text,
            "raw_text_excerpt": raw_text[:2000],
            "benefits": benefits,
        }
    )
    return job


def fetch_and_parse_job(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    html = fetch_html(url, session=session, timeout=timeout)
    return parse_detail_html(html, url=url)
