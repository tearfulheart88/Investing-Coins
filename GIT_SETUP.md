# Git & GitHub 자동 업로드 설정 가이드
## Git & GitHub Auto-Upload Setup Guide

이 문서는 Upbit 자동매매 시스템을 GitHub에 연동하고, 프로그램 업데이트 시 자동으로 커밋·푸시하도록 설정하는 방법을 설명합니다.

---

## 📋 사전 요구사항

1. **Git 설치** (Windows용)
   - 다운로드: https://git-scm.com/download/win
   - 설치 완료 후 PowerShell/CMD에서 `git --version` 확인

2. **GitHub 계정**
   - https://github.com 에서 회원가입

3. **GitHub Personal Access Token (PAT)**
   - GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
   - 생성 시 권한: `repo` (전체), `workflow`
   - 토큰 복사 후 안전하게 보관

---

## 🚀 단계별 설정

### Step 1: 로컬 Git 저장소 초기화

PowerShell (관리자 모드)을 열고 프로젝트 폴더로 이동:

```powershell
cd "C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins"

# 1. Git 저장소 초기화
git init

# 2. 전역 사용자 설정 (처음 1회만)
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"

# 3. 이 저장소의 사용자 설정 (옵션)
git config --local user.name "Your Name"
git config --local user.email "your.email@example.com"
```

### Step 2: 초기 커밋 생성

```powershell
# 1. 모든 파일 추가 (로그 제외)
git add -A

# 2. .env 파일이 실수로 추가되었는지 확인
git reset .env .env.local
git status  # 확인

# 3. 초기 커밋
git commit -m "Initial commit: Upbit auto-trading system with session logging"
```

### Step 3: GitHub 저장소 생성

1. GitHub에 로그인
2. "New repository" 클릭
3. Repository name: `Investing-Coins` (또는 원하는 이름)
4. Description: `Upbit Auto-Trading System with ATR-based Strategy`
5. Private 또는 Public 선택
6. ❌ "Initialize with README" 체크 해제 (이미 로컬에 커밋이 있음)
7. "Create repository" 클릭

### Step 4: 로컬 저장소를 GitHub와 연결

생성 후 나타나는 화면에서 다음 명령어를 복사 실행:

```powershell
# 원격 저장소 추가
git remote add origin https://github.com/YOUR_USERNAME/Investing-Coins.git

# main 브랜치로 변경 (기본값이 master인 경우)
git branch -M main

# 첫 푸시
git push -u origin main
```

### Step 5: GitHub Actions 인증 설정

#### 방법 A: Personal Access Token (PAT) 사용 - 권장

1. GitHub에서 생성한 PAT 복사
2. PowerShell에서:

```powershell
# Windows Credential Manager에 저장
$PAT = "your_personal_access_token_here"
$encodedPAT = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("YOUR_USERNAME:$PAT"))
cmdkey /add:https://github.com /user:YOUR_USERNAME /pass:$PAT
```

#### 방법 B: SSH 키 사용 (더 안전함)

```powershell
# SSH 키 쌍 생성
ssh-keygen -t ed25519 -C "your.email@example.com"
# 저장 위치: C:\Users\user\.ssh\id_ed25519

# 공개 키 확인
cat ~/.ssh/id_ed25519.pub

# GitHub → Settings → SSH and GPG keys → New SSH key
# 공개 키 내용 붙여넣기
```

저장소 URL을 SSH로 변경:

```powershell
git remote set-url origin git@github.com:YOUR_USERNAME/Investing-Coins.git

# 테스트
git push
```

### Step 6: GitHub Actions Workflow 활성화

1. GitHub 저장소 페이지 열기
2. "Actions" 탭 클릭
3. `.github/workflows/auto-commit.yml` 워크플로우가 표시됨
4. "I understand my workflows, go ahead and enable them" 클릭

---

## 🔄 자동 업로드 작동 방식

### 트리거 조건

`.github/workflows/auto-commit.yml` 파일이 자동으로:

1. **매시간 정각** - 변경 사항 검사 및 커밋
2. **수동 트리거** - GitHub Actions 탭에서 "Run workflow" 클릭
3. **코드 푸시 시** - `.py`, `config.py`, `requirements.txt` 변경 감지

### 자동 제외되는 항목

✅ **커밋되는 파일**:
- 모든 `.py` Python 파일
- `config.py` (설정)
- `requirements.txt` (의존성)
- `.github/` 설정 파일

❌ **자동으로 제외되는 항목**:
- `logs/` 전체 폴더 (세션 로그, 거래 기록)
- `.env`, `.env.local` (API 키)
- `__pycache__/` (Python 캐시)
- `*.log` 파일

---

## ✅ 검증 방법

### 1. 로컬 저장소 확인

```powershell
cd "C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins"

# 1. 저장소 상태 확인
git status

# 2. 원격 저장소 확인
git remote -v
# 출력 예:
# origin  https://github.com/YOUR_USERNAME/Investing-Coins.git (fetch)
# origin  https://github.com/YOUR_USERNAME/Investing-Coins.git (push)

# 3. 커밋 히스토리 확인
git log --oneline -5

# 4. 현재 브랜치 확인
git branch -v
```

### 2. GitHub 저장소 확인

- GitHub 저장소 페이지에서 "Code" 탭 클릭
- 파일 목록에 프로젝트 파일이 표시되어야 함
- "Commits" 탭에서 커밋 히스토리 확인 가능

### 3. 자동 커밋 테스트

```powershell
# 1. 작은 변경 후 커밋
echo "# Test" >> README.md
git add README.md
git commit -m "Test commit"
git push

# 2. GitHub Actions 탭에서 워크플로우 실행 확인
```

### 4. GitHub Actions 상태 확인

1. GitHub 저장소 → "Actions" 탭
2. "Auto-commit Code Changes" 워크플로우 클릭
3. 최근 실행 기록과 로그 확인 가능

---

## 🔒 보안 체크리스트

- [ ] `.env` 파일이 `.gitignore`에 포함됨 (확인: `.gitignore` 파일 확인)
- [ ] GitHub 저장소가 최소 Private 설정 (또는 public이지만 민감 정보 없음)
- [ ] PAT 또는 SSH 키가 안전하게 보관됨 (GitHub에만 저장)
- [ ] `.github/workflows/auto-commit.yml`에서 로그 제외 설정 확인
- [ ] 초기 커밋 후 `git log`에서 커밋 메시지 확인

---

## 📝 자주하는 질문 (FAQ)

### Q1: 프로그램 실행 중에도 커밋이 되나요?
**A**: 예, GitHub Actions는 GitHub 서버에서 실행되므로 로컬 프로그램 실행과 독립적입니다.
단, 변경 사항이 GitHub에 푸시된 후에야 감지됩니다.

### Q2: 로컬에서도 자동 커밋을 원합니다
**A**: Git pre-commit hook을 설정할 수 있습니다. `git_hooks/pre-commit` 파일 참고.

### Q3: 특정 파일을 제외하고 싶습니다
**A**: `.gitignore` 파일을 수정하고 다시 커밋하면 됩니다.
```
echo "unwanted_file.txt" >> .gitignore
git add .gitignore
git commit -m "Update gitignore"
git push
```

### Q4: 실수로 민감한 정보를 푸시했습니다
**A**: 즉시 GitHub에서 파일 제거 후 PAT 또는 키 변경:
```powershell
# 히스토리에서 파일 제거
git filter-branch --tree-filter 'rm -f .env' HEAD

# 강제 푸시 (주의!)
git push origin main --force
```

### Q5: 커밋 로그를 보고 싶습니다
```powershell
# 로컬 커밋 히스토리
git log --oneline --all

# GitHub 커밋 보기
# https://github.com/YOUR_USERNAME/Investing-Coins/commits/main
```

---

## 🛠️ 고급 설정 (선택사항)

### 로컬 자동 커밋 (pre-commit hook)

`Investing-Coins/.git/hooks/pre-commit` 파일 생성:

```bash
#!/bin/bash
# 자동 커밋이 필요한 경우만 실행

FILES=$(git diff --cached --name-only | grep -v '^logs/' | grep -v '\.log$')
if [ -z "$FILES" ]; then
  exit 0
fi

# 로그 제외
git reset logs/ 2>/dev/null || true
git reset .env .env.local 2>/dev/null || true

exit 0
```

파일 실행 권한 추가:
```powershell
chmod +x .git/hooks/pre-commit
```

### 커밋 메시지 템플릿

`.git/hooks/commit-msg` 파일로 커밋 메시지 자동화 (선택)

---

## 📞 문제 해결

| 문제 | 해결 방법 |
|------|---------|
| `git push` 실패 (401 Unauthorized) | PAT 재생성 또는 SSH 키 확인 |
| 로그 파일이 커밋됨 | `.gitignore` 재확인 및 캐시 초기화: `git rm -r --cached logs/` |
| 원격 저장소 연결 안 됨 | `git remote -v` 확인 및 URL 수정: `git remote set-url origin [URL]` |
| 파일 충돌 (conflict) | `git pull --rebase origin main` 후 수동 병합 |

---

## 📚 참고 자료

- [Git 공식 문서](https://git-scm.com/doc)
- [GitHub 가이드](https://guides.github.com/)
- [GitHub Actions 문서](https://docs.github.com/en/actions)
- [Personal Access Token 생성](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)

---

**마지막 업데이트**: 2026-03-05
**프로젝트**: Upbit Auto-Trading System with Session Logging
