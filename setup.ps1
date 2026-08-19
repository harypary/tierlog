# ============================================================
#  リポジトリ作成から公開・毎日自動巡回まで、これ1本で終わらせるスクリプト
#
#    .\setup.ps1                          # 既定のリポジトリ名で公開
#    .\setup.ps1 -RepoName my-tracker     # 名前を指定
#    .\setup.ps1 -SkipCrawl               # 初回巡回を省略(テンプレート修正後の再実行用)
#
#  事前に `gh auth login` だけ済ませておくこと。APIキーの取得は不要。
# ============================================================

param(
    # 公開リポジトリ名。そのまま公開URLになる。
    # 追跡対象ベンダーの商標(openai / notion など)をURLに含めないこと。
    # 独立したトラッカーであることが名前から分かるものにする。
    [string]$RepoName = "tierlog",
    [switch]$SkipCrawl
)

$ErrorActionPreference = "Stop"
# PowerShell 7.4+ は既定で「外部コマンドの非0終了」も例外にする。
# このスクリプトは git/gh の終了コードを自前で判定しているので無効化しておく
$PSNativeCommandUseErrorActionPreference = $false
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)       { Write-Host "    OK  $msg" -ForegroundColor Green }
function Warn($msg)     { Write-Host "    !   $msg" -ForegroundColor Yellow }
function Fail($msg)     { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

# ---- 1. 前提の確認 ------------------------------------------
Step 1 "前提を確認"

gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "GitHub CLI が未認証です。先に 'gh auth login' を実行してください。" }

$owner = (gh api user --jq .login)
$repo  = $RepoName
$siteUrl = "https://$owner.github.io/$repo"

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
Ok "公開URL: $siteUrl"

# ---- 2. 設定の検証 ------------------------------------------
Step 2 "設定を検証"

# 公開URLは .env ではなくここで決まる。CI 側は Variables を見るので、
# .env の SITE_BASE_URL がサンプルのままでも問題ない。
python -c @'
import sys, yaml, pathlib
sys.path.insert(0, ".")
from src.catalog import load_catalog, ConfigError
cfg = yaml.safe_load(pathlib.Path("config/site.yaml").read_text(encoding="utf-8"))
try:
    cat = load_catalog(pathlib.Path("config/tools.yaml"), cfg)
except ConfigError as e:
    print(f"設定エラー: {e}"); sys.exit(1)
print(f"ツール {len(cat.tools)}件 / 比較 {len(cat.comparisons)}組 / 成果リンク設定済み {len(cat.monetized)}件")
'@
if ($LASTEXITCODE -ne 0) { Fail "config/tools.yaml に問題があります。上のメッセージを確認してください。" }
Ok "設定は妥当です"

# ---- 3. 初回巡回 --------------------------------------------
Step 3 "初回巡回（各ベンダーの価格ページを1回ずつ読む）"

if ($SkipCrawl) {
    Warn "巡回をスキップしました"
    python main.py --render
} else {
    Write-Host "    追跡対象を2秒間隔で読むので1分ほどかかります..." -ForegroundColor DarkGray
    python main.py --base-url $siteUrl
}
if ($LASTEXITCODE -ne 0) { Fail "生成に失敗しました。上のログを確認してください。" }

python main.py --verify --base-url $siteUrl
if ($LASTEXITCODE -ne 0) { Fail "生成物の検証に失敗しました。上の NG を解消してから再実行してください。" }

# ここでは履歴に書き込まない(--record を付けない)。
# 価格履歴は必ずCI(米国)から取る。手元の観測を混ぜると地域差が
# 値上げとして記録されてしまうため、初回の記録もCIに任せる。
Ok "巡回と生成の確認までを実行しました（履歴の記録はCIが行います）"

# ---- 4. push ------------------------------------------------
Step 4 "GitHubへpush"

if (-not (Test-Path ".git")) { git init -q; git branch -M main }
git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { git commit -q -m "AI価格トラッカー" }

git remote get-url origin 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    # Pages を無料で使うには public である必要がある
    gh repo create $repo --public --source=. --remote=origin --push
} else {
    git push -u origin main
}
if ($LASTEXITCODE -ne 0) { Fail "push に失敗しました。" }
Ok "$owner/$repo"

# ---- 5. ワークフローに書き込み権限を与える -------------------
Step 5 "Actions の権限を設定"

# ここが最重要。既定が read-only のままだと、ワークフロー側で contents: write と
# 書いても昇格できず、毎日の価格履歴がリポジトリに残らない。
# 履歴が残らない = このサイトの唯一の資産が永久に溜まらない、ということ。
gh api -X PUT "repos/$owner/$repo/actions/permissions/workflow" `
    -f default_workflow_permissions=write `
    -F can_approve_pull_request_reviews=false 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Warn "権限の自動設定に失敗しました。手動で設定してください:"
    Warn "  Settings → Actions → General → Workflow permissions → Read and write"
} else {
    Ok "ワークフローが価格履歴を書き戻せるようになりました"
}

# ---- 6. Variables -------------------------------------------
Step 6 "Variables を登録"

gh variable set SITE_BASE_URL --body $siteUrl --repo "$owner/$repo" | Out-Null

$envVars = @{}
foreach ($line in Get-Content ".env" -Encoding UTF8) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') { $envVars[$Matches[1]] = $Matches[2] }
}
if ($envVars["GOOGLE_SITE_VERIFICATION"]) {
    gh variable set GOOGLE_SITE_VERIFICATION --body $envVars["GOOGLE_SITE_VERIFICATION"] --repo "$owner/$repo" | Out-Null
    Ok "Search Console の確認トークンを登録"
}
Ok "SITE_BASE_URL = $siteUrl"

# ---- 7. Pages を有効化 --------------------------------------
Step 7 "GitHub Pages を有効化"

# 未設定なら POST、設定済みなら PUT。どちらか片方しか成功しないので順に試す
gh api -X POST "repos/$owner/$repo/pages" -f build_type=workflow 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gh api -X PUT "repos/$owner/$repo/pages" -f build_type=workflow 2>$null | Out-Null
}
Ok "ソース: GitHub Actions"

# ---- 8. 初回デプロイ ----------------------------------------
Step 8 "初回デプロイを実行"

gh workflow run daily.yml --repo "$owner/$repo"
Ok "ワークフローを起動しました"

Write-Host "`n========================================" -ForegroundColor Yellow
Write-Host " セットアップ完了" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Yellow
Write-Host " 公開URL : $siteUrl"
Write-Host " 進捗確認: gh run watch --repo $owner/$repo"
Write-Host " 以降は毎日 UTC 7:00（JST 16:00）に自動で巡回・更新されます。"
Write-Host ""
Write-Host " 次にやること（この2つで収益が決まります）:" -ForegroundColor Yellow
Write-Host "   1. Google Search Console に $siteUrl/ を登録し sitemap.xml を送信"
Write-Host "   2. アフィリエイトプログラムに申請し、config/tools.yaml の affiliate.url を埋める"
Write-Host "      → 承認にはサイトの実在が必要なので、この順番でしか進められません（README 4章）"
Write-Host ""
