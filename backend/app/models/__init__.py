"""SQLAlchemy 模型包。import 本包即可把全部表注册进 Base.metadata（create_all / alembic 用）。"""

from app.models.activity import Activity
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.experiment import Experiment, ExperimentRun
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.llm_config import LLMProviderConfig, LLMUsage, ModelRoute
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.paper import (
    Concept,
    Paper,
    PaperNote,
    PaperTag,
    PaperUserMeta,
    paper_concepts,
    paper_tag_links,
)
from app.models.project import Project, ProjectMember
from app.models.review import ReviewMessage, ReviewSession
from app.models.ssh_credential import SSHCredential
from app.models.skill import ProjectSkill, Skill, SkillListing, SkillRating, SkillVersion
from app.models.user import User
from app.models.voyage import VoyageRun, VoyageStep

__all__ = [
    "Activity",
    "Concept",
    "Experiment",
    "ExperimentRun",
    "Gate",
    "Idea",
    "LLMProviderConfig",
    "LLMUsage",
    "Manuscript",
    "ManuscriptFile",
    "ModelRoute",
    "Paper",
    "PaperNote",
    "PaperTag",
    "PaperUserMeta",
    "Project",
    "ProjectMember",
    "ReviewMessage",
    "ReviewSession",
    "ProjectSkill",
    "SSHCredential",
    "TimestampMixin",
    "User",
    "Skill",
    "SkillListing",
    "SkillRating",
    "SkillVersion",
    "UUIDPrimaryKeyMixin",
    "VoyageRun",
    "VoyageStep",
    "paper_concepts",
    "paper_tag_links",
]
