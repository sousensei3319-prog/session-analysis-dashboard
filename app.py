# -*- coding: utf-8 -*-
"""
Session Analysis Dashboard
===========================

時間帯(セッション)別のトレード成績・値動き傾向を可視化するStreamlitダッシュボード。

■ インストール:
    pip install streamlit yfinance ccxt plotly pandas numpy kaleido

■ 起動:
    streamlit run session_dashboard/app.py

■ セルフテスト (ネットワーク不要・純ロジック検証):
    python session_dashboard/app.py --selftest

■ 環境:
    Windows 11 / Python 3.11.9
    streamlit 1.59.1 / plotly 6.9.0 / kaleido 1.3.0 / ccxt 4.5.52 / yfinance 1.4.0
    pandas 3.0.1 / numpy 2.4.6

■ 注意:
    - パスに日本語(デスクトップ)を含むため、ファイルI/Oは必ず pathlib.Path(__file__).parent 基準・encoding明示。
    - 発注・取引所への書き込みAPIは一切実装しない(閲覧・分析専用、公開エンドポイントのみ)。
    - PNGダウンロードにはGoogle ChromeまたはMicrosoft Edgeが必要(kaleidoが自動検出)。
      無ければHTMLダウンロードに自動フォールバックする。
"""

from __future__ import annotations

import sys
import io
import os
import hmac
import math
import re
import statistics
import traceback
import inspect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# streamlit / plotly は import のみなら --selftest 環境でも安全(st.*呼び出しはmain/UI関数内に限定する)
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# Windows既定コンソール(cp932)だとrun_selftest()やprintの日本語出力が文字化けするため、
# 可能な環境ではUTF-8出力を明示する(失敗しても致命的ではないため広く例外を握りつぶす)。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

# =====================================================================================
# 定数: セッション定義・銘柄マスタ・カラーパレット・特徴/Tipsテキスト
# =====================================================================================

# ---- §2.1 詳細9帯と一意hourマップ(集計の正本) ----------------------------------------
# 表示ラベル(参考構造の表記) -> 集計対象hour(JST)のタプル -> 親(大枠セッション)
BAND_HOURS: dict[str, tuple[int, ...]] = {
    "アジア早朝 (7-10)": (7, 8, 9),
    "アジア本番 (10-14)": (10, 11, 12, 13),
    "アジア後半 (14-17)": (14, 15),
    "ロンドンオープン (16-18)": (16, 17),
    "ロンドン中盤 (18-21)": (18, 19, 20),
    "NY重複 (21-1)": (21, 22, 23, 0),
    "NY中盤 (1-4)": (1, 2, 3),
    "NY後半 (4-6)": (4, 5),
    "薄商い (6-7)": (6,),
}

BAND_ORDER: list[str] = list(BAND_HOURS.keys())

BAND_TO_PARENT: dict[str, str] = {
    "アジア早朝 (7-10)": "アジア時間",
    "アジア本番 (10-14)": "アジア時間",
    "アジア後半 (14-17)": "アジア時間",
    "ロンドンオープン (16-18)": "ロンドン時間",
    "ロンドン中盤 (18-21)": "ロンドン時間",
    "NY重複 (21-1)": "NY時間",
    "NY中盤 (1-4)": "NY時間",
    "NY後半 (4-6)": "NY時間",
    "薄商い (6-7)": "薄商いゾーン",
}

PARENT_ORDER: list[str] = ["アジア時間", "ロンドン時間", "NY時間", "薄商いゾーン"]

# 大枠行のラベル(詳細帯と区別するためのサフィックス表記)
PARENT_ROW_LABEL: dict[str, str] = {p: f"{p}(大枠計)" for p in PARENT_ORDER}

# ---- 追補§1/§2: 曜日別・月別アノマリー用の定数(JST基準) ----------------------------
# pd.Timestamp.weekday()/dayofweek: 月曜=0 ... 日曜=6
WEEKDAY_LABELS: list[str] = ["月", "火", "水", "木", "金", "土", "日"]
MONTH_ORDER: list[int] = list(range(1, 13))

# hour(0-23) -> 詳細帯ラベル。24時間を漏れなく・重複なくカバーする(モジュール読込時に自己検証)。
HOUR_TO_BAND: dict[int, str] = {}
for _band, _hours in BAND_HOURS.items():
    for _h in _hours:
        HOUR_TO_BAND[_h] = _band
assert sorted(HOUR_TO_BAND.keys()) == list(range(24)), "hourマップが24時間を一意にカバーしていない"

# ---- §2.2 背景帯用の大枠色(チャート背景。詳細9帯とは別の簡易4分割) --------------------
# 6時=薄商い(グレー)、7-16時=アジア(青)、16-21時=ロンドン(緑)、21-翌4時+4-6時=NY(赤)
BG_HOUR_GROUP: dict[int, str] = {}
for _h in range(24):
    if _h == 6:
        BG_HOUR_GROUP[_h] = "thin"
    elif 7 <= _h <= 15:
        BG_HOUR_GROUP[_h] = "asia"
    elif 16 <= _h <= 20:
        BG_HOUR_GROUP[_h] = "london"
    else:  # 21,22,23,0,1,2,3,4,5
        BG_HOUR_GROUP[_h] = "ny"
assert sorted(BG_HOUR_GROUP.keys()) == list(range(24))

BG_GROUP_COLOR_RGB: dict[str, tuple[int, int, int]] = {
    "thin": (150, 150, 150),
    "asia": (70, 130, 230),
    "london": (60, 180, 100),
    "ny": (230, 80, 80),
}
BG_GROUP_LABEL: dict[str, str] = {
    "thin": "薄商いゾーン",
    "asia": "アジア時間",
    "london": "ロンドン時間",
    "ny": "NY時間",
}
BG_OPACITY = 0.10  # 関数デフォルト値・selftest用(UIでは追補v3§3のスライダー値/100で上書きされる)

# ---- 追補v3 §4: JST基準タイムゾーン・キャッシュTTL(データ取得元キャプション表示に使用) --------
JST = timezone(timedelta(hours=9))
HOURLY_CACHE_TTL_SEC = 3600  # fetch_ccxt_ohlcv/fetch_yfinance_ohlcv(1h/1d集計用キャッシュ。v6.1: 900→3600=公開URLの体感優先・振り返り分析は直近1hの鮮度差の影響が小さい)
# ⚠️persist="disk"は使わない: Streamlitはpersist+ttl併用時にTTLを無視する(公式実装で
# 「TTL will be ignored」警告・ディスク側に鮮度チェックなし)。当日を含む既定期間では
# データが最大24h凍結し表示のTTL表記と矛盾するため、v6.1で一度入れて敵対レビューで検出→撤回。
DAILY_CACHE_TTL_SEC = 10800  # fetch_daily_bundle(月別アノマリー用の長期日足キャッシュ。v6.1: 3600→10800)
HOURLY_CACHE_TTL_MIN = HOURLY_CACHE_TTL_SEC // 60
DAILY_CACHE_TTL_MIN = DAILY_CACHE_TTL_SEC // 60

# ---- 追補v4§2.4A: 分足(チャート表示専用)のキャッシュTTL ------------------------------
MINUTE_CACHE_TTL_SEC = 900   # 1分足(ccxt暗号専用)の短期キャッシュ(v6.1: 300→900)
FINE_CACHE_TTL_SEC = 1800    # 5分足/15分足の短期キャッシュ(v6.1: 600→1800)
MINUTE_CACHE_TTL_MIN = MINUTE_CACHE_TTL_SEC // 60
FINE_CACHE_TTL_MIN = FINE_CACHE_TTL_SEC // 60

# v6.3: チャート描画の自動間引き上限(1行あたりの最大バー数)。分足×長期間などで
# 数千〜数万本をSVG描画するとホバーが「かくかく」する(クライアント側の負荷)ため、
# 超過時は表示専用に粗い足へ自動リサンプルする。集計・クリック詳細・ズームは影響なし。
CHART_MAX_BARS_PER_ROW = 3000

# ---- 銘柄マスタ(研究班R1実証結果に基づく。推測でティッカーを変えない) -----------------
SYMBOL_MASTER: dict[str, dict[str, Any]] = {
    "BTC": {"source": "ccxt", "exchange": "binance", "ticker": "BTC/USDT"},
    "ETH": {"source": "ccxt", "exchange": "binance", "ticker": "ETH/USDT"},
    "SOL": {"source": "ccxt", "exchange": "binance", "ticker": "SOL/USDT"},
    "HYPE": {"source": "ccxt", "exchange": "bybit", "ticker": "HYPE/USDT"},
    "GOLD": {"source": "yfinance", "ticker": "GC=F"},
    "NQ": {"source": "yfinance", "ticker": "NQ=F"},
    "SP500": {"source": "yfinance", "ticker": "ES=F"},
    "日経225": {"source": "yfinance", "ticker": "NIY=F"},
}
DEFAULT_SYMBOL_CHECKED: dict[str, bool] = {label: (label == "BTC") for label in SYMBOL_MASTER}

# ---- 銘柄カラーパレット(チャート重畳表示用。同系色濃淡でincreasing/decreasingを色分け) --
SYMBOL_COLORS: dict[str, dict[str, str]] = {
    "BTC": {"inc": "#F7931A", "dec": "#8A5209"},
    "ETH": {"inc": "#A78BFA", "dec": "#5B3FA0"},
    "SOL": {"inc": "#14F1A6", "dec": "#0B7F5C"},
    "HYPE": {"inc": "#FF6FA5", "dec": "#B23A6C"},
    "GOLD": {"inc": "#FFD700", "dec": "#A88A00"},
    "NQ": {"inc": "#5DA9FF", "dec": "#2A5C99"},
    "SP500": {"inc": "#4DD9E8", "dec": "#1F7B85"},
    "日経225": {"inc": "#FF6B6B", "dec": "#A83A3A"},
}
CUSTOM_COLOR_CYCLE: list[dict[str, str]] = [
    {"inc": "#C4E86A", "dec": "#6F8A2E"},
    {"inc": "#E8A87C", "dec": "#8A5A3A"},
    {"inc": "#B0A8FF", "dec": "#5A4FA0"},
    {"inc": "#7CE8D3", "dec": "#3A8A7A"},
    {"inc": "#FFB0D9", "dec": "#A05A7A"},
]


def get_symbol_colors(label: str, custom_index: int = 0) -> dict[str, str]:
    """銘柄ラベルに対応するチャート色(increasing/decreasing)を返す。"""
    if label in SYMBOL_COLORS:
        return SYMBOL_COLORS[label]
    return CUSTOM_COLOR_CYCLE[custom_index % len(CUSTOM_COLOR_CYCLE)]


# ---- 追補v4§2.2: トレードマーカー(データセット別の輪郭色。弟子=水色系/師匠=金色系、以降は循環) --------
TRADE_MARKER_DEFAULT_COLOR: dict[str, str] = {"弟子": "#5DE0E6", "師匠": "#FFD24C"}
TRADE_MARKER_COLOR_CYCLE: list[str] = [
    "#5DE0E6", "#FFD24C", "#FF8AD8", "#9DFF7A", "#C9A0FF", "#FF9F5D",
]
# エントリー→決済の接続点線の色(勝敗別)。win_lossがNone(不明)の場合はdrawと同じグレーを使う。
WIN_LOSS_LINE_COLOR: dict[Optional[str], str] = {"win": "#2ECC71", "loss": "#E74C3C", "draw": "#AAAAAA", None: "#AAAAAA"}


def get_trade_marker_color(label: str, index: int) -> str:
    """追補v4§2.2: トレードマーカーのデータセット別輪郭色を返す(既知ラベルは固定色、それ以外は循環)。"""
    if label in TRADE_MARKER_DEFAULT_COLOR:
        return TRADE_MARKER_DEFAULT_COLOR[label]
    return TRADE_MARKER_COLOR_CYCLE[index % len(TRADE_MARKER_COLOR_CYCLE)]


def leverage_marker_size(leverage: Any) -> float:
    """追補v4§2.2: レバレッジ段階に応じたマーカーサイズ(1x前後=小 〜 25x+=大)。欠落時は中間値。"""
    if leverage is None or (isinstance(leverage, float) and pd.isna(leverage)) or pd.isna(leverage):
        return 10.0
    lev = float(leverage)
    if lev <= 2:
        return 7.0
    if lev <= 5:
        return 9.0
    if lev <= 10:
        return 12.0
    if lev <= 20:
        return 15.0
    return 18.0


# ---- 動きの特徴/Tips(帯ごとの静的テキスト。一般的な市場傾向の参考情報) -----------------
FOOTNOTE_GENERAL = "※一般的傾向の参考情報"
LOW_WINRATE_WARNING = " ⚠️ あなたはこの時間帯で負けやすい"
LOW_WINRATE_THRESHOLD = 45.0

BAND_FEATURE_TIPS: dict[str, dict[str, str]] = {
    "アジア早朝 (7-10)": {
        "feature": "東京勢の参入で値動きが穏やかに始まる。仲値(9:55)公示前後にドル円で断続的なフローが出やすい。",
        "tips": f"仲値公示(9:55)前後はイレギュラーな値動きに注意。{FOOTNOTE_GENERAL}",
    },
    "アジア本番 (10-14)": {
        "feature": "東京勢主体で方向感が出づらくレンジ相場になりやすい。中国・香港市場の動向が波及することがある。",
        "tips": f"レンジ想定のロジックが機能しやすいが、指標発表時は要警戒。{FOOTNOTE_GENERAL}",
    },
    "アジア後半 (14-17)": {
        "feature": "東京勢が徐々に手仕舞いに向かい商いが細る過渡期。欧州勢の参入準備で様子見ムードが強まる。",
        "tips": f"方向感に乏しく、ダマシのブレイクに注意。{FOOTNOTE_GENERAL}",
    },
    "ロンドンオープン (16-18)": {
        "feature": "欧州勢の本格参入で出来高が急増し、その日のトレンド方向が出やすい時間帯。",
        "tips": f"初動に飛び乗ると欧州勢のフェイントでダマシに遭いやすい。{FOOTNOTE_GENERAL}",
    },
    "ロンドン中盤 (18-21)": {
        "feature": "欧州勢主体でトレンドが継続しやすく、NY勢参入前の助走区間。",
        "tips": f"NY市場オープンに向けてポジション調整が入りやすい。{FOOTNOTE_GENERAL}",
    },
    "NY重複 (21-1)": {
        "feature": "欧州・NY勢が同時参加し1日で最も出来高が多い時間帯。米経済指標(21:30/23:00 JST目安)で急変しやすい。",
        "tips": f"指標発表直後はスプレッド拡大・スリッページに注意。ポジションサイズは控えめに。{FOOTNOTE_GENERAL}",
    },
    "NY中盤 (1-4)": {
        "feature": "NY勢主体でトレンドが継続しやすいが、深夜帯で流動性はやや低下し始める。",
        "tips": f"値が伸びる時間帯だが日本時間深夜のため無理のない監視体制を。{FOOTNOTE_GENERAL}",
    },
    "NY後半 (4-6)": {
        "feature": "NY勢の手仕舞いが進み値動きが収束していく時間帯。",
        "tips": f"トレンドの終息・反転サインが出やすい。{FOOTNOTE_GENERAL}",
    },
    "薄商い (6-7)": {
        "feature": "主要市場が閉まりアジア勢の参入前の閑散時間帯。出来高が最も少ない。",
        "tips": f"スプレッド拡大・薄商いによる急変(フラッシュムーブ)リスクに注意。{FOOTNOTE_GENERAL}",
    },
}

PARENT_FEATURE_TIPS: dict[str, dict[str, str]] = {
    "アジア時間": {
        "feature": "東京・香港・シンガポール等アジア勢が主体の時間帯全体。値動きは概して穏やかでレンジになりやすい。",
        "tips": f"レンジ想定の戦略と相性が良いが、指標発表時は例外。{FOOTNOTE_GENERAL}。詳細は下記の帯別行を参照。",
    },
    "ロンドン時間": {
        "feature": "欧州勢が主体となりその日のトレンドが形成されやすい時間帯全体。",
        "tips": f"オープン直後のダマシに注意しつつトレンドフォローを検討。{FOOTNOTE_GENERAL}。詳細は下記の帯別行を参照。",
    },
    "NY時間": {
        "feature": "NY勢主体で1日のうち出来高・値動きが最大となる時間帯全体。米指標発表を含む。",
        "tips": f"指標発表への警戒とポジションサイズ管理が重要。{FOOTNOTE_GENERAL}。詳細は下記の帯別行を参照。",
    },
    "薄商いゾーン": {
        "feature": "主要3市場がいずれも閉まる、あるいは閑散となる時間帯。",
        "tips": f"流動性最薄・急変リスクに最大級の注意を。{FOOTNOTE_GENERAL}。詳細は下記の帯別行を参照。",
    },
}

CROSS_TABLE_COLUMNS = [
    "平均騰落率(%)",
    "平均出来高",
    "平均ボラティリティ(%)",
    "エントリー回数",
    "勝率(%)",
    "合計損益(USD)",
    "動きの特徴",
    "Tips・注意点",
]

# ---- §3-3 足種選択(チャート表示専用。セッション集計は常に1h足ベース) --------------------
TIMEFRAME_RULES: dict[str, Optional[str]] = {
    "1時間足": None,
    "4時間足": "4h",
    "日足": "1D",
}

# ---- 追補v4§2.4A: 分足(1m/5m/15m)の選択肢・実足種・表示期間ガード(バー数上限目安) ------
# 1h/4h/日足はTIMEFRAME_RULES(既存1h足の再サンプルで賄う)のまま不変。分足は既存の再サンプル
# では作れない(1hから5mへの補間は不可能)ため、専用のフェッチ経路を持つ(下記fetch層参照)。
INTRADAY_INTERVAL_MAP: dict[str, str] = {
    "1分足(暗号のみ)": "1m",
    "5分足": "5m",
    "15分足": "15m",
}
INTRADAY_WINDOW_GUARD_DAYS: dict[str, int] = {
    "1分足(暗号のみ)": 3,
    "5分足": 15,
    "15分足": 45,
}
# サイドバー③のラジオボタン全選択肢(分足が先頭・既定は1時間足)
TIMEFRAME_CHOICES: list[str] = list(INTRADAY_INTERVAL_MAP.keys()) + list(TIMEFRAME_RULES.keys())

# ---- 追補v4§2.4B: 🔍トレードズームビュー専用の足種選択肢・実足種・キャッシュTTL ----------------
ZOOM_TIMEFRAME_CHOICES: list[str] = ["1分足(暗号のみ)", "5分足", "15分足", "1時間足"]
ZOOM_INTERVAL_MAP: dict[str, str] = {**INTRADAY_INTERVAL_MAP, "1時間足": "1h"}
ZOOM_YF_5M_AGE_LIMIT_DAYS = 60  # yfinance 5分足の実用的な遡及限界の目安(実測に基づき保守的に設定)
ZOOM_TTL_MIN_BY_CHOICE: dict[str, int] = {
    "1分足(暗号のみ)": MINUTE_CACHE_TTL_MIN, "5分足": FINE_CACHE_TTL_MIN,
    "15分足": FINE_CACHE_TTL_MIN, "1時間足": HOURLY_CACHE_TTL_MIN,
}

# トレードCSV列名ゆらぎ吸収用エイリアス(正規化キー: 空白/括弧/アンダースコア等を除去し小文字化した文字列)
_ALIASES_RAW: dict[str, list[str]] = {
    "Entry_Time": [
        "entry_time(jst)", "entry_time", "entrytime", "entry time",
        "エントリー時刻", "エントリー日時", "entry_datetime", "entrydate",
    ],
    "Exit_Time": [
        "exit_time(jst)", "exit_time", "exittime", "exit time",
        "決済時刻", "エグジット時刻", "exit_datetime", "決済日時",
    ],
    "Symbol": ["symbol", "銘柄", "ticker", "pair", "通貨ペア"],
    "PnL_USD": [
        "pnl_usd", "pnl($)", "pnl_dollar", "損益usd", "損益(usd)", "pnl",
        "profit_usd", "損益金額",
    ],
    "PnL_Percent": [
        "pnl_percent", "pnl_%", "pnl(%)", "損益%", "損益(%)", "pnl_pct", "損益率",
    ],
    "Win_Loss": ["win_loss", "result", "win/loss", "勝敗", "w/l", "勝ち負け"],
    # 追補v4 §2.1: Side/Leverage/Entry_Price/Exit_Price(全て任意列)
    "Side": ["side", "方向", "ポジション", "position", "l/s", "ls", "buy/sell", "buysell"],
    "Leverage": ["leverage", "レバレッジ", "レバ", "lev", "倍率"],
    "Entry_Price": ["entry_price", "entryprice", "entry price", "エントリー価格", "建値", "購入価格"],
    "Exit_Price": ["exit_price", "exitprice", "exit price", "決済価格", "エグジット価格", "売却価格"],
}

WIN_TOKENS = {"win", "w", "win", "勝ち", "勝", "勝利", "true", "yes"}
LOSS_TOKENS = {"loss", "lose", "l", "負け", "負", "敗北", "false", "no"}
DRAW_TOKENS = {"draw", "tie", "引き分け", "分け", "flat", "even"}
# 追補v4 §2.1: Side列の値ゆらぎ吸収(LONG/SHORT・ロング/ショート・買/売・L/S)
LONG_TOKENS = {"long", "l", "ロング", "買い", "買", "buy"}
SHORT_TOKENS = {"short", "s", "ショート", "売り", "売", "sell"}


# =====================================================================================
# 純ロジック関数群 (st非依存・selftest対象)
# =====================================================================================


def hour_to_zone(hour: int) -> str:
    """JST hour(0-23) -> 詳細帯ラベル。"""
    if not isinstance(hour, (int, np.integer)) or not (0 <= int(hour) <= 23):
        raise ValueError(f"hourは0〜23の整数である必要があります: {hour!r}")
    return HOUR_TO_BAND[int(hour)]


def zone_to_parent(band: str) -> str:
    """詳細帯ラベル -> 親(大枠セッション)ラベル。"""
    if band not in BAND_TO_PARENT:
        raise ValueError(f"未知の帯ラベルです: {band!r}")
    return BAND_TO_PARENT[band]


def _band_group_for_bg(hour: int) -> str:
    return BG_HOUR_GROUP[int(hour)]


def compute_session_stats(ohlcv_df: pd.DataFrame) -> pd.DataFrame:
    """1銘柄のOHLCV(tz-aware Asia/Tokyo, 1h足, 列: open/high/low/close/volume)から
    詳細9帯別の 平均騰落率(%)/平均出来高/平均ボラティリティ(%) を計算する。

    - 平均騰落率: セッション日毎に帯内の(最終close-最初open)/最初open*100 -> 全セッション平均
    - 平均出来高: セッション日毎の帯内出来高合計 -> 全セッション平均
    - 平均ボラティリティ: セッション日毎に帯内1h対数リターン(close/close.shift(1))のstd(母集団,ddof=0)*100 -> 全セッション平均
      (帯が1時間しかない「薄商い」は日内サンプル1点となり母集団std=0となる。これは仕様上の既知の制約。)
    - 「NY重複 (21-1)」帯は21時->翌1時(hour 21,22,23,0)の連続4時間ウィンドウのため、単純な暦日
      (ts.date())でグルーピングすると当日00:00(セッションの最後の1時間)と当日21-23時(翌セッションの
      最初の3時間)という時系列的に無関係な2点が同じ暦日として結合されてしまう(実質ほぼ丸1日の値動き
      になる既知のバグ)。これを避けるため、この帯のhour=0の行だけ「前日」をセッション日とみなす
      (=D 21:00〜D+1 00:00 を1セッションとして扱う)。他8帯は日をまたがないため影響しない。
    - 取得データがちょうどhour=0から始まる場合(実運用では選択開始日のJST 00:00がデータ先頭になるため
      常態的に発生する)、その最初のhour=0行は上記の繰り込みにより「データ範囲外の前日」というセッションに
      単独で属してしまい、21-23時のペアを持たない実体のないセッション(その1時間だけの騰落率)が
      平均に混入する。これは「21時始値->翌1時終値」という意図したセッション定義に反するため、
      21時の行を1つも含まないセッション(=前日21-23時データが存在しない孤立したhour=0のみの集団)は
      統計対象から除外する。
    """
    required_cols = {"open", "high", "low", "close", "volume"}
    missing = required_cols - set(ohlcv_df.columns)
    if missing:
        raise ValueError(f"OHLCVに必要な列が不足しています: {sorted(missing)}")
    if ohlcv_df.index.tz is None:
        raise ValueError("OHLCVのindexはtz-awareである必要があります(Asia/Tokyo)")

    df = ohlcv_df.sort_index().copy()
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["band"] = [hour_to_zone(ts.hour) for ts in df.index]
    df["cal_date"] = df.index.date

    # 「NY重複 (21-1)」帯のhour=0の行は、時系列的に連続する前日21-23時のセッションへ繰り込む。
    overnight_mask = (df["band"] == "NY重複 (21-1)") & (df.index.hour == 0)
    overnight_arr = overnight_mask.to_numpy()
    if overnight_arr.any():
        df.loc[overnight_mask, "cal_date"] = (df.index[overnight_arr] - pd.Timedelta(days=1)).date

    rows = []
    for band in BAND_ORDER:
        sub = df[df["band"] == band]
        if band == "NY重複 (21-1)" and not sub.empty:
            # 21時の行を含むセッション日のみを有効なセッションとして残す(前日21-23時データを
            # 伴わない、データ先頭の孤立したhour=0だけの疑似セッションを除外するため)。
            anchor_dates = set(sub.loc[sub.index.hour == 21, "cal_date"])
            sub = sub[sub["cal_date"].isin(anchor_dates)]
        if sub.empty:
            rows.append({"band": band, "avg_return_pct": np.nan, "avg_volume": np.nan,
                         "avg_volatility_pct": np.nan, "n_days": 0})
            continue
        grouped = sub.groupby("cal_date")
        day_open = grouped["open"].first()
        day_close = grouped["close"].last()
        day_return_pct = (day_close - day_open) / day_open * 100.0
        day_volume_sum = grouped["volume"].sum()
        day_vola_pct = grouped["log_ret"].apply(lambda s: float(np.std(s.dropna().to_numpy(), ddof=0)) * 100.0
                                                 if s.dropna().shape[0] > 0 else np.nan)
        rows.append({
            "band": band,
            "avg_return_pct": float(day_return_pct.mean()),
            "avg_volume": float(day_volume_sum.mean()),
            "avg_volatility_pct": float(day_vola_pct.mean()),
            "n_days": int(grouped.ngroups),
        })
    result = pd.DataFrame(rows).set_index("band").reindex(BAND_ORDER)
    return result


def aggregate_market_stats_multi(symbol_stats: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """複数銘柄の compute_session_stats 結果を平均する(「全選択銘柄の平均」モード用)。
    出来高は銘柄ごとにその銘柄の全帯平均出来高で正規化した相対値にしてから平均する
    (単位が銘柄間で大きく異なるため、絶対値の単純平均は無意味になる)。
    """
    if not symbol_stats:
        return pd.DataFrame(
            {"avg_return_pct": np.nan, "avg_volume": np.nan, "avg_volatility_pct": np.nan, "n_days": 0},
            index=BAND_ORDER,
        )
    normalized = {}
    for label, d in symbol_stats.items():
        dd = d.reindex(BAND_ORDER).copy()
        mean_vol = dd["avg_volume"].mean(skipna=True)
        if mean_vol and mean_vol > 0 and not pd.isna(mean_vol):
            dd["avg_volume_rel"] = dd["avg_volume"] / mean_vol
        else:
            dd["avg_volume_rel"] = np.nan
        normalized[label] = dd
    combined = pd.concat(normalized, names=["symbol", "band"])
    result = pd.DataFrame(index=BAND_ORDER)
    result["avg_return_pct"] = combined.groupby(level="band")["avg_return_pct"].mean().reindex(BAND_ORDER)
    result["avg_volume"] = combined.groupby(level="band")["avg_volume_rel"].mean().reindex(BAND_ORDER)
    result["avg_volatility_pct"] = combined.groupby(level="band")["avg_volatility_pct"].mean().reindex(BAND_ORDER)
    result["n_days"] = combined.groupby(level="band")["n_days"].mean().reindex(BAND_ORDER)
    return result


def _norm_colname_key(s: str) -> str:
    s = str(s).strip().lower()
    for ch in ["(", ")", "（", "）", " ", "_", "-", ":", "：", "　"]:
        s = s.replace(ch, "")
    return s


_ALIAS_LOOKUP: dict[str, str] = {}
for _canon, _alist in _ALIASES_RAW.items():
    for _a in _alist:
        _ALIAS_LOOKUP[_norm_colname_key(_a)] = _canon
    _ALIAS_LOOKUP[_norm_colname_key(_canon)] = _canon


def _normalize_result_token(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().lower()
    if s in WIN_TOKENS:
        return "win"
    if s in LOSS_TOKENS:
        return "loss"
    if s in DRAW_TOKENS:
        return "draw"
    return None


def _normalize_side_token(v: Any) -> Any:
    """追補v4 §2.1: Side列の値ゆらぎ(LONG/SHORT・大小文字・ロング/ショート・買/売・L/S)を吸収する。
    認識できない値・欠落はnp.nan(NaN)を返す。
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return np.nan
    s = str(v).strip().lower()
    if s in LONG_TOKENS:
        return "LONG"
    if s in SHORT_TOKENS:
        return "SHORT"
    return np.nan


def _clean_leverage_series(s: pd.Series) -> pd.Series:
    """追補v4 §2.1: "10x"/"x10"/"10倍"のような倍率表記を吸収してレバレッジを数値化する。"""
    def _clean_one(v: Any) -> Any:
        if pd.isna(v):
            return np.nan
        if isinstance(v, (int, float, np.integer, np.floating)):
            return float(v)
        s = str(v).strip().lower().replace(" ", "").replace("倍", "")
        return s.strip("x")
    return pd.to_numeric(s.map(_clean_one), errors="coerce")


def _clean_numeric_series(s: pd.Series) -> pd.Series:
    """"1,234.5" や "$100" 、"¥-50" のような文字列も数値化できるように前処理してから to_numeric する。"""
    def _clean_one(v: Any) -> Any:
        if pd.isna(v):
            return np.nan
        if isinstance(v, (int, float, np.integer, np.floating)):
            return v
        s = str(v).strip()
        for ch in [",", "$", "¥", "円", "%", " "]:
            s = s.replace(ch, "")
        return s
    return pd.to_numeric(s.map(_clean_one), errors="coerce")


_TIME_COMPONENT_RE = re.compile(r"\d{1,2}\s*[:時]\s*\d{1,2}")


def _lacks_time_component(v: Any) -> bool:
    """Entry_Time等の元の生文字列に時刻(HH:MM)成分が含まれていないかを判定する。

    pd.to_datetime は "2026-01-01" のような日付のみの文字列も暗黙的に 00:00:00 として
    解析できてしまう。00:00 は「NY重複 (21-1)」帯に集約されるため、本来は時刻不明であるはずの
    行が実データとして紛れ込み、セッション別分析結果を静かに歪める(発見しづらい実害)。
    そのため解析後の値ではなく元の生文字列側で時刻表記の有無を別途チェックする。
    """
    if pd.isna(v):
        return False
    if isinstance(v, (pd.Timestamp, datetime)):
        # 既にdatetime化された値が渡された場合は元の文字列表記を復元できないため判定不能=警告なし
        return False
    return _TIME_COMPONENT_RE.search(str(v)) is None


def normalize_trades_csv(raw_df: pd.DataFrame) -> tuple[Optional[pd.DataFrame], list[tuple[str, str]]]:
    """アップロードされたトレードCSVの列名ゆらぎ吸収・型変換・壊れた行の除去を行う。

    戻り値: (正規化後のDataFrame または 必須列欠落時はNone, (severity, message) タプルのリスト)
      severity は "error"(赤・行破損/致命的) または "warning"(黄・軽微な注意喚起) のいずれか。
      呼び出し側はこのタグで表示を出し分ける(メッセージ文言の部分一致に依存しない)。
    """
    errors: list[tuple[str, str]] = []
    if raw_df is None or raw_df.empty:
        return None, [("error", "CSVが空です。")]

    rename_map: dict[str, str] = {}
    for col in raw_df.columns:
        key = _norm_colname_key(col)
        if key in _ALIAS_LOOKUP:
            rename_map[col] = _ALIAS_LOOKUP[key]
    df = raw_df.rename(columns=rename_map).copy()

    # 重複列名(同じ正規化名に複数の列がマッピングされた)は最初の非NaNを優先して1列に潰す
    dup_cols = [c for c in set(df.columns) if list(df.columns).count(c) > 1]
    for c in dup_cols:
        sub = df.loc[:, df.columns == c]
        merged = sub.bfill(axis=1).iloc[:, 0]
        df = df.loc[:, df.columns != c]
        df[c] = merged

    required = {"Entry_Time", "Symbol", "PnL_USD"}
    missing = required - set(df.columns)
    if missing:
        return None, [("error", f"必須列が見つかりません: {', '.join(sorted(missing))}")]

    n0 = len(df)
    df = df.reset_index(drop=True)

    # Entry_Time / Exit_Time -> tz-aware Asia/Tokyo
    def _to_jst(series: pd.Series) -> pd.Series:
        # format="mixed": 「日付のみ」と「日付+時刻」の混在列で後者がNaT化するのを防ぐ
        # (pandasは先頭要素から単一フォーマットを推論するため)。
        # tz付き/naive文字列が混在するとformat="mixed"はValueErrorになるため、
        # その場合は要素単位でパースし、naiveはJSTとして扱う(utc=Trueだと9時間ズレる)。
        try:
            parsed = pd.to_datetime(series, errors="coerce", format="mixed")
        except (ValueError, TypeError):
            def _parse_one(v):
                ts = pd.to_datetime(v, errors="coerce")
                if ts is pd.NaT:
                    return pd.NaT
                if ts.tzinfo is None:
                    return ts.tz_localize("Asia/Tokyo")
                return ts.tz_convert("Asia/Tokyo")

            parsed = pd.to_datetime(series.map(_parse_one), errors="coerce")
        try:
            if parsed.dt.tz is None:
                parsed = parsed.dt.tz_localize("Asia/Tokyo", ambiguous="NaT", nonexistent="NaT")
            else:
                parsed = parsed.dt.tz_convert("Asia/Tokyo")
        except (TypeError, AttributeError):
            pass
        return parsed

    raw_entry_time_for_time_check = df["Entry_Time"].copy()
    df["Entry_Time"] = _to_jst(df["Entry_Time"])
    if "Exit_Time" in df.columns:
        df["Exit_Time"] = _to_jst(df["Exit_Time"])
    else:
        df["Exit_Time"] = pd.NaT

    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df.loc[df["Symbol"].isin(["", "nan", "None"]), "Symbol"] = np.nan

    df["PnL_USD"] = _clean_numeric_series(df["PnL_USD"])
    if "PnL_Percent" in df.columns:
        df["PnL_Percent"] = _clean_numeric_series(df["PnL_Percent"])
    else:
        df["PnL_Percent"] = np.nan

    if "Win_Loss" in df.columns:
        df["Win_Loss"] = df["Win_Loss"].map(_normalize_result_token)
    else:
        df["Win_Loss"] = None

    # 追補v4 §2.1: Side/Leverage/Entry_Price/Exit_Price(全て任意列。欠落時はNaN)
    if "Side" in df.columns:
        df["Side"] = df["Side"].map(_normalize_side_token)
    else:
        df["Side"] = np.nan
    if "Leverage" in df.columns:
        df["Leverage"] = _clean_leverage_series(df["Leverage"])
    else:
        df["Leverage"] = np.nan
    if "Entry_Price" in df.columns:
        df["Entry_Price"] = _clean_numeric_series(df["Entry_Price"])
    else:
        df["Entry_Price"] = np.nan
    if "Exit_Price" in df.columns:
        df["Exit_Price"] = _clean_numeric_series(df["Exit_Price"])
    else:
        df["Exit_Price"] = np.nan

    # Win_Loss未確定(欠落 or 認識不能)はPnL_USDの符号から導出。0は引き分け。
    def _derive(row):
        if row["Win_Loss"] in ("win", "loss", "draw"):
            return row["Win_Loss"]
        pnl = row["PnL_USD"]
        if pd.isna(pnl):
            return None
        if pnl > 0:
            return "win"
        if pnl < 0:
            return "loss"
        return "draw"

    df["Win_Loss"] = df.apply(_derive, axis=1)

    # 壊れた行を検出(このマスクはfilter前の行位置を保つため、除去は下の行番号メッセージ生成の後で行う)
    bad_mask = df["Entry_Time"].isna() | df["Symbol"].isna() | df["PnL_USD"].isna() | df["Win_Loss"].isna()
    if bad_mask.any():
        for idx in df.index[bad_mask]:
            reasons = []
            if pd.isna(df.loc[idx, "Entry_Time"]):
                reasons.append("Entry_Timeが解析できません")
            if pd.isna(df.loc[idx, "Symbol"]):
                reasons.append("Symbolが空です")
            if pd.isna(df.loc[idx, "PnL_USD"]):
                reasons.append("PnL_USDが数値変換できません")
            if pd.isna(df.loc[idx, "Win_Loss"]):
                reasons.append("Win_Lossが判定できません")
            errors.append(("error", f"行{idx + 2}: {' / '.join(reasons)}"))  # +2 = ヘッダ行+0始まり補正

    # Entry_Timeの解析自体には成功したが、元の生文字列に時刻(HH:MM)成分が無く暗黙的に00:00
    # として扱われた行を警告する(「NY重複」帯へ誤って集約されセッション分析結果が歪む実害があるため)。
    no_time_mask = (~bad_mask) & raw_entry_time_for_time_check.apply(_lacks_time_component)
    if no_time_mask.any():
        no_time_rows = [str(idx + 2) for idx in df.index[no_time_mask]]
        shown = ", ".join(no_time_rows[:20])
        suffix = f" 他{len(no_time_rows) - 20}件" if len(no_time_rows) > 20 else ""
        errors.append((
            "warning",
            f"Entry_Timeに時刻情報が無いため00:00(NY重複帯)として扱われた行が{len(no_time_rows)}件あります"
            f"(行{shown}{suffix})。実際の時刻が不明な場合、セッション別の分析結果が実態と異なる可能性があります。",
        ))

    if bad_mask.any():
        df = df.loc[~bad_mask].reset_index(drop=True)

    if df.empty:
        errors.append(("error", "有効な行が1件も残りませんでした。"))
        return None, errors

    keep_cols = [
        "Entry_Time", "Exit_Time", "Symbol", "PnL_USD", "PnL_Percent", "Win_Loss",
        "Side", "Leverage", "Entry_Price", "Exit_Price",
    ]
    df = df[keep_cols]
    if errors:
        errors.insert(0, ("warning", f"{n0}行中{len(df)}行を読み込みました({n0 - len(df)}行を除外)。"))
    return df, errors


def assign_trade_sessions(trades_df: pd.DataFrame) -> pd.DataFrame:
    """正規化済みトレードDataFrame(Entry_Time列がtz-aware JST)に band/parent 列を付加する。"""
    if "Entry_Time" not in trades_df.columns:
        raise ValueError("Entry_Time列がありません")
    df = trades_df.copy()
    df["band"] = df["Entry_Time"].apply(lambda ts: hour_to_zone(ts.hour))
    df["parent"] = df["band"].apply(zone_to_parent)
    return df


def compute_trade_stats(trades_df: pd.DataFrame) -> dict[str, Any]:
    """band/parent付きトレードDataFrameから 帯別(回数/勝率/損益) + 総合統計(PF/DD/連敗等) を計算する。

    戻り値: {"by_band": DataFrame(index=band, columns=[n_trades,n_wins,n_losses,n_draws,total_pnl_usd,win_rate_pct]),
             "overall": {total_pnl_usd, n_trades, win_rate_pct, profit_factor, avg_win, avg_loss, rr,
                         max_dd_usd, max_consec_losses, first_time, last_time}}
    """
    empty_overall = {
        "total_pnl_usd": np.nan, "n_trades": 0, "win_rate_pct": np.nan, "profit_factor": np.nan,
        "avg_win": np.nan, "avg_loss": np.nan, "rr": np.nan, "max_dd_usd": np.nan,
        "max_consec_losses": 0, "first_time": None, "last_time": None,
    }
    if trades_df is None or trades_df.empty:
        empty_band = pd.DataFrame(
            columns=["n_trades", "n_wins", "n_losses", "n_draws", "total_pnl_usd", "win_rate_pct"]
        )
        return {"by_band": empty_band, "overall": empty_overall}

    if "band" not in trades_df.columns:
        raise ValueError("band列がありません。先に assign_trade_sessions を適用してください。")

    df = trades_df.sort_values("Entry_Time").reset_index(drop=True)

    rows = []
    for band, g in df.groupby("band"):
        n_trades = len(g)
        n_wins = int((g["Win_Loss"] == "win").sum())
        n_losses = int((g["Win_Loss"] == "loss").sum())
        n_draws = int((g["Win_Loss"] == "draw").sum())
        total_pnl = float(g["PnL_USD"].sum())
        win_rate = (n_wins / (n_wins + n_losses) * 100.0) if (n_wins + n_losses) > 0 else np.nan
        rows.append({
            "band": band, "n_trades": n_trades, "n_wins": n_wins, "n_losses": n_losses,
            "n_draws": n_draws, "total_pnl_usd": total_pnl, "win_rate_pct": win_rate,
        })
    by_band = pd.DataFrame(rows).set_index("band")

    n_trades = len(df)
    n_wins = int((df["Win_Loss"] == "win").sum())
    n_losses = int((df["Win_Loss"] == "loss").sum())
    total_pnl = float(df["PnL_USD"].sum())
    win_rate = (n_wins / (n_wins + n_losses) * 100.0) if (n_wins + n_losses) > 0 else np.nan

    wins_sum = float(df.loc[df["Win_Loss"] == "win", "PnL_USD"].sum())
    losses_sum = float(df.loc[df["Win_Loss"] == "loss", "PnL_USD"].sum())  # 負の値
    if losses_sum != 0:
        profit_factor = wins_sum / abs(losses_sum)
    else:
        profit_factor = float("inf") if wins_sum > 0 else np.nan

    avg_win = float(df.loc[df["Win_Loss"] == "win", "PnL_USD"].mean()) if n_wins > 0 else np.nan
    avg_loss = float(df.loc[df["Win_Loss"] == "loss", "PnL_USD"].mean()) if n_losses > 0 else np.nan
    if n_wins > 0 and n_losses > 0 and avg_loss != 0:
        rr = avg_win / abs(avg_loss)
    else:
        rr = np.nan

    cum = df["PnL_USD"].cumsum()
    running_max = cum.cummax()
    drawdown = running_max - cum
    max_dd = float(drawdown.max()) if len(drawdown) > 0 else 0.0

    max_streak = 0
    cur_streak = 0
    for wl in df["Win_Loss"]:
        if wl == "loss":
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        elif wl == "win":
            cur_streak = 0
        # draw: streakを変化させない

    overall = {
        "total_pnl_usd": total_pnl,
        "n_trades": n_trades,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "rr": rr,
        "max_dd_usd": max_dd,
        "max_consec_losses": max_streak,
        "first_time": df["Entry_Time"].min(),
        "last_time": df["Entry_Time"].max(),
    }
    return {"by_band": by_band, "overall": overall}


_COMPARE_OVERALL_NUMERIC_KEYS = [
    "total_pnl_usd", "n_trades", "win_rate_pct", "profit_factor",
    "avg_win", "avg_loss", "rr", "max_dd_usd", "max_consec_losses",
]
_COMPARE_BY_BAND_COLUMNS = [
    "n_trades_a", "n_wins_a", "n_losses_a", "win_rate_pct_a", "total_pnl_usd_a",
    "n_trades_b", "n_wins_b", "n_losses_b", "win_rate_pct_b", "total_pnl_usd_b",
    "win_rate_diff", "total_pnl_diff", "low_n_a", "low_n_b",
]


def _to_float_or_nan(v: Any) -> float:
    if v is None:
        return float("nan")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return f


def compare_trade_stats(stats_a: dict[str, Any], stats_b: dict[str, Any]) -> dict[str, Any]:
    """compute_trade_stats() の戻り値2つ(既定=a:弟子, b:師匠)を比較する純関数。追補仕様書§1.4/§1.3。

    差分は全て「b - a」(師匠−弟子)の向きに統一する。捏造アドバイス文は一切生成しない
    (数値の突合のみ。掟2)。

    戻り値:
        {
          "overall_a": stats_a["overall"], "overall_b": stats_b["overall"],
          "overall_diff": {指標名: b-aの数値差(_COMPARE_OVERALL_NUMERIC_KEYSのみ対象)},
          "period_a": (first_time_a, last_time_a), "period_b": (first_time_b, last_time_b),
          "by_band": DataFrame(index=帯ラベル(aの登場順→bのみに有る帯を末尾追加),
                     columns=_COMPARE_BY_BAND_COLUMNS。片側にしか無い帯は無い側を0/NaNで補完し
                     low_n_*=True(n<3)を付す)。
        }
    """
    return _compare_trade_stats_impl(stats_a, stats_b)


def _compare_trade_stats_impl(stats_a: dict[str, Any], stats_b: dict[str, Any]) -> dict[str, Any]:
    overall_a = (stats_a or {}).get("overall", {}) or {}
    overall_b = (stats_b or {}).get("overall", {}) or {}

    overall_diff: dict[str, float] = {}
    for k in _COMPARE_OVERALL_NUMERIC_KEYS:
        fa = _to_float_or_nan(overall_a.get(k))
        fb = _to_float_or_nan(overall_b.get(k))
        overall_diff[k] = fb - fa

    by_band_a = (stats_a or {}).get("by_band")
    by_band_b = (stats_b or {}).get("by_band")
    if by_band_a is None:
        by_band_a = pd.DataFrame()
    if by_band_b is None:
        by_band_b = pd.DataFrame()

    bands: list[str] = list(by_band_a.index)
    for b in by_band_b.index:
        if b not in bands:
            bands.append(b)

    rows = []
    for band in bands:
        has_a = band in by_band_a.index
        has_b = band in by_band_b.index
        ra = by_band_a.loc[band] if has_a else None
        rb = by_band_b.loc[band] if has_b else None
        n_a = int(ra["n_trades"]) if has_a else 0
        n_b = int(rb["n_trades"]) if has_b else 0
        wr_a = _to_float_or_nan(ra["win_rate_pct"]) if has_a else float("nan")
        wr_b = _to_float_or_nan(rb["win_rate_pct"]) if has_b else float("nan")
        pnl_a = _to_float_or_nan(ra["total_pnl_usd"]) if has_a else float("nan")
        pnl_b = _to_float_or_nan(rb["total_pnl_usd"]) if has_b else float("nan")
        rows.append({
            "band": band,
            "n_trades_a": n_a, "n_wins_a": int(ra["n_wins"]) if has_a else 0,
            "n_losses_a": int(ra["n_losses"]) if has_a else 0,
            "win_rate_pct_a": wr_a, "total_pnl_usd_a": pnl_a,
            "n_trades_b": n_b, "n_wins_b": int(rb["n_wins"]) if has_b else 0,
            "n_losses_b": int(rb["n_losses"]) if has_b else 0,
            "win_rate_pct_b": wr_b, "total_pnl_usd_b": pnl_b,
            "win_rate_diff": (wr_b - wr_a) if not (math.isnan(wr_a) or math.isnan(wr_b)) else float("nan"),
            "total_pnl_diff": (pnl_b - pnl_a) if not (math.isnan(pnl_a) or math.isnan(pnl_b)) else float("nan"),
            "low_n_a": n_a < 3, "low_n_b": n_b < 3,
        })

    if rows:
        by_band_df = pd.DataFrame(rows).set_index("band")[_COMPARE_BY_BAND_COLUMNS]
    else:
        by_band_df = pd.DataFrame(columns=_COMPARE_BY_BAND_COLUMNS)

    return {
        "overall_a": overall_a, "overall_b": overall_b, "overall_diff": overall_diff,
        "period_a": (overall_a.get("first_time"), overall_a.get("last_time")),
        "period_b": (overall_b.get("first_time"), overall_b.get("last_time")),
        "by_band": by_band_df,
    }


def _weighted_avg(values: dict[str, float], weights: dict[str, float]) -> float:
    total_w = 0.0
    total_v = 0.0
    has_any = False
    for k, v in values.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        w = weights.get(k, 0.0)
        total_v += v * w
        total_w += w
        has_any = True
    if not has_any or total_w == 0:
        return float("nan")
    return total_v / total_w


def build_cross_table(
    market_stats: Optional[pd.DataFrame],
    trade_stats: Optional[dict[str, Any]],
) -> pd.DataFrame:
    """§4.1 多重クロス表を構築する。

    market_stats: compute_session_stats (または aggregate_market_stats_multi) の出力。Noneなら市場列は全てNaN。
    trade_stats: compute_trade_stats の出力(dict)。Noneならトレード列は全てNaN。

    行: MultiIndex(大枠, 詳細帯)。各大枠につき [大枠行(hour加重合成), 詳細帯行...] の順。
    """
    by_band = trade_stats["by_band"] if trade_stats is not None else None

    index_tuples: list[tuple[str, str]] = []
    records: list[dict[str, Any]] = []

    for parent in PARENT_ORDER:
        children = [b for b in BAND_ORDER if BAND_TO_PARENT[b] == parent]
        child_hours = {b: len(BAND_HOURS[b]) for b in children}

        # --- 市場統計: 時間数加重合成 ---
        if market_stats is not None:
            ret_vals = {b: market_stats.loc[b, "avg_return_pct"] if b in market_stats.index else np.nan for b in children}
            vol_vals = {b: market_stats.loc[b, "avg_volume"] if b in market_stats.index else np.nan for b in children}
            vola_vals = {b: market_stats.loc[b, "avg_volatility_pct"] if b in market_stats.index else np.nan for b in children}
            parent_return = _weighted_avg(ret_vals, child_hours)
            parent_volume = _weighted_avg(vol_vals, child_hours)
            parent_vola = _weighted_avg(vola_vals, child_hours)
        else:
            parent_return = parent_volume = parent_vola = np.nan

        # --- トレード統計: 直接合算(件数は加算しても二重計上にならない) ---
        if by_band is not None and not by_band.empty:
            sub = by_band.loc[by_band.index.intersection(children)]
            n_trades_sum = int(sub["n_trades"].sum()) if not sub.empty else 0
            n_wins_sum = int(sub["n_wins"].sum()) if not sub.empty else 0
            n_losses_sum = int(sub["n_losses"].sum()) if not sub.empty else 0
            total_pnl_sum = float(sub["total_pnl_usd"].sum()) if not sub.empty else 0.0
            if n_trades_sum > 0:
                parent_n_trades: Any = n_trades_sum
                parent_total_pnl: Any = total_pnl_sum
            else:
                parent_n_trades = np.nan
                parent_total_pnl = np.nan
            parent_win_rate = (n_wins_sum / (n_wins_sum + n_losses_sum) * 100.0) if (n_wins_sum + n_losses_sum) > 0 else np.nan
        else:
            parent_n_trades = np.nan
            parent_win_rate = np.nan
            parent_total_pnl = np.nan

        p_feat = PARENT_FEATURE_TIPS[parent]["feature"]
        p_tips = PARENT_FEATURE_TIPS[parent]["tips"]
        if not pd.isna(parent_win_rate) and parent_win_rate < LOW_WINRATE_THRESHOLD:
            p_tips = p_tips + LOW_WINRATE_WARNING

        index_tuples.append((parent, PARENT_ROW_LABEL[parent]))
        records.append({
            CROSS_TABLE_COLUMNS[0]: parent_return,
            CROSS_TABLE_COLUMNS[1]: parent_volume,
            CROSS_TABLE_COLUMNS[2]: parent_vola,
            CROSS_TABLE_COLUMNS[3]: parent_n_trades,
            CROSS_TABLE_COLUMNS[4]: parent_win_rate,
            CROSS_TABLE_COLUMNS[5]: parent_total_pnl,
            CROSS_TABLE_COLUMNS[6]: p_feat,
            CROSS_TABLE_COLUMNS[7]: p_tips,
        })

        for b in children:
            if market_stats is not None and b in market_stats.index:
                m_return = market_stats.loc[b, "avg_return_pct"]
                m_volume = market_stats.loc[b, "avg_volume"]
                m_vola = market_stats.loc[b, "avg_volatility_pct"]
            else:
                m_return = m_volume = m_vola = np.nan

            if by_band is not None and b in by_band.index:
                t_row = by_band.loc[b]
                t_n = int(t_row["n_trades"]) if t_row["n_trades"] > 0 else np.nan
                t_win = t_row["win_rate_pct"]
                t_pnl = t_row["total_pnl_usd"] if t_row["n_trades"] > 0 else np.nan
            else:
                t_n = np.nan
                t_win = np.nan
                t_pnl = np.nan

            feat = BAND_FEATURE_TIPS[b]["feature"]
            tips = BAND_FEATURE_TIPS[b]["tips"]
            if not pd.isna(t_win) and t_win < LOW_WINRATE_THRESHOLD:
                tips = tips + LOW_WINRATE_WARNING

            index_tuples.append((parent, b))
            records.append({
                CROSS_TABLE_COLUMNS[0]: m_return,
                CROSS_TABLE_COLUMNS[1]: m_volume,
                CROSS_TABLE_COLUMNS[2]: m_vola,
                CROSS_TABLE_COLUMNS[3]: t_n,
                CROSS_TABLE_COLUMNS[4]: t_win,
                CROSS_TABLE_COLUMNS[5]: t_pnl,
                CROSS_TABLE_COLUMNS[6]: feat,
                CROSS_TABLE_COLUMNS[7]: tips,
            })

    idx = pd.MultiIndex.from_tuples(index_tuples, names=["大枠", "詳細帯"])
    return pd.DataFrame(records, index=idx, columns=CROSS_TABLE_COLUMNS)


def split_cross_table(cross: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """追補v3 §2: build_cross_table出力(MultiIndex 大枠×詳細帯・13行)を
    「親表(大枠計4行のみ)」と「大枠ごとの詳細表(dict、詳細帯のみ)」に分割する純関数。
    多重クロス表タブのアコーディオン化(親表+大枠ごとのst.expander)用。
    親表のindexは大枠名(PARENT_ORDER)のみの単層にする(詳細帯側との重複表記を避ける)。
    """
    parent_label_set = set(PARENT_ROW_LABEL.values())
    is_parent_row = cross.index.get_level_values("詳細帯").isin(parent_label_set)
    parent_df = cross[is_parent_row].copy()
    parent_df.index = pd.Index(parent_df.index.get_level_values("大枠"), name="大枠")

    detail_by_parent: dict[str, pd.DataFrame] = {}
    for parent in PARENT_ORDER:
        children = [b for b in BAND_ORDER if BAND_TO_PARENT[b] == parent]
        mask = (cross.index.get_level_values("大枠") == parent) & (
            cross.index.get_level_values("詳細帯").isin(children)
        )
        sub = cross[mask].copy()
        sub.index = pd.Index(sub.index.get_level_values("詳細帯"), name="詳細帯")
        detail_by_parent[parent] = sub
    return parent_df, detail_by_parent


# =====================================================================================
# 追補§1/§2: 曜日別・月別アノマリー(純ロジック・st非依存・selftest対象)
# =====================================================================================


def compute_weekday_stats(ohlcv_df: pd.DataFrame) -> pd.DataFrame:
    """1銘柄のOHLCV(tz-aware Asia/Tokyo, 1h足, 列: open/high/low/close/volume)から
    JST暦日ごとに日次集計(騰落率/出来高/ボラティリティ)し、曜日(月〜日)ごとに平均する。

    - 平均騰落率: JST暦日毎に(その日の最終close-最初open)/最初open*100 -> 同じ曜日の全日平均
    - 平均出来高: JST暦日毎の出来高合計 -> 同じ曜日の全日平均
    - 平均ボラティリティ: JST暦日毎に1h対数リターン(close/close.shift(1))のstd(母集団,ddof=0)*100
      -> 同じ曜日の全日平均
    - compute_session_statsの「セッション帯」と異なり、ここでは暦日そのものを集計単位とするため、
      「NY重複」帯特有の日またぎ繰り込みは行わない。JST 23時台はその暦日の、翌0時台は翌暦日の
      曜日にそのまま帰属する(これは意図した挙動であり境界のずれではない。selftest §8-2参照)。
    - 出力: DataFrame(index=WEEKDAY_LABELSの7行、
                       columns=[avg_return_pct, avg_volume, avg_volatility_pct, n_days])
      追補仕様書 §1.1。
    """
    required_cols = {"open", "high", "low", "close", "volume"}
    missing = required_cols - set(ohlcv_df.columns)
    if missing:
        raise ValueError(f"OHLCVに必要な列が不足しています: {sorted(missing)}")
    if ohlcv_df.index.tz is None:
        raise ValueError("OHLCVのindexはtz-awareである必要があります(Asia/Tokyo)")

    df = ohlcv_df.sort_index().copy()
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["cal_date"] = df.index.date

    grouped = df.groupby("cal_date")
    day_open = grouped["open"].first()
    day_close = grouped["close"].last()
    day_return_pct = (day_close - day_open) / day_open * 100.0
    day_volume_sum = grouped["volume"].sum()
    day_vola_pct = grouped["log_ret"].apply(
        lambda s: float(np.std(s.dropna().to_numpy(), ddof=0)) * 100.0 if s.dropna().shape[0] > 0 else np.nan
    )

    daily = pd.DataFrame({
        "return_pct": day_return_pct,
        "volume": day_volume_sum,
        "vola_pct": day_vola_pct,
    })
    daily["weekday"] = [WEEKDAY_LABELS[pd.Timestamp(d).weekday()] for d in daily.index]

    rows = []
    for label in WEEKDAY_LABELS:
        sub = daily[daily["weekday"] == label]
        if sub.empty:
            rows.append({"weekday": label, "avg_return_pct": np.nan, "avg_volume": np.nan,
                         "avg_volatility_pct": np.nan, "n_days": 0})
            continue
        rows.append({
            "weekday": label,
            "avg_return_pct": float(sub["return_pct"].mean()),
            "avg_volume": float(sub["volume"].mean()),
            "avg_volatility_pct": float(sub["vola_pct"].mean()),
            "n_days": int(len(sub)),
        })
    result = pd.DataFrame(rows).set_index("weekday").reindex(WEEKDAY_LABELS)
    return result


def compute_trade_weekday_stats(trades_df: pd.DataFrame) -> pd.DataFrame:
    """正規化済みトレードDataFrame(Entry_Time列がtz-aware JST)のEntry_Timeの曜日(月〜日)ごとに
    エントリー回数/勝率(%)/合計損益(USD)等を集計する。draw(引き分け)は勝率の分母から除外する
    (compute_trade_statsのband別集計と同じ規約)。追補仕様書 §1.2。

    戻り値: DataFrame(index=WEEKDAY_LABELSの7行、
                       columns=[n_trades, n_wins, n_losses, n_draws, total_pnl_usd, win_rate_pct])
    """
    empty_cols = ["n_trades", "n_wins", "n_losses", "n_draws", "total_pnl_usd", "win_rate_pct"]
    if trades_df is None or trades_df.empty:
        return pd.DataFrame(
            {"n_trades": 0, "n_wins": 0, "n_losses": 0, "n_draws": 0,
             "total_pnl_usd": np.nan, "win_rate_pct": np.nan},
            index=WEEKDAY_LABELS,
        )[empty_cols]
    if "Entry_Time" not in trades_df.columns:
        raise ValueError("Entry_Time列がありません")

    df = trades_df.copy()
    df["weekday"] = df["Entry_Time"].apply(lambda ts: WEEKDAY_LABELS[ts.weekday()])

    rows = []
    for label in WEEKDAY_LABELS:
        sub = df[df["weekday"] == label]
        n_trades = len(sub)
        if n_trades == 0:
            rows.append({"weekday": label, "n_trades": 0, "n_wins": 0, "n_losses": 0,
                         "n_draws": 0, "total_pnl_usd": np.nan, "win_rate_pct": np.nan})
            continue
        n_wins = int((sub["Win_Loss"] == "win").sum())
        n_losses = int((sub["Win_Loss"] == "loss").sum())
        n_draws = int((sub["Win_Loss"] == "draw").sum())
        total_pnl = float(sub["PnL_USD"].sum())
        win_rate = (n_wins / (n_wins + n_losses) * 100.0) if (n_wins + n_losses) > 0 else np.nan
        rows.append({"weekday": label, "n_trades": n_trades, "n_wins": n_wins, "n_losses": n_losses,
                     "n_draws": n_draws, "total_pnl_usd": total_pnl, "win_rate_pct": win_rate})
    return pd.DataFrame(rows).set_index("weekday").reindex(WEEKDAY_LABELS)[empty_cols]


def compute_session_weekday_matrix(ohlcv_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """1銘柄のOHLCV(tz-aware Asia/Tokyo, 1h足)から 詳細9帯×7曜日 の平均騰落率(%)行列と
    サンプル日数(n)行列を算出する(セッション×曜日ヒートマップ用。追補仕様書 §1.3-3)。

    帯ごとのセッション日定義はcompute_session_statsと同一(「NY重複(21-1)」帯のhour=0は
    前日21-23時のセッションへ繰り込み、21時の行を伴わない孤立したhour=0のみのセッションは除外)。
    その繰り込み後のセッション日の曜日で列を決める。

    戻り値: (return_matrix, n_matrix)。いずれも index=BAND_ORDER(9行), columns=WEEKDAY_LABELS(7列)。
    データの無いセルはreturn_matrix=NaN・n_matrix=0。
    """
    required_cols = {"open", "high", "low", "close", "volume"}
    missing = required_cols - set(ohlcv_df.columns)
    if missing:
        raise ValueError(f"OHLCVに必要な列が不足しています: {sorted(missing)}")
    if ohlcv_df.index.tz is None:
        raise ValueError("OHLCVのindexはtz-awareである必要があります(Asia/Tokyo)")

    df = ohlcv_df.sort_index().copy()
    df["band"] = [hour_to_zone(ts.hour) for ts in df.index]
    df["cal_date"] = df.index.date

    overnight_mask = (df["band"] == "NY重複 (21-1)") & (df.index.hour == 0)
    overnight_arr = overnight_mask.to_numpy()
    if overnight_arr.any():
        df.loc[overnight_mask, "cal_date"] = (df.index[overnight_arr] - pd.Timedelta(days=1)).date

    return_matrix = pd.DataFrame(np.nan, index=BAND_ORDER, columns=WEEKDAY_LABELS)
    n_matrix = pd.DataFrame(0, index=BAND_ORDER, columns=WEEKDAY_LABELS, dtype="int64")

    for band in BAND_ORDER:
        sub = df[df["band"] == band]
        if band == "NY重複 (21-1)" and not sub.empty:
            anchor_dates = set(sub.loc[sub.index.hour == 21, "cal_date"])
            sub = sub[sub["cal_date"].isin(anchor_dates)]
        if sub.empty:
            continue
        grouped = sub.groupby("cal_date")
        day_open = grouped["open"].first()
        day_close = grouped["close"].last()
        day_return_pct = (day_close - day_open) / day_open * 100.0
        day_weekday = pd.Series(
            [WEEKDAY_LABELS[pd.Timestamp(d).weekday()] for d in day_return_pct.index],
            index=day_return_pct.index,
        )
        for wd in WEEKDAY_LABELS:
            wd_vals = day_return_pct[day_weekday == wd]
            if wd_vals.empty:
                continue
            return_matrix.loc[band, wd] = float(wd_vals.mean())
            n_matrix.loc[band, wd] = int(wd_vals.shape[0])

    return return_matrix, n_matrix


def compute_trade_session_weekday_matrix(trades_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """band付きトレードDataFrame(assign_trade_sessions適用後)から 詳細9帯×7曜日 の勝率(%)行列と
    サンプル数(n=勝ち+負けの件数。draw除く)行列を算出する(トレードのセッション×曜日勝率ヒートマップ用。
    追補仕様書 §1.3-4)。draw除外はcompute_trade_statsと同じ規約。

    戻り値: (win_rate_matrix, n_matrix)。いずれも index=BAND_ORDER(9行), columns=WEEKDAY_LABELS(7列)。
    データの無い(または勝敗が確定するトレードが無い)セルはwin_rate_matrix=NaN・n_matrix=0。
    """
    win_matrix = pd.DataFrame(np.nan, index=BAND_ORDER, columns=WEEKDAY_LABELS)
    n_matrix = pd.DataFrame(0, index=BAND_ORDER, columns=WEEKDAY_LABELS, dtype="int64")

    if trades_df is None or trades_df.empty:
        return win_matrix, n_matrix
    if "band" not in trades_df.columns:
        raise ValueError("band列がありません。先に assign_trade_sessions を適用してください。")

    df = trades_df.copy()
    df["weekday"] = df["Entry_Time"].apply(lambda ts: WEEKDAY_LABELS[ts.weekday()])

    for band, g_band in df.groupby("band"):
        if band not in BAND_ORDER:
            continue
        for wd, g in g_band.groupby("weekday"):
            n_wins = int((g["Win_Loss"] == "win").sum())
            n_losses = int((g["Win_Loss"] == "loss").sum())
            denom = n_wins + n_losses
            if denom == 0:
                continue
            win_matrix.loc[band, wd] = n_wins / denom * 100.0
            n_matrix.loc[band, wd] = denom

    return win_matrix, n_matrix


def compute_month_stats(daily_df: pd.DataFrame, today_: Optional[date] = None) -> pd.DataFrame:
    """1銘柄の日足OHLCV(index=日付, 列: open/closeを含む)から暦月オカレンス(年×月の組)ごとに
    月次集計し、月(1〜12月)ごとに 平均/中央値騰落率(%)・陽線率(%)・平均ボラティリティ(%)・
    n(その月のオカレンス数=年数) を返す。

    - 月次騰落率: 暦月オカレンス毎に(月末close-月初open)/月初open*100
    - 月内ボラ: 暦月オカレンス毎の日次対数リターン(close/close.shift(1))のstd(母集団,ddof=0)*100
    - 陽線率: 月次騰落率>0のオカレンスが占める割合(%)
    - 出力: DataFrame(index=MONTH_ORDERの12行、columns=[avg_return_pct, median_return_pct,
                       pct_positive, avg_volatility_pct, n_years, n_excluded_current])
      追補仕様書 §2.2。

    確定指摘対応(欠け月混入): today_ を渡すと、(today_.year, today_.month) と一致する暦月
    オカレンスは「進行中(未完了)」とみなし、平均・中央値・陽線率・n_years の集計対象から除外する
    (取得時点でのcloseは真の月末closeでないため)。除外件数は n_excluded_current 列で報告する。
    today_=None(既定)の場合は除外判定を行わず全オカレンスをそのまま集計する(旧挙動と完全互換)。
    """
    required_cols = {"open", "close"}
    missing = required_cols - set(daily_df.columns)
    if missing:
        raise ValueError(f"日足OHLCVに必要な列が不足しています: {sorted(missing)}")

    empty_result = pd.DataFrame(
        {"avg_return_pct": np.nan, "median_return_pct": np.nan, "pct_positive": np.nan,
         "avg_volatility_pct": np.nan, "n_years": 0, "n_excluded_current": 0},
        index=MONTH_ORDER,
    )
    if daily_df.empty:
        return empty_result

    df = daily_df.sort_index().copy()
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    ts_idx = pd.to_datetime(df.index)
    df["_year"] = ts_idx.year
    df["_month"] = ts_idx.month

    occ_rows = []
    for (yr, mo), g in df.groupby(["_year", "_month"]):
        g = g.sort_index()
        month_open = g["open"].iloc[0]
        month_close = g["close"].iloc[-1]
        month_return_pct = (month_close - month_open) / month_open * 100.0
        rets = g["log_ret"].dropna().to_numpy()
        month_vola_pct = float(np.std(rets, ddof=0)) * 100.0 if rets.shape[0] > 0 else np.nan
        is_incomplete = bool(today_ is not None and int(yr) == today_.year and int(mo) == today_.month)
        occ_rows.append({
            "month": int(mo), "return_pct": float(month_return_pct), "vola_pct": month_vola_pct,
            "is_incomplete": is_incomplete,
        })
    occ_df = pd.DataFrame(occ_rows)

    rows = []
    for mo in MONTH_ORDER:
        sub_all = occ_df[occ_df["month"] == mo]
        n_excluded = int(sub_all["is_incomplete"].sum()) if not sub_all.empty else 0
        sub = sub_all[~sub_all["is_incomplete"]] if not sub_all.empty else sub_all
        if sub.empty:
            rows.append({"month": mo, "avg_return_pct": np.nan, "median_return_pct": np.nan,
                         "pct_positive": np.nan, "avg_volatility_pct": np.nan, "n_years": 0,
                         "n_excluded_current": n_excluded})
            continue
        n_occ = len(sub)
        rows.append({
            "month": mo,
            "avg_return_pct": float(sub["return_pct"].mean()),
            "median_return_pct": float(sub["return_pct"].median()),
            "pct_positive": float((sub["return_pct"] > 0).sum() / n_occ * 100.0),
            "avg_volatility_pct": float(sub["vola_pct"].mean()),
            "n_years": int(n_occ),
            "n_excluded_current": n_excluded,
        })
    result = pd.DataFrame(rows).set_index("month").reindex(MONTH_ORDER)
    return result


def compute_trade_month_stats(trades_df: pd.DataFrame) -> pd.DataFrame:
    """正規化済みトレードDataFrame(Entry_Time列がtz-aware JST)のEntry_Timeの月(1〜12月)ごとに
    エントリー回数/勝率(%)/合計損益(USD)等を集計する。draw(引き分け)は勝率の分母から除外する。
    追補仕様書 §2.3。

    戻り値: DataFrame(index=MONTH_ORDERの12行、
                       columns=[n_trades, n_wins, n_losses, n_draws, total_pnl_usd, win_rate_pct])
    """
    empty_cols = ["n_trades", "n_wins", "n_losses", "n_draws", "total_pnl_usd", "win_rate_pct"]
    if trades_df is None or trades_df.empty:
        return pd.DataFrame(
            {"n_trades": 0, "n_wins": 0, "n_losses": 0, "n_draws": 0,
             "total_pnl_usd": np.nan, "win_rate_pct": np.nan},
            index=MONTH_ORDER,
        )[empty_cols]
    if "Entry_Time" not in trades_df.columns:
        raise ValueError("Entry_Time列がありません")

    df = trades_df.copy()
    df["_month"] = df["Entry_Time"].apply(lambda ts: ts.month)

    rows = []
    for mo in MONTH_ORDER:
        sub = df[df["_month"] == mo]
        n_trades = len(sub)
        if n_trades == 0:
            rows.append({"month": mo, "n_trades": 0, "n_wins": 0, "n_losses": 0,
                         "n_draws": 0, "total_pnl_usd": np.nan, "win_rate_pct": np.nan})
            continue
        n_wins = int((sub["Win_Loss"] == "win").sum())
        n_losses = int((sub["Win_Loss"] == "loss").sum())
        n_draws = int((sub["Win_Loss"] == "draw").sum())
        total_pnl = float(sub["PnL_USD"].sum())
        win_rate = (n_wins / (n_wins + n_losses) * 100.0) if (n_wins + n_losses) > 0 else np.nan
        rows.append({"month": mo, "n_trades": n_trades, "n_wins": n_wins, "n_losses": n_losses,
                     "n_draws": n_draws, "total_pnl_usd": total_pnl, "win_rate_pct": win_rate})
    return pd.DataFrame(rows).set_index("month").reindex(MONTH_ORDER)[empty_cols]


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """1h足OHLCVを任意の足(例: '4h','1D')にリサンプルする(チャート表示専用)。"""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(rule).agg(agg)
    out = out.dropna(subset=["open", "close"])
    return out


def clamp_start_date_for_yfinance(start_date_: date, today_: date) -> tuple[date, bool]:
    """yfinance 1h足は直近730日までのため、開始日が729日より前なら729日前にクランプする。"""
    limit = today_ - timedelta(days=729)
    if start_date_ < limit:
        return limit, True
    return start_date_, False


def compute_intraday_chart_window(
    start_date_: date, end_date_: date, interval_choice: str,
) -> tuple[date, date, bool]:
    """追補v4§2.4A: 分足のバー数上限ガード(目安5,000本)。

    1分=3日/5分=15日/15分=45日を超える選択期間の場合、終了日から遡ってguard_days分だけに
    チャート表示窓を短縮する(セッション集計は常に1h足で選択期間全体を対象にしており不変)。
    interval_choice がINTRADAY_WINDOW_GUARD_DAYSに無い(=1h/4h/日足)場合は素通しする。
    戻り値: (実効開始日, 実効終了日, 短縮したか)。
    """
    guard_days = INTRADAY_WINDOW_GUARD_DAYS.get(interval_choice)
    if guard_days is None:
        return start_date_, end_date_, False
    span_days = (end_date_ - start_date_).days + 1
    if span_days <= guard_days:
        return start_date_, end_date_, False
    eff_start = end_date_ - timedelta(days=guard_days - 1)
    return eff_start, end_date_, True


def resolve_intraday_effective_choice(source: str, nominal_choice: str) -> tuple[str, Optional[str]]:
    """追補v4§2.4A: yfinance銘柄は1分足非対応のため、1分足選択時は15分足へ自動フォールバックする
    (ccxt暗号銘柄のみ1分足対応)。5分足/15分足はyfinanceでも対応のためフォールバック不要。
    戻り値: (実効足選択ラベル, フォールバック理由メモ or None)。
    """
    if source == "yfinance" and nominal_choice == "1分足(暗号のみ)":
        return "15分足", "yfinance銘柄は1分足非対応のため15分足で表示します。"
    return nominal_choice, None


def any_ccxt_selected(labels: list[str], custom_map: Optional[dict[str, dict[str, Any]]] = None) -> bool:
    """選択銘柄群にccxt(暗号)経由のものが1つでも含まれるか判定する(1分足の選択可否判定に使う)。"""
    return any(get_symbol_routing(label, custom_map).get("source") == "ccxt" for label in labels)


def trim_ohlcv_to_jst_range(df: pd.DataFrame, start_date_: date, end_date_: date) -> pd.DataFrame:
    """indexがtz-aware(Asia/Tokyo)なOHLCVを、JST基準で[start_date_ 00:00, end_date_翌日 00:00)に厳密に絞る。

    yfinanceのyf.download(start=, end=)は「銘柄の取引所ローカルタイムゾーン」の暦日境界で
    解釈されるため(例: GC=F/NQ=F/ES=F/NIY=Fは全てAmerica/New_York基準)、JSTへtz_convertした
    だけでは指定期間の前後に最大十数時間分のズレ(欠落 or スピルオーバー)が生じる。
    ccxt側は_jst_date_to_utc_msでJST日付境界を明示的に使っているため、この関数で同じ挙動に揃える。
    """
    start_ts = pd.Timestamp(start_date_, tz="Asia/Tokyo")
    end_ts = pd.Timestamp(end_date_ + timedelta(days=1), tz="Asia/Tokyo")
    return df[(df.index >= start_ts) & (df.index < end_ts)]


def build_session_background_shapes(
    start_ts: pd.Timestamp, end_ts: pd.Timestamp, opacity: float = BG_OPACITY,
) -> list[dict[str, Any]]:
    """§2.2 セッション背景帯のshape辞書リストを構築する(add_vrectループを使わず一括生成、
    R2実証: add_vrectはO(n^2)で360本規模で48秒かかるが、shape辞書の直接構築+update_layoutなら0.17秒)。

    追補v3§3: opacityはサイドバーの「背景帯の濃さ」スライダー(5〜70%)から渡される(既定0.25)。
    """
    if start_ts is None or end_ts is None or start_ts >= end_ts:
        return []
    start_hour = start_ts.floor("h")
    end_hour = end_ts.ceil("h")
    hours = pd.date_range(start_hour, end_hour, freq="1h", tz=start_ts.tz)
    if len(hours) < 2:
        return []

    shapes: list[dict[str, Any]] = []
    seg_start = hours[0]
    seg_group = _band_group_for_bg(hours[0].hour)
    for h in hours[1:]:
        g = _band_group_for_bg(h.hour)
        if g != seg_group:
            shapes.append(_make_bg_shape(seg_start, h, seg_group, opacity))
            seg_start = h
            seg_group = g
    shapes.append(_make_bg_shape(seg_start, hours[-1], seg_group, opacity))
    return shapes


def _make_bg_shape(x0: pd.Timestamp, x1: pd.Timestamp, group: str, opacity: float = BG_OPACITY) -> dict[str, Any]:
    r, g, b = BG_GROUP_COLOR_RGB[group]
    return {
        "type": "rect",
        "xref": "x",
        "yref": "paper",
        "x0": x0,
        "x1": x1,
        "y0": 0,
        "y1": 1,
        "fillcolor": f"rgba({r},{g},{b},{opacity})",
        "line_width": 0,
        "layer": "below",
    }


def match_trade_symbol(trade_symbol: str, display_label: str, ticker: str) -> bool:
    """トレードCSVのSymbol欄が、選択中の銘柄(表示ラベル or ティッカー)と一致するかを緩く判定する。"""
    def _norm(s: str) -> str:
        return "".join(ch for ch in str(s).upper() if ch.isalnum())
    ts = _norm(trade_symbol)
    dl = _norm(display_label)
    tk = _norm(ticker)
    if not ts:
        return False
    return ts == dl or ts == tk or (dl and dl in ts) or (ts in dl) or (tk and ts in tk) or (tk and tk in ts)


_MAP_TRADES_RESULT_COLUMNS = [
    "entry_time", "entry_price", "entry_price_is_approx",
    "exit_time", "exit_price", "exit_price_is_approx",
    "side", "leverage", "pnl_usd", "pnl_percent", "win_loss", "holding_minutes",
]


def _asof_close_price(index: pd.DatetimeIndex, close: pd.Series, ts: pd.Timestamp) -> float:
    """indexの中で ts 時点までに確定していた直近の足のclose値を返す(価格フォールバック用)。
    ts が全足より前の場合は先頭足のcloseで代用する(近似である旨は呼び出し側でフラグ管理)。
    """
    if pd.isna(ts) or len(index) == 0:
        return float("nan")
    pos = int(index.searchsorted(ts, side="right")) - 1
    if pos < 0:
        pos = 0
    elif pos >= len(index):
        pos = len(index) - 1
    return float(close.iloc[pos])


def map_trades_to_chart(
    trades_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    normalize_base: Optional[float] = None,
) -> pd.DataFrame:
    """正規化済みトレードDataFrame(単一銘柄分。呼び出し側でmatch_trade_symbol等により事前に
    絞り込み済みを想定)を、チャート(ohlcv_df)上に描画するための座標へ変換する純関数。追補仕様書§2.2/§2.3。

    - 期間フィルタ: Entry_Timeがohlcv_df.indexの範囲([min, max])外のトレードは除外する。
    - 価格フォールバック: Entry_Price/Exit_Priceが無い(NaN)場合、その時点で確定していた直近の足の
      close値で近似する(is_approx=Trueで返す。実価格がある場合はFalse)。
    - 正規化変換: normalize_baseが指定されていれば 価格 = 価格 / normalize_base * 100 に変換する
      (複数銘柄正規化重畳モード用)。Noneなら実価格(USD)のまま。
    - Exit_Time欠落トレードはexit系の列を全てNaN/NaTにする(エントリーのみ描画対象という情報を保持)。

    戻り値: DataFrame(1行=1トレード。indexは元のtrades_dfの行indexラベルを保持。
      columns=_MAP_TRADES_RESULT_COLUMNS)。trades_df/ohlcv_dfが空、または該当0件の場合は
      空DataFrame(列のみ定義)を返す。
    """
    if trades_df is None or trades_df.empty or ohlcv_df is None or ohlcv_df.empty:
        return pd.DataFrame(columns=_MAP_TRADES_RESULT_COLUMNS)

    index = ohlcv_df.index
    close = ohlcv_df["close"]
    period_start = index.min()
    period_end = index.max()

    rows = []
    for orig_idx, tr in trades_df.iterrows():
        entry_time = tr.get("Entry_Time")
        if pd.isna(entry_time) or entry_time < period_start or entry_time > period_end:
            continue  # 期間フィルタ(Entry_Time不明 or 表示期間外)

        raw_entry_price = tr.get("Entry_Price")
        if pd.notna(raw_entry_price):
            entry_price = float(raw_entry_price)
            entry_is_approx = False
        else:
            entry_price = _asof_close_price(index, close, entry_time)
            entry_is_approx = True

        exit_time = tr.get("Exit_Time")
        if pd.notna(exit_time):
            raw_exit_price = tr.get("Exit_Price")
            if pd.notna(raw_exit_price):
                exit_price = float(raw_exit_price)
                exit_is_approx = False
            else:
                exit_price = _asof_close_price(index, close, exit_time)
                exit_is_approx = True
            holding_minutes = (exit_time - entry_time).total_seconds() / 60.0
        else:
            exit_time = pd.NaT
            exit_price = float("nan")
            exit_is_approx = False
            holding_minutes = float("nan")

        if normalize_base:  # None・0はガードして実価格のまま扱う(ゼロ割回避)
            entry_price = entry_price / normalize_base * 100.0
            if pd.notna(exit_price):
                exit_price = exit_price / normalize_base * 100.0

        side_val = tr.get("Side")
        side_val = side_val if pd.notna(side_val) else None
        win_loss_val = tr.get("Win_Loss")
        win_loss_val = win_loss_val if pd.notna(win_loss_val) else None

        rows.append({
            "orig_index": orig_idx,
            "entry_time": entry_time, "entry_price": entry_price, "entry_price_is_approx": entry_is_approx,
            "exit_time": exit_time, "exit_price": exit_price, "exit_price_is_approx": exit_is_approx,
            "side": side_val, "leverage": tr.get("Leverage"),
            "pnl_usd": tr.get("PnL_USD"), "pnl_percent": tr.get("PnL_Percent"),
            "win_loss": win_loss_val, "holding_minutes": holding_minutes,
        })

    if not rows:
        return pd.DataFrame(columns=_MAP_TRADES_RESULT_COLUMNS)
    return pd.DataFrame(rows).set_index("orig_index")[_MAP_TRADES_RESULT_COLUMNS]


def format_holding_duration(minutes: Any) -> str:
    """追補v4§2.2: 保有時間(分)を「N時間M分」形式に整形する(NaN/None時は「—」)。"""
    if minutes is None or (isinstance(minutes, float) and pd.isna(minutes)) or pd.isna(minutes):
        return "—"
    m = int(round(float(minutes)))
    h, mm = divmod(max(m, 0), 60)
    return f"{h}時間{mm}分" if h > 0 else f"{mm}分"


def format_trade_select_label(
    seq: int, symbol: Any, side: Any, leverage: Any,
    entry_time: Any, exit_time: Any, pnl_usd: Any,
) -> str:
    """追補v4§2.4B: 🔍トレードズームビューのトレード選択selectbox表示文字列を組み立てる。
    例: 「#12 BTC LONG 10x 07-03 14:23→15:41 +120.5USD」(仕様書§2.4B記載の書式そのまま)。
    """
    side_s = side if (side is not None and pd.notna(side)) else "?"
    lev_s = f"{int(round(float(leverage)))}x" if (leverage is not None and pd.notna(leverage)) else "—"
    entry_s = entry_time.strftime("%m-%d %H:%M") if (entry_time is not None and pd.notna(entry_time)) else "?"
    exit_s = exit_time.strftime("%H:%M") if (exit_time is not None and pd.notna(exit_time)) else "(未決済)"
    pnl_s = f"{float(pnl_usd):+.1f}USD" if (pnl_usd is not None and pd.notna(pnl_usd)) else "—"
    return f"#{seq} {symbol} {side_s} {lev_s} {entry_s}→{exit_s} {pnl_s}"


def build_zoom_trade_options(trades_df: pd.DataFrame) -> list[tuple[str, Any]]:
    """追補v4§2.4B: 🔍トレードズームビューのトレード選択selectbox用の選択肢一覧を構築する純関数。
    Entry_Time昇順に#1から連番を振り、format_trade_select_labelで表示文字列を組み立てる。
    戻り値: [(表示文字列, 元のtrades_dfのindexラベル), ...](古い順)。空/None時は空リスト。
    """
    if trades_df is None or trades_df.empty:
        return []
    ordered = trades_df.sort_values("Entry_Time")
    options: list[tuple[str, Any]] = []
    for seq, (orig_idx, row) in enumerate(ordered.iterrows(), start=1):
        label = format_trade_select_label(
            seq, row.get("Symbol"), row.get("Side"), row.get("Leverage"),
            row.get("Entry_Time"), row.get("Exit_Time"), row.get("PnL_USD"),
        )
        options.append((label, orig_idx))
    return options


def build_trade_overlays_for_chart(
    chart_labels: list[str],
    trade_datasets: dict[str, dict[str, Any]],
    selected_trade_labels: list[str],
    custom_map: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """追補v4§2.2: チャート表示中の各銘柄ラベルに対し、表示対象データセットのうちSymbol列が一致する
    トレードだけを抽出する(match_trade_symbolで照合。期間フィルタはmap_trades_to_chart側で別途行う)。
    戻り値: チャートlabel -> [{"dataset_label", "trades_df"(絞込み後), "color"}, ...]
    """
    overlays: dict[str, list[dict[str, Any]]] = {}
    for i, ds_label in enumerate(selected_trade_labels):
        rec = trade_datasets.get(ds_label)
        trades_df = rec.get("trades_assigned") if rec else None
        if trades_df is None or trades_df.empty or "Symbol" not in trades_df.columns:
            continue
        color = get_trade_marker_color(ds_label, i)
        for label in chart_labels:
            routing = get_symbol_routing(label, custom_map)
            ticker = routing.get("ticker", "") or ""
            mask = trades_df["Symbol"].apply(
                lambda s: match_trade_symbol(s, label, ticker) if pd.notna(s) else False
            )
            sub = trades_df[mask]
            if sub.empty:
                continue
            overlays.setdefault(label, []).append({"dataset_label": ds_label, "trades_df": sub, "color": color})
    return overlays


def compute_zoom_window(entry_time: pd.Timestamp, exit_time: Any) -> tuple[pd.Timestamp, pd.Timestamp]:
    """追補v4§2.4B: 🔍トレードズームビューの取得窓を算出する純関数。
    窓 = entry_time-2h 〜 (exit_timeがあればexit_time、無ければentry_time)+2h。
    """
    start = entry_time - pd.Timedelta(hours=2)
    end_base = exit_time if (exit_time is not None and pd.notna(exit_time)) else entry_time
    end = end_base + pd.Timedelta(hours=2)
    return start, end


def resolve_zoom_timeframe(source: str, window_hours: float, trade_age_days: float) -> tuple[str, Optional[str]]:
    """追補v4§2.4B: 🔍トレードズームビューの足種自動選択(ソース別・実測制約に基づく)。
    - 暗号(ccxt): 窓<=12h->1分足 / <=3日->5分足 / <=10日->15分足 / それ以上->1時間足。
    - yfinance: 1分足は使わない(方針確定・掟8「推測で進めない」の実測結果)。窓<=3日で
      トレードが直近ZOOM_YF_5M_AGE_LIMIT_DAYS日以内なら5分足(超過は15分足へフォールバック+理由付記)。
      窓<=10日->15分足 / それ以上->1時間足。
    戻り値: (ZOOM_TIMEFRAME_CHOICESのいずれか, フォールバック理由 or None)。
    """
    if source == "ccxt":
        if window_hours <= 12:
            return "1分足(暗号のみ)", None
        if window_hours <= 24 * 3:
            return "5分足", None
        if window_hours <= 24 * 10:
            return "15分足", None
        return "1時間足", None
    # yfinance
    if window_hours <= 24 * 3:
        if trade_age_days <= ZOOM_YF_5M_AGE_LIMIT_DAYS:
            return "5分足", None
        return (
            "15分足",
            f"5分足はYahoo Financeの実用的な遡及限界(目安{ZOOM_YF_5M_AGE_LIMIT_DAYS}日)を"
            "超えるため15分足にフォールバックしました。",
        )
    if window_hours <= 24 * 10:
        return "15分足", None
    return "1時間足", None


# 追補v6 §1: サンプルトレード生成の共通土台。2026-04-15〜2026-07-08(JST)の実市場「分単位」
# 価格(BTC/ETH/SOL=Binance spot 5分足open, HYPE=Bybit linear perp 5分足open。取得元
# data-api.binance.vision/api.bybit.com、5分刻みなので1分足openと同値)を基にrandom.seed(42)
# で決定的に選定した行データを直接埋め込む(取得・選定手順はscratchpad保管)。GOLDは分足を
# 遡及取得できないため追補v6でサンプルから廃止し、その分をBTC/ETH/SOL/HYPEへ再配分した
# (旧v5はGOLDを1時間足closeで代用しておりEntry/Exit価格が実際の約定時刻から最大59分ズレる
# 欠陥があったため=1分足ズームビューでマーカーと実ローソクの高さが最大±0.5%ズレるバグの原因)。
# 全行のEntry_Price/Exit_Priceは該当時刻の5分足openそのもの(ノイズ加工なし)。
# 列順はCSVと完全一致。generate_sample_trades/generate_sample_trades_mentorの両方で共有する。
_SAMPLE_TRADE_COLUMNS: tuple[str, ...] = (
    "Entry_Time(JST)", "Exit_Time(JST)", "Symbol", "PnL_USD", "PnL_Percent",
    "Win_Loss", "Side", "Leverage", "Entry_Price", "Exit_Price",
)


def _sample_rows_to_df(rows: list[tuple]) -> pd.DataFrame:
    """(Entry_Time, Exit_Time, Symbol, PnL_USD, PnL_Percent, Win_Loss, Side, Leverage,
    Entry_Price, Exit_Price) のタプル行をCSVと同一列順のDataFrameへ変換する。"""
    return pd.DataFrame([dict(zip(_SAMPLE_TRADE_COLUMNS, r)) for r in rows])


def generate_sample_trades() -> pd.DataFrame:
    """sample_trades.csv と全く同一の「弟子」サンプルトレードデータを生成する(固定データ、乱数不使用で完全再現)。
    追補v6: 2026-04-15〜2026-07-08(JST、約2.8ヶ月)の実市場「分単位」価格(5分足open。銘柄は
    BTC/ETH/SOL/HYPEの4種=GOLDは分足を遡及取得できないため対象外)に基づく48件+draw1件=49件。
    全9詳細帯に最低3件・全7曜日を含み、Entry/Exit_Priceは各時刻の5分足openそのもの(実売買可能
    価格と完全一致、ノイズ加工なし)。draw行(BTC 2026-04-20 20:25→20:55)も実データ上で
    Entry/Exit価格が完全一致した本物の30分保有ケースを採用している。
    ペルソナ: 勝率46〜48%・RR約0.78(平均勝ち<平均負け)。負けを欧州序盤(16-18時)・
    NY序盤(21-24時)に意図的に集中させ、「負けやすい時間帯」の可視化デモに使う。

    追補v7: このうち2件(2026-05-10 ETH・2026-05-27 ETH)は師匠側と「同局面ペア」
    (同一銘柄・エントリー時刻差30分以内)を成すよう決済時刻を短縮・実価格再アンカーした
    (元の保有時間が長くズームウィンドウを不要に広げていたため)。ペア一覧は
    generate_sample_trades_mentor の追補v7注記と selftest 18-8 を参照。
    """
    rows = [
        ("2026-04-15 16:05:00", "2026-04-16 12:55:00", "SOL", -508.75, -2.81, "loss", "SHORT", 17, 83.02, 85.35),
        ("2026-04-20 16:55:00", "2026-04-20 19:50:00", "HYPE", 31.18, 0.63, "win", "LONG", 3, 40.763, 41.021),
        ("2026-04-20 17:15:00", "2026-04-21 01:30:00", "SOL", -686.9, -0.93, "loss", "SHORT", 20, 84.76, 85.55),
        ("2026-04-20 20:25:00", "2026-04-20 20:55:00", "BTC", 0.0, 0.0, "draw", "LONG", 1, 75199.98, 75199.98),
        ("2026-04-24 07:50:00", "2026-04-25 10:45:00", "HYPE", 116.42, 0.23, "win", "SHORT", 18, 41.227, 41.132),
        ("2026-04-25 13:15:00", "2026-04-25 18:05:00", "HYPE", 127.35, 0.26, "win", "LONG", 19, 41.157, 41.264),
        ("2026-04-26 00:40:00", "2026-04-26 05:10:00", "HYPE", 441.4, 0.99, "win", "SHORT", 18, 41.546, 41.136),
        ("2026-04-26 11:45:00", "2026-04-26 22:10:00", "HYPE", 431.85, 1.07, "win", "SHORT", 20, 41.425, 40.983),
        ("2026-04-27 16:00:00", "2026-04-28 19:45:00", "BTC", 337.48, 1.4, "win", "SHORT", 17, 77700.12, 76614.61),
        ("2026-04-29 21:10:00", "2026-04-30 11:50:00", "SOL", -1138.69, -2.16, "loss", "LONG", 17, 84.58, 82.75),
        ("2026-05-01 23:20:00", "2026-05-02 06:15:00", "SOL", -636.13, -0.96, "loss", "LONG", 24, 84.34, 83.53),
        ("2026-05-02 16:30:00", "2026-05-03 04:50:00", "ETH", -62.5, -0.28, "loss", "SHORT", 6, 2305.48, 2311.96),
        ("2026-05-03 03:55:00", "2026-05-04 12:45:00", "HYPE", -220.88, -0.75, "loss", "SHORT", 10, 41.682, 41.995),
        ("2026-05-03 04:50:00", "2026-05-04 08:05:00", "HYPE", 31.52, 0.19, "win", "LONG", 11, 41.626, 41.707),
        ("2026-05-06 17:40:00", "2026-05-07 16:10:00", "BTC", -101.86, -0.29, "loss", "LONG", 12, 81663.61, 81427.9),
        ("2026-05-06 21:00:00", "2026-05-07 22:05:00", "BTC", -832.29, -1.71, "loss", "LONG", 18, 82522.88, 81108.12),
        ("2026-05-07 15:45:00", "2026-05-09 02:35:00", "ETH", -603.72, -1.01, "loss", "LONG", 22, 2336.24, 2312.74),
        ("2026-05-09 06:00:00", "2026-05-10 05:25:00", "SOL", 589.26, 1.19, "win", "LONG", 14, 92.12, 93.22),
        ("2026-05-10 09:30:00", "2026-05-10 10:40:00", "ETH", -83.03, -0.37, "loss", "LONG", 24, 2327.41, 2318.7),
        ("2026-05-11 17:20:00", "2026-05-13 04:45:00", "SOL", -382.97, -0.41, "loss", "LONG", 24, 95.09, 94.7),
        ("2026-05-13 01:25:00", "2026-05-13 07:50:00", "SOL", -232.68, -0.61, "loss", "SHORT", 15, 94.14, 94.71),
        ("2026-05-13 10:05:00", "2026-05-14 06:15:00", "BTC", 275.71, 1.6, "win", "SHORT", 16, 80739.9, 79449.19),
        ("2026-05-20 23:20:00", "2026-05-21 19:20:00", "BTC", 54.95, 0.27, "win", "LONG", 12, 77460.15, 77665.58),
        ("2026-05-21 21:30:00", "2026-05-23 03:10:00", "BTC", -56.57, -0.49, "loss", "LONG", 12, 77115.09, 76737.47),
        ("2026-05-27 13:00:00", "2026-05-27 17:30:00", "ETH", 495.99, 1.15, "win", "LONG", 19, 2070.01, 2093.89),
        ("2026-05-28 10:00:00", "2026-05-29 02:55:00", "HYPE", -917.17, -2.66, "loss", "SHORT", 20, 57.939, 59.478),
        ("2026-05-30 00:45:00", "2026-05-30 12:30:00", "ETH", 54.77, 0.56, "win", "SHORT", 5, 2031.33, 2019.92),
        ("2026-05-30 10:35:00", "2026-05-31 11:55:00", "SOL", 104.91, 0.35, "win", "LONG", 14, 82.76, 83.05),
        ("2026-06-03 11:10:00", "2026-06-04 00:10:00", "ETH", 161.48, 0.61, "win", "SHORT", 12, 1862.3, 1850.89),
        ("2026-06-06 04:55:00", "2026-06-07 08:50:00", "ETH", 65.8, 0.29, "win", "SHORT", 9, 1571.92, 1567.35),
        ("2026-06-06 06:30:00", "2026-06-07 14:05:00", "BTC", -181.69, -0.39, "loss", "SHORT", 19, 61578.01, 61819.04),
        ("2026-06-07 21:05:00", "2026-06-08 02:55:00", "BTC", -192.5, -0.73, "loss", "LONG", 9, 62631.21, 62171.34),
        ("2026-06-09 22:45:00", "2026-06-10 16:20:00", "BTC", -307.49, -1.23, "loss", "LONG", 13, 62433.99, 61668.39),
        ("2026-06-11 11:55:00", "2026-06-11 22:20:00", "BTC", 234.63, 1.43, "win", "LONG", 7, 62112.53, 62998.86),
        ("2026-06-14 00:50:00", "2026-06-14 12:35:00", "HYPE", 216.5, 1.28, "win", "LONG", 6, 59.864, 60.63),
        ("2026-06-15 12:05:00", "2026-06-16 16:20:00", "BTC", 185.54, 1.35, "win", "LONG", 8, 65553.99, 66440.57),
        ("2026-06-16 19:25:00", "2026-06-16 23:15:00", "SOL", 216.4, 1.64, "win", "SHORT", 7, 75.0, 73.77),
        ("2026-06-20 06:35:00", "2026-06-20 22:50:00", "ETH", -210.88, -0.88, "loss", "SHORT", 12, 1704.44, 1719.52),
        ("2026-06-21 16:50:00", "2026-06-21 20:15:00", "BTC", 225.21, 0.29, "win", "LONG", 21, 64234.69, 64422.0),
        ("2026-06-22 22:25:00", "2026-06-23 06:00:00", "BTC", -322.8, -1.04, "loss", "LONG", 14, 65107.84, 64433.8),
        ("2026-06-24 16:10:00", "2026-06-25 12:10:00", "BTC", -463.18, -3.02, "loss", "LONG", 7, 62712.0, 60821.12),
        ("2026-06-27 16:10:00", "2026-06-28 14:15:00", "SOL", -341.53, -2.03, "loss", "LONG", 6, 72.01, 70.55),
        ("2026-06-27 22:35:00", "2026-06-29 05:35:00", "HYPE", -1277.72, -1.94, "loss", "LONG", 18, 63.426, 62.194),
        ("2026-06-27 23:15:00", "2026-06-28 09:15:00", "BTC", -577.01, -0.88, "loss", "LONG", 17, 60670.6, 60137.3),
        ("2026-06-30 10:05:00", "2026-07-01 03:35:00", "SOL", 484.52, 1.56, "win", "SHORT", 21, 74.35, 73.19),
        ("2026-07-03 14:00:00", "2026-07-04 10:20:00", "SOL", -422.9, -2.07, "loss", "SHORT", 9, 80.5, 82.17),
        ("2026-07-06 03:45:00", "2026-07-06 04:20:00", "SOL", -11.52, -0.23, "loss", "LONG", 4, 81.19, 81.0),
        ("2026-07-06 23:00:00", "2026-07-07 22:15:00", "ETH", 1450.21, 1.84, "win", "LONG", 24, 1744.89, 1777.05),
        ("2026-07-07 17:00:00", "2026-07-08 18:10:00", "BTC", 655.32, 1.79, "win", "SHORT", 10, 63083.19, 61951.01),
    ]
    return _sample_rows_to_df(rows)


def generate_sample_trades_mentor() -> pd.DataFrame:
    """sample_trades_mentor.csv と全く同一の「師匠」サンプルトレードデータを生成する
    (固定データ、乱数不使用で完全再現)。追補v6: 2026-04-15〜2026-07-08(JST、約2.8ヶ月)の
    実市場「分単位」価格(5分足open。銘柄はBTC/ETH/SOL/HYPEの4種=GOLDは分足を遡及取得できない
    ため対象外。取得元はdata-api.binance.vision/api.bybit.com、random.seed(42)で決定的に選定)
    に基づく56件。全9詳細帯に最低3件・全7曜日を含み、Entry/Exit_Priceは各時刻の5分足openその
    ものである(実売買可能価格と完全一致、ノイズ加工なし)。

    弟子サンプル(generate_sample_trades)と同期間・同銘柄群・全9詳細帯に分散という条件を
    揃えつつ、56件・勝率56〜58%・RR約1.4(平均勝ち>平均負け)で、勝率/プロフィットファクター
    /損益分布が明確に良い実データを使う。師弟比較ビューの実演データ用。
    (旧v5はGOLDを含み1時間足closeで代用しておりEntry/Exit価格が実際の約定時刻から最大59分
    ズレる欠陥があったため、追補v6で弟子サンプルと同じ分単位実価格アンカー方式に統一した。)

    追補v7: ズームビューの「同局面ペア」実演のため、10件(idx 0,7,8,9,12,17,19,20,36,43)を
    弟子側トレードとの対(同一銘柄・エントリー時刻差30分以内・保有30分〜6時間)になるよう
    差し替えた(いずれも実1分足openに完全一致・PnLは実価格から再計算)。うち8件は弟子側の
    既存トレードへ、残り2件(idx36→disc idx18, idx43→disc idx24)は弟子側の決済時刻短縮と
    セットで新設。10組全てが勝敗または決済時刻30分以上のいずれかで分岐する
    (ディスカッション材料。selftest 18-8/18-8bで検証)。
    """
    rows = [
        ("2026-04-20 16:25:00", "2026-04-20 17:45:00", "HYPE", -104.94, -1.59, "loss", "LONG", 4, 41.418, 40.759),
        ("2026-04-24 05:40:00", "2026-04-25 10:20:00", "ETH", -32.19, -0.27, "loss", "LONG", 13, 2325.52, 2319.27),
        ("2026-04-24 20:35:00", "2026-04-25 07:30:00", "SOL", -114.92, -0.15, "loss", "SHORT", 23, 86.22, 86.35),
        ("2026-04-26 08:15:00", "2026-04-27 03:30:00", "HYPE", 31.91, 0.45, "win", "LONG", 6, 41.421, 41.606),
        ("2026-04-26 18:50:00", "2026-04-27 23:35:00", "ETH", 127.12, 0.74, "win", "SHORT", 6, 2332.9, 2315.61),
        ("2026-04-27 04:15:00", "2026-04-27 09:40:00", "ETH", -8.74, -0.15, "loss", "SHORT", 2, 2365.65, 2369.22),
        ("2026-04-27 07:10:00", "2026-04-27 20:55:00", "BTC", 266.45, 0.91, "win", "SHORT", 24, 78536.23, 77818.01),
        ("2026-04-20 20:00:00", "2026-04-21 00:30:00", "BTC", 69.84, 0.97, "win", "LONG", 3, 75027.26, 75754.39),
        ("2026-06-21 16:20:00", "2026-06-21 17:50:00", "BTC", -172.8, -0.48, "loss", "LONG", 12, 64252.0, 63945.64),
        ("2026-04-25 13:30:00", "2026-04-25 16:25:00", "HYPE", 74.4, 0.15, "win", "LONG", 16, 41.117, 41.179),
        ("2026-04-29 22:50:00", "2026-05-01 06:30:00", "BTC", 291.65, 0.35, "win", "SHORT", 21, 76605.45, 76337.5),
        ("2026-05-01 04:20:00", "2026-05-01 16:10:00", "BTC", 491.3, 0.85, "win", "LONG", 25, 76435.4, 77088.86),
        ("2026-04-26 00:30:00", "2026-04-26 01:40:00", "HYPE", 495.6, 1.77, "win", "SHORT", 10, 41.622, 40.884),
        ("2026-05-02 09:15:00", "2026-05-03 15:20:00", "ETH", 52.7, 0.31, "win", "LONG", 8, 2294.09, 2301.25),
        ("2026-05-05 00:55:00", "2026-05-05 19:55:00", "SOL", -175.33, -0.33, "loss", "SHORT", 21, 84.49, 84.77),
        ("2026-05-05 09:45:00", "2026-05-05 11:15:00", "SOL", -92.31, -0.4, "loss", "SHORT", 22, 84.25, 84.59),
        ("2026-05-05 09:55:00", "2026-05-06 01:45:00", "SOL", 20.4, 0.94, "win", "LONG", 2, 84.34, 85.13),
        ("2026-06-07 21:35:00", "2026-06-08 02:15:00", "BTC", 166.4, 0.8, "win", "LONG", 8, 61790.21, 62287.45),
        ("2026-05-07 13:30:00", "2026-05-08 20:30:00", "BTC", -183.22, -0.78, "loss", "LONG", 10, 80933.34, 80300.0),
        ("2026-06-16 19:30:00", "2026-06-16 21:25:00", "SOL", 42.56, 0.16, "win", "SHORT", 14, 75.04, 74.92),
        ("2026-07-06 04:05:00", "2026-07-06 07:25:00", "SOL", 241.74, 1.58, "win", "LONG", 9, 81.0, 82.28),
        ("2026-05-11 05:30:00", "2026-05-11 16:40:00", "BTC", -101.62, -0.51, "loss", "LONG", 9, 81151.87, 80736.63),
        ("2026-05-12 09:40:00", "2026-05-12 12:35:00", "BTC", -218.73, -0.46, "loss", "LONG", 17, 81564.58, 81190.36),
        ("2026-05-13 09:20:00", "2026-05-14 14:55:00", "BTC", 451.05, 0.94, "win", "SHORT", 16, 80607.99, 79853.16),
        ("2026-05-18 18:10:00", "2026-05-19 02:45:00", "ETH", -173.76, -0.5, "loss", "LONG", 12, 2117.96, 2107.46),
        ("2026-05-19 23:55:00", "2026-05-20 07:25:00", "SOL", -15.12, -0.15, "loss", "SHORT", 7, 83.99, 84.12),
        ("2026-05-20 18:50:00", "2026-05-20 23:05:00", "SOL", 13.78, 0.24, "win", "SHORT", 7, 84.91, 84.71),
        ("2026-05-21 12:40:00", "2026-05-22 03:20:00", "BTC", -62.0, -0.23, "loss", "LONG", 23, 78003.24, 77822.0),
        ("2026-05-21 16:05:00", "2026-05-22 23:55:00", "SOL", 80.77, 0.42, "win", "LONG", 10, 86.27, 86.63),
        ("2026-05-21 21:45:00", "2026-05-22 00:10:00", "BTC", 87.72, 0.35, "win", "SHORT", 13, 77243.73, 76974.69),
        ("2026-05-24 07:35:00", "2026-05-25 02:55:00", "SOL", 25.84, 0.42, "win", "SHORT", 4, 86.06, 85.7),
        ("2026-05-30 13:30:00", "2026-05-31 18:15:00", "BTC", -280.08, -0.74, "loss", "SHORT", 12, 73347.3, 73889.99),
        ("2026-05-31 11:30:00", "2026-05-31 12:05:00", "HYPE", -165.51, -0.7, "loss", "SHORT", 23, 68.155, 68.63),
        ("2026-05-31 17:40:00", "2026-06-01 04:05:00", "BTC", 75.92, 0.38, "win", "SHORT", 12, 73886.02, 73602.47),
        ("2026-06-01 05:00:00", "2026-06-01 17:15:00", "SOL", 86.39, 1.02, "win", "SHORT", 5, 81.59, 80.76),
        ("2026-06-03 01:40:00", "2026-06-03 17:40:00", "BTC", 464.55, 0.93, "win", "SHORT", 14, 67610.01, 66979.18),
        ("2026-05-10 09:15:00", "2026-05-10 10:40:00", "ETH", 125.28, 0.36, "win", "SHORT", 12, 2327.07, 2318.7),
        ("2026-06-09 20:25:00", "2026-06-11 00:05:00", "BTC", -177.91, -0.68, "loss", "LONG", 9, 62606.23, 62180.0),
        ("2026-06-10 05:45:00", "2026-06-10 07:25:00", "SOL", -72.86, -0.59, "loss", "LONG", 13, 65.61, 65.22),
        ("2026-06-11 18:40:00", "2026-06-11 21:35:00", "BTC", -67.78, -0.27, "loss", "LONG", 16, 62923.01, 62753.3),
        ("2026-06-11 19:50:00", "2026-06-12 21:50:00", "ETH", 47.98, 0.55, "win", "LONG", 3, 1658.22, 1667.33),
        ("2026-06-16 16:05:00", "2026-06-17 05:50:00", "BTC", 360.73, 0.87, "win", "SHORT", 17, 66408.0, 65830.01),
        ("2026-06-16 19:15:00", "2026-06-17 03:05:00", "BTC", -487.15, -0.66, "loss", "LONG", 22, 66540.02, 66102.01),
        ("2026-05-27 13:10:00", "2026-05-27 13:45:00", "ETH", -132.6, -0.34, "loss", "LONG", 15, 2070.4, 2063.44),
        ("2026-06-20 10:55:00", "2026-06-21 04:00:00", "BTC", -182.57, -0.35, "loss", "SHORT", 14, 63583.99, 63805.9),
        ("2026-06-26 10:20:00", "2026-06-26 20:10:00", "ETH", 516.71, 0.95, "win", "SHORT", 15, 1565.42, 1550.5),
        ("2026-06-27 12:55:00", "2026-06-28 16:10:00", "BTC", 40.9, 0.26, "win", "SHORT", 6, 60258.56, 60101.27),
        ("2026-06-28 03:00:00", "2026-06-29 01:35:00", "HYPE", 348.35, 0.87, "win", "SHORT", 14, 63.047, 62.5),
        ("2026-06-28 08:20:00", "2026-06-29 13:30:00", "BTC", -51.71, -0.16, "loss", "LONG", 17, 60050.0, 59955.13),
        ("2026-06-28 09:45:00", "2026-06-29 12:20:00", "BTC", 121.38, 0.29, "win", "SHORT", 17, 60176.87, 60003.72),
        ("2026-07-01 01:35:00", "2026-07-02 02:35:00", "HYPE", 197.26, 0.44, "win", "SHORT", 12, 64.385, 64.1),
        ("2026-07-03 07:45:00", "2026-07-03 12:35:00", "SOL", 23.78, 0.36, "win", "LONG", 5, 80.55, 80.84),
        ("2026-07-03 07:50:00", "2026-07-04 01:30:00", "SOL", 308.84, 0.99, "win", "LONG", 11, 80.54, 81.34),
        ("2026-07-05 20:05:00", "2026-07-06 22:40:00", "HYPE", -180.63, -0.74, "loss", "SHORT", 7, 68.52, 69.03),
        ("2026-07-06 00:35:00", "2026-07-07 06:00:00", "ETH", 38.15, 0.94, "win", "LONG", 2, 1776.31, 1793.04),
        ("2026-07-08 14:40:00", "2026-07-08 21:55:00", "HYPE", -18.91, -0.18, "loss", "SHORT", 13, 68.082, 68.205),
    ]
    return _sample_rows_to_df(rows)


# =====================================================================================
# データ取得層 (@st.cache_data(ttl=900))
# =====================================================================================

_EXCHANGE_CACHE: dict[str, Any] = {}


def _get_exchange(exchange_id: str):
    if exchange_id not in _EXCHANGE_CACHE:
        import ccxt  # 遅延import
        klass = getattr(ccxt, exchange_id)
        kwargs: dict[str, Any] = {"enableRateLimit": True}
        if exchange_id == "binance":
            # 現物のみロード: 既定ではload_marketsが先物(fapi.binance.com)も叩き、
            # 米国ホスティングでは先物側だけ451で全体が失敗する(2026-07-12クラウド実機で確認)。
            # 本アプリはbinance現物しか使わないため機能損失なし。
            kwargs["options"] = {"defaultType": "spot", "fetchMarkets": ["spot"]}
        ex = klass(kwargs)
        if exchange_id == "binance":
            # 米国ホスティング(Streamlit Cloud等)ではapi.binance.comがHTTP 451で地域ブロック
            # される(2026-07-12クラウド実機で確認)。Binance公式の公開データミラー
            # data-api.binance.vision(市場データ専用・地域制限なし)へ公開エンドポイントを
            # 差し替える。klines/exchangeInfoとも同一データで全環境無害。
            try:
                if isinstance(ex.urls.get("api"), dict) and "public" in ex.urls["api"]:
                    ex.urls["api"]["public"] = "https://data-api.binance.vision/api/v3"
            except Exception:  # noqa: BLE001 — ccxtのURL構造が変わっても本体動作は損なわない
                pass
        _EXCHANGE_CACHE[exchange_id] = ex
    return _EXCHANGE_CACHE[exchange_id]


def _jst_date_to_utc_ms(d: date) -> int:
    ts = pd.Timestamp(d, tz="Asia/Tokyo").tz_convert("UTC")
    return int(ts.value // 10**6)


def _fetch_ccxt_ohlcv_impl(
    exchange_id: str, symbol: str, since_ms: int, until_ms: int, timeframe: str = "1h",
) -> pd.DataFrame:
    try:
        return _fetch_ccxt_ohlcv_loop(exchange_id, symbol, since_ms, until_ms, timeframe)
    except Exception as e:  # noqa: BLE001
        # bybitも米国ホスティングから地域ブロックされ得る。HYPE等はHyperliquid本体の
        # 公開API(地域制限なし)へフォールバック(USDT表記→USDC建てperpへ読み替え)。
        # 取得元の正直な表示のためdf.attrs["actual_source"]に実際の取得元を記録する。
        msg = str(e).lower()
        geo = any(m in msg for m in ("451", "403", "restricted", "cloudfront", "eligibility"))
        if exchange_id == "bybit" and geo:
            hl_symbol = symbol.split("/")[0] + "/USDC:USDC"
            df = _fetch_ccxt_ohlcv_loop("hyperliquid", hl_symbol, since_ms, until_ms, timeframe)
            df.attrs["actual_source"] = "ccxt(hyperliquid)"
            return df
        raise


def _fetch_ccxt_ohlcv_loop(
    exchange_id: str, symbol: str, since_ms: int, until_ms: int, timeframe: str = "1h",
) -> pd.DataFrame:
    ex = _get_exchange(exchange_id)
    all_rows: list[list[float]] = []
    seen_ts: set[int] = set()
    cursor = since_ms
    max_iter = 500
    it = 0
    while cursor < until_ms and it < max_iter:
        it += 1
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not batch:
            break
        new_rows = [b for b in batch if b[0] not in seen_ts and b[0] < until_ms]
        for b in new_rows:
            seen_ts.add(b[0])
        all_rows.extend(new_rows)
        last_ts = batch[-1][0]
        if last_ts <= cursor:
            break
        cursor = last_ts + 1
    if not all_rows:
        raise ValueError(f"{symbol}({exchange_id}): 指定期間のデータが取得できませんでした")
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("dt").drop(columns=["ts"])
    df.index = df.index.tz_convert("Asia/Tokyo")
    df.index.name = None
    return df[["open", "high", "low", "close", "volume"]]


def _fetch_yfinance_ohlcv_impl(
    ticker: str, start_date_: date, end_date_: date, interval: str = "1h",
) -> pd.DataFrame:
    import yfinance as yf  # 遅延import
    end_plus = end_date_ + timedelta(days=1)
    df = yf.download(
        ticker, start=start_date_.isoformat(), end=end_plus.isoformat(),
        interval=interval, auto_adjust=False, progress=False,
    )
    if df is None or df.empty:
        raise ValueError(f"{ticker}: 指定期間のデータが取得できませんでした")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.columns = [str(c).lower() for c in df.columns]
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Tokyo")
    df.index.name = None
    df = df.sort_index()
    # yfinanceのstart/endは銘柄の取引所ローカルTZの暦日境界で解釈されるため、tz_convertしただけでは
    # JST基準の指定期間とズレる。ccxt側(_jst_date_to_utc_ms)と同じくJST日付境界で厳密に絞り直す。
    df = trim_ohlcv_to_jst_range(df, start_date_, end_date_)
    if df.empty:
        raise ValueError(f"{ticker}: JST基準の指定期間内にデータがありませんでした")
    return df


def _build_fetch_meta(source: str, ticker: str, df: pd.DataFrame) -> dict[str, Any]:
    """追補v3§4: フェッチ結果のメタ情報を組み立てる。fetched_atはこの関数が呼ばれた瞬間
    (=キャッシュmiss時の実フェッチ実行時)のJST時刻であり、cache hit時は再実行されないため
    st.cache_dataのメモ化によって「本当に取得した時刻」がそのまま保持される。"""
    return {
        "source": source,
        "ticker": ticker,
        "fetched_at": datetime.now(JST),
        "last_bar": df.index[-1] if not df.empty else None,
        "rows": int(len(df)),
    }


@st.cache_data(ttl=HOURLY_CACHE_TTL_SEC, show_spinner=False)
def fetch_ccxt_ohlcv(
    exchange_id: str, symbol: str, since_ms: int, until_ms: int, timeframe: str = "1h",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = _fetch_ccxt_ohlcv_impl(exchange_id, symbol, since_ms, until_ms, timeframe)
    # README §14/追補v3§4: 取得元表示は `ccxt(binance)` のように取引所IDをラップする仕様。
    # 地域ブロックでフォールバックした場合はattrsの実取得元を表示(取得元の正直表示)。
    return df, _build_fetch_meta(df.attrs.get("actual_source", f"ccxt({exchange_id})"), symbol, df)


@st.cache_data(ttl=HOURLY_CACHE_TTL_SEC, show_spinner=False)
def fetch_yfinance_ohlcv(
    ticker: str, start_date_: date, end_date_: date, interval: str = "1h",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = _fetch_yfinance_ohlcv_impl(ticker, start_date_, end_date_, interval)
    return df, _build_fetch_meta("yfinance", ticker, df)


# ---- 追補v4§2.4A: 分足専用フェッチ(ttlが1h/1d系と異なるため関数を分ける) -------------------
@st.cache_data(ttl=MINUTE_CACHE_TTL_SEC, show_spinner=False)
def fetch_ccxt_ohlcv_1m(
    exchange_id: str, symbol: str, since_ms: int, until_ms: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = _fetch_ccxt_ohlcv_impl(exchange_id, symbol, since_ms, until_ms, timeframe="1m")
    return df, _build_fetch_meta(df.attrs.get("actual_source", f"ccxt({exchange_id})"), symbol, df)


@st.cache_data(ttl=FINE_CACHE_TTL_SEC, show_spinner=False)
def fetch_ccxt_ohlcv_fine(
    exchange_id: str, symbol: str, since_ms: int, until_ms: int, timeframe: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = _fetch_ccxt_ohlcv_impl(exchange_id, symbol, since_ms, until_ms, timeframe)
    return df, _build_fetch_meta(df.attrs.get("actual_source", f"ccxt({exchange_id})"), symbol, df)


@st.cache_data(ttl=FINE_CACHE_TTL_SEC, show_spinner=False)
def fetch_yfinance_ohlcv_fine(
    ticker: str, start_date_: date, end_date_: date, interval: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = _fetch_yfinance_ohlcv_impl(ticker, start_date_, end_date_, interval)
    return df, _build_fetch_meta("yfinance", ticker, df)


def get_symbol_routing(label: str, custom_map: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
    if label in SYMBOL_MASTER:
        return SYMBOL_MASTER[label]
    if custom_map and label in custom_map:
        return custom_map[label]
    # ルーティング規則: '/'を含む -> ccxt(binance)、それ以外 -> yfinance
    if "/" in label:
        return {"source": "ccxt", "exchange": "binance", "ticker": label}
    return {"source": "yfinance", "ticker": label}


def any_yfinance_selected(labels: list[str], custom_map: Optional[dict[str, dict[str, Any]]] = None) -> bool:
    """選択銘柄群にyfinance経由のものが1つでも含まれるか判定する。

    yfinanceの730日クランプはyfinance経由の銘柄にのみ本来必要な制約であり、ccxt(binance/bybit)
    経由の銘柄には無関係のため、クランプ要否の判定にこの関数を使う。
    """
    return any(get_symbol_routing(label, custom_map).get("source") == "yfinance" for label in labels)


def load_symbol_data(
    label: str, routing: dict[str, Any], start_date_: date, end_date_: date,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if routing["source"] == "ccxt":
        since_ms = _jst_date_to_utc_ms(start_date_)
        until_ms = _jst_date_to_utc_ms(end_date_ + timedelta(days=1))
        return fetch_ccxt_ohlcv(routing["exchange"], routing["ticker"], since_ms, until_ms)
    else:
        return fetch_yfinance_ohlcv(routing["ticker"], start_date_, end_date_)


def load_symbol_data_daily(
    label: str, routing: dict[str, Any], start_date_: date, end_date_: date,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """追補§2.1: 月別アノマリー集計用に日足('1d')でOHLCVを取得する(load_symbol_dataの日足版)。"""
    if routing["source"] == "ccxt":
        since_ms = _jst_date_to_utc_ms(start_date_)
        until_ms = _jst_date_to_utc_ms(end_date_ + timedelta(days=1))
        return fetch_ccxt_ohlcv(routing["exchange"], routing["ticker"], since_ms, until_ms, timeframe="1d")
    else:
        return fetch_yfinance_ohlcv(routing["ticker"], start_date_, end_date_, interval="1d")


def load_symbol_data_intraday(
    label: str, routing: dict[str, Any], start_date_: date, end_date_: date, nominal_choice: str,
) -> tuple[pd.DataFrame, dict[str, Any], Optional[str], int]:
    """追補v4§2.4A: 分足(1m/5m/15m)専用のフェッチディスパッチ。

    戻り値: (df, meta, フォールバック理由メモ(ラベル付き) or None, キャッシュ分(caption表示用))。
    """
    effective_choice, fallback_reason = resolve_intraday_effective_choice(routing["source"], nominal_choice)
    tf = INTRADAY_INTERVAL_MAP[effective_choice]
    ttl_min = MINUTE_CACHE_TTL_MIN if tf == "1m" else FINE_CACHE_TTL_MIN
    if routing["source"] == "ccxt":
        since_ms = _jst_date_to_utc_ms(start_date_)
        until_ms = _jst_date_to_utc_ms(end_date_ + timedelta(days=1))
        if tf == "1m":
            df, meta = fetch_ccxt_ohlcv_1m(routing["exchange"], routing["ticker"], since_ms, until_ms)
        else:
            df, meta = fetch_ccxt_ohlcv_fine(routing["exchange"], routing["ticker"], since_ms, until_ms, tf)
    else:
        df, meta = fetch_yfinance_ohlcv_fine(routing["ticker"], start_date_, end_date_, tf)
    note = f"{label}: {fallback_reason}" if fallback_reason else None
    return df, meta, note, ttl_min


def load_intraday_chart_bundle(
    labels: list[str], custom_map: dict[str, dict[str, Any]], start_date_: date, end_date_: date,
    nominal_choice: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, Any]], list[str], list[str], dict[str, int]]:
    """複数銘柄の分足OHLCVを取得する(load_all_symbol_dataの分足版)。

    戻り値: (label->df, label->メタ, エラー一覧, フォールバック理由一覧, label->キャッシュ分)。
    """
    data: dict[str, pd.DataFrame] = {}
    meta: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    fallback_notes: list[str] = []
    ttl_by_label: dict[str, int] = {}
    for label in labels:
        routing = get_symbol_routing(label, custom_map)
        try:
            df, m, note, ttl_min = load_symbol_data_intraday(label, routing, start_date_, end_date_, nominal_choice)
            if df is None or df.empty:
                errors.append(f"{label}: データが空でした")
                continue
            data[label] = df
            meta[label] = m
            ttl_by_label[label] = ttl_min
            if note:
                fallback_notes.append(note)
        except Exception as e:  # noqa: BLE001 - ユーザー向けに変換して継続するため意図的に広く捕捉
            errors.append(f"{label}: 取得失敗 ({e})")
    return data, meta, errors, fallback_notes, ttl_by_label


def load_all_symbol_data(
    labels: list[str], custom_map: dict[str, dict[str, Any]], start_date_: date, end_date_: date,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, Any]], list[str]]:
    """複数銘柄のOHLCVを取得する。銘柄ごとにtry/exceptし、失敗銘柄はエラーメッセージに積んで続行する。

    戻り値: (label -> OHLCV, label -> 追補v3§4のフェッチメタ{source/ticker/fetched_at/last_bar/rows}, エラー一覧)。
    """
    data: dict[str, pd.DataFrame] = {}
    meta: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for label in labels:
        routing = get_symbol_routing(label, custom_map)
        try:
            df, m = load_symbol_data(label, routing, start_date_, end_date_)
            if df is None or df.empty:
                errors.append(f"{label}: データが空でした")
                continue
            data[label] = df
            meta[label] = m
        except Exception as e:  # noqa: BLE001 - ユーザー向けに変換して継続するため意図的に広く捕捉
            errors.append(f"{label}: 取得失敗 ({e})")
    return data, meta, errors


@st.cache_data(ttl=DAILY_CACHE_TTL_SEC, show_spinner=False)
def fetch_daily_bundle(
    labels: list[str], lookback_years: int, end_date_: date,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, Any]], list[str]]:
    """追補§2.1/§2.2: 月別アノマリー集計用に、指定銘柄群の日足OHLCVを直近lookback_years年分取得する。

    銘柄ごとにtry/exceptし、1銘柄の失敗が他銘柄取得を止めないようにする(load_all_symbol_dataと同方針)。
    戻り値: (label -> 日足OHLCV, label -> メタ情報dict{start/end/n_rows=取得実期間、
    追補v3§4のsource/ticker/fetched_at/last_bar/rowsも含む}, エラーメッセージ一覧)。
    (labels, lookback_years, end_date_) が同一の呼び出しは1時間キャッシュする(月別集計は頻繁な再取得が
    不要なためセッション/曜日集計のttl=900より長いttl=3600)。

    end_date_ は呼び出し側が明示的に渡す(clamp_start_date_for_yfinanceのtoday_引数と同じ規約=
    日付依存の処理はテスト容易性・キャッシュ純度のため関数内部でdate.today()を呼ばずに引数化する)。
    """
    data: dict[str, pd.DataFrame] = {}
    meta: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if not labels:
        return data, meta, errors
    start_date_ = (pd.Timestamp(end_date_) - pd.DateOffset(years=lookback_years)).date()
    for label in labels:
        routing = get_symbol_routing(label)
        try:
            df, fetch_meta = load_symbol_data_daily(label, routing, start_date_, end_date_)
            if df is None or df.empty:
                errors.append(f"{label}: データが空でした")
                continue
            data[label] = df
            meta[label] = {
                **fetch_meta,
                "start": df.index[0].date().isoformat(),
                "end": df.index[-1].date().isoformat(),
                "n_rows": int(len(df)),
            }
        except Exception as e:  # noqa: BLE001 - ユーザー向けに変換して継続するため意図的に広く捕捉
            errors.append(f"{label}: 取得失敗 ({e})")
    return data, meta, errors


# =====================================================================================
# 描画関数群 (Plotly figure生成は純関数、表示はUI関数側)
# =====================================================================================


def _trade_marker_symbol(side: Any) -> str:
    """追補v4§2.2: ▲LONG/▼SHORT/◆不明のPlotlyマーカーシンボル名。"""
    if side == "LONG":
        return "triangle-up"
    if side == "SHORT":
        return "triangle-down"
    return "diamond"


def _trade_side_label(side: Any) -> str:
    if side == "LONG":
        return "LONG"
    if side == "SHORT":
        return "SHORT"
    return "不明"


def _trade_wl_key(win_loss: Any) -> str:
    """win_loss値をwin/loss/draw(不明含む)の3値へ正規化する(接続点線の色分けグループ化用)。"""
    return win_loss if win_loss in ("win", "loss") else "draw"


def _trade_risk_labels(r: pd.Series, ohlcv_df: pd.DataFrame) -> tuple[str, str, str]:
    """v6.5: マーカーhover用リスク指標(証拠金推定/最大逆行MAE/実績RR)の表示文字列3点。
    - 証拠金(推定) = |PnL_USD| ÷ (|PnL_Percent|/100 × レバ)。CSVに証拠金列が無いため
      PnLとレバから逆算する(PnL_Percent=0や欠損時は"—")。
    - 最大逆行(MAE) = 保有中に建値からどこまで逆行したか%(チャート表示中の足の高安から算出。
      LONG=期間最安値基準/SHORT=期間最高値基準。Side不明はLONG扱い。順行のみなら0.00%)。
    - RR(実績) = PnL% ÷ |MAE%| = 「取った利幅が最大逆行の何倍か」(逆行ゼロの勝ちは∞表記)。
    """
    margin = "—"
    if (pd.notna(r.get("pnl_usd")) and pd.notna(r.get("pnl_percent"))
            and pd.notna(r.get("leverage")) and float(r["leverage"]) > 0
            and abs(float(r["pnl_percent"])) > 1e-9):
        m = abs(float(r["pnl_usd"])) / (abs(float(r["pnl_percent"])) / 100.0 * float(r["leverage"]))
        margin = f"{m:,.0f} USD"
    mae_lbl, rr_lbl = "—", "—"
    et, xt, ep = r.get("entry_time"), r.get("exit_time"), r.get("entry_price")
    if pd.notna(et) and pd.notna(xt) and pd.notna(ep) and float(ep) > 0 and not ohlcv_df.empty:
        seg = ohlcv_df.loc[(ohlcv_df.index >= et) & (ohlcv_df.index <= xt)]
        if not seg.empty:
            ep_f = float(ep)
            if r.get("side") == "SHORT":
                mae = (ep_f - float(seg["high"].max())) / ep_f * 100.0
            else:
                mae = (float(seg["low"].min()) - ep_f) / ep_f * 100.0
            mae = min(mae, 0.0)
            mae_lbl = f"{mae:.2f}%"
            if pd.notna(r.get("pnl_percent")):
                if abs(mae) > 1e-9:
                    rr_lbl = f"{float(r['pnl_percent']) / abs(mae):+.2f}"
                elif float(r["pnl_percent"]) > 0:
                    rr_lbl = "∞(逆行なし)"
    return margin, mae_lbl, rr_lbl


def _add_exit_and_link_traces(fig: go.Figure, mapped: pd.DataFrame, ds_label: str, row: int,
                              ohlcv_df: Optional[pd.DataFrame] = None) -> None:
    """add_trade_overlay_traces内部処理: 決済✕マーカー+エントリー→決済の勝敗色点線を追加する
    (Exit_Time欠落トレードは対象外=エントリーのみ描画という仕様を満たす)。
    """
    em = mapped[mapped["exit_time"].notna()].copy()
    if em.empty:
        return
    em["_wlkey"] = em["win_loss"].map(_trade_wl_key)
    exit_hover = []
    for _, r in em.iterrows():
        side_lbl = _trade_side_label(r["side"])
        lev_lbl = f"{r['leverage']:.0f}x" if pd.notna(r["leverage"]) else "—"
        pnl_lbl = f"{r['pnl_usd']:+.2f}USD" if pd.notna(r["pnl_usd"]) else "—"
        pct_lbl = f"{r['pnl_percent']:+.2f}%" if pd.notna(r["pnl_percent"]) else "—"
        hold_lbl = format_holding_duration(r["holding_minutes"])
        approx = "≈近似(直近確定足の終値)" if r["exit_price_is_approx"] else "実価格"
        margin_lbl, mae_lbl, rr_lbl = _trade_risk_labels(
            r, ohlcv_df if ohlcv_df is not None else pd.DataFrame(),
        )
        exit_hover.append(
            f"[{ds_label}] {side_lbl} 決済<br>決済価格: {approx}<br>"
            f"PnL {pnl_lbl} ({pct_lbl})<br>保有時間: {hold_lbl}<br>"
            f"証拠金(推定) {margin_lbl}<br>最大逆行(MAE) {mae_lbl}｜RR実績 {rr_lbl}"
        )
    fig.add_trace(
        go.Scatter(
            x=em["exit_time"], y=em["exit_price"], mode="markers",
            marker=dict(
                symbol="x", size=[leverage_marker_size(lv) * 0.8 for lv in em["leverage"]],
                color=[WIN_LOSS_LINE_COLOR[k] for k in em["_wlkey"]],
                line=dict(width=1, color="#111111"),
            ),
            name=f"{ds_label} 決済", legendgroup=f"trades_{ds_label}",
            hovertext=exit_hover, hoverinfo="text",
        ),
        row=row, col=1,
    )
    for wl_key in ("win", "loss", "draw"):
        sub = em[em["_wlkey"] == wl_key]
        if sub.empty:
            continue
        xs: list[Any] = []
        ys: list[Any] = []
        for _, r in sub.iterrows():
            xs.extend([r["entry_time"], r["exit_time"], None])
            ys.extend([r["entry_price"], r["exit_price"], None])
        fig.add_trace(
            go.Scatter(
                x=xs, y=ys, mode="lines", line=dict(color=WIN_LOSS_LINE_COLOR[wl_key], width=1, dash="dot"),
                showlegend=False, hoverinfo="skip", legendgroup=f"trades_{ds_label}",
            ),
            row=row, col=1,
        )


def add_trade_overlay_traces(
    fig: go.Figure, ohlcv_df: pd.DataFrame, overlays: list[dict[str, Any]],
    normalize_base: Optional[float], row: int = 1,
) -> int:
    """追補v4§2.2: 指定銘柄のチャート(fig)にトレードマーカー(▲/▼/◆エントリー・✕決済)+
    勝敗色の接続点線を追加する。map_trades_to_chart(座標算出)+レバスケール(leverage_marker_size)+
    データセット別輪郭色(get_trade_marker_color、overlaysに反映済み)を用いる。
    戻り値: 実際に描画したトレード件数(overlays内の全データセット合計)。
    """
    total_drawn = 0
    for ov in overlays:
        ds_label = ov["dataset_label"]
        color = ov["color"]
        mapped = map_trades_to_chart(ov["trades_df"], ohlcv_df, normalize_base)
        if mapped.empty:
            continue
        total_drawn += len(mapped)

        entry_hover = []
        for _, r in mapped.iterrows():
            side_lbl = _trade_side_label(r["side"])
            lev_lbl = f"{r['leverage']:.0f}x" if pd.notna(r["leverage"]) else "—"
            pnl_lbl = f"{r['pnl_usd']:+.2f}USD" if pd.notna(r["pnl_usd"]) else "—"
            pct_lbl = f"{r['pnl_percent']:+.2f}%" if pd.notna(r["pnl_percent"]) else "—"
            hold_lbl = format_holding_duration(r["holding_minutes"])
            approx = "≈近似(直近確定足の終値)" if r["entry_price_is_approx"] else "実価格"
            margin_lbl, mae_lbl, rr_lbl = _trade_risk_labels(r, ohlcv_df)
            entry_hover.append(
                f"[{ds_label}] {side_lbl} レバ{lev_lbl}<br>エントリー価格: {approx}<br>"
                f"PnL {pnl_lbl} ({pct_lbl})<br>保有時間: {hold_lbl}<br>"
                f"証拠金(推定) {margin_lbl}<br>最大逆行(MAE) {mae_lbl}｜RR実績 {rr_lbl}"
            )
        fig.add_trace(
            go.Scatter(
                x=mapped["entry_time"], y=mapped["entry_price"], mode="markers",
                marker=dict(
                    symbol=[_trade_marker_symbol(s) for s in mapped["side"]],
                    size=[leverage_marker_size(lv) for lv in mapped["leverage"]],
                    color=color, line=dict(width=1, color="#111111"),
                ),
                name=f"{ds_label} エントリー", legendgroup=f"trades_{ds_label}",
                hovertext=entry_hover, hoverinfo="text",
            ),
            row=row, col=1,
        )
        _add_exit_and_link_traces(fig, mapped, ds_label, row, ohlcv_df=ohlcv_df)
    return total_drawn


def cumulative_return_pct(series: pd.Series, base: float) -> pd.Series:
    """追補v5§1: 期間先頭終値からの累積騰落率(%)。(price/base-1)*100の純関数。
    base<=0やNaNの場合はNaN系列を返す(ゼロ割回避)。
    """
    if base is None or not np.isfinite(base) or base == 0:
        return pd.Series(np.nan, index=series.index)
    return (series / base - 1.0) * 100.0


def to_cumulative_return_ohlc(
    df: pd.DataFrame, base: Optional[float] = None,
) -> tuple[pd.DataFrame, float]:
    """追補v5§1: OHLC4列すべてを累積騰落率(%)に変換したコピーを返す。
    base未指定時は先頭行のcloseを基準にする(先頭行のclose自身は変換後0%になる)。
    戻り値: (変換後df, 使用したbase値)。
    """
    b = float(df["close"].iloc[0]) if base is None else float(base)
    out = df.copy()
    for c in ["open", "high", "low", "close"]:
        out[c] = cumulative_return_pct(out[c], b)
    return out, b


def _ohlc_hovertemplate(is_pct: bool = False) -> str:
    """追補v5§3: ローソク足trace用の日本語hovertemplate。
    is_pct=Trueはボラチャート行(累積騰落率%)向け、Falseは価格チャート行(実OHLC)向け。
    英語表記(open/high/low/close等)を画面に出さないため、全て日本語ラベルで組み立てる。
    customdata(列: [終値, 変動幅(close-open実価格), 変動率%, 値幅(high-low実価格)])が
    同時に供給されている前提(_hover_customdata参照)。通貨記号は付けない(日経225等の円建て
    銘柄があるため数値のみ+日本語ラベルで表現する)。
    """
    # v6.4: hovermode="x unified"(全銘柄を1つの箱に統合)前提のテンプレート。
    # 日時は統合箱のタイトル(xaxis.hoverformat)が出すため各行には書かない。
    # 複数銘柄が重なるボラチャート行は1銘柄1行のコンパクト表記にする(箱の巨大化防止)。
    # customdataは_hover_customdataで整形済み文字列(plotly.jsが:+,.2fの符号フラグを
    # 解釈できないための方式・実機確認済み)。
    if is_pct:
        return (
            "<b>%{fullData.name}</b> 累積騰落率 %{close:.2f}%"
            "｜終値 %{customdata[0]}｜変動幅 %{customdata[1]} (%{customdata[2]})"
            "｜値幅 %{customdata[3]}<extra></extra>"
        )
    return (
        "<b>%{fullData.name}</b><br>始値 %{open:,.2f}<br>高値 %{high:,.2f}"
        "<br>安値 %{low:,.2f}<br>終値 %{close:,.2f}"
        "<br>変動幅 %{customdata[1]} (%{customdata[2]})"
        "<br>値幅(安値→高値) %{customdata[3]}<extra></extra>"
    )


def _volume_hovertemplate() -> str:
    """追補v5§3: 出来高barトレース用の日本語hovertemplate(v6.4: unified箱用に日時なし)。"""
    return "<b>%{fullData.name}</b> 出来高 %{y:,.0f}<extra></extra>"


def build_click_detail(
    click_time: Any,
    data: dict[str, pd.DataFrame],
    base_map: Optional[dict[str, float]] = None,
    trade_rows: Optional[dict[str, pd.DataFrame]] = None,
) -> list[dict[str, Any]]:
    """追補v5§3: クリック時刻(x軸座標)から全銘柄の該当バー詳細+該当トレードを抽出する純関数(欄外パネル用)。
    銘柄ごとに click_time 以前の直近バー(asof)を採用する(欠損=バー境界と一致しない時刻の救済)。
    click_timeがその銘柄の全期間の範囲外(先頭より前/末尾より後)の場合はその銘柄を結果から除外する。
    trade_rowsはmap_trades_to_chart互換の列(entry_time/exit_time/side/leverage/pnl_usd/pnl_percent等)+
    dataset_label列を持つDataFrame(銘柄ラベル->DataFrame)。該当バー区間[bar_time, 次バー時刻)に
    entry_timeまたはexit_timeが入るトレードを"該当トレード"として併記する。
    戻り値: 銘柄ごとの詳細dictのリスト(順序=dataのキー順、該当なし銘柄は含まれない)。
    """
    if click_time is None or not data:
        return []
    try:
        ts = pd.Timestamp(click_time)
    except Exception:  # noqa: BLE001
        return []
    results: list[dict[str, Any]] = []
    for label, df in data.items():
        if df is None or df.empty:
            continue
        idx = df.index
        ts_cmp = ts
        if idx.tz is not None and ts_cmp.tzinfo is None:
            ts_cmp = ts_cmp.tz_localize(idx.tz)
        elif idx.tz is None and ts_cmp.tzinfo is not None:
            ts_cmp = ts_cmp.tz_localize(None)
        elif idx.tz is not None and ts_cmp.tzinfo is not None:
            ts_cmp = ts_cmp.tz_convert(idx.tz)
        if ts_cmp < idx[0] or ts_cmp > idx[-1]:
            continue  # 範囲外(先頭より前/末尾より後)
        pos = int(idx.searchsorted(ts_cmp, side="right")) - 1
        if pos < 0:
            continue
        bar_time = idx[pos]
        next_time = idx[pos + 1] if pos + 1 < len(idx) else None
        row = df.iloc[pos]
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        ret_pct = (c / o - 1.0) * 100.0 if o and np.isfinite(o) else float("nan")
        base = (base_map or {}).get(label)
        cum_pct = float(cumulative_return_pct(pd.Series([c]), base).iloc[0]) if base else float("nan")
        vol = float(row["volume"]) if "volume" in df.columns and pd.notna(row.get("volume")) else float("nan")
        results.append({
            "label": label, "time": bar_time, "open": o, "high": h, "low": l, "close": c,
            "return_pct": ret_pct, "volume": vol, "cum_return_pct": cum_pct,
            "trades": _trades_in_bar(label, bar_time, next_time, trade_rows),
        })
    return results


def _trades_in_bar(
    label: str, bar_time: pd.Timestamp, next_time: Optional[pd.Timestamp],
    trade_rows: Optional[dict[str, pd.DataFrame]],
) -> list[dict[str, Any]]:
    """build_click_detail内部処理: [bar_time, next_time)区間に入るentry/exitトレードを抽出する。"""
    tdf = (trade_rows or {}).get(label)
    if tdf is None or tdf.empty:
        return []
    trades: list[dict[str, Any]] = []
    for _, tr in tdf.iterrows():
        et, xt = tr.get("entry_time"), tr.get("exit_time")
        hit_entry = pd.notna(et) and bar_time <= et and (next_time is None or et < next_time)
        hit_exit = pd.notna(xt) and bar_time <= xt and (next_time is None or xt < next_time)
        if hit_entry or hit_exit:
            trades.append({
                "dataset_label": tr.get("dataset_label", ""), "side": tr.get("side"),
                "leverage": tr.get("leverage"), "pnl_usd": tr.get("pnl_usd"),
                "pnl_percent": tr.get("pnl_percent"), "is_entry": hit_entry, "is_exit": hit_exit,
            })
    return trades


def _mapped_trade_rows_for_click(
    chart_data: dict[str, pd.DataFrame], overlays: dict[str, list[dict[str, Any]]],
) -> dict[str, pd.DataFrame]:
    """追補v5§3: 欄外パネルのトレード併記用に、表示中銘柄ごとの座標算出済みトレードDataFrameを作る
    (map_trades_to_chartを再利用しdataset_label列を付加、複数データセットはconcat)。st非依存。
    """
    out: dict[str, pd.DataFrame] = {}
    for label, ov_list in overlays.items():
        df = chart_data.get(label)
        if df is None or df.empty:
            continue
        frames = []
        for ov in ov_list:
            mapped = map_trades_to_chart(ov["trades_df"], df, None)
            if mapped.empty:
                continue
            mapped = mapped.copy()
            mapped["dataset_label"] = ov["dataset_label"]
            frames.append(mapped)
        if frames:
            out[label] = pd.concat(frames, ignore_index=True)
    return out


def _hover_customdata(df: pd.DataFrame) -> np.ndarray:
    """追補v5§3: ホバーモードの拡張hovertemplate用customdata行列を実価格OHLC dfから作る。
    列=[終値, 変動幅(close-open・符号付き), 変動率%(符号付き), 値幅(high-low)]。
    Python側で整形済みの表示文字列を渡す: plotly.jsのhovertemplateはd3書式の
    符号フラグ(:+,.2f)を解釈できず生float全桁を表示する(:,.2fは効く・実機で確認済み)。
    非有限値・open=0は"—"表示に逃がす。
    """
    close = df["close"].to_numpy(dtype=float)
    open_ = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    rows: list[list[str]] = []
    for c, o, h, l in zip(close, open_, high, low):
        if np.isfinite(c) and np.isfinite(o) and o != 0:
            diff_s = f"{c - o:+,.2f}"
            pct_s = f"{(c / o - 1.0) * 100.0:+.2f}%"
            # 「-0.00」「+0.00%」等のマイナスゼロ表示を排除(v6.2・見た目の違和感解消)
            if diff_s in ("+0.00", "-0.00"):
                diff_s = "0.00"
            if pct_s in ("+0.00%", "-0.00%"):
                pct_s = "0.00%"
        else:
            diff_s, pct_s = "—", "—"
        rows.append([
            f"{c:,.2f}" if np.isfinite(c) else "—",
            diff_s,
            pct_s,
            f"{h - l:,.2f}" if (np.isfinite(h) and np.isfinite(l)) else "—",
        ])
    return np.array(rows, dtype=object)


def _median_bar_minutes(df: pd.DataFrame) -> float:
    """v6.3: dfの足間隔(分)の中央値。閉場ギャップ等の外れ値に頑健なようmedianを使う。"""
    if len(df) < 2:
        return 0.0
    diffs = pd.Series(df.index).diff().dropna()
    if diffs.empty:
        return 0.0
    return float(diffs.median().total_seconds() / 60.0)


def _auto_decimation_rule(max_len: int, base_minutes: float,
                          max_bars: int = CHART_MAX_BARS_PER_ROW) -> Optional[str]:
    """v6.3: 表示バー数がmax_barsを超える場合に表示専用の粗い足ルール(例 "2min")を返す。
    上限内・間隔不明ならNone(間引きしない)。集計/クリック詳細/ズームには使わない。
    """
    if max_len <= max_bars or base_minutes <= 0:
        return None
    import math
    factor = math.ceil(max_len / max_bars)
    return f"{int(round(base_minutes * factor))}min"


def build_candlestick_chart(
    data: dict[str, pd.DataFrame],
    timeframe_rule: Optional[str],
    show_bg: bool,
    bg_opacity: float = BG_OPACITY,
    trade_overlays: Optional[dict[str, list[dict[str, Any]]]] = None,
    detail_mode: str = "click",
) -> tuple[go.Figure, list[str], int]:
    """追補v5§1 チャートタブ用のfigureを構築する(ボラチャート+価格チャートの縦2段以上構成)。
    1段目=🌊ボラチャート: 全銘柄を累積騰落率(%、期間先頭0%基準)で重畳表示。
    2段目以降=💹価格チャート: 銘柄ごとに1行、実価格ローソク足(行ごとに独自y軸)。
    単一銘柄選択時のみ末尾に出来高行を追加する(従来どおり)。全行はshared_xaxesで時間軸同期。

    戻り値: (figure, リサンプル後に空データとなったためチャートから除外した銘柄ラベルのリスト,
      追補v4§2.2の描画したトレード件数合計(マーカー表示OFF/trade_overlays未指定なら0))。
      リサンプル(例: 日足)後に全行がNaNとなり空になる銘柄(短期間データ+粗い足種の組み合わせ等)を
      黙って描画対象から除き、呼び出し側が案内を表示できるようにする(空dfでの.iloc[0]クラッシュ回避)。

    追補v3§3: bg_opacityはサイドバー「背景帯の濃さ」スライダー値/100(show_bg=Falseなら未使用)。
    追補v4§2.2/v5§4: trade_overlaysはbuild_trade_overlays_for_chart()の戻り値(銘柄ラベル->overlay一覧)。
      価格チャート行(実価格座標)に描画する(ボラチャート行には描かない=視認性優先)。
    追補v5§2/§3: 全xaxisにspikes(縦ライン)を常時設定しhovermode='x'で全行同期する。
      detail_mode="click"(既定・欄外パネル)ではローソク足/出来高のhoverinfo="skip"にし、
      各行に透明scatterオーバーレイ(クリック捕捉用)を追加する。detail_mode="hover"では
      日本語hovertemplateをローソク足/出来高に設定しオーバーレイは追加しない。
    """
    trade_overlays = trade_overlays or {}
    marker_count = 0
    resampled: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []
    for label, df in data.items():
        d = resample_ohlcv(df, timeframe_rule) if timeframe_rule else df
        if d is None or d.empty:
            skipped.append(label)
            continue
        resampled[label] = d

    # v6.3: 描画負荷対策の自動間引き(表示専用)。最長銘柄のバー数が上限を超える場合、
    # 全銘柄を同じ粗い足へ再リサンプルする(行間の時間軸整合を保つため全銘柄同一ルール)。
    if resampled:
        max_len = max(len(d) for d in resampled.values())
        base_min = _median_bar_minutes(max(resampled.values(), key=len))
        deci_rule = _auto_decimation_rule(max_len, base_min)
        if deci_rule:
            decimated: dict[str, pd.DataFrame] = {}
            for lbl, d in resampled.items():
                d2 = resample_ohlcv(d, deci_rule)
                decimated[lbl] = d2 if (d2 is not None and not d2.empty) else d
            resampled = decimated

    labels = list(resampled.keys())
    if not labels:
        raise ValueError("選択した足種でのリサンプル後、表示可能なデータが1件もありませんでした。")

    n = len(labels)
    has_volume = n == 1  # 単一銘柄選択時のみ末尾に出来高行を維持(追補v5§1)
    rows_total = 1 + n + (1 if has_volume else 0)

    row_titles: list[Optional[str]] = ["🌊 ボラチャート"]
    for i, label in enumerate(labels):
        prefix = "💹 価格チャート — " if i == 0 else ""
        row_titles.append(f"{prefix}{label}")
    if has_volume:
        row_titles.append("出来高")

    fig = make_subplots(
        rows=rows_total, cols=1, shared_xaxes=True, vertical_spacing=0.03,
        row_heights=[1.0 / rows_total] * rows_total, subplot_titles=row_titles,
    )

    # 1段目: 🌊ボラチャート(全銘柄重畳・累積騰落率%・期間先頭0%基準)
    is_click_mode = detail_mode == "click"
    norm_by_label: dict[str, pd.DataFrame] = {}
    for i, label in enumerate(labels):
        df = resampled[label]
        norm, _base = to_cumulative_return_ohlc(df)
        norm_by_label[label] = norm
        colors = get_symbol_colors(label, custom_index=i)
        fig.add_trace(
            go.Candlestick(
                x=norm.index, open=norm["open"], high=norm["high"], low=norm["low"], close=norm["close"],
                increasing_line_color=colors["inc"], decreasing_line_color=colors["dec"],
                increasing_fillcolor=colors["inc"], decreasing_fillcolor=colors["dec"],
                name=label, legendgroup=f"sym_{label}",
                hoverinfo="skip" if is_click_mode else None,
                hovertemplate=None if is_click_mode else _ohlc_hovertemplate(is_pct=True),
                # hoverモードのみcustomdata供給(clickモードはhovertemplate=Noneで死荷重・
                # Tailscale Funnel経由の公開URLは帯域が細くペイロード増が実害のため付けない)。
                customdata=None if is_click_mode else _hover_customdata(df),
            ),
            row=1, col=1,
        )
    fig.update_yaxes(title_text="累積騰落率(%)", row=1, col=1)

    # 2段目以降: 💹価格チャート(銘柄ごとに実価格ローソク・独自y軸。トレードマーカーもここに描画)
    for i, label in enumerate(labels):
        row = 2 + i
        df = resampled[label]
        colors = get_symbol_colors(label, custom_index=i)
        fig.add_trace(
            go.Candlestick(
                x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                increasing_line_color=colors["inc"], decreasing_line_color=colors["dec"],
                increasing_fillcolor=colors["inc"], decreasing_fillcolor=colors["dec"],
                name=label, legendgroup=f"sym_{label}", showlegend=False,
                hoverinfo="skip" if is_click_mode else None,
                hovertemplate=None if is_click_mode else _ohlc_hovertemplate(is_pct=False),
                customdata=None if is_click_mode else _hover_customdata(df),
            ),
            row=row, col=1,
        )
        if label in trade_overlays:
            marker_count += add_trade_overlay_traces(fig, df, trade_overlays[label], None, row=row)

    if has_volume:
        label = labels[0]
        df = resampled[label]
        colors = get_symbol_colors(label, custom_index=0)
        vol_colors = [colors["inc"] if c >= o else colors["dec"] for o, c in zip(df["open"], df["close"])]
        fig.add_trace(
            go.Bar(
                x=df.index, y=df["volume"], marker_color=vol_colors, name=f"{label} 出来高", showlegend=False,
                hoverinfo="skip" if is_click_mode else None,
                hovertemplate=None if is_click_mode else _volume_hovertemplate(),
            ),
            row=rows_total, col=1,
        )

    if show_bg and len(labels) > 0:
        all_start = min(d.index.min() for d in resampled.values())
        all_end = max(d.index.max() for d in resampled.values())
        shapes = build_session_background_shapes(all_start, all_end, bg_opacity)
        fig.update_layout(shapes=shapes)
        # 凡例用ダミーtrace(セッション色の凡例登録)
        for group in ["asia", "london", "ny", "thin"]:
            r, g, b = BG_GROUP_COLOR_RGB[group]
            fig.add_trace(
                go.Scatter(
                    x=[None], y=[None], mode="markers",
                    marker=dict(size=10, color=f"rgba({r},{g},{b},0.6)", symbol="square"),
                    name=BG_GROUP_LABEL[group], showlegend=True,
                )
            )

    if is_click_mode:
        # 追補v5§3: 欄外パネル用の透明scatterオーバーレイ(各バーのclose位置にクリック捕捉用の点を敷く)。
        for label in labels:
            norm = norm_by_label[label]
            fig.add_trace(
                go.Scatter(
                    x=norm.index, y=norm["close"], mode="markers",
                    marker=dict(size=16, opacity=0.01, color="#888"),
                    # hoverinfo="skip"はplotly.jsのホバー/クリック判定から完全除外され
                    # on_selectが一切発火しない。"none"=ラベル非表示だがイベントは発火する。
                    hoverinfo="none", showlegend=False, name=f"__click_overlay_vola_{label}",
                ),
                row=1, col=1,
            )
        for i, label in enumerate(labels):
            row = 2 + i
            df = resampled[label]
            fig.add_trace(
                go.Scatter(
                    x=df.index, y=df["close"], mode="markers",
                    marker=dict(size=16, opacity=0.01, color="#888"),
                    hoverinfo="none", showlegend=False, name=f"__click_overlay_price_{label}",
                ),
                row=row, col=1,
            )

    fig.update_xaxes(rangeslider_visible=False)  # 全行のrangeslider無効(追補v3来維持・行数に依らず一括)
    fig.update_xaxes(
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikethickness=1, spikedash="dot", spikecolor="rgba(180,180,180,0.6)",
        hoverformat="%Y-%m-%d %H:%M",  # v6.4: unified箱のタイトル(日時)の書式
    )  # 追補v5§2: 全xaxisに縦ライン設定(行数に依らず一括=既知の罠⑤への対応)
    layout_kwargs: dict[str, Any] = dict(
        template="plotly_dark",
        hovermode="x unified",  # v6.4: 全銘柄を1つの箱に統合(銘柄毎の箱が重なり文字が隠れる問題の根治)
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=40, r=20, t=40, b=20),
        height=320 * rows_total,  # 追補v5§1: 1行あたり約320px
    )
    if not is_click_mode:
        # v6.2: ユーザー要望「枠を大きく・文字を読みやすく」— 14px+ほぼ不透明+白文字+左揃え
        layout_kwargs["hoverlabel"] = dict(
            font_size=14, font_color="#FFFFFF", bgcolor="rgba(13,17,23,0.96)",
            bordercolor="rgba(160,160,160,0.7)", align="left",
        )
    fig.update_layout(**layout_kwargs)
    return fig, skipped, marker_count


def try_export_png(fig: go.Figure) -> Optional[bytes]:
    """kaleidoでPNGを生成する。失敗時はNoneを返す(呼び出し側でHTMLフォールバックする)。"""
    try:
        return fig.to_image(format="png", scale=2)
    except Exception:
        # kaleido 1.x(orjson)はshapes/customdata内の素のpandas Timestampを
        # 直列化できない。PlotlyJSONEncoder経由のラウンドトリップでISO文字列化
        # してから再試行する(v5の複数段チャートで実際に発生・実証済みの回避策)。
        try:
            import json as _json

            fig2 = go.Figure(_json.loads(fig.to_json()))
            return fig2.to_image(format="png", scale=2)
        except Exception:
            return None


def build_session_bar_charts(market_stats_by_symbol: dict[str, pd.DataFrame]) -> tuple[go.Figure, go.Figure]:
    """§4.3 大枠4セッション別サマリー棒グラフ(平均騰落率・平均ボラ、銘柄別グループ棒)を構築する。"""
    rows_ret = []
    rows_vola = []
    for label, stats in market_stats_by_symbol.items():
        for parent in PARENT_ORDER:
            children = [b for b in BAND_ORDER if BAND_TO_PARENT[b] == parent]
            child_hours = {b: len(BAND_HOURS[b]) for b in children}
            ret_vals = {b: stats.loc[b, "avg_return_pct"] if b in stats.index else np.nan for b in children}
            vola_vals = {b: stats.loc[b, "avg_volatility_pct"] if b in stats.index else np.nan for b in children}
            rows_ret.append({"parent": parent, "symbol": label, "value": _weighted_avg(ret_vals, child_hours)})
            rows_vola.append({"parent": parent, "symbol": label, "value": _weighted_avg(vola_vals, child_hours)})

    df_ret = pd.DataFrame(rows_ret)
    df_vola = pd.DataFrame(rows_vola)
    fig_ret = px.bar(
        df_ret, x="parent", y="value", color="symbol", barmode="group",
        template="plotly_dark", labels={"parent": "大枠セッション", "value": "平均騰落率(%)", "symbol": "銘柄"},
        category_orders={"parent": PARENT_ORDER},
    )
    fig_vola = px.bar(
        df_vola, x="parent", y="value", color="symbol", barmode="group",
        template="plotly_dark", labels={"parent": "大枠セッション", "value": "平均ボラティリティ(%)", "symbol": "銘柄"},
        category_orders={"parent": PARENT_ORDER},
    )
    for f in (fig_ret, fig_vola):
        f.update_layout(margin=dict(l=40, r=20, t=40, b=20), height=380)
    return fig_ret, fig_vola


def build_correlation_heatmap(data: dict[str, pd.DataFrame]) -> go.Figure:
    """選択銘柄間の1h対数リターンのピアソン相関ヒートマップ(-1〜+1発散配色)。"""
    returns = {}
    for label, df in data.items():
        returns[label] = np.log(df["close"] / df["close"].shift(1))
    ret_df = pd.DataFrame(returns)
    corr = ret_df.corr(method="pearson")
    fig = px.imshow(
        corr, text_auto=".2f", color_continuous_scale="RdBu", zmin=-1, zmax=1,
        template="plotly_dark", aspect="auto",
    )
    fig.update_layout(margin=dict(l=40, r=20, t=40, b=20), height=420)
    return fig


def build_session_symbol_heatmap(market_stats_by_symbol: dict[str, pd.DataFrame]) -> go.Figure:
    """行=詳細9帯、列=銘柄、値=平均騰落率(%)のヒートマップ(ゼロ中心の発散配色)。"""
    cols = {}
    for label, stats in market_stats_by_symbol.items():
        cols[label] = stats.reindex(BAND_ORDER)["avg_return_pct"]
    mat = pd.DataFrame(cols, index=BAND_ORDER)
    max_abs = float(np.nanmax(np.abs(mat.to_numpy()))) if mat.size and np.isfinite(np.nanmax(np.abs(mat.to_numpy()))) else 1.0
    max_abs = max_abs if max_abs > 0 else 1.0
    fig = px.imshow(
        mat, text_auto=".2f", color_continuous_scale="RdYlGn", zmin=-max_abs, zmax=max_abs,
        template="plotly_dark", aspect="auto", labels={"color": "平均騰落率(%)"},
    )
    fig.update_layout(margin=dict(l=40, r=20, t=40, b=20), height=460)
    return fig


# ---- 追補v3 §1: 騰落率発散色付け(純関数・再利用ヘルパー) ------------------------------
# 平均騰落率(%)/中央値騰落率(%)のように0を中心に正負が発散する指標に使う共通ヘルパー。
DIVERGING_RETURN_COLUMNS: list[str] = ["平均騰落率(%)", "中央値騰落率(%)"]


def diverging_color(v: Any, vmax: float) -> str:
    """発散指標1値の背景色を返す純関数(§1)。正=緑グラデーション、負=赤グラデーション。
    v/vmaxがNaN、vmax<=0、v==0のいずれかなら無色("")を返す(境界はselftest §9で検証)。
    濃淡は abs(v)/vmax (0〜1にクリップ) に比例させ、勝率グラデーションと同系色(緑#38A860/赤#E04040)を使う。
    """
    if pd.isna(v) or pd.isna(vmax) or vmax <= 0 or v == 0:
        return ""
    intensity = min(1.0, abs(v) / vmax)
    alpha = 0.15 + 0.55 * intensity
    if v > 0:
        return f"background-color: rgba(56,168,96,{alpha:.2f}); color: #fff"
    return f"background-color: rgba(224,64,64,{alpha:.2f}); color: #fff"


def compute_diverging_vmax(df: pd.DataFrame, cols: list[str]) -> float:
    """§1: 発散色付け用の共通vmax(指定列群のうちdfに存在する列の絶対値最大値)を計算する純関数。
    該当列が無い/全てNaNなら0.0を返す(diverging_colorはvmax<=0で無色を返すため安全側)。
    """
    vals: list[float] = []
    for c in cols:
        if c in df.columns:
            m = df[c].abs().max(skipna=True)
            if pd.notna(m):
                vals.append(float(m))
    return max(vals) if vals else 0.0


def style_cross_table(df: pd.DataFrame, diverging_vmax: Optional[float] = None) -> Any:
    """勝率(%)列を条件付き色付け(<50%赤グラデーション、>=50%緑グラデーション)、
    平均騰落率(%)/中央値騰落率(%)列を発散色付け(§1・diverging_color)するStylerを返す。

    diverging_vmax: 濃淡の共通スケール(絶対値最大値)。Noneならこのdf自身の該当列から
    compute_diverging_vmaxで計算する。§2の多重クロス表アコーディオン化では、親表(大枠計4行)と
    大枠ごとの詳細表(expander内)とで濃淡を揃えるため、分割前のcross全体から計算した値を
    呼び出し側で明示的に渡す。
    """
    def _color(v: Any) -> str:
        if pd.isna(v):
            return ""
        if v < 50:
            intensity = min(1.0, (50 - v) / 50.0)
            alpha = 0.22 + 0.5 * intensity
            return f"background-color: rgba(224,64,64,{alpha:.2f}); color: #fff"
        else:
            intensity = min(1.0, (v - 50) / 50.0)
            alpha = 0.22 + 0.5 * intensity
            return f"background-color: rgba(56,168,96,{alpha:.2f}); color: #fff"

    fmt = {
        "平均騰落率(%)": lambda v: "—" if pd.isna(v) else f"{v:+.3f}%",
        "平均出来高": lambda v: "—" if pd.isna(v) else f"{v:,.1f}",
        "平均ボラティリティ(%)": lambda v: "—" if pd.isna(v) else f"{v:.3f}%",
        "エントリー回数": lambda v: "—" if pd.isna(v) else f"{int(v)}",
        "勝率(%)": lambda v: "—" if pd.isna(v) else f"{v:.1f}%",
        "合計損益(USD)": lambda v: "—" if pd.isna(v) else f"{v:,.2f}",
        # 追補§1.3/§2.4: 曜日別・月別サマリー表でも同じStylerを再利用するための追加列フォーマット。
        # 既存タブのdfにはこれらの列が存在しないため、既存表示への影響はない。
        # 「全選択銘柄の平均」モードではn(日数)/n(年数)が銘柄横断平均で小数になりうるため、
        # int()による常時切り捨て(過小表示)を避けてround()で最も近い整数に丸める(確定指摘対応)。
        "中央値騰落率(%)": lambda v: "—" if pd.isna(v) else f"{v:+.3f}%",
        "陽線率(%)": lambda v: "—" if pd.isna(v) else f"{v:.1f}%",
        "n(日数)": lambda v: "—" if pd.isna(v) else f"{round(v)}",
        "n(年数)": lambda v: "—" if pd.isna(v) else f"{round(v)}",
    }
    # st.dataframe(1.59)×pandas3.0はStyler.formatの表示値をNaNセルで無視し生の
    # "None"を描画するため、表示用文字列へ事前整形したコピーを渡す。
    # 色付けは元の数値をクロージャで保持してapplyで適用する。
    display = df.copy()
    for col, f in fmt.items():
        if col in display.columns:
            display[col] = df[col].map(f)

    win_numeric = df["勝率(%)"] if "勝率(%)" in df.columns else None

    def _color_col(_col: pd.Series) -> list[str]:
        if win_numeric is None:
            return [""] * len(_col)
        return [_color(v) for v in win_numeric]

    # §1: 平均騰落率(%)/中央値騰落率(%)の発散色付け。共通vmaxはdiverging_vmax引数優先、
    # 未指定ならこのdf自身の該当列から計算する(単独呼び出し=従来タブは自df基準のまま)。
    vmax = diverging_vmax if diverging_vmax is not None else compute_diverging_vmax(df, DIVERGING_RETURN_COLUMNS)

    def _make_diverging_colorer(col_name: str):
        numeric_col = df[col_name]

        def _colorer(_col: pd.Series) -> list[str]:
            return [diverging_color(v, vmax) for v in numeric_col]

        return _colorer

    styler = display.style
    if "勝率(%)" in display.columns:
        styler = styler.apply(_color_col, subset=["勝率(%)"])
    for _dcol in DIVERGING_RETURN_COLUMNS:
        if _dcol in display.columns:
            styler = styler.apply(_make_diverging_colorer(_dcol), subset=[_dcol])
    return styler


def _compare_diff_color(v: Any, vmax: float) -> str:
    """追補v4§1.3.3: 勝率差(師匠-弟子)の発散色付け。正(師匠優位=改善余地)=赤系、
    0以下(弟子が同等以上)=緑系。style_cross_table の diverging_color と濃淡ロジックは同系だが、
    符号の意味(色の向き)が逆転する専用版。
    """
    if pd.isna(v):
        return ""
    eff_vmax = vmax if (vmax is not None and not pd.isna(vmax) and vmax > 0) else 1.0
    intensity = min(1.0, abs(v) / eff_vmax)
    alpha = 0.18 + 0.5 * intensity
    if v > 0:
        return f"background-color: rgba(224,64,64,{alpha:.2f}); color: #fff"
    return f"background-color: rgba(56,168,96,{alpha:.2f}); color: #fff"


def style_compare_band_table(df: pd.DataFrame, label_a: str, label_b: str) -> Any:
    """追補v4§1.3.3/§1.3.5: 弟子(a)/師匠(b)の帯別・曜日別比較表(compare_trade_stats()["by_band"]
    相当の列を持つdf)を表示用に整形するStyler。勝率差列を _compare_diff_color で発散色付けし、
    n<3の帯(low_n_a/low_n_b)はindexラベルに「(◯◯n小)」を付記する。
    pandas3.0×st.dataframeのStyler.format無視対策として、表示用文字列へ事前整形したコピーを
    別に用意し、色付けは元の数値をクロージャで保持してapplyする(style_cross_tableと同じ作法)。
    """
    cols = [
        "n_trades_a", "win_rate_pct_a", "total_pnl_usd_a",
        "n_trades_b", "win_rate_pct_b", "total_pnl_usd_b", "win_rate_diff",
    ]
    base = df[cols].copy()

    low_n_a = df["low_n_a"] if "low_n_a" in df.columns else pd.Series(False, index=df.index)
    low_n_b = df["low_n_b"] if "low_n_b" in df.columns else pd.Series(False, index=df.index)
    new_index = []
    for idx in base.index:
        tags = []
        if bool(low_n_a.loc[idx]):
            tags.append(f"{label_a}n小")
        if bool(low_n_b.loc[idx]):
            tags.append(f"{label_b}n小")
        new_index.append(f"{idx} ({'/'.join(tags)})" if tags else str(idx))
    base.index = new_index

    vmax = float(df["win_rate_diff"].abs().max(skipna=True))
    if pd.isna(vmax):
        vmax = 0.0

    rename_map = {
        "n_trades_a": f"{label_a} 回数", "win_rate_pct_a": f"{label_a} 勝率(%)",
        "total_pnl_usd_a": f"{label_a} 損益(USD)",
        "n_trades_b": f"{label_b} 回数", "win_rate_pct_b": f"{label_b} 勝率(%)",
        "total_pnl_usd_b": f"{label_b} 損益(USD)",
        "win_rate_diff": f"勝率差({label_b}-{label_a})",
    }
    fmt = {
        "n_trades_a": lambda v: "—" if pd.isna(v) else f"{int(v)}",
        "win_rate_pct_a": lambda v: "—" if pd.isna(v) else f"{v:.1f}%",
        "total_pnl_usd_a": lambda v: "—" if pd.isna(v) else f"{v:,.2f}",
        "n_trades_b": lambda v: "—" if pd.isna(v) else f"{int(v)}",
        "win_rate_pct_b": lambda v: "—" if pd.isna(v) else f"{v:.1f}%",
        "total_pnl_usd_b": lambda v: "—" if pd.isna(v) else f"{v:,.2f}",
        "win_rate_diff": lambda v: "—" if pd.isna(v) else f"{v:+.1f}pt",
    }
    display = base.copy()
    for col, f in fmt.items():
        display[col] = base[col].map(f)
    display = display.rename(columns=rename_map)

    diff_series = base["win_rate_diff"]
    diff_col_display = rename_map["win_rate_diff"]

    def _colorer(_col: pd.Series) -> list[str]:
        return [_compare_diff_color(v, vmax) for v in diff_series]

    return display.style.apply(_colorer, subset=[diff_col_display])


# =====================================================================================
# 追補§1.3/§2.4: 曜日別・月別タブ用の集計束ね+表整形ヘルパー(st非依存の純関数)
# =====================================================================================


def aggregate_weekday_stats_multi(symbol_stats: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """複数銘柄の compute_weekday_stats 結果を平均する(「全選択銘柄の平均」モード用)。
    aggregate_market_stats_multiの曜日版。出来高は銘柄ごとの全曜日平均出来高で正規化した
    相対値にしてから平均する(単位が銘柄間で大きく異なるため)。
    """
    if not symbol_stats:
        return pd.DataFrame(
            {"avg_return_pct": np.nan, "avg_volume": np.nan, "avg_volatility_pct": np.nan, "n_days": 0},
            index=WEEKDAY_LABELS,
        )
    normalized = {}
    for label, d in symbol_stats.items():
        dd = d.reindex(WEEKDAY_LABELS).copy()
        mean_vol = dd["avg_volume"].mean(skipna=True)
        if mean_vol and mean_vol > 0 and not pd.isna(mean_vol):
            dd["avg_volume_rel"] = dd["avg_volume"] / mean_vol
        else:
            dd["avg_volume_rel"] = np.nan
        normalized[label] = dd
    combined = pd.concat(normalized, names=["symbol", "weekday"])
    result = pd.DataFrame(index=WEEKDAY_LABELS)
    result["avg_return_pct"] = combined.groupby(level="weekday")["avg_return_pct"].mean().reindex(WEEKDAY_LABELS)
    result["avg_volume"] = combined.groupby(level="weekday")["avg_volume_rel"].mean().reindex(WEEKDAY_LABELS)
    result["avg_volatility_pct"] = combined.groupby(level="weekday")["avg_volatility_pct"].mean().reindex(WEEKDAY_LABELS)
    result["n_days"] = combined.groupby(level="weekday")["n_days"].mean().reindex(WEEKDAY_LABELS)
    return result


def aggregate_month_stats_multi(symbol_stats: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """複数銘柄の compute_month_stats 結果を平均する(「全選択銘柄の平均」モード用・月別版)。
    月次騰落率は単位が%で銘柄間で比較可能なため、出来高のような正規化は不要で単純平均する。
    """
    cols = ["avg_return_pct", "median_return_pct", "pct_positive", "avg_volatility_pct", "n_years"]
    if not symbol_stats:
        return pd.DataFrame({c: np.nan for c in cols[:-1]} | {"n_years": 0}, index=MONTH_ORDER)
    combined = pd.concat(
        {label: d.reindex(MONTH_ORDER) for label, d in symbol_stats.items()}, names=["symbol", "month"]
    )
    result = pd.DataFrame(index=MONTH_ORDER)
    for c in cols:
        result[c] = combined.groupby(level="month")[c].mean().reindex(MONTH_ORDER)
    return result


def build_weekday_summary_table(
    market_stats: Optional[pd.DataFrame], trade_stats: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """追補§1.3-1: 曜日別サマリー表(7行)。市場統計(compute_weekday_stats)とトレード統計
    (compute_trade_weekday_stats)を結合する。トレード未読込(trade_stats=None)、または
    その曜日のトレードが0件の場合はエントリー回数/勝率/合計損益をNaN(style_cross_tableで「—」表示)にする。
    """
    cols = ["平均騰落率(%)", "平均出来高", "平均ボラティリティ(%)", "n(日数)", "エントリー回数", "勝率(%)", "合計損益(USD)"]
    rows = []
    for wd in WEEKDAY_LABELS:
        if market_stats is not None and wd in market_stats.index:
            m = market_stats.loc[wd]
            m_ret, m_vol, m_vola, m_n = m["avg_return_pct"], m["avg_volume"], m["avg_volatility_pct"], m["n_days"]
        else:
            m_ret = m_vol = m_vola = np.nan
            m_n = 0
        if trade_stats is not None and wd in trade_stats.index and trade_stats.loc[wd, "n_trades"] > 0:
            t = trade_stats.loc[wd]
            t_n, t_win, t_pnl = t["n_trades"], t["win_rate_pct"], t["total_pnl_usd"]
        else:
            t_n = t_win = t_pnl = np.nan
        rows.append({
            "平均騰落率(%)": m_ret, "平均出来高": m_vol, "平均ボラティリティ(%)": m_vola, "n(日数)": m_n,
            "エントリー回数": t_n, "勝率(%)": t_win, "合計損益(USD)": t_pnl,
        })
    return pd.DataFrame(rows, index=WEEKDAY_LABELS, columns=cols)


def build_month_summary_table(
    market_stats: Optional[pd.DataFrame], trade_stats: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """追補§2.4-1: 月別サマリー表(12行)。市場統計(compute_month_stats)とトレード統計
    (compute_trade_month_stats)を結合する。行インデックスは表示用に「1月」〜「12月」の文字列にする。
    """
    cols = [
        "平均騰落率(%)", "中央値騰落率(%)", "陽線率(%)", "平均ボラティリティ(%)", "n(年数)",
        "エントリー回数", "勝率(%)", "合計損益(USD)",
    ]
    month_index = [f"{mo}月" for mo in MONTH_ORDER]
    rows = []
    for mo in MONTH_ORDER:
        if market_stats is not None and mo in market_stats.index:
            m = market_stats.loc[mo]
            m_avg, m_med, m_pos = m["avg_return_pct"], m["median_return_pct"], m["pct_positive"]
            m_vola, m_n = m["avg_volatility_pct"], m["n_years"]
        else:
            m_avg = m_med = m_pos = m_vola = np.nan
            m_n = 0
        if trade_stats is not None and mo in trade_stats.index and trade_stats.loc[mo, "n_trades"] > 0:
            t = trade_stats.loc[mo]
            t_n, t_win, t_pnl = t["n_trades"], t["win_rate_pct"], t["total_pnl_usd"]
        else:
            t_n = t_win = t_pnl = np.nan
        rows.append({
            "平均騰落率(%)": m_avg, "中央値騰落率(%)": m_med, "陽線率(%)": m_pos,
            "平均ボラティリティ(%)": m_vola, "n(年数)": m_n,
            "エントリー回数": t_n, "勝率(%)": t_win, "合計損益(USD)": t_pnl,
        })
    return pd.DataFrame(rows, index=month_index, columns=cols)


def build_weekday_bar_charts(market_stats_by_symbol: dict[str, pd.DataFrame]) -> tuple[go.Figure, go.Figure]:
    """追補§1.3-2: 曜日別棒グラフ(平均騰落率・平均ボラの2枚、銘柄別グループ棒)。build_session_bar_chartsの曜日版。"""
    rows_ret = []
    rows_vola = []
    for label, stats in market_stats_by_symbol.items():
        s = stats.reindex(WEEKDAY_LABELS)
        for wd in WEEKDAY_LABELS:
            rows_ret.append({"weekday": wd, "symbol": label, "value": s.loc[wd, "avg_return_pct"], "n": s.loc[wd, "n_days"]})
            rows_vola.append({"weekday": wd, "symbol": label, "value": s.loc[wd, "avg_volatility_pct"], "n": s.loc[wd, "n_days"]})

    df_ret = pd.DataFrame(rows_ret)
    df_vola = pd.DataFrame(rows_vola)
    fig_ret = px.bar(
        df_ret, x="weekday", y="value", color="symbol", barmode="group", hover_data=["n"],
        template="plotly_dark", labels={"weekday": "曜日", "value": "平均騰落率(%)", "symbol": "銘柄", "n": "n(日数)"},
        category_orders={"weekday": WEEKDAY_LABELS},
    )
    fig_vola = px.bar(
        df_vola, x="weekday", y="value", color="symbol", barmode="group", hover_data=["n"],
        template="plotly_dark", labels={"weekday": "曜日", "value": "平均ボラティリティ(%)", "symbol": "銘柄", "n": "n(日数)"},
        category_orders={"weekday": WEEKDAY_LABELS},
    )
    for f in (fig_ret, fig_vola):
        f.update_layout(margin=dict(l=40, r=20, t=40, b=20), height=380)
    return fig_ret, fig_vola


def build_month_bar_chart(market_stats_by_symbol: dict[str, pd.DataFrame]) -> go.Figure:
    """追補§2.4-2: 月別騰落率棒グラフ(銘柄別グループ棒。hoverにnと中央値)。"""
    month_labels = [f"{mo}月" for mo in MONTH_ORDER]
    rows = []
    for label, stats in market_stats_by_symbol.items():
        s = stats.reindex(MONTH_ORDER)
        for mo, mo_label in zip(MONTH_ORDER, month_labels):
            rows.append({
                "month": mo_label, "symbol": label, "value": s.loc[mo, "avg_return_pct"],
                "n": s.loc[mo, "n_years"], "median": s.loc[mo, "median_return_pct"],
            })
    df = pd.DataFrame(rows)
    fig = px.bar(
        df, x="month", y="value", color="symbol", barmode="group", hover_data=["n", "median"],
        template="plotly_dark",
        labels={"month": "月", "value": "平均騰落率(%)", "symbol": "銘柄", "n": "n(年数)", "median": "中央値騰落率(%)"},
        category_orders={"month": month_labels},
    )
    fig.update_layout(margin=dict(l=40, r=20, t=40, b=20), height=400)
    return fig


def build_session_weekday_heatmap(return_matrix: pd.DataFrame, n_matrix: pd.DataFrame) -> go.Figure:
    """追補§1.3-3: セッション×曜日ヒートマップ(行=詳細9帯、列=7曜日、値=平均騰落率%)。
    ゼロ中心発散配色。各セルのhoverにn(サンプル日数)。
    """
    mat = return_matrix.reindex(index=BAND_ORDER, columns=WEEKDAY_LABELS)
    n_mat = n_matrix.reindex(index=BAND_ORDER, columns=WEEKDAY_LABELS)
    vals = mat.to_numpy()
    abs_vals = np.abs(vals[~np.isnan(vals)])
    max_abs = float(abs_vals.max()) if abs_vals.size else 1.0
    max_abs = max_abs if max_abs > 0 else 1.0
    text = [["—" if pd.isna(v) else f"{v:+.2f}" for v in row] for row in vals]
    fig = go.Figure(data=go.Heatmap(
        z=vals, x=WEEKDAY_LABELS, y=BAND_ORDER, customdata=n_mat.to_numpy(),
        colorscale="RdYlGn", zmid=0, zmin=-max_abs, zmax=max_abs,
        text=text, texttemplate="%{text}",
        hovertemplate="帯=%{y}<br>曜日=%{x}<br>平均騰落率=%{z:.3f}%<br>n(日数)=%{customdata}<extra></extra>",
        colorbar=dict(title="平均騰落率(%)"),
    ))
    fig.update_layout(
        template="plotly_dark", margin=dict(l=40, r=20, t=40, b=20), height=480,
        xaxis_title="曜日", yaxis_title="詳細帯",
    )
    return fig


def build_trade_session_weekday_heatmap(win_matrix: pd.DataFrame, n_matrix: pd.DataFrame) -> go.Figure:
    """追補§1.3-4: トレードのセッション×曜日 勝率ヒートマップ(行=詳細9帯、列=7曜日、値=勝率%)。
    セル注釈="勝率% (n)"。n<3の注意喚起は呼び出し側(render関数)のキャプションで行う。
    """
    mat = win_matrix.reindex(index=BAND_ORDER, columns=WEEKDAY_LABELS)
    n_mat = n_matrix.reindex(index=BAND_ORDER, columns=WEEKDAY_LABELS)
    vals = mat.to_numpy()
    n_vals = n_mat.to_numpy()
    text = [
        ["—" if pd.isna(v) else f"{v:.1f}% ({int(n)})" for v, n in zip(row, n_row)]
        for row, n_row in zip(vals, n_vals)
    ]
    fig = go.Figure(data=go.Heatmap(
        z=vals, x=WEEKDAY_LABELS, y=BAND_ORDER, customdata=n_vals,
        colorscale="RdYlGn", zmid=50, zmin=0, zmax=100,
        text=text, texttemplate="%{text}",
        hovertemplate="帯=%{y}<br>曜日=%{x}<br>勝率=%{z:.1f}%<br>n(件数)=%{customdata}<extra></extra>",
        colorbar=dict(title="勝率(%)"),
    ))
    fig.update_layout(
        template="plotly_dark", margin=dict(l=40, r=20, t=40, b=20), height=480,
        xaxis_title="曜日", yaxis_title="詳細帯",
    )
    return fig


def inject_custom_css() -> None:
    st.markdown(
        """
        <style>
        div[data-testid="stMetric"] {
            background-color: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px;
            padding: 12px 14px 8px 14px;
        }
        div[data-testid="stTabs"] button[data-baseweb="tab"] {
            padding-top: 8px;
            padding-bottom: 8px;
        }
        .block-container { padding-top: 2rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def trades_csv_template_bytes() -> bytes:
    """§4.4 テンプレートCSVダウンロード用のバイト列(ヘッダ行のみ、utf-8-sig)。
    追補v4§2.1: Side/Leverage/Entry_Price/Exit_Price(全て任意列)もヘッダに含める。
    """
    cols = [
        "Entry_Time(JST)", "Exit_Time(JST)", "Symbol", "PnL_USD", "PnL_Percent", "Win_Loss",
        "Side", "Leverage", "Entry_Price", "Exit_Price",
    ]
    df = pd.DataFrame(columns=cols)
    return df.to_csv(index=False).encode("utf-8-sig")


# =====================================================================================
# データ取得+集計の束ね(UI層から呼ばれる。st.*を使うためselftest対象外)
# =====================================================================================


@st.cache_data(ttl=HOURLY_CACHE_TTL_SEC, show_spinner=False)
def compute_bundle(
    labels: list[str], start_date_: date, end_date_: date,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, dict[str, Any]], list[str]]:
    """指定銘柄群のOHLCVを取得し、各銘柄の compute_session_stats を計算する。

    戻り値: (label -> OHLCVデータ, label -> 帯別統計, label -> 追補v3§4のフェッチメタ, エラーメッセージ一覧)

    (labels, start_date_, end_date_) が同一の呼び出しは15分キャッシュする。内部で呼ぶ
    fetch_ccxt_ohlcv/fetch_yfinance_ohlcv も個別にキャッシュ済みだが、無関係なウィジェット
    操作によるrerun毎にcompute_session_stats等のCPU再計算が走るのを避けるため、束ね全体もキャッシュする。
    """
    if not labels:
        return {}, {}, {}, []
    data, meta, errors = load_all_symbol_data(labels, {}, start_date_, end_date_)
    stats_by_symbol: dict[str, pd.DataFrame] = {}
    for label, df in data.items():
        try:
            stats_by_symbol[label] = compute_session_stats(df)
        except Exception as e:  # noqa: BLE001 - ユーザー向けメッセージに変換して継続
            errors.append(f"{label}: セッション集計に失敗しました ({e})")
    return data, stats_by_symbol, meta, errors


def _period_caption(period: tuple[date, date]) -> str:
    start_, end_ = period
    n_days = (end_ - start_).days
    return f"集計期間: {start_.isoformat()} 〜 {end_.isoformat()}（{n_days}日間）"


def format_data_source_caption(
    label: str, meta: Optional[dict[str, Any]], cache_ttl_min: int,
) -> str:
    """追補v3§4: 1銘柄分のフェッチメタをキャプション文字列に整形する(st非依存の純関数)。

    fetched_at/last_barは meta にフェッチ実行時に記録された値をそのまま表示する
    (描画時のdatetime.now()での代用は「取得時刻の詐称」になるため禁止=仕様の明示要件)。
    """
    if not meta:
        return f"📊 データ元: {label}=不明(メタ情報なし)"
    source = meta.get("source") or "不明"
    ticker = meta.get("ticker") or "?"
    fetched_at = meta.get("fetched_at")
    last_bar = meta.get("last_bar")
    fetched_str = fetched_at.strftime("%Y-%m-%d %H:%M") if fetched_at is not None else "不明"
    last_bar_str = last_bar.strftime("%m-%d %H:%M") if last_bar is not None else "不明"
    return (
        f"📊 データ元: {label}={source}({ticker}) | "
        f"⏱ 取得: {fetched_str} JST時点(キャッシュ{cache_ttl_min}分) | "
        f"最終足: {last_bar_str} JST"
    )


# =====================================================================================
# UI描画: タブ本体 (st.* 呼び出しを含む。main()から呼ばれる)
# =====================================================================================


def render_cross_table_tab(
    stats_a: dict[str, pd.DataFrame],
    trade_datasets: Optional[dict[str, dict[str, Any]]],
    period_a: tuple[date, date],
    compare_mode: bool,
    stats_b: Optional[dict[str, pd.DataFrame]],
    period_b: Optional[tuple[date, date]],
    selected_labels: list[str],
    meta_a: Optional[dict[str, dict[str, Any]]] = None,
    meta_b: Optional[dict[str, dict[str, Any]]] = None,
) -> None:
    if not selected_labels:
        st.info("サイドバーで銘柄を1つ以上選択してください。")
        return
    if not stats_a and not (stats_b or {}):
        st.warning("市場データが1件も取得できませんでした。トレード統計のみで表示します。")

    # 追補v4§1.2: 複数トレードデータセット時のみ「トレード統計の対象」selectboxを表示する
    # (1データセット以下なら従来どおりselectbox非表示・自動選択=後方互換)。
    trade_labels = list(trade_datasets.keys()) if trade_datasets else []
    if len(trade_labels) >= 2:
        active_trade_label = st.selectbox(
            "トレード統計の対象", trade_labels, key="cross_table_trade_dataset_choice"
        )
    else:
        active_trade_label = trade_labels[0] if trade_labels else None
    trade_stats = trade_datasets[active_trade_label]["stats"] if active_trade_label else None

    options = ["全選択銘柄の平均"] + selected_labels
    choice = st.selectbox("銘柄セレクタ", options, key="cross_table_symbol_choice")

    def _pick(stats_by_symbol: dict[str, pd.DataFrame]) -> Optional[pd.DataFrame]:
        if not stats_by_symbol:
            return None
        if choice == "全選択銘柄の平均":
            return aggregate_market_stats_multi(stats_by_symbol)
        return stats_by_symbol.get(choice)

    def _panel(
        stats_by_symbol: dict[str, pd.DataFrame], period: tuple[date, date], col_title: str,
        meta_by_symbol: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        market_stats = _pick(stats_by_symbol)
        cross = build_cross_table(market_stats, trade_stats)
        st.markdown(f"**{col_title}**  {_period_caption(period)}")
        # 追補v3§4: データ取得元キャプション(「全選択銘柄の平均」なら寄与銘柄すべてを列挙)
        source_labels = (
            list(stats_by_symbol.keys()) if choice == "全選択銘柄の平均"
            else ([choice] if choice in stats_by_symbol else [])
        )
        for lbl in source_labels:
            st.caption(format_data_source_caption(lbl, (meta_by_symbol or {}).get(lbl), HOURLY_CACHE_TTL_MIN))
        st.caption("※重複時間帯(16時台・21時-2時)は一意割当で集計(二重計上なし)")
        if trade_stats is not None:
            ov = trade_stats["overall"]
            if ov.get("first_time") is not None and ov.get("last_time") is not None:
                st.caption(
                    "トレード集計期間(全件・選択期間でフィルタしない): "
                    f"{ov['first_time'].date().isoformat()} 〜 {ov['last_time'].date().isoformat()}"
                )
        # 追補v3 §2: 親表(大枠計4行)+大枠ごとのst.expander(既定折りたたみ)にアコーディオン化。
        # 濃淡vmaxは分割前のcross全体(親+詳細13行)から計算し、親表/各詳細表で共通スケールにする。
        vmax = compute_diverging_vmax(cross, DIVERGING_RETURN_COLUMNS)
        parent_df, detail_by_parent = split_cross_table(cross)
        st.dataframe(style_cross_table(parent_df, diverging_vmax=vmax), width="stretch")
        for parent in PARENT_ORDER:
            with st.expander(f"{parent} の詳細帯", expanded=False, key=f"cross_expander_{col_title}_{parent}"):
                st.dataframe(
                    style_cross_table(detail_by_parent[parent], diverging_vmax=vmax), width="stretch"
                )
        csv_bytes = cross.to_csv(index=True).encode("utf-8-sig")
        st.download_button(
            "CSVダウンロード", data=csv_bytes, file_name=f"cross_table_{col_title}.csv",
            mime="text/csv", key=f"dl_cross_{col_title}",
        )

    if compare_mode and stats_b is not None and period_b is not None:
        c1, c2 = st.columns(2)
        with c1:
            _panel(stats_a, period_a, "Period A", meta_a)
        with c2:
            _panel(stats_b, period_b, "Period B", meta_b)
    else:
        _panel(stats_a, period_a, "集計結果", meta_a)


def _prepare_intraday_chart_data(
    timeframe_choice: str, period: tuple[date, date], labels: list[str],
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, Any]], dict[str, int], bool]:
    """追補v4§2.4A: チャートタブで分足(1m/5m/15m)が選択された場合のフェッチ+バー数ガード適用。

    セッション集計(クロス表・セッション分析・曜日/月別)は引数dataとは別系統(main()のdata_a、
    常に1h・選択期間全体)のためこの関数の影響を受けない=「集計は不変」の仕様を満たす。
    戻り値: (label->分足df, label->メタ, label->キャッシュ分, 描画続行可否)。
    """
    date_start_, date_end_ = period
    eff_start, eff_end, truncated = compute_intraday_chart_window(date_start_, date_end_, timeframe_choice)
    if truncated:
        guard_days = INTRADAY_WINDOW_GUARD_DAYS[timeframe_choice]
        st.warning(
            f"{timeframe_choice}は直近{guard_days}日のみチャート表示します"
            "(セッション集計は従来どおり1時間足で選択期間全体を対象にしています)。"
        )
    with st.spinner(f"{timeframe_choice}データを取得中..."):
        idata, imeta, errors, fallback_notes, ttl_by_label = load_intraday_chart_bundle(
            labels, {}, eff_start, eff_end, timeframe_choice,
        )
    for e in errors:
        st.warning(e)
    for note in fallback_notes:
        st.caption(f"ℹ️ {note}")
    if not idata:
        st.error(f"{timeframe_choice}のデータを1件も取得できませんでした。")
        return {}, {}, {}, False
    return idata, imeta, ttl_by_label, True


def render_click_detail_panel(
    click_event: Any,
    data: dict[str, pd.DataFrame],
    base_map: dict[str, float],
    trade_rows: dict[str, pd.DataFrame],
) -> None:
    """追補v5§3: 欄外パネル(クリックで固定)の描画。build_click_detail()の結果を日本語表で表示する。"""
    st.markdown("**📌 クリック詳細**")
    click_time = None
    if isinstance(click_event, dict):
        points = (click_event.get("selection") or {}).get("points") or []
        if points:
            click_time = points[0].get("x")
    details = build_click_detail(click_time, data, base_map, trade_rows) if click_time else []
    if not details:
        st.caption("チャートをクリックするとその時刻の詳細をここに表示します。")
        return
    rows = []
    for d in details:
        rows.append({
            "銘柄": d["label"],
            "日時(JST)": pd.Timestamp(d["time"]).strftime("%Y-%m-%d %H:%M"),
            "始値": f"{d['open']:,.2f}", "高値": f"{d['high']:,.2f}",
            "安値": f"{d['low']:,.2f}", "終値": f"{d['close']:,.2f}",
            "騰落率(%)": f"{d['return_pct']:+.2f}" if np.isfinite(d["return_pct"]) else "—",
            "出来高": f"{d['volume']:,.0f}" if np.isfinite(d["volume"]) else "—",
            "累積騰落率(%)": f"{d['cum_return_pct']:+.2f}" if np.isfinite(d["cum_return_pct"]) else "—",
        })
    st.dataframe(pd.DataFrame(rows).set_index("銘柄"), width="stretch")

    trade_lines = []
    for d in details:
        for tr in d.get("trades", []):
            kind = "/".join([k for k, v in (("エントリー", tr["is_entry"]), ("決済", tr["is_exit"])) if v]) or "—"
            lev_lbl = f"{tr['leverage']:.0f}x" if pd.notna(tr.get("leverage")) else "—"
            pnl_lbl = f"{tr['pnl_usd']:+.2f}USD" if pd.notna(tr.get("pnl_usd")) else "—"
            trade_lines.append({
                "銘柄": d["label"], "データセット": tr.get("dataset_label", ""), "種別": kind,
                "Side": _trade_side_label(tr.get("side")), "レバ": lev_lbl, "PnL": pnl_lbl,
            })
    if trade_lines:
        st.caption("該当トレード")
        st.dataframe(pd.DataFrame(trade_lines).set_index("銘柄"), width="stretch")


def render_chart_tab(
    data: dict[str, pd.DataFrame], timeframe_choice: str, show_bg: bool, period: tuple[date, date],
    bg_opacity: float = BG_OPACITY, meta: Optional[dict[str, dict[str, Any]]] = None,
    selected_labels: Optional[list[str]] = None,
    trade_datasets: Optional[dict[str, dict[str, Any]]] = None,
    show_trade_markers: bool = False,
    selected_marker_labels: Optional[list[str]] = None,
) -> None:
    if not data:
        st.info("サイドバーで銘柄を1つ以上選択してください。")
        return
    st.caption(_period_caption(period))

    if timeframe_choice in INTRADAY_INTERVAL_MAP:
        chart_data, chart_meta, ttl_by_label, ok = _prepare_intraday_chart_data(
            timeframe_choice, period, selected_labels or list(data.keys()),
        )
        if not ok:
            return
        rule = None
    else:
        chart_data, chart_meta = data, (meta or {})
        ttl_by_label = {label: HOURLY_CACHE_TTL_MIN for label in chart_data}
        rule = TIMEFRAME_RULES.get(timeframe_choice)

    for label in chart_data:
        st.caption(format_data_source_caption(label, chart_meta.get(label), ttl_by_label.get(label, HOURLY_CACHE_TTL_MIN)))

    # 追補v4§2.2: トレードマーカー用overlay構築(サイドバー④トグルON+データセット選択時のみ)。
    overlays: dict[str, list[dict[str, Any]]] = {}
    if show_trade_markers and trade_datasets:
        overlays = build_trade_overlays_for_chart(
            list(chart_data.keys()), trade_datasets, selected_marker_labels or [],
        )

    # v6.4: 詳細表示はチャート内ホバー(x unified)に一本化。
    # 欄外パネル(クリックで固定)はユーザー要望で廃止(build_candlestick_chartの
    # clickモード自体はselftest互換のため関数として残置)。
    detail_mode = "hover"

    # v6.4: 背景帯は表示期間14日以内のみ描画(90日等では1日毎の色帯が数百枚のSVG矩形になり、
    # 縞模様で判読不能な上にスクロールが重くなる主因だった)。
    period_days = (period[1] - period[0]).days if (period and period[0] and period[1]) else 0
    effective_bg = show_bg and period_days <= 14
    if show_bg and not effective_bg:
        st.caption(
            "🎨 セッション背景帯は表示期間14日以内のときのみ描画します"
            "(長期間では縞模様になり判読できず、描画負荷も大きいため)。"
        )

    try:
        fig, skipped, marker_count = build_candlestick_chart(
            chart_data, rule, effective_bg, bg_opacity, overlays, detail_mode=detail_mode,
        )
    except Exception as e:  # noqa: BLE001
        st.error(f"チャート生成に失敗しました: {e}")
        return

    # v6.3: 自動間引きの案内(本体build_candlestick_chart内と同じ判定を再現して表示)。
    _lens: list[int] = []
    _longest: Optional[pd.DataFrame] = None
    for label, df in chart_data.items():
        if label in skipped:
            continue
        d = resample_ohlcv(df, rule) if rule else df
        if d is None or d.empty:
            continue
        _lens.append(len(d))
        if _longest is None or len(d) > len(_longest):
            _longest = d
    if _lens and _longest is not None:
        _deci = _auto_decimation_rule(max(_lens), _median_bar_minutes(_longest))
        if _deci:
            st.caption(
                f"⚡ 描画負荷対策: バー数が上限({CHART_MAX_BARS_PER_ROW:,}本/行)を超えるため、"
                f"表示を約{_deci}の足へ自動間引きしています(統計集計・🔍ズームビューは元の足のまま)。"
            )
    if skipped:
        st.warning(f"次の銘柄は指定期間・足種でのリサンプル後にデータが空のため、チャートから除外しました: {', '.join(skipped)}")
    # 追補v5§1: ボラチャート(1段目)+価格チャート(2段目以降)の2段構成見出し。
    st.subheader("🌊 ボラチャート")
    st.caption(
        "各銘柄の値幅(何%動いたか)を比較するチャート。期間先頭を0%とした累積騰落率で表示。"
        "実際の価格は下の💹価格チャートを参照。"
    )
    # 追補v5§3: ホバーラベルをカーソル位置から少し離す。plotlyのSVGホバーラベルはtransform属性で
    # 配置されるが、CSSのtranslateプロパティはtransform属性と別枠で合成される仕様のため、
    # ここに指定した値は既存のtransformに対する相対オフセットとして働く(上書きにならない)。
    st.markdown(
        "<style>.stPlotlyChart .hoverlayer g.hovertext{translate:14px -18px;}</style>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(fig, width="stretch")  # v6.4: hover一本化(on_select/クリック捕捉は廃止)
    if show_trade_markers and trade_datasets:
        # 追補v4§2.2: マーカーサイズのレバ段階スケール説明+銘柄照合ルールの明記(脚注/hoverいずれかで良い旨の要求を脚注で満たす)。
        st.caption(
            "▲LONG/▼SHORT/◆Side不明=エントリー、✕=決済。点線は勝敗色(緑=勝ち/赤=負け/グレー=引分・不明)。"
            "マーカーサイズはレバレッジ段階(小=低レバ〜大=25x以上、詳細はhover参照)。"
            "データセットごとに輪郭色を分けて凡例表示。銘柄の一致判定はトレードCSVのSymbol列とチャート銘柄の"
            "表示ラベル/実ティッカーを英数字のみ・大文字に正規化した上で完全一致または部分一致(どちらかが"
            "他方を含む)で照合している。"
        )
    if show_trade_markers and trade_datasets and marker_count == 0:
        st.caption(
            "ℹ️ 表示期間内に該当トレードなし(選択中の銘柄と一致するSymbolのトレードが期間外、"
            "または銘柄名が一致しませんでした)。"
        )
    col1, col2 = st.columns([1, 2])
    with col1:
        if st.button("📷 PNGを生成", key="png_gen_btn"):
            with st.spinner("PNG生成中(kaleido)..."):
                png_bytes = try_export_png(fig)
            if png_bytes:
                st.session_state["_chart_png_bytes"] = png_bytes
            else:
                st.session_state["_chart_png_bytes"] = None
                st.warning(
                    "PNG生成に失敗しました(Google ChromeまたはMicrosoft Edgeが見つからない可能性があります)。"
                    "HTMLダウンロードをご利用いただくか、コマンドラインで `kaleido_get_chrome` を一度実行してください。"
                )
        png_bytes = st.session_state.get("_chart_png_bytes")
        if png_bytes:
            st.download_button(
                "PNGを保存", data=png_bytes, file_name="session_chart.png", mime="image/png", key="png_dl_btn",
            )
        else:
            html_bytes = fig.to_html().encode("utf-8")
            st.download_button(
                "HTMLをダウンロード(PNG失敗時の代替)", data=html_bytes, file_name="session_chart.html",
                mime="text/html", key="html_dl_btn",
            )
    with col2:
        st.caption("チャート右上のモードバーのカメラアイコンからもPNG保存できます。")


def fetch_zoom_window_ohlcv(
    symbol_label: str, window_start: pd.Timestamp, window_end: pd.Timestamp, timeframe_choice: str,
) -> tuple[pd.DataFrame, dict[str, Any], Optional[str], int, str]:
    """追補v4§2.4B: 🔍トレードズームビュー用のオンデマンド取得。
    timeframe_choiceから開始し、取得失敗(0件含む)なら ZOOM_TIMEFRAME_CHOICES 上でより粗い足へ
    段階的にフォールバックする(1分->5分->15分->1時間。1時間より粗い代替はない)。
    戻り値: (窓([window_start, window_end])に絞り込み済みdf, meta, 注記 or None, キャッシュTTL分, 実際に使った足種)。
    """
    routing = get_symbol_routing(symbol_label)
    start_date_ = window_start.date()
    end_date_ = window_end.date()
    if timeframe_choice in ZOOM_TIMEFRAME_CHOICES:
        ladder = ZOOM_TIMEFRAME_CHOICES[ZOOM_TIMEFRAME_CHOICES.index(timeframe_choice):]
    else:
        ladder = ["1時間足"]

    attempts: list[str] = []
    for choice in ladder:
        try:
            if choice in INTRADAY_INTERVAL_MAP:
                df, meta, note, ttl_min = load_symbol_data_intraday(
                    symbol_label, routing, start_date_, end_date_, choice,
                )
            else:
                df, meta = load_symbol_data(symbol_label, routing, start_date_, end_date_)
                note, ttl_min = None, HOURLY_CACHE_TTL_MIN
            trimmed = df[(df.index >= window_start) & (df.index <= window_end)]
            if trimmed.empty:
                raise ValueError("取得窓内にデータがありませんでした")
            if choice != timeframe_choice:
                fb_msg = f"{timeframe_choice}の取得に失敗したため{choice}にフォールバックしました。"
                note = f"{note} {fb_msg}" if note else fb_msg
            return trimmed, meta, note, ttl_min, choice
        except Exception as e:  # noqa: BLE001
            attempts.append(f"{choice}: {e}")
            continue
    raise ValueError("いずれの足種でも取得できませんでした(" + " / ".join(attempts) + ")")


def render_trade_zoom_section(
    trade_datasets: dict[str, dict[str, Any]], show_bg: bool, bg_opacity: float,
) -> None:
    """追補v4§2.4B: 🔍トレードズームビュー(チャートタブ下部)。トレード読込時のみ出現する。"""
    if not trade_datasets:
        return
    st.markdown("---")
    st.subheader("🔍 トレードズームビュー")

    ds_labels = list(trade_datasets.keys())
    # v6.5: 複数データセット読込時は「まとめて比較」を選択肢に追加(ユーザー要望:
    # 師匠と弟子のエントリー/利確を同じ窓で見比べてディスカッションする使い方)。
    _COMBINED = "🤝 まとめて比較(全データセット)"
    ds_choices = ([_COMBINED] + ds_labels) if len(ds_labels) > 1 else ds_labels
    if st.session_state.get("zoom_ds_select") not in ds_choices:
        st.session_state["zoom_ds_select"] = ds_choices[0]
    zoom_ds_label = (
        st.selectbox("データセット", ds_choices, key="zoom_ds_select") if len(ds_choices) > 1 else ds_choices[0]
    )
    combined_mode = zoom_ds_label == _COMBINED

    if combined_mode:
        options = []
        for dsl in ds_labels:
            for lbl, idx in build_zoom_trade_options(trade_datasets[dsl]["trades_assigned"]):
                options.append((f"[{dsl}] {lbl}", (dsl, idx)))
    else:
        options = [
            (lbl, (zoom_ds_label, idx))
            for lbl, idx in build_zoom_trade_options(trade_datasets[zoom_ds_label]["trades_assigned"])
        ]
    if not options:
        st.info("このデータセットにはトレードがありません。")
        return
    option_labels = [o[0] for o in options]
    sel_key = f"zoom_trade_select__{zoom_ds_label}"
    if st.session_state.get(sel_key) not in option_labels:
        st.session_state[sel_key] = option_labels[-1]
    chosen_label = st.selectbox("トレードを選択(この時刻を中心に窓を切ります)", option_labels, key=sel_key)
    chosen_ds, chosen_idx = dict(options)[chosen_label]
    trades_df = trade_datasets[chosen_ds]["trades_assigned"]
    row = trades_df.loc[chosen_idx]

    entry_time = row["Entry_Time"]
    exit_time = row.get("Exit_Time")
    symbol = row["Symbol"]
    window_start, window_end = compute_zoom_window(entry_time, exit_time)
    window_hours = (window_end - window_start).total_seconds() / 3600.0
    routing = get_symbol_routing(symbol)
    source = routing.get("source", "yfinance")
    now_jst = pd.Timestamp.now(tz="Asia/Tokyo")
    trade_age_days = max((now_jst - entry_time).total_seconds() / 86400.0, 0.0)
    auto_choice, auto_reason = resolve_zoom_timeframe(source, window_hours, trade_age_days)

    manual_options = ["自動"] + ZOOM_TIMEFRAME_CHOICES
    manual_pick = st.selectbox("足種(自動を手動上書き可)", manual_options, index=0, key="zoom_tf_manual")
    timeframe_choice = auto_choice if manual_pick == "自動" else manual_pick
    st.caption(f"自動選択の足種: {auto_choice}" + (f"({auto_reason})" if auto_reason else ""))

    with st.spinner(f"{symbol}周辺のデータを取得中..."):
        try:
            ohlcv_df, meta, note, ttl_min, used_choice = fetch_zoom_window_ohlcv(
                symbol, window_start, window_end, timeframe_choice,
            )
        except Exception as e:  # noqa: BLE001
            st.error(f"ズームビュー用データの取得に失敗しました: {e}")
            return
    if note:
        st.caption(f"ℹ️ {note}")

    single_row_df = trades_df.loc[[chosen_idx]]
    mapped = map_trades_to_chart(single_row_df, ohlcv_df, None)
    if mapped.empty:
        st.warning("取得したデータの範囲にこのトレードを描画できませんでした。")
        return
    m = mapped.iloc[0]

    fig = make_subplots(rows=1, cols=1)
    colors = get_symbol_colors(symbol)
    fig.add_trace(
        go.Candlestick(
            x=ohlcv_df.index, open=ohlcv_df["open"], high=ohlcv_df["high"],
            low=ohlcv_df["low"], close=ohlcv_df["close"],
            increasing_line_color=colors["inc"], decreasing_line_color=colors["dec"],
            increasing_fillcolor=colors["inc"], decreasing_fillcolor=colors["dec"],
            name=symbol, hovertemplate=_ohlc_hovertemplate(is_pct=False),  # 追補v5§3/§4: 日本語hover
        ),
        row=1, col=1,
    )
    if show_bg:
        fig.update_layout(
            shapes=build_session_background_shapes(ohlcv_df.index.min(), ohlcv_df.index.max(), bg_opacity)
        )

    # v6.5: まとめて比較モードでは、窓内に入る全データセットの同銘柄トレードを重ね描きする
    # (基準=選択トレード。単独モードは従来どおり選択1件のみ)。
    if combined_mode:
        overlays_zoom: list[dict[str, Any]] = []
        for k, dsl in enumerate(ds_labels):
            tdf = trade_datasets[dsl]["trades_assigned"]
            try:
                same_sym = tdf[tdf["Symbol"].astype(str).str.upper() == str(symbol).upper()]
                inwin = same_sym[
                    (same_sym["Entry_Time"] <= window_end)
                    & (same_sym["Exit_Time"].fillna(same_sym["Entry_Time"]) >= window_start)
                ]
            except Exception:  # noqa: BLE001 — 列欠落等はそのデータセットのみスキップ
                continue
            if inwin.empty:
                continue
            overlays_zoom.append(
                {"dataset_label": dsl, "trades_df": inwin, "color": get_trade_marker_color(dsl, k)}
            )
        n_shown = sum(len(ov["trades_df"]) for ov in overlays_zoom)
        st.caption(f"🤝 比較モード: この窓に入る全データセットの{symbol}トレード{n_shown}件を重ね描きしています。")
    else:
        overlays_zoom = [
            {"dataset_label": chosen_ds, "trades_df": single_row_df,
             "color": get_trade_marker_color(chosen_ds, 0)}
        ]
    add_trade_overlay_traces(fig, ohlcv_df, overlays_zoom, None, row=1)

    entry_approx = "≈近似" if m["entry_price_is_approx"] else "実価格"
    fig.add_hline(
        y=m["entry_price"], line_dash="dot", line_color="#AAAAAA",
        annotation_text=f"Entry {m['entry_price']:.2f}({entry_approx})", annotation_position="top left",
    )
    if pd.notna(m["exit_price"]):
        exit_approx = "≈近似" if m["exit_price_is_approx"] else "実価格"
        fig.add_hline(
            y=m["exit_price"], line_dash="dot", line_color="#888888",
            annotation_text=f"Exit {m['exit_price']:.2f}({exit_approx})", annotation_position="bottom left",
        )

    fig.update_layout(
        template="plotly_dark", xaxis_rangeslider_visible=False, hovermode="x unified",
        height=450, margin=dict(l=10, r=10, t=30, b=10),
        hoverlabel=dict(
            font_size=14, font_color="#FFFFFF", bgcolor="rgba(13,17,23,0.96)",
            bordercolor="rgba(160,160,160,0.7)", align="left",
        ),  # v6.2: チャートタブと同一の読みやすいホバー様式
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(format_data_source_caption(symbol, meta, ttl_min))
    side_lbl = _trade_side_label(row.get("Side"))
    lev_lbl = f"{row['Leverage']:.0f}x" if pd.notna(row.get("Leverage")) else "—"
    pnl_lbl = f"{row['PnL_USD']:+.2f}USD" if pd.notna(row.get("PnL_USD")) else "—"
    hold_lbl = format_holding_duration(m["holding_minutes"])
    # v6.5: リスク指標(このズーム窓の細かい足で算出=メインチャートのhoverより精密)
    margin_lbl, mae_lbl, rr_lbl = _trade_risk_labels(m, ohlcv_df)
    st.caption(
        f"Side: {side_lbl} / レバ: {lev_lbl} / PnL: {pnl_lbl} / 保有時間: {hold_lbl} / "
        f"証拠金(推定): {margin_lbl} / 最大逆行(MAE): {mae_lbl} / RR(実績): {rr_lbl} / "
        f"実際に使用した足種: {used_choice}"
    )


def render_session_analysis_tab(
    data_a: dict[str, pd.DataFrame],
    stats_a: dict[str, pd.DataFrame],
    period_a: tuple[date, date],
    compare_mode: bool,
    data_b: dict[str, pd.DataFrame],
    stats_b: dict[str, pd.DataFrame],
    period_b: Optional[tuple[date, date]],
    meta_a: Optional[dict[str, dict[str, Any]]] = None,
    meta_b: Optional[dict[str, dict[str, Any]]] = None,
) -> None:
    if not data_a:
        st.info("サイドバーで銘柄を1つ以上選択してください。")
        return

    def _panel(
        data: dict[str, pd.DataFrame], stats: dict[str, pd.DataFrame], period: tuple[date, date], title: str,
        meta: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        st.markdown(f"**{title}**  {_period_caption(period)}")
        for lbl in data:
            st.caption(format_data_source_caption(lbl, (meta or {}).get(lbl), HOURLY_CACHE_TTL_MIN))
        if not stats:
            st.warning("市場データがないため集計できません。")
            return
        fig_ret, fig_vola = build_session_bar_charts(stats)
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(fig_ret, width="stretch")
        with c2:
            st.plotly_chart(fig_vola, width="stretch")

        st.markdown("相関ヒートマップ(1h対数リターン・ピアソン)")
        if len(data) >= 2:
            st.plotly_chart(build_correlation_heatmap(data), width="stretch")
        else:
            st.info("相関ヒートマップは銘柄を2つ以上選択すると表示されます。")

        st.markdown("セッション×銘柄 ヒートマップ(平均騰落率%)")
        st.plotly_chart(build_session_symbol_heatmap(stats), width="stretch")

    if compare_mode and data_b and period_b is not None:
        c1, c2 = st.columns(2)
        with c1:
            _panel(data_a, stats_a, period_a, "Period A", meta_a)
        with c2:
            _panel(data_b, stats_b, period_b, "Period B", meta_b)
    else:
        _panel(data_a, stats_a, period_a, "集計結果", meta_a)


def render_weekday_section(
    data_a: dict[str, pd.DataFrame],
    period_a: tuple[date, date],
    compare_mode: bool,
    data_b: dict[str, pd.DataFrame],
    period_b: Optional[tuple[date, date]],
    selected_labels: list[str],
    trades_assigned: Optional[pd.DataFrame],
    trade_stats_global: Optional[dict[str, Any]] = None,
    meta_a: Optional[dict[str, dict[str, Any]]] = None,
    meta_b: Optional[dict[str, dict[str, Any]]] = None,
) -> None:
    """追補§1: 曜日別アノマリーセクション。選択期間の1hデータ(既取得分。追加フェッチなし)から算出する。"""
    st.markdown("### 曜日別アノマリー")
    if not data_a:
        st.info("サイドバーで銘柄を1つ以上選択してください。")
        return

    # 確定指摘対応: 曜日別サマリー表・勝率ヒートマップのトレード由来列は市場統計の表示期間
    # (選択期間)とは独立に「アップロード済み全トレード」から計算される(選択期間でフィルタしない)。
    # render_cross_table_tab と同じ文言・粒度でその旨を明記する(掟6・既存パターンとの一貫性)。
    trade_period_caption: Optional[str] = None
    if trade_stats_global is not None:
        ov_wd = trade_stats_global.get("overall", {})
        if ov_wd.get("first_time") is not None and ov_wd.get("last_time") is not None:
            trade_period_caption = (
                "トレード集計期間(全件・選択期間でフィルタしない): "
                f"{ov_wd['first_time'].date().isoformat()} 〜 {ov_wd['last_time'].date().isoformat()}"
            )

    weekday_stats_a: dict[str, pd.DataFrame] = {}
    for label, df in data_a.items():
        try:
            weekday_stats_a[label] = compute_weekday_stats(df)
        except Exception as e:  # noqa: BLE001
            st.warning(f"{label}: 曜日別集計に失敗しました ({e})")

    weekday_stats_b: dict[str, pd.DataFrame] = {}
    if compare_mode and data_b:
        for label, df in data_b.items():
            try:
                weekday_stats_b[label] = compute_weekday_stats(df)
            except Exception as e:  # noqa: BLE001
                st.warning(f"Period B {label}: 曜日別集計に失敗しました ({e})")

    trade_wd_stats: Optional[pd.DataFrame] = None
    if trades_assigned is not None and not trades_assigned.empty:
        try:
            trade_wd_stats = compute_trade_weekday_stats(trades_assigned)
        except Exception as e:  # noqa: BLE001
            st.warning(f"トレードの曜日別集計に失敗しました: {e}")

    options = ["全選択銘柄の平均"] + selected_labels
    choice = st.selectbox("銘柄セレクタ(曜日別)", options, key="weekday_symbol_choice")

    def _pick(stats_by_symbol: dict[str, pd.DataFrame]) -> Optional[pd.DataFrame]:
        if not stats_by_symbol:
            return None
        if choice == "全選択銘柄の平均":
            return aggregate_weekday_stats_multi(stats_by_symbol)
        return stats_by_symbol.get(choice)

    def _panel(
        data: dict[str, pd.DataFrame], wd_stats_by_symbol: dict[str, pd.DataFrame],
        period: tuple[date, date], title: str, meta: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        st.markdown(f"**{title}**  {_period_caption(period)}")
        source_labels = (
            list(data.keys()) if choice == "全選択銘柄の平均" else ([choice] if choice in data else [])
        )
        for lbl in source_labels:
            st.caption(format_data_source_caption(lbl, (meta or {}).get(lbl), HOURLY_CACHE_TTL_MIN))
        if trade_period_caption is not None:
            st.caption(trade_period_caption)
        if not wd_stats_by_symbol:
            st.warning("市場データがないため集計できません。")
            return

        summary = build_weekday_summary_table(_pick(wd_stats_by_symbol), trade_wd_stats)
        st.dataframe(style_cross_table(summary), width="stretch")

        fig_ret, fig_vola = build_weekday_bar_charts(wd_stats_by_symbol)
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(fig_ret, width="stretch")
        with c2:
            st.plotly_chart(fig_vola, width="stretch")

        st.markdown("セッション×曜日 ヒートマップ(平均騰落率%)")
        ret_mats: dict[str, pd.DataFrame] = {}
        n_mats: dict[str, pd.DataFrame] = {}
        for label, df in data.items():
            try:
                ret_mats[label], n_mats[label] = compute_session_weekday_matrix(df)
            except Exception as e:  # noqa: BLE001
                st.warning(f"{label}: セッション×曜日集計に失敗しました ({e})")
        if not ret_mats:
            st.warning("セッション×曜日ヒートマップを計算できませんでした。")
            return
        if choice == "全選択銘柄の平均":
            combined_ret = pd.concat(ret_mats, names=["symbol", "band"])
            combined_n = pd.concat(n_mats, names=["symbol", "band"])
            agg_ret = combined_ret.groupby(level="band").mean().reindex(BAND_ORDER)
            agg_n = combined_n.groupby(level="band").mean().reindex(BAND_ORDER)
            st.plotly_chart(build_session_weekday_heatmap(agg_ret, agg_n), width="stretch")
        elif choice not in ret_mats:
            # 確定指摘対応: 選択銘柄のデータが無い場合に無警告で他銘柄群の平均へすり替えない
            # (上のサマリー表はNone→「—」表示になる一方、ヒートマップだけ無関係な平均を
            # 選択銘柄のものであるかのように描画してしまう不整合を防ぐ)。
            st.warning(f"「{choice}」のデータが取得できなかったため、セッション×曜日ヒートマップを表示できません。")
        else:
            agg_ret, agg_n = ret_mats[choice], n_mats[choice]
            st.plotly_chart(build_session_weekday_heatmap(agg_ret, agg_n), width="stretch")

    if compare_mode and data_b and period_b is not None:
        c1, c2 = st.columns(2)
        with c1:
            _panel(data_a, weekday_stats_a, period_a, "Period A", meta_a)
        with c2:
            _panel(data_b, weekday_stats_b, period_b, "Period B", meta_b)
    else:
        _panel(data_a, weekday_stats_a, period_a, "集計結果", meta_a)

    st.markdown("**トレードのセッション×曜日 勝率ヒートマップ**")
    if trades_assigned is None or trades_assigned.empty:
        st.info(
            "「📒 トレード履歴」タブでトレード履歴CSVをアップロードすると、"
            "ここにセッション×曜日の勝率ヒートマップが表示されます。"
        )
    else:
        win_mat, n_mat_trade = compute_trade_session_weekday_matrix(trades_assigned)
        st.plotly_chart(build_trade_session_weekday_heatmap(win_mat, n_mat_trade), width="stretch")
        if trade_period_caption is not None:
            st.caption(trade_period_caption)
        st.caption("※n<3のセルは参考値です(サンプル数が少なく統計的信頼性が低い)。")


def render_month_section(
    selected_labels: list[str], trades_assigned: Optional[pd.DataFrame],
    trade_stats_global: Optional[dict[str, Any]] = None,
    today_: Optional[date] = None,
) -> None:
    """追補§2: 月別アノマリーセクション。サイドバーの選択期間から独立した長期日足で算出する。

    today_ は呼び出し側(main())のdate.today()を明示的に渡す(fetch_daily_bundle/
    compute_month_statsへ伝播し、進行中の当月を「欠け月」として除外するために使う)。
    """
    today_eff = today_ if today_ is not None else date.today()
    st.markdown("### 月別アノマリー")
    st.info(
        "月別アノマリーはサイドバーの選択期間ではなく、下記lookbackで指定する長期日足で算出します"
        "(90日等の短期間では各月のオカレンス数(n)が1以下になり統計的に無意味なため)。"
    )
    st.caption("※月別アノマリーは比較モード(Period A / B)の対象外です(期間非依存の集計のため)。")

    if not selected_labels:
        st.info("サイドバーで銘柄を1つ以上選択してください。")
        return

    lookback_label = st.selectbox(
        "lookback(長期日足の取得期間)", ["3年", "5年", "10年"], index=1, key="month_lookback_choice",
    )
    lookback_years = {"3年": 3, "5年": 5, "10年": 10}[lookback_label]

    with st.spinner(f"月別アノマリー用の日足データを取得中(直近{lookback_label})..."):
        data_m, meta_m, errors_m = fetch_daily_bundle(selected_labels, lookback_years, today_eff)
    for e in errors_m:
        st.warning(e)
    if not data_m:
        st.error("月別アノマリー用の日足データを1件も取得できませんでした。")
        return

    month_stats_by_symbol: dict[str, pd.DataFrame] = {}
    for label, df in data_m.items():
        try:
            month_stats_by_symbol[label] = compute_month_stats(df, today_eff)
        except Exception as e:  # noqa: BLE001
            st.warning(f"{label}: 月別集計に失敗しました ({e})")

    # 確定指摘対応(欠け月混入): 進行中(未完了)の当月が除外された銘柄があれば明示する。
    affected_current = [
        label for label, s in month_stats_by_symbol.items()
        if today_eff.month in s.index and s.loc[today_eff.month, "n_excluded_current"] > 0
    ]
    if affected_current:
        st.caption(
            f"⚠️ 当月({today_eff.year}年{today_eff.month}月)は進行中(未完了)のため、"
            f"月次騰落率の平均・中央値・陽線率・nから除外しています(対象: {', '.join(affected_current)})。"
        )

    trade_m_stats: Optional[pd.DataFrame] = None
    if trades_assigned is not None and not trades_assigned.empty:
        try:
            trade_m_stats = compute_trade_month_stats(trades_assigned)
        except Exception as e:  # noqa: BLE001
            st.warning(f"トレードの月別集計に失敗しました: {e}")

    options = ["全選択銘柄の平均"] + list(month_stats_by_symbol.keys())
    choice = st.selectbox("銘柄セレクタ(月別)", options, key="month_symbol_choice")
    if choice == "全選択銘柄の平均":
        picked = aggregate_month_stats_multi(month_stats_by_symbol)
    else:
        picked = month_stats_by_symbol.get(choice)

    summary = build_month_summary_table(picked, trade_m_stats)
    st.dataframe(style_cross_table(summary), width="stretch")

    meta_lines = [f"{label}: {m['start']} 〜 {m['end']}(全{m['n_rows']}本)" for label, m in meta_m.items()]
    st.caption(f"集計期間(実取得・銘柄ごと、lookback={lookback_label}): " + " / ".join(meta_lines))
    # 追補v3§4: 日足フェッチのデータ取得元キャプション(選択期間側の1h集計とは別行で明示)。
    source_labels_m = (
        list(month_stats_by_symbol.keys()) if choice == "全選択銘柄の平均"
        else ([choice] if choice in month_stats_by_symbol else [])
    )
    for lbl in source_labels_m:
        st.caption(format_data_source_caption(lbl, meta_m.get(lbl), DAILY_CACHE_TTL_MIN))
    # 確定指摘対応: 月別サマリー表のトレード由来列(エントリー回数/勝率/合計損益)は上記の
    # 市場データ実取得期間とは独立に「アップロード済み全トレード」から計算される。
    if trade_stats_global is not None:
        ov_m = trade_stats_global.get("overall", {})
        if ov_m.get("first_time") is not None and ov_m.get("last_time") is not None:
            st.caption(
                "トレード集計期間(全件・選択期間でフィルタしない): "
                f"{ov_m['first_time'].date().isoformat()} 〜 {ov_m['last_time'].date().isoformat()}"
            )

    st.plotly_chart(build_month_bar_chart(month_stats_by_symbol), width="stretch")

    st.warning(
        "⚠️ 月別シーズナリティはn=年数程度しかなく統計的に弱い(多重検定でどれかの月は偶然目立つ)。"
        "参考情報であり単独でエッジと見なさない。"
    )


def render_weekday_month_tab(
    data_a: dict[str, pd.DataFrame],
    period_a: tuple[date, date],
    compare_mode: bool,
    data_b: dict[str, pd.DataFrame],
    period_b: Optional[tuple[date, date]],
    selected_labels: list[str],
    trade_datasets: Optional[dict[str, dict[str, Any]]] = None,
    today_: Optional[date] = None,
    meta_a: Optional[dict[str, dict[str, Any]]] = None,
    meta_b: Optional[dict[str, dict[str, Any]]] = None,
) -> None:
    """追補§0: 新タブ「📅 曜日・月別」本体。上下2セクション(曜日別/月別)を束ねる。

    追補v4§1.2: 複数トレードデータセット時のみ「トレード統計の対象」selectboxを表示し、
    選択されたデータセットのtrades_assigned/trade_statsを両セクションへ伝播させる。
    """
    trade_labels = list(trade_datasets.keys()) if trade_datasets else []
    if len(trade_labels) >= 2:
        active_trade_label = st.selectbox(
            "トレード統計の対象", trade_labels, key="weekday_month_trade_dataset_choice"
        )
    else:
        active_trade_label = trade_labels[0] if trade_labels else None
    trades_assigned = trade_datasets[active_trade_label]["trades_assigned"] if active_trade_label else None
    trade_stats_global = trade_datasets[active_trade_label]["stats"] if active_trade_label else None

    render_weekday_section(
        data_a, period_a, compare_mode, data_b, period_b, selected_labels, trades_assigned,
        trade_stats_global, meta_a, meta_b,
    )
    st.divider()
    render_month_section(selected_labels, trades_assigned, trade_stats_global, today_)


# =====================================================================================
# 追補v4§1.1: 複数トレードCSVレコード管理ヘルパー(UI層。selftest対象外)
# =====================================================================================


def _fmt_stat(v: Any, spec: str = ",.2f") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if v == float("inf"):
        return "∞"
    try:
        return format(v, spec)
    except (ValueError, TypeError):
        return str(v)


def _dedupe_trade_label(label: str, existing_labels: list[str]) -> str:
    """追補v4§1.1: 同名ラベルに連番を付与する(label, label_2, label_3, ...)。"""
    if label not in existing_labels:
        return label
    n = 2
    while f"{label}_{n}" in existing_labels:
        n += 1
    return f"{label}_{n}"


def _decode_trade_csv_bytes(content: bytes) -> tuple[Optional[pd.DataFrame], list[str]]:
    """utf-8-sig→cp932の順でCSVバイト列をデコードする(単一ファイル時代からの既存作法を踏襲)。"""
    decode_errs: list[str] = []
    for enc in ("utf-8-sig", "cp932"):
        try:
            return pd.read_csv(io.BytesIO(content), encoding=enc), decode_errs
        except Exception as e:  # noqa: BLE001
            decode_errs.append(f"{enc}: {e}")
    return None, decode_errs


def _add_trade_dataset_record(
    records: list[dict[str, Any]], label_base: str, raw_df: pd.DataFrame, source_name: str,
) -> None:
    """CSVを正規化し、ラベル重複解決の上でrecordsへ新規レコードを追記する(rid付与)。"""
    norm, errs = normalize_trades_csv(raw_df)
    existing_labels = [r["label"] for r in records]
    label = _dedupe_trade_label(label_base, existing_labels)
    rid_n = st.session_state.get("trade_dataset_next_rid", 0)
    st.session_state["trade_dataset_next_rid"] = rid_n + 1
    records.append({
        "rid": f"rec{rid_n}", "label": label, "df": norm, "source_name": source_name,
        "loaded_at": datetime.now(JST), "errors": errs or [],
    })


def _load_sample_trade_datasets(records: list[dict[str, Any]]) -> None:
    """追補v4§1.1: 「サンプル(弟子+師匠)を読み込む」。既存レコードは壊さず2件を追記する。"""
    _add_trade_dataset_record(records, "弟子", generate_sample_trades(), "サンプルデータ(弟子・アプリ内生成)")
    _add_trade_dataset_record(records, "師匠", generate_sample_trades_mentor(), "サンプルデータ(師匠・アプリ内生成)")


def render_trade_history_tab() -> None:
    if "trade_dataset_records" not in st.session_state:
        st.session_state["trade_dataset_records"] = []
    if "trade_seen_file_ids" not in st.session_state:
        st.session_state["trade_seen_file_ids"] = set()
    records: list[dict[str, Any]] = st.session_state["trade_dataset_records"]

    col_up, col_tmpl, col_sample = st.columns([2, 1, 1])
    with col_up:
        uploaded_files = st.file_uploader(
            "トレード履歴CSVをアップロード(複数可)", type=["csv"], key="trade_uploader_multi",
            accept_multiple_files=True,
        )
    with col_tmpl:
        st.write("")
        st.download_button(
            "テンプレートCSV", data=trades_csv_template_bytes(), file_name="trade_template.csv",
            mime="text/csv", key="tmpl_dl",
        )
    with col_sample:
        st.write("")
        if st.button("サンプル(弟子+師匠)を読み込む", key="load_sample_btn"):
            _load_sample_trade_datasets(records)

    # 追補v4§1.1: 複数ファイルアップロード。file_id(アップロード発生ごとに新規発行)を
    # 既処理集合と突合し、新規分のみをレコード追記する(reruns安全・v3§14の単一版を一般化)。
    for f in (uploaded_files or []):
        if f.file_id in st.session_state["trade_seen_file_ids"]:
            continue
        st.session_state["trade_seen_file_ids"].add(f.file_id)
        raw_df, decode_errs = _decode_trade_csv_bytes(f.getvalue())
        if raw_df is None:
            st.error(
                f"{f.name}: CSVの読み込みに失敗しました(utf-8-sig / cp932 のどちらでもデコードできません)。"
                f" 詳細: {' / '.join(decode_errs)}"
            )
            continue
        _add_trade_dataset_record(records, Path(f.name).stem, raw_df, f.name)

    if not records:
        st.info("トレード履歴CSVをアップロードするか、「サンプル(弟子+師匠)を読み込む」をクリックしてください。")
        return

    # 追補v4§1.1: データセットごとのラベル編集/削除UI(同名は自動連番。1件時も同じUIを出すが
    # 既存単一ファイル運用と体感を変えないよう選択selectboxは2件以上でのみ表示)。
    st.markdown("**読み込み済みデータセット**")
    to_delete: Optional[str] = None
    for rec in records:
        c1, c2, c3 = st.columns([3, 3, 1])
        with c1:
            new_label = st.text_input(
                "ラベル", value=rec["label"], key=f"trade_label_{rec['rid']}", label_visibility="collapsed",
            )
        with c2:
            n_rows = 0 if rec["df"] is None else len(rec["df"])
            st.caption(f"{rec['source_name']} ({n_rows}件)")
        with c3:
            if st.button("削除", key=f"trade_delete_{rec['rid']}"):
                to_delete = rec["rid"]
        if new_label != rec["label"]:
            other_labels = [r["label"] for r in records if r["rid"] != rec["rid"]]
            rec["label"] = _dedupe_trade_label(new_label, other_labels)
            st.caption(f"→ 実際のラベル: {rec['label']}(重複する場合は自動で連番付与)")

    if to_delete is not None:
        st.session_state["trade_dataset_records"] = [r for r in records if r["rid"] != to_delete]
        st.rerun()

    for rec in records:
        for sev, msg in rec.get("errors", []):
            prefix = f"[{rec['label']}] "
            if sev == "error":
                st.error(prefix + msg)
            else:
                st.warning(prefix + msg)

    labels = [r["label"] for r in records]
    # v6.6: 複数データセット時は「合算」を先頭に追加(ユーザー要望: 弟子+師匠まとめた成績)。
    _ALL_DATASETS = "🤝 全データセット合算"
    if len(labels) >= 2:
        active_label = st.selectbox(
            "トレード統計の対象", [_ALL_DATASETS] + labels, key="trade_history_active_label",
        )
    else:
        active_label = labels[0]
    combined_stats = active_label == _ALL_DATASETS

    if combined_stats:
        parts: list[pd.DataFrame] = []
        srcs: list[str] = []
        for r in records:
            if r["df"] is None or r["df"].empty:
                continue
            p = r["df"].copy()
            p.insert(0, "データセット", r["label"])
            parts.append(p)
            srcs.append(f"{r['label']}({len(p)}件)")
        if not parts:
            st.info("有効なトレードデータがありません。")
            return
        trades_df = pd.concat(parts, ignore_index=True)
        source_name = " + ".join(srcs)
        st.caption(f"📊 データ元(合算): {source_name} | 指標は全データセットを1つの成績として時系列合算で計算")
    else:
        active_rec = next(r for r in records if r["label"] == active_label)
        trades_df = active_rec["df"]
        source_name = active_rec["source_name"]
        if trades_df is None or trades_df.empty:
            st.info(f"「{active_label}」に有効なトレードデータがありません。")
            if len(records) >= 2:
                st.divider()
                _render_trader_comparison_view(records)
            return
        loaded_at = active_rec.get("loaded_at")
        loaded_str = loaded_at.strftime("%Y-%m-%d %H:%M") if loaded_at is not None else "不明"
        st.caption(f"📊 データ元: {source_name} | ⏱ 読込: {loaded_str} JST時点")
    try:
        df = assign_trade_sessions(trades_df)
        stats = compute_trade_stats(df)
    except Exception as e:  # noqa: BLE001
        st.error(f"トレード統計の集計に失敗しました: {e}")
        if len(records) >= 2:
            st.divider()
            _render_trader_comparison_view(records)
        return
    overall = stats["overall"]

    first_t = overall.get("first_time")
    last_t = overall.get("last_time")
    if first_t is not None and last_t is not None:
        st.caption(f"集計期間(トレードの最初〜最後): {first_t.date().isoformat()} 〜 {last_t.date().isoformat()}")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("総損益(USD)", _fmt_stat(overall["total_pnl_usd"]))
    m2.metric("取引数", f"{overall['n_trades']}")
    m3.metric("勝率", _fmt_stat(overall["win_rate_pct"], ".1f") + ("%" if not pd.isna(overall["win_rate_pct"]) else ""))
    m4.metric("プロフィットファクター", _fmt_stat(overall["profit_factor"], ".2f"))

    m5, m6, m7, m8 = st.columns(4)
    m5.metric("平均利益(USD)", _fmt_stat(overall["avg_win"]))
    m6.metric("平均損失(USD)", _fmt_stat(overall["avg_loss"]))
    m7.metric("RR", _fmt_stat(overall["rr"], ".2f"))
    m8.metric("最大ドローダウン(USD)", _fmt_stat(overall["max_dd_usd"]))

    st.metric("最大連敗数", f"{overall['max_consec_losses']}回")

    st.markdown("**トレード一覧**" + ("(全データセット・時系列)" if combined_stats else ""))
    disp = df.copy().sort_values("Entry_Time")
    disp = disp.rename(columns={"band": "詳細帯", "parent": "大枠"})
    st.dataframe(disp, width="stretch")

    st.markdown("**累積損益曲線**")
    sorted_df = df.sort_values("Entry_Time")
    cum_fig = go.Figure()
    if combined_stats and "データセット" in sorted_df.columns:
        # v6.6: 合算(太線)+データセット別(細線)を重ね描き
        cum_fig.add_trace(
            go.Scatter(
                x=sorted_df["Entry_Time"], y=sorted_df["PnL_USD"].cumsum(),
                mode="lines+markers", name="🤝 合算", line=dict(width=3),
            )
        )
        for i, (ds_lbl, sub) in enumerate(sorted_df.groupby("データセット", sort=False)):
            sub = sub.sort_values("Entry_Time")
            cum_fig.add_trace(
                go.Scatter(
                    x=sub["Entry_Time"], y=sub["PnL_USD"].cumsum(), mode="lines",
                    name=ds_lbl, line=dict(width=1.5, dash="dot",
                                           color=get_trade_marker_color(ds_lbl, i)),
                )
            )
    else:
        cum_fig.add_trace(
            go.Scatter(
                x=sorted_df["Entry_Time"], y=sorted_df["PnL_USD"].cumsum(),
                mode="lines+markers", name="累積損益(USD)",
            )
        )
    cum_fig.update_layout(
        template="plotly_dark", height=380, margin=dict(l=40, r=20, t=40, b=20), yaxis_title="累積損益(USD)",
    )
    st.plotly_chart(cum_fig, width="stretch")

    st.markdown("**詳細帯別 損益**")
    if combined_stats and "データセット" in df.columns:
        # v6.6: データセット別に色分けしたグループ棒(帯ごとの貢献が見比べられる)
        gb = (
            df.groupby(["band", "データセット"])["PnL_USD"].sum().reset_index()
            .rename(columns={"PnL_USD": "total_pnl_usd"})
        )
        band_fig = px.bar(
            gb, x="band", y="total_pnl_usd", color="データセット", barmode="group",
            template="plotly_dark",
            labels={"band": "詳細帯", "total_pnl_usd": "損益(USD)"},
            category_orders={"band": BAND_ORDER},
        )
    else:
        by_band = stats["by_band"].reindex(BAND_ORDER).reset_index()
        band_fig = px.bar(
            by_band, x="band", y="total_pnl_usd", template="plotly_dark",
            labels={"band": "詳細帯", "total_pnl_usd": "損益(USD)"}, category_orders={"band": BAND_ORDER},
        )
    band_fig.update_layout(height=380, margin=dict(l=40, r=20, t=40, b=20))
    st.plotly_chart(band_fig, width="stretch")

    if len(records) >= 2:
        st.divider()
        _render_trader_comparison_view(records)


def _render_trader_comparison_view(records: list[dict[str, Any]]) -> None:
    """追補v4§1.3: 👥 トレーダー比較ビュー(データセット2件以上で出現)。
    数値の提示のみで一般論のアドバイス文は生成しない(掟2)。各表に両者の集計期間を明記する(掟6)。
    """
    st.markdown("### 👥 トレーダー比較ビュー")
    labels = [r["label"] for r in records]
    c1, c2 = st.columns(2)
    with c1:
        label_a = st.selectbox("比較対象1(弟子側)", labels, index=0, key="compare_label_a")
    with c2:
        default_b_idx = 1 if len(labels) > 1 else 0
        label_b = st.selectbox("比較対象2(師匠側)", labels, index=default_b_idx, key="compare_label_b")

    if label_a == label_b:
        st.warning("異なる2つのデータセットを選んでください。")
        return

    rec_a = next(r for r in records if r["label"] == label_a)
    rec_b = next(r for r in records if r["label"] == label_b)

    def _stats_of(rec: dict[str, Any]) -> Optional[tuple[pd.DataFrame, dict[str, Any]]]:
        d = rec.get("df")
        if d is None or d.empty:
            return None
        assigned = assign_trade_sessions(d)
        return assigned, compute_trade_stats(assigned)

    res_a = _stats_of(rec_a)
    res_b = _stats_of(rec_b)
    if res_a is None or res_b is None:
        st.info("両方のデータセットに有効なトレードが必要です。")
        return
    assigned_a, stats_a = res_a
    assigned_b, stats_b = res_b
    cmp = compare_trade_stats(stats_a, stats_b)
    ov_a, ov_b = cmp["overall_a"], cmp["overall_b"]
    p_a_first, p_a_last = cmp["period_a"]
    p_b_first, p_b_last = cmp["period_b"]

    def _period_str(first: Any, last: Any) -> str:
        if first is None or last is None:
            return "データなし"
        return f"{first.date().isoformat()} 〜 {last.date().isoformat()}"

    # 掟6: 両データセットの集計期間を明記
    st.caption(
        f"集計期間: {label_a}={_period_str(p_a_first, p_a_last)} / "
        f"{label_b}={_period_str(p_b_first, p_b_last)}"
    )

    st.markdown("**① 総合統計(並列)**")
    rows_spec = [
        ("総損益(USD)", "total_pnl_usd", ",.2f"), ("取引数", "n_trades", ",.0f"),
        ("勝率(%)", "win_rate_pct", ".1f"), ("プロフィットファクター", "profit_factor", ".2f"),
        ("平均利益(USD)", "avg_win", ",.2f"), ("平均損失(USD)", "avg_loss", ",.2f"),
        ("RR", "rr", ".2f"), ("最大DD(USD)", "max_dd_usd", ",.2f"),
        ("最大連敗数", "max_consec_losses", ",.0f"),
    ]
    card_rows = [
        {"指標": jp, label_a: _fmt_stat(ov_a.get(key), spec), label_b: _fmt_stat(ov_b.get(key), spec)}
        for jp, key, spec in rows_spec
    ]
    st.dataframe(pd.DataFrame(card_rows).set_index("指標"), width="stretch")

    st.markdown("**② 累積損益曲線の重ね描き**")
    normalize = st.toggle("1トレードあたり平均PnLで正規化", value=False, key="compare_normalize_toggle")
    cum_fig = go.Figure()
    for lbl, assigned in ((label_a, assigned_a), (label_b, assigned_b)):
        sdf = assigned.sort_values("Entry_Time")
        y = sdf["PnL_USD"].cumsum()
        if normalize:
            avg_abs = sdf["PnL_USD"].abs().mean()
            if avg_abs and not pd.isna(avg_abs) and avg_abs != 0:
                y = y / avg_abs
        cum_fig.add_trace(go.Scatter(x=sdf["Entry_Time"], y=y, mode="lines+markers", name=lbl))
    cum_fig.update_layout(
        template="plotly_dark", height=380, margin=dict(l=40, r=20, t=40, b=20),
        yaxis_title="累積損益(正規化後・単位なし)" if normalize else "累積損益(USD)",
    )
    st.plotly_chart(cum_fig, width="stretch")

    st.markdown("**③ 詳細帯別 比較表**")
    band_cmp = cmp["by_band"].reindex(BAND_ORDER)
    for c in ("n_trades_a", "n_wins_a", "n_losses_a", "n_trades_b", "n_wins_b", "n_losses_b"):
        band_cmp[c] = band_cmp[c].fillna(0)
    for c in ("low_n_a", "low_n_b"):
        band_cmp[c] = band_cmp[c].fillna(True)
    st.dataframe(style_compare_band_table(band_cmp, label_a, label_b), width="stretch")
    st.caption("「(n小)」= 当該データセットの該当帯のトレード数が3件未満。")

    st.markdown("**④ 改善ヒント(データ駆動・数字のみ。一般論のアドバイス文は生成しない)**")
    hint_df = cmp["by_band"].copy()
    hint_df = hint_df[hint_df["win_rate_diff"].notna()]
    top_gap = hint_df.sort_values("win_rate_diff", ascending=False).head(3)
    if top_gap.empty:
        st.caption("両者に共通する帯データが不足しているため、勝率差TOP3を算出できません。")
    else:
        lines = [
            f"- {band}: 勝率差 {row['win_rate_diff']:+.1f}pt "
            f"({label_a} {row['win_rate_pct_a']:.1f}%・n={int(row['n_trades_a'])} / "
            f"{label_b} {row['win_rate_pct_b']:.1f}%・n={int(row['n_trades_b'])})"
            for band, row in top_gap.iterrows()
        ]
        st.markdown(f"{label_b}との勝率差が大きい帯 TOP3:\n" + "\n".join(lines))

    # win_rate_diffのNaN(相手側n=0)に依存させない: a側のみの指標なのでcmp["by_band"]を直接フィルタする
    band_all = cmp["by_band"]
    freq_low = band_all[
        (band_all["n_trades_a"] >= 3) & (band_all["win_rate_pct_a"] < LOW_WINRATE_THRESHOLD)
    ].sort_values("n_trades_a", ascending=False)
    if freq_low.empty:
        st.caption(f"エントリー数が多い(n≥3)のに勝率が{LOW_WINRATE_THRESHOLD:.0f}%未満の帯はありません({label_a}側)。")
    else:
        lines2 = [
            f"- {band}: {label_a} n={int(row['n_trades_a'])}・勝率{row['win_rate_pct_a']:.1f}%"
            for band, row in freq_low.iterrows()
        ]
        st.markdown(
            f"エントリーが多いのに勝率が低い帯({label_a}側・n≥3かつ勝率<{LOW_WINRATE_THRESHOLD:.0f}%):\n"
            + "\n".join(lines2)
        )

    st.markdown("**⑤ 曜日別 勝率差ミニ表**")
    wd_a = compute_trade_weekday_stats(assigned_a)
    wd_b = compute_trade_weekday_stats(assigned_b)
    wd_cmp = compare_trade_stats({"overall": {}, "by_band": wd_a}, {"overall": {}, "by_band": wd_b})["by_band"]
    st.dataframe(style_compare_band_table(wd_cmp, label_a, label_b), width="stretch")


# =====================================================================================
# main()
# =====================================================================================


def render_password_gate() -> None:
    """簡易パスワードゲート(公開運用時のみ)。

    環境変数 DASHBOARD_PASSWORD が設定されている場合だけ有効(ローカル利用は無影響)。
    パスワードはコードに直書きしない(D-015: .env管理)。
    """
    expected = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not expected:
        return
    if st.session_state.get("auth_ok") is True:
        return
    st.markdown("### 🔒 閲覧パスワード")
    pw = st.text_input("パスワードを入力してください", type="password", key="auth_pw_input")
    if pw:
        if hmac.compare_digest(pw, expected):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    st.stop()


def main() -> None:
    st.set_page_config(page_title="Session Analysis Dashboard", page_icon="🕐", layout="wide")
    render_password_gate()
    inject_custom_css()
    st.title("🕐 Session Analysis Dashboard")
    st.caption("時間帯(セッション)別の値動き傾向・トレード成績を可視化するダッシュボード。")

    today = date.today()

    # ------------------------------------------------------------------
    # サイドバー: §3-1 期間選択
    # ------------------------------------------------------------------
    st.sidebar.header("① 期間選択")
    if "date_start" not in st.session_state:
        st.session_state["date_start"] = today - timedelta(days=90)
    if "date_end" not in st.session_state:
        st.session_state["date_end"] = today

    # 前回の実行(銘柄選択確定後)で発生したyfinance 730日クランプの警告があれば、
    # ウィジェット生成前にここでポップして表示する(rerunを跨いでsession_state経由で伝搬させる)。
    for _msg_key in ("_yf_clamp_msg_a", "_yf_clamp_msg_b"):
        _pending_msg = st.session_state.pop(_msg_key, None)
        if _pending_msg:
            st.sidebar.warning(_pending_msg)

    preset_defs = [("過去7日", 7), ("過去30日", 30), ("過去90日", 90), ("過去1年", 365)]
    preset_cols = st.sidebar.columns(4)
    for col, (label, days) in zip(preset_cols, preset_defs):
        if col.button(label, key=f"preset_{days}"):
            st.session_state["date_start"] = today - timedelta(days=days)
            st.session_state["date_end"] = today
            st.rerun()

    # ウィジェット生成"前"にsession_stateそのものを補正する(表示値と実際の集計対象を一致させるため。
    # date_input生成後にローカル変数だけ書き換えても画面表示とcompute_bundleの引数がズレてしまう)。
    warn_future = False
    warn_order = False
    if st.session_state["date_end"] > today:
        st.session_state["date_end"] = today
        warn_future = True
    if st.session_state["date_start"] > st.session_state["date_end"]:
        st.session_state["date_start"] = st.session_state["date_end"]
        warn_order = True

    date_start = st.sidebar.date_input("開始日", key="date_start", max_value=today)
    date_end = st.sidebar.date_input("終了日", key="date_end", max_value=today)

    if warn_future:
        st.sidebar.warning("終了日が未来日のため今日にクランプしました。")
    if warn_order:
        st.sidebar.warning("開始日が終了日より後だったため、開始日=終了日に補正しました。")

    compare_mode = st.sidebar.toggle("比較モード (Period A / B)", key="compare_mode")
    date_start_b: Optional[date] = None
    date_end_b: Optional[date] = None
    if compare_mode:
        st.sidebar.markdown("**Period B**")
        if "date_start_b" not in st.session_state:
            st.session_state["date_start_b"] = today - timedelta(days=180)
        if "date_end_b" not in st.session_state:
            st.session_state["date_end_b"] = today - timedelta(days=91)

        if st.session_state["date_end_b"] > today:
            st.session_state["date_end_b"] = today
        if st.session_state["date_start_b"] > st.session_state["date_end_b"]:
            st.session_state["date_start_b"] = st.session_state["date_end_b"]

        date_start_b = st.sidebar.date_input("開始日(B)", key="date_start_b", max_value=today)
        date_end_b = st.sidebar.date_input("終了日(B)", key="date_end_b", max_value=today)

    # ------------------------------------------------------------------
    # サイドバー: §3-2 銘柄選択
    # ------------------------------------------------------------------
    st.sidebar.header("② 銘柄選択")
    selected_labels: list[str] = []
    for label in SYMBOL_MASTER:
        checked = st.sidebar.checkbox(label, value=DEFAULT_SYMBOL_CHECKED[label], key=f"chk_{label}")
        if checked:
            selected_labels.append(label)

    st.sidebar.markdown("**その他(任意ティッカー)**")
    st.session_state.setdefault("custom_tickers", [])
    with st.sidebar.form("add_custom_ticker_form", clear_on_submit=True):
        t1 = st.text_input("ティッカー1 (例: DOGE/USDT)", key="custom_ticker_input_1")
        t2 = st.text_input("ティッカー2 (例: SPY)", key="custom_ticker_input_2")
        t3 = st.text_input("ティッカー3", key="custom_ticker_input_3")
        submitted = st.form_submit_button("追加")
        if submitted:
            for v in (t1, t2, t3):
                v = v.strip()
                if v and v not in st.session_state["custom_tickers"]:
                    st.session_state["custom_tickers"].append(v)

    remove_target: Optional[str] = None
    for ct in list(st.session_state["custom_tickers"]):
        cc1, cc2 = st.sidebar.columns([4, 1])
        checked = cc1.checkbox(ct, value=True, key=f"chk_custom_{ct}")
        if checked:
            selected_labels.append(ct)
        if cc2.button("削除", key=f"del_custom_{ct}"):
            remove_target = ct
    if remove_target is not None:
        st.session_state["custom_tickers"].remove(remove_target)
        st.rerun()

    # ------------------------------------------------------------------
    # ①の続き: yfinance 730日クランプは銘柄選択が確定した後にのみ適用する
    #   (ccxt(binance/bybit)専用選択時はyfinanceの制約と無関係なため、不要なクランプ・警告を
    #    出さない。date_inputウィジェットは既にこのrunで生成済みのため、補正が必要な場合は
    #    session_stateを書き換えてst.rerun()し、次のrunでウィジェット表示に反映させる)
    # ------------------------------------------------------------------
    if any_yfinance_selected(selected_labels):
        new_start, was_clamped = clamp_start_date_for_yfinance(date_start, today)
        if was_clamped:
            st.session_state["date_start"] = new_start
            st.session_state["_yf_clamp_msg_a"] = (
                f"yfinanceの1時間足は直近730日までのため、開始日を {new_start.isoformat()} にクランプしました。"
            )
            st.rerun()
        if compare_mode and date_start_b is not None:
            new_start_b, was_clamped_b = clamp_start_date_for_yfinance(date_start_b, today)
            if was_clamped_b:
                st.session_state["date_start_b"] = new_start_b
                st.session_state["_yf_clamp_msg_b"] = (
                    f"Period B開始日を {new_start_b.isoformat()} にクランプしました(730日制限)。"
                )
                st.rerun()

    # ------------------------------------------------------------------
    # サイドバー: §3-3 足種選択
    # ------------------------------------------------------------------
    st.sidebar.header("③ 足種選択")
    timeframe_choice = st.sidebar.radio(
        "足種", TIMEFRAME_CHOICES, index=TIMEFRAME_CHOICES.index("1時間足"), key="timeframe_choice",
    )
    # 追補v4§2.4A: 1分足(暗号のみ)は暗号銘柄が1つも選択されていない場合は使用不可(警告+15分足へ代替)。
    effective_timeframe_choice = timeframe_choice
    if timeframe_choice == "1分足(暗号のみ)" and not any_ccxt_selected(selected_labels):
        st.sidebar.warning("暗号銘柄が選択されていないため1分足は使用できません。15分足で表示します。")
        effective_timeframe_choice = "15分足"

    # ------------------------------------------------------------------
    # サイドバー: §3-5 チャートオプション (§3-4比較モードは上で処理済み)
    # ------------------------------------------------------------------
    st.sidebar.header("④ チャートオプション")
    period_days = (date_end - date_start).days
    show_bg_toggle = st.sidebar.toggle("セッション背景帯", value=True, key="show_session_bg")
    bg_opacity_pct = st.sidebar.slider(
        "背景帯の濃さ", min_value=5, max_value=70, value=25, step=5,
        key="bg_opacity_pct", disabled=not show_bg_toggle,
    )
    bg_opacity = bg_opacity_pct / 100.0
    show_bg = show_bg_toggle
    if period_days > 120:
        show_bg = False
        st.sidebar.info("期間が120日を超えるためセッション背景帯を自動OFFにしました。")

    # 追補v4§2.2: トレードマーカー表示トグル(既定ON)。トレード読込時のみ実際に描画される。
    trade_ds_labels_for_ui = [
        rec["label"] for rec in st.session_state.get("trade_dataset_records", [])
        if rec.get("df") is not None and not rec["df"].empty
    ]
    show_trade_markers = st.sidebar.toggle(
        "トレードマーカー表示", value=True, key="show_trade_markers",
        help="チャートにエントリー▲/決済✕マーカーを重ねる(トレードCSV読込時のみ有効)。",
    )
    selected_marker_labels: list[str] = []
    if show_trade_markers and trade_ds_labels_for_ui:
        if len(trade_ds_labels_for_ui) > 1:
            selected_marker_labels = st.sidebar.multiselect(
                "マーカー表示データセット", trade_ds_labels_for_ui, default=trade_ds_labels_for_ui,
                key="marker_ds_select",
            )
        else:
            selected_marker_labels = trade_ds_labels_for_ui

    if not selected_labels:
        st.info("サイドバーの「② 銘柄選択」で銘柄を1つ以上選択してください。")

    # ------------------------------------------------------------------
    # データ取得: Period A / B
    # ------------------------------------------------------------------
    with st.spinner("Period A のデータを取得中..."):
        data_a, stats_a, meta_a, errors_a = compute_bundle(selected_labels, date_start, date_end)
    for e in errors_a:
        st.warning(e)
    if selected_labels and not data_a:
        st.error("選択した銘柄すべてでデータ取得に失敗しました。")

    data_b: dict[str, pd.DataFrame] = {}
    stats_b: dict[str, pd.DataFrame] = {}
    meta_b: dict[str, dict[str, Any]] = {}
    if compare_mode and date_start_b is not None and date_end_b is not None:
        with st.spinner("Period B のデータを取得中..."):
            data_b, stats_b, meta_b, errors_b = compute_bundle(selected_labels, date_start_b, date_end_b)
        for e in errors_b:
            st.warning(e)
        if selected_labels and not data_b:
            st.error("Period B: 選択した銘柄すべてでデータ取得に失敗しました。")

    # ------------------------------------------------------------------
    # セクション (v6: 遅延レンダリング。st.tabsは非表示タブも毎回全構築するため、
    # Oracle小型VM(2コア/956MB)+Funnel帯域では1操作ごとに全5タブ分のCPUと
    # ペイロードを支払っていた。選択中セクションだけ構築して約1/5にする。
    # トレード履歴のアップロード/サンプルはsession_state("trade_dataset_records")に
    # 永続するため、非表示時も下の集計ループで他セクションへ伝播が維持される。
    # 注: セクション切替でセクション内ウィジェット(足種等)は既定値に戻る(既知の代償)。
    # ------------------------------------------------------------------
    _SECTIONS = ["📊 多重クロス表", "🕯️ チャート", "📈 セッション分析", "📅 曜日・月別", "📒 トレード履歴"]
    section = st.segmented_control(
        "表示セクション", _SECTIONS, default=_SECTIONS[0], key="main_section_v6",
        label_visibility="collapsed",
    )
    if not section:  # segmented_controlは選択解除でNoneを返す
        section = _SECTIONS[0]

    if section == "📒 トレード履歴":
        render_trade_history_tab()

    # 追補v4§1.2: 複数トレードデータセット(rid付きレコードのリスト)を集計し、
    # ラベル→{trades_assigned, stats}のdictとして各セクションへ伝播する。
    trade_datasets: dict[str, dict[str, Any]] = {}
    for rec in st.session_state.get("trade_dataset_records", []):
        norm_df = rec.get("df")
        if norm_df is None or norm_df.empty:
            continue
        try:
            t_assigned = assign_trade_sessions(norm_df)
            t_stats = compute_trade_stats(t_assigned)
        except Exception as e:  # noqa: BLE001
            st.error(f"トレード統計の集計に失敗しました({rec.get('label')}): {e}")
            continue
        trade_datasets[rec["label"]] = {"trades_assigned": t_assigned, "stats": t_stats}

    if section == "📊 多重クロス表":
        render_cross_table_tab(
            stats_a, trade_datasets, (date_start, date_end), compare_mode,
            stats_b if compare_mode else None,
            (date_start_b, date_end_b) if (compare_mode and date_start_b and date_end_b) else None,
            selected_labels,
            meta_a, meta_b if compare_mode else None,
        )

    elif section == "🕯️ チャート":
        render_chart_tab(
            data_a, effective_timeframe_choice, show_bg, (date_start, date_end), bg_opacity, meta_a,
            selected_labels, trade_datasets, show_trade_markers, selected_marker_labels,
        )
        render_trade_zoom_section(trade_datasets, show_bg, bg_opacity)

    elif section == "📈 セッション分析":
        render_session_analysis_tab(
            data_a, stats_a, (date_start, date_end), compare_mode, data_b, stats_b,
            (date_start_b, date_end_b) if (compare_mode and date_start_b and date_end_b) else None,
            meta_a, meta_b,
        )

    elif section == "📅 曜日・月別":
        render_weekday_month_tab(
            data_a, (date_start, date_end), compare_mode, data_b,
            (date_start_b, date_end_b) if (compare_mode and date_start_b and date_end_b) else None,
            selected_labels, trade_datasets,
            today, meta_a, meta_b,
        )


# =====================================================================================
# run_selftest() … spec §7 の6項目をネットワーク非依存で検証する。st.*を一切呼ばない。
# =====================================================================================


def run_selftest() -> bool:
    all_ok = True
    fail_details: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal all_ok
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
        if not cond:
            all_ok = False
            fail_details.append(f"{name}: {detail}")

    print("=" * 78)
    print("SELFTEST START")
    print("=" * 78)

    # -----------------------------------------------------------------
    # 1. hourマップ: 0-23全hourが一意に1帯へ割当・9帯全て非空・大枠マップ整合
    # -----------------------------------------------------------------
    print("\n--- 1. hourマップ ---")
    all_hours: set[int] = set()
    for _band, _hours in BAND_HOURS.items():
        all_hours.update(_hours)
    check("1-1 24時間を漏れなく一意にカバー", all_hours == set(range(24)) and len(HOUR_TO_BAND) == 24)
    check("1-2 詳細帯が9本、全て非空", len(BAND_HOURS) == 9 and all(len(v) > 0 for v in BAND_HOURS.values()))
    check("1-3 大枠が4種", set(BAND_TO_PARENT.values()) == set(PARENT_ORDER) and len(PARENT_ORDER) == 4)
    for _h in range(24):
        try:
            band = hour_to_zone(_h)
            parent = zone_to_parent(band)
            ok = band in BAND_HOURS and _h in BAND_HOURS[band] and parent == BAND_TO_PARENT[band]
        except Exception as e:
            ok = False
        check(f"1-4 hour={_h:02d} -> 帯/大枠 整合", ok)

    # -----------------------------------------------------------------
    # 2. セッション境界判定
    # -----------------------------------------------------------------
    print("\n--- 2. セッション境界判定 ---")
    boundary_cases = [
        ("05:59", "NY後半 (4-6)"),
        ("06:00", "薄商い (6-7)"),
        ("06:59", "薄商い (6-7)"),
        ("07:00", "アジア早朝 (7-10)"),
        ("09:59", "アジア早朝 (7-10)"),
        ("10:00", "アジア本番 (10-14)"),
        ("15:59", "アジア後半 (14-17)"),
        ("16:00", "ロンドンオープン (16-18)"),
        ("17:59", "ロンドンオープン (16-18)"),
        ("18:00", "ロンドン中盤 (18-21)"),
        ("20:59", "ロンドン中盤 (18-21)"),
        ("21:00", "NY重複 (21-1)"),
        ("00:59", "NY重複 (21-1)"),
        ("01:00", "NY中盤 (1-4)"),
        ("03:59", "NY中盤 (1-4)"),
        ("04:00", "NY後半 (4-6)"),
    ]
    for time_str, expected_band in boundary_cases:
        hour_val = int(time_str.split(":")[0])
        got = hour_to_zone(hour_val)
        check(f"2- {time_str} -> {expected_band}", got == expected_band, f"got={got}")

    # -----------------------------------------------------------------
    # 3. compute_session_stats: 既知値を仕込んだ合成OHLCVで手計算と一致
    # -----------------------------------------------------------------
    print("\n--- 3. compute_session_stats ---")
    idx_list = []
    open_list = []
    close_list = []
    vol_list = []
    bases = [100.0, 150.0]
    for day_i, base in enumerate(bases):
        day0 = pd.Timestamp("2026-01-01", tz="Asia/Tokyo") + pd.Timedelta(days=day_i)
        for h in range(24):
            ts = day0 + pd.Timedelta(hours=h)
            o = base - 1.0 + h
            c = base + h
            idx_list.append(ts)
            open_list.append(o)
            close_list.append(c)
            vol_list.append(10.0 * (h + 1))
    synth_df = pd.DataFrame(
        {
            "open": open_list, "high": [max(o, c) for o, c in zip(open_list, close_list)],
            "low": [min(o, c) for o, c in zip(open_list, close_list)], "close": close_list, "volume": vol_list,
        },
        index=pd.DatetimeIndex(idx_list),
    )
    stats3 = compute_session_stats(synth_df)

    # 3-a: アジア早朝(7-10) hours(7,8,9) -> 日またぎなしのクリーンな帯
    band_a = "アジア早朝 (7-10)"
    exp_returns = []
    exp_vols = []
    exp_volas = []
    for base in bases:
        day_open = base - 1.0 + 7
        day_close = base + 9
        exp_returns.append((day_close - day_open) / day_open * 100.0)
        exp_vols.append(sum(10.0 * (h + 1) for h in (7, 8, 9)))
        rets = [math.log((base + h) / (base + h - 1)) for h in (7, 8, 9)]
        mean_r = sum(rets) / len(rets)
        var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
        exp_volas.append(math.sqrt(var_r) * 100.0)
    exp_return_a = sum(exp_returns) / 2
    exp_vol_a = sum(exp_vols) / 2
    exp_vola_a = sum(exp_volas) / 2
    got_a = stats3.loc[band_a]
    check("3-1 アジア早朝 平均騰落率(%)", abs(got_a["avg_return_pct"] - exp_return_a) < 1e-6,
          f"exp={exp_return_a:.6f} got={got_a['avg_return_pct']:.6f}")
    check("3-2 アジア早朝 平均出来高", abs(got_a["avg_volume"] - exp_vol_a) < 1e-6,
          f"exp={exp_vol_a} got={got_a['avg_volume']}")
    check("3-3 アジア早朝 平均ボラティリティ(%)", abs(got_a["avg_volatility_pct"] - exp_vola_a) < 1e-6,
          f"exp={exp_vola_a:.6f} got={got_a['avg_volatility_pct']:.6f}")
    check("3-4 アジア早朝 n_days=2", int(got_a["n_days"]) == 2)

    # 3-b: 薄商い(6-7) hour(6)のみ -> 単一サンプルの母集団std=0
    band_b = "薄商い (6-7)"
    got_b = stats3.loc[band_b]
    check("3-5 薄商い 平均ボラティリティ=0(単一サンプル)", abs(got_b["avg_volatility_pct"] - 0.0) < 1e-9,
          f"got={got_b['avg_volatility_pct']}")

    # 3-c: NY重複(21-1) hours(21,22,23,0) は「D 21:00 -> D+1 00:00」を1セッションとする
    # (compute_session_statsのovernight_mask+anchor除外による暦日繰り込み挙動)。期待値をここで独立に
    # 再現する: day_i(0..n-2)のセッションはday_iの21-23時(open基準)とday_(i+1)の0時(close基準)を結合。
    # 最終日はデータに翌日0時が存在しないため、単独の21-23時セッション(23時closeで終端)となる
    # (これは意図した「21時始値->翌1時終値」定義とは異なるが、データがそこで終わる以上避けられない
    # 既知の終端アーティファクトであり、compute_session_statsのdocstringにも明記されている)。
    band_c = "NY重複 (21-1)"
    n_bases = len(bases)
    exp_returns_c = []
    exp_vols_c = []
    for i, base in enumerate(bases):
        vol_2123 = sum(10.0 * (h + 1) for h in (21, 22, 23))
        day_open = base - 1.0 + 21
        if i + 1 < n_bases:
            next_base = bases[i + 1]
            day_close = next_base + 0
            vol = vol_2123 + 10.0 * (0 + 1)
        else:
            day_close = base + 23
            vol = vol_2123
        exp_returns_c.append((day_close - day_open) / day_open * 100.0)
        exp_vols_c.append(vol)
    exp_return_c = sum(exp_returns_c) / n_bases
    exp_vol_c = sum(exp_vols_c) / n_bases
    got_c = stats3.loc[band_c]
    check("3-6 NY重複 平均騰落率(%)", abs(got_c["avg_return_pct"] - exp_return_c) < 1e-6,
          f"exp={exp_return_c:.6f} got={got_c['avg_return_pct']:.6f}")
    check("3-7 NY重複 平均出来高", abs(got_c["avg_volume"] - exp_vol_c) < 1e-6,
          f"exp={exp_vol_c} got={got_c['avg_volume']}")
    check("3-8 NY重複 n_days=2", int(got_c["n_days"]) == 2)

    # -----------------------------------------------------------------
    # 4. トレード集計: 合成トレードで 回数/勝率/合計損益/PF/最大DD/最大連敗 が既知解と一致
    # -----------------------------------------------------------------
    print("\n--- 4. トレード集計 ---")
    trade_rows = [
        ("2026-02-01 07:00:00", "BTC", 100.0, "win"),
        ("2026-02-01 08:00:00", "BTC", -30.0, "loss"),
        ("2026-02-01 09:00:00", "BTC", -20.0, "loss"),
        ("2026-02-01 10:00:00", "BTC", 0.0, "draw"),
        ("2026-02-01 11:00:00", "BTC", -50.0, "loss"),
        ("2026-02-01 12:00:00", "BTC", 200.0, "win"),
        ("2026-02-01 13:00:00", "BTC", -10.0, "loss"),
    ]
    tdf = pd.DataFrame(
        {
            "Entry_Time": [pd.Timestamp(t, tz="Asia/Tokyo") for t, _, _, _ in trade_rows],
            "Exit_Time": [pd.NaT] * len(trade_rows),
            "Symbol": [s for _, s, _, _ in trade_rows],
            "PnL_USD": [p for _, _, p, _ in trade_rows],
            "PnL_Percent": [np.nan] * len(trade_rows),
            "Win_Loss": [w for _, _, _, w in trade_rows],
        }
    )
    tdf_assigned = assign_trade_sessions(tdf)
    trade_stats4 = compute_trade_stats(tdf_assigned)
    ov4 = trade_stats4["overall"]

    exp_n_trades = 7
    exp_n_wins = 2
    exp_n_losses = 4
    exp_total_pnl = 100 - 30 - 20 + 0 - 50 + 200 - 10
    exp_win_rate = exp_n_wins / (exp_n_wins + exp_n_losses) * 100.0
    exp_wins_sum = 100 + 200
    exp_losses_sum = -30 - 20 - 50 - 10
    exp_pf = exp_wins_sum / abs(exp_losses_sum)
    exp_avg_win = exp_wins_sum / exp_n_wins
    exp_avg_loss = exp_losses_sum / exp_n_losses
    exp_rr = exp_avg_win / abs(exp_avg_loss)
    exp_max_dd = 100.0  # 累積: 100,70,50,50,0,200,190 -> running_max-cum の最大は100(4件目地点)
    exp_max_streak = 3  # loss,loss,(draw維持),loss で3連敗

    check("4-1 取引数", ov4["n_trades"] == exp_n_trades)
    check("4-2 勝率(%)", abs(ov4["win_rate_pct"] - exp_win_rate) < 1e-9, f"exp={exp_win_rate} got={ov4['win_rate_pct']}")
    check("4-3 合計損益(USD)", abs(ov4["total_pnl_usd"] - exp_total_pnl) < 1e-9)
    check("4-4 プロフィットファクター", abs(ov4["profit_factor"] - exp_pf) < 1e-9, f"exp={exp_pf} got={ov4['profit_factor']}")
    check("4-5 平均利益/平均損失/RR", abs(ov4["avg_win"] - exp_avg_win) < 1e-9 and abs(ov4["avg_loss"] - exp_avg_loss) < 1e-9
          and abs(ov4["rr"] - exp_rr) < 1e-9)
    check("4-6 最大ドローダウン(USD)", abs(ov4["max_dd_usd"] - exp_max_dd) < 1e-9, f"exp={exp_max_dd} got={ov4['max_dd_usd']}")
    check("4-7 最大連敗数", ov4["max_consec_losses"] == exp_max_streak, f"exp={exp_max_streak} got={ov4['max_consec_losses']}")

    # -----------------------------------------------------------------
    # 5. normalize_trades_csv: 列名ゆらぎ3パターン+Win_Loss欠落+壊れ行の吸収
    # -----------------------------------------------------------------
    print("\n--- 5. normalize_trades_csv ---")

    raw1 = pd.DataFrame({
        "Entry_Time(JST)": ["2026-01-01 07:15:00", "2026-01-01 08:20:00"],
        "Exit_Time(JST)": ["2026-01-01 07:50:00", "2026-01-01 09:00:00"],
        "Symbol": ["BTC", "ETH"],
        "PnL_USD": [100, -50],
        "PnL_Percent": [1.0, -0.5],
        "Win_Loss": ["Win", "Loss"],
    })
    norm1, err1 = normalize_trades_csv(raw1)
    check("5-1 列名パターン1(英語(JST)表記)が正規化される", norm1 is not None and len(norm1) == 2
          and list(norm1["Win_Loss"]) == ["win", "loss"])

    raw2 = pd.DataFrame({
        "エントリー時刻": ["2026-02-01 10:00:00", "2026-02-01 11:30:00"],
        "銘柄": ["SOL", "HYPE"],
        "損益(USD)": ["1,200.50", "-300"],
    })
    norm2, err2 = normalize_trades_csv(raw2)
    check("5-2 列名パターン2(日本語+Win_Loss欠落からの導出)", norm2 is not None and len(norm2) == 2
          and list(norm2["Win_Loss"]) == ["win", "loss"]
          and abs(norm2["PnL_USD"].iloc[0] - 1200.50) < 1e-9)

    raw3 = pd.DataFrame({
        "entry_time": ["2026-03-01 21:10:00"],
        "symbol": ["BTC"],
        "pnl": [0],
        "win/loss": ["draw"],
    })
    norm3, err3 = normalize_trades_csv(raw3)
    check("5-3 列名パターン3(小文字英語+明示的draw優先)", norm3 is not None and len(norm3) == 1
          and norm3["Win_Loss"].iloc[0] == "draw")

    raw4 = pd.DataFrame({
        "Entry_Time": ["2026-04-01 07:00:00", "not-a-date", "2026-04-01 09:00:00", "2026-04-01 10:00:00"],
        "Symbol": ["BTC", "ETH", "", "SOL"],
        "PnL_USD": [100, 50, 30, "abc"],
    })
    norm4, err4 = normalize_trades_csv(raw4)
    check("5-4 壊れた行(不正日時/空Symbol/非数値PnL)の除外", norm4 is not None and len(norm4) == 1
          and norm4["Win_Loss"].iloc[0] == "win", f"len={len(norm4) if norm4 is not None else None}")
    check("5-5 壊れた行のエラーメッセージが記録される", len(err4) >= 4, f"err4={err4}")

    norm_missing, err_missing = normalize_trades_csv(pd.DataFrame({"Symbol": ["BTC"], "PnL_USD": [1]}))
    check("5-6 必須列欠落(Entry_Time無し)はNoneを返す", norm_missing is None and len(err_missing) >= 1)

    # -----------------------------------------------------------------
    # 6. クロス表組み立て: 市場統計のみ/トレードのみ/両方 の3ケース
    # -----------------------------------------------------------------
    print("\n--- 6. build_cross_table ---")
    expected_rows = 4 + 9  # 大枠4行+詳細帯9行

    cross_market_only = build_cross_table(stats3, None)
    check("6-1 市場統計のみ: 行数=13・列数=8", cross_market_only.shape == (expected_rows, len(CROSS_TABLE_COLUMNS)))
    check("6-2 市場統計のみ: エントリー回数は全てNaN", cross_market_only["エントリー回数"].isna().all())
    check("6-3 市場統計のみ: 平均騰落率は値が入っている", cross_market_only["平均騰落率(%)"].notna().any())

    cross_trades_only = build_cross_table(None, trade_stats4)
    check("6-4 トレードのみ: 行数=13・列数=8", cross_trades_only.shape == (expected_rows, len(CROSS_TABLE_COLUMNS)))
    check("6-5 トレードのみ: 平均騰落率は全てNaN", cross_trades_only["平均騰落率(%)"].isna().all())
    check("6-6 トレードのみ: エントリー回数に値が入っている", cross_trades_only["エントリー回数"].notna().any())

    cross_both = build_cross_table(stats3, trade_stats4)
    check("6-7 両方: 行数=13・列数=8", cross_both.shape == (expected_rows, len(CROSS_TABLE_COLUMNS)))
    check("6-8 両方: 平均騰落率・エントリー回数の両方に値が入っている",
          cross_both["平均騰落率(%)"].notna().any() and cross_both["エントリー回数"].notna().any())

    # -----------------------------------------------------------------
    # 7. 追加検証(敵対検証指摘の修正分): any_yfinance_selected / trim_ohlcv_to_jst_range /
    #    normalize_trades_csv severityタグ・時刻欠落警告 / build_candlestick_chart 空データスキップ
    # -----------------------------------------------------------------
    print("\n--- 7. 追加検証(修正分) ---")

    # 7-1: any_yfinance_selected
    check("7-1a any_yfinance_selected: ccxtのみ(BTC,ETH)->False", any_yfinance_selected(["BTC", "ETH"]) is False)
    check("7-1b any_yfinance_selected: yfinance込み(BTC,GOLD)->True", any_yfinance_selected(["BTC", "GOLD"]) is True)
    check("7-1c any_yfinance_selected: 空リスト->False", any_yfinance_selected([]) is False)
    check("7-1d any_yfinance_selected: カスタム'/'付き->ccxt->False", any_yfinance_selected(["DOGE/USDT"]) is False)
    check("7-1e any_yfinance_selected: カスタム'/'無し->yfinance->True", any_yfinance_selected(["SPY"]) is True)

    # 7-2: trim_ohlcv_to_jst_range (yfinanceの取引所ローカルTZ境界バグの回避ロジック)
    idx7 = pd.date_range("2026-01-01 00:00", "2026-01-05 23:00", freq="1h", tz="Asia/Tokyo")
    df7 = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx7)
    trimmed7 = trim_ohlcv_to_jst_range(df7, date(2026, 1, 2), date(2026, 1, 3))
    exp_first7 = pd.Timestamp("2026-01-02 00:00", tz="Asia/Tokyo")
    exp_last7 = pd.Timestamp("2026-01-03 23:00", tz="Asia/Tokyo")
    check("7-2a trim_ohlcv_to_jst_range: 行数=48(2日分)", len(trimmed7) == 48, f"len={len(trimmed7)}")
    check("7-2b trim_ohlcv_to_jst_range: 先頭=開始日00:00 JST", trimmed7.index.min() == exp_first7,
          f"got={trimmed7.index.min()}")
    check("7-2c trim_ohlcv_to_jst_range: 末尾=終了日23:00 JST(翌日00:00は含まない)", trimmed7.index.max() == exp_last7,
          f"got={trimmed7.index.max()}")

    # 7-3: normalize_trades_csv の severityタグ + Entry_Time時刻欠落警告
    # (注: Entry_Time列に日付のみ表記と時刻付き表記が"混在"すると、pandasのto_datetimeが列全体で
    #  単一フォーマットを推定しようとするため一部がNaTになる別の既知挙動があるため、ここでは
    #  日付のみ表記で統一したケースで時刻欠落警告そのものを検証する)
    raw5 = pd.DataFrame({
        "Entry_Time": ["2026-05-01", "2026-05-02"],
        "Symbol": ["BTC", "ETH"],
        "PnL_USD": [10, 20],
    })
    norm5, err5 = normalize_trades_csv(raw5)
    check("7-3a 時刻欠落行(日付のみ)は除外されず両方読み込まれる", norm5 is not None and len(norm5) == 2,
          f"len={len(norm5) if norm5 is not None else None}")
    warn_msgs5 = [m for sev, m in err5 if sev == "warning" and "時刻情報が無い" in m]
    check("7-3b 時刻欠落はwarningタグで1件(該当2行分)報告される",
          len(warn_msgs5) == 1 and "2件あります" in warn_msgs5[0], f"err5={err5}")
    check("7-3c severityタグ: 必須列欠落はerror",
          normalize_trades_csv(pd.DataFrame({"Symbol": ["BTC"]}))[1][0][0] == "error")
    check("7-3d severityタグ: CSV空はerror", normalize_trades_csv(pd.DataFrame())[1][0][0] == "error")
    bad_tags4 = [sev for sev, _ in err4]
    check("7-3e severityタグ: 壊れた行3件=error・件数サマリ1件=warning",
          bad_tags4.count("error") == 3 and bad_tags4.count("warning") == 1, f"tags={bad_tags4}")

    # 7-4: build_candlestick_chart の空データ銘柄スキップ(単独.iloc[0]クラッシュの回避)
    good_idx7 = pd.date_range("2026-01-01", periods=5, freq="1h", tz="Asia/Tokyo")
    good_df7 = pd.DataFrame(
        {"open": [1, 2, 3, 4, 5], "high": [1, 2, 3, 4, 5], "low": [1, 2, 3, 4, 5],
         "close": [1, 2, 3, 4, 5], "volume": [1, 1, 1, 1, 1]}, index=good_idx7,
    )
    bad_df7 = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    bad_df7.index = pd.DatetimeIndex([], tz="Asia/Tokyo")
    fig7, skipped7, _mc7 = build_candlestick_chart({"GOOD": good_df7, "BAD": bad_df7}, None, False)
    check("7-4a build_candlestick_chart: 空データ銘柄'BAD'をスキップ", skipped7 == ["BAD"], f"skipped={skipped7}")
    check("7-4b build_candlestick_chart: 残った銘柄'GOOD'でfigureを構築できる", fig7 is not None and len(fig7.data) >= 1)
    raised7 = False
    try:
        build_candlestick_chart({"BAD": bad_df7}, None, False)
    except ValueError:
        raised7 = True
    check("7-4c build_candlestick_chart: 全銘柄が空ならValueError", raised7)

    # -----------------------------------------------------------------
    # 8. 追補§1/§2: 曜日別・月別アノマリー + セッション×曜日マトリクス(追補仕様書§3)
    # -----------------------------------------------------------------
    print("\n--- 8. 曜日別・月別アノマリー ---")

    # --- 8-1: compute_weekday_stats(§1.1) 複数年月曜(n=3)+単発火曜(n=1)+データ無し曜日+入力エラー ---
    def _make_day_ohlcv8(day0: pd.Timestamp, base: float) -> pd.DataFrame:
        idx_d, o_d, c_d, v_d = [], [], [], []
        for h in range(24):
            idx_d.append(day0 + pd.Timedelta(hours=h))
            o_d.append(base - 1.0 + h)
            c_d.append(base + h)
            v_d.append(10.0 * (h + 1))
        return pd.DataFrame(
            {"open": o_d, "high": [max(o, c) for o, c in zip(o_d, c_d)],
             "low": [min(o, c) for o, c in zip(o_d, c_d)], "close": c_d, "volume": v_d},
            index=pd.DatetimeIndex(idx_d),
        )

    day_specs8 = [
        ("2026-03-02", 100.0),  # 月(1)
        ("2026-03-09", 150.0),  # 月(2)
        ("2026-03-16", 200.0),  # 月(3)
        ("2026-03-03", 120.0),  # 火(1)
    ]
    parts8 = [_make_day_ohlcv8(pd.Timestamp(d, tz="Asia/Tokyo"), b) for d, b in day_specs8]
    ohlcv8 = pd.concat(parts8).sort_index()
    wstats8 = compute_weekday_stats(ohlcv8)

    check("8-1a compute_weekday_stats: 出力7行(月〜日)", list(wstats8.index) == WEEKDAY_LABELS)

    exp_mon_returns8 = [((base + 23) - (base - 1.0)) / (base - 1.0) * 100.0 for base in (100.0, 150.0, 200.0)]
    exp_mon_return8 = sum(exp_mon_returns8) / 3
    got_mon8 = wstats8.loc["月"]
    check("8-1b 月: n_days=3", int(got_mon8["n_days"]) == 3)
    check("8-1c 月: 平均騰落率(%)", abs(got_mon8["avg_return_pct"] - exp_mon_return8) < 1e-6,
          f"exp={exp_mon_return8:.6f} got={got_mon8['avg_return_pct']:.6f}")
    check("8-1d 月: 平均出来高=3000(1日24hの出来高合計は base に依らず一定)",
          abs(got_mon8["avg_volume"] - 3000.0) < 1e-6)

    base_tue8 = 120.0
    exp_tue_return8 = ((base_tue8 + 23) - (base_tue8 - 1.0)) / (base_tue8 - 1.0) * 100.0
    got_tue8 = wstats8.loc["火"]
    check("8-1e 火: n_days=1・平均騰落率(%)", int(got_tue8["n_days"]) == 1
          and abs(got_tue8["avg_return_pct"] - exp_tue_return8) < 1e-6)

    for _wd8 in ["水", "木", "金", "土", "日"]:
        got_empty8 = wstats8.loc[_wd8]
        check(f"8-1f {_wd8}: データ無し -> n_days=0・NaN", int(got_empty8["n_days"]) == 0
              and pd.isna(got_empty8["avg_return_pct"]) and pd.isna(got_empty8["avg_volume"])
              and pd.isna(got_empty8["avg_volatility_pct"]))

    # 8-1g: 単発オカレンス単独(shift(1)の日またぎ汚染を排除)で平均ボラティリティを厳密検証
    iso_ohlcv8 = _make_day_ohlcv8(pd.Timestamp("2026-04-06", tz="Asia/Tokyo"), 300.0)  # 単独の月曜日
    iso_wstats8 = compute_weekday_stats(iso_ohlcv8)
    rets_iso_w8 = [math.log((300.0 + h) / (300.0 + h - 1)) for h in range(1, 24)]
    mean_iso_w8 = sum(rets_iso_w8) / len(rets_iso_w8)
    var_iso_w8 = sum((r - mean_iso_w8) ** 2 for r in rets_iso_w8) / len(rets_iso_w8)
    exp_vola_iso_w8 = math.sqrt(var_iso_w8) * 100.0
    check("8-1h 単独日: 平均ボラティリティ(%)(先頭shift(1)=NaNで前日分の混入なし)",
          abs(iso_wstats8.loc["月", "avg_volatility_pct"] - exp_vola_iso_w8) < 1e-6,
          f"exp={exp_vola_iso_w8:.6f} got={iso_wstats8.loc['月', 'avg_volatility_pct']:.6f}")

    raised8a = False
    try:
        compute_weekday_stats(ohlcv8.drop(columns=["volume"]))
    except ValueError:
        raised8a = True
    check("8-1i 必須列欠落はValueError", raised8a)

    raised8b = False
    try:
        compute_weekday_stats(ohlcv8.tz_convert(None))
    except ValueError:
        raised8b = True
    check("8-1j tz-naive indexはValueError", raised8b)

    # --- 8-2: 日またぎ境界の帰属 ---
    # compute_weekday_stats は暦日をそのまま単位とする一方、compute_session_weekday_matrix の
    # 「NY重複(21-1)」帯は21時アンカーへ hour=0 を繰り込む(§1.3-3)。両者の対比をここで直接検証する。
    print("\n--- 8-2. 日またぎ境界の帰属(weekday vs session-weekday matrix) ---")
    sun0_8 = pd.Timestamp("2026-01-04 21:00", tz="Asia/Tokyo")  # 2026-01-04は日曜
    idx82 = [sun0_8 + pd.Timedelta(hours=k) for k in range(4)]  # hour=21,22,23(日) + hour=0(月,翌暦日)
    o82 = [100.0, 101.0, 102.0, 200.0]
    c82 = [101.0, 102.0, 103.0, 205.0]
    v82 = [5.0, 5.0, 5.0, 7.0]
    ohlcv82 = pd.DataFrame(
        {"open": o82, "high": [max(o, c) for o, c in zip(o82, c82)],
         "low": [min(o, c) for o, c in zip(o82, c82)], "close": c82, "volume": v82},
        index=pd.DatetimeIndex(idx82),
    )

    wstats82 = compute_weekday_stats(ohlcv82)
    check("8-2a compute_weekday_stats: 日曜はhour21-23のみで完結(騰落率=(103-100)/100*100=3.0%)",
          abs(wstats82.loc["日", "avg_return_pct"] - 3.0) < 1e-9,
          f"got={wstats82.loc['日', 'avg_return_pct']}")
    check("8-2b compute_weekday_stats: 日曜 平均出来高=15(hour21-23分のみ)",
          abs(wstats82.loc["日", "avg_volume"] - 15.0) < 1e-9)
    check("8-2c compute_weekday_stats: 月曜はhour0のみ、繰り込みなし(騰落率=(205-200)/200*100=2.5%)",
          abs(wstats82.loc["月", "avg_return_pct"] - 2.5) < 1e-9,
          f"got={wstats82.loc['月', 'avg_return_pct']}")
    check("8-2d compute_weekday_stats: 月曜 平均出来高=7(hour0のみ)",
          abs(wstats82.loc["月", "avg_volume"] - 7.0) < 1e-9)

    ret_mat82, n_mat82 = compute_session_weekday_matrix(ohlcv82)
    band_ny82 = "NY重複 (21-1)"
    check("8-2e compute_session_weekday_matrix: NY重複帯は21時アンカーへ日曜へ繰り込み"
          "(騰落率=(205-100)/100*100=105.0%)",
          abs(ret_mat82.loc[band_ny82, "日"] - 105.0) < 1e-9,
          f"got={ret_mat82.loc[band_ny82, '日']}")
    check("8-2f compute_session_weekday_matrix: NY重複帯 n(日曜)=1", int(n_mat82.loc[band_ny82, "日"]) == 1)
    check("8-2g compute_session_weekday_matrix: NY重複帯 月曜列はデータなし(hour0は日曜へ繰り込み済)",
          pd.isna(ret_mat82.loc[band_ny82, "月"]) and int(n_mat82.loc[band_ny82, "月"]) == 0)
    check("8-2h compute_session_weekday_matrix: 出力shape=(9,7)",
          ret_mat82.shape == (9, 7) and n_mat82.shape == (9, 7))

    # 8-2i/j: NY重複以外の一般帯でも weekday グルーピングが正しく機能することを確認(単発帯=薄商い(6-7))
    thin_band8 = "薄商い (6-7)"
    ret_mat8b, n_mat8b = compute_session_weekday_matrix(ohlcv8)
    exp_thin_mon8 = sum(100.0 / (base + 5.0) for base in (100.0, 150.0, 200.0)) / 3
    exp_thin_tue8 = 100.0 / (120.0 + 5.0)
    check("8-2i 薄商い帯: 月 平均騰落率(%)・n=3", abs(ret_mat8b.loc[thin_band8, "月"] - exp_thin_mon8) < 1e-9
          and int(n_mat8b.loc[thin_band8, "月"]) == 3)
    check("8-2j 薄商い帯: 火 平均騰落率(%)・n=1", abs(ret_mat8b.loc[thin_band8, "火"] - exp_thin_tue8) < 1e-9
          and int(n_mat8b.loc[thin_band8, "火"]) == 1)

    raised8c = False
    try:
        compute_session_weekday_matrix(ohlcv82.drop(columns=["high"]))
    except ValueError:
        raised8c = True
    check("8-2k compute_session_weekday_matrix: 必須列欠落はValueError", raised8c)

    raised8d = False
    try:
        compute_session_weekday_matrix(ohlcv82.tz_convert(None))
    except ValueError:
        raised8d = True
    check("8-2l compute_session_weekday_matrix: tz-naive indexはValueError", raised8d)

    # --- 8-3: compute_trade_weekday_stats(§1.2) ---
    print("\n--- 8-3. compute_trade_weekday_stats ---")
    trow8 = [
        ("2026-03-02 07:00:00", 100.0, "win"),   # 月
        ("2026-03-02 08:00:00", -30.0, "loss"),  # 月
        ("2026-03-02 09:00:00", 0.0, "draw"),    # 月
        ("2026-03-03 07:00:00", 50.0, "win"),    # 火
        ("2026-03-03 08:00:00", 20.0, "win"),    # 火
        ("2026-03-03 09:00:00", -10.0, "loss"),  # 火
    ]
    tdf8 = pd.DataFrame({
        "Entry_Time": [pd.Timestamp(t, tz="Asia/Tokyo") for t, _, _ in trow8],
        "PnL_USD": [p for _, p, _ in trow8],
        "Win_Loss": [w for _, _, w in trow8],
    })
    twstats8 = compute_trade_weekday_stats(tdf8)
    check("8-3a 出力7行(月〜日)", list(twstats8.index) == WEEKDAY_LABELS)

    got_mon83 = twstats8.loc["月"]
    check("8-3b 月: n_trades=3/wins=1/losses=1/draws=1",
          got_mon83["n_trades"] == 3 and got_mon83["n_wins"] == 1 and got_mon83["n_losses"] == 1
          and got_mon83["n_draws"] == 1)
    check("8-3c 月: 合計損益=70.0・勝率=50.0%(draw除外分母)",
          abs(got_mon83["total_pnl_usd"] - 70.0) < 1e-9 and abs(got_mon83["win_rate_pct"] - 50.0) < 1e-9)

    got_tue83 = twstats8.loc["火"]
    exp_tue_wr83 = 2 / 3 * 100.0
    check("8-3d 火: n_trades=3/wins=2/losses=1/draws=0",
          got_tue83["n_trades"] == 3 and got_tue83["n_wins"] == 2 and got_tue83["n_losses"] == 1
          and got_tue83["n_draws"] == 0)
    check("8-3e 火: 合計損益=60.0・勝率", abs(got_tue83["total_pnl_usd"] - 60.0) < 1e-9
          and abs(got_tue83["win_rate_pct"] - exp_tue_wr83) < 1e-9)

    for _wd83 in ["水", "木", "金", "土", "日"]:
        got_e83 = twstats8.loc[_wd83]
        check(f"8-3f {_wd83}: トレード無し -> n_trades=0・勝率NaN",
              got_e83["n_trades"] == 0 and got_e83["n_wins"] == 0 and got_e83["n_losses"] == 0
              and got_e83["n_draws"] == 0 and pd.isna(got_e83["total_pnl_usd"]) and pd.isna(got_e83["win_rate_pct"]))

    empty_tw8 = compute_trade_weekday_stats(pd.DataFrame())
    check("8-3g 空DataFrame入力: 全曜日 n_trades=0・NaN",
          (empty_tw8["n_trades"] == 0).all() and empty_tw8["total_pnl_usd"].isna().all())

    raised8e = False
    try:
        compute_trade_weekday_stats(pd.DataFrame({"PnL_USD": [1.0]}))
    except ValueError:
        raised8e = True
    check("8-3h Entry_Time列欠落(非空df)はValueError", raised8e)

    # --- 8-4: compute_month_stats(§2.2) 複数年月(1月:陽線2/陰線1)+単発月(2月)+isolated分散検証 ---
    print("\n--- 8-4. compute_month_stats ---")
    daily_rows8 = [
        ("2024-01-10", 100.0, 101.0), ("2024-01-11", 102.0, 110.0),   # 2024年1月: +10.0%
        ("2025-01-10", 200.0, 195.0), ("2025-01-11", 194.0, 180.0),   # 2025年1月: -10.0%
        ("2026-01-10", 300.0, 305.0), ("2026-01-11", 306.0, 315.0),   # 2026年1月: +5.0%
        ("2026-02-10", 400.0, 405.0), ("2026-02-11", 406.0, 420.0),   # 2026年2月: +5.0%
    ]
    daily_df8 = pd.DataFrame(
        {"open": [o for _, o, _ in daily_rows8], "close": [c for _, _, c in daily_rows8]},
        index=pd.DatetimeIndex([d for d, _, _ in daily_rows8]),
    )
    mstats8 = compute_month_stats(daily_df8)
    check("8-4a compute_month_stats: 出力12行(1〜12月)", list(mstats8.index) == MONTH_ORDER)

    got_jan8 = mstats8.loc[1]
    exp_jan_returns8 = [10.0, -10.0, 5.0]
    exp_jan_avg8 = sum(exp_jan_returns8) / 3
    exp_jan_median8 = statistics.median(exp_jan_returns8)
    exp_jan_pctpos8 = sum(1 for r in exp_jan_returns8 if r > 0) / 3 * 100.0
    check("8-4b 1月: n_years=3", int(got_jan8["n_years"]) == 3)
    check("8-4c 1月: 平均騰落率(%)", abs(got_jan8["avg_return_pct"] - exp_jan_avg8) < 1e-9,
          f"exp={exp_jan_avg8} got={got_jan8['avg_return_pct']}")
    check("8-4d 1月: 中央値騰落率(%)", abs(got_jan8["median_return_pct"] - exp_jan_median8) < 1e-9)
    check("8-4e 1月: 陽線率(%)=66.667(3年中2年プラス)", abs(got_jan8["pct_positive"] - exp_jan_pctpos8) < 1e-9,
          f"exp={exp_jan_pctpos8} got={got_jan8['pct_positive']}")

    got_feb8 = mstats8.loc[2]
    check("8-4f 2月: n_years=1・平均騰落率=5.0%・陽線率=100%",
          int(got_feb8["n_years"]) == 1 and abs(got_feb8["avg_return_pct"] - 5.0) < 1e-9
          and abs(got_feb8["pct_positive"] - 100.0) < 1e-9)

    for _mo8 in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
        got_e84 = mstats8.loc[_mo8]
        check(f"8-4g {_mo8}月: データ無し -> n_years=0・NaN", int(got_e84["n_years"]) == 0
              and pd.isna(got_e84["avg_return_pct"]) and pd.isna(got_e84["median_return_pct"])
              and pd.isna(got_e84["pct_positive"]) and pd.isna(got_e84["avg_volatility_pct"]))

    # 8-4h/i: 単発オカレンス単独(前後にデータ無し)で月内ボラを厳密検証(shift(1)の日またぎ汚染を排除)
    iso_rows8 = [("2026-03-05", 500.0, 510.0), ("2026-03-06", 511.0, 505.0), ("2026-03-07", 506.0, 520.0)]
    iso_df8 = pd.DataFrame(
        {"open": [o for _, o, _ in iso_rows8], "close": [c for _, _, c in iso_rows8]},
        index=pd.DatetimeIndex([d for d, _, _ in iso_rows8]),
    )
    iso_mstats8 = compute_month_stats(iso_df8)
    closes_iso8 = [c for _, _, c in iso_rows8]
    rets_iso8 = [math.log(closes_iso8[i] / closes_iso8[i - 1]) for i in range(1, len(closes_iso8))]
    mean_iso8 = sum(rets_iso8) / len(rets_iso8)
    var_iso8 = sum((r - mean_iso8) ** 2 for r in rets_iso8) / len(rets_iso8)
    exp_vola_iso8 = math.sqrt(var_iso8) * 100.0
    exp_return_iso8 = (520.0 - 500.0) / 500.0 * 100.0
    got_iso8 = iso_mstats8.loc[3]
    check("8-4h 単発月: 平均騰落率(%)=(520-500)/500*100", abs(got_iso8["avg_return_pct"] - exp_return_iso8) < 1e-9)
    check("8-4i 単発月: 平均ボラティリティ(%)(先頭shift(1)=NaNで前オカレンス分の混入なし)",
          abs(got_iso8["avg_volatility_pct"] - exp_vola_iso8) < 1e-6,
          f"exp={exp_vola_iso8:.6f} got={got_iso8['avg_volatility_pct']:.6f}")

    raised8f = False
    try:
        compute_month_stats(daily_df8.drop(columns=["close"]))
    except ValueError:
        raised8f = True
    check("8-4j 必須列欠落はValueError", raised8f)

    empty_m8 = compute_month_stats(pd.DataFrame(columns=["open", "close"]))
    check("8-4k 空DataFrame入力: 全12ヶ月 n_years=0・NaN",
          (empty_m8["n_years"] == 0).all() and empty_m8["avg_return_pct"].isna().all())

    # --- 8-4l〜p: 確定指摘#1対応(欠け月混入): today_ 引数による進行中当月の除外 ---
    today8_jan = date(2026, 1, 15)  # 2026年1月はdaily_rows8の1月3件中の1件(+5.0%オカレンス)
    mstats8_excl_jan = compute_month_stats(daily_df8, today_=today8_jan)
    got_jan8_excl = mstats8_excl_jan.loc[1]
    exp_jan_returns8_excl = [10.0, -10.0]  # 2026年1月分を除外した残り2件
    exp_jan_avg8_excl = sum(exp_jan_returns8_excl) / len(exp_jan_returns8_excl)
    exp_jan_median8_excl = statistics.median(exp_jan_returns8_excl)
    exp_jan_pctpos8_excl = sum(1 for r in exp_jan_returns8_excl if r > 0) / len(exp_jan_returns8_excl) * 100.0
    check("8-4l today_指定: 1月はn_years=3->2・n_excluded_current=1",
          int(got_jan8_excl["n_years"]) == 2 and int(got_jan8_excl["n_excluded_current"]) == 1)
    check("8-4m today_指定: 1月の平均/中央値/陽線率が除外後の値と一致",
          abs(got_jan8_excl["avg_return_pct"] - exp_jan_avg8_excl) < 1e-9
          and abs(got_jan8_excl["median_return_pct"] - exp_jan_median8_excl) < 1e-9
          and abs(got_jan8_excl["pct_positive"] - exp_jan_pctpos8_excl) < 1e-9)
    got_feb8_excl = mstats8_excl_jan.loc[2]
    check("8-4n today_指定: 無関係の2月はn_excluded_current=0で従来通り(n_years=1)",
          int(got_feb8_excl["n_excluded_current"]) == 0 and int(got_feb8_excl["n_years"]) == 1)

    today8_feb = date(2026, 2, 20)  # 2026年2月は単発オカレンス -> 除外後n_years=0でNaN
    mstats8_excl_feb = compute_month_stats(daily_df8, today_=today8_feb)
    got_feb8_excl2 = mstats8_excl_feb.loc[2]
    check("8-4o today_指定: 単発月が当月除外でn_years=0・NaN・n_excluded_current=1",
          int(got_feb8_excl2["n_years"]) == 0 and int(got_feb8_excl2["n_excluded_current"]) == 1
          and pd.isna(got_feb8_excl2["avg_return_pct"]))

    check("8-4p today_=None(既定)は従来どおり全件計上(回帰ガード)",
          int(mstats8.loc[1]["n_years"]) == 3 and int(mstats8.loc[1]["n_excluded_current"]) == 0
          and int(mstats8.loc[2]["n_years"]) == 1 and int(mstats8.loc[2]["n_excluded_current"]) == 0)

    # --- 8-5: compute_trade_month_stats(§2.3) ---
    print("\n--- 8-5. compute_trade_month_stats ---")
    mrow8 = [
        ("2026-01-05 07:00:00", 100.0, "win"),
        ("2026-01-06 07:00:00", -40.0, "loss"),
        ("2026-01-07 07:00:00", 0.0, "draw"),
        ("2026-02-05 07:00:00", 70.0, "win"),
        ("2026-02-06 07:00:00", 30.0, "win"),
    ]
    tmdf8 = pd.DataFrame({
        "Entry_Time": [pd.Timestamp(t, tz="Asia/Tokyo") for t, _, _ in mrow8],
        "PnL_USD": [p for _, p, _ in mrow8],
        "Win_Loss": [w for _, _, w in mrow8],
    })
    tmstats8 = compute_trade_month_stats(tmdf8)
    check("8-5a 出力12行(1〜12月)", list(tmstats8.index) == MONTH_ORDER)

    got_jan85 = tmstats8.loc[1]
    check("8-5b 1月: n_trades=3/wins=1/losses=1/draws=1",
          got_jan85["n_trades"] == 3 and got_jan85["n_wins"] == 1 and got_jan85["n_losses"] == 1
          and got_jan85["n_draws"] == 1)
    check("8-5c 1月: 合計損益=60.0・勝率=50.0%(draw除外分母)",
          abs(got_jan85["total_pnl_usd"] - 60.0) < 1e-9 and abs(got_jan85["win_rate_pct"] - 50.0) < 1e-9)

    got_feb85 = tmstats8.loc[2]
    check("8-5d 2月: n_trades=2/wins=2/losses=0・合計損益=100.0・勝率=100%",
          got_feb85["n_trades"] == 2 and got_feb85["n_wins"] == 2 and got_feb85["n_losses"] == 0
          and abs(got_feb85["total_pnl_usd"] - 100.0) < 1e-9 and abs(got_feb85["win_rate_pct"] - 100.0) < 1e-9)

    for _mo85 in [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
        got_e85 = tmstats8.loc[_mo85]
        check(f"8-5e {_mo85}月: トレード無し -> n_trades=0・NaN", got_e85["n_trades"] == 0
              and pd.isna(got_e85["total_pnl_usd"]) and pd.isna(got_e85["win_rate_pct"]))

    empty_tm8 = compute_trade_month_stats(pd.DataFrame())
    check("8-5f 空DataFrame入力: 全12ヶ月 n_trades=0・NaN",
          (empty_tm8["n_trades"] == 0).all() and empty_tm8["total_pnl_usd"].isna().all())

    raised8g = False
    try:
        compute_trade_month_stats(pd.DataFrame({"PnL_USD": [1.0]}))
    except ValueError:
        raised8g = True
    check("8-5g Entry_Time列欠落(非空df)はValueError", raised8g)

    # --- 8-6: compute_trade_session_weekday_matrix(§1.3-4) ---
    print("\n--- 8-6. compute_trade_session_weekday_matrix ---")
    srow8 = [
        ("2026-03-02 08:00:00", "win"),   # 月・アジア早朝(7-10)
        ("2026-03-02 09:00:00", "loss"),  # 月・アジア早朝(7-10)
        ("2026-03-03 08:00:00", "win"),   # 火・アジア早朝(7-10)
    ]
    sdf8 = pd.DataFrame({
        "Entry_Time": [pd.Timestamp(t, tz="Asia/Tokyo") for t, _ in srow8],
        "PnL_USD": [0.0] * len(srow8),
        "Win_Loss": [w for _, w in srow8],
    })
    sdf8_assigned = assign_trade_sessions(sdf8)
    win_mat8, n_mat8 = compute_trade_session_weekday_matrix(sdf8_assigned)
    band_asia8 = "アジア早朝 (7-10)"
    check("8-6a 出力shape=(9,7)", win_mat8.shape == (9, 7) and n_mat8.shape == (9, 7))
    check("8-6b 月・アジア早朝: 勝率=50.0%・n=2", abs(win_mat8.loc[band_asia8, "月"] - 50.0) < 1e-9
          and int(n_mat8.loc[band_asia8, "月"]) == 2)
    check("8-6c 火・アジア早朝: 勝率=100.0%・n=1", abs(win_mat8.loc[band_asia8, "火"] - 100.0) < 1e-9
          and int(n_mat8.loc[band_asia8, "火"]) == 1)
    check("8-6d 水: 当該帯データ無し -> NaN・n=0",
          pd.isna(win_mat8.loc[band_asia8, "水"]) and int(n_mat8.loc[band_asia8, "水"]) == 0)

    empty_win8, empty_n8 = compute_trade_session_weekday_matrix(pd.DataFrame())
    check("8-6e 空DataFrame入力: 全セルNaN/0", empty_win8.isna().all().all() and (empty_n8 == 0).all().all())

    raised8h = False
    try:
        compute_trade_session_weekday_matrix(sdf8)  # band列を付与していない生データ
    except ValueError:
        raised8h = True
    check("8-6f band列が無い入力はValueError", raised8h)

    # -----------------------------------------------------------------
    # 9. 追補v3 §1: diverging_color / compute_diverging_vmax(境界値必須)
    # -----------------------------------------------------------------
    print("\n--- 9. diverging_color / compute_diverging_vmax ---")
    check("9-1 正の値: 緑グラデーション(rgba(56,168,96,...))",
          diverging_color(5.0, 10.0).startswith("background-color: rgba(56,168,96,"))
    check("9-2 負の値: 赤グラデーション(rgba(224,64,64,...))",
          diverging_color(-5.0, 10.0).startswith("background-color: rgba(224,64,64,"))
    check("9-3 v=0.0は無色(境界)", diverging_color(0.0, 10.0) == "")
    check("9-4 v=NaNは無色", diverging_color(float("nan"), 10.0) == "")
    check("9-5 vmax=NaNは無色", diverging_color(5.0, float("nan")) == "")
    check("9-6 vmax=0は無色(境界・ゼロ割回避)", diverging_color(5.0, 0.0) == "")
    check("9-7 vmax<0は無色", diverging_color(5.0, -1.0) == "")

    alpha_full9 = 0.15 + 0.55 * 1.0
    alpha_half9 = 0.15 + 0.55 * 0.5
    check("9-8a v=vmax(飽和): 緑・alpha頭打ち",
          diverging_color(10.0, 10.0) == f"background-color: rgba(56,168,96,{alpha_full9:.2f}); color: #fff")
    check("9-8b |v|>vmax(範囲外)は1.0にクリップされ同じalpha",
          diverging_color(20.0, 10.0) == f"background-color: rgba(56,168,96,{alpha_full9:.2f}); color: #fff")
    check("9-8c v=-vmax/2: 赤・alpha中間値",
          diverging_color(-5.0, 10.0) == f"background-color: rgba(224,64,64,{alpha_half9:.2f}); color: #fff")

    vmax_df9 = pd.DataFrame({
        "平均騰落率(%)": [1.0, -3.5, np.nan],
        "中央値騰落率(%)": [2.0, np.nan, -7.0],
        "勝率(%)": [60.0, 40.0, 50.0],
    })
    check("9-9a compute_diverging_vmax: 平均/中央値2列合わせた絶対値最大=7.0",
          abs(compute_diverging_vmax(vmax_df9, DIVERGING_RETURN_COLUMNS) - 7.0) < 1e-9)
    check("9-9b 該当列が無いdfは0.0",
          compute_diverging_vmax(pd.DataFrame({"勝率(%)": [60.0]}), DIVERGING_RETURN_COLUMNS) == 0.0)
    check("9-9c 該当列が全NaNのみは0.0",
          compute_diverging_vmax(pd.DataFrame({"平均騰落率(%)": [np.nan, np.nan]}), DIVERGING_RETURN_COLUMNS) == 0.0)

    # -----------------------------------------------------------------
    # 10. 追補v3 §2: split_cross_table(親表4行+大枠ごとの詳細表への分割)
    # -----------------------------------------------------------------
    print("\n--- 10. split_cross_table ---")
    stats10 = pd.DataFrame(
        {"avg_return_pct": 1.0, "avg_volume": 100.0, "avg_volatility_pct": 0.5}, index=BAND_ORDER,
    )
    cross10 = build_cross_table(stats10, None)
    parent_df10, detail_by_parent10 = split_cross_table(cross10)
    check("10-1 親表は4行・indexはPARENT_ORDERそのもの",
          list(parent_df10.index) == PARENT_ORDER and len(parent_df10) == 4)
    check("10-2 親表の値はbuild_cross_tableの大枠計行と一致(全帯同値=1.0の加重平均=1.0)",
          (parent_df10["平均騰落率(%)"] == 1.0).all())
    check("10-3 詳細表のキーは大枠4種、各行数は子帯数と一致",
          set(detail_by_parent10.keys()) == set(PARENT_ORDER)
          and all(
              len(detail_by_parent10[p]) == len([b for b in BAND_ORDER if BAND_TO_PARENT[b] == p])
              for p in PARENT_ORDER
          ))
    check("10-4 詳細表のindexは対応する大枠の子帯のみ(親行ラベル(大枠計)を含まない)",
          all(
              set(detail_by_parent10[p].index) == {b for b in BAND_ORDER if BAND_TO_PARENT[b] == p}
              for p in PARENT_ORDER
          ))
    check("10-5 親表+全詳細表の行数合計は元のcrossの行数(13)と一致(欠損・重複無し)",
          len(parent_df10) + sum(len(d) for d in detail_by_parent10.values()) == len(cross10))

    # -----------------------------------------------------------------
    # 11. 追補v3 §3/§4: 背景帯opacity配線・_build_fetch_meta契約・キャプション整形
    # -----------------------------------------------------------------
    print("\n--- 11. 背景帯opacity / _build_fetch_meta / format_data_source_caption ---")
    start_ts11 = pd.Timestamp("2026-01-05 00:00", tz=JST)
    end_ts11 = pd.Timestamp("2026-01-05 06:00", tz=JST)
    shapes_lo11 = build_session_background_shapes(start_ts11, end_ts11, opacity=0.05)
    shapes_hi11 = build_session_background_shapes(start_ts11, end_ts11, opacity=0.70)
    check("11-1a opacity下限0.05(スライダー最小5%)がfillcolorのalphaへ反映",
          len(shapes_lo11) > 0 and all(s["fillcolor"].endswith(",0.05)") for s in shapes_lo11))
    check("11-1b opacity上限0.70(スライダー最大70%)がfillcolorのalphaへ反映",
          len(shapes_hi11) > 0 and all(s["fillcolor"].endswith(",0.7)") for s in shapes_hi11))

    meta_empty11 = _build_fetch_meta("yfinance", "GC=F", pd.DataFrame())
    check("11-2a 空df: 5キー(source/ticker/fetched_at/last_bar/rows)が揃う",
          set(meta_empty11.keys()) == {"source", "ticker", "fetched_at", "last_bar", "rows"})
    check("11-2b 空df: last_barはNone・rowsは0", meta_empty11["last_bar"] is None and meta_empty11["rows"] == 0)
    check("11-2c fetched_atはdatetime型", isinstance(meta_empty11["fetched_at"], datetime))

    idx11 = pd.date_range("2026-01-01", periods=3, freq="1h", tz=JST)
    df11 = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx11)
    meta_full11 = _build_fetch_meta("ccxt(binance)", "BTC/USDT", df11)
    check("11-3a 非空df: last_barはdf.index[-1]と一致", meta_full11["last_bar"] == idx11[-1])
    check("11-3b 非空df: rowsはlen(df)と一致(=3)", meta_full11["rows"] == 3)
    check("11-3c sourceは受け取った文字列をそのまま保持(加工しない)",
          meta_full11["source"] == "ccxt(binance)")

    src11 = inspect.getsource(fetch_ccxt_ohlcv)
    check("11-4 fetch_ccxt_ohlcv: README§14契約通りsourceを`ccxt(...)`でラップして渡す",
          "ccxt(" in src11 and "_build_fetch_meta" in src11)

    check("11-5a meta=Noneは「不明」フォールバック文字列",
          format_data_source_caption("BTC", None, 15) == "📊 データ元: BTC=不明(メタ情報なし)")
    check("11-5b meta={}(空dict)も同じ「不明」フォールバック",
          format_data_source_caption("BTC", {}, 15) == "📊 データ元: BTC=不明(メタ情報なし)")

    meta_ccxt11 = {
        "source": "ccxt(binance)", "ticker": "BTC/USDT", "fetched_at": None, "last_bar": None, "rows": 0,
    }
    cap_ccxt11 = format_data_source_caption("BTC", meta_ccxt11, 15)
    check("11-6a ccxt sourceは`ccxt(binance)`のまま(README表記通り)表示される",
          "BTC=ccxt(binance)(BTC/USDT)" in cap_ccxt11)
    check("11-6b fetched_at/last_bar=None時はどちらも「不明」表示",
          "取得: 不明" in cap_ccxt11 and "最終足: 不明" in cap_ccxt11)

    fetched11 = datetime(2026, 7, 10, 22, 22, tzinfo=JST)
    last_bar11 = pd.Timestamp("2026-07-01 02:00", tz=JST)
    meta_yf11 = {
        "source": "yfinance", "ticker": "GC=F", "fetched_at": fetched11, "last_bar": last_bar11, "rows": 100,
    }
    check("11-7 正常メタ(yfinance): 取得時刻・最終足・キャッシュ分が期待通り整形される",
          format_data_source_caption("GOLD", meta_yf11, 15) == (
              "📊 データ元: GOLD=yfinance(GC=F) | ⏱ 取得: 2026-07-10 22:22 JST時点(キャッシュ15分) | "
              "最終足: 07-01 02:00 JST"
          ))

    # -----------------------------------------------------------------
    # 12. 追補v4: Side/Leverage/Entry_Price/Exit_Price ゆらぎ吸収 + compare_trade_stats +
    #     map_trades_to_chart(§1.4/§2.1/§2.3)
    # -----------------------------------------------------------------
    print("\n--- 12. Side/Leverage/Price吸収・compare_trade_stats・map_trades_to_chart ---")

    # 12-1: Side列ゆらぎ吸収(LONG/SHORT・大小文字・ロング/ショート・買/売・L/S・認識不能はNaN)
    raw12_side = pd.DataFrame({
        "Entry_Time": ["2026-01-01 07:00:00"] * 11,
        "Symbol": ["BTC"] * 11,
        "PnL_USD": [1] * 11,
        "Side": ["LONG", "SHORT", "long", "short", "ロング", "ショート", "買", "売", "l", "s", "xyz"],
    })
    norm12_side, _ = normalize_trades_csv(raw12_side)
    side_vals12 = list(norm12_side["Side"])
    expected_side12 = ["LONG", "SHORT"] * 5 + [None]
    check("12-1 Sideゆらぎ10パターン+認識不能1件がLONG/SHORT/NaNへ正規化",
          all(
              (pd.isna(got) if exp is None else got == exp)
              for got, exp in zip(side_vals12, expected_side12)
          ), f"got={side_vals12}")

    # 12-2: Leverage列ゆらぎ吸収("10x"/"x10"/数値/認識不能)
    raw12_lev = pd.DataFrame({
        "Entry_Time": ["2026-01-01 07:00:00"] * 6,
        "Symbol": ["BTC"] * 6,
        "PnL_USD": [1] * 6,
        "Leverage": ["10x", "x10", "25", "3.5", 10, "abc"],
    })
    norm12_lev, _ = normalize_trades_csv(raw12_lev)
    lev_vals12 = list(norm12_lev["Leverage"])
    expected_lev12 = [10.0, 10.0, 25.0, 3.5, 10.0, float("nan")]
    check("12-2 Leverageゆらぎ(10x/x10/数値文字列/数値/認識不能)が数値化・NaN化される",
          all(
              (math.isnan(got) if math.isnan(exp) else abs(got - exp) < 1e-9)
              for got, exp in zip(lev_vals12, expected_lev12)
          ), f"got={lev_vals12}")

    # 12-3: Entry_Price/Exit_Price 列名ゆらぎ(日本語)+ 数値クレンジング($ , ¥ 等)
    raw12_price = pd.DataFrame({
        "Entry_Time": ["2026-01-01 07:00:00"] * 3,
        "Symbol": ["BTC"] * 3,
        "PnL_USD": [1, 2, 3],
        "エントリー価格": ["$68,000.5", "70000", None],
        "決済価格": [69000, "¥71,000", None],
    })
    norm12_price, _ = normalize_trades_csv(raw12_price)
    entry_p12 = list(norm12_price["Entry_Price"])
    exit_p12 = list(norm12_price["Exit_Price"])
    check("12-3a Entry_Price(日本語列名+$,区切り)が数値化される",
          abs(entry_p12[0] - 68000.5) < 1e-9 and abs(entry_p12[1] - 70000.0) < 1e-9 and math.isnan(entry_p12[2]),
          f"entry_p12={entry_p12}")
    check("12-3b Exit_Price(日本語列名+¥,区切り)が数値化される",
          abs(exit_p12[0] - 69000.0) < 1e-9 and abs(exit_p12[1] - 71000.0) < 1e-9 and math.isnan(exit_p12[2]),
          f"exit_p12={exit_p12}")

    # 12-4: Side/Leverage/Entry_Price/Exit_Price列が全く無いCSVでも読み込め、該当4列は全てNaN(欠落時NaN)
    raw12_missing = pd.DataFrame({
        "Entry_Time": ["2026-01-01 07:00:00", "2026-01-02 08:00:00"],
        "Symbol": ["BTC", "ETH"], "PnL_USD": [1, -1],
    })
    norm12_missing, err12_missing = normalize_trades_csv(raw12_missing)
    check("12-4 新4列が無いCSVも正常読込・4列とも全行NaN(後方互換)",
          norm12_missing is not None and len(norm12_missing) == 2
          and all(c in norm12_missing.columns for c in ["Side", "Leverage", "Entry_Price", "Exit_Price"])
          and norm12_missing["Side"].isna().all() and norm12_missing["Leverage"].isna().all()
          and norm12_missing["Entry_Price"].isna().all() and norm12_missing["Exit_Price"].isna().all(),
          f"cols={list(norm12_missing.columns) if norm12_missing is not None else None}")

    # 12-5: compare_trade_stats() 既知解(帯の合併・片側欠落・overall_diffの符号=b-a)
    stats_a12 = {
        "overall": {
            "total_pnl_usd": 100.0, "n_trades": 10, "win_rate_pct": 50.0, "profit_factor": 1.5,
            "avg_win": 20.0, "avg_loss": -15.0, "rr": 1.33, "max_dd_usd": -80.0, "max_consec_losses": 3,
            "first_time": pd.Timestamp("2026-01-01", tz="Asia/Tokyo"),
            "last_time": pd.Timestamp("2026-01-10", tz="Asia/Tokyo"),
        },
        "by_band": pd.DataFrame(
            {"n_trades": [5, 2], "n_wins": [3, 1], "n_losses": [2, 1], "n_draws": [0, 0],
             "total_pnl_usd": [50.0, 10.0], "win_rate_pct": [60.0, 50.0]},
            index=pd.Index(["BandX", "BandY"], name="band"),
        ),
    }
    stats_b12 = {
        "overall": {
            "total_pnl_usd": 300.0, "n_trades": 12, "win_rate_pct": 80.0, "profit_factor": 4.0,
            "avg_win": 30.0, "avg_loss": -10.0, "rr": 3.0, "max_dd_usd": -20.0, "max_consec_losses": 1,
            "first_time": pd.Timestamp("2026-02-01", tz="Asia/Tokyo"),
            "last_time": pd.Timestamp("2026-02-15", tz="Asia/Tokyo"),
        },
        "by_band": pd.DataFrame(
            {"n_trades": [6, 4], "n_wins": [5, 4], "n_losses": [1, 0], "n_draws": [0, 0],
             "total_pnl_usd": [150.0, 100.0], "win_rate_pct": [83.333333333, 100.0]},
            index=pd.Index(["BandX", "BandZ"], name="band"),
        ),
    }
    cmp12 = compare_trade_stats(stats_a12, stats_b12)
    diff12 = cmp12["overall_diff"]
    exp_diff12 = {
        "total_pnl_usd": 200.0, "n_trades": 2.0, "win_rate_pct": 30.0, "profit_factor": 2.5,
        "avg_win": 10.0, "avg_loss": 5.0, "rr": 1.67, "max_dd_usd": 60.0, "max_consec_losses": -2.0,
    }
    check("12-5a overall_diffが全指標で b-a の符号・値と一致",
          all(abs(diff12[k] - exp_diff12[k]) < 1e-6 for k in exp_diff12), f"diff12={diff12}")
    bb12 = cmp12["by_band"]
    check("12-5b by_band帯の合併順序が a登場順→bのみの帯を末尾 (BandX,BandY,BandZ)",
          list(bb12.index) == ["BandX", "BandY", "BandZ"], f"index={list(bb12.index)}")
    row_x12 = bb12.loc["BandX"]
    check("12-5c BandX(両側にあり)の実数値・勝率差・低n判定が正しい",
          row_x12["n_trades_a"] == 5 and row_x12["n_trades_b"] == 6
          and abs(row_x12["win_rate_pct_a"] - 60.0) < 1e-9 and abs(row_x12["win_rate_pct_b"] - 83.333333333) < 1e-6
          and abs(row_x12["win_rate_diff"] - 23.333333333) < 1e-4
          and abs(row_x12["total_pnl_diff"] - 100.0) < 1e-9
          and row_x12["low_n_a"] == False and row_x12["low_n_b"] == False, f"row_x12={dict(row_x12)}")
    row_y12 = bb12.loc["BandY"]
    check("12-5d BandY(aのみ)はb側が0/NaN補完・low_n両True(片側欠落かつn<3)",
          row_y12["n_trades_a"] == 2 and row_y12["n_trades_b"] == 0
          and math.isnan(row_y12["win_rate_pct_b"]) and math.isnan(row_y12["win_rate_diff"])
          and row_y12["low_n_a"] == True and row_y12["low_n_b"] == True, f"row_y12={dict(row_y12)}")
    row_z12 = bb12.loc["BandZ"]
    check("12-5e BandZ(bのみ)はa側が0/NaN補完・low_n_a=True/low_n_b=False(n=4)",
          row_z12["n_trades_b"] == 4 and row_z12["n_trades_a"] == 0
          and math.isnan(row_z12["win_rate_pct_a"]) and math.isnan(row_z12["total_pnl_diff"])
          and row_z12["low_n_a"] == True and row_z12["low_n_b"] == False, f"row_z12={dict(row_z12)}")
    check("12-5f period_a/period_bがoverallのfirst_time/last_timeをそのまま反映",
          cmp12["period_a"] == (stats_a12["overall"]["first_time"], stats_a12["overall"]["last_time"])
          and cmp12["period_b"] == (stats_b12["overall"]["first_time"], stats_b12["overall"]["last_time"]),
          f"period_a={cmp12['period_a']} period_b={cmp12['period_b']}")

    # 12-6: map_trades_to_chart() 境界(実価格/近似価格・正規化・期間外除外・Exit欠落)
    idx12m = pd.date_range("2026-01-01 00:00", periods=6, freq="1h", tz="Asia/Tokyo")
    ohlcv12m = pd.DataFrame({"close": [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]}, index=idx12m)
    trades12m = pd.DataFrame({
        "Entry_Time": [
            pd.Timestamp("2026-01-01 01:00", tz="Asia/Tokyo"),
            pd.Timestamp("2026-01-01 02:30", tz="Asia/Tokyo"),
            pd.Timestamp("2025-12-31 23:00", tz="Asia/Tokyo"),
            pd.Timestamp("2026-01-01 00:15", tz="Asia/Tokyo"),
        ],
        "Entry_Price": [103.0, np.nan, 999.0, np.nan],
        "Exit_Time": [
            pd.Timestamp("2026-01-01 03:00", tz="Asia/Tokyo"), pd.NaT,
            pd.Timestamp("2025-12-31 23:30", tz="Asia/Tokyo"),
            pd.Timestamp("2026-01-01 04:45", tz="Asia/Tokyo"),
        ],
        "Exit_Price": [107.0, np.nan, 999.0, np.nan],
        "Side": ["LONG", "SHORT", "LONG", np.nan],
        "Leverage": [10.0, np.nan, 1.0, 5.0],
        "PnL_USD": [50.0, -10.0, 5.0, 20.0], "PnL_Percent": [2.0, -1.0, 0.5, 1.0],
        "Win_Loss": ["win", "loss", "win", "win"],
    }, index=[10, 11, 12, 13])
    mapped12m = map_trades_to_chart(trades12m, ohlcv12m, normalize_base=None)
    check("12-6a 期間外(12)は除外され3件のみ残る・indexラベル保持",
          list(mapped12m.index) == [10, 11, 13], f"index={list(mapped12m.index)}")
    r10_12m = mapped12m.loc[10]
    check("12-6b 実価格ありトレード(10)は実値そのまま・is_approx=False・保有時間120分",
          abs(r10_12m["entry_price"] - 103.0) < 1e-9 and bool(r10_12m["entry_price_is_approx"]) == False
          and abs(r10_12m["exit_price"] - 107.0) < 1e-9 and bool(r10_12m["exit_price_is_approx"]) == False
          and abs(r10_12m["holding_minutes"] - 120.0) < 1e-9, f"r10={dict(r10_12m)}")
    r11_12m = mapped12m.loc[11]
    check("12-6c 価格欠落+Exit欠落(11)は近似close(02:00足=104)・is_approx=True・exit系は全NaN/NaT",
          abs(r11_12m["entry_price"] - 104.0) < 1e-9 and bool(r11_12m["entry_price_is_approx"]) == True
          and pd.isna(r11_12m["exit_time"]) and math.isnan(r11_12m["exit_price"])
          and math.isnan(r11_12m["holding_minutes"]), f"r11={dict(r11_12m)}")
    r13_12m = mapped12m.loc[13]
    check("12-6d 価格欠落だがExitあり(13)はentry≈100(00:00足)/exit≈108(04:00足)・両is_approx=True・保有270分",
          abs(r13_12m["entry_price"] - 100.0) < 1e-9 and bool(r13_12m["entry_price_is_approx"]) == True
          and abs(r13_12m["exit_price"] - 108.0) < 1e-9 and bool(r13_12m["exit_price_is_approx"]) == True
          and abs(r13_12m["holding_minutes"] - 270.0) < 1e-9, f"r13={dict(r13_12m)}")

    mapped12m_norm = map_trades_to_chart(trades12m, ohlcv12m, normalize_base=200.0)
    r10n_12m = mapped12m_norm.loc[10]
    check("12-6e normalize_base=200指定で実価格が/200*100に変換(103→51.5, 107→53.5)",
          abs(r10n_12m["entry_price"] - 51.5) < 1e-9 and abs(r10n_12m["exit_price"] - 53.5) < 1e-9,
          f"r10n={dict(r10n_12m)}")

    empty_trades12m = map_trades_to_chart(pd.DataFrame(), ohlcv12m, None)
    empty_ohlcv12m = map_trades_to_chart(trades12m, pd.DataFrame(), None)
    check("12-6f trades_df空/ohlcv_df空のどちらでも列定義のみの空DataFrameを返す",
          list(empty_trades12m.columns) == _MAP_TRADES_RESULT_COLUMNS and empty_trades12m.empty
          and list(empty_ohlcv12m.columns) == _MAP_TRADES_RESULT_COLUMNS and empty_ohlcv12m.empty,
          f"cols_a={list(empty_trades12m.columns)} cols_b={list(empty_ohlcv12m.columns)}")

    # -----------------------------------------------------------------
    # 13. 追補v4§2.4A(C3a): 分足対応 — TIMEFRAME_CHOICES・any_ccxt_selected・
    #     resolve_intraday_effective_choice・compute_intraday_chart_window
    # -----------------------------------------------------------------
    print("\n--- 13. 分足対応(C3a): 足種選択肢・暗号判定・フォールバック・バー数ガード ---")

    check("13-0a TIMEFRAME_CHOICESは分足3種+既存3種=6選択肢(1分足が先頭・既定は1時間足)",
          TIMEFRAME_CHOICES == ["1分足(暗号のみ)", "5分足", "15分足", "1時間足", "4時間足", "日足"],
          f"got={TIMEFRAME_CHOICES}")
    check("13-0b INTRADAY_INTERVAL_MAPが1m/5m/15mへ正しくマップ",
          INTRADAY_INTERVAL_MAP == {"1分足(暗号のみ)": "1m", "5分足": "5m", "15分足": "15m"})
    check("13-0c INTRADAY_WINDOW_GUARD_DAYSが3/15/45",
          INTRADAY_WINDOW_GUARD_DAYS == {"1分足(暗号のみ)": 3, "5分足": 15, "15分足": 45})

    check("13-1a any_ccxt_selected: BTC(ccxt)含む→True", any_ccxt_selected(["BTC", "GOLD"]) is True)
    check("13-1b any_ccxt_selected: yfinance銘柄のみ→False", any_ccxt_selected(["GOLD", "NQ"]) is False)
    check("13-1c any_ccxt_selected: 空リスト→False", any_ccxt_selected([]) is False)
    check("13-1d any_ccxt_selected: '/'含む任意ティッカーはccxt扱い", any_ccxt_selected(["DOGE/USDT"]) is True)
    check("13-1e any_ccxt_selected: '/'含まない任意ティッカーはyfinance扱い", any_ccxt_selected(["SPY"]) is False)

    eff13a, note13a = resolve_intraday_effective_choice("yfinance", "1分足(暗号のみ)")
    check("13-2a yfinance×1分足→15分足へフォールバック+理由メモ付き",
          eff13a == "15分足" and note13a is not None, f"eff={eff13a} note={note13a}")
    eff13b, note13b = resolve_intraday_effective_choice("ccxt", "1分足(暗号のみ)")
    check("13-2b ccxt×1分足→フォールバックなし(理由メモNone)", eff13b == "1分足(暗号のみ)" and note13b is None)
    eff13c, note13c = resolve_intraday_effective_choice("yfinance", "5分足")
    check("13-2c yfinance×5分足→フォールバックなし(yfinanceも5分足対応)", eff13c == "5分足" and note13c is None)
    eff13d, note13d = resolve_intraday_effective_choice("yfinance", "15分足")
    check("13-2d yfinance×15分足→フォールバックなし", eff13d == "15分足" and note13d is None)

    start13 = date(2026, 1, 1)
    s13a, e13a, t13a = compute_intraday_chart_window(start13, date(2026, 1, 3), "1分足(暗号のみ)")
    check("13-3a 1分足: span=3日(境界ちょうど)は短縮されない",
          (s13a, e13a, t13a) == (start13, date(2026, 1, 3), False))
    end13b = date(2026, 1, 4)
    s13b, e13b, t13b = compute_intraday_chart_window(start13, end13b, "1分足(暗号のみ)")
    check("13-3b 1分足: span=4日は直近3日(guard_days-1=2日前始まり)に短縮",
          t13b is True and s13b == end13b - timedelta(days=2) and e13b == end13b,
          f"s={s13b} e={e13b} t={t13b}")

    start13c = date(2026, 1, 1)
    end13c = date(2026, 1, 15)
    s13c, e13c, t13c = compute_intraday_chart_window(start13c, end13c, "5分足")
    check("13-3c 5分足: span=15日(境界ちょうど)は短縮されない", (s13c, e13c, t13c) == (start13c, end13c, False))
    end13d = date(2026, 1, 16)
    s13d, e13d, t13d = compute_intraday_chart_window(start13c, end13d, "5分足")
    check("13-3d 5分足: span=16日は直近15日に短縮",
          t13d is True and s13d == end13d - timedelta(days=14) and e13d == end13d,
          f"s={s13d} e={e13d} t={t13d}")

    start13e = date(2026, 1, 1)
    end13e = start13e + timedelta(days=44)
    s13e, e13e, t13e = compute_intraday_chart_window(start13e, end13e, "15分足")
    check("13-3e 15分足: span=45日(境界ちょうど)は短縮されない", t13e is False)
    end13f = start13e + timedelta(days=45)
    s13f, e13f, t13f = compute_intraday_chart_window(start13e, end13f, "15分足")
    check("13-3f 15分足: span=46日は直近45日に短縮",
          t13f is True and s13f == end13f - timedelta(days=44) and e13f == end13f,
          f"s={s13f} e={e13f} t={t13f}")

    s13g, e13g, t13g = compute_intraday_chart_window(date(2020, 1, 1), date(2026, 1, 1), "1時間足")
    check("13-3g ガード対象外の足種(1時間足)はどんな期間でも短縮されない",
          (s13g, e13g, t13g) == (date(2020, 1, 1), date(2026, 1, 1), False))

    print("\n--- 14. 追補v4§2.2/2.4B(C3b): トレード選択ラベル+ズーム窓+足種自動選択の純ロジック ---")

    ts_e1 = pd.Timestamp("2026-07-03 14:23:00", tz="Asia/Tokyo")
    ts_x1 = pd.Timestamp("2026-07-03 15:41:00", tz="Asia/Tokyo")
    lbl14a = format_trade_select_label(12, "BTC", "LONG", 10, ts_e1, ts_x1, 120.5)
    check("14-1a format_trade_select_label: 仕様書記載例と一致",
          lbl14a == "#12 BTC LONG 10x 07-03 14:23→15:41 +120.5USD", f"got={lbl14a}")
    lbl14b = format_trade_select_label(3, "ETH", None, None, ts_e1, None, -50.0)
    check("14-1b format_trade_select_label: Side/Leverage/Exit欠落は?/—/(未決済)",
          lbl14b == "#3 ETH ? — 07-03 14:23→(未決済) -50.0USD", f"got={lbl14b}")

    w14a_s, w14a_e = compute_zoom_window(ts_e1, ts_x1)
    check("14-2a compute_zoom_window: exit有り→entry-2h〜exit+2h",
          w14a_s == ts_e1 - pd.Timedelta(hours=2) and w14a_e == ts_x1 + pd.Timedelta(hours=2))
    w14b_s, w14b_e = compute_zoom_window(ts_e1, pd.NaT)
    check("14-2b compute_zoom_window: exit欠落→entry-2h〜entry+2h",
          w14b_s == ts_e1 - pd.Timedelta(hours=2) and w14b_e == ts_e1 + pd.Timedelta(hours=2))

    print("\n--- 15. 追補v5§1: ボラチャート+価格チャート2段構成 ---")

    # 15-1: cumulative_return_pct 既知解((price/base-1)*100・先頭0%)
    s15 = pd.Series([100.0, 110.0, 90.0, 100.0, 50.0])
    r15 = cumulative_return_pct(s15, base=100.0)
    check("15-1a cumulative_return_pct: 既知解[0,10,-10,0,-50]",
          [round(v, 6) for v in r15.tolist()] == [0.0, 10.0, -10.0, 0.0, -50.0], f"got={list(r15)}")
    r15b = cumulative_return_pct(s15, base=0.0)
    check("15-1b cumulative_return_pct: base=0はゼロ割回避で全NaN", r15b.isna().all())
    r15c = cumulative_return_pct(s15, base=float("nan"))
    check("15-1c cumulative_return_pct: base=NaNも全NaN", r15c.isna().all())

    idx15 = pd.date_range("2026-01-01 00:00", periods=3, freq="1h", tz="Asia/Tokyo")
    dfA15 = pd.DataFrame({"open": [100, 105, 95], "high": [110, 110, 100], "low": [95, 100, 90],
                           "close": [100, 110, 90], "volume": [1, 1, 1]}, index=idx15)
    dfB15 = pd.DataFrame({"open": [50000, 51000, 49000], "high": [51000, 52000, 50000],
                           "low": [49000, 50000, 48000], "close": [50000, 52000, 48000],
                           "volume": [1, 1, 1]}, index=idx15)
    normA15, baseA15 = to_cumulative_return_ohlc(dfA15)
    check("15-2a to_cumulative_return_ohlc: 先頭closeは常に0%(銘柄A)", normA15["close"].iloc[0] == 0.0)
    check("15-2b to_cumulative_return_ohlc: 2本目close=110→+10%(銘柄A)",
          abs(normA15["close"].iloc[1] - 10.0) < 1e-9, f"got={normA15['close'].iloc[1]}")
    check("15-2c to_cumulative_return_ohlc: baseは先頭close(100)をそのまま返す", baseA15 == 100.0)
    normB15, baseB15 = to_cumulative_return_ohlc(dfB15)
    check("15-2d to_cumulative_return_ohlc: 桁違いの銘柄Bも独立に先頭close=0%基準",
          normB15["close"].iloc[0] == 0.0 and baseB15 == 50000.0)
    check("15-2e to_cumulative_return_ohlc: 銘柄Bの2本目close=52000→+4%",
          abs(normB15["close"].iloc[1] - 4.0) < 1e-9, f"got={normB15['close'].iloc[1]}")

    # 15-3: build_candlestick_chart の行構成(1段目=ボラ全銘柄重畳・2段目以降=銘柄別価格・独自y軸)
    fig15a, _sk15a, _mc15a = build_candlestick_chart({"A": dfA15, "B": dfB15}, None, False)
    candles15a = [t for t in fig15a.data if t.type == "candlestick"]
    check("15-3a 複数銘柄(2つ): ローソクtrace=ボラ2本+価格2本=4本", len(candles15a) == 4, f"got={len(candles15a)}")
    yaxes15a = sorted({t.yaxis or "y" for t in candles15a})
    check("15-3b 複数銘柄: 3つの独自y軸(vola=y共通・価格2行=y2/y3)を使用",
          yaxes15a == ["y", "y2", "y3"], f"got={yaxes15a}")
    vola15a = [t for t in candles15a if t.yaxis in (None, "y")]
    check("15-3c 複数銘柄: 1段目(y軸y)にA・Bの2本が重畳", len(vola15a) == 2, f"got={len(vola15a)}")
    check("15-3d 複数銘柄: 出来高barトレースは無い(単一銘柄時のみ)",
          len([t for t in fig15a.data if t.type == "bar"]) == 0)
    check("15-3e 複数銘柄: 価格行(y2/y3)はshowlegend=False(凡例重複回避)",
          all(t.showlegend is False for t in candles15a if t.yaxis in ("y2", "y3")))

    fig15b, _sk15b, _mc15b = build_candlestick_chart({"A": dfA15}, None, False)
    candles15b = [t for t in fig15b.data if t.type == "candlestick"]
    bars15b = [t for t in fig15b.data if t.type == "bar"]
    check("15-3f 単一銘柄: ローソクtrace=ボラ1本+価格1本=2本・出来高barが1本",
          len(candles15b) == 2 and len(bars15b) == 1, f"c={len(candles15b)} b={len(bars15b)}")
    check("15-3g 単一銘柄: 出来高barはy3(3段目=vola/価格/出来高)", bars15b[0].yaxis == "y3",
          f"got={bars15b[0].yaxis}")
    check("15-3h 単一銘柄: 高さ=320px×3行=960", fig15b.layout.height == 960, f"got={fig15b.layout.height}")
    check("15-3i 複数銘柄(2つ): 高さ=320px×3行(vola1+価格2)=960", fig15a.layout.height == 960,
          f"got={fig15a.layout.height}")

    # 15-4: セッション背景帯(yref='paper')が行数に依らず1組のみ(全段貫通)
    idx15w = pd.date_range("2026-01-01 00:00", periods=30, freq="1h", tz="Asia/Tokyo")
    dfA15w = pd.DataFrame(
        {"open": range(30), "high": range(30), "low": range(30), "close": range(30), "volume": [1] * 30},
        index=idx15w,
    )
    dfB15w = dfA15w.copy()
    direct_shapes15 = build_session_background_shapes(idx15w.min(), idx15w.max(), 0.2)
    check("15-4a build_session_background_shapes: 30時間窓で複数セグメントに分割される(>1)",
          len(direct_shapes15) > 1, f"n={len(direct_shapes15)}")
    fig15c, _sk15c, _mc15c = build_candlestick_chart({"A": dfA15w}, None, True, bg_opacity=0.2)
    fig15d, _sk15d, _mc15d = build_candlestick_chart({"A": dfA15w, "B": dfB15w}, None, True, bg_opacity=0.2)
    check("15-4b 単一銘柄(3行)のshapes数は直接呼び出しと一致(重複追加なし)",
          len(fig15c.layout.shapes) == len(direct_shapes15), f"got={len(fig15c.layout.shapes)}")
    check("15-4c 複数銘柄(3行)のshapes数も同一(行数を増やしても1組のみ)",
          len(fig15d.layout.shapes) == len(direct_shapes15), f"got={len(fig15d.layout.shapes)}")
    check("15-4d 全shapeがyref='paper'(全段を貫通する塗り)",
          all(s.yref == "paper" for s in fig15d.layout.shapes))

    # -----------------------------------------------------------------
    # 16. 追補v5§2/§3: 縦ライン同期+detail_mode(click/hover)+build_click_detail
    # -----------------------------------------------------------------
    print("\n--- 16. 縦ライン同期+欄外パネル/ホバー切替 ---")
    fig16a, _sk16a, _mc16a = build_candlestick_chart({"A": dfA15, "B": dfB15}, None, False, detail_mode="click")
    overlays16a = [t for t in fig16a.data if t.type == "scatter" and str(t.name).startswith("__click_overlay")]
    check("16-1a click既定: 透明overlayがボラ2+価格2=4本", len(overlays16a) == 4, f"got={len(overlays16a)}")
    check("16-1b overlayはopacity=0.01・hoverinfo=none(skipはon_select無反応バグの根治)",
          all(t.marker.opacity == 0.01 and t.hoverinfo == "none" for t in overlays16a))
    candles16a = [t for t in fig16a.data if t.type == "candlestick"]
    check("16-1c click: 全ローソクtraceがhoverinfo=skip(英語OHLC非表示)",
          all(t.hoverinfo == "skip" for t in candles16a))
    check("16-1d hovermode='x unified'(v6.4: 複数箱の重なり根治)", fig16a.layout.hovermode == "x unified")
    xaxis_names16 = ["xaxis", "xaxis2", "xaxis3"]
    check("16-1e 全xaxis(3行)にshowspikes=True・spikemode=across",
          all(getattr(fig16a.layout, nm).showspikes is True and getattr(fig16a.layout, nm).spikemode == "across"
              for nm in xaxis_names16))
    check("16-1f clickモード: 全ローソクtraceはcustomdataがNone(死荷重ガード)",
          all(t.customdata is None for t in candles16a))

    fig16b, _sk16b, _mc16b = build_candlestick_chart({"A": dfA15}, None, False, detail_mode="hover")
    candles16b = [t for t in fig16b.data if t.type == "candlestick"]
    check("16-2a hoverモード: ローソクtraceはhoverinfo=skipでない", all(t.hoverinfo != "skip" for t in candles16b))
    check("16-2b hoverモード: ボラ行hovertemplateに『累積騰落率』(日本語)", "累積騰落率" in candles16b[0].hovertemplate)
    check("16-2c hoverモード: 価格行hovertemplateに『始値』等(日本語OHLC)",
          all(w in candles16b[1].hovertemplate for w in ["始値", "高値", "安値", "終値"]))
    check("16-2d hoverモード: overlayは追加されない",
          not any(str(t.name).startswith("__click_overlay") for t in fig16b.data))
    bars16b = [t for t in fig16b.data if t.type == "bar"]
    check("16-2e hoverモード: 出来高hovertemplateに『出来高』(日本語)",
          len(bars16b) == 1 and "出来高" in bars16b[0].hovertemplate)
    check("16-2f hoverモード: ボラ行customdataは4列・hovertemplateに『値幅』",
          candles16b[0].customdata is not None and np.asarray(candles16b[0].customdata).shape[1] == 4
          and "値幅" in candles16b[0].hovertemplate)
    check("16-2g hoverモード: 価格行customdataがあり・hovertemplateに『変動幅』",
          candles16b[1].customdata is not None and "変動幅" in candles16b[1].hovertemplate)

    # 16-6: v6.5 _trade_risk_labels(証拠金推定/最大逆行MAE/実績RR)の純ロジック
    idx166 = pd.date_range("2026-03-01 10:00", periods=5, freq="1h", tz="Asia/Tokyo")
    ohlcv166 = pd.DataFrame(
        {"open": [100.0] * 5, "high": [101, 102, 103, 102, 101],
         "low": [99, 98, 99.5, 99.8, 99.9], "close": [100.5] * 5, "volume": [1.0] * 5},
        index=idx166,
    )
    row166 = pd.Series({
        "entry_time": idx166[0], "exit_time": idx166[4], "entry_price": 100.0,
        "side": "LONG", "leverage": 10.0, "pnl_usd": 100.0, "pnl_percent": 1.0,
    })
    mg166, mae166, rr166 = _trade_risk_labels(row166, ohlcv166)
    check("16-6a 証拠金推定=|100USD|/(1%×10x)=1,000 USD", mg166 == "1,000 USD", f"got={mg166}")
    check("16-6b LONGのMAE=期間最安98→-2.00%", mae166 == "-2.00%", f"got={mae166}")
    check("16-6c RR実績=+1.0%÷2.0%=+0.50", rr166 == "+0.50", f"got={rr166}")
    row166s = row166.copy()
    row166s["side"] = "SHORT"
    _, mae166s, _ = _trade_risk_labels(row166s, ohlcv166)
    check("16-6d SHORTのMAE=期間最高103→-3.00%", mae166s == "-3.00%", f"got={mae166s}")
    row166n = row166.copy()
    row166n["pnl_percent"] = 0.0
    mg166n, _, _ = _trade_risk_labels(row166n, ohlcv166)
    check("16-6e PnL0%は証拠金算出不能=—", mg166n == "—", f"got={mg166n}")

    # 16-5: v6.3 チャート自動間引き(描画負荷対策)の純ロジック
    check("16-5a 上限内は間引きなし", _auto_decimation_rule(3000, 1.0) is None)
    check("16-5b 1分足6000本→2minへ間引き", _auto_decimation_rule(6000, 1.0) == "2min")
    check("16-5c 1時間足8760本→180minへ間引き", _auto_decimation_rule(8760, 60.0) == "180min")
    check("16-5d 足間隔不明(0分)は間引きしない", _auto_decimation_rule(10000, 0.0) is None)
    idx165 = pd.date_range("2026-01-01", periods=10, freq="1min", tz="Asia/Tokyo")
    df165 = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx165,
    )
    check("16-5e 足間隔の中央値=1分", abs(_median_bar_minutes(df165) - 1.0) < 1e-9)

    # 16-3: build_click_detail(欄外パネルの純ロジック) — 該当バー・範囲外・欠損
    idx16 = pd.date_range("2026-02-01 00:00", periods=5, freq="1h", tz="Asia/Tokyo")
    df16A = pd.DataFrame(
        {"open": [100, 101, 102, 103, 104], "high": [101, 102, 103, 104, 105],
         "low": [99, 100, 101, 102, 103], "close": [100.5, 101.5, 102.5, 103.5, 104.5],
         "volume": [10, 20, 30, 40, 50]},
        index=idx16,
    )
    data16 = {"A": df16A}
    base16 = {"A": 100.5}

    check("16-3a click_time=Noneは空リスト", build_click_detail(None, data16, base16) == [])

    d16_hit = build_click_detail(idx16[2], data16, base16)
    check("16-3b 該当バー(完全一致): 1件・OHLC一致", len(d16_hit) == 1 and d16_hit[0]["label"] == "A"
          and d16_hit[0]["open"] == 102.0 and d16_hit[0]["close"] == 102.5, f"got={d16_hit}")
    check("16-3c 該当バー: 累積騰落率=(102.5/100.5-1)*100",
          abs(d16_hit[0]["cum_return_pct"] - (102.5 / 100.5 - 1) * 100.0) < 1e-9)

    d16_before = build_click_detail(idx16[0] - pd.Timedelta(hours=2), data16, base16)
    check("16-3d 範囲外(先頭より前)は除外", d16_before == [])
    d16_after = build_click_detail(idx16[-1] + pd.Timedelta(hours=5), data16, base16)
    check("16-3e 範囲外(末尾より後)は除外", d16_after == [])

    d16_gap = build_click_detail(idx16[1] + pd.Timedelta(minutes=30), data16, base16)
    check("16-3f 欠損(バー境界と不一致)は直近の前バーへasof",
          len(d16_gap) == 1 and d16_gap[0]["time"] == idx16[1] and d16_gap[0]["close"] == 101.5, f"got={d16_gap}")

    idx16b = pd.date_range("2026-02-01 10:00", periods=3, freq="1h", tz="Asia/Tokyo")
    df16B = pd.DataFrame(
        {"open": [10, 11, 12], "high": [11, 12, 13], "low": [9, 10, 11],
         "close": [10.5, 11.5, 12.5], "volume": [1, 1, 1]},
        index=idx16b,
    )
    d16_multi = build_click_detail(idx16[2], {"A": df16A, "B": df16B}, {"A": 100.5, "B": 10.5})
    check("16-3g 複数銘柄: 範囲外の銘柄(B)は結果から除外・Aのみ1件",
          len(d16_multi) == 1 and d16_multi[0]["label"] == "A", f"got={[d['label'] for d in d16_multi]}")

    trades16 = pd.DataFrame({
        "entry_time": [idx16[2] + pd.Timedelta(minutes=10)],
        "exit_time": [idx16[3] + pd.Timedelta(minutes=5)],
        "side": ["LONG"], "leverage": [10.0], "pnl_usd": [50.0], "pnl_percent": [2.0],
        "dataset_label": ["弟子"],
    })
    trade_rows16 = {"A": trades16}
    d16_entry = build_click_detail(idx16[2], data16, base16, trade_rows16)
    check("16-3h 該当トレード併記(エントリー側)",
          len(d16_entry[0]["trades"]) == 1 and d16_entry[0]["trades"][0]["is_entry"] is True
          and d16_entry[0]["trades"][0]["is_exit"] is False, f"got={d16_entry[0]['trades']}")
    d16_exit = build_click_detail(idx16[3], data16, base16, trade_rows16)
    check("16-3i 該当トレード併記(決済側)",
          len(d16_exit[0]["trades"]) == 1 and d16_exit[0]["trades"][0]["is_exit"] is True
          and d16_exit[0]["trades"][0]["is_entry"] is False, f"got={d16_exit[0]['trades']}")

    # -----------------------------------------------------------------
    # 17. 追補v5§4: トレードマーカーは価格チャート行のみ・ボラチャート行には描かない
    # -----------------------------------------------------------------
    print("\n--- 17. 追補v5§4: トレードマーカーの行配線(価格チャート行のみ) ---")
    trades17 = pd.DataFrame({
        "Entry_Time": [idx15[1]], "Entry_Price": [np.nan],
        "Exit_Time": [idx15[2]], "Exit_Price": [np.nan],
        "Side": ["LONG"], "Leverage": [10.0], "PnL_USD": [50.0], "PnL_Percent": [2.0],
    })
    overlays17 = {"A": [{"dataset_label": "弟子", "trades_df": trades17, "color": "#00BFFF"}]}
    fig17, _sk17, mc17 = build_candlestick_chart(
        {"A": dfA15, "B": dfB15}, None, False, trade_overlays=overlays17,
    )
    check("17-1a マーカー描画件数=1(A側のみ)", mc17 == 1, f"got={mc17}")
    trade_traces17 = [t for t in fig17.data if str(t.legendgroup or "").startswith("trades_")]
    check("17-1b トレード関連trace(エントリー/決済/接続線)が存在", len(trade_traces17) >= 2,
          f"got={len(trade_traces17)}")
    check("17-1c 全トレードtraceがボラ行(y/None)には無い",
          all(t.yaxis not in (None, "y") for t in trade_traces17),
          f"yaxes={[t.yaxis for t in trade_traces17]}")
    check("17-1d 全トレードtraceがA(先頭の価格行=y2)に配置", all(t.yaxis == "y2" for t in trade_traces17),
          f"yaxes={[t.yaxis for t in trade_traces17]}")
    entry_traces17 = [t for t in trade_traces17 if str(t.name).endswith("エントリー")]
    check("17-1e エントリーtraceが1本・シンボルtriangle-up(LONG)", len(entry_traces17) == 1
          and list(entry_traces17[0].marker.symbol) == ["triangle-up"], f"got={entry_traces17}")

    # -----------------------------------------------------------------
    # 18. 追補v5: サンプルデータ(弟子/師匠)の不変条件+CSVファイルとの完全一致
    # -----------------------------------------------------------------
    print("\n--- 18. 追補v5: サンプルデータ不変条件+CSV同期 ---")
    disc18 = generate_sample_trades()
    mentor18 = generate_sample_trades_mentor()
    check("18-1 弟子49件(内draw1件)", len(disc18) == 49 and (disc18["Win_Loss"] == "draw").sum() == 1)
    check("18-2 師匠56件", len(mentor18) == 56)

    def _band_of18(ts_str: str) -> str:
        return hour_to_zone(int(ts_str[11:13]))

    combined18 = pd.concat([disc18, mentor18], ignore_index=True)
    band_counts18 = combined18["Entry_Time(JST)"].map(_band_of18).value_counts()
    check("18-3 全9詳細帯が合算で3件以上",
          set(BAND_HOURS.keys()) == set(band_counts18.index) and band_counts18.min() >= 3,
          f"{band_counts18.to_dict()}")
    weekdays18 = set(pd.to_datetime(combined18["Entry_Time(JST)"]).dt.weekday)
    check("18-4 全7曜日を合算でカバー", weekdays18 == set(range(7)), f"got={weekdays18}")

    for _label18, _df18, _lo, _hi, _rr_less in (
        ("弟子", disc18, 46.0, 48.0, True), ("師匠", mentor18, 56.0, 58.0, False),
    ):
        wl18 = _df18[_df18["Win_Loss"] != "draw"]
        n_w18 = int((wl18["Win_Loss"] == "win").sum())
        n_l18 = int((wl18["Win_Loss"] == "loss").sum())
        wr18 = n_w18 / (n_w18 + n_l18) * 100.0
        avg_w18 = wl18.loc[wl18["Win_Loss"] == "win", "PnL_Percent"].abs().mean()
        avg_l18 = wl18.loc[wl18["Win_Loss"] == "loss", "PnL_Percent"].abs().mean()
        check(f"18-5 {_label18}勝率{_lo:.0f}-{_hi:.0f}%", _lo <= wr18 <= _hi, f"got={wr18:.2f}")
        rr_ok = (avg_w18 < avg_l18) if _rr_less else (avg_w18 > avg_l18)
        check(f"18-6 {_label18}平均勝ち/平均負けの大小関係", rr_ok, f"win={avg_w18:.3f} loss={avg_l18:.3f}")

    for _fname18, _gen18, _label18b in (
        ("sample_trades.csv", generate_sample_trades, "弟子"),
        ("sample_trades_mentor.csv", generate_sample_trades_mentor, "師匠"),
    ):
        _csv_path18 = Path(__file__).parent / _fname18
        if not _csv_path18.exists():
            check(f"18-7 {_label18b} {_fname18} 存在確認", True, "ファイル無し・比較スキップ")
            continue
        _file_text18 = _csv_path18.read_text(encoding="utf-8").replace("\r\n", "\n")
        _gen_text18 = _gen18().to_csv(index=False).replace("\r\n", "\n")
        check(f"18-7 {_label18b} CSVファイルとto_csv文字列が完全一致",
              _file_text18 == _gen_text18, f"len file={len(_file_text18)} gen={len(_gen_text18)}")

    # 18-8: 追補v7 同局面ペア(同銘柄・エントリー時刻差30分以内)。両生成関数の出力から
    # 貪欲最近傍マッチング(エントリー差が小さい順に1対1対応)で純ロジック計算する
    # (ハードコードのインデックス一覧に依存しない=行を並べ替えても崩れない検査)。
    def _find_same_scene_pairs18(disc_df: pd.DataFrame, mentor_df: pd.DataFrame,
                                  max_entry_diff_min: float = 30.0) -> list[dict]:
        d_entry = pd.to_datetime(disc_df["Entry_Time(JST)"])
        m_entry = pd.to_datetime(mentor_df["Entry_Time(JST)"])
        d_exit = pd.to_datetime(disc_df["Exit_Time(JST)"])
        m_exit = pd.to_datetime(mentor_df["Exit_Time(JST)"])
        used_mentor: set[int] = set()
        found: list[dict] = []
        for di in range(len(disc_df)):
            cands = []
            for mi in range(len(mentor_df)):
                if mi in used_mentor or disc_df["Symbol"].iat[di] != mentor_df["Symbol"].iat[mi]:
                    continue
                ediff = abs((d_entry.iat[di] - m_entry.iat[mi]).total_seconds()) / 60.0
                if ediff <= max_entry_diff_min:
                    cands.append((ediff, mi))
            if not cands:
                continue
            cands.sort(key=lambda c: c[0])
            ediff_best, best_mi = cands[0]
            used_mentor.add(best_mi)
            xdiff = abs((d_exit.iat[di] - m_exit.iat[best_mi]).total_seconds()) / 60.0
            divergent = (disc_df["Win_Loss"].iat[di] != mentor_df["Win_Loss"].iat[best_mi]) or (xdiff >= 30.0)
            found.append({"disc_idx": di, "mentor_idx": best_mi, "entry_diff_min": ediff_best,
                          "exit_diff_min": xdiff, "divergent": divergent})
        return found

    pairs18 = _find_same_scene_pairs18(disc18, mentor18)
    check("18-8 同局面ペア(同銘柄・エントリー差30分以内)が10組以上", len(pairs18) >= 10,
          f"n={len(pairs18)}")
    ndiv18 = sum(1 for p in pairs18 if p["divergent"])
    check("18-8b 同局面ペアのうち結果/決済時刻が分かれるものが6組以上", ndiv18 >= 6,
          f"divergent={ndiv18}/{len(pairs18)}")

    print("\n" + "=" * 78)
    if all_ok:
        print("SELFTEST: ALL PASS")
    else:
        print("SELFTEST: FAILED")
        for d in fail_details:
            print(f"  - {d}")
    print("=" * 78)
    return all_ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        ok = run_selftest()
        sys.exit(0 if ok else 1)
    main()
