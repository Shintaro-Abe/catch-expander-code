import base64
import contextlib
import logging
import time

import requests

# T1-2b (Tier 1.2): API 呼び出し成否 + rate limit emit。詳細は notion_client.py 参照。
try:
    from src.observability import emit_api_call_completed, emit_rate_limit_hit
except ImportError:  # pragma: no cover

    def emit_api_call_completed(emitter, **kwargs) -> None:  # type: ignore[no-redef]
        return None

    def emit_rate_limit_hit(emitter, **kwargs) -> None:  # type: ignore[no-redef]
        return None


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
        # T1-2b: orchestrator が後から self._emitter を代入。未代入時は graceful skip。
        self._emitter = None

    def _request_with_retry(self, method: str, url: str, json_data: dict | None = None) -> dict:
        """リトライ付きでGitHub APIリクエストを送信する"""
        # T1-2b: 終端で api_call_completed、429 は rate_limit_hit を追加 emit。
        start_ns = time.monotonic_ns()
        success = False
        final_status: int | None = None
        endpoint_path = url.replace(GITHUB_API_BASE, "") or "/"
        try:
            last_error: requests.HTTPError | None = None
            for attempt in range(MAX_RETRIES):
                try:
                    response = requests.request(method, url, headers=self.headers, json=json_data, timeout=30)
                    response.raise_for_status()
                    success = True
                    final_status = response.status_code
                    return response.json()
                except requests.HTTPError as e:
                    last_error = e
                    final_status = e.response.status_code
                    if e.response.status_code == 429:
                        retry_after_raw = e.response.headers.get("Retry-After")
                        retry_after_seconds: int | None = None
                        if retry_after_raw is not None:
                            with contextlib.suppress(ValueError):
                                retry_after_seconds = int(retry_after_raw)
                        emit_rate_limit_hit(
                            self._emitter,
                            subtype="github_429",
                            endpoint_path=endpoint_path,
                            retry_after_seconds=retry_after_seconds,
                        )
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
        finally:
            duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            emit_api_call_completed(
                self._emitter,
                subtype="github",
                success=success,
                duration_ms=duration_ms,
                response_status_code=final_status,
                endpoint_path=endpoint_path,
            )

    def _put_file(self, path: str, content: str, message: str) -> dict:
        """リポジトリにファイルを作成・更新する"""
        url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
        }

        # 既存ファイルの場合はSHAが必要 (新規なら 404 が返る)。
        # T1-2b (Codex P2 対応): この preflight GET も api_call_completed として観測する。
        # 200 / 404 は API としての正常応答とみなし success=True、4xx>=400 で 404 以外と 5xx は success=False。
        # 429 検出時は rate_limit_hit を追加 emit。
        endpoint_path = url.replace(GITHUB_API_BASE, "") or "/"
        get_start_ns = time.monotonic_ns()
        get_success = False
        get_final_status: int | None = None
        try:
            existing = requests.get(url, headers=self.headers, timeout=30)
            get_final_status = existing.status_code
            get_success = existing.status_code in (200, 404)
            if existing.status_code == 200:
                payload["sha"] = existing.json()["sha"]
            if existing.status_code == 429:
                retry_after_raw = existing.headers.get("Retry-After")
                retry_after_seconds: int | None = None
                if retry_after_raw is not None:
                    with contextlib.suppress(ValueError):
                        retry_after_seconds = int(retry_after_raw)
                emit_rate_limit_hit(
                    self._emitter,
                    subtype="github_429",
                    endpoint_path=endpoint_path,
                    retry_after_seconds=retry_after_seconds,
                )
        except requests.RequestException:
            # ネットワーク等の失敗。GET 失敗時は新規ファイル扱いで PUT 続行 (既存挙動維持)。
            get_success = False
        finally:
            get_duration_ms = (time.monotonic_ns() - get_start_ns) // 1_000_000
            emit_api_call_completed(
                self._emitter,
                subtype="github",
                success=get_success,
                duration_ms=get_duration_ms,
                response_status_code=get_final_status,
                endpoint_path=endpoint_path,
            )

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
