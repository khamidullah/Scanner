# -*- coding: utf-8 -*-
"""
Облачный сканер SPRING + TEST (5m/15m/1h) для GitHub Actions.
Один запуск = один проход. Память (отправленные сигналы и время
последних сканов) живет в state.json и переносится между запусками.
Почта берется из секретов GitHub (переменные окружения).
"""
import ccxt
import pandas as pd
import time
import smtplib
import os
import json
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

# ═══════════ 1. НАСТРОЙКИ ═══════════
EMAIL_LOGIN = os.environ.get('EMAIL_LOGIN', '')
EMAIL_APP_PASSWORD = os.environ.get('EMAIL_APP_PASSWORD', '')
EMAIL_TO = os.environ.get('EMAIL_TO', '')

TIMEFRAMES = {
    '5m':  {'scan_every_min': 5,  'spring_depth': 0.015},
    '15m': {'scan_every_min': 15, 'spring_depth': 0.015},
    '1h':  {'scan_every_min': 60, 'spring_depth': 0.015},
}

RANGE_LEN = 30
CANDLES_TO_FETCH = 200
CHECK_BACK = 3
TEST_BOUNCE_PCT = 0.01
TEST_TOUCH_PCT = 0.015
TEST_WINDOW_BARS = 20
STATE_FILE = 'state.json'
MSK = timezone(timedelta(hours=3))

exchange = ccxt.bybit({'options': {'defaultType': 'spot'}, 'enableRateLimit': True})

# ═══════════ 2. ПАМЯТЬ МЕЖДУ ЗАПУСКАМИ ═══════════
def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('alerted', {}), data.get('last_scan', {}), False
    except Exception:
        return {}, {}, True   # True = первый запуск, состояния нет

def save_state(alerted, last_scan):
    week_ago = time.time() - 7 * 86400
    alerted = {k: v for k, v in alerted.items() if v > week_ago}
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'alerted': alerted, 'last_scan': last_scan}, f)

# ═══════════ 3. ПОЧТА ═══════════
def send_email(subject, body):
    for attempt in range(1, 3):
        try:
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['Subject'] = subject
            msg['From'] = EMAIL_LOGIN
            msg['To'] = EMAIL_TO
            with smtplib.SMTP_SSL('smtp.yandex.ru', 465, timeout=20) as server:
                server.login(EMAIL_LOGIN, EMAIL_APP_PASSWORD.replace(' ', ''))
                server.send_message(msg)
            print("📧 Письмо отправлено:", subject)
            return True
        except Exception as e:
            print(f"❌ Ошибка почты (попытка {attempt}): {e}")
            time.sleep(5)
    return False

# ═══════════ 4. СИГНАЛЫ (формулы прежние) ═══════════
def get_signals(symbol, tf, depth):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=CANDLES_TO_FETCH)
    if not ohlcv or len(ohlcv) < RANGE_LEN + 5:
        return []

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['range_low_1'] = df['low'].shift(1).rolling(window=RANGE_LEN).min()

    spring = (df['low'] < df['range_low_1'] * (1.0 - depth)) & \
             (df['close'] > df['range_low_1'])

    lows = df['low'].tolist()
    highs = df['high'].tolist()
    closes = df['close'].tolist()
    stamps = df['timestamp'].tolist()
    spring_flags = spring.tolist()
    range_lows = df['range_low_1'].tolist()

    spring_low = None
    spring_idx = None
    bounce_seen = False
    test_done = False

    events = []
    n = len(df)
    first_check = n - 1 - CHECK_BACK

    for i in range(n):
        if spring_flags[i]:
            spring_low = lows[i]
            spring_idx = i
            bounce_seen = False
            test_done = False

        if spring_idx is None:
            continue
        bars_after = i - spring_idx

        if bars_after >= 1 and highs[i] >= spring_low * (1.0 + TEST_BOUNCE_PCT):
            bounce_seen = True

        if (not test_done) and (2 <= bars_after <= TEST_WINDOW_BARS) and bounce_seen:
            if (lows[i] >= spring_low * 0.995) and \
               (lows[i] <= spring_low * (1.0 + TEST_TOUCH_PCT)) and \
               (closes[i] > spring_low):
                test_done = True
                if first_check <= i <= n - 2:
                    events.append(('TEST', closes[i], stamps[i], spring_low))

        if spring_flags[i] and first_check <= i <= n - 2:
            events.append(('SPRING', closes[i], stamps[i], range_lows[i]))

    return events

# ═══════════ 5. ТЕКСТЫ ПИСЕМ ═══════════
def make_email(name, symbol, tf, close_price, candle_ts, level):
    candle_time = datetime.fromtimestamp(candle_ts / 1000, tz=MSK).strftime('%d.%m.%Y %H:%M')
    if name == 'SPRING':
        subject = f"🟡 SPRING — {symbol} [{tf}]"
        body = (
            f"СОБЫТИЕ: SPRING (прокол нижней границы диапазона)\n\n"
            f"Пара: {symbol}\n"
            f"Таймфрейм: {tf}\n"
            f"Цена закрытия свечи: {close_price}\n"
            f"Граница диапазона: {level}\n"
            f"Свеча закрылась в: {candle_time} (МСК)\n"
            f"Биржа: Bybit Spot (облако GitHub Actions)\n\n"
            f"Это зона внимания: возможен как SOW, так и начало пружины.\n"
            f"Жди письмо TEST как подтверждение удержания минимума!"
        )
    else:
        subject = f"✅ TEST — {symbol} [{tf}]"
        body = (
            f"СОБЫТИЕ: TEST (минимум спринга УДЕРЖАН)\n\n"
            f"Пара: {symbol}\n"
            f"Таймфрейм: {tf}\n"
            f"Цена закрытия свечи: {close_price}\n"
            f"Минимум спринга (удержан): {level}\n"
            f"Свеча закрылась в: {candle_time} (МСК)\n"
            f"Биржа: Bybit Spot (облако GitHub Actions)\n\n"
            f"Цена отскочила от минимума спринга, вернулась к нему\n"
            f"и закрылась выше — минимум удержан. Подтвержденный сигнал!"
        )
    return subject, body

# ═══════════ 6. ОДИН ПРОХОД ═══════════
def main():
    alerted, last_scan, first_run = load_state()
    now = time.time()

    markets = None
    for attempt in range(1, 4):
        try:
            markets = exchange.load_markets()
            break
        except Exception as e:
            print(f"⚠ Bybit не отвечает (попытка {attempt}): {e}")
            time.sleep(10)
    if markets is None:
        print("❌ Не удалось подключиться к Bybit. Выход (след. запуск через 5 мин).")
        return

    usdt_pairs = [
        s for s, info in markets.items()
        if info['spot'] and info['active'] and s.endswith('/USDT')
    ]
    print(f"✅ Пар под наблюдением: {len(usdt_pairs)}")

    if first_run:
        send_email(
            "✅ Облачный сканер запущен (GitHub Actions)",
            f"Сканер SPRING + TEST теперь работает в облаке.\n"
            f"Пар: {len(usdt_pairs)}\nТаймфреймы: 5m / 15m / 1h.\n"
            f"Ноутбук можно выключать."
        )

    total = 0
    for tf, cfg in TIMEFRAMES.items():
        if now - last_scan.get(tf, 0) < cfg['scan_every_min'] * 60:
            print(f"[{tf}] ещё не время — пропускаю.")
            continue
        print(f"--- Сканирую {tf} | глубина {cfg['spring_depth']} ---")
        for symbol in usdt_pairs:
            try:
                events = get_signals(symbol, tf, cfg['spring_depth'])
            except Exception as e:
                print(f"❌ Ошибка при проверке {symbol} [{tf}]: {e}")
                continue
            for name, close_price, candle_ts, level in events:
                key = f"{symbol}_{tf}_{name}_{candle_ts}"
                if key in alerted:
                    continue
                alerted[key] = now
                total += 1
                subject, body = make_email(name, symbol, tf, close_price, candle_ts, level)
                print(f"🔥 {name} на {symbol} [{tf}] | Цена: {close_price}")
                send_email(subject, body)
        last_scan[tf] = time.time()

    save_state(alerted, last_scan)
    print(f"✅ Проход завершен. Сигналов за запуск: {total}")

if __name__ == '__main__':
    main()
