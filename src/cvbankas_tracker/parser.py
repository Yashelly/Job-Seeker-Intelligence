from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from .models import Vacancy


class VacancyParser:
    def parse(
        self,
        html_text: str,
        source_url: str,
        source_name: str = "cvbankas",
    ) -> Vacancy:
        source_id = self._extract_source_id(html_text, source_url)
        location, inline_company = self._extract_location_and_inline_company(html_text)
        company = self._extract_company(html_text) or inline_company

        return Vacancy(
            source_name=source_name,
            source_id=source_id,
            source_url=source_url,
            title=self._extract_title(html_text),
            company=company,
            location=location,
            salary_text=self._extract_salary(html_text),
            requirements=self._extract_section_list(
                html_text,
                ["Reikalavimai kandidatui (-ei):", "Reikalavimai darbuotojui", "Reikalavimai"],
            ),
            responsibilities=self._extract_section_list(
                html_text,
                ["Pagrindinės atsakomybės:", "Darbo pobūdis", "Pagrindinės užduotys:"],
                fallback_intro=True,
            ),
            raw_text=html_text,
        )

    def _extract_source_id(self, html_text: str, source_url: str) -> str:
        attribute_match = re.search(r'data-source-id="([^"]+)"', html_text)
        if attribute_match:
            return attribute_match.group(1).strip()

        path_id_match = re.search(r"/(1-\d+)", source_url)
        if path_id_match:
            return path_id_match.group(1)

        path = urlparse(source_url).path.strip("/")
        return path or source_url

    def _extract_title(self, html_text: str) -> str:
        for pattern in (
            r'<h1[^>]*id="jobad_heading1"[^>]*>(.*?)</h1>',
            r'<h1[^>]*class="title"[^>]*>(.*?)</h1>',
            r'<meta\s+property="og:title"\s+content="([^"]+)"',
        ):
            match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
            if match:
                title = self._clean_text(match.group(1))
                if title:
                    return title
        return ""

    def _extract_company(self, html_text: str) -> str:
        for pattern in (
            r'<h2[^>]*id="jobad_company_title"[^>]*>(.*?)</h2>',
            r'<div[^>]*class="company"[^>]*>(.*?)</div>',
        ):
            match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
            if match:
                company = self._clean_text(match.group(1))
                if company:
                    return company
        return ""

    def _extract_location_and_inline_company(self, html_text: str) -> tuple[str, str]:
        match = re.search(
            r'<div[^>]*id="jobad_location"[^>]*>(.*?)</div>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return "", ""

        clean_text = self._clean_text(match.group(1))
        if " - " in clean_text:
            location, company = clean_text.split(" - ", maxsplit=1)
            return location.strip(), company.strip()
        return clean_text, ""

    def _extract_salary(self, html_text: str) -> str:
        salary_section_match = re.search(
            r"<h2[^>]*>\s*Atlyginimas\s*</h2>\s*<div[^>]*class=\"jobad_txt\"[^>]*>(.*?)</div>",
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        if salary_section_match:
            return self._clean_text(salary_section_match.group(1))

        amount_match = re.search(
            r'<span[^>]*class="data_tag_component_salary_amount"[^>]*>(.*?)</span>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        if amount_match:
            header_match = re.search(
                r'<div[^>]*class="data_tag_component_body[^"]*salary_bl_[^"]*"[^>]*>(.*?)</div>',
                html_text,
                re.IGNORECASE | re.DOTALL,
            )
            if header_match:
                return self._clean_text(header_match.group(1))
            return self._clean_text(amount_match.group(1))

        legacy = re.search(
            r'<div\s+class="salary">\s*(.*?)\s*</div>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        return self._clean_text(legacy.group(1)) if legacy else ""

    def _extract_section_list(
        self,
        html_text: str,
        headings: list[str],
        fallback_intro: bool = False,
    ) -> list[str]:
        for heading in headings:
            match = re.search(
                rf'<h2[^>]*>\s*{re.escape(heading)}\s*</h2>\s*<div[^>]*class="jobad_txt"[^>]*>(.*?)</div>',
                html_text,
                re.IGNORECASE | re.DOTALL,
            )
            if not match:
                continue
            items = re.findall(r"<li>(.*?)</li>", match.group(1), re.IGNORECASE | re.DOTALL)
            cleaned_items = [self._clean_text(item) for item in items if self._clean_text(item)]
            if cleaned_items:
                return cleaned_items
            clean_text = self._clean_text(match.group(1))
            if clean_text:
                return [clean_text]

        if fallback_intro:
            intro_match = re.search(
                r'<section>\s*<div[^>]*class="jobad_txt"[^>]*>(.*?)</div>\s*</section>',
                html_text,
                re.IGNORECASE | re.DOTALL,
            )
            if intro_match:
                intro = self._clean_text(intro_match.group(1))
                if intro:
                    return [intro]

        legacy_block = self._extract_legacy_list_block(html_text, headings)
        if legacy_block:
            return legacy_block

        return []

    def _extract_legacy_list_block(self, html_text: str, headings: list[str]) -> list[str]:
        css_map = {
            "reikalavimai kandidatui (-ei):": "requirements",
            "reikalavimai darbuotojui": "requirements",
            "reikalavimai": "requirements",
            "pagrindinės atsakomybės:": "responsibilities",
            "darbo pobūdis": "responsibilities",
            "pagrindinės užduotys:": "responsibilities",
        }

        for heading in headings:
            css_name = css_map.get(heading.lower())
            if not css_name:
                continue
            match = re.search(
                rf'<div\s+class="{css_name}">\s*<ul>(.*?)</ul>\s*</div>',
                html_text,
                re.IGNORECASE | re.DOTALL,
            )
            if not match:
                continue
            items = re.findall(r"<li>(.*?)</li>", match.group(1), re.IGNORECASE | re.DOTALL)
            cleaned_items = [self._clean_text(item) for item in items if self._clean_text(item)]
            if cleaned_items:
                return cleaned_items

        return []

    def _clean_text(self, value: str) -> str:
        value = html.unescape(value)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()
