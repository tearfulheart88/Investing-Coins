import os
from dotenv import load_dotenv


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
ENV_PATH: str = os.path.join(BASE_DIR, ".env")
LOCAL_ENV_PATH: str = os.path.join(BASE_DIR, ".env.local")

load_dotenv(ENV_PATH)
load_dotenv(LOCAL_ENV_PATH, override=True)

# ─── API 인증 ────────────────────────────────────────────────────────────────
ACCESS_KEY: str = os.environ.get("UPBIT_ACCESS_KEY", "")
SECRET_KEY: str = os.environ.get("UPBIT_SECRET_KEY", "")

# ─── 전략 선택 ────────────────────────────────────────────────────────────────
# 변경만으로 전략 교체: vb_noise_filter / vb_standard / mr_rsi / mr_bollinger
# 기본 단일전략은 실거래 기본 배치의 첫 번째 전략과 맞춘다.
SELECTED_STRATEGY: str = "mean_reversion"
SELECTED_SCENARIO: str = "mr_rsi"

# ─── 거래 대상 & 예산 ─────────────────────────────────────────────────────────
TICKERS: list = ["KRW-BTC", "KRW-ETH", "KRW-SOL"]
BUDGET_PER_TRADE: int = 100_000          # 종목당 고정 투자금 (KRW)

# ─── 종목 블랙리스트 ─────────────────────────────────────────────────────────
TICKER_BLACKLIST: list = [
    # 스테이블코인
    "KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-BUSD",
    # 데이터 부족 (신규 상장 / 4h봉 200개 미달 → Code not found / OHLCV 오류 반복)
    "KRW-EDGE",   # 상장 ~6일, 4h봉 38개 (필요 200개)
    "KRW-SIGN",   # 신규 상장, OHLCV 데이터 부족
    # 거래 불가 / Code not found (Upbit API가 마켓 코드 자체를 인식 못함)
    "KRW-PDA",    # get_current_price() → Code not found (API 레벨 오류)
    "KRW-NU",     # get_current_price() → Code not found (API 레벨 오류)
]

# ─── 동적 종목 선택 (거래대금 상위 N개 자동 선택) ──────────────────────────────
# USE_DYNAMIC_TICKERS = True 이면 TICKERS 무시, 24h 거래대금 기준 상위 N개 자동 선택
# TOP_TICKERS_COUNT  : 10 단위로 설정 권장 (10 / 20 / 30 … 100)
#                      예) 10 → 상위 10개, 20 → 상위 20개
# TICKER_REFRESH_HOURS: 종목 목록 갱신 주기(시간). 기본 24h (매일 시가 기준 재선정)
USE_DYNAMIC_TICKERS: bool = False
TOP_TICKERS_COUNT: int = 10
TICKER_REFRESH_HOURS: float = 1.0

# ─── 리스크 관리 ──────────────────────────────────────────────────────────────
STOP_LOSS_PCT: float = 0.03             # 3% 손절
MAX_DRAWDOWN_PCT: float = 0.10          # 10% 최대 낙폭 제한

# ─── 전략 파라미터 ────────────────────────────────────────────────────────────
NOISE_FILTER_DAYS: int = 5              # 노이즈 k 계산 기간 (vb_noise_filter)
MA_PERIOD: int = 15                     # 이동평균선 기간 (VB 계열)
RSI_PERIOD: int = 14                    # RSI 기간 (mr_rsi)
BB_PERIOD: int = 20                     # 볼린저 밴드 기간 (mr_bollinger)
BB_STD: float = 2.0                     # 볼린저 밴드 표준편차 배수

# ─── 스케줄 ──────────────────────────────────────────────────────────────────
SELL_HOUR_KST: int = 9                  # 익일 09:00 KST 스케줄 매도
SELL_MINUTE_KST: int = 0

# ─── 주문 제약 ────────────────────────────────────────────────────────────────
MIN_ORDER_KRW: int = 5_000              # 업비트 최소 주문 금액
MAX_ORDER_KRW: int = 500_000            # 1종목당 최대 투자 금액 (KRW)
FEE_RATE: float = 0.0005               # 업비트 수수료율 0.05%
ORDER_CONFIRM_TIMEOUT_SEC: int = 30     # 주문 체결 확인 폴링 타임아웃
ORDER_CONFIRM_POLL_SEC: float = 0.5     # 체결 확인 폴링 간격
ORDER_SM_ENTRY_TIMEOUT_SEC: float = 10.0   # 매수 체결 대기 타임아웃 (상태머신)
ORDER_SM_EXIT_TIMEOUT_SEC: float = 10.0    # 매도 체결 대기 타임아웃 (상태머신)

# ─── 루프 & 레이트 리밋 ──────────────────────────────────────────────────────
PRICE_CHECK_INTERVAL_SEC: int = 10      # 매수 신호 확인 주기 (초) — 5→10 (API/CPU 부하 절감)
EXCHANGE_POSITION_SYNC_SEC: int = 60    # 거래소 잔고/포지션 자동 동기화 주기 (초)
ORDER_RATE_LIMIT_PER_SEC: int = 5       # 주문 API 보수적 제한 (업비트: 8/s)
DATA_RATE_LIMIT_PER_SEC: int = 8        # 시세 API 보수적 제한 (업비트: 10/s)
WEBSOCKET_STALE_SEC: float = 10.0       # 이 시간 초과 시 REST 폴백

# ─── 경로 ────────────────────────────────────────────────────────────────────
LOGS_DIR: str = os.path.join(BASE_DIR, "logs")
TRADES_JSON_PATH: str = os.path.join(LOGS_DIR, "trades", "trades.json")
POSITIONS_PATH: str = os.path.join(LOGS_DIR, "state", "positions.json")
SYSTEM_LOG_DIR: str = os.path.join(LOGS_DIR, "system")
REAL_LOG_DIR: str = os.path.join(LOGS_DIR, "real")
PAPER_LOG_DIR: str = os.path.join(LOGS_DIR, "paper")
SESSIONS_DIR: str = os.path.join(LOGS_DIR, "sessions")
ANALYSIS_DIR: str = os.path.join(LOGS_DIR, "analysis")
ANALYSIS_REAL_DIR: str = os.path.join(ANALYSIS_DIR, "real")
ANALYSIS_PAPER_DIR: str = os.path.join(ANALYSIS_DIR, "paper")
SIGNAL_TRACE_DIR: str = os.path.join(LOGS_DIR, "signal_traces")
SIGNAL_TRACE_REAL_DIR: str = os.path.join(SIGNAL_TRACE_DIR, "real")
SIGNAL_TRACE_PAPER_DIR: str = os.path.join(SIGNAL_TRACE_DIR, "paper")
REAL_PERFORMANCE_MD_PATH: str = os.path.join(REAL_LOG_DIR, "realized_performance.md")

# ─── 가상거래 ─────────────────────────────────────────────────────────────────
PAPER_TRADING: bool = False             # True = 가상거래, False = 실제거래

# ─── 예산 퍼센트 기본값 ──────────────────────────────────────────────────────
BUDGET_PER_TRADE_PCT: float = 30.0             # 단일전략/레거시 기본값
DEFAULT_WEIGHT_PCT: float = 100.0              # 단일 전략 시 계좌 전체 사용
REAL_DEFAULT_BUDGET_PCT: float = 50.0          # 실거래 기본 1회 매매 비중

# ─── 실거래 기본 시나리오 ─────────────────────────────────────────────────────
# 현재 운영 기본값: 안정형 2전략만 실거래 투입
REAL_SCENARIO_DEFAULTS: list[dict] = [
    {
        "strategy_id": "mean_reversion",
        "scenario_id": "mr_rsi",
        "weight_pct": 50.0,
        "ticker_count": 10,
        "budget_pct": 50.0,
    },
    {
        "strategy_id": "trend_following",
        "scenario_id": "smrh_stop",
        "weight_pct": 50.0,
        "ticker_count": 10,
        "budget_pct": 50.0,
    },
]

# ─── 가상거래 시나리오 기본값 ──────────────────────────────────────────────────
PAPER_TOTAL_BUDGET: int = 1_000_000            # 전체 가상거래 예산 (전략 수 × 100,000 — UI에서 자동 갱신)
PAPER_DEFAULT_BALANCE: int = 100_000           # 시나리오별 기본 초기자금 (KRW)
PAPER_DEFAULT_BUDGET_PCT: float = 50.0         # 시나리오 잔고의 50%를 1회 거래에 사용
PAPER_DEFAULT_TICKER_COUNT: int = 10           # 시나리오별 기본 종목 수
PAPER_TICKER_COUNT_OPTIONS: list = [3, 5, 10, 30, 50, 100]

# ─── 가상거래 기본 시나리오 (전략 검증용) ────────────────────────────────────
# 실거래는 안정형만, 공격형은 가상계좌에서 충분히 로그를 쌓으며 검증한다.
PAPER_SCENARIO_DEFAULTS: list[dict] = [
    {"scenario_id": "vb_noise_filter",       "ticker_count": 10, "budget_pct": 50.0, "profile": "neutral"},
    {"scenario_id": "vb_standard",           "ticker_count": 10, "budget_pct": 50.0, "profile": "neutral"},
    {"scenario_id": "mr_rsi",                "ticker_count": 10, "budget_pct": 50.0, "profile": "stable"},
    {"scenario_id": "mr_bollinger",          "ticker_count": 10, "budget_pct": 50.0, "profile": "stable"},
    {"scenario_id": "scalping_triple_ema",   "ticker_count": 5,  "budget_pct": 40.0, "profile": "aggressive"},
    {"scenario_id": "scalping_bb_rsi",       "ticker_count": 5,  "budget_pct": 40.0, "profile": "aggressive"},
    {"scenario_id": "scalping_5ema_reversal","ticker_count": 5,  "budget_pct": 40.0, "profile": "aggressive"},
    {"scenario_id": "pump_catcher",          "ticker_count": 10, "budget_pct": 30.0, "profile": "aggressive"},
    {"scenario_id": "macd_rsi_trend",        "ticker_count": 10, "budget_pct": 50.0, "profile": "neutral"},
    {"scenario_id": "smrh_stop",             "ticker_count": 10, "budget_pct": 50.0, "profile": "stable"},
]

# ─── 옵시디언 ─────────────────────────────────────────────────────────────────
OBSIDIAN_VAULT_PATH: str = ""           # 볼트 경로 (비어있으면 비활성화)
OBSIDIAN_FOLDER: str = "자동매매"        # 볼트 내 하위 폴더

# ─── 알림 ─────────────────────────────────────────────────────────────────────
NOTIFICATION_INTERVAL_HOURS: float = 3.0   # 요약 알림 주기 (시간)

# Telegram notifications (local .env.local preferred)
TELEGRAM_ENABLED: bool = _env_bool("TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_NOTIFY_REAL_SELLS: bool = _env_bool("TELEGRAM_NOTIFY_REAL_SELLS", True)
TELEGRAM_NOTIFY_REAL_STOP_SUMMARY: bool = _env_bool("TELEGRAM_NOTIFY_REAL_STOP_SUMMARY", True)

# ─── 호가 WebSocket (OrderbookManager) ──────────────────────────────────────
ORDERBOOK_WS_ENABLED: bool = True           # 호가 WS 활성화 (스프레드 계산용)

# ─── 포지션 사이징 (PositionSizer) ──────────────────────────────────────────
# USE_ATR_SIZING = True 이면 BUDGET_PER_TRADE 대신 ATR 기반 동적 사이징.
USE_ATR_SIZING: bool = False                # True = ATR 기반, False = 고정금액
RISK_PER_TRADE: float = 0.005               # 계좌 대비 1회 리스크 비율 (0.5%)
MAX_POSITION_PCT: float = 0.20              # 계좌 대비 1종목 최대 비중 (20%)

# ─── AutoTuner ──────────────────────────────────────────────────────────────
USE_AUTO_TUNER: bool = False                # ATR% 기반 파라미터 자동 조정
FEE_EDGE_MULT: float = 3.0                 # ATR% >= fee_edge_mult × round_fee

# ─── UniverseSelector (스코어 기반 종목 선정) ────────────────────────────────
USE_SCORE_SELECTION: bool = False            # True = 스코어 기반, False = 거래대금만
MIN_24H_VALUE_KRW: float = 5_000_000_000    # 최소 24h 거래대금 (50억)
MAX_SPREAD_BPS: float = 25.0                # 최대 호가 스프레드 (bps)

# ─── 세션 시간 ────────────────────────────────────────────────────────────────
SESSION_DURATION_SEC: int | None = None     # None = 무제한

# ─── 전략별 세부 파라미터 (UI 슬라이더로 조정 가능) ────────────────────────────
STRATEGY_PARAMS: dict = {
    "vb": {                              # vb_noise_filter / vb_standard 공통
        "noise_filter_days":  5,         # 노이즈 k 계산 기간 (일봉)
        "ma_period":          15,        # MA 필터 기간
        "k_min":              0.3,       # K 클램프 하한 (공격적 진입 방지)
        "k_max":              0.8,       # K 클램프 상한 (지나친 보수 방지)
        "time_cut_hours":     2.5,       # v5: 2.0→2.5h (추세 형성 여유 시간 확보)
        "min_momentum_pct":   0.5,       # v5: 0.3→0.5% (유의미한 모멘텀 구분 기준)
        "vol_mult":           2.0,       # v5: 2.5→2.0 (매수 기회 과도 제한 방지)
        "be_trigger_pct":     1.0,       # v3: 본절방어 활성화 기준 (peak PnL ≥ N%)
        "be_floor_pct":       0.2,       # v3: 본절방어 최소수익률 (SL→진입가+N%)
        "trail_drop_pct":     1.0,       # v5: 0.5→1.0% (노이즈 조기청산 방지, ATR 적응)
        "use_atr_trail":      True,      # v5: ATR 기반 동적 트레일링 활성화
        "atr_trail_mult":     0.5,       # v5: ATR%의 N배를 trail 폭으로 (최소=trail_drop_pct)
        "ema200_filter":      True,      # v6: EMA200(4h) 장기 추세 필터 (하락 추세 제외)
        "adx_min_vb":         15.0,      # v6: ADX 최소 추세 강도 (0=비활성, VB 횡보 필터)
    },
    "mr_rsi": {
        "rsi_buy":            30.0,      # v5: RSI 과매도 매수 기준 강화 (추세장)
        "rsi_buy_range":      32.0,      # v5: RSI 완화 매수 기준 강화 (약한 횡보)
        "rsi_sell":           65.0,      # RSI 회복 매도 기준
        "adx_range_thr":      15.0,      # v5: 이 ADX 미만 → 완화 매수 기준 적용
        "max_hold_hours":     24.0,      # 최대 보유 시간 (초과 시 강제 청산)
        "hard_sl_pct":        7.0,       # v5: 전략 내부 하드 손절 (%)
    },
    "mr_bollinger": {
        "rsi_buy":            35.0,      # RSI 과매도 기준
        "adx_limit":          25.0,      # 횡보 필터 ADX 한도 (초과 시 진입 금지)
        "bb_period":          20,        # 볼린저 밴드 기간
        "bb_std_trend":       2.0,       # 추세장 BB 표준편차 (ADX >= adx_range_thr)
        "bb_std_range":       1.5,       # 약한 횡보장 BB 표준편차 (ADX < adx_range_thr)
        "adx_range_thr":      20.0,      # 이 ADX 미만 → 좁은 밴드 적용
        "max_hold_hours":     48.0,      # 최대 보유 시간 (초과 시 강제 청산)
    },
    "scalping_triple_ema": {
        "tp_pct":          0.6,          # Trailing 활성화 기준 TP% (0.6 → +0.6%)
        "sl_pct":          0.3,          # 손절 / trailing 폭 (0.3 → -0.3%)
        "adx_min":         20.0,         # ADX 최소 추세 강도 (횡보 필터)
        "ema_spread_min":  0.3,          # EMA10-EMA50 이격도 최소 기준 (%)
        "trail_min_pct":   1.5,          # 트레일링 최소 폭 (% — 최소 1.5% 버퍼)
    },
    "scalping_bb_rsi": {
        "rsi_buy":   30.0,               # RSI 과매도 기준
        "adx_limit": 25.0,               # 횡보 필터 ADX 한도
        "atr_mult":  1.2,                # ATR 손절 배수
    },
    "scalping_5ema_reversal": {
        "rr_ratio":         3.0,         # 손익비 RR (TP = entry + SL_dist × RR)
        "adx_min":          20.0,        # ADX 최소 추세 강도 (횡보 필터)
        "rsi_entry_max":    40.0,        # RSI 진입 최대값 (과매도 확인, RSI < 이 값)
        "vol_mult":         1.5,         # 거래량 급증 배수 기준 (Vol_SMA × vol_mult)
        "time_cut_min":     15.0,        # 타임컷 기준 시간(분) — 5분봉 기준 3캔들
        "min_momentum_pct": 0.5,         # 타임컷 최소 수익률 기준 (%)
    },
    "macd_rsi_trend": {
        "rsi_entry_min": 55.0,           # RSI 최소 진입 기준 (가짜 반등 필터)
        "rsi_sl":        45.0,           # RSI 손절 기준 (추세 약화)
        "vol_mult":      1.5,            # 거래량 급증 배수 기준 (Vol_SMA × vol_mult)
        # 진입: MACD 골든크로스 (제로라인 아래)  / 청산: MACD 데드크로스
    },
    "smrh_stop": {
        "rsi_min":      50.0,            # 4h + 30m 공통 RSI 최소 기준
        "macd_signal":  70,              # MACD 시그널 기간 (원본 명세: 70)
    },
    "pump_catcher": {
        "vol_mult":              8.0,    # 거래량 폭발 배수 (SMA20 × N배 이상) — v2: 15→8 (0거래 해소)
        "spike_pct":             1.5,    # 1분봉 시가 대비 최소 급등률 (%) — v2: 3.0→1.5
        "max_gain_from_open":   15.0,    # 일봉 시가 대비 최대 허용 상승률 (%)
        "min_body_ratio":        0.5,    # 양봉 몸통 비율 하한 (설거지 위꼬리 방지)
        "rsi_max":              85.0,    # RSI 최대 허용값 (과열 방지)
        "trail_pct":             2.0,    # 기본 트레일링 스탑 (%)
        "hard_sl_pct":           3.0,    # 하드 손절 (%)
        "tp_lock_pct":           5.0,    # 수익 보존 강화 발동 기준 (%)
        "trail_locked_pct":      1.0,    # 수익 보존 후 좁혀진 트레일링 (%)
        "vol_fade_mult":         2.0,    # 거래량 소멸 판정 기준 배수
        "max_hold_minutes":     10.0,    # 최대 보유 시간 (분)
        "cooldown_minutes":     30.0,    # 동일 종목 재진입 쿨다운 (분)
    },
}

# ─── Gemini API (전략 분석 & Claude 프롬프트 생성) ──────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# ─── GitHub 업로드 기능 (로컬 전용) ────────────────────────────────────────
# .env 에 GITHUB_UPLOAD_ENABLED=true 설정 시에만 UI 버튼 표시
# 미설정 또는 false → 버튼 숨김 (다른 사람이 클론해도 업로드 불가)
GITHUB_UPLOAD_ENABLED: bool = _env_bool("GITHUB_UPLOAD_ENABLED", False)
GEMINI_MAX_TRADES: int = 50              # 분석에 사용할 최근 거래 수

# ─── 수익 재진입 (Re-entry) ───────────────────────────────────────────────────
# 수익 구간 매도 신호 발생 시 실제 매도하지 않고, 해당 가격을 새 매수단가로
# 갱신하여 포지션을 유지합니다. 종목별 손절가는 새 단가 기준으로 재계산됩니다.
#
# REENTRY_ENABLED_SCENARIOS: 재진입을 활성화할 시나리오 ID 집합
#   예) {"vb_noise_filter", "mr_rsi"}
#       빈 set() = 전체 비활성화
REENTRY_ENABLED_SCENARIOS: set = {      # 기본값: 전체 전략 활성화
    "vb_noise_filter",
    "vb_standard",
    "mr_rsi",
    "mr_bollinger",
    "scalping_triple_ema",
    "scalping_bb_rsi",
    "scalping_5ema_reversal",
    "macd_rsi_trend",
    "pump_catcher",             # 거래량 폭발 펌핑 스캘핑
}
