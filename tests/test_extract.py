"""価格抽出の回帰テスト。

ここにあるケースは全部、**実際に誤った価格を公開してしまった事故**から来ている。
思いつきのテストではないので、失敗したら必ず原因を直すこと。
テストの方を緩めると、同じ嘘をもう一度公開することになる。

    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extract import extract


def prices(html: str, plans: tuple[str, ...], rules: dict | None = None) -> dict:
    """プラン名 → 金額 の辞書。取れなかったものは None。"""
    return {p.plan: p.amount for p in extract(f"<html><body>{html}</body></html>", plans, rules).plans}


def periods(html: str, plans: tuple[str, ...]) -> dict:
    return {p.plan: p.period for p in extract(f"<html><body>{html}</body></html>", plans).plans}


# ---------------------------------------------------------------
# 事故1: 「$79/月・年払い」を年額として掲載した(12倍のずれ)
# ---------------------------------------------------------------
def test_billed_annually_is_not_a_yearly_unit():
    """「per month, billed annually」の金額は月額。年払いは支払い方法の話。"""
    html = "<div>Starter</div><div>$79</div><div>Save $240/year</div><div>per month, billed annually</div>"
    assert periods(html, ("Starter",))["Starter"] == "month"


def test_monthly_marker_beats_nearby_yearly_text():
    """「$33/mo … $390 billed yearly … Save $78 per year」は月額$33。"""
    html = "<div>Creator</div><div>$33</div><div>/mo</div><div>$390 billed yearly</div><div>Save $78 per year</div>"
    assert periods(html, ("Creator",))["Creator"] == "month"


def test_genuine_yearly_unit_is_kept():
    """本当に年額単位なら year のまま。上の修正で潰していないことを確認する。"""
    html = "<div>Annual</div><div>$288</div><div>per year</div>"
    assert periods(html, ("Annual",))["Annual"] == "year"


# ---------------------------------------------------------------
# 事故2: 割引額を価格として掲載した
# ---------------------------------------------------------------
def test_savings_amount_is_not_the_price():
    """「Save $240」の $240 を価格にしない。"""
    html = "<div>Standard</div><div>Save $240</div><div>$99</div><div>per month</div>"
    assert prices(html, ("Standard",))["Standard"] == 99.0


def test_price_right_after_a_savings_figure_is_still_found():
    """割引額の直後にある本当の価格を取り逃さない(beehiiv で 2プラン落とした)。"""
    html = "<div>Scale</div><div>Save $71/year</div><div>$43</div><div>per month</div>"
    assert prices(html, ("Scale",))["Scale"] == 43.0


# ---------------------------------------------------------------
# 事故3: 無料プランに隣のカードの有料価格が入った
# ---------------------------------------------------------------
def test_free_named_plan_never_gets_a_paid_price():
    """monday.com の Free に隣の $10 が入って公開された。"""
    html = "<div>Free</div><div>$10</div><div>per month</div><div>Basic</div><div>$18</div><div>per month</div>"
    got = prices(html, ("Free", "Basic"))
    assert got["Free"] == 0.0
    assert got["Basic"] == 18.0


def test_free_forever_is_also_free():
    html = "<div>Free Forever</div><div>Unlimited</div><div>$7</div><div>per month</div>"
    got = prices(html, ("Free Forever", "Unlimited"))
    assert got["Free Forever"] == 0.0
    assert got["Unlimited"] == 7.0


def test_freelancer_is_not_a_free_plan():
    """語境界。'Freelancer' は Free を含むが無料ではない。"""
    html = "<div>Freelancer</div><div>$29</div><div>per month</div>"
    assert prices(html, ("Freelancer",))["Freelancer"] == 29.0


# ---------------------------------------------------------------
# 事故4: ページに存在しない無料プランを捏造した
# ---------------------------------------------------------------
def test_free_plan_absent_from_page_is_not_invented():
    """copy-ai は無料プランが無いのに「Free — $0」を出していた。"""
    html = "<div>Chat</div><div>$29</div><div>per month</div>"
    assert prices(html, ("Free", "Chat"))["Free"] is None


# ---------------------------------------------------------------
# 事故5: プラン名の部分一致で無関係な数字を拾った
# ---------------------------------------------------------------
def test_plan_name_matches_on_word_boundary():
    """'Pro' が 'products' や 'Professional' にヒットしないこと。"""
    html = "<div>Our products start at $5 per month for professionals</div><div>Pro</div><div>$30</div><div>per month</div>"
    assert prices(html, ("Pro",))["Pro"] == 30.0


def test_plan_name_ending_in_symbol_is_found():
    """'Pro+' の末尾に \\b を付けると絶対にマッチしなくなる。"""
    html = "<div>Pro+</div><div>$39</div><div>per month</div>"
    assert prices(html, ("Pro+",))["Pro+"] == 39.0


# ---------------------------------------------------------------
# 採用基準そのもの
# ---------------------------------------------------------------
def test_far_number_without_a_period_is_rejected():
    """遠くにある課金周期なしの数字は採用しない(機能表の数量など)。"""
    html = "<div>Pro</div>" + "<div>filler text here</div>" * 12 + "<div>$500</div>"
    assert prices(html, ("Pro",))["Pro"] is None


def test_implausible_amounts_are_rejected():
    html = "<div>Pro</div><div>$0.30</div><div>per month</div>"
    assert prices(html, ("Pro",))["Pro"] is None


def test_next_plan_name_closes_the_window():
    """無料プランの探索窓が隣のカードまで伸びない(Cursor の Hobby)。"""
    html = "<div>Hobby</div><div>Includes everything to start</div><div>Pro</div><div>$20</div><div>per month</div>"
    got = prices(html, ("Hobby", "Pro"))
    assert got["Pro"] == 20.0
    assert got["Hobby"] != 20.0


# ---------------------------------------------------------------
# 構造化データと patterns
# ---------------------------------------------------------------
def test_jsonld_string_prices_are_read():
    """JSON-LD の price は数値ではなく文字列 "20" で書かれることが多い。"""
    html = """<script type="application/ld+json">
    {"@type":"Product","offers":[
      {"@type":"Offer","name":"Pro","price":"20","priceCurrency":"USD"},
      {"@type":"Offer","name":"Teams","price":"40","priceCurrency":"USD"}]}
    </script><div>Pro</div><div>Teams</div>"""
    got = prices(html, ("Pro", "Teams"))
    assert got["Pro"] == 20.0
    assert got["Teams"] == 40.0


def test_patterns_override_the_heuristic():
    """config の patterns は近傍探索より必ず優先される。"""
    html = "<div>Pro</div><div>$99</div><div>per month</div><div>real: $30</div>"
    got = prices(html, ("Pro",), {"Pro": r"real: \$(\d+)"})
    assert got["Pro"] == 30.0


def test_claude_team_standard_seat_pattern():
    """2026-09-04 の作り替えで Team の見出しと価格が離れ、11日間読めていなかった。

    config の patterns そのものを検証する。同じカードにある月払いの $25 と、
    Premium seat の $100 を掴まないこと。
    """
    import yaml

    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "config" / "tools.yaml").read_text(encoding="utf-8"))
    rules = next(t for t in cfg["tools"] if t["slug"] == "claude")["patterns"]
    html = (
        "<h3>Team</h3><p>For teams of 2 to 150</p><a>Get Team plan</a>"
        "<h4>Standard seat</h4><p>All Claude features, plus more usage than Pro*</p>"
        "<span>$20</span><p>Per seat / month if billed annually. $25 if billed monthly.</p>"
        "<h4>Premium seat</h4><p>5x more usage than standard seats*</p>"
        "<span>$100</span><p>Per seat / month if billed annually. $125 if billed monthly.</p>"
    )
    (team,) = extract(f"<html><body>{html}</body></html>", ("Team",), rules).plans
    assert (team.amount, team.period) == (20.0, "month")


def test_bare_usd_notation_is_read():
    """「99 USD per month」のように通貨記号を使わないページ(Surfer)。"""
    html = "<div>Standard</div><div>99</div><div>USD</div><div>per month</div>"
    assert prices(html, ("Standard",))["Standard"] == 99.0


# ---------------------------------------------------------------
# シグネチャ(変更検出の土台)
# ---------------------------------------------------------------
def test_signature_ignores_card_order():
    """カードの並べ替えを値上げと誤検出しない。"""
    a = extract("<html><body>$10 $20 $30</body></html>", ())
    b = extract("<html><body>$30 $10 $20</body></html>", ())
    assert a.signature == b.signature


def test_signature_changes_when_a_price_changes():
    a = extract("<html><body>$10 $20</body></html>", ())
    b = extract("<html><body>$10 $25</body></html>", ())
    assert a.signature != b.signature


def test_no_prices_is_not_ok():
    """価格が1つも無いページは失敗として扱う(JS描画のページなど)。"""
    assert not extract("<html><body>No pricing here</body></html>", ("Pro",)).ok
