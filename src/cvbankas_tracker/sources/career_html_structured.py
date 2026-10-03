import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlparse
from xml.etree import ElementTree


class _Anchors(HTMLParser):
    def __init__(self, results_only=False):
        super().__init__()
        self.results_only = results_only
        self.results_depth = 0
        self.href = None
        self.parts = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "ul":
            if self.results_depth:
                self.results_depth += 1
            elif attributes.get("id") == "search-results-jobs":
                self.results_depth = 1
        if tag == "a" and (not self.results_only or self.results_depth):
            self.href = attributes.get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.items.append((html.unescape(self.href), " ".join(" ".join(self.parts).split())))
            self.href = None
            self.parts = []
        if tag == "ul" and self.results_depth:
            self.results_depth -= 1


def _fetch(fetch_html, url):
    page = fetch_html(url)
    if isinstance(page, tuple):
        page = page[0]
    if isinstance(page, bytes):
        page = page.decode("utf-8", errors="replace")
    return page


def _moodys(company, max_pages, fetch_html):
    base = company.get("ats_url")
    if not base:
        return None
    jobs = {}
    pages = []
    total = None
    for number in range(1, max_pages + 1):
        url = base if number == 1 else f"{base}?p={number}"
        page = _fetch(fetch_html, url)
        pages.append(url)
        if total is None:
            match = re.search(r'data-results-count="(\d+)"', page)
            if not match:
                return None
            total = int(match.group(1))
        parser = _Anchors(results_only=True)
        parser.feed(page)
        before = len(jobs)
        for href, label in parser.items:
            job_url = urljoin(url, href)
            parsed = urlparse(job_url)
            if parsed.hostname != urlparse(base).hostname or not parsed.path.startswith("/en/job/"):
                continue
            job_id = parsed.path.rstrip("/").split("/")[-1]
            jobs[job_url] = {
                "id": job_id,
                "title": label or parsed.path.rstrip("/").split("/")[-3].replace("-", " ").title(),
                "url": job_url,
                "location": "",
                "description": "",
            }
        if len(jobs) == total:
            return list(jobs.values()), pages, False
        if len(jobs) <= before or len(jobs) > total:
            break
    return list(jobs.values()), pages, True


def _valantic(company, max_pages, fetch_html):
    base = company.get("ats_url")
    if not base:
        return None
    root = _fetch(fetch_html, base)
    categories = {}
    pattern = r'<a\b[^>]*href="(https://www\.valantic\.com/en/careers/[^"/]+/)"[^>]*>(.*?)</a>'
    for match in re.finditer(pattern, root, re.IGNORECASE | re.DOTALL):
        count = re.search(r'data-default="(\d+)"', match.group(2))
        if count:
            categories[html.unescape(match.group(1))] = int(count.group(1))
    if not categories:
        return None
    pages = [base]
    jobs = {}
    incomplete = False
    for category_url, expected in categories.items():
        if len(pages) >= max_pages:
            incomplete = True
            break
        page = _fetch(fetch_html, category_url)
        pages.append(category_url)
        parser = _Anchors()
        parser.feed(page)
        found = set()
        for href, label in parser.items:
            job_url = urljoin(category_url, href)
            parsed = urlparse(job_url)
            if parsed.hostname != urlparse(base).hostname or not parsed.path.startswith("/en/careers/vacancies/"):
                continue
            job_url = parsed._replace(query="", fragment="").geturl()
            found.add(job_url)
            title = label if 0 < len(label) <= 120 else parsed.path.rstrip("/").split("/")[-1].replace("-", " ").title()
            jobs[job_url] = {
                "id": parsed.path.rstrip("/").split("/")[-1],
                "title": title,
                "url": job_url,
                "location": "",
                "description": "",
            }
        if len(found) != expected:
            incomplete = True
    return list(jobs.values()), pages, incomplete


def _visma(company, fetch_html):
    base = company.get("ats_url")
    if not base:
        return None
    page = _fetch(fetch_html, base)
    parser = _Anchors()
    parser.feed(page)
    jobs = {}
    for href, label in parser.items:
        parsed = urlparse(urljoin(base, href))
        if not parsed.hostname or parsed.hostname == "www.linkedin.com" or "/jobs/" not in parsed.path:
            continue
        if not label or label.casefold() in {"apply", "share"}:
            continue
        job_url = parsed._replace(query="", fragment="").geturl()
        jobs[job_url] = {
            "id": job_url,
            "title": label,
            "url": job_url,
            "location": "",
            "description": "",
        }
    if not jobs:
        return None
    incomplete = bool(re.search(r'<a\b[^>]*class="[^"]*w-pagination-next', page, re.IGNORECASE))
    return list(jobs.values()), [base], incomplete


def _wargaming(company, max_pages, fetch_html):
    url = "https://wargaming.com/en/api/careers/vacancy/?limit=100"
    pages = []
    jobs = {}
    total = None
    while url and len(pages) < max_pages:
        payload = json.loads(_fetch(fetch_html, url))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return None
        count = payload.get("count")
        if not isinstance(count, int):
            return None
        if total is None:
            total = count
        elif count != total:
            return list(jobs.values()), pages, True
        pages.append(url)
        for posting in payload["results"]:
            if not isinstance(posting, dict):
                return None
            job_id = posting.get("id")
            slug = posting.get("slug")
            title = posting.get("title")
            if not job_id or not isinstance(slug, str) or not slug.startswith("vacancy_") or not title:
                return None
            job_url = urljoin(company["ats_url"], "/en/careers/" + slug + "/")
            jobs[str(job_id)] = {
                "id": str(job_id),
                "title": str(title).strip(),
                "url": job_url,
                "location": "",
                "description": "",
            }
        following = payload.get("next")
        if not following:
            return list(jobs.values()), pages, len(jobs) != total
        url = urljoin(url, following)
        if urlparse(url).hostname != "wargaming.com":
            return list(jobs.values()), pages, True
    return list(jobs.values()), pages, True


def _epam(company, max_pages, fetch_html):
    endpoint = "https://careers.epam.com/api/jobs/v2/search/careers-i18n"
    pages = []
    jobs = {}
    total = None
    incomplete = False
    for number in range(max_pages):
        query = urlencode({
            "lang": "en",
            "sortBy": "relevance;relocation=asc",
            "size": 50,
            "from": number * 50,
            "websiteLocale": "en-us",
        })
        url = endpoint + "?" + query
        payload = json.loads(_fetch(fetch_html, url))
        pages.append(url)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return list(jobs.values()), pages, True
        count = data.get("total")
        if not isinstance(count, int):
            return list(jobs.values()), pages, True
        if total is None:
            total = count
        elif count != total:
            incomplete = True
        postings = data["jobs"]
        for posting in postings:
            if not isinstance(posting, dict):
                incomplete = True
                continue
            job_id = posting.get("unique_id") or posting.get("uid")
            title = posting.get("name")
            seo = posting.get("seo")
            path = seo.get("url") if isinstance(seo, dict) else None
            if not job_id or not title or not isinstance(path, str) or "/vacancy/" not in path:
                incomplete = True
                continue
            countries = posting.get("country") or []
            location = ", ".join(str(country.get("name")) for country in countries if isinstance(country, dict) and country.get("name"))
            job_url = urljoin("https://careers.epam.com", path)
            jobs[str(job_id)] = {
                "id": str(job_id),
                "title": str(title),
                "url": job_url,
                "location": location,
                "description": str(posting.get("description") or ""),
            }
        if len(jobs) == total:
            return list(jobs.values()), pages, incomplete
        if not postings or len(jobs) > total:
            break
    return list(jobs.values()), pages, True


def _danske(company, max_pages, fetch_html):
    base = company.get("ats_url") or ""
    parsed = urlparse(base)
    site = re.search(r"/sites/([A-Za-z0-9_]+)/jobs/?$", parsed.path)
    if parsed.scheme != "https" or not site:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    endpoint = origin + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    pages = []
    jobs = {}
    total = None
    incomplete = False
    for number in range(max_pages):
        offset = f",offset={number * 200}" if number else ""
        url = (
            endpoint
            + "?onlyData=true&expand=requisitionList.workLocation,requisitionList.otherWorkLocations,"
            "requisitionList.secondaryLocations,flexFieldsFacet.values,requisitionList.requisitionFlexFields"
            f"&finder=findReqs;siteNumber={site.group(1)},"
            "facetsList=LOCATIONS%3BWORK_LOCATIONS%3BWORKPLACE_TYPES%3BTITLES%3BCATEGORIES%3B"
            f"ORGANIZATIONS%3BPOSTING_DATES%3BFLEX_FIELDS,limit=200{offset},sortBy=POSTING_DATES_DESC"
        )
        payload = json.loads(_fetch(fetch_html, url))
        pages.append(url)
        searches = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(searches, list) or len(searches) != 1 or not isinstance(searches[0], dict):
            return list(jobs.values()), pages, True
        result = searches[0]
        count = result.get("TotalJobsCount")
        postings = result.get("requisitionList")
        if not isinstance(count, int) or not isinstance(postings, list):
            return list(jobs.values()), pages, True
        if total is None:
            total = count
        elif count != total:
            incomplete = True
        for posting in postings:
            if not isinstance(posting, dict) or not posting.get("Id") or not posting.get("Title"):
                incomplete = True
                continue
            job_id = str(posting["Id"])
            jobs[job_id] = {
                "id": job_id,
                "title": str(posting["Title"]).strip(),
                "url": f"{origin}/hcmUI/CandidateExperience/en/sites/{site.group(1)}/job/{job_id}",
                "location": str(posting.get("PrimaryLocation") or ""),
                "description": str(posting.get("ShortDescriptionStr") or ""),
            }
        if len(jobs) == total:
            return list(jobs.values()), pages, incomplete
        if not postings or len(jobs) > total:
            break
    return list(jobs.values()), pages, True

def _bartus(company, fetch_html):
    base = company.get("ats_url")
    if not base:
        return None
    page = _fetch(fetch_html, base)
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", page, flags=re.IGNORECASE | re.DOTALL)
    visible = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", visible)).split())
    if "We do not run a careers page full of fictional roles." not in visible:
        return None
    parser = _Anchors()
    parser.feed(page)
    for href, _ in parser.items:
        link = urlparse(urljoin(base, href))
        if re.search(r"/(?:jobs?|roles?|positions?|vacancies)(?:/|$)|/careers/[^/]+$", link.path, re.IGNORECASE):
            return None
    return [], [base], False

def _revolut(company, fetch_html):
    base = company.get("ats_url")
    if not base:
        return None
    page = _fetch(fetch_html, base)
    match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        positions = data["props"]["pageProps"]["positions"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(positions, list):
        return None
    count = re.search(r"We have\s+([\d,]+)\s+open positions", page, re.IGNORECASE)
    incomplete = count is None or len(positions) != int(count.group(1).replace(",", ""))
    jobs = {}
    for position in positions:
        if not isinstance(position, dict):
            incomplete = True
            continue
        job_id = position.get("id")
        title = position.get("text")
        if not isinstance(job_id, str) or not isinstance(title, str) or not job_id or not title:
            incomplete = True
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        if not slug:
            incomplete = True
            continue
        locations = position.get("locations") or []
        location = ", ".join(
            item.get("name", "") for item in locations
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        jobs[job_id] = {
            "id": job_id,
            "title": title,
            "url": f"https://www.revolut.com/careers/position/{slug}-{job_id}/",
            "location": location,
            "description": str(position.get("description") or ""),
        }
    return list(jobs.values()), [base], incomplete or len(jobs) != len(positions)


def _linkedin_listing(company, fetch_html, site):
    base = company.get("ats_url")
    if not base:
        return None
    page = _fetch(fetch_html, base)
    parser = _Anchors()
    parser.feed(page)
    links = {}
    for href, label in parser.items:
        parsed = urlparse(urljoin(base, href))
        if parsed.hostname in {"www.linkedin.com", "linkedin.com"} and re.fullmatch(r"/jobs/view/\d+/?", parsed.path):
            links[parsed._replace(query="", fragment="").geturl()] = label
    if not links:
        return None
    jobs = {}
    if site == "connectpay":
        for job_url, title in links.items():
            if not title or title.casefold() in {"apply", "view role"}:
                continue
            job_id = urlparse(job_url).path.rstrip("/").split("/")[-1]
            jobs[job_url] = {"id": job_id, "title": title, "url": job_url, "location": "", "description": ""}
    else:
        pattern = (
            r'<h4\b[^>]*class="[^"]*\bheading-6\b[^"]*"[^>]*>(.*?)</h4>\s*'
            r'<span\b[^>]*>(.*?)</span>\s*</div>\s*'
            r'<a\b[^>]*href="(https://www\.linkedin\.com/jobs/view/\d+/?)"'
        )
        for match in re.finditer(pattern, page, re.IGNORECASE | re.DOTALL):
            title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())
            location = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(2))).split())
            location = location.split("\u2022", 1)[-1].strip()
            job_url = match.group(3)
            if not title:
                continue
            job_id = urlparse(job_url).path.rstrip("/").split("/")[-1]
            jobs[job_url] = {"id": job_id, "title": title, "url": job_url, "location": location, "description": ""}
    return list(jobs.values()), [base], len(jobs) != len(links)


def _teamdash(company, fetch_html):
    base = company.get("ats_url")
    if not base:
        return None
    page = _fetch(fetch_html, base)
    match = re.search(r"<script>\s*window\.context\s*=\s*(\{.*?\});\s*</script>", page, re.DOTALL)
    if not match:
        return None
    try:
        context = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(context, dict) or context.get("is_landing") is not True:
        return None
    landing = context.get("landing")
    feeds = context.get("career_page_feed_contents")
    if not isinstance(landing, dict) or landing.get("page_type") != "career" or not isinstance(feeds, dict):
        return None
    jobs = {}
    incomplete = False
    for entries in feeds.values():
        if not isinstance(entries, list):
            incomplete = True
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                incomplete = True
                continue
            job_url = entry.get("url")
            title = entry.get("title")
            parsed = urlparse(job_url) if isinstance(job_url, str) else None
            identifier = re.match(r"/p/job/([A-Za-z0-9]+)/", parsed.path) if parsed else None
            if not identifier or parsed.hostname != "vilniausvandenys.teamdash.com" or not isinstance(title, str) or not title:
                incomplete = True
                continue
            job_id = identifier.group(1)
            if job_id not in jobs:
                jobs[job_id] = {"id": job_id, "title": title, "url": job_url, "location": str(entry.get("location") or ""), "description": ""}
    return list(jobs.values()), [base], incomplete


def _cognizant(company, fetch_html):
    base = company.get("ats_url")
    if not base:
        return None
    feed_url = urljoin(base.rstrip("/") + "/", "jobs/xml/?rss=true")
    page = _fetch(fetch_html, feed_url)
    try:
        root = ElementTree.fromstring(page)
    except ElementTree.ParseError:
        return None
    if root.tag != "source" or root.findtext("publisher") != "Cognizant":
        return None
    postings = root.findall("job")
    jobs = {}
    incomplete = False
    for posting in postings:
        job_id = posting.findtext("requisitionid")
        title = posting.findtext("title")
        job_url = posting.findtext("url")
        if not job_id or not title or not job_url or urlparse(job_url).hostname != "careers.cognizant.com":
            incomplete = True
            continue
        location = ", ".join(filter(None, (posting.findtext("city"), posting.findtext("state"), posting.findtext("country"))))
        jobs[job_id] = {
            "id": job_id,
            "title": title,
            "url": job_url,
            "location": location,
            "description": posting.findtext("description") or "",
        }
    return list(jobs.values()), [feed_url], incomplete or len(jobs) != len(postings)


def _no_listed_roles(company, fetch_html, marker):
    base = company.get("ats_url")
    if not base:
        return None
    page = _fetch(fetch_html, base)
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", page, flags=re.IGNORECASE | re.DOTALL)
    visible = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", visible)).split())
    if marker not in visible:
        return None
    parser = _Anchors()
    parser.feed(page)
    for href, _ in parser.items:
        parsed = urlparse(urljoin(base, href))
        if parsed.path.rstrip("/") == urlparse(base).path.rstrip("/"):
            continue
        if re.search(r"/(?:jobs?|roles?|positions?|vacancies)(?:/|$)|/careers/[^/]+$", parsed.path, re.IGNORECASE):
            return None
    return [], [base], False

def collect_structured_html(company, max_pages, fetch_html):
    if max_pages < 1:
        return None
    source_id = company.get("company_id")
    if source_id == "revolut":
        return _revolut(company, fetch_html)
    if source_id in {"connectpay", "paystrax"}:
        return _linkedin_listing(company, fetch_html, source_id)
    if source_id == "vilniaus-vandenys":
        return _teamdash(company, fetch_html)
    if source_id == "cognizant":
        return _cognizant(company, fetch_html)
    if source_id == "idenfy":
        return _no_listed_roles(company, fetch_html, "We are waiting for your application!")
    if source_id == "strapi":
        return _no_listed_roles(company, fetch_html, "Come into the open")
    if source_id == "bartus-it-solutions":
        return _bartus(company, fetch_html)
    if source_id == "danske-bank":
        return _danske(company, max_pages, fetch_html)
    if source_id == "epam":
        return _epam(company, max_pages, fetch_html)
    if source_id == "wargaming":
        return _wargaming(company, max_pages, fetch_html)
    if source_id == "moody-s":
        return _moodys(company, max_pages, fetch_html)
    if source_id == "valantic":
        return _valantic(company, max_pages, fetch_html)
    if source_id == "visma":
        return _visma(company, fetch_html)
    return None
