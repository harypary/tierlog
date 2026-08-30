"""AI価格トラッカーの自動生成パイプライン。

    python main.py --demo     # ネットワーク不要。合成した履歴で見た目を確認
    python main.py --check    # 価格抽出が壊れていないか点検し、前回と比較(履歴は汚さない)
    python main.py --render   # 取得をスキップし、既存の履歴からサイトだけ作り直す
    python main.py --verify   # 生成済みの docs/ を検査する。CIがデプロイ前に実行する
    python main.py            # 本番: 巡回して履歴に記録し、サイトを再生成
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.catalog import ConfigError, load_catalog
from src.collect import check, collect, compare_checks
from src.demo import build_demo_history
from src.fetch import Fetcher
from src.indexnow import changed_urls, submit
from src.pages import build_compare_pages, build_feed, build_tool_pages
from src.render import render_site
from src.track import build_state, load_history, load_latest, save_latest
from src.verify import verify_site

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "prices.jsonl"
LATEST = ROOT / "data" / "latest.json"
# 前回の点検結果。これと比べて「静かな劣化」を見つける
CHECK_STATE = ROOT / "data" / "check.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI tool price tracker")
    p.add_argument("--config", default=ROOT / "config" / "site.yaml", type=Path)
    p.add_argument("--tools", default=ROOT / "config" / "tools.yaml", type=Path)
    p.add_argument("--out", default=ROOT / "docs", type=Path)
    p.add_argument("--base-url", default=None, help="未指定なら .env の SITE_BASE_URL")
    p.add_argument("--demo", action="store_true", help="合成した履歴で生成(通信しない)")
    p.add_argument("--check", action="store_true", help="価格抽出の健全性を点検して終了")
    p.add_argument("--render", action="store_true", help="巡回せず既存履歴から再生成")
    p.add_argument("--verify", action="store_true", help="生成済みの docs/ を検査して終了")
    p.add_argument(
        "--record",
        action="store_true",
        help="価格履歴に書き込む。CI(米国)専用。手元から使うと観測地点が混ざる",
    )
    p.add_argument(
        "--ping",
        action="store_true",
        help="価格が動いたページを IndexNow で通知する。CI専用",
    )
    p.add_argument(
        "--save-check",
        action="store_true",
        help="点検結果を data/check.json に保存する。次回の比較基準になる。CI専用",
    )
    return p.parse_args()


def main() -> int:
    # Windowsのコンソールは既定がcp932で、日本語ログや記号でUnicodeEncodeErrorになる
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()
    args = parse_args()
    now = datetime.now(UTC)

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    try:
        catalog = load_catalog(args.tools, cfg)
    except ConfigError as e:
        logging.error("設定エラー: %s", e)
        return 1

    gen = cfg["generation"]
    base_url = args.base_url or os.getenv("SITE_BASE_URL", "http://localhost:8000")

    def fetcher() -> Fetcher:
        # 連絡先を含む User-Agent で名乗る。匿名クローラは真っ先にブロックされる
        return Fetcher(
            user_agent=f"AIToolPriceTrackerBot/1.0 (+{cfg['site']['contact_url']})",
            interval_sec=gen["request_interval_sec"],
            timeout_sec=gen["timeout_sec"],
        )

    # ---- 点検モード: サイトは作らない ----
    if args.check:
        results = check(catalog, fetcher())

        previous = {}
        if CHECK_STATE.exists():
            try:
                previous = json.loads(CHECK_STATE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logging.warning("%s が壊れています。比較を省略します", CHECK_STATE.name)

        degraded = compare_checks(previous, results)
        if degraded:
            print("\n⚠️  前回より悪化した項目:")
            for line in degraded:
                print(f"  - {line}")

        if args.save_check:
            CHECK_STATE.parent.mkdir(parents=True, exist_ok=True)
            CHECK_STATE.write_text(
                json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        ng = [s for s, r in results.items() if r["status"] == "NG"]
        unreachable = [s for s, r in results.items() if r["status"] == "FETCH"]
        if ng:
            print(f"\n❌ 価格を取り出せません: {', '.join(ng)}")
            print("   config/tools.yaml の patterns を書くか、該当プランを外してください。")
        if unreachable:
            print(f"\n⚠️  ページを取得できません: {', '.join(unreachable)}")
            print("   相手側の遮断や障害です。patterns では直りません。")
            print("   継続するようなら追跡対象から外すことを検討してください。")
        if ng or unreachable or degraded:
            return 1
        print("\n✅ 前回から悪化した項目はありません")
        return 0

    # ---- 検査モード: 生成済みの docs/ を見るだけ ----
    if args.verify:
        problems = verify_site(args.out, base_url)
        for problem in problems:
            print(f"  NG: {problem}")
        if problems:
            print(f"\n❌ {len(problems)}件の問題があります。この状態で公開しないでください。")
            return 1
        print("\n✅ 検証に問題はありません")
        return 0

    # ---- 履歴を用意する ----
    if args.demo:
        logging.warning("デモモード: 合成した履歴で生成します(実際の価格ではありません)")
        history, latest = build_demo_history(catalog, now)
    elif args.render:
        history, latest = load_history(HISTORY), load_latest(LATEST)
        if not history:
            logging.error(
                "履歴がありません。まず `python main.py` で1回巡回するか、"
                "`--demo` で見た目だけ確認してください。"
            )
            return 1
    else:
        latest, recorded_slugs, failed = collect(
            catalog, fetcher(), HISTORY, now, record=args.record
        )
        recorded = len(recorded_slugs)
        save_latest(LATEST, latest)
        history = load_history(HISTORY)

        if failed == len(catalog.tools):
            # 全滅はネットワーク断かIPブロック。古い履歴で上書き生成すると
            # 「全ツールが同時に確認不能」という異常な見た目のサイトが公開される。
            logging.error("全%d件の取得に失敗しました。今回の生成は中止します。", failed)
            return 1
        if args.record:
            logging.info("巡回完了: 変更%d件を記録 / 失敗%d件", recorded, failed)
        else:
            logging.info(
                "巡回完了: 変更%d件を検出(未記録) / 失敗%d件"
                " — 履歴を書けるのはCIだけです(--record)",
                recorded,
                failed,
            )

    # ---- 履歴 → ページ ----
    states = {
        tool.slug: build_state(
            slug=tool.slug,
            plan_names=tool.plans,
            snaps=history.get(tool.slug, []),
            latest_entry=latest.get(tool.slug, {}),
            now=now,
            stale_after_days=gen["stale_after_days"],
        )
        for tool in catalog.tools
    }

    tool_pages = build_tool_pages(catalog, states)
    compare_pages = build_compare_pages(catalog, tool_pages)
    feed = build_feed(tool_pages, gen["recent_changes"])
    all_changes = build_feed(tool_pages, 10_000)

    render_site(
        catalog=catalog,
        tool_pages=tool_pages,
        compare_pages=compare_pages,
        feed=feed,
        all_changes=all_changes,
        cfg=cfg,
        base_url=base_url,
        out_dir=args.out,
        now=now,
        google_site_verification=os.getenv("GOOGLE_SITE_VERIFICATION", ""),
        demo=args.demo,
    )

    # ---- 価格が動いたページだけ検索エンジンに通知する ----
    # 内容が変わっていないURLを毎日送るのは IndexNow の仕様違反になるので、
    # 記録した slug がある日だけ、そのページと一覧ページを送る。
    if args.ping and args.record:
        key = str((cfg.get("indexnow") or {}).get("key") or "")
        urls = changed_urls(base_url, recorded_slugs)
        if not key:
            logging.warning("indexnow.key が未設定のため通知しません")
        elif not urls:
            logging.info("価格に変更が無いので IndexNow への通知は省略します")
        else:
            ok, detail = submit(urls, key, f"{base_url.rstrip('/')}/{key}.txt")
            if ok:
                logging.info("IndexNow に通知しました: %s", detail)
            else:
                # 通知の失敗でサイトの更新まで止める理由はない
                logging.warning("IndexNow への通知に失敗: %s", detail)

    monetized = len(catalog.monetized)
    print(
        f"\n✅ {len(tool_pages)}ツール / {len(compare_pages)}比較ページを"
        f" {args.out} に生成しました"
    )
    print(f"   ローカル確認: python -m http.server -d {args.out} 8000")
    if not monetized and not args.demo:
        # 動いているのに1円も入らない状態。気づかないまま数ヶ月経つのが最悪なので毎回言う
        print(
            "\n⚠️  成果リンクが1本も設定されていません。収益は発生しません。\n"
            "   config/tools.yaml の affiliate.url を埋めてください（README 4章）。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
