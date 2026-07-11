#!/usr/bin/env python3
"""既存 learned_preferences への適用スコープ一括付与スクリプト（使い捨て）。

.steering/20260706-preference-scope/design.md §3.8。
検証の基準 (CLAUDE.md §1) に従い、書き戻し前に目視確認を挟む 2 段構成:

  1. dry-run（デフォルト）: user-profiles を scan し、scope 未付与の好みを
     ローカルの `claude -p`（headless）で分類。対照表を stdout に出力し、
     提案 JSON を scripts/.migration_proposal.json に保存する。
  2. ユーザーが対照表を目視確認（誤分類は proposal JSON を手で修正可能）。
  3. --apply: proposal JSON を enum バリデーション後に put_item で書き戻す。
     書き戻し前の値を stdout に出力する（手動ロールバック用）。

前提: `aws login` 済み / ローカルに `claude` CLI。

usage:
    uv run python scripts/migrate_preference_scopes.py            # dry-run
    uv run python scripts/migrate_preference_scopes.py --apply    # 書き戻し
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "agent"))

from feedback.scope import (  # noqa: E402
    SCOPE_CATEGORIES,
    SCOPE_DELIVERABLES,
    format_scope_label,
    validate_scope,
)

TABLE_NAME = "catch-expander-user-profiles"
REGION = "ap-northeast-1"
PROPOSAL_PATH = REPO_ROOT / "scripts" / ".migration_proposal.json"

_CLASSIFY_PROMPT = """# 学習済み好みのスコープ分類タスク

以下はユーザーの成果物生成 AI に蓄積された「好み」のリストです。
各好みが**将来どの範囲のトピック・成果物に適用されるべきか**を分類してください。

## スコープの定義
- categories: 適用するトピックカテゴリのリスト。次の5値のみ使用可:
  技術（クラウド・プログラミング・インフラ・AI/ML等）/ 時事（国際情勢・政治・社会問題等）/
  ビジネス（市場動向・企業戦略・経済等）/ 学術（研究・論文・理論等）/ カルチャー（技術文化・トレンド等）
- deliverables: 適用する成果物区分のリスト。次の6値のみ使用可:
  code（IaC・プログラム等のコード全般）/ research_report（調査レポート）/
  architecture_design（アーキテクチャ設計書）/ comparison_table（比較表）/
  cost_estimate（料金見積もり）/ procedure_guide（手順書）
- トピックや成果物の種類によらず常に当てはまる嗜好（文体・構成・簡潔さ等）は両方を空配列にする
- 適用範囲に迷う場合は狭くスコープする

## 好みリスト
{prefs_json}

**重要**: 前置き文・説明文は不要です。
入力と同じ順序・同じ件数で、以下のJSON形式のみを```jsonブロックで出力してください。

```json
{{"scopes": [{{"categories": [], "deliverables": []}}]}}
```
"""


def _call_claude(prompt: str) -> str:
    # ローカル運用者が手動実行する使い捨てスクリプト。コマンドは固定で、
    # prompt は自分の DynamoDB データ由来のため S603/S607 は許容する。
    result = subprocess.run(  # noqa: S603
        ["claude", "-p", prompt, "--output-format", "text"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return result.stdout


def _extract_json(raw: str) -> dict:
    match = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.DOTALL)
    payload = match.group(1) if match else raw.strip()
    return json.loads(payload)


def _load_profiles(table) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _classify(prefs: list[dict]) -> list[dict]:
    """scope 未付与の好み全件を 1 回の claude 呼び出しで分類する。"""
    prefs_json = json.dumps([{"index": i, "text": p["text"]} for i, p in enumerate(prefs)], ensure_ascii=False)
    raw = _call_claude(_CLASSIFY_PROMPT.format(prefs_json=prefs_json))
    scopes = _extract_json(raw).get("scopes", [])
    if len(scopes) != len(prefs):
        print(f"ERROR: 分類結果の件数不一致 (期待 {len(prefs)} / 実際 {len(scopes)})。中断します。")
        sys.exit(1)
    # 分類結果にも書き込み側と同じ enum バリデーションを適用（フォールバック先なし = 汎用に倒さず
    # invalid はそのまま空へ。dry-run の目視確認で気付ける形にする）
    return [validate_scope(s, None, None) for s in scopes]


def _dry_run(table) -> None:
    profiles = _load_profiles(table)
    proposal: dict = {}
    for profile in profiles:
        user_id = profile["user_id"]
        prefs = profile.get("learned_preferences", [])
        if not isinstance(prefs, list):
            print(f"user {user_id}: learned_preferences が list でないためスキップ")
            continue
        # Codex Pass 1 P2 対応: 同一 text の duplicate があっても衝突しないよう、
        # 保存配列内の絶対 index をキーにして提案する
        unscoped = [
            (i, p)
            for i, p in enumerate(prefs)
            if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"] and "scope" not in p
        ]
        if not unscoped:
            print(f"user {user_id}: scope 未付与の好みなし（全 {len(prefs)} 件）")
            continue
        print(f"\nuser {user_id}: {len(unscoped)} / {len(prefs)} 件を分類します...")
        scopes = _classify([p for _, p in unscoped])
        rows = []
        for (idx, pref), scope in zip(unscoped, scopes, strict=True):
            labeled = {"index": idx, "text": pref["text"], "scope": scope}
            rows.append(labeled)
            print(f"  {idx}: [{format_scope_label(labeled)}] {pref['text']}")
        proposal[user_id] = rows

    if not proposal:
        print("\n移行対象なし。")
        return
    PROPOSAL_PATH.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n提案を {PROPOSAL_PATH} に保存しました。")
    print("対照表を目視確認し（必要ならファイルを手修正し）、--apply で書き戻してください。")


def _apply(table) -> None:
    if not PROPOSAL_PATH.exists():
        print(f"ERROR: {PROPOSAL_PATH} がありません。先に dry-run を実行してください。")
        sys.exit(1)
    proposal = json.loads(PROPOSAL_PATH.read_text(encoding="utf-8"))

    for user_id, rows in proposal.items():
        # Codex Pass 2 P3 対応: proposal / DynamoDB 側のコンテナ型自体も明示検証し、
        # AttributeError 等の分かりにくい落ち方をさせない
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            print(f"ERROR: proposal の user {user_id} エントリが list[dict] ではありません。中断します。")
            sys.exit(1)
        resp = table.get_item(Key={"user_id": user_id})
        profile = resp.get("Item")
        if not profile:
            print(f"user {user_id}: プロファイルが見つかりません。スキップ。")
            continue
        prefs = profile.get("learned_preferences", [])
        if not isinstance(prefs, list):
            print(f"ERROR: user {user_id} の learned_preferences が list ではありません。中断します。")
            sys.exit(1)
        print(f"\nuser {user_id}: 書き戻し前の learned_preferences（ロールバック用）:")
        print(json.dumps(prefs, ensure_ascii=False, indent=2, default=str))

        # Codex Pass 1 P2/P3 対応: 手修正されうる proposal を index 単位で厳密検証し、
        # 1 件でも突合できなければ put_item せずに中断する（部分適用を作らない）
        validated: list[tuple[int, dict]] = []
        for row in rows:
            idx = row.get("index")
            scope = row.get("scope")
            if not isinstance(idx, int) or isinstance(idx, bool):
                print(f"ERROR: proposal の index が不正です: {row}")
                sys.exit(1)
            if not isinstance(scope, dict):
                print(f"ERROR: proposal の scope が不正です: {row}")
                sys.exit(1)
            categories = scope.get("categories")
            deliverables = scope.get("deliverables")
            if not isinstance(categories, list) or not isinstance(deliverables, list):
                print(f"ERROR: categories / deliverables は list 必須です: {row}")
                sys.exit(1)
            invalid = [c for c in categories if not isinstance(c, str) or c not in SCOPE_CATEGORIES]
            invalid += [d for d in deliverables if not isinstance(d, str) or d not in SCOPE_DELIVERABLES]
            if invalid:
                print(f"ERROR: enum 外の値が含まれています: {invalid} ({row.get('text')})")
                sys.exit(1)
            pref = prefs[idx] if 0 <= idx < len(prefs) else None
            if not (isinstance(pref, dict) and "scope" not in pref and pref.get("text") == row.get("text")):
                print(
                    f"ERROR: index {idx} の現在値が proposal と一致しません"
                    f"（dry-run 後にデータが変わった可能性）。中断します: {row}"
                )
                sys.exit(1)
            validated.append((idx, {"categories": list(categories), "deliverables": list(deliverables)}))

        for idx, scope in validated:
            prefs[idx]["scope"] = scope
        if len(validated) != len(rows):
            print(f"ERROR: 適用件数不一致 ({len(validated)} != {len(rows)})。中断します。")
            sys.exit(1)
        profile["learned_preferences"] = prefs
        table.put_item(Item=profile)
        print(f"user {user_id}: {len(validated)} 件に scope を付与して書き戻しました。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="proposal JSON を検証して書き戻す")
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    if args.apply:
        _apply(table)
    else:
        _dry_run(table)


if __name__ == "__main__":
    main()
