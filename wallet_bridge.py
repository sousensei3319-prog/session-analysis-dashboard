#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallet_bridge.py — Hyperliquid追跡ウォレット約定履歴 → Session Analysis Dashboard用トレードCSV 橋渡しツール

【目的】
whale_radarプロジェクト等で発掘したHyperliquidの大口ウォレットのアドレスを入力に、Hyperliquid公開API
(userFillsByTime・認証不要)から個別約定(fills)を取得し、(coin, Long/Short)単位のポジション会計で
往復トレード(1トレード=1回のClose)へ再構築、session_dashboard/app.py が読めるトレードCSVへ変換する。

【使い方(1行)】
    python wallet_bridge.py --days 30 --top 5

【CLI】
    python wallet_bridge.py [--addrs 0xA,0xB | --file path.json] [--top N(既定10)]
                            [--min-trades 20] [--days 365] [--out wallet_trades] [--selftest]

    --addrs         カンマ区切りのアドレス直接指定(指定時は既定ファイルより優先)
    --file          ウォレットリストJSON(wallet_bridge_whales.json互換の{"whales":[{"addr":...,"tag":...}]}構造)
                    省略時はこのスクリプトと同じディレクトリの wallet_bridge_whales.json を使う
    --top           直近--days日間の合計closedPnl降順で上位N件だけCSV化する(既定10)
    --min-trades    上位N選定の対象に含める最低トレード数(既定20)。未満のウォレットもindex.jsonには残る
    --days          何日分のfillsを遡って取得するか(既定365)
    --out           出力ディレクトリ(既定 wallet_trades)
    --selftest      ネットワークを一切使わず、合成データで再構築ロジック・ページング・列名互換性を検算する

【重要な仕様上の注意 — ページング方向についての実証結果(推測ではなく実APIで確認済み)】
    設計時に想定されていた「1回最大2000件→endTimeを最古fillのtime-1にして遡る」という後方(過去方向)
    ページング仕様は、実際にHyperliquid公開APIを叩いて検証した結果、実挙動と一致しないことを確認した
    (CLAUDE.md 掟8「推測で進めない」に基づき実物確認を優先)。
    実際のuserFillsByTimeは指定startTime以降のfillsを「古い→新しい」の昇順で返し、2000件の上限に達した
    場合は「もっと新しいfillsがまだ残っている」ことを意味する。したがって本ツールは
        次のstartTime = 直前ページで返ってきた最新fillのtime + 1 (endTimeは固定)
    という「前方(未来方向)」ページングを実装している。これは2026-07-22に実ウォレット3件・複数レンジで
    実測し、取得件数・時系列の連続性を確認済み(下記smoke testログ参照)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import requests

# =====================================================================================
# 定数
# =====================================================================================

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
PAGE_LIMIT = 2000  # userFillsByTime 1回あたりの最大件数(Hyperliquid仕様)
DEFAULT_SLEEP_SEC = 0.8  # レート配慮: ウォレット間の呼び出し間隔(2026-07-22: 実測で後半ウォレットが429で
# スキップされたため0.35→0.8へ引き上げ。あわせて_default_postに429/5xxの指数バックオフを追加=下記参照)
DEFAULT_MAX_PAGES = 2000  # 1ウォレットあたりのページング安全上限(暴走防止。実務上ここまで到達しない想定)
RETRY_BACKOFF_SEC: tuple[float, ...] = (2.0, 4.0, 8.0)  # 429/5xx時の指数バックオフ(最大3リトライ=計4試行)

JST = timezone(timedelta(hours=9))
DEFAULT_WHALES_FILENAME = "wallet_bridge_whales.json"

# ダッシュボード(app.py)へ渡すCSVの列順(タスク仕様どおり)。Leverageは常に空欄
# (HLのfillsにはレバレッジ情報が無いため不明。ダッシュボード側は任意列として扱える=app.py確認済み)。
TRADE_COLUMNS = [
    "Entry_Time(JST)", "Exit_Time(JST)", "Symbol", "PnL_USD", "PnL_Percent",
    "Win_Loss", "Side", "Leverage", "Entry_Price", "Exit_Price", "Quantity",
]

# app.py L5571 (2026-07-22時点でReadして確認): TRADER_STYLE_THRESHOLDS_H = (0.5, 24.0)
# 「中央値保有時間 <0.5h=スキャルピング / 0.5〜24h=デイトレード / >24h=スイング」の閾値。
# 単体実行性(依存最小化)を優先するため app.py はimportせず、同じ値をここへ複製している。
# app.py側でこの値が変更された場合はここも追随して更新すること(重複管理の既知トレードオフ)。
TRADER_STYLE_THRESHOLDS_H = (0.5, 24.0)

# app.py L380-406 (2026-07-22時点でReadして転記): _ALIASES_RAW のうち、本ツールが出力する列に
# 対応する部分のみを複製した互換性検証用サブセット。app.py はimportしない方針のため、
# 生成したCSVの列名がダッシュボードの列エイリアスに引っかかることを、app.py と同一の正規化規則
# (_norm_colname_key: 小文字化+括弧/空白/アンダースコア/ハイフン/コロン除去)で自己検算する。
# ※app.py側で_ALIASES_RAWが変更された場合はこのサブセットも手動追随が必要(README注記済み)。
_ALIASES_RAW_SUBSET: dict[str, list[str]] = {
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
    "Side": ["side", "方向", "ポジション", "position", "l/s", "ls", "buy/sell", "buysell"],
    "Leverage": ["leverage", "レバレッジ", "レバ", "lev", "倍率"],
    "Entry_Price": ["entry_price", "entryprice", "entry price", "エントリー価格", "建値", "購入価格"],
    "Exit_Price": ["exit_price", "exitprice", "exit price", "決済価格", "エグジット価格", "売却価格"],
    "Quantity": ["quantity", "qty", "数量", "約定数量", "成交数量"],
}


def _norm_colname_key(s: str) -> str:
    """app.py L547-551 _norm_colname_key と同一ロジック(手動複製・importしない方針のため)。"""
    s = str(s).strip().lower()
    for ch in ["(", ")", "（", "）", " ", "_", "-", ":", "：", "　"]:
        s = s.replace(ch, "")
    return s


def _build_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canon, alist in _ALIASES_RAW_SUBSET.items():
        for a in alist:
            lookup[_norm_colname_key(a)] = canon
        lookup[_norm_colname_key(canon)] = canon
    return lookup


_ALIAS_LOOKUP_LOCAL = _build_alias_lookup()


def verify_dashboard_column_compat(columns: list[str]) -> tuple[bool, list[str]]:
    """生成CSVの列名がapp.pyの列エイリアス(_ALIASES_RAW)で認識可能かを自己検算する。
    戻り値: (全列OKか, 認識できなかった列名のリスト)。"""
    bad: list[str] = []
    for c in columns:
        key = _norm_colname_key(c)
        if key not in _ALIAS_LOOKUP_LOCAL:
            bad.append(c)
    return (len(bad) == 0, bad)


# =====================================================================================
# 時刻変換
# =====================================================================================


def ms_to_jst_str(ms: float) -> str:
    """Hyperliquidのtime(ms・UTC)をJST(+9h)の"%Y-%m-%d %H:%M:%S"文字列へ変換する。"""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _safe_filename_part(s: Any) -> str:
    """ファイル名に使えない文字を'_'へ置換する(tag・addr接頭の安全化)。"""
    s = str(s) if s is not None else ""
    s = re.sub(r"[^0-9A-Za-z_\-]", "_", s)
    return s or "unknown"


# =====================================================================================
# Hyperliquid公開API取得(userFillsByTime・認証不要)
# =====================================================================================


def _http_post_with_retry(
    body: dict,
    request_fn: Optional[Callable[[dict], Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    backoff_sec: tuple[float, ...] = RETRY_BACKOFF_SEC,
) -> list:
    """requests.postを429/5xxで指数バックオフ再試行するラッパー(2026-07-22追加)。

    実測でHL公開APIが429を返し、後半ウォレットのfills取得がまるごとスキップされる事象が
    あったための対策。backoff_sec=(2,4,8)の順に待ってから再試行し、最大3リトライ(初回込み計4試行)。
    応答にRetry-Afterヘッダがあれば、そちらの秒数を優先して待つ(ヘッダ尊重)。

    request_fn(body)->レスポンスオブジェクト(.status_code/.headers/.json()/.raise_for_status()を
    持つ)を注入可能。--selftestはこれを合成応答(429→200等)に差し替えてネットワーク無しで検証する。
    sleep_fnも注入可能(--selftestではtime.sleepを実際には呼ばず即時進行させるためのフック)。
    """
    def _real_request(b: dict):
        return requests.post(HL_INFO_URL, json=b, timeout=20)

    req = request_fn if request_fn is not None else _real_request
    resp = None
    for attempt in range(len(backoff_sec) + 1):
        resp = req(body)
        status = getattr(resp, "status_code", 200)
        if status == 429 or status >= 500:
            if attempt >= len(backoff_sec):
                break  # リトライ上限到達。ループを抜けて最後にraise_for_statusさせる
            wait_sec = backoff_sec[attempt]
            retry_after = getattr(resp, "headers", {}).get("Retry-After") if hasattr(resp, "headers") else None
            if retry_after:
                try:
                    wait_sec = float(retry_after)
                except (TypeError, ValueError):
                    pass
            sleep_fn(wait_sec)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"userFillsByTimeの応答が想定外の型です: {type(data)}")
        return data
    # リトライ上限到達: 最後の応答で例外化(429/5xxならHTTPErrorが飛ぶ)
    resp.raise_for_status()
    raise RuntimeError("userFillsByTime: リトライ上限到達も応答が異常系ではありませんでした")  # pragma: no cover


def _default_post(body: dict) -> list:
    """HL公開APIへPOSTしJSONを返す既定の通信関数(--selftestでは呼ばれない=post_fn注入で差し替え)。
    429/5xx時は_http_post_with_retryが指数バックオフで自動リトライする。"""
    return _http_post_with_retry(body)


def fetch_user_fills_by_time(
    addr: str,
    start_ms: int,
    end_ms: int,
    post_fn: Optional[Callable[[dict], list]] = None,
    sleep_sec: float = DEFAULT_SLEEP_SEC,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[dict]:
    """userFillsByTimeを前方(未来方向)ページングで全件取得する。

    実装メモ(モジュールdocstring参照): 実測の結果、Hyperliquid側は指定startTime以降のfillsを
    古い→新しい昇順で返し、ページ上限(2000件)ちょうど返ってきた場合は続きがあることを意味する。
    そのため次のstartTimeを「直前ページ内の最新fillのtime + 1」に進めて再取得する(endTimeは固定)。
    ページが2000件未満(または0件)なら、それが最後のページ=既にendTimeまで到達したとみなして終了する。

    post_fn(body: dict) -> list[dict] を注入可能。--selftestはこれを合成データに差し替えることで
    ネットワーク無しでページング境界ロジックを検証する。
    """
    poster = post_fn if post_fn is not None else _default_post
    all_fills: dict[Any, dict] = {}  # 重複排除キー(tid優先)→fill
    cur_start = start_ms
    pages = 0
    while pages < max_pages:
        if cur_start > end_ms:
            break
        body = {"type": "userFillsByTime", "user": addr, "startTime": cur_start, "endTime": end_ms}
        page = poster(body)
        pages += 1
        if post_fn is None:
            time.sleep(sleep_sec)
        if not page:
            break
        newest_t: Optional[int] = None
        for f in page:
            t = int(f.get("time", 0))
            newest_t = t if newest_t is None else max(newest_t, t)
            key = f.get("tid")
            if key is None:
                key = (f.get("oid"), f.get("time"), f.get("px"), f.get("sz"), f.get("dir"))
            all_fills[key] = f
        if len(page) < PAGE_LIMIT:
            break  # 最後のページ(これ以上先は無い)
        if newest_t is None or newest_t + 1 <= cur_start:
            break  # 安全弁: 進捗が無い場合は無限ループを避けて打ち切る
        cur_start = newest_t + 1
    return list(all_fills.values())


# =====================================================================================
# 往復トレード再構築 — (coin, Long/Short)単位のポジション会計
# =====================================================================================


def reconstruct_wallet_fills(fills: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Hyperliquidのfillsリスト→往復トレードリストへ再構築する(BingX注文履歴再構築と同じ考え方)。

    ポジション会計: (coin, Long/Short)単位で、Open系fillにより数量・加重平均建値・建玉時刻を集約する
    (数量がゼロの状態からの最初のOpenで建玉時刻をリセットする。以降追加のOpenは時刻を更新しない)。
    Close系fillが来るたびに1トレードを発行する(部分決済は複数トレードになる=各実現イベントが1トレード)。
    エントリー=集約平均建値/建玉時刻、エグジット=当該Closeの約定価格/時刻。

    dirフィールドを正とする(side+startPositionからの推定はしない)。
    - dirが無い(欠落・空)fillはスキップし、skip_counts["missing_dir"]へ計上する。
    - dirが"Open Long"/"Open Short"/"Close Long"/"Close Short"のいずれでもないfill
      (例: フリップ"Long > Short"、現物の"Buy"/"Sell"等)はスキップし、
      skip_counts["unrecognized_dir"]へ計上する(現状フリップ1発注同時ドテンには非対応。README参照)。
    - 建玉が無い状態でのClose(orphan=このツールの取得ウィンドウより前に建玉されたポジションの決済等)は
      エントリー情報が無いため出力せずスキップし、skip_counts["orphan_close"]へ計上する。
    - px/sz/timeの数値変換に失敗したfillはskip_counts["bad_fields"]へ計上してスキップする。

    戻り値: (trades: TRADE_COLUMNS相当のdictのリスト, skip_counts: dict[str,int])
    """
    skip_counts = {"missing_dir": 0, "unrecognized_dir": 0, "orphan_close": 0, "bad_fields": 0}
    # 時刻昇順で処理する(ページ結合後は順序が保証されないため必ずソートする)
    def _fill_time(f: dict) -> float:
        try:
            return float(f.get("time", 0))
        except (TypeError, ValueError):
            return 0.0

    sorted_fills = sorted(fills, key=_fill_time)

    pos: dict[tuple[str, str], dict[str, Any]] = {}
    trades: list[dict] = []

    for f in sorted_fills:
        dir_raw = f.get("dir")
        if not dir_raw or not str(dir_raw).strip():
            skip_counts["missing_dir"] += 1
            continue
        parts = str(dir_raw).strip().split()
        if len(parts) != 2 or parts[0] not in ("Open", "Close") or parts[1] not in ("Long", "Short"):
            skip_counts["unrecognized_dir"] += 1
            continue
        act, side = parts[0], parts[1]  # side: "Long"/"Short"

        try:
            coin = str(f["coin"])
            sz = float(f["sz"])
            px = float(f["px"])
            t = float(f["time"])
        except (KeyError, TypeError, ValueError):
            skip_counts["bad_fields"] += 1
            continue
        try:
            closed_pnl = float(f.get("closedPnl", 0) or 0)
        except (TypeError, ValueError):
            closed_pnl = 0.0

        key = (coin, side)

        if act == "Open":
            st = pos.setdefault(key, {"qty": 0.0, "cost": 0.0, "open_time": t})
            if st["qty"] <= 1e-12:
                st["open_time"] = t  # 数量ゼロからの最初のOpenで建玉時刻リセット
            st["qty"] += sz
            st["cost"] += sz * px
            continue

        # act == "Close"
        st = pos.get(key)
        if st is None or st["qty"] <= 1e-12:
            skip_counts["orphan_close"] += 1
            continue

        avg = st["cost"] / st["qty"]
        cq = min(sz, st["qty"])
        sgn = 1.0 if side == "Long" else -1.0
        pnl_pct = (px / avg - 1.0) * 100.0 * sgn if avg else None
        if closed_pnl > 0:
            win_loss = "win"
        elif closed_pnl < 0:
            win_loss = "loss"
        else:
            win_loss = "draw"

        trades.append({
            "Entry_Time(JST)": ms_to_jst_str(st["open_time"]),
            "Exit_Time(JST)": ms_to_jst_str(t),
            "Symbol": coin,
            "PnL_USD": closed_pnl,
            "PnL_Percent": pnl_pct,
            "Win_Loss": win_loss,
            "Side": "LONG" if side == "Long" else "SHORT",
            "Leverage": None,  # HL fillsからは不明(README注記)
            "Entry_Price": avg,
            "Exit_Price": px,
            "Quantity": cq,
        })

        st["qty"] -= cq
        st["cost"] -= cq * avg
        if st["qty"] <= 1e-12:
            st["qty"] = 0.0
            st["cost"] = 0.0

    return trades, skip_counts


# =====================================================================================
# サマリー算出
# =====================================================================================


def summarize_trades(trades: list[dict]) -> dict[str, Any]:
    """トレードリストからウォレット単位のサマリー指標を算出する。
    分類閾値はTRADER_STYLE_THRESHOLDS_H(=app.py L5571 TRADER_STYLE_THRESHOLDS_Hと同値。上部コメント参照)。
    """
    empty = {
        "n_trades": 0, "total_pnl": 0.0, "win_rate": float("nan"),
        "median_hold_h": float("nan"), "style": "不明",
        "period_start": None, "period_end": None,
    }
    if not trades:
        return empty

    df = pd.DataFrame(trades)
    entry = pd.to_datetime(df["Entry_Time(JST)"], format="%Y-%m-%d %H:%M:%S")
    exit_ = pd.to_datetime(df["Exit_Time(JST)"], format="%Y-%m-%d %H:%M:%S")
    hold_h = (exit_ - entry).dt.total_seconds() / 3600.0
    hold_h = hold_h[hold_h.notna() & (hold_h >= 0)]
    median_h = float(hold_h.median()) if len(hold_h) else float("nan")

    lo, hi = TRADER_STYLE_THRESHOLDS_H
    if pd.isna(median_h):
        style = "不明"
    elif median_h < lo:
        style = "スキャルピング"
    elif median_h < hi:
        style = "デイトレード"
    else:
        style = "スイング"

    wins = int((df["Win_Loss"] == "win").sum())
    losses = int((df["Win_Loss"] == "loss").sum())
    decided = wins + losses
    win_rate = (100.0 * wins / decided) if decided > 0 else float("nan")

    return {
        "n_trades": int(len(df)),
        "total_pnl": float(pd.to_numeric(df["PnL_USD"], errors="coerce").sum()),
        "win_rate": win_rate,
        "median_hold_h": median_h,
        "style": style,
        "period_start": entry.min().strftime("%Y-%m-%d %H:%M:%S") if len(entry) else None,
        "period_end": exit_.max().strftime("%Y-%m-%d %H:%M:%S") if len(exit_) else None,
    }


# =====================================================================================
# ウォレットリスト読み込み
# =====================================================================================


def load_whale_specs(addrs_arg: Optional[str], file_arg: Optional[str], script_dir: Path) -> list[dict]:
    """--addrs / --file / 既定wallet_bridge_whales.json の優先順で[{"addr","tag"}, ...]を返す。"""
    if addrs_arg:
        out = []
        for a in addrs_arg.split(","):
            a = a.strip()
            if a:
                out.append({"addr": a, "tag": "manual"})
        return out

    path = Path(file_arg) if file_arg else (script_dir / DEFAULT_WHALES_FILENAME)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    whales = data.get("whales", [])
    out = []
    for w in whales:
        addr = w.get("addr")
        if not addr:
            continue
        out.append({"addr": str(addr), "tag": str(w.get("tag", "") or "")})
    return out


# =====================================================================================
# メイン処理パイプライン(1ウォレット分)
# =====================================================================================


def process_one_wallet(addr: str, tag: str, start_ms: int, end_ms: int,
                        sleep_sec: float = DEFAULT_SLEEP_SEC) -> dict[str, Any]:
    """1ウォレット分のfills取得→再構築→サマリー算出。失敗時は例外を投げる(呼び出し側でcatchして継続)。"""
    fills = fetch_user_fills_by_time(addr, start_ms, end_ms, sleep_sec=sleep_sec)
    trades, skip_counts = reconstruct_wallet_fills(fills)
    summary = summarize_trades(trades)
    return {
        "addr": addr, "tag": tag, "n_fills_fetched": len(fills),
        "trades": trades, "skip_counts": skip_counts, "summary": summary,
    }


def run_pipeline(args: argparse.Namespace, script_dir: Path) -> int:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(args.days) * 86_400_000

    period_start_str = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    period_end_str = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"集計期間: {period_start_str} 〜 {period_end_str} JST(過去{args.days}日)")

    try:
        specs = load_whale_specs(args.addrs, args.file, script_dir)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[FATAL] ウォレットリストの読み込みに失敗しました: {e}", file=sys.stderr)
        return 1
    if not specs:
        print("[FATAL] ウォレットリストが空です。", file=sys.stderr)
        return 1
    print(f"対象ウォレット数: {len(specs)}")

    results: list[dict[str, Any]] = []
    for i, spec in enumerate(specs, 1):
        addr, tag = spec["addr"], spec["tag"]
        print(f"[{i}/{len(specs)}] {tag:<16s} {addr} を取得中...", file=sys.stderr)
        try:
            r = process_one_wallet(addr, tag, start_ms, now_ms, sleep_sec=DEFAULT_SLEEP_SEC)
        except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
            print(f"  [WARN] 取得失敗のためスキップ: {addr} ({e!r})", file=sys.stderr)
            continue
        results.append(r)

    if not results:
        print("[WARN] 全ウォレットの取得に失敗しました。index.jsonは作成されません。", file=sys.stderr)
        return 1

    # --- index.json (全candidate) ---
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_entries = []
    for r in results:
        s = r["summary"]
        index_entries.append({
            "addr": r["addr"], "tag": r["tag"],
            "n_trades": s["n_trades"], "total_pnl": s["total_pnl"], "win_rate": s["win_rate"],
            "median_hold_h": s["median_hold_h"], "style": s["style"],
            "period_start": s["period_start"], "period_end": s["period_end"],
            "skipped_orphans": r["skip_counts"]["orphan_close"],
            # 追加の透明性情報(タスク必須項目に加えて有用な内訳を残す)
            "skipped_missing_dir": r["skip_counts"]["missing_dir"],
            "skipped_unrecognized_dir": r["skip_counts"]["unrecognized_dir"],
            "skipped_bad_fields": r["skip_counts"]["bad_fields"],
            "n_fills_fetched": r["n_fills_fetched"],
        })
    index_path = out_dir / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "period_start_jst": period_start_str, "period_end_jst": period_end_str,
            "days": args.days, "wallets": index_entries,
        }, f, ensure_ascii=False, indent=2)

    # --- top N選定(min-trades以上・合計closedPnl降順) ---
    eligible = [r for r in results if r["summary"]["n_trades"] >= args.min_trades]
    ranked = sorted(eligible, key=lambda r: r["summary"]["total_pnl"], reverse=True)
    selected = ranked[: args.top]
    selected_addrs = {r["addr"] for r in selected}

    for r in selected:
        fname = f"{_safe_filename_part(r['tag'])}_{_safe_filename_part(r['addr'][:8])}.csv"
        fpath = out_dir / fname
        df = pd.DataFrame(r["trades"], columns=TRADE_COLUMNS)
        df.to_csv(fpath, index=False, encoding="utf-8-sig")
        r["csv_path"] = str(fpath)
        # ダッシュボードの列エイリアスとの互換性を実出力ファイルで自己検算(タスク検証項目3)
        back = pd.read_csv(fpath, encoding="utf-8-sig")
        ok, bad = verify_dashboard_column_compat(list(back.columns))
        if not ok:
            print(f"  [WARN] {fname}: ダッシュボード列エイリアス非適合列 -> {bad}", file=sys.stderr)
        else:
            print(f"  [OK] {fname}: 全列がダッシュボードの列エイリアスと適合({len(back)}行)", file=sys.stderr)

    # --- 実行サマリを表で標準出力 ---
    table_rows = []
    for r in results:
        s = r["summary"]
        table_rows.append({
            "tag": r["tag"], "addr": r["addr"][:10] + "…",
            "n_trades": s["n_trades"],
            "total_pnl": round(s["total_pnl"], 2),
            "win_rate_%": (round(s["win_rate"], 1) if pd.notna(s["win_rate"]) else None),
            "median_hold_h": (round(s["median_hold_h"], 2) if pd.notna(s["median_hold_h"]) else None),
            "style": s["style"],
            "skipped_orphans": r["skip_counts"]["orphan_close"],
            "csv化": "○" if r["addr"] in selected_addrs else "",
        })
    summary_df = pd.DataFrame(table_rows).sort_values("total_pnl", ascending=False)
    print()
    print(f"=== 実行サマリ(集計期間: {period_start_str} 〜 {period_end_str} JST・過去{args.days}日) ===")
    print(summary_df.to_string(index=False))
    print()
    print(f"CSV化: {len(selected)}件 → {out_dir}/  (min_trades>={args.min_trades} かつ closedPnl上位{args.top})")
    print(f"index.json: {index_path} (全{len(results)}ウォレット分のサマリー)")

    return 0


# =====================================================================================
# --selftest: ネットワーク無し・合成fillsによる検算
# =====================================================================================


def _mk_fill(coin: str, dir_: Optional[str], sz: float, px: float, t_ms: float,
             closed_pnl: float = 0.0, **extra) -> dict:
    d = {"coin": coin, "sz": sz, "px": px, "time": t_ms, "dir": dir_, "closedPnl": closed_pnl,
         "side": "B", "oid": 1, "tid": None}
    d.update(extra)
    return d


def _check(name: str, cond: bool, results: list[tuple[str, bool]]) -> None:
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def run_selftests() -> bool:
    results: list[tuple[str, bool]] = []
    print("=== wallet_bridge.py --selftest (ネットワーク不使用・合成データ検算) ===")

    # --- 1. Long: Open x2(加重平均建値) → 部分Close → 全Close ---
    T0, T1, T2, T3 = 1_700_000_000_000, 1_700_000_060_000, 1_700_000_120_000, 1_700_000_180_000
    fills_long = [
        _mk_fill("BTC", "Open Long", 1.0, 100.0, T0, tid=1),
        _mk_fill("BTC", "Open Long", 1.0, 110.0, T1, tid=2),
        _mk_fill("BTC", "Close Long", 1.0, 120.0, T2, closed_pnl=15.0, tid=3),
        _mk_fill("BTC", "Close Long", 1.0, 90.0, T3, closed_pnl=-15.0, tid=4),
    ]
    trades, skips = reconstruct_wallet_fills(fills_long)
    exp_avg = (1.0 * 100.0 + 1.0 * 110.0) / 2.0  # =105.0
    _check("1a Long: 2トレード発行(部分Close+全Close)", len(trades) == 2, results)
    if len(trades) == 2:
        t1_, t2_ = trades[0], trades[1]
        _check("1b Long: 両方のEntry_Priceが加重平均建値(105.0)", abs(t1_["Entry_Price"] - exp_avg) < 1e-9 and abs(t2_["Entry_Price"] - exp_avg) < 1e-9, results)
        _check("1c Long: 両方のEntry_Timeが最初のOpen時刻(建玉時刻リセット=最初のみ)",
               t1_["Entry_Time(JST)"] == ms_to_jst_str(T0) and t2_["Entry_Time(JST)"] == ms_to_jst_str(T0), results)
        _check("1d Long: 部分Close Quantity=min(sz,残玉)=1.0", abs(t1_["Quantity"] - 1.0) < 1e-9, results)
        exp_pct1 = (120.0 / exp_avg - 1.0) * 100.0
        _check("1e Long: PnL_Percent=(Exit/Entry-1)*100(符号+1)", abs(t1_["PnL_Percent"] - exp_pct1) < 1e-9, results)
        _check("1f Long: Win_Loss(closedPnl>0→win, <0→loss)", t1_["Win_Loss"] == "win" and t2_["Win_Loss"] == "loss", results)
        _check("1g Long: Side=LONG", t1_["Side"] == "LONG" and t2_["Side"] == "LONG", results)
        _check("1h Long: Leverage列は常に空欄(None)", t1_["Leverage"] is None and t2_["Leverage"] is None, results)
    _check("1i Long: skip_countsは全てゼロ(正常系)", all(v == 0 for v in skips.values()), results)

    # --- 2. Short対称(価格下落で利益・PnL_Percent符号反転) ---
    fills_short = [
        _mk_fill("ETH", "Open Short", 2.0, 50.0, T0, tid=10),
        _mk_fill("ETH", "Close Short", 1.0, 40.0, T1, closed_pnl=10.0, tid=11),
        _mk_fill("ETH", "Close Short", 1.0, 60.0, T2, closed_pnl=-10.0, tid=12),
    ]
    trades_s, skips_s = reconstruct_wallet_fills(fills_short)
    _check("2a Short: 2トレード発行", len(trades_s) == 2, results)
    if len(trades_s) == 2:
        s1, s2 = trades_s[0], trades_s[1]
        _check("2b Short: Entry_Price=50.0(単一Openの建値)", abs(s1["Entry_Price"] - 50.0) < 1e-9, results)
        exp_pct_s1 = (40.0 / 50.0 - 1.0) * 100.0 * -1.0  # 価格下落=Short利益 → 正の%
        _check("2c Short: PnL_Percentは符号反転(価格下落→正)", abs(s1["PnL_Percent"] - exp_pct_s1) < 1e-9 and s1["PnL_Percent"] > 0, results)
        _check("2d Short: Side=SHORT", s1["Side"] == "SHORT" and s2["Side"] == "SHORT", results)
        _check("2e Short: Win_Loss", s1["Win_Loss"] == "win" and s2["Win_Loss"] == "loss", results)

    # --- 3. orphan Close(建玉なしでのClose)・missing dir・unrecognized dir ---
    fills_orphan = [
        _mk_fill("SOL", "Close Long", 1.0, 100.0, T0, closed_pnl=5.0, tid=20),  # orphan(建玉無し)
        _mk_fill("SOL", None, 1.0, 100.0, T1, closed_pnl=0.0, tid=21),          # dir欠落
        _mk_fill("SOL", "Long > Short", 1.0, 100.0, T2, closed_pnl=0.0, tid=22),  # フリップ=非対応
        _mk_fill("SOL", "Buy", 1.0, 100.0, T3, closed_pnl=0.0, tid=23),         # 現物buy=非対応
    ]
    trades_o, skips_o = reconstruct_wallet_fills(fills_orphan)
    _check("3a orphan Close: トレード出力なし(0件)", len(trades_o) == 0, results)
    _check("3b orphan Close: skip_counts['orphan_close']==1", skips_o["orphan_close"] == 1, results)
    _check("3c dir欠落: skip_counts['missing_dir']==1", skips_o["missing_dir"] == 1, results)
    _check("3d 未対応dir(フリップ+現物buy): skip_counts['unrecognized_dir']==2", skips_o["unrecognized_dir"] == 2, results)

    # --- 4. ページング境界(前方ページング。ネットワーク無し・post_fn注入で検証) ---
    call_log: list[dict] = []
    PAGE1_N = 2000
    page1 = [_mk_fill("BTC", "Open Long", 1.0, 100.0, T0 + i * 1000, tid=1000 + i) for i in range(PAGE1_N)]
    page2 = [_mk_fill("BTC", "Open Long", 1.0, 100.0, T0 + (PAGE1_N + i) * 1000, tid=5000 + i) for i in range(300)]

    def fake_post(body: dict) -> list:
        call_log.append(dict(body))
        if len(call_log) == 1:
            return page1
        elif len(call_log) == 2:
            return page2
        return []

    fetched = fetch_user_fills_by_time("0xTEST", T0, T0 + 10_000_000, post_fn=fake_post)
    _check("4a ページング: 呼び出し回数=2(2000件フル→300件で終了)", len(call_log) == 2, results)
    _check("4b ページング: 合計取得件数=2300(2000+300)", len(fetched) == 2300, results)
    if len(call_log) == 2:
        expected_next_start = max(f["time"] for f in page1) + 1
        _check("4c ページング: 2回目のstartTimeは1回目最新fillのtime+1(前方ページング)",
               call_log[1]["startTime"] == expected_next_start, results)
        _check("4d ページング: endTimeは固定のまま", call_log[0]["endTime"] == call_log[1]["endTime"], results)

    # --- 5. ダッシュボード列互換性(実CSV列名の自己検算) ---
    ok_cols, bad_cols = verify_dashboard_column_compat(TRADE_COLUMNS)
    _check(f"5a TRADE_COLUMNS全列がapp.py列エイリアスと適合(bad={bad_cols})", ok_cols, results)

    # --- 6. サマリー算出(トレーダータイプ分類) ---
    scalp_trades = [{
        "Entry_Time(JST)": "2026-01-01 00:00:00", "Exit_Time(JST)": "2026-01-01 00:10:00",
        "Symbol": "BTC", "PnL_USD": 10.0, "PnL_Percent": 1.0, "Win_Loss": "win", "Side": "LONG",
        "Leverage": None, "Entry_Price": 100.0, "Exit_Price": 101.0, "Quantity": 1.0,
    }]
    swing_trades = [{
        "Entry_Time(JST)": "2026-01-01 00:00:00", "Exit_Time(JST)": "2026-01-03 00:00:00",
        "Symbol": "BTC", "PnL_USD": -5.0, "PnL_Percent": -1.0, "Win_Loss": "loss", "Side": "SHORT",
        "Leverage": None, "Entry_Price": 100.0, "Exit_Price": 101.0, "Quantity": 1.0,
    }]
    s_scalp = summarize_trades(scalp_trades)
    s_swing = summarize_trades(swing_trades)
    _check("6a 中央値保有10分→スキャルピング分類(<0.5h)", s_scalp["style"] == "スキャルピング", results)
    _check("6b 中央値保有48h→スイング分類(>24h)", s_swing["style"] == "スイング", results)
    _check("6c summarize_trades: 空リストはn_trades=0", summarize_trades([])["n_trades"] == 0, results)

    # --- 7. 429/5xx指数バックオフ再試行(ネットワーク無し・request_fn/sleep_fn注入で検証) ---
    class _FakeResp:
        def __init__(self, status_code: int, json_data: Any = None, headers: Optional[dict] = None):
            self.status_code = status_code
            self._json_data = json_data
            self.headers = headers or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise requests.HTTPError(f"status={self.status_code}")

        def json(self) -> Any:
            return self._json_data

    # 7a: 429を1回挟んで2回目に成功 → リトライ後に正常データが返り、待ち時間はbackoff_sec[0]
    calls_7a: list[dict] = []
    sleeps_7a: list[float] = []

    def req_7a(body: dict):
        calls_7a.append(dict(body))
        if len(calls_7a) == 1:
            return _FakeResp(429, headers={})
        return _FakeResp(200, json_data=[{"tid": 1}])

    out_7a = _http_post_with_retry({"probe": "7a"}, request_fn=req_7a, sleep_fn=lambda s: sleeps_7a.append(s))
    _check("7a 429→200: 2回目で正常データを取得", out_7a == [{"tid": 1}], results)
    _check("7a 429→200: 呼び出し回数=2", len(calls_7a) == 2, results)
    _check("7a 429→200: 待ち時間はbackoff_sec[0]=2.0(Retry-Afterヘッダ無し)", sleeps_7a == [2.0], results)

    # 7b: Retry-Afterヘッダがあれば既定backoffよりそちらを優先する
    calls_7b: list[dict] = []
    sleeps_7b: list[float] = []

    def req_7b(body: dict):
        calls_7b.append(dict(body))
        if len(calls_7b) == 1:
            return _FakeResp(429, headers={"Retry-After": "5"})
        return _FakeResp(200, json_data=[])

    _http_post_with_retry({"probe": "7b"}, request_fn=req_7b, sleep_fn=lambda s: sleeps_7b.append(s))
    _check("7b Retry-Afterヘッダ尊重: 待ち時間=5.0(既定2.0でなく)", sleeps_7b == [5.0], results)

    # 7c: 5xxも同様にリトライ対象
    calls_7c: list[dict] = []

    def req_7c(body: dict):
        calls_7c.append(dict(body))
        if len(calls_7c) == 1:
            return _FakeResp(503)
        return _FakeResp(200, json_data=[{"tid": 2}])

    out_7c = _http_post_with_retry({"probe": "7c"}, request_fn=req_7c, sleep_fn=lambda s: None)
    _check("7c 503→200: 5xxもリトライして正常データを取得", out_7c == [{"tid": 2}], results)

    # 7d: リトライ上限(3回)を超えて常に429 → HTTPErrorが送出され、試行回数は4回(初回+3リトライ)
    calls_7d: list[dict] = []

    def req_7d(body: dict):
        calls_7d.append(dict(body))
        return _FakeResp(429)

    raised_7d = False
    try:
        _http_post_with_retry({"probe": "7d"}, request_fn=req_7d, sleep_fn=lambda s: None)
    except requests.HTTPError:
        raised_7d = True
    _check("7d 常時429: リトライ上限到達でHTTPErrorが送出される", raised_7d, results)
    _check("7d 常時429: 試行回数=4(初回+最大3リトライ)", len(calls_7d) == 4, results)

    n_pass = sum(1 for _, ok in results if ok)
    n_total = len(results)
    print(f"\n合計: {n_pass}/{n_total} PASS")
    if n_pass == n_total:
        print("SELFTEST: ALL PASS")
    else:
        print("SELFTEST: FAILED")
        for name, ok in results:
            if not ok:
                print(f"  failed -> {name}")
    return n_pass == n_total


# =====================================================================================
# エントリーポイント
# =====================================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Hyperliquid追跡ウォレットのfillsをSession Analysis Dashboard用トレードCSVへ変換する",
    )
    p.add_argument("--addrs", type=str, default=None, help="カンマ区切りのアドレス直接指定(0xA,0xB)")
    p.add_argument("--file", type=str, default=None, help="ウォレットリストJSON(既定: 同ディレクトリのwallet_bridge_whales.json)")
    p.add_argument("--top", type=int, default=10, help="CSV化する上位ウォレット数(既定10)")
    p.add_argument("--min-trades", dest="min_trades", type=int, default=20, help="上位N選定の最低トレード数(既定20)")
    p.add_argument("--days", type=int, default=365, help="遡って取得する日数(既定365)")
    p.add_argument("--out", type=str, default="wallet_trades", help="出力ディレクトリ(既定wallet_trades)")
    p.add_argument("--selftest", action="store_true", help="ネットワーク無しで合成データによる検算のみ実行する")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        ok = run_selftests()
        return 0 if ok else 1

    script_dir = Path(__file__).resolve().parent
    return run_pipeline(args, script_dir)


if __name__ == "__main__":
    sys.exit(main())
