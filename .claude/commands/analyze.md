---
description: "Gemini API로 현재 전략을 분석하고 Claude 프롬프트 JSON을 생성합니다"
---

프로젝트 루트: `C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins`

Gemini 전략 분석을 실행하세요:

## 사전 확인
1. `.env` 파일에 `GEMINI_API_KEY`가 설정되어 있는지 확인
2. `google-generativeai` 패키지 설치 여부 확인: `python -c "import google.generativeai"`

## 분석 실행
```bash
python -c "
import sys
sys.path.insert(0, r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins')
from dotenv import load_dotenv
load_dotenv(r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins\.env')
import config
from core.gemini_analyzer import GeminiStrategyAnalyzer

if not config.GEMINI_API_KEY:
    print('❌ GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.')
    sys.exit(1)

analyzer = GeminiStrategyAnalyzer(config.GEMINI_API_KEY)
print(f'🔍 분석 시작: {config.SELECTED_SCENARIO}')
result = analyzer.analyze(config.SELECTED_SCENARIO, max_trades=config.GEMINI_MAX_TRADES)

print(f\"\\n📊 분석 완료\")
print(f\"  거래 건수: {result['trade_count']}건\")
print(f\"  승률: {result['win_rate']*100:.1f}%\")
print(f\"  평균 수익률: {result['avg_pnl_pct']*100:+.2f}%\")
print(f\"\\n🔴 문제점:\")
for i, issue in enumerate(result.get('issues', []), 1):
    print(f\"  {i}. {issue}\")
print(f\"\\n🟢 개선안:\")
for i, imp in enumerate(result.get('improvements', []), 1):
    print(f\"  {i}. {imp}\")

# Claude 프롬프트 JSON 저장
import json, os
out_dir = r'C:\Users\user\Desktop\AI\GoogleDrive\Claude\Investing-Coins\logs'
out_path = os.path.join(out_dir, f'gemini_analysis_{config.SELECTED_SCENARIO}.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f\"\\n💾 결과 저장: {out_path}\")
print(f\"📋 Claude JSON: logs/gemini_analysis_{config.SELECTED_SCENARIO}.json\")
"
```

## 인수 처리
- `$ARGUMENTS`에 시나리오 ID가 있으면 해당 시나리오로 분석 (예: `/analyze mr_rsi`)
- `--trades <N>`: 분석할 거래 수 지정 (기본: 50)

## 결과 활용
분석 결과의 `claude_prompt_json` 필드를 Claude에게 붙여넣으면 전략 개선 제안을 받을 수 있습니다.
저장 위치: `logs/gemini_analysis_<scenario_id>.json`
