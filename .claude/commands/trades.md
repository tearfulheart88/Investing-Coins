---
description: "거래 데이터를 SQL 또는 조건으로 쿼리합니다 (SQLite / JSONL)"
---

프로젝트 루트: `C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins`
DB 경로: `logs/trades/trades.db` (없으면 JSONL 폴백)

거래 데이터 쿼리를 실행하세요:

## SQLite 쿼리 (trades.db 있을 때)
```bash
python -c "
import sqlite3, os
db = r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins\logs\trades\trades.db'
if not os.path.exists(db):
    print('trades.db 없음 — JSONL 모드로 전환')
else:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # 아래 SQL을 사용자 요청에 맞게 교체
    sql = '''
        SELECT ticker, action, price, pnl_pct, reason, timestamp
        FROM trades
        ORDER BY timestamp DESC
        LIMIT 20
    '''
    rows = con.execute(sql).fetchall()
    for r in rows:
        pnl = f\"{r['pnl_pct']*100:+.2f}%\" if r['pnl_pct'] else '  -   '
        print(f\"[{r['timestamp'][:16]}] {r['action']:6} {r['ticker']:12} {r['price']:>12,.0f}원  pnl={pnl}  {r['reason']}\")
    con.close()
"
```

## 자주 쓰는 SQL 패턴

| 목적 | SQL |
|------|-----|
| 시나리오별 승률 | `SELECT scenario_id, COUNT(*) total, ROUND(AVG(CASE WHEN pnl_pct>0 THEN 1.0 ELSE 0 END)*100,1) win_rate FROM trades WHERE action='SELL' GROUP BY scenario_id` |
| 종목별 총 손익 | `SELECT ticker, SUM(pnl_krw) total_pnl FROM trades WHERE action='SELL' GROUP BY ticker ORDER BY total_pnl DESC` |
| 최대 손실 거래 | `SELECT * FROM trades WHERE action='SELL' ORDER BY pnl_pct ASC LIMIT 5` |
| 오늘 거래 | `SELECT * FROM trades WHERE date(timestamp) = date('now','localtime') ORDER BY timestamp DESC` |
| 재진입 이력 | `SELECT * FROM trades WHERE action='REENTRY' ORDER BY timestamp DESC LIMIT 20` |
| 세션별 성과 | `SELECT session_id, COUNT(*) cnt, SUM(pnl_krw) pnl FROM trades WHERE action='SELL' GROUP BY session_id ORDER BY session_id DESC` |

## JSONL 폴백 쿼리
trades.db가 없을 때는 JSONL에서 직접 읽어 필터링:
```bash
python -c "
import json
path = r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins\logs\trades\trades.jsonl'
records = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]
# 사용자 조건에 맞게 필터
sells = [r for r in records if r['action']=='SELL']
wins = [r for r in sells if (r.get('pnl_pct') or 0) > 0]
print(f'SELL {len(sells)}건 | 승리 {len(wins)}건 | 승률 {len(wins)/max(len(sells),1)*100:.1f}%')
"
```

사용자가 특정 조건을 요청하면 위 SQL 또는 Python 필터를 적절히 수정해 실행하세요.
