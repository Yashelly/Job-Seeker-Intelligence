from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .utils import normalize_spaces


@dataclass
class IngestedContent:
    source_type: str
    source_ref: str
    raw_text: str


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)


def ingest_text(text: str) -> IngestedContent:
    return IngestedContent(source_type="text", source_ref="inline", raw_text=normalize_spaces(text))


def ingest_text_file(path: str) -> IngestedContent:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    return IngestedContent(source_type="text_file", source_ref=str(p), raw_text=normalize_spaces(raw))


def ingest_url(url: str, timeout: int = 20) -> IngestedContent:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = normalize_spaces(text)
    return IngestedContent(source_type="url", source_ref=url, raw_text=text)


def ingest(source_text: Optional[str] = None, source_file: Optional[str] = None, source_url: Optional[str] = None) -> IngestedContent:
    provided = [x for x in [source_text, source_file, source_url] if x]
    if len(provided) != 1:
        raise ValueError("Exactly one of source_text, source_file, source_url must be provided.")

    if source_text:
        return ingest_text(source_text)
    if source_file:
        return ingest_text_file(source_file)
    return ingest_url(source_url)  # type: ignore[arg-type]
