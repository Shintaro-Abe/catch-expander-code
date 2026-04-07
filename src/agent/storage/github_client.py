import base64
import logging
import time

import requests

logger = logging.getLogger("catch-expander-agent")

GITHUB_API_BASE = "https://api.github.com"
MAX_RETRIES = 3


class GitHubClient:
    """GitHub API操作クライアント"""

    def __init__(self, token: str, repo: str) -> None:
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request_with_retry(self, method: str, url: str, json_data: dict | None = None) -> dict:
        """リトライ付きでGitHub APIリクエストを送信する"""
        last_error: requests.HTTPError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.request(method, url, headers=self.headers, json=json_data, timeout=30)
                response.raise_for_status()
                return response.json()
            except requests.HTTPError as e:
                last_error = e
                wait = 2**attempt
                logger.warning(
                    "GitHub API error, retrying",
                    extra={"attempt": attempt + 1, "wait_seconds": wait, "status": e.response.status_code},
                )
                time.sleep(wait)
        if last_error:
            raise last_error
        msg = "Unexpected: no error and no response"
        raise RuntimeError(msg)

    def _put_file(self, path: str, content: str, message: str) -> dict:
        """リポジトリにファイルを作成・更新する"""
        url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
        }

        # 既存ファイルの場合はSHAが必要
        try:
            existing = requests.get(url, headers=self.headers, timeout=30)
            if existing.status_code == 200:
                payload["sha"] = existing.json()["sha"]
        except requests.RequestException:
            pass

        return self._request_with_retry("PUT", url, payload)

    def push_files(self, directory_name: str, files: dict[str, str]) -> str:
        """ファイル群をリポジトリにpushし、ディレクトリURLを返す

        Args:
            directory_name: ディレクトリ名（例: ai-pipeline-20260404）
            files: {ファイルパス: ファイル内容} の辞書

        Returns:
            GitHubディレクトリのURL
        """
        for file_path, content in files.items():
            full_path = f"{directory_name}/{file_path}"
            self._put_file(full_path, content, f"Add {full_path}")
            logger.info("File pushed to GitHub", extra={"path": full_path})

        return f"https://github.com/{self.repo}/tree/main/{directory_name}"

    def create_readme(self, directory_name: str, content: str, notion_url: str) -> None:
        """README.mdを作成する（Notionページへのリンク含む）

        Args:
            directory_name: ディレクトリ名
            content: README本文
            notion_url: NotionページのURL
        """
        readme_content = f"{content}\n\n---\n\n📝 [Notionで詳細を見る]({notion_url})\n"
        self._put_file(f"{directory_name}/README.md", readme_content, f"Add README for {directory_name}")
