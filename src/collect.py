"""毎日の観測。全ツールの価格ページを巡回して履歴に記録する。

生成(render)と分離してあるのは、ネットワークに触る処理とHTMLを書く処理を
混ぜると、片方の失敗でもう片方が巻き添えになるため。
1つのツールの取得に失敗しても、他のツールと過去の履歴でサイトは成立する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .catalog import Catalog
from .extract import extract
from .fetch import Fetcher
from .track import (
    append_snapshot,
    last_ok,
    load_history,
    page_changed,
    should_record,
    to_snapshot,
)

log = logging.getLogger(__name__)


@dataclass
class CollectResult:
    latest: dict[str, dict]
    # 読者に見える内容が変わった slug。IndexNow の送信対象
    changed: list[str] = field(default_factory=list)
    failed: int = 0
    # 前回の巡回では読めたのに、今回は読めなかったプラン(slug → プラン名)
    lost: dict[str, list[str]] = field(default_factory=dict)


def lost_plans(previous_entry: dict, resolved: set[str]) -> list[str]:
    """前回の巡回で読めて、今回は読めなかったプラン。

    plans_seen[プラン] が前回の checked_at と一致する = 前回その価格を読めた。
    読めなくなった「その日」だけ返すので、直すまで毎日通知が続くことはない
    (翌日には plans_seen がもう前回の checked_at と一致しない)。
    """
    checked = previous_entry.get("checked_at")
    seen = previous_entry.get("plans_seen")
    if not checked or not isinstance(seen, dict):
        return []  # 比べる基準が無い(初回、または plans_seen を導入する前)
    return sorted(plan for plan, ts in seen.items() if ts == checked and plan not in resolved)


def lost_report(lost: dict[str, list[str]], catalog: Catalog, now: datetime) -> str:
    """読めなくなったプランを知らせる Issue の本文。"""
    by_slug = {t.slug: t for t in catalog.tools}
    lines = [
        f"**{now:%Y-%m-%d} の日次巡回**",
        "",
        "前回の巡回では読めていたプランの価格を、今回は読めませんでした。",
        "料金ページが作り替えられた可能性が高いです。",
        "",
    ]
    for slug, plans in sorted(lost.items()):
        tool = by_slug.get(slug)
        name, url = (tool.name, tool.pricing_url) if tool else (slug, "")
        lines.append(f"- **{name}** (`{slug}`): {', '.join(plans)} — {url}")
    lines += [
        "",
        "### サイト上の扱い",
        "",
        "該当ツールは「Partly verified」表示になり、読めなかった行には最後に読めた日付が出ます。",
        "読めない日が `stale_after_days`（既定14日）を超えると、そのプランの価格は伏せられます。",
        "誤った価格が出ることはありませんが、放置すると空欄になります。",
        "",
        "### 直し方",
        "",
        "`config/tools.yaml` の該当ツールに `patterns` を書きます"
        "（金額を捉えるグループを1つだけ含む正規表現）。",
        "直せない場合は、そのプランを `plans` から外してください。",
        "",
        "この通知は読めなくなった日に1回だけ出ます。直すまで毎日届くことはありません。",
    ]
    return "\n".join(lines) + "\n"


def collect(
    catalog: Catalog,
    fetcher: Fetcher,
    history_path: Path,
    now: datetime,
    previous_latest: dict[str, dict] | None = None,
    record: bool = False,
) -> CollectResult:
    """全ツールを巡回する。

    record=False のときは履歴に書き込まない。既定を False にしてあるのは、
    履歴は必ず「同じ観測地点」から取られなければならないため。
    多くのSaaSはアクセス元の国で言語と通貨を変えるので、日本から見た結果と
    CI(米国)から見た結果が混ざると、実際には起きていない値上げや
    プラン追加が履歴に残る。実際 Notion を日本から見ると Free しか
    読めず、CIの結果と混ざって「Notion が Plus を追加した」という
    嘘の変更イベントが生成された。

    したがって履歴を書けるのは GitHub Actions だけ(--record)で、
    手元の実行は巡回と生成の確認までに留める。

    previous_latest は前回の latest.json。プランごとの「最後に読めた時刻」
    (plans_seen)を引き継ぐのと、読めなくなったプランを見つけるのに使う。
    """
    history = load_history(history_path)
    result = CollectResult(latest={})
    previous_latest = previous_latest or {}

    for tool in catalog.tools:
        prev_entry = previous_latest.get(tool.slug) or {}
        # 読めなかった日は前回の値を持ち越す。設定から外したプランは持ち越さない
        seen = {
            plan: ts
            for plan, ts in (prev_entry.get("plans_seen") or {}).items()
            if plan in tool.plans
        }
        fetched = fetcher.get(tool.pricing_url)

        if not fetched.ok:
            result.failed += 1
            log.warning("%s: 取得できませんでした (%s)", tool.slug, fetched.error)
            result.latest[tool.slug] = {
                "checked_at": now.isoformat(),
                "ok": False,
                "note": fetched.error,
                "http_status": fetched.status,
                "plans_seen": seen,
            }
            # 取得できない日は「読めなくなった」とは言わない。相手側の一時的な障害が
            # ほとんどで、続けば鮮度切れで価格が伏せられ、月次点検が FETCH として拾う
            continue

        extraction = extract(fetched.html, tool.plans, tool.patterns)
        snapshot = to_snapshot(tool.slug, extraction, now)
        previous = last_ok(history.get(tool.slug, []))

        if not extraction.ok:
            result.failed += 1
            log.warning("%s: 価格を抽出できませんでした (%s)", tool.slug, extraction.note)
        elif should_record(previous, snapshot):
            if record:
                append_snapshot(history_path, snapshot)
                history.setdefault(tool.slug, []).append(snapshot)
            if previous is None:
                result.changed.append(tool.slug)
                log.info("%s: 初回記録 (%d プラン取得)", tool.slug, len(snapshot.plans))
            elif page_changed(previous, snapshot):
                result.changed.append(tool.slug)
                log.info(
                    "%s: 変更を検出 %s → %s",
                    tool.slug,
                    previous.signature or "(なし)",
                    snapshot.signature,
                )
            else:
                # 価格表記の一覧を持たない古い記録の後に、基準として追記しただけ。
                # ページは何も変わっていないので通知しない
                log.info("%s: 価格表記の一覧を基準として記録", tool.slug)
        else:
            log.info("%s: 変更なし", tool.slug)

        resolved_names = {p.plan for p in extraction.resolved_plans}
        for plan in resolved_names:
            seen[plan] = now.isoformat()

        lost = [p for p in lost_plans(prev_entry, resolved_names) if p in tool.plans]
        if lost:
            result.lost[tool.slug] = lost
            log.warning(
                "%s: 前回は読めた %s を今回は読めませんでした", tool.slug, ", ".join(lost)
            )

        resolved = len(resolved_names)
        if extraction.ok and resolved < len(tool.plans):
            # 全プラン取れないのは普通だが、0件が続くならセレクタが死んでいる
            log.info(
                "%s: %d/%d プランのみ特定 (%s)",
                tool.slug,
                resolved,
                len(tool.plans),
                extraction.method,
            )

        result.latest[tool.slug] = {
            "checked_at": now.isoformat(),
            "ok": extraction.ok,
            "note": extraction.note,
            "http_status": fetched.status,
            "method": extraction.method,
            "plans_resolved": resolved,
            "plans_expected": len(tool.plans),
            # プランごとに、最後に価格を読めた時刻。サイトの確認日表示と、
            # 読めなくなったプランの検出に使う
            "plans_seen": seen,
        }

    return result


# 状態の良し悪しの順序。前回より下がったかを判定するのに使う。
#   FETCH … ページを取得できなかった。相手のブロックや障害で、こちらの設定の問題ではない
#   NG    … 取得はできたが価格を1つも取り出せない。patterns を書く必要がある
# 直し方が違うので混ぜない。FETCH に patterns を書いても意味がない。
STATUS_RANK = {"FETCH": 0, "NG": 0, "PARTIAL": 1, "OK": 2}


def check(catalog: Catalog, fetcher: Fetcher) -> dict[str, dict]:
    """--check 用。履歴を汚さずに、どのツールの抽出が壊れているかだけ報告する。

    プラン名の表記変更や料金ページのURL変更は必ず起きる。しかもこの壊れ方は
    サイト上では「価格が空欄」になるだけで、エラーも出さずに静かに進行する。
    それを早く見つけるための道具。

    戻り値は slug ごとの結果。前回分と比べて劣化を検出するのに使う。
    """
    results: dict[str, dict] = {}
    print(f"{'slug':<16} {'status':<8} {'plans':<9} method")
    print("-" * 56)

    for tool in catalog.tools:
        result = fetcher.get(tool.pricing_url)
        if not result.ok:
            print(f"{tool.slug:<16} {'FETCH':<8} {'-':<9} {result.error}")
            results[tool.slug] = {
                "status": "FETCH",
                "resolved": 0,
                "expected": len(tool.plans),
                "detail": result.error,
                "missing": list(tool.plans),
            }
            continue

        extraction = extract(result.html, tool.plans, tool.patterns)
        resolved = len(extraction.resolved_plans)
        ratio = f"{resolved}/{len(tool.plans)}"

        if not extraction.ok or resolved == 0:
            status = "NG"
        elif resolved < len(tool.plans):
            status = "PARTIAL"
        else:
            status = "OK"

        detail = extraction.method
        if extraction.note:
            detail = f"{detail} — {extraction.note}"
        print(f"{tool.slug:<16} {status:<8} {ratio:<9} {detail}")

        missing = [p.plan for p in extraction.plans if p.amount is None]
        if missing and status != "NG":
            print(f"{'':<16} {'':<8} 未検出プラン: {', '.join(missing)}")

        results[tool.slug] = {
            "status": status,
            "resolved": resolved,
            "expected": len(tool.plans),
            "detail": detail,
            "missing": missing,
        }

    ng = sum(1 for r in results.values() if r["status"] == "NG")
    unreachable = sum(1 for r in results.values() if r["status"] == "FETCH")
    print("-" * 56)
    print(f"要対応: NG {ng}件（patterns が必要） / FETCH {unreachable}件（相手側の遮断・障害）")
    return results


def compare_checks(previous: dict[str, dict], current: dict[str, dict]) -> list[str]:
    """前回の点検結果と比べて「悪くなった」ものだけを挙げる。

    ずっと PARTIAL のままのツールを毎月通知しても読まれなくなるだけなので、
    知らせる価値があるのは状態が下がった瞬間と、取れるプランが減った瞬間だけ。
    """
    degraded: list[str] = []
    for slug, now in current.items():
        was = previous.get(slug)
        if not was:
            continue  # 新しく追加したツールは劣化ではない
        # 未知の状態文字列で点検全体を落とさない。古い check.json には
        # FETCH が無かったように、状態の種類は後から増える。
        rank_now = STATUS_RANK.get(now["status"], 0)
        rank_was = STATUS_RANK.get(was["status"], 0)
        if rank_now < rank_was:
            degraded.append(
                f"{slug}: {was['status']} → {now['status']}"
                f" ({now['resolved']}/{now['expected']}プラン) — {now['detail']}"
            )
        elif now["resolved"] < was["resolved"]:
            degraded.append(
                f"{slug}: 取得できるプランが {was['resolved']} → {now['resolved']} に減少"
                f" — 未検出: {', '.join(now.get('missing') or []) or '不明'}"
            )

    # 設定から外したツールは意図的なので通知しない
    return degraded

