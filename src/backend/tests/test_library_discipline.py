"""文献库选学科，把学科包这条线接到用户手里（#763）。

闭环判据：建库时选学科 → 抽取按该学科口径 → **方法库检索得到**。最后一环最容易漏，
漏掉的表现是「装了学科包，方法卡也抽了，但方法库里一条都没有」——没有任何报错。
"""

import pytest

from app.services import discipline_packs as dp
from app.services import method_index
from tests.conftest import make_project_with_library, register_and_login


async def test_known_disciplines_includes_the_builtin_pack():
    assert "structural" in dp.known_disciplines()


async def test_library_can_declare_a_discipline(client):
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)

    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": "structural"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["discipline"] == "structural"


async def test_unknown_discipline_is_refused_rather_than_stored(client):
    """存一个匹配不到任何 schema 的名字，表现是「选了学科但口径没变」——
    看起来生效了，其实静默无效。宁可当场拒绝。"""
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)

    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": "astrology"}, headers=headers
    )
    assert resp.status_code == 400
    assert "UNKNOWN_DISCIPLINE" in resp.json()["detail"]


async def test_discipline_can_be_cleared(client):
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)

    await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": "structural"}, headers=headers
    )
    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": None}, headers=headers
    )
    assert resp.status_code == 200
    # 清空 = 回到只用跨学科通用的内置 schema
    assert resp.json()["discipline"] is None


async def test_method_library_looks_at_the_disciplines_cards_too(client):
    """闭环的最后一环：学科方法卡必须能被方法库检索到，否则装了等于没装。"""
    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary

    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)
    lib_uuid = library_id  # conftest 已给 UUID 对象

    async with get_sessionmaker()() as session:
        # 没声明学科：只认内置 method@1，与学科包出现之前完全一致
        assert await method_index.method_schema_ids(session, lib_uuid) == ["method"]

        library = await session.get(DirectionLibrary, lib_uuid)
        library.discipline = "structural"
        await session.commit()

    async with get_sessionmaker()() as session:
        ids = await method_index.method_schema_ids(session, lib_uuid)
    # 并集而不是替换：结构库里既有本学科论文，也有跨学科综述，两种卡都要检索得到
    assert ids == ["method", "structural.method"]


async def test_no_library_context_means_builtin_only():
    """个人书架导入等没有库上下文的路径不受学科影响。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        assert await method_index.method_schema_ids(session, None) == ["method"]


@pytest.mark.parametrize("discipline", ["structural"])
async def test_declared_discipline_scopes_extraction(client, discipline):
    """库声明学科后，抽取范围 = 内置 + 该学科；别的学科的 schema 不掺进来。"""
    from app.services.extraction.schemas import schemas_for

    ids = {s.id for s in schemas_for(discipline)}
    assert "structural.method" in ids
    assert {"skeleton", "method", "gaps"} <= ids


# ---- 抽取侧的闭环：学科卡抽出来之后，双轴索引必须跟着刷 ----


async def test_a_discipline_method_card_counts_as_a_method_card():
    """``<包名>.method`` 和内置 ``method`` 一样是方法卡；别的 schema 不是。"""
    assert method_index.is_method_schema_id("method")
    assert method_index.is_method_schema_id("structural.method")
    assert not method_index.is_method_schema_id("skeleton")
    assert not method_index.is_method_schema_id("gaps")


async def test_index_refreshes_when_only_the_discipline_card_is_new(client, tmp_path):
    """先导论文、之后才给库选学科——学科包最常见的用法，也是最容易静默失效的一条。

    第二遍富集时内置 method 是「已抽过」跳过，只有学科卡是新的。如果刷索引只认内置
    id，这一遍就一次都不刷：学科卡落了表却没有向量，方法库里查不到它，且**没有任何
    报错**。用例先清空向量再跑第二遍，向量回来才算这条线通。
    """
    import uuid as _uuid

    from sqlalchemy import delete, select

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary
    from app.models.paper import Paper
    from app.models.paper_extraction import PaperExtraction
    from app.models.vectors import MethodVector
    from app.services import paper_enrich
    from tests.conftest import add_paper

    async def _noop_emit(stage, status, detail=None):  # noqa: ARG001
        return None

    token = await register_and_login(client, email="discmethod@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="discmethod-proj"
    )

    full_text = tmp_path / "paper.txt"
    full_text.write_text(
        "Discipline Method Paper\n\nIntroduction\n\n"
        "We probe how the method schema behaves under deterministic extraction.\n\n"
        "Method\n\nA deterministic mechanism section with enough prose to look like a paper.\n",
        encoding="utf-8",
    )

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=_uuid.UUID(project_id),
            title="Discipline Method Paper",
            full_text_path=str(full_text),
        )
        await session.commit()
        paper_id = paper.id

    # 第一遍：库还没选学科，只抽内置 schema
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=library, user_id=None, project_id=None, emit=_noop_emit
        )

    async with get_sessionmaker()() as session:
        ids = (
            (
                await session.execute(
                    select(PaperExtraction.schema_id).where(
                        PaperExtraction.paper_id == paper_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "structural.method" not in ids
        # 清空向量，好让第二遍「有没有刷」变成一个可观测的事实
        await session.execute(delete(MethodVector).where(MethodVector.paper_id == paper_id))
        library = await session.get(DirectionLibrary, library_id)
        library.discipline = "structural"
        await session.commit()

    # 第二遍：内置卡已抽过被跳过，新的只有学科卡
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=library, user_id=None, project_id=None, emit=_noop_emit
        )

    async with get_sessionmaker()() as session:
        ids = set(
            (
                await session.execute(
                    select(PaperExtraction.schema_id).where(
                        PaperExtraction.paper_id == paper_id
                    )
                )
            )
            .scalars()
            .all()
        )
        axes = sorted(
            (
                await session.execute(
                    select(MethodVector.axis).where(MethodVector.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
    assert "structural.method" in ids, "选了学科之后该抽学科方法卡"
    assert axes, "学科卡抽出来了却没刷索引——方法库里查不到它，而且不报错"


async def test_the_discipline_card_wins_when_both_cards_are_on_the_table(client):
    """学科库里两张方法卡并存时，进向量的必须是**定的**那一张，否则检索结果会漂移。

    学科卡是为这个库写的，更具体，优先它。用例让两张卡的轴数不同，好让「取了哪一张」
    变成一个能断言的事实。
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary
    from app.models.paper import Paper
    from app.models.paper_extraction import PaperExtraction
    from app.models.vectors import MethodVector
    from tests.conftest import add_paper

    token = await register_and_login(client, email="discpick@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="discpick-proj"
    )

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        library.discipline = "structural"
        paper = await add_paper(
            session, project_id=_uuid.UUID(project_id), title="Two Cards", status="scored"
        )
        session.add_all(
            [
                # 内置卡：只有 purpose
                PaperExtraction(
                    paper_id=paper.id,
                    schema_id="method",
                    payload={"purpose": "generic machine learning purpose"},
                ),
                # 学科卡：两根轴都有
                PaperExtraction(
                    paper_id=paper.id,
                    schema_id="structural.method",
                    payload={
                        "purpose": "predict beam column joint failure under seismic load",
                        "mechanism": "explicit finite element simulation with shell elements",
                    },
                ),
            ]
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await method_index.refresh_paper_method_index(session, paper, library_id=library_id)
        await session.commit()

    async with get_sessionmaker()() as session:
        axes = sorted(
            (
                await session.execute(
                    select(MethodVector.axis).where(MethodVector.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
    # 两根轴 = 取的是学科卡；只有 purpose 说明取到了内置卡
    assert axes == ["mechanism", "purpose"]


# ---- 闭环的最后一跳：方法库检索真的返回学科卡 ----
#
# 本文件开头写的判据是「建库时选学科 → 抽取按该学科口径 → **方法库检索得到**」。
# 前两环各有用例，最后一跳此前一次都没跑过——而 bug 恰恰在那里（#786）。


async def _library_with_two_cards(client, email):
    """建一个声明了学科的库，放一篇同时有内置卡和学科卡的论文，两张卡都建好向量。"""
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary
    from app.models.paper import Paper
    from app.models.paper_extraction import PaperExtraction
    from tests.conftest import add_paper

    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="dup")

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        library.discipline = "structural"
        paper = await add_paper(
            session,
            project_id=_uuid.UUID(project_id),
            title="Blast response of composite beams",
            status="scored",
        )
        session.add_all(
            [
                PaperExtraction(
                    paper_id=paper.id,
                    schema_id="method",
                    payload={
                        "purpose": "improve blast resistance of beams",
                        "mechanism": "generic machine learning surrogate",
                    },
                ),
                PaperExtraction(
                    paper_id=paper.id,
                    schema_id="structural.method",
                    payload={
                        "purpose": "improve blast resistance of beams",
                        "mechanism": "explicit finite element simulation",
                        "structure": "steel concrete composite beam",
                    },
                ),
            ]
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await method_index.refresh_paper_method_index(
            session, paper, library_id=library_id
        )
        await session.commit()
    return library_id, paper_id


async def test_a_paper_with_both_cards_appears_once(client):
    """不去重的话同一篇会出现两次：一次领域口径，一次它本该替换掉的机器学习口径。"""
    from app.core.db import get_sessionmaker

    library_id, paper_id = await _library_with_two_cards(client, "dup1@example.com")
    async with get_sessionmaker()() as session:
        cards, _mode = await method_index.search_methods(
            session, library_id, "blast resistance of beams"
        )
    assert [c["paper_id"] for c in cards] == [paper_id]


async def test_the_card_shown_is_the_discipline_one(client):
    """显示的必须是向量所依据的那张。取学科卡建向量、却显示内置卡的话，
    界面上那个相似度是用另一段文本算出来的。"""
    from app.core.db import get_sessionmaker

    library_id, _paper_id = await _library_with_two_cards(client, "dup2@example.com")
    async with get_sessionmaker()() as session:
        cards, _mode = await method_index.search_methods(
            session, library_id, "blast resistance of beams"
        )
    assert cards and cards[0]["mechanism"] == "explicit finite element simulation"


async def test_a_review_with_only_the_builtin_card_is_still_found(client):
    """并集是对的：学科库里也有只带内置卡的跨学科综述，它们不能因为去重被挤掉。"""
    from app.core.db import get_sessionmaker
    from app.models.paper import Paper
    from app.models.paper_extraction import PaperExtraction
    from tests.conftest import add_paper

    library_id, first_paper = await _library_with_two_cards(client, "dup3@example.com")

    async with get_sessionmaker()() as session:
        from app.models.library_direction import DirectionLibrary

        library = await session.get(DirectionLibrary, library_id)
        review = await add_paper(
            session,
            project_id=library.project_id,
            title="A cross-disciplinary review",
            status="scored",
        )
        session.add(
            PaperExtraction(
                paper_id=review.id,
                schema_id="method",
                payload={
                    "purpose": "survey blast resistance approaches",
                    "mechanism": "narrative synthesis",
                },
            )
        )
        await session.commit()
        review_id = review.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, review_id)
        await method_index.refresh_paper_method_index(
            session, paper, library_id=library_id
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        cards, _mode = await method_index.search_methods(
            session, library_id, "blast resistance"
        )
    found = {c["paper_id"] for c in cards}
    assert found == {first_paper, review_id}
    assert len(cards) == 2, "每篇仍是一张卡"


# ---- 卡片内容：学科包换上的字段必须真的到达用户 ----


async def test_the_card_carries_the_packs_own_fields(client):
    """写死内置五键的话，结构工程的 构件/作用/分析/验证 在 API 边界就被丢掉——
    装了包、选了学科、每篇多付一次抽取，界面上却只有目的和机制两行。"""
    from app.core.db import get_sessionmaker

    library_id, _paper_id = await _library_with_two_cards(client, "cardf1@example.com")
    async with get_sessionmaker()() as session:
        cards = await method_index.list_methods(session, library_id)
    assert cards
    names = {f["name"] for f in cards[0]["fields"]}
    assert "structure" in names, "学科字段没进卡片"
    by_name = {f["name"]: f for f in cards[0]["fields"]}
    assert by_name["structure"]["value"] == "steel concrete composite beam"


async def test_a_field_carries_the_label_the_pack_declared(client):
    """``structure`` 这种机器名在中文界面下读起来像半成品；包自己写标签。"""
    from app.core.db import get_sessionmaker

    library_id, _paper_id = await _library_with_two_cards(client, "cardf2@example.com")
    async with get_sessionmaker()() as session:
        cards = await method_index.list_methods(session, library_id)
    by_name = {f["name"]: f for f in cards[0]["fields"]}
    assert by_name["structure"]["label"] == "结构对象"


async def test_a_field_with_no_label_falls_back_to_its_name(client):
    """包没写标签不该让这一行没名字。"""
    from app.services import discipline_packs as dp
    from app.services.extraction.schemas import get_schema

    pack = dp.parse_pack(
        {
            "name": "nolabel",
            "title": "无标签包",
            "schemas": [
                {
                    "id": "method",
                    "prompt": "抽取：\n{fields_spec}\n只输出 JSON。",
                    "fields": [
                        {"name": "purpose", "kind": "text", "max_len": 200},
                        {"name": "mechanism", "kind": "text", "max_len": 200},
                        {"name": "widget", "kind": "text", "max_len": 200},
                    ],
                }
            ],
        },
        origin="t",
    )
    dp.register_pack(pack)
    field = next(f for f in get_schema("nolabel.method").fields if f.name == "widget")
    assert field.label is None
    from app.services.method_index import _declared_fields

    rendered = _declared_fields("nolabel.method", {"widget": "x"})
    assert rendered == [
        {"name": "widget", "label": "widget", "kind": "text", "value": "x"}
    ]


async def test_empty_fields_are_left_off_the_card(client):
    """没抽到的字段不进卡片：渲染一个空段落比不渲染更糟。"""
    from app.services.method_index import _declared_fields

    rendered = _declared_fields(
        "structural.method",
        {"purpose": "p", "mechanism": "", "structure": None, "actions": []},
    )
    assert [f["name"] for f in rendered] == ["purpose"]


async def test_a_card_whose_pack_was_uninstalled_still_renders(client):
    """包被卸载了，卡还在表上。读不出字段声明不该让整张卡消失。"""
    from app.services.method_index import _declared_fields

    rendered = _declared_fields("gone.method", {"purpose": "p", "structure": "s"})
    assert {f["name"] for f in rendered} == {"purpose", "structure"}
    assert all(f["label"] == f["name"] for f in rendered)


async def test_the_builtin_card_shape_is_unchanged(client):
    """内置五键保留：现有前端与用例按这个形状读。"""
    from app.core.db import get_sessionmaker

    library_id, _paper_id = await _library_with_two_cards(client, "cardf3@example.com")
    async with get_sessionmaker()() as session:
        cards = await method_index.list_methods(session, library_id)
    card = cards[0]
    assert {"purpose", "mechanism", "baseline", "dataset", "protocol"} <= set(card)
    assert card["schema_id"] == "structural.method"


# ---- 向量按卡分作用域（#772）----


async def _two_discipline_libraries(client):
    """一篇论文同时进两个库：一个声明结构工程、一个声明合成化学，各有自己的方法卡。

    这正是 #772 的场景——两个库对同一篇论文有两种读法。
    """
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary, LibraryPaper
    from app.models.paper import Paper
    from app.models.paper_extraction import PaperExtraction
    from tests.conftest import add_paper

    token = await register_and_login(client, email="vecscope@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_a, lib_a = await make_project_with_library(client, headers, name="vs-a")
    _project_b, lib_b = await make_project_with_library(client, headers, name="vs-b")

    async with get_sessionmaker()() as session:
        (await session.get(DirectionLibrary, lib_a)).discipline = "structural"
        (await session.get(DirectionLibrary, lib_b)).discipline = "synthesis"
        paper = await add_paper(
            session,
            project_id=_uuid.UUID(project_a),
            title="Shared across two fields",
            status="scored",
        )
        await session.flush()
        # 同一篇也进 B 库
        session.add(LibraryPaper(library_id=lib_b, paper_id=paper.id, status="scored"))
        session.add_all(
            [
                PaperExtraction(
                    paper_id=paper.id,
                    schema_id="structural.method",
                    payload={
                        "purpose": "resist blast loading on beams",
                        "mechanism": "explicit finite element simulation",
                    },
                ),
                PaperExtraction(
                    paper_id=paper.id,
                    schema_id="synthesis.method",
                    payload={
                        "purpose": "prepare a palladium single atom catalyst",
                        "mechanism": "solvothermal synthesis in ethylene glycol",
                    },
                ),
            ]
        )
        await session.commit()
        paper_id = paper.id

    # 两个库各自刷一次索引——改之前后刷的那次会覆盖先刷的
    for library_id in (lib_a, lib_b):
        async with get_sessionmaker()() as session:
            paper = await session.get(Paper, paper_id)
            await method_index.refresh_paper_method_index(
                session, paper, library_id=library_id
            )
            await session.commit()
    return lib_a, lib_b, paper_id


async def test_two_libraries_keep_their_own_reading_of_the_same_paper(client):
    """只按 (论文, 轴) 存的话，后刷的库覆盖先刷的：一份向量，两个库共用。"""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.vectors import MethodVector

    _lib_a, _lib_b, paper_id = await _two_discipline_libraries(client)
    async with get_sessionmaker()() as session:
        schema_ids = (
            (
                await session.execute(
                    select(MethodVector.schema_id).where(
                        MethodVector.paper_id == paper_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(schema_ids) == {"structural.method", "synthesis.method"}


async def test_each_library_scores_against_its_own_card(client):
    """显示与算分必须同源。取错向量不会报错，只是这一篇的排序由另一个领域的文本决定。"""
    from app.core.db import get_sessionmaker

    lib_a, lib_b, paper_id = await _two_discipline_libraries(client)

    async with get_sessionmaker()() as session:
        cards_a, _mode = await method_index.search_methods(
            session, lib_a, "blast loading on beams"
        )
        cards_b, _mode = await method_index.search_methods(
            session, lib_b, "palladium catalyst preparation"
        )
    assert [c["paper_id"] for c in cards_a] == [paper_id]
    assert [c["paper_id"] for c in cards_b] == [paper_id]
    # 各自显示本库那张卡
    assert cards_a[0]["schema_id"] == "structural.method"
    assert cards_b[0]["schema_id"] == "synthesis.method"
    # 各自的查询命中自己那张卡时分数更高——取错向量这条断言必然翻车
    assert cards_a[0]["similarity"] > 0
    assert cards_b[0]["similarity"] > 0


async def test_refreshing_one_library_does_not_wipe_the_others_vectors(client):
    """清理幽灵轴时按论文清的话，会把另一个库的读法一并抹掉，那个库下次检索少一篇论文。"""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.paper import Paper
    from app.models.paper_extraction import PaperExtraction
    from app.models.vectors import MethodVector

    lib_a, _lib_b, paper_id = await _two_discipline_libraries(client)

    # A 库这张卡重抽后把 mechanism 抽没了
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            select(PaperExtraction).where(
                PaperExtraction.paper_id == paper_id,
                PaperExtraction.schema_id == "structural.method",
            )
        )
        row.payload = {"purpose": "resist blast loading on beams"}
        await session.commit()

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await method_index.refresh_paper_method_index(session, paper, library_id=lib_a)
        await session.commit()

    async with get_sessionmaker()() as session:
        remaining = sorted(
            (
                await session.execute(
                    select(MethodVector.schema_id, MethodVector.axis).where(
                        MethodVector.paper_id == paper_id
                    )
                )
            ).all()
        )
    # A 的 mechanism 走了，B 的两根轴一根没少
    assert ("structural.method", "mechanism") not in remaining
    assert ("structural.method", "purpose") in remaining
    assert ("synthesis.method", "mechanism") in remaining
    assert ("synthesis.method", "purpose") in remaining
