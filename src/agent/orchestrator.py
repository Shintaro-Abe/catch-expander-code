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


def _load_prompt(name: str) -> str:
    """プロンプトファイルを読み込む"""
    return (PROMPTS_DIR / f"{name}.md").read_text()


def call_claude(prompt: str, allowed_tools: list[str] | None = None) -> str:
    """Claude Code CLIを呼び出し、結果を返す（リトライ付き）

    Args:
        prompt: CLIに渡すプロンプト
        allowed_tools: 許可するツールのリスト

    Returns:
        CLIのstdout出力（JSON文字列）
    """
    cmd = ["claude", "-p", prompt, "--model", "opus", "--output-format", "json"]
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(MAX_CLAUDE_RETRIES):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
            return result.stdout
        except subprocess.CalledProcessError as e:
            last_error = e
            wait = 2 ** (attempt + 1)
            logger.warning(
                "Claude CLI error, retrying",
                extra={
                    "attempt": attempt + 1,
                    "wait_seconds": wait,
                    "returncode": e.returncode,
                    "stderr": e.stderr[:500] if e.stderr else "",
                    "stdout": e.stdout[:500] if e.stdout else "",
                },
            )
            time.sleep(wait)

    if last_error:
        raise last_error
    msg = "Unexpected: no error and no response"
    raise RuntimeError(msg)


def _parse_claude_response(raw: str) -> dict:
    """Claude CLIのJSON応答をパースする"""
    data = json.loads(raw)
    # --output-format json の場合、result フィールドに応答本文が入る
    if isinstance(data, dict) and "result" in data:
        text = data["result"]
    elif isinstance(data, dict) and "content" in data:
        text = data["content"]
    else:
        text = raw

    # 応答テキストからJSONブロックを抽出
    if isinstance(text, str):
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        return json.loads(text)
    return text


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
        profile_text = json.dumps(profile, ensure_ascii=False) if profile else "プロファイル未登録"

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

        # 6. レビュアー起動 + レビューループ
        self.db.update_execution_status(execution_id, "reviewing")
        self.slack.post_progress(slack_channel, slack_thread_ts, "🔎 品質検証中...")

        reviewer_prompt = _load_prompt("reviewer")
        all_sources = []
        for r in research_results:
            all_sources.extend(r.get("sources", []))

        review_result = self._run_review_loop(
            reviewer_prompt, deliverables, all_sources, category, gen_prompt, slack_channel, slack_thread_ts
        )
        quality_metadata = review_result.get("quality_metadata", {})

        # 7. 格納処理
        self.db.update_execution_status(execution_id, "storing")
        self.slack.post_progress(slack_channel, slack_thread_ts, "💾 成果物を格納中...")

        # GitHub格納（コード成果物がある場合）
        github_url = None
        code_files = deliverables.get("code_files")
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

        notion_url = self.notion.create_page(
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

        self.notion.update_page_status(notion_url.split("-")[-1][:32], "完了")

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
                    f"カテゴリ: {category}\n"
                    f"ステップ: {step['step_name']}\n"
                    f"内容: {step['description']}\n"
                    f"検索ヒント: {json.dumps(step.get('search_hints', []), ensure_ascii=False)}"
                )
                raw = call_claude(prompt, allowed_tools=["WebSearch", "WebFetch"])
                result = _parse_claude_response(raw)
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
    ) -> dict:
        """レビューループを実行する（最大2回）"""
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
            raw = call_claude(review_prompt, allowed_tools=["WebFetch"])
            review_result = _parse_claude_response(raw)

            if review_result.get("passed", False):
                logger.info("Review passed", extra={"loop": loop})
                return review_result

            errors = [i for i in review_result.get("issues", []) if i.get("severity") == "error"]
            if not errors or loop >= MAX_REVIEW_LOOPS:
                # 上限到達: 残りの指摘事項を品質メタデータのnotesに記載
                if errors:
                    notes = review_result.get("quality_metadata", {}).get("notes", [])
                    notes.append(f"レビュー修正上限（{MAX_REVIEW_LOOPS}回）に到達。未修正の指摘: {len(errors)}件")
                    review_result.setdefault("quality_metadata", {})["notes"] = notes
                logger.info("Review loop limit reached", extra={"loop": loop, "remaining_errors": len(errors)})
                return review_result

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
            current_deliverables = _parse_claude_response(fix_raw)

        return review_result

    def _build_quality_metadata_block(self, metadata: dict) -> list[dict]:
        """品質メタデータをNotionブロック形式で構築する"""
        lines = ["■ 品質情報\n"]

        verified = metadata.get("sources_verified", 0)
        unverified = metadata.get("sources_unverified", 0)
        lines.append(f"検証ステータス: ✅ 出典検証済み: {verified}件")
        if unverified > 0:
            details = metadata.get("unverified_details", [])
            lines.append(f"  ⚠️ 未検証の記述: {unverified}件（{', '.join(details)}）")

        newest = metadata.get("newest_source_date", "N/A")
        oldest = metadata.get("oldest_source_date", "N/A")
        lines.append(f"\n情報の鮮度: 最新 {newest} / 最古 {oldest}")

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
