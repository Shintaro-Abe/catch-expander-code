"""DynamoDbClient のユニットテスト"""

from unittest.mock import MagicMock, patch


class TestPutSources:
    """put_sources のテスト（M1: source_id は呼び出し元で一意化済み前提）"""

    def _make_client(self, batch_writer_mock: MagicMock):
        from state.dynamodb_client import DynamoDbClient

        with patch("state.dynamodb_client.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_batch_ctx = MagicMock()
            mock_batch_ctx.__enter__ = MagicMock(return_value=batch_writer_mock)
            mock_batch_ctx.__exit__ = MagicMock(return_value=False)
            mock_table.batch_writer.return_value = mock_batch_ctx
            mock_resource.return_value.Table.return_value = mock_table
            return DynamoDbClient("catch-expander")

    def test_preserves_source_id_without_uuid_overwrite(self):
        """呼び出し元が付けた source_id を UUID で上書きせず、そのまま保存する"""
        batch = MagicMock()
        client = self._make_client(batch)

        sources = [
            {"source_id": "research-1:src-001", "url": "https://a", "title": "A"},
            {"source_id": "research-2:src-001", "url": "https://b", "title": "B"},
        ]
        client.put_sources("exec-001", sources)

        assert batch.put_item.call_count == 2
        saved_ids = [c.kwargs["Item"]["source_id"] for c in batch.put_item.call_args_list]
        assert saved_ids == ["research-1:src-001", "research-2:src-001"]

    def test_skips_duplicate_source_ids(self):
        """同じ source_id が複数回現れたら後続をスキップする"""
        batch = MagicMock()
        client = self._make_client(batch)

        sources = [
            {"source_id": "r-1:src-001", "url": "https://a"},
            {"source_id": "r-1:src-001", "url": "https://a-dup"},
            {"source_id": "r-2:src-001", "url": "https://b"},
        ]
        client.put_sources("exec-001", sources)

        assert batch.put_item.call_count == 2
        saved_ids = [c.kwargs["Item"]["source_id"] for c in batch.put_item.call_args_list]
        assert saved_ids == ["r-1:src-001", "r-2:src-001"]

    def test_skips_duplicate_urls(self):
        """URL が同一のものは後続をスキップする（source_id が異なっていても）"""
        batch = MagicMock()
        client = self._make_client(batch)

        sources = [
            {"source_id": "r-1:src-001", "url": "https://shared"},
            {"source_id": "r-2:src-001", "url": "https://shared"},
            {"source_id": "r-2:src-002", "url": "https://unique"},
        ]
        client.put_sources("exec-001", sources)

        assert batch.put_item.call_count == 2
        saved_ids = [c.kwargs["Item"]["source_id"] for c in batch.put_item.call_args_list]
        assert saved_ids == ["r-1:src-001", "r-2:src-002"]

    def test_skips_source_without_source_id(self):
        """source_id 欠損の出典はスキップされる"""
        batch = MagicMock()
        client = self._make_client(batch)

        sources = [
            {"url": "https://a"},  # source_id なし
            {"source_id": "r-1:src-001", "url": "https://b"},
        ]
        client.put_sources("exec-001", sources)

        assert batch.put_item.call_count == 1
        saved = batch.put_item.call_args_list[0].kwargs["Item"]
        assert saved["source_id"] == "r-1:src-001"

    def test_adds_execution_id_and_ttl(self):
        """保存項目に execution_id と ttl が付与される"""
        batch = MagicMock()
        client = self._make_client(batch)

        sources = [{"source_id": "r-1:src-001", "url": "https://a"}]
        client.put_sources("exec-XYZ", sources)

        saved = batch.put_item.call_args_list[0].kwargs["Item"]
        assert saved["execution_id"] == "exec-XYZ"
        assert "ttl" in saved
        assert isinstance(saved["ttl"], int)

    def test_handles_empty_list(self):
        """空リストでも例外を起こさない"""
        batch = MagicMock()
        client = self._make_client(batch)

        client.put_sources("exec-001", [])
        assert batch.put_item.call_count == 0
