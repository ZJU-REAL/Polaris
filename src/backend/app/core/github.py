"""极简 GitHub REST 客户端：用 PAT 建 issue（用户反馈 → issue）。

不引 SDK，httpx 直连 api.github.com。token 为空时抛 GitHubNotConfigured，
上层据此禁用「建 issue」、只留草稿。GitHub 属外部服务，复用 outbound_proxy。
"""

import httpx

from app.core.config import get_settings


class GitHubNotConfigured(Exception):
    """未配置 POLARIS_GITHUB_TOKEN。"""


class GitHubError(Exception):
    """GitHub API 调用失败。"""


def github_enabled() -> bool:
    return bool(get_settings().github_token)


async def create_issue(
    *, title: str, body: str, labels: list[str] | None = None
) -> tuple[int, str]:
    """在 settings.github_repo 建 issue，返回 (number, html_url)。"""
    settings = get_settings()
    if not settings.github_token:
        raise GitHubNotConfigured("POLARIS_GITHUB_TOKEN not set")
    repo = settings.github_repo.strip().strip("/")
    url = f"https://api.github.com/repos/{repo}/issues"
    payload: dict[str, object] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    proxy = settings.outbound_proxy or None
    try:
        async with httpx.AsyncClient(timeout=20.0, proxy=proxy) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise GitHubError(f"request failed: {e}") from e
    if resp.status_code >= 300:
        raise GitHubError(f"github {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return int(data["number"]), str(data["html_url"])
