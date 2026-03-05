"""
ATR% 기반 전략 파라미터 자동 계산 엔진 — AutoTuner

업비트 종목별 변동성(ATR%), 스프레드, 수수료를 기반으로
전략 파라미터를 동적으로 조정.

■ 3층 구조:
  1. global_defaults    — 전체 공통 기본값
  2. strategy.base_params — 전략별 기본 파라미터
  3. auto_tune.by_atr_pct — ATR% 범위별 오버라이드

■ ATR% 분류 (변동성):
  Low    : < 1.0%   → 보수적 파라미터 (EMA 길게, SL 타이트)
  Medium : 1.0~3.0% → 기본 파라미터
  High   : > 3.0%   → 공격적 파라미터 (EMA 짧게, SL 넓게)

■ 필터:
  fee_edge : ATR% >= fee_edge_mult × round_fee  (수수료 대비 기대폭)
  spread   : spread_bps <= max_spread_bps        (호가 스프레드 상한)
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ─── 심볼별 시장 데이터 ────────────────────────────────────────────────────────

@dataclass
class SymbolMetrics:
    """종목별 계산된 시장 지표 (auto-tune 입력)."""
    ticker: str
    last_close: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0         # ATR / last_close
    spread_bps: float = 0.0      # 10000 × (ask-bid) / mid
    acc_trade_value_24h: float = 0.0   # 24h 거래대금 (KRW)
    bid_fee: float = 0.0005      # 매수 수수료율
    ask_fee: float = 0.0005      # 매도 수수료율
    round_fee: float = 0.001     # bid_fee + ask_fee (왕복)


# ─── 튜닝된 파라미터 결과 ──────────────────────────────────────────────────────

@dataclass
class TunedParams:
    """auto-tune 적용된 최종 전략 파라미터."""
    ticker: str
    strategy_id: str
    atr_class: str = "medium"           # low / medium / high

    # EMA 계열
    ema_fast: int = 9
    ema_mid: int = 21
    ema_slow: int = 55

    # ADX / RSI
    adx_min: float = 25.0
    rsi_os: float = 30.0                # 과매도 RSI (mean-reversion용)
    rsi_entry_min: float = 50.0         # 추세 진입 최소 RSI

    # BB
    bb_len: int = 20
    bb_std: float = 2.0

    # 손절 / 익절 (ATR 배수)
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 2.0

    # 거래량
    vol_spike_mult: float = 2.0

    # 그리드
    grid_step_atr_mult: float = 0.5
    grid_max_adds: int = 3

    # 이격 (EMA deviation reversal)
    min_deviation_pct: float = -0.02

    # 트레일링
    trail_drawdown_pct: float = 0.015

    # 포지션 사이징
    risk_per_trade: float = 0.005       # 계좌 대비 1회 리스크 비율

    # 필터 통과 여부
    fee_edge_ok: bool = True            # ATR% >= fee_edge_mult × round_fee
    spread_ok: bool = True              # spread <= max_spread_bps

    # 최종 진입 가능 여부
    @property
    def entry_allowed(self) -> bool:
        return self.fee_edge_ok and self.spread_ok


# ─── Auto-Tune 규칙 정의 ──────────────────────────────────────────────────────

# ATR% 범위별 오버라이드 (user 제공 스펙 기반)
_EMA_PULLBACK_TUNE = [
    # (atr_pct_max, overrides)
    (0.01, {"ema_fast": 12, "ema_mid": 26, "ema_slow": 60, "adx_min": 28.0, "sl_atr_mult": 1.2}),
    (0.03, {"ema_fast":  9, "ema_mid": 21, "ema_slow": 55, "adx_min": 25.0, "sl_atr_mult": 1.5}),
    (1.00, {"ema_fast":  7, "ema_mid": 18, "ema_slow": 48, "adx_min": 22.0, "sl_atr_mult": 1.8}),
]

_BB_RSI_MEANREV_TUNE = [
    (0.01, {"rsi_os": 35.0, "sl_atr_mult": 1.5, "grid_step_atr_mult": 0.4, "bb_len": 20, "bb_std": 2.0}),
    (0.03, {"rsi_os": 30.0, "sl_atr_mult": 2.0, "grid_step_atr_mult": 0.5, "bb_len": 20, "bb_std": 2.0}),
    (1.00, {"rsi_os": 25.0, "sl_atr_mult": 2.5, "grid_step_atr_mult": 0.7, "bb_len": 15, "bb_std": 2.2}),
]

_EMA_DEVIATION_TUNE = [
    (0.03, {"vol_spike_mult": 2.0}),
    (1.00, {"vol_spike_mult": 1.6}),
]

_EMA_TREND_4H_TUNE = [
    (0.01, {"ema_fast": 30, "ema_slow": 90, "sl_atr_mult": 1.8}),
    (0.03, {"ema_fast": 20, "ema_slow": 60, "sl_atr_mult": 2.0}),
    (1.00, {"ema_fast": 15, "ema_slow": 50, "sl_atr_mult": 2.3}),
]

# 전략별 기본 파라미터 + auto_tune 규칙 매핑
_STRATEGY_CONFIGS: dict[str, dict] = {
    "scalping_triple_ema": {
        "base": {
            "ema_fast": 9, "ema_mid": 21, "ema_slow": 55,
            "adx_min": 25.0, "sl_atr_mult": 1.5,
            "trail_drawdown_pct": 0.015,
        },
        "tune": _EMA_PULLBACK_TUNE,
        "max_spread_bps": 25.0,
    },
    "scalping_5ema_reversal": {
        "base": {
            "ema_fast": 5, "vol_spike_mult": 2.0,
            "min_deviation_pct": -0.02,
        },
        "tune": _EMA_DEVIATION_TUNE,
        "max_spread_bps": 25.0,
        # 이격 자동조정: deviation = -max(0.02, 1.2 × ATR%)
        "deviation_auto": True,
    },
    "mr_bollinger": {
        "base": {
            "bb_len": 20, "bb_std": 2.0, "rsi_os": 30.0,
            "sl_atr_mult": 2.0, "grid_step_atr_mult": 0.5, "grid_max_adds": 3,
        },
        "tune": _BB_RSI_MEANREV_TUNE,
        "max_spread_bps": 20.0,
    },
    "mr_rsi": {
        "base": {
            "rsi_os": 35.0, "sl_atr_mult": 2.0,
        },
        "tune": _BB_RSI_MEANREV_TUNE,  # RSI 전략도 유사 규칙 적용
        "max_spread_bps": 20.0,
    },
    "macd_rsi_trend": {
        "base": {
            "rsi_entry_min": 50.0, "vol_spike_mult": 1.5,
            "sl_atr_mult": 2.0, "tp_atr_mult": 2.0,
        },
        "tune": _EMA_TREND_4H_TUNE,
        "max_spread_bps": 25.0,
    },
    "smrh_stop": {
        "base": {
            "rsi_entry_min": 50.0, "sl_atr_mult": 2.0,
        },
        "tune": _EMA_TREND_4H_TUNE,
        "max_spread_bps": 25.0,
    },
    "vb_noise_filter": {
        "base": {"sl_atr_mult": 1.5, "vol_spike_mult": 2.5},
        "tune": [],
        "max_spread_bps": 30.0,
    },
    "vb_standard": {
        "base": {"sl_atr_mult": 1.5},
        "tune": [],
        "max_spread_bps": 30.0,
    },
}

# 글로벌 기본 설정
_GLOBAL_DEFAULTS = {
    "fee_edge_mult": 3.0,      # ATR% ≥ fee_edge_mult × round_fee
    "risk_per_trade": 0.005,   # 계좌 대비 0.5%
    "spread_bps_hard_limit": 25.0,
}


class AutoTuner:
    """
    종목별 시장 지표 → 전략 파라미터 자동 계산.

    사용법:
        tuner = AutoTuner()
        metrics = SymbolMetrics(ticker="KRW-BTC", atr_pct=0.018, spread_bps=3.2, ...)
        params = tuner.compute("scalping_triple_ema", metrics)
        if params.entry_allowed:
            # 파라미터 사용
    """

    def __init__(self, fee_edge_mult: float = 3.0, risk_per_trade: float = 0.005) -> None:
        self._fee_edge_mult = fee_edge_mult
        self._risk_per_trade = risk_per_trade

    def compute(self, scenario_id: str, metrics: SymbolMetrics) -> TunedParams:
        """
        전략 + 종목 지표 → 최종 파라미터 계산.

        1. 전략별 기본 파라미터 로드
        2. ATR% 범위에 따라 오버라이드 적용
        3. 스프레드/수수료 필터 판정
        4. TunedParams 반환
        """
        cfg = _STRATEGY_CONFIGS.get(scenario_id)
        if cfg is None:
            logger.warning(f"[AutoTuner] 미등록 전략: {scenario_id} → 기본값 반환")
            return TunedParams(ticker=metrics.ticker, strategy_id=scenario_id)

        # 1) 기본 파라미터
        result = TunedParams(ticker=metrics.ticker, strategy_id=scenario_id)
        base = cfg.get("base", {})
        for k, v in base.items():
            if hasattr(result, k):
                setattr(result, k, v)

        result.risk_per_trade = self._risk_per_trade

        # 2) ATR% 분류
        atr_pct = metrics.atr_pct
        if atr_pct < 0.01:
            result.atr_class = "low"
        elif atr_pct < 0.03:
            result.atr_class = "medium"
        else:
            result.atr_class = "high"

        # 3) ATR% 범위별 오버라이드
        tune_rules = cfg.get("tune", [])
        for max_atr, overrides in tune_rules:
            if atr_pct < max_atr:
                for k, v in overrides.items():
                    if hasattr(result, k):
                        setattr(result, k, v)
                break

        # 4) 이격 자동조정 (ema_deviation_reversal 전용)
        if cfg.get("deviation_auto"):
            result.min_deviation_pct = -max(0.02, 1.2 * atr_pct)

        # 5) 필터: 수수료 대비 기대폭 (fee_edge)
        round_fee = metrics.round_fee or 0.001
        fee_edge_threshold = self._fee_edge_mult * round_fee
        result.fee_edge_ok = atr_pct >= fee_edge_threshold

        # 6) 필터: 스프레드 상한
        max_spread = cfg.get("max_spread_bps", _GLOBAL_DEFAULTS["spread_bps_hard_limit"])
        result.spread_ok = metrics.spread_bps <= max_spread

        if not result.fee_edge_ok:
            logger.debug(
                f"[AutoTuner] {metrics.ticker}/{scenario_id} "
                f"fee_edge 미달 ATR%={atr_pct:.4f} < {fee_edge_threshold:.4f}"
            )
        if not result.spread_ok:
            logger.debug(
                f"[AutoTuner] {metrics.ticker}/{scenario_id} "
                f"spread 초과 {metrics.spread_bps:.1f}bps > {max_spread:.1f}bps"
            )

        return result

    def compute_position_size(
        self,
        equity_krw: float,
        entry_price: float,
        sl_price: float,
        risk_per_trade: float | None = None,
        min_order_krw: float = 5000.0,
    ) -> float:
        """
        ATR 기반 포지션 사이징.

        공식: qty = (equity × risk_ratio) / |entry - SL|
        제약: qty × entry >= min_order_krw

        Parameters
        ----------
        equity_krw : 계좌 총 자산 (KRW)
        entry_price : 진입 예상 가격
        sl_price : 손절 가격
        risk_per_trade : 1회 리스크 비율 (None이면 기본값 사용)
        min_order_krw : 최소 주문 금액

        Returns
        -------
        투자 금액 (KRW). 0이면 진입 불가.
        """
        r = risk_per_trade or self._risk_per_trade
        stop_dist = abs(entry_price - sl_price)
        if stop_dist <= 0 or entry_price <= 0:
            return 0.0

        risk_krw = equity_krw * r
        qty = risk_krw / stop_dist
        order_krw = qty * entry_price

        if order_krw < min_order_krw:
            return 0.0

        return round(order_krw, 0)

    @staticmethod
    def classify_atr(atr_pct: float) -> str:
        """ATR% → 변동성 분류 (low / medium / high)."""
        if atr_pct < 0.01:
            return "low"
        if atr_pct < 0.03:
            return "medium"
        return "high"

    @staticmethod
    def available_strategies() -> list[str]:
        """등록된 전략 목록."""
        return list(_STRATEGY_CONFIGS.keys())
