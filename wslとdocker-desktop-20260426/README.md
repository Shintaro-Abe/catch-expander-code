## プログラムコード（Python またはユーザープロファイルの技術スタック）

# WSL2 + Docker Desktop: Lambda + API Gateway PoC

> **PoC品質**: このリポジトリは概念実証用のスケルトンです。本番環境での使用前に十分なレビューを行ってください。

WSL2 + Docker Desktop バックエンドを使用した AWS CDK 開発環境のサンプルです。  
Lambda（Python 3.12）+ API Gateway REST API の構成をローカル（SAM CLI）でテストできます。

## アーキテクチャ

```
API Gateway (REST API)
├── GET  /hello  → HelloFunction (Lambda)
└── POST /items  → ItemsFunction (Lambda)
```

## ファイル構成

```
.
├── app.py                 # CDK アプリエントリーポイント
├── lambda_stack.py        # CDK スタック定義（Lambda + API Gateway 統合）
├── lambda/
│   └── handler.py         # Lambda ハンドラー関数
├── wsl2_docker_setup.sh   # WSL2 + Docker Desktop 環境セットアップスクリプト
└── README.md
```

## セットアップ

### 1. WSL2 環境構築（初回のみ）

```bash
chmod +x wsl2_docker_setup.sh
./wsl2_docker_setup.sh
```

Docker Desktop の WSL2 統合が有効であることを確認してください:  
**Settings > Resources > WSL Integration > Ubuntu: ON**

### 2. 推奨 `.wslconfig`（Windows 側）

`C:\Users\<USERNAME>\.wslconfig` に以下を設定し `wsl --shutdown` で再起動:

```ini
[wsl2]
processors=4
memory=8GB
swap=2GB

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
```

### 3. Python 依存ライブラリインストール

```bash
source ~/.venvs/wsl-docker-cdk/bin/activate
pip install aws-cdk-lib constructs
```

## ローカルテスト（SAM CLI + Docker Desktop）

```bash
# 1. CDK テンプレートを生成
cdk synth

# 2. API Gateway をローカルエミュレーション（Docker Desktop が必要）
sam local start-api -t cdk.out/WslDockerLambdaStack.template.json

# 3. エンドポイントを呼び出し
curl http://127.0.0.1:3000/hello
curl http://127.0.0.1:3000/hello?name=Taro

curl -X POST http://127.0.0.1:3000/items \
  -H "Content-Type: application/json" \
  -d '{"name": "サンプルアイテム", "description": "テスト用"}'
```

## クラウドデプロイ

```bash
# AWS 認証（Windows の認証情報を共有する場合）
ln -s /mnt/c/Users/<WindowsUser>/.aws ~/.aws
aws sts get-caller-identity

# CDK ブートストラップ（初回のみ）
cdk bootstrap

# デプロイ
cdk deploy --context account=<AWS_ACCOUNT_ID> --context region=ap-northeast-1
```

## パフォーマンス注意事項

| ボリュームマウント | 推奨 | 理由 |
|---|---|---|
| `~/projects`（WSL2 ネイティブ） | ✅ 推奨 | ext4 I/O: 高速 |
| `/mnt/c/Users/...`（Windows FS） | ❌ 非推奨 | 9P プロトコル経由: 最大 20 倍遅い |

プロジェクトは **WSL2 のホームディレクトリ（`~/`）以下** に配置してください。


---

📝 [Notionで詳細を見る](https://www.notion.so/WSL-Docker-Desktop-34e47b55202e81b1925cdb67afb158f8)
