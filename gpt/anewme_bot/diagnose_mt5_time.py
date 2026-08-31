"""Read-only time evidence from an existing terminal; no orders, AI or Telegram."""
import argparse
import json
import time
from datetime import datetime, timezone
import MetaTrader5 as mt5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--terminal', required=True)
    args = parser.parse_args()
    if not mt5.initialize(args.terminal, timeout=5000):
        raise RuntimeError(str(mt5.last_error()))
    try:
        terminal = mt5.terminal_info()
        print(json.dumps({'connected': bool(terminal and terminal.connected)}))
        for sample in range(3):
            now = datetime.now(timezone.utc)
            for symbol in ('EURUSD', 'XAUUSD'):
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    print(json.dumps({'symbol': symbol, 'tick': None}))
                    continue
                raw = tick.time_msc / 1000 if tick.time_msc else tick.time
                record = {'sample': sample, 'symbol': symbol, 'system_utc': now.isoformat(),
                          'raw_tick': raw, 'tick_as_utc': datetime.fromtimestamp(raw, timezone.utc).isoformat(),
                          'raw_minus_system_seconds': round(raw-now.timestamp(), 3)}
                if sample == 0:
                    record['candles'] = {}
                    for name, tf in [('M5', mt5.TIMEFRAME_M5), ('M15', mt5.TIMEFRAME_M15), ('H1', mt5.TIMEFRAME_H1)]:
                        bars = mt5.copy_rates_from_pos(symbol, tf, 0, 3)
                        record['candles'][name] = [] if bars is None else [
                            {'raw_open': int(b['time']), 'open_as_utc': datetime.fromtimestamp(int(b['time']), timezone.utc).isoformat(),
                             'close': float(b['close']), 'shift': len(bars)-1-i} for i,b in enumerate(bars)]
                print(json.dumps(record))
            if sample < 2:
                time.sleep(1)
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    main()
