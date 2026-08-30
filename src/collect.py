"""毎日の観測。全ツールの価格ページを巡回して履歴に記録する。

生成(render)と分離してあるのは、ネットワークに触る処理とHTMLを書く処理を
混ぜると、片方の失敗でもう片方が巻き添えになるため。
1つのツールの取得に失敗しても、他のツールと過去の履歴でサイトは成立する。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .catalog import Catalog
from .extract import extract
from .fetch import Fetcher
from .track import append_snapshot, last_ok, load_history, should_record, to_snapshot

log = logging.getLogger(__name__)


def collect(
    catalog: Catalog,
    fetcher: Fetcher,
    history_path: Path,
    now: datetime,
    record: bool = False,
) -> tuple[dict[str, dict], list[str], int]:
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

    戻り値: (latest.json に書く辞書, 変更を記録した slug の一覧, 取得に失敗した数)
    """
    history = load_history(history_path)
    latest: dict[str, dict] = {}
    recorded: list[str] = []
    failed = 0

    for tool in catalog.tools:
        result = fetcher.get(tool.pricing_url)

        if not result.ok:
            failed += 1
            log.warning("%s: 取得できませんでした (%s)", tool.slug, result.error)
            latest[tool.slug] = {
                "checked_at": now.isoformat(),
                "ok": False,
                "note": result.error,
                "http_status": result.status,
            }
            continue

        extraction = extract(result.html, tool.plans, tool.patterns)
        snapshot = to_snapshot(tool.slug, extraction, now)
        previous = last_ok(history.get(tool.slug, []))

        if not extraction.ok:
            failed += 1
            log.warning("%s: 価格を抽出できませんでした (%s)", tool.slug, extraction.note)
        elif should_record(previous, snapshot):
            if record:
                append_snapshot(history_path, snapshot)
                history.setdefault(tool.slug, []).append(snapshot)
            recorded.append(tool.slug)
            if previous is None:
                log.info("%s: 初回記録 (%d プラン取得)", tool.slug, len(snapshot.plans))
            else:
                log.info(
                    "%s: 変更を検出 %s → %s",
                    tool.slug,
                    previous.signature or "(なし)",
                    snapshot.signature,
                )
        else:
            log.info("%s: 変更なし", tool.slug)

        resolved = len(extraction.resolved_plans)
        if extraction.ok and resolved < len(tool.plans):
            # 全プラン取れないのは普通だが、0件が続くならセレクタが死んでいる
            log.info(
                "%s: %d/%d プランのみ特定 (%s)",
                tool.slug,
                resolved,
                len(tool.plans),
                extraction.method,
            )

        latest[tool.slug] = {
            "checked_at": now.isoformat(),
            "ok": extraction.ok,
            "note": extraction.note,
            "http_status": result.status,
            "method": extraction.method,
            "plans_resolved": resolved,
            "plans_expected": len(tool.plans),
        }

    return latest, recorded, failed


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

