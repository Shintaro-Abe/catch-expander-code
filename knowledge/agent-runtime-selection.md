# エージェント実行基盤の選定調査

## 調査日
2026-04-04

## 調査目的
Catch Expanderのマルチ AIエージェントを動かす実行基盤とLLMアクセス方式を選定する。

## 前提条件
- LLMモデル: Claude Opus（高度な推論能力が必須）
- コスト: Opus従量課金は高額なため、Maxプラン（月額固定）で抑えたい
- エージェント基盤: 自律的にワークフローを構築・実行するマルチAIエージェント

## 調査対象

4つの組み合わせを調査した。

| # | 実行環境 | エージェント基盤 | LLMアクセス |
|---|---------|---------------|------------|
| A | Bedrock AgentCore | Claude Agent SDK | Bedrock従量課金 |
| B | Bedrock AgentCore | Claude Agent SDK | Maxプラン |
| C | 自前ECS | Claude Agent SDK | Maxプラン |
| D | 自前ECS | Claude Code CLI | Maxプラン |

---

## 1. Bedrock AgentCore + Agent SDK（パターンA / B）

### AgentCoreの特徴
- AWSのマネージドサービスでエージェントを実行
- Firecracker MicroVMでセッションごとにハードウェアレベルで隔離
- サーバーレス実行（インフラ管理不要）
- Agent SDK公式サポートあり

### AgentCore + Bedrock従量課金（パターンA）
- AWS公式でサポートされた構成
- ポリシー上の問題なし
- Opus従量課金が高額（月100回利用で約$270）

### AgentCore + Maxプラン（パターンB）
- MaxプランのOAuth認証をAWSのマネージドサービス上で使用する構成
- AgentCoreはBedrock認証を前提に設計されており、Maxプラン認証の設定方法が不明確

### 参考情報
- AWS公式: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/using-any-agent-framework.html
- AWS Samples: https://github.com/aws-samples/sample-agentic-ai-with-claude-agent-sdk-and-amazon-bedrock-agentcore
- AWS Blog: https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-and-claude-transforming-business-with-agentic-ai/

---

## 2. Anthropicのポリシー調査

### 公式ドキュメントの原文

**出典1: https://code.claude.com/docs/en/legal-and-compliance**

> Developers building products or services that interact with Claude's capabilities, including those using the Agent SDK, should use API key authentication through Claude Console or a supported cloud provider. Anthropic does not permit third-party developers to offer Claude.ai login or to route requests through Free, Pro, or Max plan credentials on behalf of their users.

**出典2: https://platform.claude.com/docs/en/agent-sdk/overview**

> Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Please use the API key authentication methods described in this document instead.

### ポリシーの解釈

禁止されている行為:
- 第三者開発者が自分の製品で「Claude.aiログイン」を提供すること
- 第三者開発者がユーザーのFree/Pro/Maxプラン認証を代理してリクエストを送ること

ポイント:
- 「Claude.aiログイン」= OAuth認証（同じもの）
- 禁止の主体は「third-party developers」が「offer」する行為
- Agent SDKを使う場合はAPIキー認証が明示的に推奨されている

### 事前承認プロセス
- 「Unless previously approved」とあるが、公式な承認プロセスは文書化されていない
- https://www.anthropic.com/contact-sales への問い合わせが唯一の手段

---

## 3. 自前ECS + Agent SDK + Maxプラン（パターンC）

### 構成
- 自分のECS Container上でAgent SDKを実行
- MaxプランのOAuth認証を使用

### ポリシー上の評価
- Agent SDKのドキュメントが「APIキー認証を使え」と明記している
- 自分だけが使う個人ツールであっても、Agent SDK + Maxプランの組み合わせはグレーゾーン
- 「第三者にClaude.ai loginを提供している」わけではないが、Agent SDKのドキュメントの推奨に反する

---

## 4. 自前ECS + Claude Code CLI + Maxプラン（パターンD）★ 選定

### 構成
- 自分のECS Container上でClaude Code CLIを実行
- MaxプランのOAuth認証を使用

### ポリシー上の評価
- Claude CodeはAnthropic公式アプリケーションであり、Maxプランの想定された利用先
- Claude.ai loginを「提供（offer）」しているのはAnthropic（Claude Code自体）であり、自分ではない
- 自分は「Anthropic公式アプリを自分の環境で自分が使っている」だけ
- Agent SDKのドキュメントの制約（「APIキー認証を使え」）はClaude Code CLIには適用されない

### Claude Code CLIの優位性

| 観点 | Agent SDK | Claude Code CLI |
|------|-----------|----------------|
| Maxプランのポリシー適合性 | グレー | 問題なし |
| マルチエージェント | spawn/await実装が必要 | Agentツールで組み込み |
| Web検索 | 外部API別途必要 | WebSearch/WebFetch組み込み |
| ファイル操作 | 自前実装 | Read/Write/Bash組み込み |
| MCP対応 | 自前実装 | 組み込み |
| 実装コスト | 高い | 低い（CLIを呼ぶだけ） |

### プログラムからの実行方法
```bash
# 単発実行
claude -p "プロンプト" --output-format json

# ツール制限
claude -p "プロンプト" --allowedTools "Agent,WebSearch,WebFetch,Read,Write,Bash"

# モデル指定
claude -p "プロンプト" --model opus
```

---

## 5. 選定結果

### 決定: パターンD（自前ECS + Claude Code CLI + Maxプラン）

| 項目 | 決定 |
|------|------|
| LLMモデル | Claude Opus |
| プラン | Maxプラン（月額固定） |
| エージェント基盤 | Claude Code CLI |
| 実行環境 | 自前ECS Container |
| 認証 | MaxプランOAuth（Claude Code公式アプリとして使用） |

### 選定理由
1. **コスト**: Maxプラン月額固定でOpusの従量課金を回避
2. **ポリシー適合性**: Claude Codeは公式アプリケーションであり、Maxプランの利用に問題なし
3. **実装コスト**: Claude Code組み込みのツール（Agent, WebSearch, MCP等）を活用し、開発量を最小化
4. **マルチエージェント**: Claude CodeのAgentツールでサブエージェントの並列起動が可能

### リスクと対策

| リスク | 対策 |
|--------|------|
| Anthropicがポリシーを変更しClaude Code CLIの自動実行を制限する可能性 | Anthropic API従量課金へのフォールバック設計を用意（環境変数切り替えで移行可能） |
| Maxプランの利用上限に達する可能性 | 月間利用量をモニタリングし、上限に近づいたらSlackで通知 |
| ECSのコンテナ管理が必要 | Fargateで運用負荷を最小化 |

### 不採用理由

| パターン | 不採用理由 |
|---------|-----------|
| A. AgentCore + Bedrock従量課金 | Opus従量課金が高額 |
| B. AgentCore + Maxプラン | AgentCore上でのMaxプラン認証設定が不明確 |
| C. ECS + Agent SDK + Maxプラン | Agent SDKドキュメントがAPIキー認証を推奨しており、Maxプランはグレーゾーン |
