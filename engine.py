"""
거래 관리 엔진 (Trade Management Engine)
ATR 기반 손절·본전보호·추세추종·추가진입·리스크 관리 엔진
"""

import math
import json
import os
from datetime import datetime
from typing import Optional, Dict, List, Any
from copy import deepcopy

import yfinance as yf
import pandas as pd


def _safe_float(v):
    """NaN/Inf를 0으로 변환 (JSON 직렬화 안전)"""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return 0
    return v


# ─── 기본 설정 ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    # ── Entry ──
    "position_direction": "long",       # long / short
    "entry_source": "manual",           # bar_close / manual
    "trade_start_bar": 0,               # 거래 시작 봉 인덱스 (bar_close 모드)
    "manual_entry_price": 0.0,
    "manual_activation": "immediate",   # immediate / first_touch
    "manual_uses_start_time": False,

    # ── Position Sizing ──
    "initial_qty": 1.0,
    "sizing_mode": "manual",            # manual / risk_based
    "total_capital": 10000000.0,
    "risk_per_trade_pct": 1.0,
    "qty_preset": "stocks",             # stocks / futures / crypto / custom
    "custom_rounding": "floor",         # none / floor / round / ceil
    "custom_qty_step": 1.0,
    "show_warnings": True,

    # ── Manual Scale-In ──
    "enable_manual_scale_ins": False,
    "scale_in_stop_handling": "keep",        # keep / rebase / rebase_no_wider / soft_rebase / block
    "pyramid_stop_handling": "keep",
    "avg_down_stop_handling": "keep",
    "soft_rebase_weight": 0.5,
    "cap_to_risk_budget": True,
    "min_add_spacing_atr": 0.5,
    "max_pyramid_adds": 3,
    "max_avg_down_adds": 2,
    "avg_down_allowed_until": "always",      # always / before_trail_armed / before_breakeven
    "manual_add_fill_mode": "first_touch",   # first_touch / scheduled_close
    "manual_adds": [],  # [{bar, price, qty, type: pyramid/avg_down}, ...]

    # ── Conditional Pyramid ──
    "enable_conditional_pyramid": False,
    "auto_add_trigger": "trail_armed",       # trail_armed / breakeven_active / new_extreme / mfe_step
    "auto_add_qty_mode": "fixed",            # fixed / pct_initial / risk_remaining
    "auto_add_fixed_qty": 1.0,
    "auto_add_qty_pct": 50.0,
    "max_auto_adds": 2,
    "auto_add_cooldown_bars": 3,
    "auto_add_base_mfe_atr": 1.5,
    "auto_add_step_mfe_atr": 1.0,

    # ── Reset ──
    "trade_id": 1,

    # ── ATR ──
    "atr_length": 14,

    # ── Profile A Stop ──
    "initial_stop_atr_mult": 2.0,
    "trail_arm_trigger_atr": 1.0,
    "trail_arm_after_bars": 5,
    "time_arm_min_progress_atr": 0.3,
    "trail_arm_on_new_extreme": True,
    "trailing_atr_mult": 2.5,
    "use_breakeven_ratchet": True,
    "breakeven_trigger_atr": 1.5,
    "breakeven_offset_atr": 0.1,
    "stop_hit_mode": "wick",             # wick / close
    "update_on_bar_close": True,
    "intrabar_conflict": "conservative",  # legacy / conservative

    # ── Basis ──
    "trend_basis_reset": "initial_entry",   # initial_entry / reset_on_rebase
    "breakeven_basis": "risk_basis",        # risk_basis / trend_basis
    "pyramid_favorable_basis": "risk_basis", # risk_basis / trend_basis / pyramid_basis

    # ── Adaptive Management ──
    "enable_adaptive_arm_be": True,
    "enable_adaptive_trailing": True,
    "use_hybrid_trailing_atr": False,
    "adaptive_lookback": 20,
    "atr_regime_lookback": 50,
    "early_mfe_mae_window": 5,
    "use_trade_memory": True,
    "adaptive_history_trades": 5,
    "adaptive_history_weight": 0.3,
    "trailing_atr_ema_length": 10,
    "trailing_atr_higher_tf_weight": 0.3,
    "trailing_atr_early_bars": 3,
    "trailing_atr_expansion_cap": 2.0,
    "trailing_atr_contraction_floor": 0.5,
    "adaptive_arm_be_strength": 1.0,
    "adaptive_trailing_strength": 1.0,
    "adaptive_arm_min_atr": 0.5,
    "adaptive_arm_max_atr": 3.0,
    "adaptive_be_min_atr": 0.8,
    "adaptive_be_max_atr": 3.0,
    "adaptive_trailing_min_atr": 1.0,
    "adaptive_trailing_max_atr": 5.0,
    "enable_quality_gate": True,
    "quality_min_trend_mfe_atr": 1.0,
    "quality_min_stop_improvement_atr": 0.3,
    "quality_min_efficiency": 0.3,
    "quality_min_headroom_pct": 20.0,

    # ── Display ──
    "label_language": "ko",  # ko / en

    # ── Market Data ──
    "ticker_symbol": "",           # 종목 코드 (예: 005930.KS, AAPL, BTCUSD=X)
    "ticker_name": "",             # 종목 이름
    "data_interval": "1d",         # 봉 간격 (1m,5m,15m,30m,60m,1d,1wk,1mo)
    "data_count": 200,             # 가져올 봉 수
    "auto_refresh": False,         # 자동 갱신 활성화
    "auto_refresh_sec": 60,        # 자동 갱신 주기 (초)
}


# ─── 유틸리티 ───────────────────────────────────────────────

def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def round_qty(qty, preset, rounding="floor", step=1.0):
    if preset == "stocks":
        step = 1.0
    elif preset == "futures":
        step = 1.0
    elif preset == "crypto":
        step = 0.001
    # custom uses provided step

    if step <= 0:
        step = 1.0

    if rounding == "none":
        return qty
    elif rounding == "floor":
        return math.floor(qty / step) * step
    elif rounding == "round":
        return round(qty / step) * step
    elif rounding == "ceil":
        return math.ceil(qty / step) * step
    return qty


# ─── 바 데이터 ──────────────────────────────────────────────

class Bar:
    def __init__(self, timestamp: str, open_p: float, high: float, low: float,
                 close: float, volume: float = 0):
        self.timestamp = timestamp
        self.open = open_p
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "open": _safe_float(self.open),
            "high": _safe_float(self.high),
            "low": _safe_float(self.low),
            "close": _safe_float(self.close),
            "volume": _safe_float(self.volume),
        }

    @staticmethod
    def from_dict(d):
        return Bar(d["timestamp"], d["open"], d["high"], d["low"],
                   d["close"], d.get("volume", 0))


# ─── 거래 상태 ──────────────────────────────────────────────

class TradeState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.direction = "long"
        self.entry_price = 0.0
        self.entry_time = ""
        self.entry_bar_idx = 0
        self.avg_entry = 0.0
        self.total_qty = 0.0
        self.initial_qty = 0.0

        # Stops
        self.initial_stop = 0.0
        self.trailing_stop = 0.0
        self.active_stop = 0.0
        self.breakeven_stop = 0.0
        self.prev_active_stop = 0.0

        # Trail
        self.trail_armed = False
        self.trail_arm_bar = 0
        self.trail_arm_reason = ""

        # Breakeven
        self.breakeven_active = False
        self.breakeven_bar = 0

        # Extremes
        self.highest = 0.0
        self.lowest = float("inf")
        self.mfe = 0.0
        self.mae = 0.0

        # Bars
        self.bars_in_trade = 0

        # Scale-in
        self.pyramid_count = 0
        self.avg_down_count = 0
        self.auto_add_count = 0
        self.last_add_bar = -999
        self.last_add_price = 0.0
        self.fills = []  # [{bar, price, qty, type, reason}]

        # Risk
        self.initial_risk_per_unit = 0.0
        self.initial_risk_total = 0.0
        self.current_risk = 0.0
        self.max_risk = 0.0
        self.risk_budget = 0.0

        # ATR at entry
        self.entry_atr = 0.0

        # Basis
        self.risk_basis = 0.0
        self.trend_basis = 0.0
        self.pyramid_basis = 0.0

        # Adaptive
        self.adaptive_arm_mult = 1.0
        self.adaptive_be_mult = 1.0
        self.adaptive_trail_mult = 1.0
        self.noise_score = 0.0
        self.trend_efficiency = 0.0
        self.atr_regime = 1.0

        # P&L
        self.unrealized_pnl = 0.0
        self.unrealized_pnl_pct = 0.0
        self.r_multiple = 0.0

        # Events
        self.events = []

        # Stop line history (for chart)
        self.stop_history = []

    def to_dict(self):
        _s = lambda v, n=6: _safe_float(round(v, n))
        return {
            "active": self.active,
            "direction": self.direction,
            "entry_price": _s(self.entry_price),
            "entry_time": self.entry_time,
            "entry_bar_idx": self.entry_bar_idx,
            "avg_entry": _s(self.avg_entry),
            "total_qty": _s(self.total_qty),
            "initial_qty": _s(self.initial_qty),
            "initial_stop": _s(self.initial_stop),
            "trailing_stop": _s(self.trailing_stop),
            "active_stop": _s(self.active_stop),
            "breakeven_stop": _s(self.breakeven_stop),
            "trail_armed": self.trail_armed,
            "trail_arm_reason": self.trail_arm_reason,
            "breakeven_active": self.breakeven_active,
            "highest": _s(self.highest),
            "lowest": _s(self.lowest),
            "mfe": _s(self.mfe, 4),
            "mae": _s(self.mae, 4),
            "bars_in_trade": self.bars_in_trade,
            "pyramid_count": self.pyramid_count,
            "avg_down_count": self.avg_down_count,
            "auto_add_count": self.auto_add_count,
            "initial_risk_per_unit": _s(self.initial_risk_per_unit),
            "initial_risk_total": _s(self.initial_risk_total, 2),
            "current_risk": _s(self.current_risk, 2),
            "max_risk": _s(self.max_risk, 2),
            "risk_budget": _s(self.risk_budget, 2),
            "entry_atr": _s(self.entry_atr),
            "risk_basis": _s(self.risk_basis),
            "trend_basis": _s(self.trend_basis),
            "pyramid_basis": _s(self.pyramid_basis),
            "adaptive_arm_mult": _s(self.adaptive_arm_mult, 4),
            "adaptive_be_mult": _s(self.adaptive_be_mult, 4),
            "adaptive_trail_mult": _s(self.adaptive_trail_mult, 4),
            "noise_score": _s(self.noise_score, 4),
            "trend_efficiency": _s(self.trend_efficiency, 4),
            "atr_regime": _s(self.atr_regime, 4),
            "unrealized_pnl": _s(self.unrealized_pnl, 2),
            "unrealized_pnl_pct": _s(self.unrealized_pnl_pct, 4),
            "r_multiple": _s(self.r_multiple, 4),
            "events": self.events[-100:],
            "fills": self.fills,
            "stop_history": self.stop_history[-500:],
        }

    def from_dict_restore(self, d: dict):
        """딕셔너리에서 거래 상태 복원"""
        self.active = d.get("active", False)
        self.direction = d.get("direction", "long")
        self.entry_price = d.get("entry_price", 0.0)
        self.entry_time = d.get("entry_time", "")
        self.entry_bar_idx = d.get("entry_bar_idx", 0)
        self.avg_entry = d.get("avg_entry", 0.0)
        self.total_qty = d.get("total_qty", 0.0)
        self.initial_qty = d.get("initial_qty", 0.0)
        self.initial_stop = d.get("initial_stop", 0.0)
        self.trailing_stop = d.get("trailing_stop", 0.0)
        self.active_stop = d.get("active_stop", 0.0)
        self.breakeven_stop = d.get("breakeven_stop", 0.0)
        self.trail_armed = d.get("trail_armed", False)
        self.trail_arm_reason = d.get("trail_arm_reason", "")
        self.breakeven_active = d.get("breakeven_active", False)
        self.highest = d.get("highest", 0.0)
        self.lowest = d.get("lowest", 999999999.0)
        self.mfe = d.get("mfe", 0.0)
        self.mae = d.get("mae", 0.0)
        self.bars_in_trade = d.get("bars_in_trade", 0)
        self.pyramid_count = d.get("pyramid_count", 0)
        self.avg_down_count = d.get("avg_down_count", 0)
        self.auto_add_count = d.get("auto_add_count", 0)
        self.initial_risk_per_unit = d.get("initial_risk_per_unit", 0.0)
        self.initial_risk_total = d.get("initial_risk_total", 0.0)
        self.current_risk = d.get("current_risk", 0.0)
        self.max_risk = d.get("max_risk", 0.0)
        self.risk_budget = d.get("risk_budget", 0.0)
        self.entry_atr = d.get("entry_atr", 0.0)
        self.risk_basis = d.get("risk_basis", 0.0)
        self.trend_basis = d.get("trend_basis", 0.0)
        self.pyramid_basis = d.get("pyramid_basis", 0.0)
        self.adaptive_arm_mult = d.get("adaptive_arm_mult", 1.0)
        self.adaptive_be_mult = d.get("adaptive_be_mult", 1.0)
        self.adaptive_trail_mult = d.get("adaptive_trail_mult", 1.0)
        self.noise_score = d.get("noise_score", 0.0)
        self.trend_efficiency = d.get("trend_efficiency", 0.0)
        self.atr_regime = d.get("atr_regime", 1.0)
        self.unrealized_pnl = d.get("unrealized_pnl", 0.0)
        self.unrealized_pnl_pct = d.get("unrealized_pnl_pct", 0.0)
        self.r_multiple = d.get("r_multiple", 0.0)
        self.events = d.get("events", [])
        self.fills = d.get("fills", [])
        self.stop_history = d.get("stop_history", [])


# ─── 거래 요약 ──────────────────────────────────────────────

class TradeSummary:
    def __init__(self):
        self.direction = "long"
        self.entry_price = 0.0
        self.avg_entry = 0.0
        self.exit_price = 0.0
        self.exit_reason = ""
        self.qty = 0.0
        self.pnl = 0.0
        self.pnl_pct = 0.0
        self.bars_held = 0
        self.scale_ins = 0
        self.initial_risk = 0.0
        self.max_risk = 0.0
        self.r_initial = 0.0
        self.r_max = 0.0
        self.mfe = 0.0
        self.mae = 0.0
        self.entry_atr = 0.0
        self.entry_time = ""
        self.exit_time = ""
        self.early_mfe = 0.0
        self.early_mae = 0.0

    def to_dict(self):
        return {
            "direction": self.direction,
            "entry_price": round(self.entry_price, 6),
            "avg_entry": round(self.avg_entry, 6),
            "exit_price": round(self.exit_price, 6),
            "exit_reason": self.exit_reason,
            "qty": round(self.qty, 6),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 4),
            "bars_held": self.bars_held,
            "scale_ins": self.scale_ins,
            "initial_risk": round(self.initial_risk, 2),
            "max_risk": round(self.max_risk, 2),
            "r_initial": round(self.r_initial, 4),
            "r_max": round(self.r_max, 4),
            "mfe": round(self.mfe, 4),
            "mae": round(self.mae, 4),
            "entry_atr": round(self.entry_atr, 6),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
        }


# ─── 메인 엔진 ──────────────────────────────────────────────

class TradeEngine:
    def __init__(self, config: dict = None):
        self.config = deepcopy(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        # 포트폴리오: 종목 코드별로 여러 포지션 관리
        self.states: Dict[str, TradeState] = {}  # symbol -> TradeState
        self.current_symbol: str = ""  # 현재 작업 중인 종목
        # 하위호환성을 위해 state 프로퍼티도 유지
        self._legacy_state = TradeState()
        
        self.bars: List[Bar] = []
        self.atr_values: List[float] = []
        self.current_atr = 0.0
        self.trade_history: List[dict] = []
        self.bar_index = 0
        self.entry_pending = False
        self.manual_touch_occurred = False

    @property
    def state(self) -> TradeState:
        """현재 활성 포지션 또는 마지막 작업 포지션 반환 (하위호환성)"""
        if self.current_symbol and self.current_symbol in self.states:
            return self.states[self.current_symbol]
        # 활성 포지션이 하나만 있으면 그것 반환
        active = [s for s in self.states.values() if s.active]
        if len(active) == 1:
            return active[0]
        return self._legacy_state
    
    @state.setter
    def state(self, value: TradeState):
        """하위호환성을 위한 setter (deprecated)"""
        if self.current_symbol:
            self.states[self.current_symbol] = value
        else:
            self._legacy_state = value

    def _get_or_create_state(self, symbol: str) -> TradeState:
        """종목의 거래 상태를 가져오거나 새로 생성"""
        if symbol not in self.states:
            self.states[symbol] = TradeState()
        self.current_symbol = symbol
        return self.states[symbol]

    # ── ATR 계산 ──────────────────────────────────────────

    def _calc_true_range(self, idx: int) -> float:
        bar = self.bars[idx]
        hl = bar.high - bar.low
        if idx == 0:
            return hl
        prev_close = self.bars[idx - 1].close
        return max(hl, abs(bar.high - prev_close), abs(bar.low - prev_close))

    def _update_atr(self):
        length = self.config["atr_length"]
        idx = len(self.bars) - 1
        tr = self._calc_true_range(idx)

        if len(self.atr_values) == 0:
            self.atr_values.append(tr)
        elif len(self.atr_values) < length:
            self.atr_values.append(tr)
        else:
            prev_atr = self.atr_values[-1]
            new_atr = (prev_atr * (length - 1) + tr) / length
            self.atr_values.append(new_atr)

        self.current_atr = self.atr_values[-1] if self.atr_values else tr

    # ── 적응형 관리 ───────────────────────────────────────

    def _calc_wick_noise(self) -> float:
        lookback = self.config["adaptive_lookback"]
        start = max(0, len(self.bars) - lookback)
        if start >= len(self.bars):
            return 0.5
        ratios = []
        for i in range(start, len(self.bars)):
            b = self.bars[i]
            body = abs(b.close - b.open)
            upper = b.high - max(b.open, b.close)
            lower = min(b.open, b.close) - b.low
            total = body + upper + lower
            if total > 0:
                ratios.append((upper + lower) / total)
            else:
                ratios.append(0.5)
        return sum(ratios) / len(ratios) if ratios else 0.5

    def _calc_trend_efficiency(self) -> float:
        lookback = self.config["adaptive_lookback"]
        if len(self.bars) < 2:
            return 0.5
        start = max(0, len(self.bars) - lookback)
        net = abs(self.bars[-1].close - self.bars[start].close)
        total = 0.0
        for i in range(start + 1, len(self.bars)):
            total += abs(self.bars[i].close - self.bars[i - 1].close)
        if total == 0:
            return 0.5
        return clamp(net / total, 0.0, 1.0)

    def _calc_atr_regime(self) -> float:
        lookback = self.config["atr_regime_lookback"]
        if len(self.atr_values) < 2:
            return 1.0
        start = max(0, len(self.atr_values) - lookback)
        avg = sum(self.atr_values[start:]) / len(self.atr_values[start:])
        if avg == 0:
            return 1.0
        return self.current_atr / avg

    def _update_adaptive(self):
        cfg = self.config
        if not (cfg["enable_adaptive_arm_be"] or cfg["enable_adaptive_trailing"]):
            return

        noise = self._calc_wick_noise()
        efficiency = self._calc_trend_efficiency()
        regime = self._calc_atr_regime()

        self.state.noise_score = noise
        self.state.trend_efficiency = efficiency
        self.state.atr_regime = regime

        # 노이즈 점수: 높을수록 시장이 시끄러움 (0~1)
        # 효율 점수: 높을수록 추세가 깨끗함 (0~1)
        # 조합: 노이즈 높고 효율 낮으면 → 보수적, 반대면 → 공격적
        difficulty = clamp(noise * 0.5 + (1.0 - efficiency) * 0.3 + (regime - 1.0) * 0.2, 0.0, 1.0)

        # 최근 거래 기억 반영
        memory_adj = 0.0
        if cfg["use_trade_memory"] and self.trade_history:
            recent = self.trade_history[-cfg["adaptive_history_trades"]:]
            early_mfes = [t.get("mfe", 0) for t in recent]
            early_maes = [t.get("mae", 0) for t in recent]
            avg_mfe = sum(early_mfes) / len(early_mfes) if early_mfes else 0
            avg_mae = sum(early_maes) / len(early_maes) if early_maes else 0
            if avg_mfe + avg_mae > 0:
                memory_adj = (avg_mae - avg_mfe) / (avg_mfe + avg_mae) * cfg["adaptive_history_weight"]

        total_difficulty = clamp(difficulty + memory_adj, 0.0, 1.0)

        if cfg["enable_adaptive_arm_be"]:
            strength = cfg["adaptive_arm_be_strength"]
            # 어려운 장 → arm/BE 문턱을 높임 (더 보수적)
            arm_mult = 1.0 + total_difficulty * strength * 0.5
            be_mult = 1.0 + total_difficulty * strength * 0.3
            self.state.adaptive_arm_mult = clamp(arm_mult, 0.5, 2.0)
            self.state.adaptive_be_mult = clamp(be_mult, 0.5, 2.0)

        if cfg["enable_adaptive_trailing"]:
            strength = cfg["adaptive_trailing_strength"]
            # 어려운 장 → trailing 폭을 넓힘
            trail_mult = 1.0 + total_difficulty * strength * 0.4
            self.state.adaptive_trail_mult = clamp(trail_mult, 0.5, 2.0)

    # ── 포지션 사이즈 계산 ────────────────────────────────

    def _calc_position_size(self, entry_price: float) -> float:
        cfg = self.config
        if cfg["sizing_mode"] == "manual":
            return cfg["initial_qty"]

        # Risk-Based
        capital = cfg["total_capital"]
        risk_pct = cfg["risk_per_trade_pct"] / 100.0
        max_loss = capital * risk_pct

        stop_dist = self.current_atr * cfg["initial_stop_atr_mult"]
        if stop_dist <= 0:
            return cfg["initial_qty"]

        raw_qty = max_loss / stop_dist
        qty = round_qty(raw_qty, cfg["qty_preset"], cfg["custom_rounding"],
                        cfg["custom_qty_step"])
        return max(qty, 0)

    # ── 초기 손절 ─────────────────────────────────────────

    def _calc_initial_stop(self, entry_price: float, atr: float) -> float:
        mult = self.config["initial_stop_atr_mult"]
        if self.state.direction == "long":
            return entry_price - atr * mult
        else:
            return entry_price + atr * mult

    # ── Break-even 계산 ───────────────────────────────────

    def _calc_breakeven_level(self) -> float:
        cfg = self.config
        if cfg["breakeven_basis"] == "trend_basis":
            basis = self.state.trend_basis
        else:
            basis = self.state.risk_basis

        offset = self.current_atr * cfg["breakeven_offset_atr"]
        if self.state.direction == "long":
            return basis + offset
        else:
            return basis - offset

    # ── Trailing Stop 계산 ────────────────────────────────

    def _calc_trailing_stop(self) -> float:
        cfg = self.config
        s = self.state
        base_mult = cfg["trailing_atr_mult"]

        # Adaptive 조정
        effective_mult = base_mult * s.adaptive_trail_mult
        effective_mult = clamp(effective_mult,
                               cfg["adaptive_trailing_min_atr"],
                               cfg["adaptive_trailing_max_atr"])

        # ATR cap/floor
        trail_atr = self.current_atr
        if s.entry_atr > 0:
            cap = s.entry_atr * cfg["trailing_atr_expansion_cap"]
            floor = s.entry_atr * cfg["trailing_atr_contraction_floor"]
            trail_atr = clamp(trail_atr, floor, cap)

        dist = trail_atr * effective_mult
        if s.direction == "long":
            return s.highest - dist
        else:
            return s.lowest + dist

    # ── Trail Arm 조건 확인 ───────────────────────────────

    def _check_trail_arm(self, bar: Bar) -> bool:
        cfg = self.config
        s = self.state
        if s.trail_armed:
            return True
        if s.entry_atr <= 0:
            return False

        base_trigger = cfg["trail_arm_trigger_atr"]
        effective_trigger = base_trigger * s.adaptive_arm_mult
        effective_trigger = clamp(effective_trigger,
                                  cfg["adaptive_arm_min_atr"],
                                  cfg["adaptive_arm_max_atr"])

        # 1) MFE 기준
        if s.mfe >= effective_trigger:
            s.trail_armed = True
            s.trail_arm_bar = self.bar_index
            s.trail_arm_reason = f"MFE ({s.mfe:.2f} ATR ≥ {effective_trigger:.2f})"
            self._add_event("trail_armed", s.trail_arm_reason)
            return True

        # 2) 시간 기준
        min_bars = cfg["trail_arm_after_bars"]
        min_progress = cfg["time_arm_min_progress_atr"]
        if min_bars > 0 and s.bars_in_trade >= min_bars:
            if s.mfe >= min_progress:
                s.trail_armed = True
                s.trail_arm_bar = self.bar_index
                s.trail_arm_reason = f"시간 ({s.bars_in_trade}봉, MFE {s.mfe:.2f})"
                self._add_event("trail_armed", s.trail_arm_reason)
                return True

        # 3) 새 극값 기준
        if cfg["trail_arm_on_new_extreme"]:
            if s.direction == "long" and bar.high > s.highest:
                if s.mfe >= min_progress * 0.5:
                    s.trail_armed = True
                    s.trail_arm_bar = self.bar_index
                    s.trail_arm_reason = "새 고점"
                    self._add_event("trail_armed", s.trail_arm_reason)
                    return True
            elif s.direction == "short" and bar.low < s.lowest:
                if s.mfe >= min_progress * 0.5:
                    s.trail_armed = True
                    s.trail_arm_bar = self.bar_index
                    s.trail_arm_reason = "새 저점"
                    self._add_event("trail_armed", s.trail_arm_reason)
                    return True

        return False

    # ── Break-even 조건 확인 ──────────────────────────────

    def _check_breakeven(self):
        cfg = self.config
        s = self.state
        if not cfg["use_breakeven_ratchet"] or s.breakeven_active:
            return
        if s.entry_atr <= 0:
            return

        base_trigger = cfg["breakeven_trigger_atr"]
        effective_trigger = base_trigger * s.adaptive_be_mult
        effective_trigger = clamp(effective_trigger,
                                  cfg["adaptive_be_min_atr"],
                                  cfg["adaptive_be_max_atr"])

        if s.mfe >= effective_trigger:
            s.breakeven_active = True
            s.breakeven_bar = self.bar_index
            s.breakeven_stop = self._calc_breakeven_level()
            self._add_event("breakeven", f"본전보호 활성화 (MFE {s.mfe:.2f} ATR)")

    # ── 손절 판정 ─────────────────────────────────────────

    def _check_stop_hit(self, bar: Bar) -> Optional[float]:
        s = self.state
        stop = s.active_stop
        if stop <= 0:
            return None

        mode = self.config["stop_hit_mode"]
        if s.direction == "long":
            if mode == "wick":
                if bar.low <= stop:
                    return stop
            else:  # close
                if bar.close <= stop:
                    return bar.close
        else:
            if mode == "wick":
                if bar.high >= stop:
                    return stop
            else:
                if bar.close >= stop:
                    return bar.close
        return None

    # ── 추가진입 관련 ─────────────────────────────────────

    def _check_scale_in_gates(self, add_type: str, price: float, qty: float) -> tuple:
        """추가진입 허용 여부를 검사. (allowed, reason) 반환"""
        cfg = self.config
        s = self.state

        if not s.active:
            return False, "거래 비활성"

        # 방향 검사
        is_favorable = (s.direction == "long" and price > s.avg_entry) or \
                       (s.direction == "short" and price < s.avg_entry)

        if add_type == "pyramid" and not is_favorable:
            return False, "피라미딩은 유리한 방향만 가능"
        if add_type == "avg_down" and is_favorable:
            return False, "물타기는 불리한 방향만 가능"

        # 간격 검사
        if s.last_add_price > 0 and self.current_atr > 0:
            spacing = abs(price - s.last_add_price) / self.current_atr
            if spacing < cfg["min_add_spacing_atr"]:
                return False, f"간격 부족 ({spacing:.2f} < {cfg['min_add_spacing_atr']} ATR)"

        # 횟수 검사
        if add_type == "pyramid" and s.pyramid_count >= cfg["max_pyramid_adds"]:
            return False, f"피라미딩 횟수 초과 ({s.pyramid_count}/{cfg['max_pyramid_adds']})"
        if add_type == "avg_down" and s.avg_down_count >= cfg["max_avg_down_adds"]:
            return False, f"물타기 횟수 초과 ({s.avg_down_count}/{cfg['max_avg_down_adds']})"

        # 물타기 시점 검사
        if add_type == "avg_down":
            if cfg["avg_down_allowed_until"] == "before_trail_armed" and s.trail_armed:
                return False, "Trail Armed 이후 물타기 불가"
            if cfg["avg_down_allowed_until"] == "before_breakeven" and s.breakeven_active:
                return False, "Break-even 이후 물타기 불가"

        # 품질 검사 (피라미딩만)
        if add_type == "pyramid" and cfg["enable_quality_gate"]:
            if s.mfe < cfg["quality_min_trend_mfe_atr"]:
                return False, f"추세 MFE 부족 ({s.mfe:.2f} < {cfg['quality_min_trend_mfe_atr']})"
            if s.trend_efficiency < cfg["quality_min_efficiency"]:
                return False, f"효율 부족 ({s.trend_efficiency:.2f} < {cfg['quality_min_efficiency']})"
            if s.risk_budget > 0:
                headroom = max(0, s.risk_budget - s.current_risk) / s.risk_budget * 100
                if headroom < cfg["quality_min_headroom_pct"]:
                    return False, f"리스크 여유 부족 ({headroom:.1f}% < {cfg['quality_min_headroom_pct']}%)"

        # 리스크 예산 검사
        if cfg["cap_to_risk_budget"] and s.risk_budget > 0:
            new_risk = qty * abs(price - s.active_stop)
            if s.current_risk + new_risk > s.risk_budget:
                return False, "리스크 예산 초과"

        return True, "통과"

    def _handle_scale_in_stop(self, add_type: str, add_price: float, add_qty: float):
        """추가진입 후 손절선 처리"""
        cfg = self.config
        s = self.state

        # 처리 방식 선택
        if add_type == "pyramid":
            handling = cfg["pyramid_stop_handling"]
        elif add_type == "avg_down":
            handling = cfg["avg_down_stop_handling"]
        else:
            handling = cfg["scale_in_stop_handling"]

        if handling == "keep":
            return

        old_stop = s.active_stop
        new_avg = s.avg_entry  # 이미 업데이트됨

        if handling == "rebase":
            new_stop = self._calc_initial_stop(new_avg, self.current_atr)
            s.initial_stop = new_stop

        elif handling == "rebase_no_wider":
            new_stop = self._calc_initial_stop(new_avg, self.current_atr)
            if s.direction == "long":
                new_stop = max(new_stop, old_stop)
            else:
                new_stop = min(new_stop, old_stop)
            s.initial_stop = new_stop

        elif handling == "soft_rebase":
            new_stop = self._calc_initial_stop(new_avg, self.current_atr)
            w = cfg["soft_rebase_weight"]
            blended = new_stop * w + old_stop * (1 - w)
            s.initial_stop = blended

        # Trend basis 재설정
        if cfg["trend_basis_reset"] == "reset_on_rebase" and handling in ("rebase", "rebase_no_wider", "soft_rebase"):
            s.trend_basis = new_avg

    def _execute_scale_in(self, price: float, qty: float, add_type: str, reason: str = ""):
        s = self.state

        # 평균단가 업데이트
        old_total = s.avg_entry * s.total_qty
        s.total_qty += qty
        s.avg_entry = (old_total + price * qty) / s.total_qty if s.total_qty > 0 else price

        # Basis 업데이트
        s.risk_basis = s.avg_entry
        s.pyramid_basis = price

        # 카운트 업데이트
        if add_type == "pyramid":
            s.pyramid_count += 1
        else:
            s.avg_down_count += 1

        s.last_add_bar = self.bar_index
        s.last_add_price = price

        # 리스크 재계산
        stop_dist = abs(s.avg_entry - s.active_stop) if s.active_stop > 0 else abs(s.avg_entry - s.initial_stop)
        s.current_risk = s.total_qty * stop_dist
        s.max_risk = max(s.max_risk, s.current_risk)

        fill_record = {
            "bar": self.bar_index,
            "price": round(price, 6),
            "qty": round(qty, 6),
            "type": add_type,
            "reason": reason,
            "avg_after": round(s.avg_entry, 6),
            "total_qty_after": round(s.total_qty, 6),
        }
        s.fills.append(fill_record)

        # 손절선 처리
        self._handle_scale_in_stop(add_type, price, qty)

        type_str = "피라미딩" if add_type == "pyramid" else "물타기"
        self._add_event("scale_in",
                        f"{type_str} 체결: {qty}주 @ {price:.2f} (평균 {s.avg_entry:.2f}) {reason}")

    def _check_manual_scale_ins(self, bar: Bar):
        cfg = self.config
        s = self.state
        if not cfg["enable_manual_scale_ins"]:
            return

        for add_cfg in cfg.get("manual_adds", []):
            if not add_cfg or add_cfg.get("executed"):
                continue

            target_bar = add_cfg.get("bar", 0)
            target_price = add_cfg.get("price", 0)
            qty = add_cfg.get("qty", 0)
            add_type = add_cfg.get("type", "pyramid")

            if qty <= 0 or target_price <= 0:
                continue

            # 시점/가격 조건
            triggered = False
            fill_price = target_price

            if cfg["manual_add_fill_mode"] == "first_touch":
                if self.bar_index >= target_bar:
                    if s.direction == "long":
                        if add_type == "pyramid" and bar.high >= target_price:
                            triggered = True
                        elif add_type == "avg_down" and bar.low <= target_price:
                            triggered = True
                    else:
                        if add_type == "pyramid" and bar.low <= target_price:
                            triggered = True
                        elif add_type == "avg_down" and bar.high >= target_price:
                            triggered = True
            else:  # scheduled_close
                if self.bar_index == target_bar:
                    fill_price = bar.close
                    triggered = True

            if not triggered:
                continue

            # 게이트 검사
            stop_handling = cfg.get(f"{add_type}_stop_handling", cfg["scale_in_stop_handling"])
            if stop_handling == "block":
                self._add_event("blocked", f"{add_type} 추가진입 차단됨 (설정: block)")
                add_cfg["executed"] = True
                continue

            allowed, reason = self._check_scale_in_gates(add_type, fill_price, qty)
            if not allowed:
                self._add_event("blocked", f"추가진입 차단: {reason}")
                add_cfg["executed"] = True
                continue

            self._execute_scale_in(fill_price, qty, add_type, "수동")
            add_cfg["executed"] = True

    def _check_conditional_pyramid(self, bar: Bar):
        cfg = self.config
        s = self.state
        if not cfg["enable_conditional_pyramid"]:
            return
        if s.auto_add_count >= cfg["max_auto_adds"]:
            return
        if self.bar_index - s.last_add_bar < cfg["auto_add_cooldown_bars"]:
            return

        trigger = cfg["auto_add_trigger"]
        triggered = False

        if trigger == "trail_armed" and s.trail_armed and \
                self.bar_index == s.trail_arm_bar:
            triggered = True
        elif trigger == "breakeven_active" and s.breakeven_active and \
                self.bar_index == s.breakeven_bar:
            triggered = True
        elif trigger == "new_extreme":
            if s.trail_armed:
                if s.direction == "long" and bar.high >= s.highest:
                    triggered = True
                elif s.direction == "short" and bar.low <= s.lowest:
                    triggered = True
        elif trigger == "mfe_step":
            threshold = cfg["auto_add_base_mfe_atr"] + \
                        s.auto_add_count * cfg["auto_add_step_mfe_atr"]
            if s.mfe >= threshold:
                triggered = True

        if not triggered:
            return

        # 수량 계산
        qty_mode = cfg["auto_add_qty_mode"]
        if qty_mode == "fixed":
            qty = cfg["auto_add_fixed_qty"]
        elif qty_mode == "pct_initial":
            qty = s.initial_qty * cfg["auto_add_qty_pct"] / 100.0
        else:  # risk_remaining
            remaining = max(0, s.risk_budget - s.current_risk)
            stop_dist = abs(bar.close - s.active_stop) if s.active_stop > 0 else self.current_atr * cfg["initial_stop_atr_mult"]
            if stop_dist > 0:
                qty = remaining / stop_dist
            else:
                qty = 0

        qty = round_qty(qty, cfg["qty_preset"], cfg["custom_rounding"], cfg["custom_qty_step"])
        if qty <= 0:
            return

        # 피라미딩 stop handling 확인
        if cfg["pyramid_stop_handling"] == "block":
            self._add_event("blocked", "자동 피라미딩 차단됨 (설정: block)")
            return

        allowed, reason = self._check_scale_in_gates("pyramid", bar.close, qty)
        if not allowed:
            self._add_event("blocked", f"자동 피라미딩 차단: {reason}")
            return

        s.auto_add_count += 1
        self._execute_scale_in(bar.close, qty, "pyramid", "자동")

    # ── 이벤트 기록 ───────────────────────────────────────

    def _add_event(self, event_type: str, message: str):
        self.state.events.append({
            "bar": self.bar_index,
            "type": event_type,
            "message": message,
            "timestamp": self.bars[-1].timestamp if self.bars else "",
        })

    # ── 거래 종료 ─────────────────────────────────────────

    def _close_trade(self, exit_price: float, reason: str, symbol: str = "default") -> dict:
        s = self.states.get(symbol, self.state)
        summary = TradeSummary()
        summary.direction = s.direction
        summary.entry_price = s.entry_price
        summary.avg_entry = s.avg_entry
        summary.exit_price = exit_price
        summary.exit_reason = reason
        summary.qty = s.total_qty
        summary.bars_held = s.bars_in_trade
        summary.scale_ins = s.pyramid_count + s.avg_down_count
        summary.mfe = s.mfe
        summary.mae = s.mae
        summary.entry_atr = s.entry_atr
        summary.entry_time = s.entry_time
        summary.exit_time = self.bars[-1].timestamp if self.bars else ""

        if s.direction == "long":
            summary.pnl = (exit_price - s.avg_entry) * s.total_qty
        else:
            summary.pnl = (s.avg_entry - exit_price) * s.total_qty

        if s.avg_entry > 0:
            summary.pnl_pct = summary.pnl / (s.avg_entry * s.total_qty) * 100

        summary.initial_risk = s.initial_risk_total
        summary.max_risk = s.max_risk
        if s.initial_risk_total != 0:
            summary.r_initial = summary.pnl / abs(s.initial_risk_total)
        if s.max_risk != 0:
            summary.r_max = summary.pnl / abs(s.max_risk)

        self._add_event("exit",
                        f"청산: {exit_price:.2f} | 손익: {summary.pnl:+.2f} "
                        f"({summary.pnl_pct:+.2f}%) | R(초기): {summary.r_initial:+.2f} "
                        f"| {reason}")

        result = summary.to_dict()
        result["symbol"] = symbol  # 거래 기록에 종목 코드 추가
        self.trade_history.append(result)
        s.reset()
        return result

    # ── 메인 업데이트 루프 ────────────────────────────────

    def process_bar(self, bar: Bar) -> dict:
        """새 봉 처리. 결과 상태 반환."""
        self.bars.append(bar)
        self.bar_index = len(self.bars) - 1
        self._update_atr()

        result = {
            "bar_index": self.bar_index,
            "atr": round(self.current_atr, 6),
            "trade_active": False,
            "exit": None,
        }

        s = self.state

        # ── 진입 전 ──
        if not s.active:
            entry_price = self._check_entry(bar)
            if entry_price is not None and entry_price > 0:
                self._execute_entry(entry_price, bar)

        # ── 거래 중 ──
        if s.active:
            result["trade_active"] = True

            # 가격 추적 업데이트
            s.highest = max(s.highest, bar.high)
            s.lowest = min(s.lowest, bar.low)
            s.bars_in_trade += 1

            # MFE/MAE 업데이트
            if s.entry_atr > 0:
                if s.direction == "long":
                    s.mfe = max(s.mfe, (s.highest - s.trend_basis) / s.entry_atr)
                    s.mae = max(s.mae, (s.trend_basis - s.lowest) / s.entry_atr)
                else:
                    s.mfe = max(s.mfe, (s.trend_basis - s.lowest) / s.entry_atr)
                    s.mae = max(s.mae, (s.highest - s.trend_basis) / s.entry_atr)

            # 적응형 업데이트
            self._update_adaptive()

            # Trail Arm 확인
            self._check_trail_arm(bar)

            # Trailing Stop 업데이트
            if s.trail_armed:
                new_trail = self._calc_trailing_stop()
                if s.direction == "long":
                    if new_trail > s.trailing_stop:
                        s.trailing_stop = new_trail
                else:
                    if s.trailing_stop == 0 or new_trail < s.trailing_stop:
                        s.trailing_stop = new_trail

            # Break-even 확인
            self._check_breakeven()

            # Active stop 결정 (롱: 가장 높은 것, 숏: 가장 낮은 것)
            candidates = [s.initial_stop]
            if s.trailing_stop > 0:
                candidates.append(s.trailing_stop)
            if s.breakeven_active and s.breakeven_stop > 0:
                candidates.append(s.breakeven_stop)

            if s.direction == "long":
                s.active_stop = max(candidates)
            else:
                s.active_stop = min(c for c in candidates if c > 0) if any(c > 0 for c in candidates) else s.initial_stop

            # 리스크 재계산
            stop_dist = abs(s.avg_entry - s.active_stop)
            s.current_risk = s.total_qty * stop_dist
            s.max_risk = max(s.max_risk, s.current_risk)

            # Stop history 기록
            s.stop_history.append({
                "bar": self.bar_index,
                "initial": round(s.initial_stop, 6),
                "trailing": round(s.trailing_stop, 6),
                "breakeven": round(s.breakeven_stop, 6),
                "active": round(s.active_stop, 6),
            })

            # 추가진입 체크
            self._check_manual_scale_ins(bar)
            self._check_conditional_pyramid(bar)

            # 손절 판정
            # Intrabar conflict: conservative 모드에서는 진입 봉이면 손절 스킵
            skip_stop = (self.config["intrabar_conflict"] == "conservative" and
                         self.bar_index == s.entry_bar_idx)

            if not skip_stop:
                exit_price = self._check_stop_hit(bar)
                if exit_price is not None:
                    result["exit"] = self._close_trade(exit_price, "손절")
                    result["trade_active"] = False

            # P&L 업데이트
            if s.active:
                if s.direction == "long":
                    s.unrealized_pnl = (bar.close - s.avg_entry) * s.total_qty
                else:
                    s.unrealized_pnl = (s.avg_entry - bar.close) * s.total_qty

                if s.avg_entry > 0 and s.total_qty > 0:
                    s.unrealized_pnl_pct = s.unrealized_pnl / (s.avg_entry * s.total_qty) * 100

                if s.initial_risk_total != 0:
                    s.r_multiple = s.unrealized_pnl / abs(s.initial_risk_total)

        result["state"] = s.to_dict()
        return result

    # ── 진입 체크 ─────────────────────────────────────────

    def _check_entry(self, bar: Bar) -> Optional[float]:
        cfg = self.config
        if cfg["entry_source"] == "bar_close":
            if self.bar_index == cfg["trade_start_bar"]:
                return bar.close
        else:  # manual
            price = cfg["manual_entry_price"]
            if price <= 0:
                return None

            if cfg["manual_uses_start_time"] and self.bar_index < cfg.get("trade_start_bar", 0):
                return None

            if cfg["manual_activation"] == "immediate":
                return price
            else:  # first_touch
                if cfg["position_direction"] == "long":
                    if bar.low <= price <= bar.high:
                        return price
                else:
                    if bar.low <= price <= bar.high:
                        return price
        return None

    # ── 진입 실행 ─────────────────────────────────────────

    def _execute_entry(self, entry_price: float, bar: Bar):
        s = self.state
        cfg = self.config

        s.active = True
        s.direction = cfg["position_direction"]
        s.entry_price = entry_price
        s.entry_time = bar.timestamp
        s.entry_bar_idx = self.bar_index
        s.entry_atr = self.current_atr

        # 수량 계산
        qty = self._calc_position_size(entry_price)
        s.initial_qty = qty
        s.total_qty = qty

        # 단가 설정
        s.avg_entry = entry_price
        s.risk_basis = entry_price
        s.trend_basis = entry_price
        s.pyramid_basis = entry_price

        # 초기 손절
        s.initial_stop = self._calc_initial_stop(entry_price, self.current_atr)
        s.active_stop = s.initial_stop

        # 가격 추적 초기화
        s.highest = bar.high
        s.lowest = bar.low

        # 리스크 계산
        s.initial_risk_per_unit = abs(entry_price - s.initial_stop)
        s.initial_risk_total = s.initial_risk_per_unit * qty
        s.current_risk = s.initial_risk_total
        s.max_risk = s.initial_risk_total

        # 리스크 예산
        if cfg["sizing_mode"] == "risk_based":
            s.risk_budget = cfg["total_capital"] * cfg["risk_per_trade_pct"] / 100.0
        else:
            s.risk_budget = s.initial_risk_total * 2  # 수동 시 초기의 2배

        # 초기 fill 기록
        s.fills.append({
            "bar": self.bar_index,
            "price": round(entry_price, 6),
            "qty": round(qty, 6),
            "type": "entry",
            "reason": "최초진입",
            "avg_after": round(entry_price, 6),
            "total_qty_after": round(qty, 6),
        })

        s.stop_history.append({
            "bar": self.bar_index,
            "initial": round(s.initial_stop, 6),
            "trailing": 0,
            "breakeven": 0,
            "active": round(s.active_stop, 6),
        })

        dir_str = "롱" if s.direction == "long" else "숏"
        self._add_event("entry",
                        f"{dir_str} 진입: {qty}주 @ {entry_price:.2f} | "
                        f"초기손절: {s.initial_stop:.2f} | ATR: {self.current_atr:.4f}")

    # ── 직접 진입 (UI에서 호출) ───────────────────────────

    def manual_entry(self, price: float, qty: float = 0, entry_date: str = "", symbol: str = ""):
        """UI에서 직접 진입 실행. entry_date가 주어지면 해당 날짜 봉을 기준으로 진입."""
        s = self._get_or_create_state(symbol or self.current_symbol or "default")
        
        if s.active:
            return {"error": "이미 활성 거래가 있습니다"}
        if not self.bars:
            return {"error": "가격 데이터가 없습니다"}

        cfg = self.config

        # 진입 날짜에 해당하는 봉 찾기
        bar = self.bars[-1]
        bar_idx = self.bar_index
        entry_atr = self.current_atr

        if entry_date:
            found = False
            for i, b in enumerate(self.bars):
                # timestamp 형식: "YYYY-MM-DD HH:MM" 또는 "YYYY-MM-DD"
                if b.timestamp.startswith(entry_date):
                    bar = b
                    bar_idx = i
                    # 해당 봉 시점의 ATR 사용 (가능하면)
                    if i < len(self.atr_values) and self.atr_values[i] > 0:
                        entry_atr = self.atr_values[i]
                    found = True
                    break
            if not found:
                return {"error": f"'{entry_date}' 날짜의 봉 데이터를 찾을 수 없습니다"}

        s.active = True
        s.direction = cfg["position_direction"]
        s.entry_price = price
        s.entry_time = bar.timestamp
        s.entry_bar_idx = bar_idx
        s.entry_atr = entry_atr

        if qty <= 0:
            qty = self._calc_position_size(price)
        s.initial_qty = qty
        s.total_qty = qty

        s.avg_entry = price
        s.risk_basis = price
        s.trend_basis = price
        s.pyramid_basis = price

        s.initial_stop = self._calc_initial_stop(price, entry_atr)
        s.active_stop = s.initial_stop

        s.highest = max(bar.high, price)
        s.lowest = min(bar.low, price)

        s.initial_risk_per_unit = abs(price - s.initial_stop)
        s.initial_risk_total = s.initial_risk_per_unit * qty
        s.current_risk = s.initial_risk_total
        s.max_risk = s.initial_risk_total

        if cfg["sizing_mode"] == "risk_based":
            s.risk_budget = cfg["total_capital"] * cfg["risk_per_trade_pct"] / 100.0
        else:
            s.risk_budget = s.initial_risk_total * 2

        s.fills.append({
            "bar": bar_idx,
            "price": round(price, 6),
            "qty": round(qty, 6),
            "type": "entry",
            "reason": "수동진입",
            "avg_after": round(price, 6),
            "total_qty_after": round(qty, 6),
        })

        s.stop_history.append({
            "bar": bar_idx,
            "initial": round(s.initial_stop, 6),
            "trailing": 0,
            "breakeven": 0,
            "active": round(s.active_stop, 6),
        })

        dir_str = "롱" if s.direction == "long" else "숏"
        self._add_event("entry",
                        f"{dir_str} 수동진입: {qty}주 @ {price:.2f} | "
                        f"날짜: {bar.timestamp} | 초기손절: {s.initial_stop:.2f} | ATR: {entry_atr:.4f}")

        # 진입 즉시 P&L 계산 (최신 봉 종가 기준)
        if self.bars:
            last_close = self.bars[-1].close
            if s.direction == "long":
                s.unrealized_pnl = (last_close - s.avg_entry) * s.total_qty
            else:
                s.unrealized_pnl = (s.avg_entry - last_close) * s.total_qty
            if s.avg_entry > 0 and s.total_qty > 0:
                s.unrealized_pnl_pct = s.unrealized_pnl / (s.avg_entry * s.total_qty) * 100
            if s.initial_risk_total != 0:
                s.r_multiple = s.unrealized_pnl / abs(s.initial_risk_total)

        return {"success": True, "state": s.to_dict()}

    def manual_add(self, price: float, qty: float, add_type: str = "pyramid", symbol: str = "") -> dict:
        """UI에서 직접 추가진입"""
        s = self._get_or_create_state(symbol or self.current_symbol or "default")
        if not s.active:
            return {"error": "활성 거래가 없습니다"}

        handling = self.config.get(f"{add_type}_stop_handling",
                                   self.config["scale_in_stop_handling"])
        if handling == "block":
            return {"error": f"{add_type} 추가진입이 차단되어 있습니다"}

        allowed, reason = self._check_scale_in_gates(add_type, price, qty)
        if not allowed:
            return {"error": reason}

        self._execute_scale_in(price, qty, add_type, "수동")

        # 추가진입 후 P&L 재계산
        if self.bars:
            last_close = self.bars[-1].close
            if s.direction == "long":
                s.unrealized_pnl = (last_close - s.avg_entry) * s.total_qty
            else:
                s.unrealized_pnl = (s.avg_entry - last_close) * s.total_qty
            if s.avg_entry > 0 and s.total_qty > 0:
                s.unrealized_pnl_pct = s.unrealized_pnl / (s.avg_entry * s.total_qty) * 100

        return {"success": True, "state": s.to_dict()}

    def manual_close(self, price: float = 0, symbol: str = "") -> dict:
        """UI에서 직접 청산"""
        s = self._get_or_create_state(symbol or self.current_symbol or "default")
        if not s.active:
            return {"error": "활성 거래가 없습니다"}
        if price <= 0 and self.bars:
            price = self.bars[-1].close
        result = self._close_trade(price, "수동청산", symbol or self.current_symbol or "default")
        return {"success": True, "summary": result}

    # ── 리셋 ──────────────────────────────────────────────

    def reset(self, symbol: str = ""):
        """현재 또는 지정된 포지션 리셋"""
        sym = symbol or self.current_symbol or "default"
        if sym in self.states:
            self.states[sym].reset()
        self.entry_pending = False
        self.manual_touch_occurred = False

    def full_reset(self):
        """모든 포지션 및 데이터 리셋"""
        self.states.clear()
        self._legacy_state.reset()
        self.current_symbol = ""
        self.bars.clear()
        self.atr_values.clear()
        self.current_atr = 0.0
        self.bar_index = 0
        self.entry_pending = False
        self.manual_touch_occurred = False

    # ── 상태 조회 ─────────────────────────────────────────

    def get_status(self) -> dict:
        # 최신 봉 가격으로 모든 포지션 P&L 재계산
        last_close = self.bars[-1].close if self.bars else 0
        for symbol, state in self.states.items():
            if state.active and last_close > 0:
                if state.direction == "long":
                    state.unrealized_pnl = (last_close - state.avg_entry) * state.total_qty
                else:
                    state.unrealized_pnl = (state.avg_entry - last_close) * state.total_qty
                if state.avg_entry > 0 and state.total_qty > 0:
                    state.unrealized_pnl_pct = state.unrealized_pnl / (state.avg_entry * state.total_qty) * 100
                if state.initial_risk_total != 0:
                    state.r_multiple = state.unrealized_pnl / abs(state.initial_risk_total)

        # 현재 활성 포지션들을 모두 반환
        trades = {}
        for symbol, state in self.states.items():
            if state.active or symbol == self.current_symbol:
                trades[symbol] = state.to_dict()
        
        # 하위호환성: 단一 포지션의 경우 "trade" 키도 제공
        if not trades and self.current_symbol in self.states:
            trades[self.current_symbol] = self.states[self.current_symbol].to_dict()
        
        current_trade = self.state.to_dict() if self.state.active else None
        
        return {
            "trade": current_trade,  # 하위호환성 (현재 활성 포지션)
            "trades": trades,  # 새로운 구조 (모든 포지션)
            "atr": _safe_float(round(self.current_atr, 6)),
            "bar_count": len(self.bars),
            "bar_index": self.bar_index,
            "config": self.config,
            "trade_history": self.trade_history[-20:],
            "current_symbol": self.current_symbol,
        }

    def get_chart_data(self) -> dict:
        bars_data = [b.to_dict() for b in self.bars[-500:]]
        # stop_history/fills 내 float 안전 처리
        safe_stops = []
        for s in self.state.stop_history[-500:]:
            safe_stops.append({k: _safe_float(v) if isinstance(v, float) else v for k, v in s.items()})
        safe_fills = []
        for f in self.state.fills:
            safe_fills.append({k: _safe_float(v) if isinstance(v, float) else v for k, v in f.items()})
        return {
            "bars": bars_data,
            "stop_history": safe_stops,
            "fills": safe_fills,
            "entry_price": _safe_float(self.state.avg_entry) if self.state.active else 0,
            "active": self.state.active,
        }

    def get_dashboard_stats(self) -> dict:
        history = self.trade_history
        total = len(history)
        wins = [t for t in history if t.get("pnl", 0) > 0]
        losses = [t for t in history if t.get("pnl", 0) <= 0]

        total_pnl = sum(t.get("pnl", 0) for t in history)
        avg_r_initial = (sum(t.get("r_initial", 0) for t in history) / total) if total > 0 else 0
        win_rate = (len(wins) / total * 100) if total > 0 else 0

        avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
        avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
        profit_factor = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) \
            if losses and sum(t["pnl"] for t in losses) != 0 else 0

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_r": round(avg_r_initial, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "active_trade": self.state.to_dict() if self.state.active else None,
            "current_atr": round(self.current_atr, 6),
            "bar_count": len(self.bars),
        }

    # ── 저장/불러오기 ─────────────────────────────────────

    def save_state(self, filepath: str):
        # 모든 포지션 상태를 딕셔너리로 변환
        states_data = {symbol: state.to_dict() for symbol, state in self.states.items()}
        
        data = {
            "config": self.config,
            "bars": [b.to_dict() for b in self.bars],
            "atr_values": self.atr_values,
            "trade_history": self.trade_history,
            "bar_index": self.bar_index,
            "current_symbol": self.current_symbol,
            "states": states_data,  # 포트폴리오 처리
            "ticker_info": getattr(self, '_ticker_info', {}),
        }
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_state(self, filepath: str):
        if not os.path.exists(filepath):
            return
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.config.update(data.get("config", {}))
        self.bars = [Bar.from_dict(b) for b in data.get("bars", [])]
        self.atr_values = data.get("atr_values", [])
        self.trade_history = data.get("trade_history", [])
        self.bar_index = data.get("bar_index", 0)
        self.current_symbol = data.get("current_symbol", "")
        self._ticker_info = data.get("ticker_info", {})
        
        # 포트폴리오 복원
        states_data = data.get("states", {})
        self.states.clear()
        for symbol, state_dict in states_data.items():
            state = TradeState()
            state.from_dict_restore(state_dict)
            self.states[symbol] = state
        
        if self.atr_values:
            self.current_atr = self.atr_values[-1]


# ─── 설정 검증 ──────────────────────────────────────────────

def validate_config(config: dict) -> List[str]:
    warnings = []
    if config.get("initial_qty", 0) <= 0 and config.get("sizing_mode") == "manual":
        warnings.append("초기 수량이 0 이하입니다.")
    if config.get("risk_per_trade_pct", 0) <= 0 and config.get("sizing_mode") == "risk_based":
        warnings.append("거래당 리스크(%)가 0 이하입니다.")
    if config.get("total_capital", 0) <= 0 and config.get("sizing_mode") == "risk_based":
        warnings.append("총 자본금이 0 이하입니다.")
    if config.get("initial_stop_atr_mult", 0) <= 0:
        warnings.append("초기 손절 ATR 배수가 0 이하입니다.")
    if config.get("trailing_atr_mult", 0) <= 0:
        warnings.append("Trailing ATR 배수가 0 이하입니다.")
    if config.get("atr_length", 0) < 1:
        warnings.append("ATR 기간이 1 미만입니다.")
    # manual_entry_price 경고는 UI에서 직접 가격을 입력하므로 제거
    # (거래 관리 탭에서 직접 가격을 입력하는 방식과 충돌)
    return warnings


# ─── 시장 데이터 자동 가져오기 ────────────────────────────────

# yfinance interval → period 매핑 (최대 기간)
INTERVAL_PERIODS = {
    "1m": "7d",
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "1h": "730d",
    "1d": "10y",
    "1wk": "10y",
    "1mo": "max",
}

INTERVAL_LABELS = {
    "1m": "1분",
    "2m": "2분",
    "5m": "5분",
    "15m": "15분",
    "30m": "30분",
    "60m": "1시간",
    "1h": "1시간",
    "1d": "일봉",
    "1wk": "주봉",
    "1mo": "월봉",
}


class MarketDataFetcher:
    """yfinance를 이용한 시장 데이터 자동 가져오기"""

    @staticmethod
    def search_ticker(query: str) -> list:
        """종목 검색 (yfinance search)"""
        try:
            results = []
            # 직접 티커로 시도
            ticker = yf.Ticker(query)
            info = ticker.info
            if info and info.get("symbol"):
                results.append({
                    "symbol": info.get("symbol", query),
                    "name": info.get("shortName", info.get("longName", query)),
                    "exchange": info.get("exchange", ""),
                    "type": info.get("quoteType", ""),
                    "currency": info.get("currency", ""),
                })
            # yfinance search API
            search = yf.Search(query, max_results=8)
            if hasattr(search, 'quotes') and search.quotes:
                for q in search.quotes:
                    sym = q.get("symbol", "")
                    if sym and not any(r["symbol"] == sym for r in results):
                        results.append({
                            "symbol": sym,
                            "name": q.get("shortname", q.get("longname", sym)),
                            "exchange": q.get("exchange", ""),
                            "type": q.get("quoteType", ""),
                            "currency": q.get("currency", ""),
                        })
            return results[:10]
        except Exception as e:
            return [{"error": str(e)}]

    @staticmethod
    def fetch_bars(symbol: str, interval: str = "1d", period: str = None,
                   count: int = 200) -> List[Bar]:
        """종목의 OHLCV 데이터를 Bar 리스트로 가져옴"""
        if not period:
            period = INTERVAL_PERIODS.get(interval, "1y")

        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval)

            if df is None or df.empty:
                return []

            # NaN 제거 (yfinance가 간혹 NaN 반환)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                return []

            # 최근 count개만
            if len(df) > count:
                df = df.tail(count)

            bars = []
            for idx, row in df.iterrows():
                ts = idx.strftime("%Y-%m-%d %H:%M") if hasattr(idx, 'strftime') else str(idx)
                bars.append(Bar(
                    timestamp=ts,
                    open_p=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0)),
                ))
            return bars
        except Exception as e:
            print(f"[MarketDataFetcher] fetch error: {e}")
            return []

    @staticmethod
    def get_ticker_info(symbol: str) -> dict:
        """종목 기본 정보"""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return {
                "symbol": info.get("symbol", symbol),
                "name": info.get("shortName", info.get("longName", symbol)),
                "currency": info.get("currency", ""),
                "exchange": info.get("exchange", ""),
                "market_price": info.get("regularMarketPrice", info.get("currentPrice", 0)),
                "previous_close": info.get("previousClose", 0),
                "market_cap": info.get("marketCap", 0),
                "sector": info.get("sector", ""),
            }
        except Exception:
            return {"symbol": symbol, "name": symbol}

    @staticmethod
    def get_latest_price(symbol: str) -> dict:
        """현재가 / 최근 종가"""
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="2d", interval="1d")
            if hist is not None and not hist.empty:
                last = hist.iloc[-1]
                return {
                    "price": float(last["Close"]),
                    "high": float(last["High"]),
                    "low": float(last["Low"]),
                    "open": float(last["Open"]),
                    "volume": float(last.get("Volume", 0)),
                    "timestamp": hist.index[-1].strftime("%Y-%m-%d %H:%M"),
                }
            return {"price": 0}
        except Exception:
            return {"price": 0}
