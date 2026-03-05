# Git 저장소 초기화 스크립트 (Windows PowerShell)
# Git Repository Initialization Script

param(
    [string]$GitHubUsername = "",
    [string]$RepositoryName = "Investing-Coins",
    [switch]$SkipRemote = $false
)

Write-Host "================================" -ForegroundColor Cyan
Write-Host "Upbit Trading System - Git Init" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

# 1. Git 설치 확인
Write-Host "[1/5] Checking Git installation..." -ForegroundColor Yellow
$gitVersion = git --version 2>$null
if ($gitVersion) {
    Write-Host "✓ Git installed: $gitVersion" -ForegroundColor Green
} else {
    Write-Host "✗ Git not found. Please install from https://git-scm.com/download/win" -ForegroundColor Red
    exit 1
}

# 2. 현재 디렉터리에서 Git 초기화
Write-Host "[2/5] Initializing Git repository..." -ForegroundColor Yellow
if (Test-Path ".git") {
    Write-Host "⚠ Repository already exists. Skipping git init." -ForegroundColor Yellow
} else {
    git init
    Write-Host "✓ Git repository initialized" -ForegroundColor Green
}

# 3. 사용자 설정
Write-Host "[3/5] Configuring Git user..." -ForegroundColor Yellow
$userName = git config --local user.name 2>$null
if ($userName) {
    Write-Host "✓ User already configured: $userName" -ForegroundColor Green
} else {
    if ([string]::IsNullOrEmpty($GitHubUsername)) {
        $GitHubUsername = Read-Host "Enter your GitHub username"
    }
    git config --local user.name $GitHubUsername
    git config --local user.email "$GitHubUsername@users.noreply.github.com"
    Write-Host "✓ User configured: $GitHubUsername" -ForegroundColor Green
}

# 4. .gitignore 확인
Write-Host "[4/5] Checking .gitignore..." -ForegroundColor Yellow
if (Test-Path ".gitignore") {
    $gitignoreLines = @(Get-Content ".gitignore" | Measure-Object -Line).Lines
    Write-Host "✓ .gitignore found ($gitignoreLines lines)" -ForegroundColor Green
} else {
    Write-Host "✗ .gitignore not found" -ForegroundColor Red
}

# 5. 초기 커밋 여부
Write-Host "[5/5] Checking commits..." -ForegroundColor Yellow
$commits = git rev-list --count HEAD 2>$null
if ($commits -gt 0) {
    Write-Host "✓ Commits exist ($commits commits)" -ForegroundColor Green
    Write-Host ""
    Write-Host "Current status:" -ForegroundColor Cyan
    git status
} else {
    Write-Host "⚠ No commits yet. Creating initial commit..." -ForegroundColor Yellow
    git add -A
    git reset .env .env.local 2>$null

    if (git diff --cached --quiet) {
        Write-Host "✗ No files to commit" -ForegroundColor Red
    } else {
        git commit -m "Initial commit: Upbit auto-trading system with session logging"
        Write-Host "✓ Initial commit created" -ForegroundColor Green
    }
}

# 6. 원격 저장소 설정 (옵션)
Write-Host ""
Write-Host "Remote repository setup:" -ForegroundColor Cyan
if (-not $SkipRemote) {
    $remote = git config --get remote.origin.url 2>$null
    if ($remote) {
        Write-Host "✓ Remote already configured: $remote" -ForegroundColor Green
    } else {
        Write-Host "⚠ Remote not configured" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "To connect to GitHub:" -ForegroundColor Cyan
        Write-Host "1. Create repository on GitHub: https://github.com/new" -ForegroundColor White
        Write-Host "2. Run command:" -ForegroundColor White
        Write-Host "   git remote add origin https://github.com/$GitHubUsername/$RepositoryName.git" -ForegroundColor Cyan
        Write-Host "3. Push to GitHub:" -ForegroundColor White
        Write-Host "   git branch -M main" -ForegroundColor Cyan
        Write-Host "   git push -u origin main" -ForegroundColor Cyan
    }
}

Write-Host ""
Write-Host "================================" -ForegroundColor Cyan
Write-Host "Setup Complete!" -ForegroundColor Green
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "📖 For detailed setup instructions, see: GIT_SETUP.md" -ForegroundColor Cyan
