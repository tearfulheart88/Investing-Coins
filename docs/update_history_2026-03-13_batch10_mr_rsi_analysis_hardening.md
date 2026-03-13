# 2026-03-13 Batch 10 - mr_rsi Analysis Hardening

## Why
- `mr_rsi` 최신 분석 JSON이 `sell_count=0`만 보고 "매도 기능 미작동"으로 오판하는 문제가 있었다.
- 멀티시나리오 실거래 세션에서 `_obs_session_trades`의 BUY가 첫 번째 전략 시나리오 ID로 잘못 기록되어, `mr_rsi` 분석 로그에 `SMRH_STOP(...)` 매수 사유가 섞여 보였다.
- 실제 원인 추적을 위해 `mr_rsi`의 sell-side signal trace도 남길 필요가 있었다.

## Changes
- `core/gemini_analyzer.py`
  - 최신 세션 payload 전체를 읽어 `analysis_context`를 생성하도록 변경.
  - 오픈 포지션 수, 포지션 스냅샷, 최근 signal trace, 세션 요약을 Gemini/Claude 프롬프트에 함께 전달.
  - `sell_count=0`과 `requires_scheduled_sell=False`를 잘못 해석하지 않도록 프롬프트 해석 규칙 강화.
  - `buy_reasons`, `open_position_count`, `analysis_notes`를 통계에 포함.
- `logging_/session_log_writer.py`
  - 최신 세션 payload 전체를 반환하는 `load_latest_session_payload()` 추가.
- `core/trader.py`
  - 멀티시나리오 실거래 BUY의 `_obs_session_trades` 기록이 실제 `scenario.scenario_id`를 사용하도록 수정.
  - sell-side signal trace를 기록하는 `_record_sell_signal_trace()` 추가.
- `core/paper_engine.py`
  - paper 엔진에도 sell-side signal trace 기록 추가.
- `strategies/base_strategy.py`
  - `SellSignal`에 `metadata` 필드 추가.
- `strategies/mr_rsi.py`
  - 하드손절 / 트레일링 / RSI 회복 / 최대보유시간 / sell data error 경로에 `sell_evaluation` trace 추가.

## Expected Effect
- `mr_rsi`에 오픈 포지션만 남아 있는 상황을 "매도 기능 고장"으로 오판하는 보고서가 크게 줄어든다.
- 멀티시나리오 세션 종료 후 전략별 분석 로그가 서로 덜 오염된다.
- 다음부터는 `mr_rsi`의 실제 sell 트리거 이유를 buy trace와 같은 포맷으로 추적할 수 있다.

## Verification
- `python -m py_compile`:
  - `strategies/base_strategy.py`
  - `strategies/mr_rsi.py`
  - `core/trader.py`
  - `core/paper_engine.py`
  - `core/gemini_analyzer.py`
  - `logging_/session_log_writer.py`
