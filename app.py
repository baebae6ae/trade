"""
거래 관리 시스템 - Flask 애플리케이션
"""

import os
import json
import csv
import io
import threading
from flask import Flask, render_template, request, jsonify, redirect, url_for
from engine import TradeEngine, Bar, DEFAULT_CONFIG, validate_config, MarketDataFetcher, INTERVAL_PERIODS, INTERVAL_LABELS

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
os.makedirs(DATA_DIR, exist_ok=True)

# 전역 엔진 인스턴스
engine = TradeEngine()

# 저장된 상태 불러오기
if os.path.exists(STATE_FILE):
    try:
        engine.load_state(STATE_FILE)
    except Exception:
        pass

# 시작 시 현재 종목의 봉 데이터 자동 로드 (ATR 계산용)
fetcher = MarketDataFetcher()
try:
    startup_symbol = engine.config.get("ticker_symbol", "")
    if startup_symbol:
        bars = fetcher.fetch_bars(
            startup_symbol,
            interval=engine.config.get("data_interval", "1d"),
            count=engine.config.get("data_count", 200)
        )
        if bars:
            for bar in bars:
                engine.bars.append(bar)
                engine.bar_index = len(engine.bars) - 1
                engine._update_atr()
            engine.update_price_cache(startup_symbol, bars[-1].close)
except Exception:
    pass


def _save():
    """state.json에 포지션 데이터 저장"""
    engine.save_state(STATE_FILE)


# ─── 페이지 라우트 ──────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/trade")
def trade():
    return render_template("trade.html")


@app.route("/settings")
def settings():
    return render_template("settings.html")


@app.route("/history")
def history():
    return render_template("history.html")


# ─── API 라우트 ─────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify(engine.get_status())


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(engine.get_dashboard_stats())


@app.route("/api/chart")
def api_chart():
    return jsonify(engine.get_chart_data())


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(engine.config)


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json()
    if not data:
        return jsonify({"error": "설정 데이터가 없습니다"}), 400

    # 타입 변환
    for key, val in data.items():
        if key in engine.config:
            expected = type(DEFAULT_CONFIG.get(key, val))
            try:
                if expected == bool:
                    data[key] = bool(val)
                elif expected == int:
                    data[key] = int(val)
                elif expected == float:
                    data[key] = float(val)
            except (ValueError, TypeError):
                pass

    engine.config.update(data)
    warnings = validate_config(engine.config) if engine.config.get("show_warnings") else []
    _save()
    return jsonify({"success": True, "warnings": warnings})


@app.route("/api/config/reset", methods=["POST"])
def api_reset_config():
    from copy import deepcopy
    engine.config = deepcopy(DEFAULT_CONFIG)
    _save()
    return jsonify({"success": True})


@app.route("/api/bar", methods=["POST"])
def api_add_bar():
    """새 봉 데이터 추가"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "봉 데이터가 없습니다"}), 400

    try:
        bar = Bar(
            timestamp=str(data.get("timestamp", "")),
            open_p=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data.get("volume", 0)),
        )
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"잘못된 데이터: {e}"}), 400

    # OHLC 유효성 검증
    if bar.high < bar.low:
        return jsonify({"error": "고가가 저가보다 낮습니다"}), 400
    if bar.high < bar.open or bar.high < bar.close:
        return jsonify({"error": "고가가 시가/종가보다 낮습니다"}), 400
    if bar.low > bar.open or bar.low > bar.close:
        return jsonify({"error": "저가가 시가/종가보다 높습니다"}), 400

    result = engine.process_bar(bar)
    _save()
    return jsonify(result)


@app.route("/api/bars/bulk", methods=["POST"])
def api_add_bars_bulk():
    """CSV 형식 봉 데이터 일괄 추가"""
    data = request.get_json()
    if not data or "bars" not in data:
        return jsonify({"error": "봉 데이터가 없습니다"}), 400

    results = []
    for bd in data["bars"]:
        try:
            bar = Bar(
                timestamp=str(bd.get("timestamp", "")),
                open_p=float(bd["open"]),
                high=float(bd["high"]),
                low=float(bd["low"]),
                close=float(bd["close"]),
                volume=float(bd.get("volume", 0)),
            )
            if bar.high < bar.low:
                continue
            result = engine.process_bar(bar)
            results.append(result)
        except (KeyError, ValueError, TypeError):
            continue

    _save()
    last = results[-1] if results else {}
    return jsonify({
        "processed": len(results),
        "last_result": last,
        "state": engine.state.to_dict() if engine.state.active else None,
    })


@app.route("/api/bars/csv", methods=["POST"])
def api_upload_csv():
    """CSV 파일 업로드"""
    if "file" not in request.files:
        # JSON body에서 CSV 텍스트 읽기
        data = request.get_json()
        if not data or "csv" not in data:
            return jsonify({"error": "CSV 데이터가 없습니다"}), 400
        csv_text = data["csv"]
    else:
        csv_file = request.files["file"]
        csv_text = csv_file.read().decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(csv_text))
    bars_data = []
    for row in reader:
        try:
            bars_data.append({
                "timestamp": row.get("timestamp", row.get("date", row.get("time", ""))),
                "open": float(row.get("open", row.get("Open", 0))),
                "high": float(row.get("high", row.get("High", 0))),
                "low": float(row.get("low", row.get("Low", 0))),
                "close": float(row.get("close", row.get("Close", 0))),
                "volume": float(row.get("volume", row.get("Volume", 0))),
            })
        except (ValueError, TypeError):
            continue

    results = []
    for bd in bars_data:
        bar = Bar(bd["timestamp"], bd["open"], bd["high"], bd["low"],
                  bd["close"], bd["volume"])
        if bar.high >= bar.low:
            result = engine.process_bar(bar)
            results.append(result)

    _save()
    return jsonify({
        "processed": len(results),
        "total_bars": len(engine.bars),
        "state": engine.state.to_dict() if engine.state.active else None,
    })


@app.route("/api/entry", methods=["POST"])
def api_manual_entry():
    """수동 진입 (포트폴리오 지원)"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "데이터가 없습니다"}), 400

    price = float(data.get("price", 0))
    qty = float(data.get("qty", 0))
    entry_date = data.get("entry_date", "")
    symbol = data.get("symbol", "")  # 포트폴리오 심볼
    if price <= 0:
        return jsonify({"error": "가격을 입력하세요"}), 400

    result = engine.manual_entry(price, qty, entry_date=entry_date, symbol=symbol)
    _save()
    return jsonify(result)


@app.route("/api/add", methods=["POST"])
def api_manual_add():
    """수동 추가진입 (포트폴리오 지원, 날짜 기능)"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "데이터가 없습니다"}), 400

    price = float(data.get("price", 0))
    qty = float(data.get("qty", 0))
    add_type = data.get("type", "pyramid")
    symbol = data.get("symbol", "")  # 포트폴리오 심볼
    entry_date = data.get("entry_date", "")  # 날짜 기능

    if price <= 0 or qty <= 0:
        return jsonify({"error": "가격과 수량을 입력하세요"}), 400
    if add_type not in ("pyramid", "avg_down"):
        return jsonify({"error": "유형은 pyramid 또는 avg_down"}), 400

    result = engine.manual_add(price, qty, add_type, symbol=symbol, entry_date=entry_date)
    _save()
    return jsonify(result)


@app.route("/api/close", methods=["POST"])
def api_manual_close():
    """수동 청산 (전체/부분, 포트폴리오 지원)"""
    data = request.get_json() or {}
    price = float(data.get("price", 0))
    qty = float(data.get("qty", 0))  # 매도 수량 (0이면 전체 매도)
    symbol = data.get("symbol", "")  # 포트폴리오 심볼
    result = engine.manual_close(price, qty=qty, symbol=symbol)
    _save()
    return jsonify(result)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """거래 리셋 (가격 데이터 유지, 포트폴리오 지원)"""
    data = request.get_json() or {}
    symbol = data.get("symbol", "")
    engine.reset(symbol=symbol)
    _save()
    return jsonify({"success": True})


@app.route("/api/full-reset", methods=["POST"])
def api_full_reset():
    """전체 리셋 (모든 데이터 초기화)"""
    engine.full_reset()
    engine.trade_history.clear()
    _save()
    return jsonify({"success": True})


@app.route("/api/positions")
def api_positions():
    """모든 활성 포지션 목록 반환 (종목별 실제 가격으로 P&L 계산)"""
    positions = []
    for symbol, state in engine.states.items():
        if state.active:
            # 현재 로드된 종목이면 bars 가격, 아니면 캐시된 가격 사용
            if symbol == engine.current_symbol and engine.bars:
                current_price = engine.bars[-1].close
            else:
                current_price = engine.get_cached_price(symbol)
            
            # P&L 계산
            if current_price > 0:
                if state.direction == "long":
                    state.unrealized_pnl = (current_price - state.avg_entry) * state.total_qty
                else:
                    state.unrealized_pnl = (state.avg_entry - current_price) * state.total_qty
                if state.avg_entry > 0 and state.total_qty > 0:
                    state.unrealized_pnl_pct = state.unrealized_pnl / (state.avg_entry * state.total_qty) * 100
                if state.initial_risk_total != 0:
                    state.r_multiple = state.unrealized_pnl / abs(state.initial_risk_total)

            signal = engine._get_position_signal(state, current_price)
            positions.append({
                "symbol": symbol,
                "direction": state.direction,
                "qty": state.total_qty,
                "entry_price": round(state.avg_entry, 6) if state.avg_entry > 0 else 0,
                "current_price": round(current_price, 2) if current_price > 0 else 0,
                "unrealized_pnl": round(state.unrealized_pnl, 2),
                "unrealized_pnl_pct": round(state.unrealized_pnl_pct, 2),
                "bars_in_trade": state.bars_in_trade,
                "initial_stop": round(state.initial_stop, 6),
                "active_stop": round(state.active_stop, 6),
                "trailing_stop": round(state.trailing_stop, 6),
                "breakeven_stop": round(state.breakeven_stop, 6),
                "r_multiple": round(state.r_multiple, 4),
                "pyramid_count": state.pyramid_count,
                "avg_down_count": state.avg_down_count,
                "current_risk": round(state.current_risk, 2),
                "fills": state.fills,
                "events": state.events[-20:],
                "signal": signal,
            })
    return jsonify({"positions": positions, "count": len(positions)})


@app.route("/api/position/<symbol>")
def api_position(symbol):
    """특정 심볼 포지션 상세 조회"""
    if symbol not in engine.states:
        return jsonify({"error": f"포지션 '{symbol}' 없음"}), 404
    
    state = engine.states[symbol]
    if not state.active:
        return jsonify({"error": f"포지션 '{symbol}' 비활성"}), 404
    
    return jsonify(state.to_dict())


@app.route("/api/history")
def api_history():
    return jsonify(engine.trade_history)


@app.route("/api/events")
def api_events():
    """현재 심볼의 이벤트 로그 조회"""
    state = engine.state  # current_symbol의 상태
    if state:
        return jsonify(state.events[-200:])
    return jsonify([])


# ─── 시장 데이터 API ────────────────────────────────────────

# fetcher는 상단에서 이미 생성됨


@app.route("/api/ticker/search")
def api_ticker_search():
    """종목 검색"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    results = fetcher.search_ticker(q)
    return jsonify(results)


@app.route("/api/ticker/info")
def api_ticker_info():
    """종목 정보"""
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "종목 코드 필요"}), 400
    info = fetcher.get_ticker_info(symbol)
    return jsonify(info)


@app.route("/api/ticker/price")
def api_ticker_price():
    """현재가"""
    symbol = request.args.get("symbol", engine.config.get("ticker_symbol", "")).strip()
    if not symbol:
        return jsonify({"error": "종목 코드 필요"}), 400
    price = fetcher.get_latest_price(symbol)
    return jsonify(price)


@app.route("/api/ticker/set", methods=["POST"])
def api_ticker_set():
    """종목 설정 + 데이터 가져오기 (종목 선택 시 호출)"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "데이터 없음"}), 400

    symbol = data.get("symbol", "").strip()
    interval = data.get("interval", "1d")
    count = int(data.get("count", 200))

    if not symbol:
        return jsonify({"error": "종목 코드 필요"}), 400

    # 종목 정보 가져오기
    info = fetcher.get_ticker_info(symbol)

    # 봉 데이터 가져오기
    bars = fetcher.fetch_bars(symbol, interval=interval, count=count)
    if not bars:
        return jsonify({"error": f"{symbol} 데이터를 가져올 수 없습니다"}), 400

    # 기존 포트폴리오 포지션 보존! (봉/ATR만 초기화)
    saved_states = dict(engine.states)  # 포지션 백업
    saved_history = list(engine.trade_history)  # 거래 내역 백업

    engine.bars.clear()
    engine.atr_values.clear()
    engine.current_atr = 0.0
    engine.bar_index = 0
    engine.entry_pending = False
    engine.manual_touch_occurred = False

    for bar in bars:
        engine.bars.append(bar)
        engine.bar_index = len(engine.bars) - 1
        engine._update_atr()

    # 포지션 & 거래내역 복원
    engine.states = saved_states
    engine.trade_history = saved_history
    
    # 현재 선택 종목으로 업데이트 (다중 종목 UI 동기화)
    engine.current_symbol = symbol
    
    # 현재가 캐시 업데이트
    if bars:
        engine.update_price_cache(symbol, bars[-1].close)

    # 설정 업데이트
    engine.config["ticker_symbol"] = symbol
    engine.config["ticker_name"] = info.get("name", symbol)
    engine.config["data_interval"] = interval
    engine.config["data_count"] = count
    engine._ticker_info = info

    _save()
    return jsonify({
        "success": True,
        "symbol": symbol,
        "name": info.get("name", symbol),
        "bars_loaded": len(bars),
        "atr": round(engine.current_atr, 6),
        "last_price": bars[-1].close if bars else 0,
        "info": info,
    })


@app.route("/api/ticker/refresh", methods=["POST"])
def api_ticker_refresh():
    """현재 설정된 종목의 데이터를 새로 가져오기 (갱신)"""
    symbol = engine.config.get("ticker_symbol", "")
    interval = engine.config.get("data_interval", "1d")
    count = engine.config.get("data_count", 200)

    if not symbol:
        return jsonify({"error": "설정된 종목이 없습니다"}), 400

    # 포트폴리오 상태 전체 백업
    saved_states = dict(engine.states)
    saved_history = list(engine.trade_history)

    # 새 데이터 가져오기
    bars = fetcher.fetch_bars(symbol, interval=interval, count=count)
    if not bars:
        return jsonify({"error": "데이터 갱신 실패"}), 400

    # 봉/ATR만 리셋
    engine.bars.clear()
    engine.atr_values.clear()
    engine.current_atr = 0.0
    engine.bar_index = 0

    for bar in bars:
        engine.bars.append(bar)
        engine.bar_index = len(engine.bars) - 1
        engine._update_atr()

    # 포트폴리오 상태 복원
    engine.states = saved_states
    engine.trade_history = saved_history

    _save()
    return jsonify({
        "success": True,
        "bars_loaded": len(bars),
        "atr": round(engine.current_atr, 6),
        "last_price": bars[-1].close if bars else 0,
        "trade_preserved": len([s for s in engine.states.values() if s.active]) > 0,
    })


@app.route("/api/intervals")
def api_intervals():
    """사용 가능한 인터벌 목록"""
    return jsonify({
        "intervals": [
            {"value": k, "label": INTERVAL_LABELS.get(k, k)}
            for k in INTERVAL_PERIODS.keys()
        ]
    })


@app.route("/api/prices/refresh", methods=["POST"])
def api_refresh_prices():
    """모든 활성 포지션의 현재가 일괄 업데이트"""
    updated = {}
    for symbol, state in engine.states.items():
        if state.active:
            try:
                price_data = fetcher.get_latest_price(symbol)
                price = price_data.get("price", 0)
                if price > 0:
                    engine.update_price_cache(symbol, price)
                    updated[symbol] = price
            except Exception:
                pass
    # 현재 선택된 종목은 bars에서 최신 가격도 업데이트
    if engine.current_symbol and engine.bars:
        price = engine.bars[-1].close
        engine.update_price_cache(engine.current_symbol, price)
        updated[engine.current_symbol] = price
    _save()
    return jsonify({"updated": updated, "count": len(updated)})


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
