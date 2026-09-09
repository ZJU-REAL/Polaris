"""技能三套并行收敛第一步（#741）：止血 + 删审核残列

1. 新表 guidance_documents：跨学科工作流指引搬离已冻结的 v1 技能表。
   存量搬运：v1 builtin 行 interdisciplinary-research-workflow 的**每个版本**
   拷成一行（id 沿用 skill_versions.id，保留可追溯性），v1 行标记归档
   （保守起见不删——downgrade 时解除归档即可整体还原）。
2. skill_listings 删 status/decided_by/comment：#592 起发布即上架，审核状态机
   已无写入口。「在架」改由 delisted_at 表达：为空 = 在架。存量映射：
   status != 'approved' 的行（历史上的 pending/rejected/delisted）一律视为
   已下架，delisted_at 取 updated_at（下架动作会刷新它，近似准确）。

Revision ID: a5b9c3d7e1f2
Revises: 351c324f4f6b
Create Date: 2026-09-08
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a5b9c3d7e1f2"
down_revision: str | None = "351c324f4f6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUIDANCE_SLUG = "interdisciplinary-research-workflow"


def _loads(value):  # sqlite 存 JSON 文本，pg JSON 列可能已是结构
    if value is None or isinstance(value, (dict, list)):
        return value or {}
    return json.loads(value)


def upgrade() -> None:
    op.create_table(
        "guidance_documents",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", "version", name="uq_guidance_documents_slug_ver"),
    )
    op.create_index(
        op.f("ix_guidance_documents_slug"), "guidance_documents", ["slug"], unique=False
    )

    conn = op.get_bind()
    skill = conn.execute(
        sa.text(
            "SELECT id, name FROM skills WHERE slug = :slug AND scope = 'builtin'"
        ),
        {"slug": GUIDANCE_SLUG},
    ).first()
    if skill is not None:
        versions = conn.execute(
            sa.text(
                "SELECT id, version, body, manifest, created_at, updated_at "
                "FROM skill_versions WHERE skill_id = :sid ORDER BY version"
            ),
            {"sid": skill.id},
        ).fetchall()
        for row in versions:
            manifest = _loads(row.manifest)
            conn.execute(
                sa.text(
                    "INSERT INTO guidance_documents "
                    "(id, slug, version, name, body, targets, steps, created_at, updated_at) "
                    "VALUES (:id, :slug, :version, :name, :body, :targets, :steps, :ca, :ua)"
                ),
                {
                    # id 沿用 skill_versions.id：存量 checkpoint 里的 skill_version_id
                    # 仍能对回这行（str()：sqlite 存 32 位 hex 原样、pg 接受带横线串）
                    "id": str(row.id),
                    "slug": GUIDANCE_SLUG,
                    "version": row.version,
                    "name": skill.name,
                    "body": row.body,
                    "targets": json.dumps(manifest.get("targets") or [], ensure_ascii=False),
                    "steps": json.dumps(manifest.get("steps") or [], ensure_ascii=False),
                    "ca": row.created_at,
                    "ua": row.updated_at,
                },
            )
        # v1 行归档而非删除（保守；用户若在 user_skills 里手工挂过它也不至于悬空）
        conn.execute(
            sa.text("UPDATE skills SET is_archived = :yes WHERE id = :sid"),
            {"yes": True, "sid": skill.id},
        )

    # 审核残列：先把「非在架」映射到 delisted_at，再删三列
    op.add_column(
        "skill_listings", sa.Column("delisted_at", sa.DateTime(timezone=True), nullable=True)
    )
    conn.execute(
        sa.text(
            "UPDATE skill_listings SET delisted_at = updated_at WHERE status != 'approved'"
        )
    )
    with op.batch_alter_table("skill_listings") as batch:
        batch.drop_index(op.f("ix_skill_listings_status"))
        batch.drop_column("comment")
        batch.drop_column("decided_by")
        batch.drop_column("status")


def downgrade() -> None:
    # 状态机按 f5a6b7c8d9e0 的形状回来；decided_by/comment 数据不可恢复（落 NULL——
    # 与「审核流从未真正用过」的现实一致）
    with op.batch_alter_table("skill_listings") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="approved")
        )
        batch.add_column(sa.Column("decided_by", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("comment", sa.Text(), nullable=True))
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE skill_listings SET status = 'delisted' WHERE delisted_at IS NOT NULL")
    )
    with op.batch_alter_table("skill_listings") as batch:
        batch.drop_column("delisted_at")
        batch.create_index(op.f("ix_skill_listings_status"), ["status"], unique=False)

    # v1 行解除归档（内容从未离开 skills/skill_versions，直接还原）。
    # 迁移后在 guidance_documents 里新增的版本（若有）不搬回 v1——builtin 技能在
    # v1 本就只能由种子/迁移写入，回退丢弃新增版本是明确的取舍。
    conn.execute(
        sa.text(
            "UPDATE skills SET is_archived = :no WHERE slug = :slug AND scope = 'builtin'"
        ),
        {"no": False, "slug": GUIDANCE_SLUG},
    )
    op.drop_index(op.f("ix_guidance_documents_slug"), table_name="guidance_documents")
    op.drop_table("guidance_documents")
