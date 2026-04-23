import concurrent.futures
import json
import logging
import subprocess
import time
from pathlib import Path

from notify.slack_client import SlackClient
from state.dynamodb_client import DynamoDbClient
from storage.github_client import GitHubClient
from storage.notion_client import NotionClient

logger = logging.getLogger("catch-expander-agent")

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_REVIEW_LOOPS = 2
MAX_CLAUDE_RETRIES = 3

# generator は text 成果物のみを返す契約のため、レビュー修正レスポンスを
# current_deliverables に代入すると iac_code/program_code 由来の code_files が失われる。
# 修正適用後に明示的に引き継ぐ独立生成フィールドの一覧。
_PRESERVED_DELIVERABLE_FIELDS = ("code_files",)


def _load_prompt(name: str) -> str:
    """プロンプトファイルを読み込む"""
    return (PROMPTS_DIR / f"{name}.md").read_text()


def _namespace_source_ids(result: dict, step_id: str) -> None:
    """リサーチャー結果の source_id を {step_id}:{source_id} 形式にリマップする（in-place）

    並列実行されるリサーチャーが独立に src-001.. を付番するため、統合時の重複を防ぐ。
    LLM が自身の step_id を誤って別値で返すケースも補正する。
    既に prefix が付与済みの場合は再付与しない（冪等性）。
    """
    if not isinstance(result, dict):
        return
    result["step_id"] = step_id
    sources = result.get("sources")
    if not isinstance(sources, list):
        return
    prefix = f"{step_id}:"
    for src in sources:
        if not isinstance(src, dict):
            continue
        original = src.get("source_id")
        if not isinstance(original, str) or not original:
            continue
        if original.startswith(prefix):
            continue
        src["source_id"] = f"{prefix}{original}"


def call_claude(prompt: str, allowed_tools: list[str] | None = None, model: str = "sonnet") -> str:
    """Claude Code CLIを呼び出し、結果を返す（リトライ付き）

    Args:
        prompt: CLIに渡すプロンプト
        allowed_tools: 許可するツールのリスト
        model: 使用するモデル名（デフォルト: sonnet）

    Returns:
        CLIのstdout出力（JSON文字列）
    """
    cmd = ["claude", "-p", "-", "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(MAX_CLAUDE_RETRIES):
        try:
            result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, check=True)  # noqa: S603
            return result.stdout
        except subprocess.CalledProcessError as e:
            last_error = e
            wait = 2 ** (attempt + 1)
            stderr_snippet = e.stderr[:500] if e.stderr else ""
            stdout_snippet = e.stdout[:500] if e.stdout else ""
            logger.warning(
                "Claude CLI error, retrying | rc=%s | stderr=%s | stdout=%s",
                e.returncode,
                stderr_snippet,
                stdout_snippet,
                extra={"attempt": attempt + 1, "wait_seconds": wait},
            )
            time.sleep(wait)

    if last_error:
        raise last_error
    msg = "Unexpected: no error and no response"
    raise RuntimeError(msg)


def _parse_claude_response(raw: str) -> dict:
    """Claude CLIのJSON応答をパースする"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"raw_text": raw}

    # --output-format json の場合、result フィールドに応答本文が入る
    if isinstance(data, dict) and "result" in data:
        text = data["result"]
    elif isinstance(data, dict) and "content" in data:
        text = data["content"]
    else:
        text = raw

    if not isinstance(text, str):
        return text

    # 応答テキストからJSONブロックを抽出（複数の戦略で試行）

    # 戦略1: ```json コードブロックから抽出
    if "```json" in text:
        candidate = text.split("```json", 1)[1].split("```", 1)[0].strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 戦略2: ``` コードブロックから抽出
    if "```" in text:
        candidate = text.split("```", 1)[1].split("```", 1)[0].strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 戦略3: テキスト全体を直接JSONパース
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 戦略4: テキスト内の有効なJSONオブジェクトをスキャン
    # raw_decode を使い、前置き文の後に来るJSONを検出する
    # 小さな（キー数2以下の）断片的なJSONオブジェクトは除外する
    decoder = json.JSONDecoder()
    for i, char in enumerate(text):
        if char == "{":
            try:
                obj, _ = decoder.raw_decode(text, i)
                if isinstance(obj, dict) and len(obj) >= 3:  # noqa: PLR2004
                    return obj
            except json.JSONDecodeError:
                continue

    logger.warning(
        "Failed to parse Claude response as JSON, returning as text",
        extra={"text_preview": text[:300]},
    )
    return {"raw_text": text, "parse_error": True}


_CODE_TYPE_LABELS = {
    "iac_code": "IaCコード（Terraform または CloudFormation）",
    "program_code": "プログラムコード（Python またはユーザープロファイルの技術スタック）",
}

_CODE_FAILURE_PREVIEW_LIMIT = 500
_CODE_FAILURE_TOP_KEYS_LIMIT = 10

_FILE_EXTENSIONS = {
    ".tf", ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".txt", ".md",
    ".yaml", ".yml", ".go", ".rs", ".java", ".toml", ".sh", ".env",
    ".cfg", ".ini", ".html", ".css", ".rb", ".kt", ".swift",
}
_FILENAME_EXACT = {"Dockerfile", "Makefile", "Procfile", ".gitignore", ".env.example"}
_RESERVED_META_KEYS = {"readme_content", "summary", "parse_error", "raw_text", "files"}
_FILE_KEY_RATIO_THRESHOLD = 0.8
_FILE_KEY_MIN_COUNT = 2


def _looks_like_file_path(key: str) -> bool:
    """キーがファイルパスらしいか判定する（`/` 含む or 既知拡張子 or 既知ファイル名）"""
    if not isinstance(key, str) or not key:
        return False
    if "/" in key:
        return True
    name = key.rsplit("/", 1)[-1]
    if name in _FILENAME_EXACT:
        return True
    if "." not in name:
        return False
    suffix = "." + name.rsplit(".", 1)[-1]
    return suffix in _FILE_EXTENSIONS


def _normalize_code_files_payload(parsed: object) -> object:
    """コード生成応答が files キーをラップせず、ファイルパスを直接トップレベルに置いた場合に正規化する。

    Phase A 観測（exec-20260418073748-2c27bb2f）で iac_code / program_code 共に
    `{"bin/app.ts": "...", "package.json": "..."}` 形式で返ったケースを救済する。
    既に `{"files": {...}}` 形式 / dict でない / parse_error の場合は無変更。
    """
    if not isinstance(parsed, dict):
        return parsed
    if "files" in parsed:
        return parsed
    if parsed.get("parse_error"):
        return parsed

    file_keys: list[str] = []
    other_keys: list[str] = []
    for key, value in parsed.items():
        if key in _RESERVED_META_KEYS:
            continue
        if _looks_like_file_path(key) and isinstance(value, str):
            file_keys.append(key)
        else:
            other_keys.append(key)

    total_non_meta = len(file_keys) + len(other_keys)
    if len(file_keys) < _FILE_KEY_MIN_COUNT or total_non_meta == 0:
        return parsed
    if len(file_keys) / total_non_meta < _FILE_KEY_RATIO_THRESHOLD:
        return parsed

    normalized: dict = {"files": {key: parsed[key] for key in file_keys}}
    readme = parsed.get("readme_content")
    if isinstance(readme, str):
        normalized["readme_content"] = readme
    return normalized


def _build_code_failure_diagnostics(raw: str, parsed: object) -> dict:
    """コード生成失敗時の診断情報を組み立てる。

    ロガーの formatter が extra を出力しないため、警告メッセージ本文に
    埋め込む形で root cause 判定に必要な観測項目を返す。
    """
    raw_text = raw if isinstance(raw, str) else ""
    response_chars = len(raw_text)
    response_preview = raw_text[:_CODE_FAILURE_PREVIEW_LIMIT]

    if not isinstance(parsed, dict):
        return {
            "parse_error": True,
            "files_kind": "<not-dict>",
            "files_count": 0,
            "top_level_keys": [],
            "response_chars": response_chars,
            "response_preview": response_preview,
        }

    parse_error = bool(parsed.get("parse_error", False))
    top_level_keys = list(parsed.keys())[:_CODE_FAILURE_TOP_KEYS_LIMIT]

    if "files" not in parsed:
        files_kind = "missing"
        files_count = 0
    else:
        files_value = parsed["files"]
        files_kind = type(files_value).__name__
        if isinstance(files_value, (dict, list)):
            files_count = len(files_value)
        else:
            files_count = 0

    return {
        "parse_error": parse_error,
        "files_kind": files_kind,
        "files_count": files_count,
        "top_level_keys": top_level_keys,
        "response_chars": response_chars,
        "response_preview": response_preview,
    }


def _build_code_generation_prompt(
    topic: str,
    category: str,
    research_results: list[dict],
    profile_text: str,
    code_type: str,
) -> str:
    """単一タイプのコード成果物生成専用プロンプトを構築する

    コード生成をテキスト成果物生成から、さらにタイプごとに分離することで、
    応答サイズ超過によるJSON解析失敗を防ぐ。
    """
    requested_type = _CODE_TYPE_LABELS.get(code_type, code_type)

    # 調査サマリーのみ抽出（ソース一覧は省略してプロンプトサイズを抑制）
    research_summary = "\n\n".join(
        f"### {r.get('step_id', 'unknown')}\n{r.get('summary', '')}"
        for r in research_results
        if not r.get("error") and not r.get("parse_error") and r.get("summary")
    )
    if not research_summary:
        research_summary = "（調査結果なし）"

    return (
        "# コード成果物生成\n\n"
        "## 依頼\n\n"
        "以下のトピックに関するコード成果物のみを生成してください。\n"
        "テキストコンテンツ（調査レポート等）は不要です。コードファイルのみを出力してください。\n\n"
        f"トピック: {topic}\n"
        f"カテゴリ: {category}\n"
        f"ユーザープロファイル:\n{profile_text}\n\n"
        "## 生成するコード種別\n\n"
        f"- {requested_type}\n\n"
        "このプロンプトでは上記 **1種類のみ** を生成してください。\n\n"
        "## 調査結果サマリー\n\n"
        f"{research_summary}\n\n"
        "## 出力形式\n\n"
        "**重要**: 前置き文・説明文は不要です。以下のJSON形式のみを ` ```json ` ブロックで出力してください。\n\n"
        "```json\n"
        "{\n"
        '  "files": {\n'
        '    "ファイルパス": "ファイル内容（コメント付き）"\n'
        "  },\n"
        '  "readme_content": "README.md本文"\n'
        "}\n"
        "```\n\n"
        "## 制約\n\n"
        "- **PoC品質であることを各ファイルの冒頭コメントで明示**すること\n"
        "- ハードコードされたシークレット・認証情報を含めない\n"
        "- プロファイルがない場合はAWS + Python（またはTerraform）を標準として生成\n"
        "- コードは機能的なスケルトン（実装の骨格）として提供。詳細な業務ロジックは省略可\n"
        "- ファイル数は最大5ファイルに抑える（main, variables, outputs, README等）\n"
    )


class Orchestrator:
    """マルチエージェントワークフローを制御するオーケストレーター"""

    def __init__(
        self,
        slack_client: SlackClient,
        db_client: DynamoDbClient,
        notion_token: str,
        notion_database_id: str,
        github_token: str,
        github_repo: str,
    ) -> None:
        self.slack = slack_client
        self.db = db_client
        self.notion = NotionClient(notion_token, notion_database_id)
        self.github = GitHubClient(github_token, github_repo)

    def run(
        self,
        execution_id: str,
        user_id: str,
        topic: str,
        slack_channel: str,
        slack_thread_ts: str,
    ) -> None:
        """ワークフローを実行する"""
        logger.info("Orchestrator started", extra={"execution_id": execution_id, "topic": topic})

        # 1. プロファイル取得
        profile = self.db.get_user_profile(user_id) or {}
        profile_text_base = json.dumps(profile, ensure_ascii=False) if profile else "プロファイル未登録"
        learned_prefs = profile.get("learned_preferences", [])
        if learned_prefs:
            prefs_lines = "\n".join(f"- {p['text']}" for p in learned_prefs)
            profile_text = (
                f"{profile_text_base}\n\n"
                "## ユーザーの蓄積された好み（学習済み）\n"
                "以下の好みを成果物の生成方針に必ず反映してください：\n"
                f"{prefs_lines}"
            )
        else:
            profile_text = profile_text_base

        # 2. トピック解析
        orchestrator_prompt = _load_prompt("orchestrator")
        analysis_prompt = (
            f"{orchestrator_prompt}\n\n"
            f"## トピック解析を実行してください\n\n"
            f"トピック: {topic}\n\n"
            f"ユーザープロファイル:\n{profile_text}"
        )
        analysis_raw = call_claude(analysis_prompt)
        analysis = _parse_claude_response(analysis_raw)
        logger.info("Topic analyzed", extra={"execution_id": execution_id, "category": analysis.get("category")})

        self.db.update_execution_status(execution_id, "planning")

        # 3. ワークフロー設計
        wf_prompt = (
            f"{orchestrator_prompt}\n\n"
            f"## ワークフロー設計を実行してください\n\n"
            f"トピック解析結果:\n```json\n{json.dumps(analysis, ensure_ascii=False)}\n```\n\n"
            f"ユーザープロファイル:\n{profile_text}"
        )
        wf_raw = call_claude(wf_prompt)
        workflow = _parse_claude_response(wf_raw)
        logger.info(
            "Workflow designed",
            extra={
                "execution_id": execution_id,
                "research_steps": len(workflow.get("research_steps", [])),
                "generate_steps": [s.get("deliverable_type") for s in workflow.get("generate_steps", [])],
                "storage_targets": workflow.get("storage_targets", ["notion"]),
            },
        )

        # ワークフロー計画をSlack通知
        research_names = [s["step_name"] for s in workflow.get("research_steps", [])]
        generate_names = [s["step_name"] for s in workflow.get("generate_steps", [])]
        storage_targets = workflow.get("storage_targets", ["notion"])
        plan_text = (
            f"📋 以下の計画で進めます：\n"
            f"  調査 [{', '.join(research_names)}]\n"
            f"  成果物 [{', '.join(generate_names)}]\n"
            f"  格納先 [{' + '.join(t.capitalize() for t in storage_targets)}]"
        )
        self.slack.post_progress(slack_channel, slack_thread_ts, plan_text)

        # ワークフロー計画をDynamoDBに保存
        self.db._table("workflow-executions").update_item(
            Key={"execution_id": execution_id},
            UpdateExpression=(
                "SET workflow_plan = :plan, category = :cat, intent = :intent,"
                " perspectives = :persp, deliverable_types = :dtypes"
            ),
            ExpressionAttributeValues={
                ":plan": workflow,
                ":cat": analysis.get("category", ""),
                ":intent": analysis.get("intent", ""),
                ":persp": analysis.get("perspectives", []),
                ":dtypes": analysis.get("deliverable_types", []),
            },
        )

        self.db.update_execution_status(execution_id, "researching")

        # 4. リサーチャー並列実行
        researcher_prompt = _load_prompt("researcher")
        research_steps = workflow.get("research_steps", [])
        category = analysis.get("category", "技術")

        research_results = self._run_researchers(
            execution_id, research_steps, researcher_prompt, category, slack_channel, slack_thread_ts
        )

        self.slack.post_progress(slack_channel, slack_thread_ts, "🔍 調査が完了しました")

        # 5. ジェネレーター起動
        self.db.update_execution_status(execution_id, "generating")
        self.slack.post_progress(slack_channel, slack_thread_ts, "📝 成果物を生成中...")

        generator_prompt = _load_prompt("generator")
        combined_research = json.dumps(research_results, ensure_ascii=False)
        gen_prompt = (
            f"{generator_prompt}\n\n"
            f"## 成果物を生成してください\n\n"
            f"トピック: {topic}\n"
            f"カテゴリ: {category}\n\n"
            f"ワークフロー計画:\n```json\n{json.dumps(workflow, ensure_ascii=False)}\n```\n\n"
            f"調査結果:\n```json\n{combined_research}\n```\n\n"
            f"ユーザープロファイル:\n{profile_text}"
        )
        gen_raw = call_claude(gen_prompt)
        deliverables = _parse_claude_response(gen_raw)
        # ジェネレーターは text 成果物のみを返す。code_files は常に独立生成する
        deliverables.pop("code_files", None)

        # 5b. コード成果物のタイプ別独立生成
        # テキスト成果物と分離し、さらに iac_code / program_code ごとに個別の Claude 呼び出しに分割して
        # 応答サイズ超過による JSON 解析失敗を防ぐ
        generate_step_types = [s.get("deliverable_type") for s in workflow.get("generate_steps", [])]
        code_types = [t for t in generate_step_types if t in ("iac_code", "program_code")]
        if code_types and "github" in storage_targets:
            self.slack.post_progress(slack_channel, slack_thread_ts, "⚙️ コード成果物を生成中...")
            code_files_merged: dict[str, str] = {}
            readme_parts: list[str] = []
            for code_type in code_types:
                logger.info(
                    "Generating code files for type",
                    extra={"execution_id": execution_id, "code_type": code_type},
                )
                code_raw = call_claude(
                    _build_code_generation_prompt(topic, category, research_results, profile_text, code_type)
                )
                code_result = _parse_claude_response(code_raw)
                code_result = _normalize_code_files_payload(code_result)
                files = code_result.get("files") if isinstance(code_result, dict) else None
                if isinstance(files, dict) and files:
                    code_files_merged.update(files)
                    readme = code_result.get("readme_content") if isinstance(code_result, dict) else None
                    if isinstance(readme, str) and readme.strip():
                        label = _CODE_TYPE_LABELS.get(code_type, code_type)
                        readme_parts.append(f"## {label}\n\n{readme}")
                    logger.info(
                        "Code files generated",
                        extra={
                            "execution_id": execution_id,
                            "code_type": code_type,
                            "files_count": len(files),
                        },
                    )
                else:
                    diag = _build_code_failure_diagnostics(code_raw, code_result)
                    logger.warning(
                        "Code generation failed for type | execution_id=%s code_type=%s "
                        "parse_error=%s files_kind=%s files_count=%s top_level_keys=%s "
                        "response_chars=%d response_preview=%r",
                        execution_id,
                        code_type,
                        diag["parse_error"],
                        diag["files_kind"],
                        diag["files_count"],
                        diag["top_level_keys"],
                        diag["response_chars"],
                        diag["response_preview"],
                    )
            if code_files_merged:
                deliverables["code_files"] = {
                    "files": code_files_merged,
                    "readme_content": "\n\n".join(readme_parts) if readme_parts else "",
                }

        # 6. レビュアー起動 + レビューループ
        self.db.update_execution_status(execution_id, "reviewing")
        self.slack.post_progress(slack_channel, slack_thread_ts, "🔎 品質検証中...")

        reviewer_prompt = _load_prompt("reviewer")
        all_sources = []
        for r in research_results:
            all_sources.extend(r.get("sources", []))

        review_result, deliverables = self._run_review_loop(
            reviewer_prompt, deliverables, all_sources, category, gen_prompt, slack_channel, slack_thread_ts
        )
        quality_metadata = review_result.get("quality_metadata", {})

        # 7. 格納処理
        self.db.update_execution_status(execution_id, "storing")
        self.slack.post_progress(slack_channel, slack_thread_ts, "💾 成果物を格納中...")

        # GitHub格納（コード成果物がある場合）
        github_url = None
        code_files = deliverables.get("code_files")
        logger.info(
            "Storage decision",
            extra={
                "execution_id": execution_id,
                "storage_targets": storage_targets,
                "has_code_files": bool(code_files),
                "github_triggered": bool(code_files and "github" in storage_targets),
                "deliverables_parse_error": deliverables.get("parse_error", False),
            },
        )
        if code_files and "github" in storage_targets:
            import datetime

            dir_name = f"{topic.replace(' ', '-').lower()}-{datetime.date.today().strftime('%Y%m%d')}"
            github_url = self.github.push_files(dir_name, code_files.get("files", {}))
            notion_url_placeholder = ""  # Notionページ作成後に更新
            self.github.create_readme(dir_name, code_files.get("readme_content", ""), notion_url_placeholder)

        # Notion格納
        content_blocks = deliverables.get("content_blocks", [])
        # 品質メタデータブロックを追加
        quality_block = self._build_quality_metadata_block(quality_metadata)
        content_blocks.extend(quality_block)

        notion_url, notion_page_id = self.notion.create_page(
            title=topic,
            category=category,
            content_blocks=content_blocks,
            github_url=github_url,
            slack_user=user_id,
        )

        # GitHubのREADMEをNotionリンクで更新
        if github_url and code_files:
            import datetime

            dir_name = f"{topic.replace(' ', '-').lower()}-{datetime.date.today().strftime('%Y%m%d')}"
            self.github.create_readme(dir_name, code_files.get("readme_content", ""), notion_url)

        self.notion.update_page_status(notion_page_id, "完了")

        # 成果物レコードをDynamoDBに保存
        self.db.put_deliverable(
            {
                "execution_id": execution_id,
                "deliverable_id": f"dlv-{execution_id}",
                "type": "all",
                "storage": "notion" if not github_url else "notion+github",
                "external_url": notion_url,
                "quality_metadata": quality_metadata,
            }
        )

        # 出典をDynamoDBに保存
        if all_sources:
            self.db.put_sources(execution_id, all_sources)

        # 8. 完了通知
        summary = deliverables.get("summary", f"{topic}の成果物が完成しました。")
        self.slack.post_completion(slack_channel, slack_thread_ts, summary, notion_url, github_url)

        logger.info("Workflow completed", extra={"execution_id": execution_id, "notion_url": notion_url})

    def _run_researchers(
        self,
        execution_id: str,
        steps: list[dict],
        researcher_prompt: str,
        category: str,
        slack_channel: str,
        slack_thread_ts: str,
    ) -> list[dict]:
        """リサーチャーエージェントを並列実行する"""
        if not steps:
            return []

        # ステップレコードをDynamoDBに登録
        for step in steps:
            self.db.put_step(
                {
                    "execution_id": execution_id,
                    "step_id": step["step_id"],
                    "phase": "research",
                    "step_name": step["step_name"],
                    "step_order": steps.index(step) + 1,
                    "status": "pending",
                }
            )

        def _execute_research(step: dict) -> dict:
            step_id = step["step_id"]
            self.db.update_step_status(execution_id, step_id, "running")
            try:
                prompt = (
                    f"{researcher_prompt}\n\n"
                    f"## 調査指示\n\n"
                    f"あなたのステップID: {step_id}\n"
                    f"カテゴリ: {category}\n"
                    f"ステップ: {step['step_name']}\n"
                    f"内容: {step['description']}\n"
                    f"検索ヒント: {json.dumps(step.get('search_hints', []), ensure_ascii=False)}"
                )
                raw = call_claude(prompt, allowed_tools=["WebSearch", "WebFetch"])
                result = _parse_claude_response(raw)
                _namespace_source_ids(result, step_id)
                self.db.update_step_status(execution_id, step_id, "completed", result)
                return result
            except Exception as e:
                logger.exception("Research step failed", extra={"execution_id": execution_id, "step_id": step_id})
                self.db.update_step_status(execution_id, step_id, "failed")
                return {"step_id": step_id, "error": str(e), "summary": "", "sources": []}

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(steps)) as executor:
            futures = {executor.submit(_execute_research, step): step for step in steps}
            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                step = futures[future]
                result = future.result()
                results.append(result)
                if not result.get("error"):
                    self.slack.post_progress(
                        slack_channel,
                        slack_thread_ts,
                        f"🔍 {step['step_name']}の調査が完了しました（{i + 1}/{len(steps)}）",
                    )

        # 部分失敗のチェック
        failed = [r for r in results if r.get("error")]
        successful = [r for r in results if not r.get("error")]
        if not successful:
            msg = "All research steps failed"
            raise RuntimeError(msg)
        if failed:
            logger.warning(
                "Some research steps failed",
                extra={"execution_id": execution_id, "failed_count": len(failed)},
            )

        return results

    def _run_review_loop(
        self,
        reviewer_prompt: str,
        deliverables: dict,
        sources: list[dict],
        category: str,
        gen_prompt: str,
        slack_channel: str,
        slack_thread_ts: str,
    ) -> tuple[dict, dict]:
        """レビューループを実行する（最大2回）

        Returns:
            (review_result, final_deliverables) — 修正が適用された場合は最終版成果物を返す
        """
        current_deliverables = deliverables
        sources_text = json.dumps(sources, ensure_ascii=False)

        for loop in range(MAX_REVIEW_LOOPS + 1):
            review_prompt = (
                f"{reviewer_prompt}\n\n"
                f"## レビュー対象\n\n"
                f"カテゴリ: {category}\n\n"
                f"成果物:\n```json\n{json.dumps(current_deliverables, ensure_ascii=False)}\n```\n\n"
                f"出典リスト:\n```json\n{sources_text}\n```"
            )
            raw = call_claude(review_prompt, allowed_tools=["WebFetch"], model="opus")
            review_result = _parse_claude_response(raw)

            if review_result.get("passed", False):
                logger.info("Review passed", extra={"loop": loop})
                return review_result, current_deliverables

            errors = [i for i in review_result.get("issues", []) if i.get("severity") == "error"]
            if not errors or loop >= MAX_REVIEW_LOOPS:
                # 上限到達: 残りの指摘事項を品質メタデータのnotesに記載
                if errors:
                    notes = review_result.get("quality_metadata", {}).get("notes", [])
                    notes.append(f"レビュー修正上限（{MAX_REVIEW_LOOPS}回）に到達。未修正の指摘: {len(errors)}件")
                    review_result.setdefault("quality_metadata", {})["notes"] = notes
                logger.info("Review loop limit reached", extra={"loop": loop, "remaining_errors": len(errors)})
                return review_result, current_deliverables

            # 修正指示でジェネレーターを再実行
            self.slack.post_progress(
                slack_channel, slack_thread_ts, f"🔄 レビュー指摘に基づき修正中...（{loop + 1}/{MAX_REVIEW_LOOPS}）"
            )
            fix_instructions = json.dumps(review_result.get("issues", []), ensure_ascii=False)
            fix_prompt = (
                f"{gen_prompt}\n\n"
                f"## 修正指示\n\n"
                f"以下のレビュー指摘に基づき、成果物を修正してください。\n\n"
                f"指摘事項:\n```json\n{fix_instructions}\n```\n\n"
                f"現在の成果物:\n```json\n{json.dumps(current_deliverables, ensure_ascii=False)}\n```"
            )
            fix_raw = call_claude(fix_prompt)
            parsed = _parse_claude_response(fix_raw)
            if parsed.get("parse_error"):
                logger.warning(
                    "Fix attempt produced unparseable response, keeping previous deliverables",
                    extra={"loop": loop, "issues_count": len(errors)},
                )
            else:
                preserved = {
                    k: current_deliverables[k]
                    for k in _PRESERVED_DELIVERABLE_FIELDS
                    if k in current_deliverables
                }
                current_deliverables = parsed
                current_deliverables.update(preserved)
                logger.info(
                    "Deliverables updated by review fix",
                    extra={
                        "loop": loop,
                        "issues_count": len(errors),
                        "preserved_fields": list(preserved.keys()),
                    },
                )

        return review_result, current_deliverables

    @staticmethod
    def _format_freshness_line(metadata: dict) -> str:
        """情報の鮮度行を組み立てる。日付値が無い場合は注意書きにフォールバックする。

        reviewer 側で `published_at` が "unknown" / "continuously-updated" の出典は
        newest/oldest 集計から除外され null になる前提。表示側でも null と
        非日付文字列を「日付情報なし」として同じ表記に寄せる。
        """
        newest = metadata.get("newest_source_date")
        oldest = metadata.get("oldest_source_date")
        non_date = {None, "", "unknown", "continuously-updated", "N/A"}
        newest_valid = newest not in non_date
        oldest_valid = oldest not in non_date
        if not newest_valid and not oldest_valid:
            return "情報の鮮度: 取得日不明のソースが含まれます"
        newest_text = newest if newest_valid else "不明"
        oldest_text = oldest if oldest_valid else "不明"
        return f"情報の鮮度: 最新 {newest_text} / 最古 {oldest_text}"

    def _build_quality_metadata_block(self, metadata: dict) -> list[dict]:
        """品質メタデータをNotionブロック形式で構築する"""
        lines = ["■ 品質情報\n"]

        verified = metadata.get("sources_verified", 0)
        unverified = metadata.get("sources_unverified", 0)
        # sources_total は reviewer 側で常時併記される想定。互換性のため未指定時は verified を分母にフォールバック
        total = metadata.get("sources_total")
        if isinstance(total, int) and total >= verified:
            lines.append(f"検証ステータス: ✅ 出典検証済み: {verified}/{total} 件")
        else:
            lines.append(f"検証ステータス: ✅ 出典検証済み: {verified}件")
        if unverified > 0:
            details = metadata.get("unverified_details", [])
            lines.append(f"  ⚠️ 未検証の記述: {unverified}件（{', '.join(details)}）")

        lines.append(f"\n{self._format_freshness_line(metadata)}")

        passed = metadata.get("checklist_passed", 0)
        total = metadata.get("checklist_total", 0)
        lines.append(f"\nセルフレビュー結果: チェック項目 {passed}/{total} 合格")

        notes = metadata.get("notes", [])
        if notes:
            lines.append("\n注意事項:")
            for note in notes:
                lines.append(f"  - {note}")

        return [
            {"type": "divider", "divider": {}},
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": "\n".join(lines)}}]},
            },
        ]
