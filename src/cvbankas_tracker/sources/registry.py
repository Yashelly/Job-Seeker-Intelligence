from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from .base import VacancySource
from .cvbankas import CvbankasSource
from .euremotejobs import EuRemoteJobsSource
from .hh import HhHtmlSource
from .justjoin_it import JustJoinItSource
from .sample import SampleVacancySource
from .startup_jobs import StartupJobsSource


def _options_for_source(source_options: Mapping[str, object] | None, name: str) -> dict:
    if not isinstance(source_options, Mapping):
        return {}
    options = source_options.get(name, {})
    return options if isinstance(options, dict) else {}


def build_source_registry(
    data_dir: str | Path,
    *,
    source_options: Mapping[str, object] | None = None,
) -> dict[str, VacancySource]:
    hh_source = HhHtmlSource.from_options(_options_for_source(source_options, "hh"))
    justjoin_source = JustJoinItSource()
    startup_jobs_source = StartupJobsSource()
    euremotejobs_source = EuRemoteJobsSource()
    return {
        "cvbankas": CvbankasSource(),
        "euremotejobs": euremotejobs_source,
        "eu_remote_jobs": euremotejobs_source,
        "hh": hh_source,
        "hh_html": hh_source,
        "justjoin": justjoin_source,
        "justjoin_it": justjoin_source,
        "sample": SampleVacancySource(data_dir),
        "startup_jobs": startup_jobs_source,
        "startupjobs": startup_jobs_source,
    }


def resolve_sources(
    source_names: Iterable[str],
    *,
    data_dir: str | Path,
    source_options: Mapping[str, object] | None = None,
) -> list[VacancySource]:
    registry = build_source_registry(data_dir, source_options=source_options)
    resolved: list[VacancySource] = []
    unknown: list[str] = []

    for name in source_names:
        normalized = name.strip().lower()
        if not normalized:
            continue
        source = registry.get(normalized)
        if source is None:
            unknown.append(name)
            continue
        resolved.append(source)

    if unknown:
        available = ", ".join(sorted(registry))
        raise ValueError(
            f"Unknown vacancy source(s): {', '.join(unknown)}. Available sources: {available}."
        )
    if not resolved:
        raise ValueError("At least one vacancy source must be enabled.")
    return resolved


def resolve_source_for_url(
    url: str,
    source_names: Iterable[str],
    *,
    data_dir: str | Path,
    source_options: Mapping[str, object] | None = None,
) -> VacancySource:
    for source in resolve_sources(
        source_names,
        data_dir=data_dir,
        source_options=source_options,
    ):
        if source.can_handle_url(url):
            return source

    available = ", ".join(
        source.name
        for source in resolve_sources(
            source_names,
            data_dir=data_dir,
            source_options=source_options,
        )
    )
    raise ValueError(f"No enabled source can handle URL: {url}. Enabled sources: {available}.")
