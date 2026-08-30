"""IndexNow で検索エンジンにURLの更新を通知する。

【なぜ必要か】
  Google はリンクを辿ってサイトを見つける。外部リンクが1本も無い新規サイトは、
  サイトマップを送っても「まだ知らないサイト」のままクロールされない。
  実際このサイトは公開から3週間、Google が一度も取得しに来なかった。

  IndexNow は「こちらから通知する」プロトコルで、アカウントも審査も要らない。
  Bing / Yandex / Seznam / Naver が参加していて、1回の送信で全部に届く。
  Google は参加していないが、Bing は ChatGPT の検索基盤でもあるので、
  AIツールの価格を扱うこのサイトとは相性が良い。

【送りすぎない】
  内容が変わっていないURLを毎日送るのは仕様違反(スパム扱い)になる。
  価格改定を記録した日だけ、変わったページを送る。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ENDPOINT = "https://api.indexnow.org/indexnow"
# 1回に送れる上限。仕様上は10,000だが、このサイトは全ページでも30程度
MAX_URLS = 10_000
TIMEOUT = 20


class IndexNowError(Exception):
    """送信に失敗した。サイトの生成自体は妨げない。"""


def submit(urls: list[str], key: str, key_location: str) -> tuple[bool, str]:
    """URLの更新を通知する。(成功したか, 説明) を返す。

    キーファイルはホストのルートでなくてもよい。その場合 keyLocation を
    必ず添えること(Bing公式ドキュメントの "other locations within the same host")。
    このサイトは harypary.github.io の /tierlog/ 配下にしか書き込めないので、
    キーもそこに置いている。
    """
    urls = [u for u in dict.fromkeys(urls) if u.startswith("https://")][:MAX_URLS]
    if not urls:
        return False, "送信対象のURLがありません"

    host = urlsplit(urls[0]).netloc
    payload = {
        "host": host,
        "key": key,
        "keyLocation": key_location,
        "urlList": urls,
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status = response.status
    except urllib.error.HTTPError as e:
        # 400=不正なリクエスト, 403=キー不一致, 422=URLがホストと不一致, 429=多すぎ
        return False, f"HTTP {e.code} — {e.reason}"
    except urllib.error.URLError as e:
        return False, f"到達できません — {e.reason}"

    # 200=受理, 202=受理したがキーは未検証。どちらも成功として扱う
    if status in (200, 202):
        return True, f"HTTP {status} — {len(urls)}件を送信"
    return False, f"HTTP {status}"


def changed_urls(base_url: str, slugs: list[str]) -> list[str]:
    """価格が動いたツールのページと、それを載せている一覧ページ。

    トップと変更ログは、どのツールが動いても内容が変わるので必ず含める。
    逆に、何も動いていない日は空リストを返す = 送信しない。
    """
    if not slugs:
        return []
    base = base_url.rstrip("/")
    return [f"{base}/", f"{base}/changes/"] + [f"{base}/tools/{s}/" for s in sorted(slugs)]
