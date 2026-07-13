"""SQLAlchemy 模型包。import 本包即可把全部表注册进 Base.metadata（create_all / alembic 用）。"""

from app.models.activity import Activity
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.experiment import Experiment, ExperimentRun
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.paper import Concept, Paper, paper_concepts
from app.models.project import Project, ProjectMember
from app.models.review import ReviewMessage, ReviewSession
from app.models.user import User

__all__ = [
    "Activity",
    "Concept",
    "Experiment",
    "ExperimentRun",
    "Gate",
    "Idea",
    "Manuscript",
    "ManuscriptFile",
    "Paper",
    "Project",
    "ProjectMember",
    "ReviewMessage",
    "ReviewSession",
    "TimestampMixin",
    "User",
    "UUIDPrimaryKeyMixin",
    "paper_concepts",
]
