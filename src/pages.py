"""履歴と設定から、テンプレートに渡すページ単位のデータを組み立てる。

英語の文面をテンプレートではなくここで作っているのは、
「$49 → $69 に上がった」を "raised" と書くか "increased" と書くかが
データの中身(符号)に依存するため。表示のロジックはPython側に寄せてある。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .catalog import Catalog, Tool
from .track import (
    KIND_ADDED,
    KIND_DECREASE,
    KIND_FIRST,
    KIND_INCREASE,
    KIND_PAGE,
    KIND_REMOVED,
    Change,
    ToolState,
)


@dataclass(frozen=True)
class ToolPage:
    tool: Tool
    state: ToolState
    category_name: str

    @property
    def path(self) -> str:
        return f"tools/{self.tool.slug}/"

    @property
    def title(self) -> str:
        return f"{self.tool.name} Pricing — Current Plans and Price History"

    @property
    def description(self) -> str:
        if self.state.stale or not self.state.has_prices:
            return (
                f"{self.tool.name} pricing plans, tracked daily. "
                "See every price change we have recorded since we started tracking."
            )
        cheapest = min(
            (p for p in self.state.plans if p.amount not in (None, 0)),
            key=lambda p: p.amount,
            default=None,
        )
        lead = f"{self.tool.name} starts at {cheapest.display()}. " if cheapest else ""
        return (
            f"{lead}Checked daily against the official pricing page. "
            f"{len(self.state.changes) - 1} changes recorded so far."
        )


@dataclass(frozen=True)
class ComparePage:
    left: ToolPage
    right: ToolPage

    @property
    def slug(self) -> str:
        return f"{self.left.tool.slug}-vs-{self.right.tool.slug}"

    @property
    def path(self) -> str:
        return f"compare/{self.slug}/"

    @property
    def title(self) -> str:
        return f"{self.left.tool.name} vs {self.right.tool.name} — Pricing Compared"

    @property
    def description(self) -> str:
        return (
            f"Side-by-side pricing for {self.left.tool.name} and {self.right.tool.name}, "
            "refreshed daily from both official pricing pages, with the price history of each."
        )


@dataclass(frozen=True)
class FeedItem:
    """トップページとRSSに出す1件の変更。"""

    change: Change
    tool: Tool

    @property
    def when(self) -> datetime:
        return self.change.when

    @property
    def headline(self) -> str:
        return headline_for(self.change, self.tool)

    @property
    def path(self) -> str:
        return f"tools/{self.tool.slug}/"

    @property
    def badge(self) -> str:
        # 種別が増えたときに全ページの生成を落とさない
        return {
            KIND_INCREASE: "Price up",
            KIND_DECREASE: "Price down",
            KIND_ADDED: "Now tracked",
            KIND_REMOVED: "Delisted",
            KIND_PAGE: "Page changed",
            KIND_FIRST: "Tracking started",
        }.get(self.change.kind, "Updated")


def _period(change: Change) -> str:
    return {"month": "/mo", "year": "/yr"}.get(change.period, "")


def _amounts(tokens: tuple[str, ...], limit: int = 3) -> str:
    if len(tokens) <= limit:
        return ", ".join(tokens)
    return f"{', '.join(tokens[:limit])} and {len(tokens) - limit} other amounts"


def headline_for(change: Change, tool: Tool) -> str:
    name = tool.name
    unit = _period(change)

    if change.kind == KIND_INCREASE:
        pct = change.pct
        tail = f" ({pct:+.0f}%)" if pct is not None and abs(pct) >= 1 else ""
        return (
            f"{name} raised {change.plan} from {change.before_display} "
            f"to {change.after_display}{unit}{tail}"
        )
    if change.kind == KIND_DECREASE:
        pct = change.pct
        tail = f" ({pct:+.0f}%)" if pct is not None and abs(pct) >= 1 else ""
        return (
            f"{name} cut {change.plan} from {change.before_display} "
            f"to {change.after_display}{unit}{tail}"
        )
    # 「ベンダーがプランを追加した」とは書かないこと。
    # プランが履歴に現れる理由は2つあり、区別できない:
    #   (a) ベンダーが本当に新設した
    #   (b) こちらの抽出が改善して、前からあったものを読めるようになった
    # 実際 beehiiv の Scale/Max は (b) だったのに「added」と公開してしまった。
    # どちらでも真である「追跡し始めた」という言い方にする。
    if change.kind == KIND_ADDED:
        if change.after == 0:
            return f"Now tracking {name} {change.plan} — free"
        return f"Now tracking {name} {change.plan} at {change.after_display}{unit}"
    if change.kind == KIND_REMOVED:
        # 消えた理由も同様に断定できない(掲載終了か、読めなくなったか)
        return f"{name} {change.plan} is no longer listed"
    if change.kind == KIND_PAGE:
        # プラン単位まで特定できていないので、どのプランの値段かは言わない。
        # 「値上げした」と書いて外れると、このサイトの存在意義が消える。
        # 言えるのは「ページに現れた金額・消えた金額」までで、
        # それは自分の記録どうしの比較なので断定してよい。
        parts = []
        if change.appeared:
            parts.append(f"now shows {_amounts(change.appeared)}")
        if change.disappeared:
            parts.append(f"no longer shows {_amounts(change.disappeared)}")
        if parts:
            return f"{name}'s pricing page {' and '.join(parts)}"
        return f"{name} changed its pricing page"
    return f"Started tracking {name}"


def build_tool_pages(
    catalog: Catalog, states: dict[str, ToolState]
) -> list[ToolPage]:
    return [
        ToolPage(
            tool=tool,
            state=states[tool.slug],
            category_name=catalog.categories[tool.category],
        )
        for tool in catalog.tools
        if tool.slug in states
    ]


def build_compare_pages(catalog: Catalog, pages: list[ToolPage]) -> list[ComparePage]:
    by_slug = {p.tool.slug: p for p in pages}
    out: list[ComparePage] = []
    for left, right in catalog.comparisons:
        if left in by_slug and right in by_slug:
            out.append(ComparePage(left=by_slug[left], right=by_slug[right]))
    return out


def build_feed(pages: list[ToolPage], limit: int) -> list[FeedItem]:
    """全ツールの変更を新しい順に混ぜる。トップページの主役。"""
    items = [
        FeedItem(change=change, tool=page.tool)
        for page in pages
        for change in page.state.changes
    ]
    items.sort(key=lambda i: i.change.ts, reverse=True)

    # 初回記録は全ツール分が同じ日に並んで邪魔になるので、
    # 実質的な変更が十分あるときだけ隠す。
    real = [i for i in items if i.change.kind != KIND_FIRST]
    return (real if len(real) >= limit else items)[:limit]


def grouped_by_category(
    catalog: Catalog, pages: list[ToolPage]
) -> list[tuple[str, list[ToolPage]]]:
    """カテゴリ順は tools.yaml の categories の並びに従う。"""
    grouped: list[tuple[str, list[ToolPage]]] = []
    for key, label in catalog.categories.items():
        group = [p for p in pages if p.tool.category == key]
        if group:
            grouped.append((label, group))
    return grouped


def related(page: ToolPage, pages: list[ToolPage], limit: int = 6) -> list[ToolPage]:
    """内部リンク。同カテゴリを優先し、足りなければ収益化済みツールで埋める。

    収益化済みを埋め草に使うのは意図的。集客担当のページ(ChatGPT等)から
    成果リンクのあるページへ導線を通すのがこのサイトの収益構造そのもの。
    """
    same = [p for p in pages if p.tool.category == page.tool.category and p is not page]
    earners = [
        p
        for p in pages
        if p.tool.affiliate.is_monetized and p is not page and p not in same
    ]
    others = [p for p in pages if p is not page and p not in same and p not in earners]
    return (same + earners + others)[:limit]
