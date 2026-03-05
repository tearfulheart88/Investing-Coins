# GitHub 자동 업로드 통합 — 완료 문서
## GitHub Auto-Upload Integration — Complete Documentation

**상태**: ✅ 완료 (Ready to Use)
**작성일**: 2026-03-05
**프로젝트**: Upbit 자동매매 시스템 with 세션 기반 로깅

---

## 📦 생성된 파일 목록

### 1. `.gitignore` ✅
- **위치**: 프로젝트 루트
- **목적**: Git이 추적하지 말아야 할 파일 지정
- **주요 제외 항목**:
  - `.env`, `.env.local` (API 키, 민감한 정보)
  - `logs/` 전체 (세션 로그, 거래 기록)
  - `__pycache__/`, `*.pyc` (Python 캐시)
  - `venv/`, 가상 환경 (만약 있는 경우)
  - `.DS_Store`, `Thumbs.db` (OS 파일)

### 2. `.github/workflows/auto-commit.yml` ✅
- **위치**: `.github/workflows/` 디렉터리
- **목적**: GitHub Actions 워크플로우 (자동 커밋/푸시)
- **작동 방식**:
  - **트리거 1**: 매시간 정각 (매 시간 변경 사항 검사)
  - **트리거 2**: 수동 실행 (GitHub Actions 탭에서 클릭)
  - **트리거 3**: Python 파일 변경 감지
- **자동 제외**:
  - `logs/` 폴더 (거래 로그 제외)
  - `.env` 파일 (API 키 제외)
  - `*.log` 파일 (시스템 로그 제외)

### 3. `GIT_SETUP.md` ✅
- **위치**: 프로젝트 루트
- **목적**: 상세한 단계별 설정 가이드
- **포함 내용**:
  - Git 설치 및 초기화
  - GitHub 저장소 생성
  - GitHub Actions 인증 설정
  - 자동 업로드 검증 방법
  - FAQ 및 문제 해결

### 4. `init_git.ps1` ✅
- **위치**: 프로젝트 루트
- **목적**: PowerShell 자동 초기화 스크립트 (Windows)
- **기능**:
  - Git 설치 확인
  - 저장소 초기화
  - 사용자 정보 설정
  - 초기 커밋 생성
  - 원격 저장소 연결 가이드

---

## 🚀 빠른 시작 (Quick Start)

### Windows (PowerShell 관리자 모드)

```powershell
# 1. 프로젝트 폴더로 이동
cd "C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins"

# 2. 자동 초기화 스크립트 실행
.\init_git.ps1

# 3. GitHub 저장소 생성 (GitHub 웹사이트)
# https://github.com/new
# Repository name: Investing-Coins
# Description: Upbit Auto-Trading System with ATR-based Strategy

# 4. 원격 저장소 연결 및 푸시
git remote add origin https://github.com/YOUR_USERNAME/Investing-Coins.git
git branch -M main
git push -u origin main

# 5. GitHub Actions 활성화
# https://github.com/YOUR_USERNAME/Investing-Coins/actions
```

---

## 🔄 작동 방식

### 전체 플로우

```
┌─────────────────────────────────────────────────────────┐
│           프로그램 파일 수정/저장                        │
│  (config.py, strategies/*.py, core/trader.py 등)        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ├─→ 로컬: git add/commit (선택)
                     │
┌────────────────────▼────────────────────────────────────┐
│     GitHub에 푸시 (git push)                            │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  GitHub Actions 워크플로우 트리거                       │
│  (.github/workflows/auto-commit.yml)                    │
│                                                         │
│  1. 변경 사항 감지 (logs/ 제외)                        │
│  2. 커밋 생성 "[Auto-commit] Program update - ..."    │
│  3. GitHub에 자동 푸시                                 │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  GitHub 저장소 업데이트 완료                           │
│  • 커밋 히스토리 기록                                  │
│  • 모든 변경 사항 저장                                 │
│  • 로그 파일은 제외 (프라이빗 유지)                   │
└─────────────────────────────────────────────────────────┘
```

### 자동 제외 메커니즘

✅ **커밋되는 항목**:
- `*.py` - Python 전략 파일
- `config.py` - 설정 (민감한 값은 .env에 분리)
- `requirements.txt` - 라이브러리 의존성
- `.github/workflows/` - 자동화 설정
- 기타 코드 파일

❌ **자동으로 제외되는 항목**:
- `logs/` - 세션별 로그, 거래 기록
- `.env`, `.env.local` - API 키, 인증 정보
- `__pycache__/`, `*.pyc` - Python 캐시
- `*.log` - 시스템 로그 파일

---

## 💾 세션 로깅 & GitHub 통합

### 세션 로그 구조 (로컬만 저장)

```
logs/
├── system/                  # 전역 시스템 로그
├── trades/                  # 전역 거래 기록
│   ├── trades.jsonl        # (GitHub에 푸시되지 않음)
│   └── trades_2026-03.csv
└── sessions/                # 세션별 로그 (GitHub에 푸시되지 않음)
    └── vb_noise_filter_2026-03-05_14-30-00/
        ├── system.log
        ├── trades_session.jsonl
        ├── positions_snapshot.json
        └── summary.json
```

### GitHub에 저장되는 항목

```
Investing-Coins/
├── .github/
│   └── workflows/
│       └── auto-commit.yml
├── .gitignore
├── config.py                # 설정 (민감한 값은 환경변수)
├── main.py                  # 진입점
├── requirements.txt         # 의존성
├── core/                    # 핵심 모듈
├── strategies/              # 거래 전략
├── data/                    # 데이터 처리
├── exchange/                # 거래소 API
└── logging_/                # 로깅 시스템
```

---

## 🔐 보안 사항

### ✅ 보호되는 항목

- ✓ `.env` 파일 (API 키, 시크릿 키)
- ✓ `logs/` 폴더 (개인 거래 기록)
- ✓ 포지션 정보 (positions_snapshot.json)
- ✓ Python 캐시 및 가상 환경

### ⚠️ 주의사항

1. **환경 변수 사용**
   ```python
   # ✅ 권장
   API_KEY = os.getenv("UPBIT_ACCESS_KEY")

   # ❌ 절대 금지
   API_KEY = "direct_key_in_code"
   ```

2. **GitHub 저장소 설정**
   - Private 저장소 권장
   - 또는 Public이지만 민감한 정보 없음

3. **PAT/SSH 키 관리**
   - GitHub Personal Access Token은 로컬에만 보관
   - SSH 키는 `~/.ssh/` 폴더에 안전하게 보관
   - GitHub Actions는 자동으로 안전하게 처리

---

## ✅ 검증 체크리스트

먼저 다음을 확인하세요:

- [ ] `.gitignore` 파일이 생성됨
- [ ] `.github/workflows/auto-commit.yml` 파일이 생성됨
- [ ] `GIT_SETUP.md` 파일이 생성됨
- [ ] `init_git.ps1` 스크립트가 생성됨
- [ ] `.env` 파일이 `.gitignore`에 포함됨

### 로컬 저장소 초기화 후

```powershell
# 1. 저장소 상태 확인
git status

# 2. 커밋 확인
git log --oneline -5

# 3. 원격 저장소 확인
git remote -v
```

### GitHub 업로드 후

```powershell
# 1. GitHub Actions 활성화
# https://github.com/YOUR_USERNAME/Investing-Coins/actions

# 2. 워크플로우 상태 확인
# "Auto-commit Code Changes" 워크플로우가 표시되어야 함

# 3. 첫 실행 (수동 트리거)
# "Run workflow" 버튼 클릭
```

---

## 📋 다음 단계

### 1단계: GitHub 계정 준비
```
□ GitHub 계정 생성 (https://github.com)
□ Personal Access Token 생성
  - Settings → Developer settings → Personal access tokens
  - 권한: repo, workflow
□ 토큰 안전하게 보관
```

### 2단계: 로컬 Git 설정
```
□ Git 설치 확인 (git --version)
□ PowerShell에서 init_git.ps1 실행
  .\init_git.ps1
□ 초기 커밋 생성 확인
  git log --oneline
```

### 3단계: GitHub 저장소 생성
```
□ https://github.com/new 에서 저장소 생성
□ Repository name: Investing-Coins
□ Public 또는 Private 선택 (권장: Private)
□ 초기화 옵션 없음 (README 체크 해제)
```

### 4단계: 원격 저장소 연결
```
□ git remote add origin [GITHUB_URL]
□ git branch -M main
□ git push -u origin main
```

### 5단계: GitHub Actions 활성화
```
□ https://github.com/[USER]/Investing-Coins/actions 방문
□ "Auto-commit Code Changes" 워크플로우 확인
□ "Run workflow" 버튼으로 테스트 실행
```

---

## 🛠️ 문제 해결

### 문제: `git init` 오류
**해결**: `git --version` 확인 후 Git 설치 (https://git-scm.com/download/win)

### 문제: `.env` 파일이 푸시됨
**해결**:
```powershell
git rm --cached .env
git commit -m "Remove .env from tracking"
git push
```

### 문제: GitHub Actions 워크플로우가 실행 안 됨
**해결**:
- GitHub 저장소 → Actions 탭 → 워크플로우 활성화 필요
- PAT 또는 SSH 인증 설정 확인

### 문제: 커밋 충돌 (Conflict)
**해결**:
```powershell
git pull --rebase origin main
git push
```

---

## 📞 지원

상세한 가이드는 `GIT_SETUP.md` 파일을 참고하세요.

---

## 📊 요약

| 항목 | 상태 | 위치 |
|------|------|------|
| `.gitignore` | ✅ 생성됨 | 프로젝트 루트 |
| GitHub Actions 워크플로우 | ✅ 생성됨 | `.github/workflows/auto-commit.yml` |
| 설정 가이드 | ✅ 생성됨 | `GIT_SETUP.md` |
| 자동 초기화 스크립트 | ✅ 생성됨 | `init_git.ps1` |

**준비 상태**: 🟢 GitHub 연동 준비 완료

---

**프로젝트**: Upbit Auto-Trading System with Session Logging
**마지막 업데이트**: 2026-03-05 20:35 UTC
**담당**: Claude Code Agent
