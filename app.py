#!/usr/bin/env python3
"""
DLSI - Daily Liquidity Stress Index
Indicador diario de estrés de liquidez basado en proxies de mercado.

Componentes:
1. VIX (20%) - Volatilidad implícita
2. Credit Spread HY-IG (20%) - Riesgo de crédito  
3. TED Spread (15%) - Estrés interbancario
4. DXY Momentum (15%) - Presión del dólar
5. Yield Curve 2Y-10Y (15%) - Expectativas económicas
6. SOFR-FF Spread (15%) - Tensión en funding

Escala: 0-100 (0=sin estrés, 100=estrés máximo)
"""

import os
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import requests

app = Flask(__name__, static_folder='.')
CORS(app)

# ============================================
# CONFIGURACIÓN
# ============================================

FRED_API_KEY = os.environ.get('FRED_API_KEY', 'TU_API_KEY_AQUI')
FRED_BASE_URL = 'https://api.stlouisfed.org/fred/series/observations'

# Series FRED - todas diarias
FRED_SERIES = {
    'VIX': 'VIXCLS',                    # VIX Close (diario)
    'HY_SPREAD': 'BAMLH0A0HYM2',        # HY Option-Adjusted Spread (diario)
    'IG_SPREAD': 'BAMLC0A0CM',          # IG Option-Adjusted Spread (diario)
    'FED_FUNDS': 'DFF',                 # Fed Funds Effective Rate (diario)
    'SOFR': 'SOFR',                     # SOFR Rate (diario)
    'TBILL_3M': 'DTB3',                 # 3-Month T-Bill (diario)
    'YIELD_2Y': 'DGS2',                 # 2-Year Treasury (diario)
    'YIELD_10Y': 'DGS10',               # 10-Year Treasury (diario)
    'YIELD_CURVE': 'T10Y2Y',            # 10Y-2Y Spread (diario)
    'TED_SPREAD': 'TEDRATE',            # TED Spread (diario)
    'DXY': 'DTWEXBGS',                  # Trade Weighted USD Broad (diario)
}

# Thresholds para normalización (basados en históricos)
THRESHOLDS = {
    'VIX': {'low': 12, 'mid': 20, 'high': 30, 'extreme': 45},
    'CREDIT_SPREAD': {'low': 1.0, 'mid': 2.0, 'high': 4.0, 'extreme': 8.0},  # HY-IG diff
    'TED_SPREAD': {'low': 0.1, 'mid': 0.3, 'high': 0.5, 'extreme': 1.0},
    'DXY_CHANGE': {'low': 0.5, 'mid': 1.0, 'high': 2.0, 'extreme': 3.0},  # % change 5d
    'YIELD_CURVE': {'inverted': -0.5, 'flat': 0, 'normal': 1.0, 'steep': 2.0},
    'SOFR_FF': {'low': 0.02, 'mid': 0.05, 'high': 0.10, 'extreme': 0.20},
}

# Cache
cache = {}
CACHE_DURATION = timedelta(minutes=15)

# ============================================
# DATA FETCHING
# ============================================

def fetch_fred_series(series_id, days=90):
    """Fetch data from FRED API"""
    cache_key = f"fred_{series_id}"
    
    if cache_key in cache:
        data, timestamp = cache[cache_key]
        if datetime.now() - timestamp < CACHE_DURATION:
            return data
    
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    try:
        params = {
            'series_id': series_id,
            'api_key': FRED_API_KEY,
            'file_type': 'json',
            'observation_start': start_date,
            'sort_order': 'asc'
        }
        
        response = requests.get(FRED_BASE_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if 'error_message' in data:
            print(f"FRED error for {series_id}: {data['error_message']}")
            return None
        
        observations = []
        for obs in data.get('observations', []):
            if obs['value'] != '.':
                try:
                    observations.append({
                        'date': obs['date'],
                        'value': float(obs['value'])
                    })
                except ValueError:
                    continue
        
        cache[cache_key] = (observations, datetime.now())
        return observations
        
    except Exception as e:
        print(f"Error fetching FRED {series_id}: {e}")
        return None


def get_latest(arr, offset=0):
    """Get latest value from array with optional offset"""
    if not arr or len(arr) <= offset:
        return None
    return arr[-(1 + offset)]['value']


def get_change_pct(arr, period=5):
    """Get percentage change over period"""
    if not arr or len(arr) <= period:
        return None
    current = arr[-1]['value']
    prev = arr[-(1 + period)]['value']
    if prev == 0:
        return None
    return ((current - prev) / prev) * 100


# ============================================
# DLSI CALCULATION
# ============================================

def calculate_stress_score(value, thresholds, invert=False):
    """
    Convert a value to a 0-100 stress score based on thresholds.
    Higher score = more stress.
    """
    if value is None:
        return 50  # Neutral if no data
    
    low = thresholds.get('low', 0)
    mid = thresholds.get('mid', 50)
    high = thresholds.get('high', 75)
    extreme = thresholds.get('extreme', 100)
    
    if value <= low:
        score = (value / low) * 20 if low > 0 else 0
    elif value <= mid:
        score = 20 + ((value - low) / (mid - low)) * 30
    elif value <= high:
        score = 50 + ((value - mid) / (high - mid)) * 25
    elif value <= extreme:
        score = 75 + ((value - high) / (extreme - high)) * 20
    else:
        score = 95 + min(5, (value - extreme) / extreme * 5)
    
    score = max(0, min(100, score))
    
    if invert:
        score = 100 - score
    
    return score


def calculate_yield_curve_score(spread):
    """
    Yield curve stress score.
    Inverted curve = high stress, steep curve = low stress
    """
    if spread is None:
        return 50
    
    if spread < -1.0:  # Deeply inverted
        return 95
    elif spread < -0.5:  # Inverted
        return 80
    elif spread < 0:  # Slightly inverted
        return 65
    elif spread < 0.5:  # Flat
        return 50
    elif spread < 1.0:  # Normal
        return 35
    elif spread < 1.5:  # Healthy
        return 20
    else:  # Steep
        return 10


def calculate_dlsi(data):
    """Calculate the Daily Liquidity Stress Index"""
    
    # Extract latest values
    vix = get_latest(data.get('VIX'))
    hy_spread = get_latest(data.get('HY_SPREAD'))
    ig_spread = get_latest(data.get('IG_SPREAD'))
    fed_funds = get_latest(data.get('FED_FUNDS'))
    sofr = get_latest(data.get('SOFR'))
    yield_curve = get_latest(data.get('YIELD_CURVE'))
    ted_spread = get_latest(data.get('TED_SPREAD'))
    dxy_change = get_change_pct(data.get('DXY'), 5)
    
    # Calculate credit spread (HY - IG)
    credit_spread = None
    if hy_spread is not None and ig_spread is not None:
        credit_spread = hy_spread - ig_spread
    
    # Calculate SOFR-FF spread
    sofr_ff_spread = None
    if sofr is not None and fed_funds is not None:
        sofr_ff_spread = abs(sofr - fed_funds)
    
    # Component scores
    components = {}
    
    # 1. VIX (20%)
    vix_score = calculate_stress_score(vix, THRESHOLDS['VIX'])
    components['vix'] = {
        'name': 'VIX',
        'value': vix,
        'score': vix_score,
        'weight': 0.20,
        'description': 'Volatilidad implícita SPX'
    }
    
    # 2. Credit Spread HY-IG (20%)
    credit_score = calculate_stress_score(credit_spread, THRESHOLDS['CREDIT_SPREAD'])
    components['credit'] = {
        'name': 'Credit Spread',
        'value': credit_spread,
        'hy': hy_spread,
        'ig': ig_spread,
        'score': credit_score,
        'weight': 0.20,
        'description': 'HY - IG Option-Adjusted Spread'
    }
    
    # 3. TED Spread (15%)
    ted_score = calculate_stress_score(ted_spread, THRESHOLDS['TED_SPREAD'])
    components['ted'] = {
        'name': 'TED Spread',
        'value': ted_spread,
        'score': ted_score,
        'weight': 0.15,
        'description': 'T-Bill vs LIBOR spread'
    }
    
    # 4. DXY Momentum (15%)
    dxy_score = calculate_stress_score(abs(dxy_change) if dxy_change else 0, THRESHOLDS['DXY_CHANGE'])
    # Strong dollar move in either direction = stress
    components['dxy'] = {
        'name': 'USD Momentum',
        'value': dxy_change,
        'score': dxy_score,
        'weight': 0.15,
        'description': 'Trade-weighted USD cambio 5d'
    }
    
    # 5. Yield Curve (15%)
    curve_score = calculate_yield_curve_score(yield_curve)
    components['curve'] = {
        'name': 'Yield Curve',
        'value': yield_curve,
        'score': curve_score,
        'weight': 0.15,
        'description': '10Y-2Y Treasury spread'
    }
    
    # 6. SOFR-FF Spread (15%)
    sofr_score = calculate_stress_score(sofr_ff_spread, THRESHOLDS['SOFR_FF'])
    components['sofr'] = {
        'name': 'SOFR-FF Spread',
        'value': sofr_ff_spread,
        'sofr': sofr,
        'fed_funds': fed_funds,
        'score': sofr_score,
        'weight': 0.15,
        'description': 'SOFR vs Fed Funds spread'
    }
    
    # Calculate weighted DLSI
    dlsi = 0
    for key in components:
        dlsi += components[key]['score'] * components[key]['weight']
    
    return {
        'dlsi': dlsi,
        'components': components,
        'interpretation': get_interpretation(dlsi),
        'timestamp': datetime.now().isoformat()
    }


def get_interpretation(dlsi):
    """Get text interpretation of DLSI value"""
    if dlsi < 20:
        return {
            'level': 'VERY LOW',
            'emoji': '🟢',
            'text': 'Condiciones de liquidez excelentes. Mercados tranquilos, baja volatilidad.',
            'action': 'Favorable para risk assets'
        }
    elif dlsi < 35:
        return {
            'level': 'LOW',
            'emoji': '🟢',
            'text': 'Estrés bajo. Condiciones normales de mercado.',
            'action': 'Ambiente constructivo'
        }
    elif dlsi < 50:
        return {
            'level': 'MODERATE',
            'emoji': '🟡',
            'text': 'Estrés moderado. Algunas tensiones pero manejables.',
            'action': 'Monitorear evolución'
        }
    elif dlsi < 65:
        return {
            'level': 'ELEVATED',
            'emoji': '🟠',
            'text': 'Estrés elevado. Tensiones visibles en múltiples indicadores.',
            'action': 'Cautela recomendada'
        }
    elif dlsi < 80:
        return {
            'level': 'HIGH',
            'emoji': '🔴',
            'text': 'Estrés alto. Condiciones de liquidez deteriorándose.',
            'action': 'Reducir exposición a riesgo'
        }
    else:
        return {
            'level': 'EXTREME',
            'emoji': '🔴',
            'text': 'Estrés extremo. Crisis de liquidez potencial.',
            'action': 'Modo defensivo'
        }


def build_history(data, days=60):
    """Build historical DLSI values"""
    
    # Use VIX as base since it's the most reliable daily series
    vix_data = data.get('VIX', [])
    if not vix_data:
        return []
    
    # Build maps for all series
    maps = {}
    for key, series_data in data.items():
        if series_data:
            maps[key] = {d['date']: d['value'] for d in series_data}
    
    history = []
    start_idx = max(0, len(vix_data) - days - 5)
    
    for i in range(start_idx + 5, len(vix_data)):
        date = vix_data[i]['date']
        
        # Get values for this date
        vix = maps.get('VIX', {}).get(date)
        hy = maps.get('HY_SPREAD', {}).get(date)
        ig = maps.get('IG_SPREAD', {}).get(date)
        ted = maps.get('TED_SPREAD', {}).get(date)
        curve = maps.get('YIELD_CURVE', {}).get(date)
        sofr = maps.get('SOFR', {}).get(date)
        ff = maps.get('FED_FUNDS', {}).get(date)
        
        # DXY change needs 5-day lookback
        dxy_current = maps.get('DXY', {}).get(date)
        prev_date = vix_data[i-5]['date'] if i >= 5 else None
        dxy_prev = maps.get('DXY', {}).get(prev_date) if prev_date else None
        dxy_change = ((dxy_current - dxy_prev) / dxy_prev * 100) if dxy_current and dxy_prev else None
        
        # Calculate component scores
        vix_score = calculate_stress_score(vix, THRESHOLDS['VIX']) if vix else 50
        
        credit_spread = (hy - ig) if hy and ig else None
        credit_score = calculate_stress_score(credit_spread, THRESHOLDS['CREDIT_SPREAD']) if credit_spread else 50
        
        ted_score = calculate_stress_score(ted, THRESHOLDS['TED_SPREAD']) if ted else 50
        
        dxy_score = calculate_stress_score(abs(dxy_change) if dxy_change else 0, THRESHOLDS['DXY_CHANGE'])
        
        curve_score = calculate_yield_curve_score(curve) if curve else 50
        
        sofr_ff = abs(sofr - ff) if sofr and ff else None
        sofr_score = calculate_stress_score(sofr_ff, THRESHOLDS['SOFR_FF']) if sofr_ff else 50
        
        # Calculate DLSI
        dlsi = (vix_score * 0.20 + 
                credit_score * 0.20 + 
                ted_score * 0.15 + 
                dxy_score * 0.15 + 
                curve_score * 0.15 + 
                sofr_score * 0.15)
        
        history.append({
            'date': date,
            'value': dlsi
        })
    
    return history


# ============================================
# ROUTES
# ============================================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/dlsi')
def get_dlsi():
    """Get DLSI data"""
    
    if FRED_API_KEY == 'TU_API_KEY_AQUI':
        return jsonify({'error': 'Configura tu FRED API key'}), 400
    
    # Fetch all series
    data = {}
    errors = []
    
    for key, series_id in FRED_SERIES.items():
        series_data = fetch_fred_series(series_id)
        if series_data:
            data[key] = series_data
        else:
            errors.append({'series': key, 'error': 'Failed to fetch'})
    
    # Calculate DLSI
    result = calculate_dlsi(data)
    result['history'] = build_history(data)
    result['errors'] = errors
    
    return jsonify(result)


@app.route('/api/all')
def get_all_data():
    """Get all data (for unified dashboard)"""
    
    if FRED_API_KEY == 'TU_API_KEY_AQUI':
        return jsonify({'error': 'Configura tu FRED API key'}), 400
    
    results = {}
    errors = []
    
    # Fetch LII series
    lii_series = {
        'RESERVES': 'WRESBAL',
        'RRP': 'RRPONTSYD',
        'WALCL': 'WALCL',
        'FOREIGN_REPO': 'WLRRAFOIAL',
        'BANK_BORROWING': 'WLCFLPCL',
    }
    
    for key, series_id in lii_series.items():
        data = fetch_fred_series(series_id, days=120)
        if data:
            results[key] = data
        else:
            errors.append({'series': key, 'error': 'Failed to fetch'})
    
    # Fetch TGA from Treasury
    tga_data = fetch_treasury_tga()
    if tga_data:
        results['TGA_DAILY'] = tga_data
    
    # Fetch DLSI series (sin prefijo, el dashboard los espera así)
    dlsi_series = {
        'VIX': 'VIXCLS',
        'HY_SPREAD': 'BAMLH0A0HYM2',
        'IG_SPREAD': 'BAMLC0A0CM',
        'EFFR': 'DFF',
        'SOFR': 'SOFR',
        'DGS2': 'DGS2',
        'DGS10': 'DGS10',
        'DXY': 'DTWEXBGS',
        'VIX3M': 'VIXCLS',  # Usamos VIX como proxy
        'USDJPY': 'DEXJPUS',  # USD/JPY exchange rate (diario)
    }
    
    for key, series_id in dlsi_series.items():
        data = fetch_fred_series(series_id)
        if data:
            results[key] = data
        else:
            errors.append({'series': key, 'error': 'Failed to fetch'})
    
    return jsonify({
        'data': results,
        'errors': errors,
        'timestamp': datetime.now().isoformat()
    })


def fetch_treasury_tga(days=120):
    """Fetch daily TGA from Treasury Fiscal Data API"""
    cache_key = "treasury_tga"
    
    if cache_key in cache:
        data, timestamp = cache[cache_key]
        if datetime.now() - timestamp < CACHE_DURATION:
            return data
    
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    try:
        endpoint = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
        
        params = {
            'filter': f'record_date:gte:{start_date},account_type:eq:Treasury General Account (TGA) Closing Balance',
            'fields': 'record_date,account_type,open_today_bal,open_month_bal,open_fiscal_year_bal',
            'sort': 'record_date',
            'page[size]': 1000,
            'format': 'json'
        }
        
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        observations = []
        for item in data.get('data', []):
            try:
                bal = None
                for field in ['open_today_bal', 'open_month_bal', 'open_fiscal_year_bal']:
                    val = item.get(field)
                    if val and val != 'null' and val != '':
                        try:
                            bal = float(val)
                            break
                        except (ValueError, TypeError):
                            continue
                
                if bal is not None:
                    observations.append({
                        'date': item['record_date'],
                        'value': bal
                    })
            except:
                continue
        
        observations.sort(key=lambda x: x['date'])
        
        if len(observations) > 0:
            cache[cache_key] = (observations, datetime.now())
            return observations
        
        return None
        
    except Exception as e:
        print(f"Error fetching Treasury TGA: {e}")
        return None


@app.route('/api/status')
def status():
    return jsonify({
        'status': 'ok',
        'version': 'Liquidity Dashboard 2.0 (LII + DLSI)',
        'api_key_configured': FRED_API_KEY != 'TU_API_KEY_AQUI',
        'indices': ['IELG', 'LII', 'DLSI'],
        'timestamp': datetime.now().isoformat()
    })


# ============================================
# MAIN
# ============================================

if __name__ == '__main__':
    print("""
╔═══════════════════════════════════════════════════════════╗
║     Liquidity Dashboard Server (LII + DLSI)               ║
╠═══════════════════════════════════════════════════════════╣
║                                                           ║
║  Índices:                                                 ║
║  • IELG v2 - Estrés macro (semanal)                       ║
║  • LII - Liquidity Impulse Index (5 días)                 ║
║  • DLSI - Daily Liquidity Stress Index (diario)           ║
║                                                           ║
║  DLSI Componentes:                                        ║
║  • VIX (20%) - Volatilidad                                ║
║  • Credit Spread HY-IG (20%) - Riesgo crédito             ║
║  • TED Spread (15%) - Estrés interbancario                ║
║  • USD Momentum (15%) - Presión dólar                     ║
║  • Yield Curve (15%) - 10Y-2Y spread                      ║
║  • SOFR-FF Spread (15%) - Tensión funding                 ║
║                                                           ║
╠═══════════════════════════════════════════════════════════╣
║  Abrí http://localhost:8081                               ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    if FRED_API_KEY == 'TU_API_KEY_AQUI':
        print("⚠️  ADVERTENCIA: No configuraste tu FRED API key!\n")
    else:
        print(f"✅ FRED API Key: {FRED_API_KEY[:8]}...{FRED_API_KEY[-4:]}\n")
    
    # Render usa la variable PORT
    port = int(os.environ.get('PORT', 8081))
    app.run(host='0.0.0.0', port=port, debug=False)
