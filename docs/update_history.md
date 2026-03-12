# Update History

이 파일은 코드 변경 이력을 Markdown으로 기록하는 롤백 참고용 문서입니다.
앞으로 기능 수정이나 운영 로직 변경이 생기면 같은 형식으로 아래에 계속 추가합니다.

## 2026-03-12 Batch 01 - `mr_rsi` 재진입/분석 왜곡 수정

### 목적
- 외부 동기화 포지션이 재진입 대상으로 오인되는 문제를 막기 위함
- 최신 무거래 세션이 예전 거래 로그로 덮여 분석되는 문제를 막기 위함
- `exchange_sync`/`unknown` 레거시 기록이 전략 성과에 섞이는 문제를 막기 위함

### 변경 내용
- `mr_rsi` 재진입 시 peak를 새 진입가로 리셋하도록 추가
- `EXCHANGE_SYNC` / `exchange_sync` 포지션은 재진입 대상에서 제외
- 최신 세션 로그가 `0 trades`여도 레거시 JSONL로 fallback하지 않도록 수정
- Gemini 분석 통계에서 `exchange_sync`, `unknown` 기록 제외

### 변경 파일
- [strategies/base_strategy.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/strategies/base_strategy.py)
- [strategies/mr_rsi.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/strategies/mr_rsi.py)
- [core/trader.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/core/trader.py)
- [core/gemini_analyzer.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/core/gemini_analyzer.py)
- [logging_/session_log_writer.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/logging_/session_log_writer.py)

### 롤백 힌트
- 재진입 관련 변경만 되돌리려면 `base_strategy.py`, `mr_rsi.py`, `trader.py`의 재진입 훅과 외부 포지션 제외 조건을 되돌리면 됩니다.
- 분석 왜곡 수정만 되돌리려면 `gemini_analyzer.py`, `session_log_writer.py`의 최신 세션 우선 로딩과 외부 기록 제외 로직을 이전 버전으로 복원하면 됩니다.

## 2026-03-12 Batch 02 - 분석 로그 분리 및 진단 메타데이터 강화

### 목적
- 실거래와 가상거래 분석 로그를 폴더 단위로 분리해 혼선을 줄이기 위함
- 세션 종료 시점의 종목/포지션/외부동기화 상태를 함께 남겨 원인 추적을 쉽게 하기 위함
- 실거래 멀티시나리오 환경에서 전략별 분석 로그를 따로 남기기 위함

### 변경 내용
- 분석 로그 저장 경로를 아래처럼 분리
  - `logs/analysis/real`
  - `logs/analysis/paper`
- 레거시 `logs/analysis/*.json` 파일은 계속 읽을 수 있도록 로더 유지
- 분석 로그에 `diagnostics` 메타데이터 추가
  - 모드
  - 세션 ID / 계좌 ID
  - 활성 종목 목록
  - 액션별 거래 건수
  - 종료 시점 오픈 포지션 목록
  - 실거래의 경우 외부 동기화 포지션 수
- 실거래 종료 시 멀티시나리오를 시나리오별로 따로 저장하도록 수정

### 변경 파일
- [config.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/config.py)
- [logging_/session_log_writer.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/logging_/session_log_writer.py)
- [core/trader.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/core/trader.py)
- [core/paper_engine.py](/C:/Users/user/Desktop/AI/GoogleDrive/Claude/Investing-Coins/core/paper_engine.py)

### 롤백 힌트
- 분석 로그 폴더 분리만 되돌리려면 `config.py`, `session_log_writer.py`를 이전 구조로 복원하면 됩니다.
- 실거래 시나리오별 저장을 되돌리려면 `trader.py`의 세션 종료 저장부를 단일 `save_session_log(...)` 호출로 되돌리면 됩니다.
- 진단 메타데이터만 빼고 싶다면 `diagnostics` 생성 및 저장 인자를 제거하면 됩니다.
