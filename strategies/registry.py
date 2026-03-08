"""
전략 레지스트리.
모든 전략 임포트를 이 파일에서만 중앙 관리.
새 전략 추가: 파일 생성 후 STRATEGY_MAP에 한 줄 추가.
"""
from strategies.vb_noise_filter        import VBNoiseFilterStrategy
from strategies.vb_standard            import VBStandardStrategy
from strategies.mr_rsi                 import RSIStrategy
from strategies.mr_bollinger           import BollingerStrategy
from strategies.grid_arithmetic        import GridArithmeticStrategy
from strategies.scalping_triple_ema    import TripleEMAStrategy
from strategies.scalping_bb_rsi        import ScalpingBBRSIStrategy
from strategies.scalping_5ema_reversal import FiveEMAReversalStrategy
from strategies.macd_rsi_trend         import MACDRSITrendStrategy
from strategies.smrh_stop              import SMRHStopStrategy
from strategies.pump_catcher           import PumpCatcherStrategy
from strategies.base_strategy          import BaseStrategy

STRATEGY_MAP: dict[tuple[str, str], type] = {
    ("volatility_breakout", "vb_noise_filter")       : VBNoiseFilterStrategy,
    ("volatility_breakout", "vb_standard")           : VBStandardStrategy,
    ("mean_reversion",      "mr_rsi")                : RSIStrategy,
    ("mean_reversion",      "mr_bollinger")          : BollingerStrategy,
    ("grid_trading",        "grid_arithmetic")       : GridArithmeticStrategy,
    ("scalping",            "scalping_triple_ema")   : TripleEMAStrategy,
    ("scalping",            "scalping_bb_rsi")       : ScalpingBBRSIStrategy,
    ("scalping",            "scalping_5ema_reversal"): FiveEMAReversalStrategy,
    ("scalping",            "pump_catcher")          : PumpCatcherStrategy,
    ("trend_following",     "macd_rsi_trend")        : MACDRSITrendStrategy,
    ("trend_following",     "smrh_stop")             : SMRHStopStrategy,
}


def load_strategy(market_data, strategy_id: str, scenario_id: str) -> BaseStrategy:
    """
    config에서 지정한 전략을 로드.
    등록되지 않은 전략 ID → ValueError 발생.
    """
    key = (strategy_id, scenario_id)
    cls = STRATEGY_MAP.get(key)
    if cls is None:
        available = list(STRATEGY_MAP.keys())
        raise ValueError(
            f"등록되지 않은 전략: {key}\n"
            f"사용 가능한 전략: {available}"
        )
    return cls(market_data)
