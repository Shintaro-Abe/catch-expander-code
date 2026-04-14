# Claude Code プラグイン: version unknown について

## 調査日
2026-04-14

## 概要

`claude plugin list` を実行すると、一部のプラグインが `Version: unknown` と表示される。
これはインストール失敗ではなく、プラグインマニフェスト（`plugin.json`）にバージョン番号が定義されていないことを示す正常な状態。

## 事象

```
❯ terraform@claude-plugins-official
  Version: unknown
  Scope: user
  Status: ✔ enabled
```

## 原因

### 公式仕様に基づく説明

Claude Code の公式ドキュメント（[Plugins reference — Plugin manifest schema](https://code.claude.com/docs/en/plugins-reference)）によると、`plugin.json` のフィールド定義は以下のとおり。

**必須フィールド:**

| フィールド | 型 | 説明 |
|---|---|---|
| `name` | string | プラグインの一意識別子（kebab-case）|

> "If you include a manifest, `name` is the only required field."
> （マニフェストを含める場合、`name` が唯一の必須フィールドです）

**オプションフィールド（`version` を含む）:**

| フィールド | 型 | 説明 |
|---|---|---|
| `version` | string | セマンティックバージョン。**必須ではない** |
| `description` | string | プラグインの説明 |
| `author` | object | 作者情報 |

また、マーケットプレイス経由で配布される場合の注記として公式ドキュメントに以下の記述がある。

> "If your plugin is within a marketplace directory, you can manage the version through `marketplace.json` instead and omit the `version` field from `plugin.json`."
> （プラグインがマーケットプレイスディレクトリ内にある場合、`marketplace.json` でバージョンを管理し、`plugin.json` の `version` フィールドを省略できます）

### まとめ

`version: unknown` は、プラグイン開発者（HashiCorp、GitHub 等）が `plugin.json` に `version` フィールドを定義していない場合に表示される。これは仕様の範囲内であり、エラーではない。

```json
// version フィールドが存在しない plugin.json の実例（terraform プラグイン）
{
  "name": "terraform",
  "description": "The Terraform MCP Server provides seamless integration...",
  "author": { "name": "HashiCorp" }
}
```

## version: unknown となる既知のプラグイン

| プラグイン | 提供元 |
|---|---|
| `terraform@claude-plugins-official` | HashiCorp |
| `github@claude-plugins-official` | GitHub |
| `agent-sdk-dev@claude-plugins-official` | Anthropic |
| `code-review@claude-plugins-official` | Anthropic |
| `security-guidance@claude-plugins-official` | Anthropic |

## 正常か異常かの判別方法

`version: unknown` だけでは正常・異常を区別できない。
キャッシュディレクトリにファイルが存在するかで確認する。

```bash
# 正常: ファイルが存在する
ls ~/.claude/plugins/cache/claude-plugins-official/<plugin-name>/unknown/
# 例: .claude-plugin/  .mcp.json  README.md  など

# 異常（インストール失敗）: ディレクトリが空
ls ~/.claude/plugins/cache/claude-plugins-official/<plugin-name>/unknown/
# → （何も表示されない）
```

## インストール失敗時の修復手順

```bash
# プラグインを再インストール（既存ディレクトリに上書き）
claude plugin install <plugin-name>@<marketplace>

# 例
claude plugin install terraform@claude-plugins-official
claude plugin install github@claude-plugins-official
claude plugin install agent-sdk-dev@claude-plugins-official
claude plugin install code-review@claude-plugins-official
claude plugin install security-guidance@claude-plugins-official
```

再インストール後、Claude Code を再起動すると MCP サーバーが有効になる。

## 備考

- `claude plugin update` は version が解決できないプラグインでは "Plugin not found" エラーになる場合がある。その場合は `install` コマンドを使う
- インストール成功の確認は `ls` でキャッシュディレクトリの中身を確認するのが確実

## 参考資料

- [Claude Code Plugins reference — Plugin manifest schema](https://code.claude.com/docs/en/plugins-reference) — `version` フィールドのオプション仕様、マーケットプレイス経由での省略可否について記載
