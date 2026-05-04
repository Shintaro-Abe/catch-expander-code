import logging
from datetime import UTC, datetime

import boto3

logger = logging.getLogger("catch-expander-agent")

TTL_DAYS = 90


class DynamoDbClient:
    """DynamoDB状態管理クライアント"""

    def __init__(self, table_prefix: str) -> None:
        self.dynamodb = boto3.resource("dynamodb")
        self.table_prefix = table_prefix

    def _table(self, name: str) -> "boto3.dynamodb.Table":
        return self.dynamodb.Table(f"{self.table_prefix}-{name}")

    def _ttl_timestamp(self) -> int:
        """現在時刻からTTL_DAYS日後のUNIXタイムスタンプを返す"""
        return int(datetime.now(tz=UTC).timestamp()) + TTL_DAYS * 86400

    # ============================
    # ユーザープロファイル
    # ============================
    def get_user_profile(self, user_id: str) -> dict | None:
        """ユーザープロファイルを取得する"""
        response = self._table("user-profiles").get_item(Key={"user_id": user_id})
        return response.get("Item")

    def put_user_profile(self, profile: dict) -> None:
        """ユーザープロファイルを保存する"""
        now = datetime.now(tz=UTC).isoformat()
        profile.setdefault("created_at", now)
        profile["updated_at"] = now
        self._table("user-profiles").put_item(Item=profile)

    # ============================
    # ワークフロー実行
    # ============================
    def create_execution(self, execution: dict) -> None:
        """実行レコードを作成する"""
        execution["ttl"] = self._ttl_timestamp()
        execution.setdefault("created_at", datetime.now(tz=UTC).isoformat())
        self._table("workflow-executions").put_item(Item=execution)

    def update_execution_status(self, execution_id: str, status: str) -> None:
        """実行ステータスを更新する"""
        update_expr = "SET #s = :status"
        attr_names = {"#s": "status"}
        attr_values: dict = {":status": status}

        if status == "completed" or status == "failed":
            update_expr += ", completed_at = :completed_at"
            attr_values[":completed_at"] = datetime.now(tz=UTC).isoformat()

        self._table("workflow-executions").update_item(
            Key={"execution_id": execution_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )

    def update_execution_tokens(
        self,
        execution_id: str,
        total_tokens: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        total_cost: "decimal.Decimal | None",
    ) -> None:
        """実行レコードにトークン使用量とコストを書き込む（実行完了時）"""
        import decimal
        attr_values: dict = {}
        sets: list[str] = []
        if total_tokens is not None:
            sets.append("total_tokens_used = :tt")
            attr_values[":tt"] = total_tokens
        if input_tokens is not None:
            sets.append("total_input_tokens = :ti")
            attr_values[":ti"] = input_tokens
        if output_tokens is not None:
            sets.append("total_output_tokens = :to")
            attr_values[":to"] = output_tokens
        if total_cost is not None:
            sets.append("total_cost_usd = :tc")
            attr_values[":tc"] = total_cost
        if not sets:
            return
        self._table("workflow-executions").update_item(
            Key={"execution_id": execution_id},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=attr_values,
        )

    def get_execution(self, execution_id: str) -> dict:
        """実行レコードを取得する"""
        response = self._table("workflow-executions").get_item(Key={"execution_id": execution_id})
        return response["Item"]

    # ============================
    # ワークフローステップ
    # ============================
    def put_step(self, step: dict) -> None:
        """ステップレコードを保存する"""
        step["ttl"] = self._ttl_timestamp()
        self._table("workflow-steps").put_item(Item=step)

    def update_step_status(self, execution_id: str, step_id: str, status: str, result: dict | None = None) -> None:
        """ステップステータスを更新する"""
        update_expr = "SET #s = :status"
        attr_names = {"#s": "status"}
        attr_values: dict = {":status": status}

        if status == "running":
            update_expr += ", started_at = :started_at"
            attr_values[":started_at"] = datetime.now(tz=UTC).isoformat()
        elif status in ("completed", "failed"):
            update_expr += ", completed_at = :completed_at"
            attr_values[":completed_at"] = datetime.now(tz=UTC).isoformat()

        if result is not None:
            update_expr += ", #r = :result"
            attr_names["#r"] = "result"
            attr_values[":result"] = result

        self._table("workflow-steps").update_item(
            Key={"execution_id": execution_id, "step_id": step_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )

    # ============================
    # 成果物
    # ============================
    def put_deliverable(self, deliverable: dict) -> None:
        """成果物レコードを保存する"""
        deliverable.setdefault("created_at", datetime.now(tz=UTC).isoformat())
        self._table("deliverables").put_item(Item=deliverable)

    # ============================
    # 出典
    # ============================
    def put_sources(self, execution_id: str, sources: list[dict]) -> None:
        """出典レコードを一括保存する

        source_id は呼び出し元で system-wide に一意化済み（step_id prefix）である前提。
        source_id / URL の重複分はスキップする。
        """
        table = self._table("sources")
        ttl = self._ttl_timestamp()
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        with table.batch_writer() as batch:
            for source in sources:
                source_id = source.get("source_id", "")
                url = source.get("url", "")
                if not source_id:
                    logger.warning(
                        "Skipping source without source_id",
                        extra={"execution_id": execution_id, "url": url},
                    )
                    continue
                if source_id in seen_ids:
                    continue
                if url and url in seen_urls:
                    continue
                seen_ids.add(source_id)
                if url:
                    seen_urls.add(url)
                item = dict(source)
                item["execution_id"] = execution_id
                item["ttl"] = ttl
                batch.put_item(Item=item)
