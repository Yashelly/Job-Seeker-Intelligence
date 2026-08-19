from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# CEFR language levels, ordered from lowest to highest proficiency. Used to
# express and compare an English-level ceiling on a profile against the level a
# vacancy requires.
CEFR_LEVELS: tuple[str, ...] = ("A1", "A2", "B1", "B2", "C1", "C2")


def normalize_cefr_level(value: object) -> str | None:
    """Coerce an arbitrary value to a canonical CEFR level (e.g. 'B2') or None.

    Anything that is not one of the six CEFR levels degrades to None so a bogus
    profile value simply means "no English-level ceiling" instead of raising.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().upper()
    return text if text in CEFR_LEVELS else None


class AnalysisMethod(StrEnum):
    AI_BASED = "ai_based"
    RULE_BASED = "rule_based"


class FitLabel(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class ApplicationStatus(StrEnum):
    SAVED = "Saved"
    APPLIED = "Applied"
    INTERVIEW = "Interview"
    OFFER = "Offer"
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"


class ApplicationStatusOrigin(StrEnum):
    CLI = "cli"
    TUI = "tui"
    WEB = "web"
    MIGRATION = "migration"
    SYSTEM = "system"


class ApplicationStatusEventKind(StrEnum):
    NORMAL = "normal"
    CORRECTIVE = "corrective"
    BASELINE = "baseline"


class ActionState(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"


@dataclass(slots=True)
class Vacancy:
    source_id: str
    source_url: str
    title: str
    company: str
    location: str
    salary_text: str
    requirements: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    raw_text: str = ""
    source_name: str = "cvbankas"

    @property
    def searchable_text(self) -> str:
        return " ".join(
            [
                self.title,
                self.company,
                self.location,
                self.salary_text,
                *self.requirements,
                *self.responsibilities,
            ]
        ).lower()


@dataclass(slots=True)
class UserProfile:
    name: str
    target_roles: list[str]
    skills: list[str]
    preferred_locations: list[str]
    experience_level: str
    # Whole years are stored as int; sub-year experience uses a fraction
    # (e.g. 0.2, 0.4, 0.8) so near-match scoring can grade it against a
    # vacancy that asks for "1 year".
    years_of_experience: int | float | None = None
    salary_expectation: str | None = None
    additional_keywords: list[str] = field(default_factory=list)
    must_have_skills: list[str] = field(default_factory=list)
    nice_to_have_skills: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    # Highest English level the candidate is comfortable with, as a CEFR level
    # (e.g. "B2"). When set, vacancies requiring English above this level are
    # penalized in analysis. None means no ceiling.
    max_english_level: str | None = None


@dataclass(frozen=True, slots=True)
class VacancyAnalysis:
    vacancy_source_url: str
    analysis_method: AnalysisMethod
    score: int
    fit_label: FitLabel
    explanation: str
    matched_points: tuple[str, ...]
    missing_points: tuple[str, ...]
    notes: str = ""




@dataclass(frozen=True, slots=True)
class CollectionRun:
    id: int
    db_path: str
    started_at: str
    finished_at: str | None
    status: str
    source_summary: dict[str, object]
    error_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class InboxPreferences:
    minimum_score: int = 0
    hide_below_threshold: bool = False
    sort_by: str = "score"
    source_name: str = ""
    fit_label: str = ""
    application_status: str = ""
    new_only: bool = False
    current_run_only: bool = True


@dataclass(frozen=True, slots=True)
class InboxItem:
    source_name: str
    source_id: str
    source_url: str
    original_source_url: str | None
    title: str
    company: str
    location: str
    first_seen_at: str | None
    last_seen_at: str | None
    first_seen_run_id: int | None
    last_seen_run_id: int | None
    is_new_in_run: bool
    is_current_run: bool
    latest_score: int | None
    latest_fit_label: str | None
    explanation: str
    matched_points: tuple[str, ...]
    missing_points: tuple[str, ...]
    application_status: ApplicationStatus | None


@dataclass(frozen=True, slots=True)
class VacancyListItem:
    source_name: str
    source_id: str
    source_url: str
    title: str
    company: str
    location: str
    latest_score: int | None
    latest_fit_label: str | None
    application_status: ApplicationStatus | None


@dataclass(frozen=True, slots=True)
class TrackedApplicationItem:
    source_name: str
    source_id: str
    source_url: str
    title: str
    company: str
    status: ApplicationStatus
    latest_score: int | None
    latest_fit_label: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class ApplicationStatusEvent:
    id: int
    vacancy_source_url: str
    previous_status: ApplicationStatus | None
    new_status: ApplicationStatus
    changed_at: str
    origin: ApplicationStatusOrigin
    kind: ApplicationStatusEventKind
    note: str
    reason: str


@dataclass(frozen=True, slots=True)
class ActionItem:
    id: int
    vacancy_source_url: str
    title: str
    notes: str
    due_at_utc: str | None
    state: ActionState
    completed_at_utc: str | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True, slots=True)
class ActionReminderItem:
    action: ActionItem
    reminder_state: str


class ApplicationRecord:
    _ALLOWED_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
        ApplicationStatus.SAVED: {
            ApplicationStatus.APPLIED,
            ApplicationStatus.INTERVIEW,
            ApplicationStatus.WITHDRAWN,
        },
        ApplicationStatus.APPLIED: {
            ApplicationStatus.INTERVIEW,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        },
        ApplicationStatus.INTERVIEW: {
            ApplicationStatus.OFFER,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        },
        ApplicationStatus.OFFER: {ApplicationStatus.WITHDRAWN},
        ApplicationStatus.REJECTED: set(),
        ApplicationStatus.WITHDRAWN: set(),
    }

    def __init__(
        self,
        vacancy_source_url: str,
        analysis_id: int | None = None,
        status: ApplicationStatus = ApplicationStatus.SAVED,
        notes: str = "",
    ) -> None:
        self.vacancy_source_url = vacancy_source_url
        self.analysis_id = analysis_id
        self._status = status
        self._notes = notes

    @property
    def status(self) -> ApplicationStatus:
        return self._status

    @property
    def notes(self) -> str:
        return self._notes

    def update_status(self, new_status: ApplicationStatus) -> None:
        if new_status == self._status:
            raise ValueError(f"Status is already {new_status.value}.")

        allowed = self._ALLOWED_TRANSITIONS[self._status]
        if new_status not in allowed:
            raise ValueError(
                f"Cannot move application from {self._status.value} to {new_status.value}."
            )

        self._status = new_status

    def set_status(self, new_status: ApplicationStatus) -> None:
        self._status = new_status

    def set_notes(self, notes: str) -> None:
        self._notes = notes.strip()
