# 2026-03-13 Batch 08 - STEEM 반복 매수/매도 실거래 수정

## 배경
- 실거래 `KRW-STEEM`에서 `smrh_stop`가 수 초 간격으로 반복 매수/즉시 매도를 수행했다.
- 업비트 실제 주문 이력 기준으로 `09:09:48~09:15:30 KST` 사이 `bid -> ask` 왕복이 여러 차례 반복되었고, 수수료 손실이 누적됐다.

## 확인된 원인
1. `smrh_stop.should_sell_on_signal()`의 MACD 약화 청산 조건이 `macd_30m["hist_prev"] < 0`로 구현되어 있었다.
   - 이 값은 `MACD bullish cross(hist_prev<0, hist>0)` 직후에도 참이므로,
   - 매수 직후 다음 루프에서 곧바로 `MACD_30M_NEG_PREV(...)` 청산이 발생했다.
2. 실거래 `Trader._execute_sell()`에서 `sell_metadata`가 정의되지 않은 상태로 참조되고 있었다.
   - 실제 매도 주문은 정상 체결되었지만,
   - SELL 로그/DB 기록 직전에 `NameError: name 'sell_metadata' is not defined`가 발생해
   - 거래 DB에 SELL 레코드가 누락되었다.
3. `smrh_stop`는 같은 30분 신호봉 안에서 매수 조건이 계속 유지될 수 있어,
   - 한번 청산된 뒤에도 같은 봉에서 재매수가 반복될 여지가 있었다.
4. 기본 `REENTRY_ENABLED_SCENARIOS`에 `smrh_stop`가 포함돼 있어,
   - breakout 전략에 맞지 않는 synthetic re-entry가 섞일 수 있었다.

## 반영 내용
- `strategies/smrh_stop.py`
  - 같은 30분 신호봉에서는 재매수하지 않도록 `SAME_SIGNAL_BAR(...)` 가드 추가
  - `MACD_30M_NEG_PREV(...)` 즉시 청산 제거
  - MACD 청산은 다음 2가지일 때만 발생하도록 수정
    - `MACD_30M_TURNED_NEG(prev>=0 -> hist<0)`
    - `MACD_30M_WEAKENING(prev>0, hist>=0, hist<hist_prev, 그리고 peak pnl이 본절 트리거 이상)`
- `core/trader.py`
  - SELL 체결 후 `sell_metadata`를 명시적으로 구성하도록 수정
  - SELL 로그/DB 저장 실패가 매매 루프 전체 오류로 번지지 않도록 예외 보호 추가
- `config.py`
  - 기본 `REENTRY_ENABLED_SCENARIOS`에서 `smrh_stop` 제거

## 기대 효과
- `smrh_stop`가 MACD cross 직후 같은 봉에서 바로 청산되는 churn 제거
- 같은 30분 신호봉 내 반복 재매수 차단
- 실제 SELL이 더 이상 DB/Markdown 성과 기록에서 누락되지 않음
- `smrh_stop`는 breakout 전략답게 실제 추세 약화/역전 시점에만 청산 판단

## 검증
- `py_compile` 통과
  - `strategies/smrh_stop.py`
  - `core/trader.py`
  - `config.py`

