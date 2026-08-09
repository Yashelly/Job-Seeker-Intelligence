"""Multi-source vacancy discovery, analysis, and tracking package."""

from .analysis import (
    AIBasedAnalysisStrategy,
    DemoAIAnalysisClient,
    OpenAIAnalysisClient,
    RuleBasedAnalysisStrategy,
    VacancyAnalysisBuilder,
    VacancyAnalysisService,
)
from .collector import CvbankasCollector
from .io_utils import ProfileFileReader, ReportFileWriter
from .models import (
    AnalysisMethod,
    ApplicationRecord,
    ApplicationStatus,
    FitLabel,
    UserProfile,
    Vacancy,
    VacancyAnalysis,
)
from .parser import VacancyParser
from .sources import (
    CvbankasSource,
    EuRemoteJobsSource,
    HhHtmlSource,
    JustJoinItSource,
    SampleVacancySource,
    StartupJobsSource,
    VacancySource,
    resolve_sources,
)
from .storage import DatabaseManager
from .tracking import ApplicationTracker

__all__ = [
    "AIBasedAnalysisStrategy",
    "AnalysisMethod",
    "ApplicationRecord",
    "ApplicationStatus",
    "ApplicationTracker",
    "CvbankasCollector",
    "CvbankasSource",
    "DatabaseManager",
    "DemoAIAnalysisClient",
    "EuRemoteJobsSource",
    "FitLabel",
    "HhHtmlSource",
    "JustJoinItSource",
    "OpenAIAnalysisClient",
    "ProfileFileReader",
    "ReportFileWriter",
    "RuleBasedAnalysisStrategy",
    "SampleVacancySource",
    "StartupJobsSource",
    "UserProfile",
    "Vacancy",
    "VacancyAnalysis",
    "VacancyAnalysisBuilder",
    "VacancyAnalysisService",
    "VacancyParser",
    "VacancySource",
    "resolve_sources",
]
