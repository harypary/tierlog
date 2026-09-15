"""価格ページのHTMLから価格を取り出す。

【設計の前提】完璧なパースは不可能だと最初から認めている
  相手のページは予告なく構造が変わるし、SPAだと価格がHTMLに載っていないこともある。
  そこでこのモジュールは2段構えにしてある。

  1. signature (堅い)  … ページ上の価格トークン集合のハッシュ。
                          個々のプランを正しく取れなくても「何かが変わった」は
                          高い精度で検出できる。サイトの中核はこちら。
  2. plans (best-effort) … プラン名の近傍から金額を拾う。外すことがある。
                          外したら confidence を下げ、表示側で伏せる。

  取れなかったものを推測で埋めない。価格サイトで嘘の数字を出したら終わりなので、
  「分からない」を「分からない」のまま返すのがこのモジュールの責任。
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

# 価格表記の2形式:
#   $1,234.56  … 一般的な表記。直後が数字/記号なら誤検出なので除外し、
#                "$5M" のようなマーケティング文言も弾く
#   99 USD     … Surfer のように通貨記号を使わないページ向け。
#                これが無いと料金表の本体を丸ごと取りこぼす
_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?"
PRICE_RE = re.compile(
    rf"\$\s?({_NUM})(?![\d.,])(?!\s*[MKB]\b)"
    rf"|(?<![\w.]){_NUM}\s?(?=USD\b)(?P<bare>)",
    re.IGNORECASE,
)
_BARE_NUM_RE = re.compile(_NUM)

# 「$240 お得」のような割引額。プランの価格ではないので候補から外す。
# これを弾かないと、年払いの節約額を月額として掲載してしまう。
_SAVINGS_RE = re.compile(
    r"sav(?:e|ing)|discount|\boff\b|worth|value|credit|coupon|instead of|was\b",
    re.IGNORECASE,
)


def _amount(match: re.Match[str]) -> float:
    """マッチから金額を取り出す。$表記と 'NN USD' 表記の両方に対応する。"""
    raw = match.group(1)
    if raw is None:  # 'NN USD' 側
        raw = _BARE_NUM_RE.search(match.group(0)).group(0)
    return float(raw.replace(",", ""))


def _token(match: re.Match[str]) -> str:
    """シグネチャ用に表記ゆれを潰した金額文字列。"""
    amount = _amount(match)
    return f"${amount:g}"

_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_LDJSON_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_DROP_RE = re.compile(r"<(script|style|svg|noscript|template)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{2,}")

# 月額/年額の判定。プラン名からこの距離以内に出た表記を採用する。
# 課金周期の判定。ここは「その金額の単位は何か」だけを見る。
#
# 重要: "billed annually" / "billed yearly" は支払いをまとめる約束であって
# 単価の単位ではない。"$79 per month, billed annually" の $79 は月額。
# これを年額と解釈すると12倍ずれた価格を掲載することになる(実際に起きた)。
# したがって annually 系は年額の目印に含めない。
_MONTHLY_RE = re.compile(r"/\s?mo\b|/\s?month|per\s+month|a month|monthly", re.IGNORECASE)
_ANNUAL_RE = re.compile(r"/\s?yr\b|/\s?year\b|per\s+year|a year", re.IGNORECASE)
# 「Save $240/year」の /year は割引額の単位であって価格の単位ではない。
# 判定前にこの手の文言を窓から取り除く。
_SAVINGS_PHRASE_RE = re.compile(r"sav(?:e|ings?)\b[^|\n]{0,32}", re.IGNORECASE)
# _walk_json が出力する「price: $20」のような、価格だと宣言済みの行の頭。
# これが直前にあるなら、周期の表記が無くても価格として信用してよい。
_PRICE_LABEL_RE = re.compile(r"\b\w*(?:price|amount|cost)\w*\s*:\s*$", re.IGNORECASE)

# プラン名から金額を探す窓幅(文字)。
#   TIGHT … 「プラン名のすぐ隣に書いてある」と言える距離。これだけを採用する
#   FAR   … ここまでは探すが、採用は課金周期の表記を伴う場合に限る
# 実データで検証した結果、この2段構えでないと隣のカードの価格や
# 機能説明中の数字を掴む。緩めるほど掲載数は増えるが、増える分は嘘になる。
TIGHT = 60  # プラン名から金額までの上限。これを超えたら課金周期の表記が必要
FAR = 400  # 探索そのものを打ち切る幅

# 月額サブスクとして妥当な範囲(USD)。この外は掲載しない。
# beehiiv で $0.3、ClickUp で $750 のような明らかな誤検出が出たため。
MIN_PRICE = 3.0
MAX_PRICE = 5000.0  # copy-ai の Scale が $3,000/月。この規模の実在プランを弾かない


@dataclass(frozen=True)
class PlanPrice:
    plan: str
    amount: float | None  # None = ページ上に見つからなかった
    raw: str = ""
    period: str = ""  # month | year | ""
    confidence: str = "none"  # high | low | none
    # この価格を最後に読めた時刻。表示用の状態を組み立てるとき(track.build_state)
    # にだけ入る。抽出した直後は None
    verified_at: datetime | None = None

    @property
    def is_free(self) -> bool:
        return self.amount == 0.0

    def display(self) -> str:
        if self.amount is None:
            return "—"
        if self.amount == 0:
            return "Free"
        suffix = {"month": "/mo", "year": "/yr"}.get(self.period, "")
        amount = f"{self.amount:,.2f}".rstrip("0").rstrip(".")
        return f"${amount}{suffix}"


@dataclass(frozen=True)
class Extraction:
    ok: bool
    signature: str = ""
    tokens: tuple[str, ...] = ()
    plans: tuple[PlanPrice, ...] = ()
    method: str = "none"  # text | jsonld | embedded-json | mixed
    note: str = ""
    _corpus_len: int = field(default=0, repr=False)

    @property
    def resolved_plans(self) -> tuple[PlanPrice, ...]:
        return tuple(p for p in self.plans if p.amount is not None)


# JSON中の「これは価格だ」と断定できるキー。値が数値ならそのまま採用できる。
# 本文からの近傍探索と違って推測が入らないので、ここで拾えた分が最も確実。
_PRICE_KEY_RE = re.compile(
    r"^(?:price|amount|cost|monthly|yearly|annual)"
    r"|(?:price|amount|cost)(?:incents|indollars|permonth|peryear|permonth)?$",
    re.IGNORECASE,
)
_PERIOD_KEY_RE = re.compile(r"month|annual|year", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"\d+(?:\.\d+)?")
# 名前からして無料であるプラン。"Free" "Free Forever" など
_FREE_PLAN_RE = re.compile(r"\bfree\b", re.IGNORECASE)


def _walk_json(obj: object, out: list[str], depth: int = 0) -> None:
    """JSONを "key: value" の行に平坦化する。

    プラン名とその価格が元の構造で隣接していれば、平坦化後も近い行に並ぶ。
    これで可視テキストと同じ窓探索がそのまま使える。

    価格と断定できるキーの数値には $ を付けて出す。こうすると
    「プラン名 → $金額」が最短距離で並び、本文中の関係ない数字より
    先に採用される。Semrush のように本文に価格が無く構造化データにだけ
    載っているページは、これが無いと正しい値を取れない。
    """
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k)
            # JSON-LD の price は数値ではなく文字列 "20" で書かれることが多い
            # (schema.org の例がそうなっている)。型で弾くと丸ごと取り逃がす。
            if isinstance(v, str) and _NUMERIC_RE.fullmatch(v.strip()):
                v = float(v.strip())
            is_number = isinstance(v, (int, float)) and not isinstance(v, bool)
            if is_number and _PRICE_KEY_RE.search(key):
                # セント単位で持つ実装が多い。1000以上かつ整数なら100で割る
                amount = float(v)
                if amount >= 1000 and amount == int(amount) and amount % 100 == 0:
                    amount /= 100
                period = "/month" if _PERIOD_KEY_RE.search(key) else ""
                out.append(f"{key}: ${amount:g}{period}")
            elif isinstance(v, (str, int, float)) and v not in (None, ""):
                out.append(f"{key}: {v}")
            else:
                out.append(f"{key}:")
                _walk_json(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:  # 巨大配列で膨らませない
            _walk_json(v, out, depth + 1)
    elif isinstance(obj, (str, int, float)):
        out.append(str(obj))


def _json_corpus(html: str) -> tuple[str, bool, bool]:
    """埋め込みJSONを平坦化して返す。(テキスト, JSON-LDあり, その他JSONあり)"""
    lines: list[str] = []
    has_ldjson = False
    has_other = False

    for match in _LDJSON_RE.finditer(html):
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        has_ldjson = True
        _walk_json(data, lines)

    for match in _SCRIPT_RE.finditer(html):
        body = match.group(1).strip()
        # JSONそのものを埋め込んでいる script(__NEXT_DATA__ など)だけを拾う
        if not body.startswith(("{", "[")):
            continue
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if "$" not in body and "price" not in body.lower():
            continue
        has_other = True
        _walk_json(data, lines)

    return "\n".join(lines), has_ldjson, has_other


_UNICODE_ESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
# 1ページから取り込むストリームの上限。料金ページで1MBを超える分は
# まず本文ではないので、CIの実行時間を守るために切る。
_STREAM_LIMIT = 1_000_000


def _stream_corpus(html: str) -> str:
    """Next.js App Router などが流し込む「JSONではないがJSONを含む」script を救う。

    これらは self.__next_f.push([1,"...エスケープされたJSON..."]) の形なので
    json.loads では読めない。OpenAI のようにHTMLに価格が一切無く、
    ここにしか載っていないサイトがあるため、素の文字列として展開して拾う。
    ノイズを混ぜないよう、展開後に価格表記があるものだけ採用する。
    """
    chunks: list[str] = []
    total = 0
    for match in _SCRIPT_RE.finditer(html):
        body = match.group(1)
        if "$" not in body or body.lstrip().startswith(("{", "[")):
            continue  # JSONそのものは _json_corpus が既に処理している
        text = _UNICODE_ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), body)
        text = text.replace('\\"', '"').replace("\\/", "/").replace("\\n", "\n")
        if not PRICE_RE.search(text):
            continue
        chunks.append(text)
        total += len(text)
        if total > _STREAM_LIMIT:
            break
    return "\n".join(chunks)


def _visible_text(html: str) -> str:
    text = _DROP_RE.sub("\n", html)
    text = _TAG_RE.sub("\n", text)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    return _BLANK_RE.sub("\n", text).strip()


def _period_near(corpus: str, at: int) -> str:
    window = _SAVINGS_PHRASE_RE.sub(" ", corpus[at : at + 60])
    # 月額の表記があればそれが単位。年払いの案内が併記されていても月額のまま。
    # 順序が逆だと "$33/mo ... billed yearly" を年額と読んでしまう。
    if _MONTHLY_RE.search(window):
        return "month"
    if _ANNUAL_RE.search(window):
        return "year"
    return ""


def _price_after(corpus: str, start: int, end: int) -> re.Match[str] | None:
    """[start, end) の範囲で、割引額ではない最初の金額を返す。

    「Save」が効くのは直後の1つの金額だけ、と考える。
    文字数で遡ると、区切り文字の入り方次第で次の本物の価格まで巻き込む。
    実際「Save $240 → $99/mo」の $99 を落とした。
    そこで「直前の金額から今の金額までの間」だけを見る。
    """
    pos = start
    previous_end = start
    while pos < end:
        match = PRICE_RE.search(corpus, pos, end)
        if match is None:
            return None
        between = corpus[previous_end : match.start()]
        if not _SAVINGS_RE.search(between):
            return match
        previous_end = match.end()
        pos = match.end()
    return None


def _plan_pattern(plan: str) -> re.Pattern[str]:
    """プラン名を単語境界で照合するパターン。

    部分一致にすると "Pro" が "products" や "Professional" にヒットし、
    無関係な数字をそのプランの価格として掲載してしまう。
    ただし単語境界は端が英数字のときだけ付ける。"Pro+" の末尾に \\b を付けると
    記号の後に単語文字を要求することになり、絶対にマッチしなくなる。
    """
    left = r"\b" if plan[:1].isalnum() else ""
    right = r"\b" if plan[-1:].isalnum() else ""
    return re.compile(left + re.escape(plan) + right, re.IGNORECASE)


def _rank(p: PlanPrice) -> int:
    return {"high": 2, "low": 1, "none": 0}[p.confidence]


def _accept(
    corpus: str, plan: str, match: re.Match[str], distance: int, limit: int
) -> PlanPrice | None:
    """見つけた金額を「そのプランの価格」として採用してよいか判定する。

    ここが緩いと、掲載件数は増えるがサイトの信用が消える。
    採用条件は次のどちらか:
      - プラン名のすぐ隣(TIGHT以内)に書かれている
      - 少し離れていても "/mo" のような課金周期の表記を伴っている
    どちらでもない数字は、機能説明の中の数量や隣のカードの価格である
    可能性が高いので捨てる。
    """
    amount = _amount(match)
    # "Free" という名前のプランに有料の額が入ることは定義上ありえない。
    # 近傍探索が隣のカードに滑った証拠なので、値ごと捨てる。
    # (monday.com の Free に $10 が入って公開されたのがこれ)
    if amount != 0 and _FREE_PLAN_RE.search(plan):
        return None
    # $0 は捨てないこと。無料プランを表す正当な値であり、ここで弾くと
    # 探索が次のプランの価格まで滑り、無料プランに有料の額が入る
    # (Cursor の Hobby に Pro の $20 が入ったのがこれ)。
    if amount != 0 and not (MIN_PRICE <= amount <= MAX_PRICE):
        return None

    period = _period_near(corpus, match.end())

    # 構造化データの price キー直下の値は、周期の表記が無くても採用する。
    # ベンダー自身が「これは価格だ」と機械向けに宣言しているので、
    # 本文の近くに /mo と書いてあるかどうかを条件にする理由がない。
    # ここを要求していたせいで JSON-LD だけのページを取り逃していた。
    declared = _PRICE_LABEL_RE.search(corpus[max(0, match.start() - 24) : match.start()])

    if amount != 0 and not declared and (distance > limit or not period):
        return None

    return PlanPrice(
        plan=plan, amount=amount, raw=_token(match), period=period, confidence="high"
    )


def _find_plans(corpus: str, plans: tuple[str, ...], limit: int = TIGHT) -> dict[str, PlanPrice]:
    """全プランの価格を一度に決める。

    プランごとに独立して探すと、無料プランの探索窓が隣のカードまで伸びて
    「次のプランの価格」を拾ってしまう(Cursor の Hobby に Pro の $20 が入る等)。
    そこで全プラン名の位置を先に集め、次のプラン名が現れた時点で窓を閉じる。
    料金表は必ずカードが横に並ぶので、この境界が実質的にカードの境界になる。
    """
    marks: list[tuple[int, int, str]] = []
    for plan in plans:
        for m in _plan_pattern(plan).finditer(corpus):
            marks.append((m.start(), m.end(), plan))
    marks.sort()

    best: dict[str, PlanPrice] = {plan: PlanPrice(plan, None) for plan in plans}

    # 名前に Free が入っているプランは探索するまでもなく $0。
    # 探索させると隣のカードの有料価格を拾うことがあり、
    # 「無料プランが $10」という一目で分かる嘘を公開してしまう。
    #
    # ただし「ページに実在すること」が条件。設定に書いてあるだけで
    # 料金表に無いプランまで $0 と断定すると、無料プランが存在しない製品に
    # 「Free — $0」の行を捏造することになる(copy-ai で実際に起きた)。
    present = {plan for _, _, plan in marks}
    for plan in plans:
        if _FREE_PLAN_RE.search(plan) and plan in present:
            best[plan] = PlanPrice(plan, 0.0, "Free", "", "high")

    for i, (_, end, plan) in enumerate(marks):
        if _FREE_PLAN_RE.search(plan):
            continue  # 上で確定済み
        # 次に別のプラン名が出るところで窓を閉じる
        limit = len(corpus)
        for next_start, _, next_plan in marks[i + 1 :]:
            if next_plan != plan:
                limit = max(next_start, end)
                break
        window_end = min(end + FAR, limit)
        match = _price_after(corpus, end, window_end)

        if match:
            candidate = _accept(corpus, plan, match, match.start() - end, limit)
            if candidate is None:
                continue
        else:
            # 窓の中に価格が無いときだけ無料判定に落ちる。
            # 判定は "$0" が書いてあるか、プラン名自体が Free かの2つに限る。
            # 料金ページは "Start for free" "free trial" のような文言で
            # 埋まっているので、本文に free があることを根拠にすると
            # 有料プランが軒並み $0 になる(実際にそうなった)。
            tail = corpus[end:window_end].lower()
            # プラン名の直後に Free と書いてあるだけの無料プラン("Hobby | Free")。
            # 距離を厳しく切るのが要点で、広く取ると "start for free" を拾う
            adjacent_free = re.match(r"[\s|:·・-]{0,6}free\b", tail) is not None
            named_free = _FREE_PLAN_RE.search(plan) is not None
            if "$0" in tail or "0 usd" in tail or adjacent_free or named_free:
                candidate = PlanPrice(plan, 0.0, "Free", "", "high")
            else:
                continue

        if _rank(candidate) > _rank(best[plan]):
            best[plan] = candidate

    return {plan: price for plan, price in best.items() if price.amount is not None}


def _find_by_rules(corpus: str, rules: dict[str, str]) -> dict[str, PlanPrice]:
    """tools.yaml の patterns で明示された取り出し方を適用する。

    近傍探索はページの作りに勝てないことがある(無料プランなのに隣のカードの
    価格を拾う、機能表の数量を価格と誤認する等)。そういうツールは
    config 側に「このプランはこの正規表現で取る」と書いて上書きする。
    ここで取れた値は推測を含まないので、常に近傍探索より優先する。

    パターンには金額を捉える groupを1つだけ書くこと。例:
      Hobby: 'Hobby[^$]{0,40}\\$\\s?(\\d+)'
    """
    found: dict[str, PlanPrice] = {}
    for plan, pattern in rules.items():
        try:
            match = re.search(pattern, corpus, re.IGNORECASE | re.DOTALL)
        except re.error:
            # 設定の書き間違いで巡回全体を落とさない。catalog 側で検証済み
            continue
        if not match or not match.groups():
            continue
        raw = (match.group(1) or "").replace(",", "").replace("$", "").strip()
        try:
            amount = float(raw)
        except ValueError:
            continue
        if amount != 0 and not (MIN_PRICE <= amount <= MAX_PRICE):
            continue
        found[plan] = PlanPrice(
            plan=plan,
            amount=amount,
            raw=f"${amount:g}",
            period=_period_near(corpus, match.end()),
            confidence="high",
        )
    return found


def extract(
    html: str, plans: tuple[str, ...], rules: dict[str, str] | None = None
) -> Extraction:
    """HTMLから価格シグネチャとプラン別価格を取り出す。"""
    if not html.strip():
        return Extraction(ok=False, note="HTMLが空です")

    json_text, has_ldjson, has_other = _json_corpus(html)
    visible = _visible_text(html)
    parts = [p for p in (visible, json_text) if p]

    # 可視テキストと通常のJSONで価格が1つも取れなかったときだけ、
    # ストリームまで踏み込む。先に混ぜると、まともなページでも
    # 内部データの古い価格を拾ってしまう危険がある。
    stream_text = ""
    if not PRICE_RE.search("\n".join(parts)):
        stream_text = _stream_corpus(html)
        if stream_text:
            parts.append(stream_text)
    stream_used = bool(stream_text)

    corpus = "\n".join(parts)

    tokens = tuple(_token(m) for m in PRICE_RE.finditer(corpus))
    if not tokens:
        return Extraction(
            ok=False,
            _corpus_len=len(corpus),
            note=(
                "ページ内に価格らしい表記が見つかりませんでした。"
                "JavaScriptで価格を描画している可能性があります"
            ),
        )

    # シグネチャは「出現順」ではなく「集合」から作る。
    # A/Bテストやカードの並べ替えで順序が入れ替わっただけの変更を
    # 値上げとして誤検出しないため。
    unique = sorted({t for t in tokens}, key=lambda s: (len(s), s))
    signature = hashlib.sha256("|".join(unique).encode()).hexdigest()[:16]

    # 価格は「根拠の強い順」に決める。弱い根拠は強い根拠を上書きしない。
    #
    #   1. patterns   … 運用者が明示した取り出し方。推測ゼロ
    #   2. 構造化データ … JSON-LD / 埋め込みJSON。キーと値の対応が機械向けに
    #                    書かれているので、隣接関係に意味がある
    #   3. 本文       … 最後の手段。プラン名の「すぐ横」に課金周期付きで
    #                    書かれている場合だけ採る
    #
    # 3 の距離を緩めると掲載件数は増えるが、増えた分は隣のカードの価格や
    # 機能表の数量になる。実データで確認済みなので広げないこと。
    resolved = _find_plans(corpus, plans, TIGHT)
    if rules:
        resolved.update(_find_by_rules(corpus, rules))

    found = tuple(resolved.get(plan, PlanPrice(plan, None)) for plan in plans)

    if stream_used:
        method = "next-stream"
    elif has_ldjson and has_other:
        method = "mixed"
    elif has_ldjson:
        method = "jsonld"
    elif has_other:
        method = "embedded-json"
    else:
        method = "text"

    resolved = sum(1 for p in found if p.amount is not None)
    note = "" if resolved else "価格トークンは見つかりましたがプラン名と対応付けられませんでした"

    return Extraction(
        ok=True,
        signature=signature,
        tokens=tuple(unique),
        plans=found,
        method=method,
        note=note,
        _corpus_len=len(corpus),
    )
