"""
統合テスト用 conftest。

orchestrator.py の _load_prompt() は src/agent/prompts/*.md を読み込むが、
テスト環境にはプロンプトファイルが存在しないため、ダミー文字列に差し替える。
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_load_prompt():
    with patch("orchestrator._load_prompt", return_value="# テスト用プロンプト"):
        yield
