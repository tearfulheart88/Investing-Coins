---
description: "모든 Python 파일 문법 검사 및 import 오류 확인"
---

프로젝트 루트: `C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins`

Python 파일 전체 문법·임포트 검사를 실행하세요:

1. **문법 검사** (py_compile):
   ```bash
   python -c "
   import py_compile, glob, sys
   root = r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins'
   files = glob.glob(root + '/**/*.py', recursive=True)
   files = [f for f in files if '__pycache__' not in f]
   errors = []
   for f in sorted(files):
       try:
           py_compile.compile(f, doraise=True)
           print(f'  ✅ {f.replace(root, \"\")}')
       except py_compile.PyCompileError as e:
           print(f'  ❌ {f.replace(root, \"\")}: {e}')
           errors.append(f)
   print(f'\n총 {len(files)}개 검사 | 오류: {len(errors)}개')
   sys.exit(1 if errors else 0)
   "
   ```

2. **핵심 모듈 임포트 검사**:
   ```bash
   python -c "import config; import core.trader; import core.risk_manager; import data.market_data; import data.state_manager; import logging_.trade_logger; print('✅ 핵심 모듈 임포트 정상')"
   ```
   - 이 명령은 프로젝트 루트에서 실행해야 합니다.

3. 오류 발견 시: 파일명, 줄 번호, 오류 내용을 명확히 보고하고 수정 여부를 물어보세요.
4. 모두 정상이면: "✅ 전체 {N}개 파일 문법 검사 통과" 출력
