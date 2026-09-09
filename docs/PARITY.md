# バックテスト再現(トレード単位の照合)の仕組みと限界

## 何をしているか

`backtest/replay.py` に照合が 2 段階ある。

### ① シグナル単位(entry)— `replay.run()` / mode='signal'

```
OHLC → strategies.generate_all_signals()  … 4手法の entry をウォークフォワード再現
     → 「期待シグナル」[(ts, pair, strategy, side), ...]
     ↕  (pair, strategy) 一致 & 時刻 ±90分 でマッチング
     → 実運用の signal_events(event_type='signal')
     ⇒ MATCHED / MISMATCH(方向逆) / MISSING(期待だけ) / EXTRA(実運用だけ)
```

- **MISSING** = バックテストなら出したはずのシグナルを実運用が出していない = 取り逃し
- **EXTRA**   = バックテストにないシグナルを実運用が出している = 想定外の発注
- **MISMATCH**= 同じ時刻・ペアで方向(LONG/SHORT)が逆
- 一致率 = MATCHED / 期待件数

### ② トレード・損益単位(exit まで再現)— `replay.run_trades()` / mode='trade'

```
期待シグナル → exits.simulate_trades()  … SL/TP/トレールを OHLC で 1 本ずつ進めて手仕舞い
            → 「バックテストなら成立したトレード」[entry/exit/R損益/決済理由/保有本数]
            ↕  (pair, strategy) 一致 & entry 時刻 ±2h でマッチング
            → 実運用の trades(status='closed')
            ⇒ MATCHED(勝敗一致/不一致・決済理由一致・R比較) / MISSING / EXTRA
```

- **カバレッジ** = MATCHED / シミュレート件数 … バックテスト想定のうち実際に取れた割合
  (低い = 証拠金不足 / `max_positions` 等で見送っている。①の実行率チェックと連動)
- **勝敗一致率** = 勝敗が一致した MATCHED / MATCHED … 取れたトレードが想定どおりの結果になったか
- **平均R sim vs 実** … 実運用がバックテストの取り分をどれだけ実現できているか
- 比較対象は実運用データが存在する期間に自動で絞る(OHLC は warmup 用に長く渡してよい)

## 使い方

```bash
# ★推奨: 本番ボットと同じ Yahoo Finance の 1時間足で照合(要 pip install yfinance)
#   --mode both(既定)= ①シグナル + ②トレード・損益 の両方を parity_runs に書く
python -m backtest.replay --yfinance --start 2026-07-10 --end 2026-09-07
python -m backtest.replay --yfinance --mode trade --start ... --end ...   # ②だけ

# 参考: バックテストに使った 1時間足 = fx_cache/(HistData.com 由来。ボットの実フィードではない)
python -m backtest.replay --fx-cache ../fx_cache --start 2026-03-01 --end 2026-06-05

# 単一 CSV(このリポジトリ同梱のスライス)/ 合成 OHLC(デモ)
python -m backtest.replay --ohlc backtest/sample_ohlc.csv
python -m backtest.replay --demo
```

OHLC の CSV は 2 形式に対応(列名の大小・順序は自動判別):
- `ts,pair,open,high,low,close`(このリポジトリの標準)
- `time,Open,High,Low,Close,Volume`(`fx_cache/USD_JPY.csv` 等。1ファイル1ペア、ペア名はファイル名から)

## 限界(正直に)

### 1. 戦略は「参照実装(サンプルパラメータ)」であって監視対象ボットのコードそのものではない

`backtest/strategies.py` の 4 手法は**一般的な教科書パラメータに置き換えたサンプル**で、
監視対象ボットで実際に使っているチューニング済みの値ではない(このリポジトリは公開ポートフォリオ)。
また、監視対象ボットのシグナル判定関数をそのまま呼んでいるわけでもない
(そのコードは yfinance 取得と取引所 API 呼び出しに密結合しているため)。

→ **厳密なトレード単位一致**を取るには、監視対象ボットのシグナル判定部を副作用のない純粋関数として
共有モジュール(例: `strategy_core.py`)に切り出し、**ボットとこのリプレイの両方が import** する
構成にする。そこまでやって初めて「差分 = 実行環境の問題(データ遅延・二重起動・証拠金)」と
言い切れる。現状は「参照実装との差分」なので、参照実装自体のズレも差分に含まれる。

### 2. 価格フィードが3つ絡む(重要)

このシステムには**別々の価格系列が3つ**ある:

| 用途 | フィード | 根拠 |
|---|---|---|
| **本番ボットのシグナル判定** | **Yahoo Finance の 1時間足** | `監視対象ボット` の `_download_ohlcv()` = `yf.download(interval='1h', period='60d')`。ペアは `GMO_TO_YF`(USD_JPY→`JPY=X` 等) |
| 本番ボットの約定価格 | GMO ticker(`forex-api.coin.z.com/public/v1/ticker`) | 発注時の entry price / JPY 換算にのみ使用 |
| バックテスト | HistData.com(`DAT_ASCII_<PAIR>_M1` を1h化)= `fx_cache/*.csv` | zip とスクリプトの `DATA_DIR='fx_cache'` |

→ **parity を正しく取るには、リプレイもボットと同じ Yahoo Finance の1時間足を使う**。
`python -m backtest.replay --yfinance --start ... --end ...`(`backtest/ohlc.py::load_yfinance`、
ボットと同一の `GMO_TO_YF` マップを使用)。`--fx-cache`(HistData)や `--demo` はフィードが違うため、
一致率にフィード差ぶんのノイズが乗る。

**残る誤差要因**(Yahoo で揃えても消えないもの):
- Yahoo Finance は過去バーを事後改定することがある(`auto_adjust=True`)。ボットが取得した瞬間の値と、
  後日リプレイで取得する値が完全一致する保証はない
- Yahoo の hourly は直近 ~730 日のみ。深い期間は照合できない
- 約定価格・スリッページは GMO 由来なので、PnL レベルの照合には依然 GMO が絡む

### 3. エグジットは近似モデル(トレード単位のみ)

`backtest/exits.py` の SL/TP/トレールは**仕様からの近似**:
- SL = 直近スイング(10本)± ATR×0.2、最低 0.5ATR
- TP = SB/BB_SQ は 2R、MACD は 1.618R、DM_PSAR はトレール(1R 起動・1R 刻み・8R セーフティ)
- 約定は「シグナル足の次バー始値」、スリッページ 0.5pip を entry/exit で控除

本番の SL/TP は各手法のシグナル関数内で BB 幅・PSAR・ATR から計算されるため、こことは
細部が異なる。トレード単位の一致率にはこの近似ぶんの誤差も乗る。

## デモでの見え方

`python -m scripts.seed_demo_data` は合成 OHLC → 参照戦略で期待シグナル + `exits.simulate_trades` で
想定トレードを作り、実運用側は「その約 9% を検出漏れ・数% 方向反転・少量 EXTRA、成立分は
勝敗の一部が反転・R を目減り」させて `signal_events` / `trades` を生成する。結果:

- ① シグナル単位: 一致率 **~90%**、MISSING/EXTRA 少量
- ② トレード単位: カバレッジ **~35%**(証拠金不足で大量に見送り)、勝敗一致率 **~70%**、
  平均R sim ~1.0 vs 実 ~0.4(バックテストの取り分の半分も実現できていない、という状態を再現)

## 実ログの決済損益を補う(実装済み: `ingest/gmo_history.py`)

`fxbot.log` は決済(GMO 側 OCO / SL)の価格・損益を記録しないため、KPI・勝率・PF・
エクイティカーブ・②トレード損益は実ログだけでは埋まらない。GMO の約定履歴で補完する:

- `python -m ingest.gmo_history --api` … GMO Private API `latestExecutions`(直近約1ヶ月)。
  `GMO_API_KEY` / `GMO_API_SECRET` を `.env` に。署名は監視対象ボットと同じ HMAC-SHA256、依存は標準ライブラリのみ
- `python -m ingest.gmo_history --csv <path>` … 取引ツールからエクスポートした約定履歴 CSV(英語/日本語ヘッダ両対応)。API の1ヶ月より前の期間はこちら

`positionId` で `trades` と突合し、`settleType=CLOSE` の `lossGain` を `pnl_jpy`、`price` を
`exit_price` に。`OPEN` は `entry_price` が未設定の trade を補完する。

## 次の一手(exact parity)

1. `監視対象ボット` からシグナル判定**と SL/TP/エグジット計算**を純粋関数として抽出 → `strategy_core.py`
2. 本番ボットもこのリプレイも `strategy_core` を import(単一の真実)。①も②もこれで exact になる
3. ボットが毎サイクル取得した Yahoo Finance の DataFrame を**そのまま保存**しておき
   (取得時点のスナップショット)、リプレイはそれを読む。事後 `yf.download` の改定差を無くせる
4. リプレイを CI or systemd タイマーで定期実行し、parity 低下を Slack 通知
