# wallet_bridge.py — Hyperliquid追跡ウォレット → Session Analysis Dashboard 橋渡しツール

Hyperliquidの大口ウォレット(whale_radar等で発掘したアドレス)の約定履歴(fills)を公開API(認証不要)から取得し、
`session_dashboard/app.py` がそのまま読めるトレードCSV(1行=1往復トレード)へ変換する単体スクリプト。

**app.pyは一切変更していません**(このツールは新規ファイルのみ)。app.pyとの連携は「CSVの列名がapp.pyの
列エイリアス(`_ALIASES_RAW`)に一致すること」のみで成立しており、import等の直接依存はありません
(理由: app.pyは862KB・streamlit依存の巨大アプリのため、単体CLIとしての起動性・保守性を優先)。

## 1. 使い方(1行)

```bash
python wallet_bridge.py --days 30 --top 5
```

既定では同ディレクトリの `wallet_bridge_whales.json`(30ウォレット)を読み込み、過去30日分のfillsを取得、
再構築後のcclosedPnl合計が多い上位5ウォレットだけをCSV化する。

### 主なオプション

| オプション | 既定値 | 説明 |
|---|---|---|
| `--addrs 0xA,0xB` | なし | カンマ区切りでアドレスを直接指定(指定時は`--file`より優先) |
| `--file path.json` | `wallet_bridge_whales.json` | `{"whales":[{"addr":...,"tag":...}, ...]}`形式のJSON |
| `--top N` | 10 | closedPnl合計降順で上位N件だけCSV化 |
| `--min-trades N` | 20 | 上位N選定の対象に含める最低トレード数(未満は候補から除外されるが index.json には残る) |
| `--days N` | 365 | 何日分のfillsを遡って取得するか |
| `--out DIR` | `wallet_trades` | 出力ディレクトリ |
| `--selftest` | - | ネットワーク無しで合成データによる検算のみ実行(下記参照) |

## 2. データソース(Hyperliquid公開API・認証不要)

```
POST https://api.hyperliquid.xyz/info
body: {"type":"userFillsByTime","user":<addr>,"startTime":<ms>,"endTime":<ms>}
```

- 1回のリクエストで最大2000件までしか返らない。
- 呼び出し間隔は `time.sleep(0.35)` でレートに配慮。
- 1ウォレットの取得失敗(HTTPエラー・JSON異常等)は警告を出して次のウォレットへスキップする
  (ツール全体は止めない)。

### ⚠️ ページング方向についての重要な訂正(実機検証済み・2026-07-22)

設計段階では「1回最大2000件→`endTime`を最古fillの`time-1`にして過去方向へ遡ってページングする」
という想定だったが、**実際にHyperliquid公開APIを叩いて検証した結果、この想定は実挙動と一致しないことを
確認した**(CLAUDE.md 掟8「推測で進めない・実物確認」に基づき、コードを書く前に実APIで裏取りした)。

実際の`userFillsByTime`は、指定した`startTime`以降のfillsを **古い→新しい昇順** で返し、上限2000件に
達した場合は「もっと新しいfillsがまだ残っている」ことを意味する。したがって本ツールは

```
次のstartTime = 直前ページで返ってきた最新fillのtime + 1  (endTimeは固定のまま)
```

という **前方(未来方向)ページング** を実装している。これは実ウォレット3件・複数レンジで実測し、
返却件数・時系列の連続性を確認済み(下記smoke test参照)。もし将来API仕様が変わった場合は
`fetch_user_fills_by_time()` のロジックを再検証すること。

## 3. 往復トレード再構築ロジック

BingXブローカー明細の再構築(`app.py reconstruct_bingx_order_history`)と同じ考え方の
ポジション会計を採用している(参照実装として一致させた)。

- `(coin, Long/Short)` 単位でポジションを管理する。
- `dir` フィールドが `"Open Long"` / `"Open Short"` のfillは、数量・加重平均建値を集約する。
  数量がゼロの状態から最初のOpenが来たときだけ建玉時刻をリセットする(以降の追加Openは時刻を更新しない)。
- `dir` が `"Close Long"` / `"Close Short"` のfillが来るたびに **1トレードを発行** する
  (部分決済は複数トレードになる=各実現イベントが1トレード)。
  - `Entry_Time(JST)` / `Exit_Time(JST)`: ms(UTC) → JST(+9h)、`%Y-%m-%d %H:%M:%S`
  - `Entry_Price` = 集約された加重平均建値、`Exit_Price` = 当該Closeの約定価格(`px`)
  - `Quantity` = `min(sz, 残玉数量)`
  - `PnL_USD` = `closedPnl`(**手数料[fee]は含まない**。Hyperliquidの`closedPnl`は手数料控除前の実現損益)
  - `PnL_Percent` = `(Exit_Price/Entry_Price - 1) * 100 * (LONGなら+1/SHORTなら-1)`
    (レバレッジを考慮しない「価格変動%」。ダッシュボードの既存流儀=BingX再構築と同一の定義)
  - `Win_Loss` = `closedPnl > 0` → `win` / `< 0` → `loss` / `== 0` → `draw`
- **建玉前クローズ(orphan)は出力しない**: このツールの取得ウィンドウ(`--days`)より前に建てられた
  ポジションのClose fillは、対応するOpenが手元に無いため、エントリー情報の無い行として出力せず
  スキップし、件数を記録する(`skipped_orphans`)。**BingX再構築と異なり、orphan行はCSVに含まれない**
  (仕様上「エントリー情報なし行として出力せず」と明記されているため)。
- `dir` が無い(欠落・空)fillはスキップし `skipped_missing_dir` へ計上する
  (`side`+`startPosition`からの推定はしない=仕様どおり)。
- `dir` が上記4パターンのいずれでもないfill(フリップ`"Long > Short"`/`"Short > Long"`、現物の
  `"Buy"`/`"Sell"`、`"Settlement"`、`"Spot Dust Conversion"`等)はスキップし
  `skipped_unrecognized_dir` へ計上する。**現状、フリップ(ドテン)1発注での両建て解消+新規建ては
  未対応**(仕様がOpen系/Close系の2分類のみを前提としているため。フリップを正しく扱うには別途
  「Close+Open同時発生」の特別処理が必要=既知の残課題)。

### 実機での挙動: orphan(建玉前クローズ)が多発するケースについて

`--days`の窓より前から保有され続けているスイング/HODLポジションを、窓の内側で少しずつ決済している
ウォレットでは、**ほとんどのClose fillがorphanとしてスキップされる**(対応するOpenが窓の外側=それより
前にあるため)。実機smoke testでもこの挙動を確認済み(下記参照)。`index.json`の`skipped_orphans`を
必ず確認し、値が大きい場合は「このウォレットは長期保有中心で、指定した`--days`では建玉時期を
捕捉しきれていない」というデータ完全性の注意信号として扱うこと。長期保有ウォレットを正しく
捕捉したい場合は`--days`を大きくする(建玉時期まで遡れる日数にする)。

### トレーダータイプ分類

`(Exit_Time - Entry_Time)`の中央値保有時間で分類する。閾値は
`app.py` L5571 `TRADER_STYLE_THRESHOLDS_H = (0.5, 24.0)`(2026-07-22時点でRead確認)と同値を
本スクリプト内に複製している(単体実行性を優先し、862KB・streamlit依存のapp.pyはimportしない方針のため。
app.py側で閾値が変更された場合はこのファイルの`TRADER_STYLE_THRESHOLDS_H`も手動で追随させること)。

- 中央値保有時間 < 0.5h → スキャルピング
- 0.5h 〜 24h → デイトレード
- \> 24h → スイング

## 4. 出力

`--out`ディレクトリ(既定`wallet_trades/`)に以下を生成する。

- `<tag>_<addr先頭8文字>.csv`(utf-8-sig・Excel対応): `--top N`で選ばれたウォレットのみ。
  列順は `Entry_Time(JST),Exit_Time(JST),Symbol,PnL_USD,PnL_Percent,Win_Loss,Side,Leverage,Entry_Price,Exit_Price,Quantity`。
  `Leverage`は常に空欄(HLのfillsにはレバレッジ情報が含まれないため不明。ダッシュボード側は
  この列を任意列として扱えるため空欄でも読み込みに支障はない=app.py確認済み)。
- `index.json`: **fillsを取得できた全ウォレット**(`--min-trades`未満で候補外になったものも含む)の
  サマリーを保持する。各要素: `addr, tag, n_trades, total_pnl, win_rate, median_hold_h, style,
  period_start, period_end, skipped_orphans`(+透明性のための追加フィールド
  `skipped_missing_dir, skipped_unrecognized_dir, skipped_bad_fields, n_fills_fetched`)。
- 標準出力に実行サマリ表(集計期間を明記。CLAUDE.md 掟6)。

`--top N`は「fills取得後、直近`--days`日間の合計closedPnl降順」で決める。`--min-trades`未満のウォレットは
この順位付けの対象から除外される(CSV化されない)が、`index.json`には残る。

## 5. 検証

### 5.1 selftest(ネットワーク不使用・合成データ)

```bash
python wallet_bridge.py --selftest
```

以下を合成fillsで検算し、`SELFTEST: ALL PASS`が出れば正常(exit code 0)。

1. Long: Open 2回(加重平均建値)→部分Close→全Close(2トレード発行・Entry時刻据え置き確認)
2. Short対称(価格下落でPnL_Percentが正になる符号反転の確認)
3. orphan Close(出力されずスキップ・件数記録)/ dir欠落 / 未対応dir(フリップ・現物buy)のスキップ分類
4. ページング境界(前方ページング。合成post_fnを注入し、2000件フルページ→300件で終了、
   2回目の`startTime`が1回目最新fillの`time+1`になっていること等をネットワーク無しで検証)
5. 生成CSV相当の列名がapp.pyの列エイリアス(`_ALIASES_RAW`)へ全て正規化できること
6. サマリー算出(トレーダータイプ分類の境界値)

**実行結果(2026-07-22実施)**: `26/26 PASS` → `SELFTEST: ALL PASS`(exit code 0)。
(Git Bash端末では日本語がコンソールのコードページ都合で文字化けして見える場合があるが、
これは表示上の問題であり判定結果には影響しない。PowerShellでは正しく表示され、実行結果も一致した。)

### 5.2 実liveスモークテスト(実際のHyperliquid公開API)

```bash
python wallet_bridge.py --addrs 0x8def9f50456c6c4e37fa5d3d57f108ed23992dae --days 30 --top 1 --min-trades 1 --out wallet_trades_smoke
```

**実行結果(2026-07-22実施)**:

```
集計期間: 2026-06-22 09:02:36 〜 2026-07-22 09:02:36 JST(過去30日)
n_fills_fetched=12592件 → n_trades=1件(total_pnl=24.43・win_rate=100%・median_hold_h=3.92・スタイル=デイトレード)
skipped_orphans=5976(内訳の大半はxyz:TSLAの長期Shortポジションを窓内で分割決済している影響。
                       上記「orphanが多発するケース」参照。バグではなく想定どおりの挙動)
skipped_unrecognized_dir=3(現物Buy1件・Settlement1件・Spot Dust Conversion1件)
CSV: wallet_trades_smoke/manual_0x8def9f.csv (1行)
```

別ウォレット(`0x8af700ba841f30e0a3fcb0ee4c4a9d223e1efa05`, tag=directional, `--days 14`)でも
実行し、より一般的な「多数の完結トレードが窓内に収まるケース」を確認済み:
`n_fills_fetched`多数 → `n_trades=2075`件・`total_pnl=192054.17`・`win_rate=87.0%`・
`median_hold_h=127.44`(スタイル=スイング)・`skipped_orphans=528`。生成CSVをpandasで
読み戻し、`PnL_USD`合計が一致することも確認済み。

### 5.3 ダッシュボード列エイリアス適合の自己検算

`wallet_bridge.py`内に app.py L380-406 `_ALIASES_RAW` の該当部分(本ツールが出力する列のみ)を
手動転記した`_ALIASES_RAW_SUBSET`と、L547-551 `_norm_colname_key`と同一の正規化関数を複製している
(import不使用のため)。CSV書き出し後、実際に`pandas.read_csv`で読み戻した列名に対して
`verify_dashboard_column_compat()`で自己検算し、通常実行時にもログへ`[OK]`/`[WARN]`を出力する。
selftestの項目5でも同じ検算を行っている。

**保守上の注意**: `_ALIASES_RAW_SUBSET`はapp.pyの`_ALIASES_RAW`の手動コピーであるため、
app.py側でエイリアスが変更された場合はこのファイルも追随して更新する必要がある
(import しない設計上のトレードオフ)。

## 6. 既知の制約・残リスク

1. **フリップ(ドテン)1発注は未対応**: `dir="Long > Short"`等、1回のfillで既存ポジションの
   クローズと新規反対方向ポジションのオープンが同時に起きるケースは、現状
   `skipped_unrecognized_dir`としてスキップするのみで、トレードとしても新規建玉としても
   処理していない。高頻度フリップを多用するウォレットでは統計が過小評価される。
2. **`--days`窓より前に建てられたポジションのCloseはorphanとして捨てられる**(上記5.2参照)。
   長期保有型ウォレットを正確に捕捉するには`--days`を十分大きくする必要がある。
3. **Leverageは常に不明(空欄)**: Hyperliquidのfillsレスポンスにはレバレッジ情報が含まれないため。
4. **手数料(fee)は含まない**: `PnL_USD`はHLの`closedPnl`(手数料控除前)をそのまま使用している。
5. **ページング安全上限**: 1ウォレットあたり最大2000ページ(`DEFAULT_MAX_PAGES`)で強制打ち切り。
   超高頻度ウォレット×長期間(`--days`大)の組み合わせでは理論上到達し得るが、smoke testでは
   30日で7ページ程度だったため実務上は十分な余裕がある。
6. **レート制御は逐次・単純**: 429等のレート制限応答時のリトライ/バックオフは未実装
   (発生時はその呼び出しが例外化し、該当ウォレットがスキップされるのみ)。

## 7. 依存関係

`stdlib` + `requests` + `pandas` のみ(検証環境: Python 3.11.9 / requests 2.31.0 / pandas 3.0.1)。
秘密情報(APIキー等)は一切使用しない(Hyperliquid公開API・認証不要)。
