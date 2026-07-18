"""文件管理器增强：新建文件夹 / 上传文本与二进制 / raw 下载 / 文件夹级联删除。"""

from tests.test_manuscripts import _create_manuscript, _setup_project

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)


async def _new_ms(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    return headers, resp.json()["id"]


async def test_create_folder_and_upload_text(client):
    headers, ms_id = await _new_ms(client)

    resp = await client.post(
        f"/api/manuscripts/{ms_id}/folders", json={"path": "sections"}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["is_folder"] is True

    # 上传文本文件到子目录（可编辑）
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/files/upload",
        files={"file": ("intro.tex", b"\\section{Intro}\n", "text/plain")},
        data={"path": "sections/intro.tex"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    meta = resp.json()
    assert meta["is_binary"] is False
    assert meta["readonly"] is False
    assert meta["path"] == "sections/intro.tex"
    # 文本内容可读回
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    fid = next(f["id"] for f in detail["files"] if f["path"] == "sections/intro.tex")
    content = (await client.get(f"/api/manuscripts/{ms_id}/files/{fid}", headers=headers)).json()[
        "content"
    ]
    assert "\\section{Intro}" in content


async def test_upload_binary_and_raw_download(client):
    headers, ms_id = await _new_ms(client)
    # 注意 figures/ 是保留前缀（编译自动填实验图），用户图片放别处，如 img/
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/files/upload",
        files={"file": ("logo.png", _PNG, "image/png")},
        data={"path": "img/logo.png"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    meta = resp.json()
    assert meta["is_binary"] is True
    assert meta["readonly"] is True
    assert meta["size"] == len(_PNG)
    fid = meta["id"]

    # raw 下载拿回原始字节
    resp = await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/raw", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == _PNG

    # 文本读取端点对二进制无意义，但删除应清掉磁盘字节
    resp = await client.delete(f"/api/manuscripts/{ms_id}/files/{fid}", headers=headers)
    assert resp.status_code == 204
    resp = await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/raw", headers=headers)
    assert resp.status_code == 404


async def test_delete_folder_cascades(client):
    headers, ms_id = await _new_ms(client)
    await client.post(f"/api/manuscripts/{ms_id}/folders", json={"path": "assets"}, headers=headers)
    await client.post(
        f"/api/manuscripts/{ms_id}/files/upload",
        files={"file": ("a.png", _PNG, "image/png")},
        data={"path": "assets/a.png"},
        headers=headers,
    )
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    folder_id = next(f["id"] for f in detail["files"] if f["path"] == "assets")

    resp = await client.delete(f"/api/manuscripts/{ms_id}/files/{folder_id}", headers=headers)
    assert resp.status_code == 204
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    paths = {f["path"] for f in detail["files"]}
    assert "assets" not in paths and "assets/a.png" not in paths  # 级联删除
