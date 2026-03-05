---
description: "GitHub에 변경사항을 커밋하고 푸시합니다 (logs/, .env 제외)"
---

프로젝트 루트: `C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins`

다음 순서로 GitHub 업로드를 실행하세요:

1. **스테이징**: `git -C "<프로젝트경로>" add -A`
2. **민감파일 제외**: `git -C "<프로젝트경로>" reset -- logs/ .env 2>/dev/null || true`
3. **변경사항 확인**: `git -C "<프로젝트경로>" diff --cached --stat`
   - 변경사항 없으면: "✅ 변경사항 없음 - 업로드 불필요" 출력 후 종료
4. **커밋**: 현재 KST 시각으로 메시지 생성 후 커밋
   ```
   git -C "<프로젝트경로>" commit -m "[Auto-commit] $(python -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M KST'))")"
   ```
5. **푸시**: `git -C "<프로젝트경로>" push origin main`
   - 실패 시 rebase 후 재시도: `git pull --rebase origin main && git push origin main`
6. 결과 보고 (성공/실패 여부, 커밋 해시, 변경 파일 수)
