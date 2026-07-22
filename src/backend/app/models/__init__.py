"""SQLAlchemy 模型包。import 本包即可把全部表注册进 Base.metadata（create_all / alembic 用）。"""

from app.models.activity import Activity
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.experiment import Experiment, ExperimentRun
from app.models.feedback import Feedback, FeedbackImage
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.library import UserLibraryEntry
from app.models.library_direction import DirectionLibrary, DirectionLibraryCurator, LibraryPaper
from app.models.llm_config import LLMCallLog, LLMProviderConfig, LLMUsage, ModelRoute
from app.models.manuscript import (
    Manuscript,
    ManuscriptFile,
    ManuscriptFileVersion,
    ManuscriptTemplate,
)
from app.models.paper import (
    Concept,
    Paper,
    PaperChunk,
    PaperHighlight,
    PaperNote,
    PaperTag,
    PaperUserMeta,
    paper_concepts,
    paper_tag_links,
)
from app.models.project import Project, ProjectInvite, ProjectMember
from app.models.publication import UserAuthorProfile, UserPublication
from app.models.registration_code import RegistrationCode
from app.models.review import ReviewMessage, ReviewSession
from app.models.skill import ProjectSkill, Skill, SkillListing, SkillRating, SkillVersion
from app.models.ssh_credential import SSHCredential
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.models.voyage import VoyageRun, VoyageStep

__all__ = [
    "Activity",
    "Concept",
    "DirectionLibrary",
    "DirectionLibraryCurator",
    "Experiment",
    "ExperimentRun",
    "Feedback",
    "FeedbackImage",
    "Gate",
    "Idea",
    "LLMCallLog",
    "LibraryPaper",
    "LLMProviderConfig",
    "LLMUsage",
    "Manuscript",
    "ManuscriptFile",
    "ManuscriptFileVersion",
    "ManuscriptTemplate",
    "ModelRoute",
    "Paper",
    "PaperChunk",
    "PaperHighlight",
    "PaperNote",
    "PaperTag",
    "PaperUserMeta",
    "Project",
    "ProjectInvite",
    "ProjectMember",
    "ProjectSkill",
    "RegistrationCode",
    "ReviewMessage",
    "ReviewSession",
    "SSHCredential",
    "Skill",
    "SkillListing",
    "SkillRating",
    "SkillVersion",
    "SystemSetting",
    "TimestampMixin",
    "User",
    "UserAuthorProfile",
    "UserLibraryEntry",
    "UserPublication",
    "UUIDPrimaryKeyMixin",
    "VoyageRun",
    "VoyageStep",
    "paper_concepts",
    "paper_tag_links",
]
