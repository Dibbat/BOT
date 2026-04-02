# MT5 auto bridge

This folder runs an automatic MT5 bridge driven by the existing PowerTrader signal files.

MetaTrader 5 Python integration is supported on Windows environments where MT5 terminal is installed.

This folder also contains MT5-native training and backtesting tools that do not depend on KuCoin.

## 1) Install dependencies

From the project root:

pip install -r requirements.txt

## 2) Create config

Copy and edit:

- mt5/mt5_config.example.json -> mt5/mt5_config.json

Set your MT5 login/password/server and symbol mapping.

Important safety setting:

- trade_enabled: false means dry-run (no real orders)
- trade_enabled: true means live trading

Backtest safety keys (optional, recommended):

- trade_start_level: minimum long signal level to open (default 3)
- dca_multiplier: additional buy sizing factor during DCA
- max_dca_buys_per_24h: rolling cap on DCA frequency
- max_dca_per_trade: hard DCA stage cap per position
- pm_start_pct_no_dca: trailing start threshold (%) when no DCA was used
- pm_start_pct_with_dca: trailing start threshold (%) after DCA
- trailing_gap_pct: trailing gap (%) once trailing is active
- per_coin_stop_loss_pct: hard per-symbol stop loss (%)

## 3) Run bridge

From project root:

python mt5/pt_mt5_bridge.py --config mt5/mt5_config.json

Bridge includes MT5 auth retry and demo-server safety check controls:

python mt5/pt_mt5_bridge.py --config mt5/mt5_config.json --retries 5 --retry-delay 8 --terminal-warmup 10

One-cycle test mode:

python mt5/pt_mt5_bridge.py --config mt5/mt5_config.json --once

One-cycle test mode with explicit dry-run:

python mt5/pt_mt5_bridge.py --config mt5/mt5_config.json --once --dry-run

## 4) MT5-native training (no KuCoin required)

Train memory patterns directly from MT5 candles:

python mt5/pt_mt5_trainer.py --coin BTC
python mt5/pt_mt5_trainer.py --coin ETH --timeframe 1hour
python mt5/pt_mt5_trainer.py --coin XRP --lookback-days 90

Scalping training examples:

python mt5/pt_mt5_trainer.py --coin BTC --timeframe 5min --lookback-days 14
python mt5/pt_mt5_trainer.py --coin ETH --timeframe 15min --lookback-days 30

This writes/updates:

- memories_1hour.txt
- memory_weights_1hour.txt
- trainer_status.json

For scalping timeframes it writes timeframe-specific files, for example:

- memories_5min.txt
- memory_weights_5min.txt
- neural_perfect_threshold_5min.txt

## 5) Export historical signals for backtesting

Generate per-bar CSV signal history from trained memory:

python mt5/pt_mt5_signal_exporter.py --coin BTC --mt5-symbol BTCUSD
python mt5/pt_mt5_signal_exporter.py --coin ETH --mt5-symbol ETHUSD
python mt5/pt_mt5_signal_exporter.py --coin XRP --mt5-symbol XRPUSD
python mt5/pt_mt5_signal_exporter.py --coin DOGE --mt5-symbol DOGUSD
python mt5/pt_mt5_signal_exporter.py --coin BNB --mt5-symbol BNBUSD

True scalping signal export examples:

python mt5/pt_mt5_signal_exporter.py --coin BTC --mt5-symbol BTCUSD --timeframe 5min --lookback-days 14
python mt5/pt_mt5_signal_exporter.py --coin ETH --mt5-symbol ETHUSD --timeframe 5min --lookback-days 14
python mt5/pt_mt5_signal_exporter.py --coin XRP --mt5-symbol XRPUSD --timeframe 5min --lookback-days 14
python mt5/pt_mt5_signal_exporter.py --coin DOGE --mt5-symbol DOGUSD --timeframe 5min --lookback-days 14
python mt5/pt_mt5_signal_exporter.py --coin BNB --mt5-symbol BNBUSD --timeframe 5min --lookback-days 14

Optional strictness control:

- set neural_perfect_threshold_1hour.txt to a lower value (for example 1.2) to reduce loose matches.
- for scalping, tune neural_perfect_threshold_5min.txt (for example 0.8 to 1.2).

Output files are written to:

- mt5/signal_history/<COIN>_signals.csv

## 6) Run backtest with exported signals

python mt5/pt_mt5_backtest.py --start "2026-01-22 00:00" --end "2026-03-22 23:59" --timeframe H1 --signals-dir mt5/signal_history --confirm-logic --min-win-rate 30 --max-drawdown-pct 15 --max-forced-stops 0 --max-notional-leverage 0.25

Scalping backtest example (M5):

python mt5/pt_mt5_backtest.py --start "2026-03-01 00:00" --end "2026-03-22 23:59" --timeframe M5 --signals-dir mt5/signal_history --confirm-logic --min-win-rate 35 --max-drawdown-pct 8 --max-forced-stops 0 --max-notional-leverage 0.15

Read these key lines in output:

- [RESULT] per symbol stats
- [TOTAL] realized/floating/total pnl
- [AUTO-DECISION] GO or NO-GO
- [CHECK] entry_path, dca_path, trailing_sell_path

## Auto behavior

For each configured coin, the script reads:

- long_dca_signal.txt
- short_dca_signal.txt
- futures_long_profit_margin.txt
- futures_short_profit_margin.txt

Then it will:

- open long when long signal >= open_threshold
- open short when short signal >= open_threshold
- scale in position count by signal level (3 -> 1, 4 -> 2, etc., capped by max_scale_ins)
- close opposite side on opposite signal if enabled
- close side when signal fades below close_threshold
- optionally close by profit margin target from signal files

## Folder mapping

Signals root defaults to project root.

- BTC reads directly from root files
- Other symbols read from subfolders like ETH, DOGE, XRP, BNB when present

This matches the existing bot folder behavior.

## Windows PowerShell notes

- Do not paste markdown links into commands. Use plain paths only.
- Correct:
	Set-Content "neural_perfect_threshold_1hour.txt" "1.2"
- Incorrect:
	Set-Content [neural_perfect_threshold_1hour.txt](...) "1.2"

If JSON property assignment fails with "property cannot be found", add missing keys first (for example with Add-Member), or edit mt5_config.json directly.
