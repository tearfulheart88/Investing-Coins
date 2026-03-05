---
description: "최근 거래 로그 및 세션 요약을 조회합니다"
---

프로젝트 루트: `C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins`
로그 경로: `logs/trades/trades.jsonl`, `logs/sessions/`

다음 순서로 로그를 조회하고 요약하세요:

1. **최근 거래 조회** (trades.jsonl 마지막 30행):
   ```bash
   python -c "
   import json, os
   path = r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins\logs\trades\trades.jsonl'
   if not os.path.exists(path):
       print('거래 기록 없음')
   else:
       with open(path, encoding='utf-8') as f:
           lines = f.readlines()
       records = [json.loads(l) for l in lines[-30:] if l.strip()]
       print(f'최근 {len(records)}건 거래:')
       for r in records:
           pnl = f\" | pnl={r['pnl_pct']*100:+.2f}%\" if r.get('pnl_pct') else ''
           print(f\"  [{r['timestamp'][:16]}] {r['action']:6} {r['ticker']:12} {r['price']:>12,.0f}원  {r['reason']}{pnl}\")
   "
   ```

2. **세션 목록** (sessions 디렉토리):
   ```bash
   python -c "
   import os, json
   sdir = r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins\logs\sessions'
   if not os.path.isdir(sdir):
       print('세션 기록 없음')
   else:
       files = sorted(os.listdir(sdir))[-5:]
       for f in files:
           p = os.path.join(sdir, f)
           try:
               data = json.load(open(p, encoding='utf-8'))
               print(f\"{f}: 시작={data.get('started_at','?')[:16]}  거래={data.get('trade_count',0)}건  PnL={data.get('total_pnl_krw',0):+,.0f}원\")
           except: print(f'{f}: 파싱 실패')
   "
   ```

3. **전체 통계 요약**:
   - 총 거래 건수, 승률(SELL 기준 pnl_pct > 0), 평균 수익률, 최대 수익/손실 거래 출력
   - 시나리오별 성과 비교

인수(argument)가 주어진 경우:
- `--session <session_id>` : 특정 세션의 거래만 필터
- `--ticker <ticker>` : 특정 종목만 필터 (예: `KRW-BTC`)
- `--tail <N>` : 마지막 N건 조회 (기본 30)
- `--win` : 수익 거래만 표시
- `--loss` : 손실 거래만 표시
