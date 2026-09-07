"""バックテスト再現エンジン(トレード単位の照合)。

実運用が見たのと同じ OHLC を、バックテストの戦略ロジックに通して
「バックテストなら出したはずのシグナル」を再現し、実運用の signal_events と 1 件ずつ突き合わせる。

- `indicators.py` … EMA / MACD / Bollinger / ATR / PSAR 等(ベクトル化)
- `strategies.py` … 4 手法のシグナル生成(仕様から起こした参照実装。厳密な一致は PARITY.md 参照)
- `ohlc.py`       … OHLC CSV ローダ + デモ生成
- `replay.py`     … ウォークフォワード再現 + 実運用シグナルとの差分 → parity_runs へ保存
"""
