# -*- coding: utf-8 -*-
"""確率スキャナー スキャンジョブ(追補v6 §4・E2)。

用途:
    python scan_job.py --run    # 増分1m取得→3銘柄スキャン→scan_results.json書出→カレンダー照合→Discord通知
    python scan_job.py --dry    # 通知(⑤)なしで①〜④のみ実行(json生成まで)
    python scan_job.py --selftest  # このファイル固有の純ロジック(webhook解決/カレンダー行パース/
                                    # 重要度分類/上位候補フィルタ/embed組立/json roundtrip)を検証

純粋な統計エンジン(階層グループ化・Wilson CI・BH-FDR・カレンダー±15分照合など)は
app.py 側にあり、`python app.py --selftest` の一部として検証済み(仕様§6 1-7)。
このファイルはそれらをimportして使うジョブ層(データ取得・実行順序・JSON書出・通知)のみを持つ。

メモリ規律: 銘柄ごとに逐次処理しdel+gc(Oracle 956MB対策・float32維持。仕様§4)。
掟5: 発注機能なし。掟6: scan_results.jsonのmetaに集計期間・updated_at(JST)を必ず含める。
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent  # Claudcode/ (.env は D-015準拠でここに置く)
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import app  # noqa: E402 - 純ロジック(load_1m/compute_scan_cells/annotate_calendar_flags等)を再利用
import backfill_1m  # noqa: E402 - last_saved_ts/fetch_range(増分取得①)を再利用

JST = app.JST
SYMBOLS: list[str] = list(backfill_1m.SYMBOLS.keys())  # ["BTC", "ETH", "SOL"]

RESULTS_PATH = HERE / "scan_results.json"
CALENDAR_CACHE_PATH = HERE / "calendar_cache.json"

# ---- 追補v7.2§1.2: data_1m不在環境(クラウド)向けカレンダービュー・スナップショット出力 ----
# app.CALENDAR_CELLS_PATH(session_dashboard/calendar_cells.json)と同じ場所に書き出す
# (app.load_calendar_cells_snapshotが読む既定パス)。3銘柄×粒度(30分/1時間)×月(全月+1〜12月)の
# 全セルを事前計算しておき、data_1m不在環境ではこのスナップショットを表示する(仕様§1.2)。
CALENDAR_CELLS_PATH = HERE / "calendar_cells.json"
CALENDAR_CELLS_FREQS: list[int] = [30, 60]  # app.SCAN_SLOTS_PER_DAYが対応する月カレンダー粒度
CALENDAR_CELLS_MONTHS: list[Optional[int]] = [None] + list(range(1, 13))  # None=全月合算
CALENDAR_CACHE_TTL_SEC = 6 * 3600  # 仕様§4: 6hキャッシュ
CALENDAR_DAYS_BEFORE = 1  # GMT/JST日境界のズレ吸収用に前日分も取得
CALENDAR_DAYS_AFTER = 7   # 今日を含め直近7日で全曜日(月〜日)を1回ずつカバー
CALENDAR_API_URL = "https://api.nasdaq.com/api/calendar/economicevents"
CALENDAR_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 仕様§4: 「指標はボラ拡大させるが方向は予測不可」の注記は常設(検証済み知見・ダッシュボード側にも表示)
CALENDAR_DISCLAIMER = "指標イベントはボラティリティを拡大させるが方向は予測不可(検証済み知見)。"
PROB_DISCLAIMER = "確率は過去の発生頻度であり将来の保証ではない。多重検定のためFDR補正q値を併記。"

ENV_PATH = PROJECT_ROOT / ".env"
PRIMARY_ENV_KEY = "DISCORD_WEBHOOK_SESSION_SCAN"
FALLBACK_ENV_KEY = "DISCORD_WEBHOOK_NEW_TEST"

FDR_Q_THRESHOLD = 0.10  # 仕様§4: 上位候補 = FDR q<0.10 かつ n>=30
MIN_N_CANDIDATE = 30
TOP_N_DISCORD_PER_SYMBOL = 3


# =====================================================================================
# 共通ユーティリティ(進捗表示・アトミック書出・webhook解決)
# =====================================================================================

def log(msg: str) -> None:
    """進捗print(仕様§4完了条件c: 実走時に進捗printが必須)。flush=Trueで即座に見えるようにする。"""
    ts = datetime.now(JST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def atomic_write_json(path: Path, data: dict[str, Any], *, compact: bool = False) -> None:
    """一時ファイルへ書いてからreplace(アトミック書出。仕様§4③)。
    compact=True: indentなし・区切り最小化で書出す(#5指摘対処: calendar_cells.jsonが仕様の
    見積り「数MB」に対し実測45MBだったため。scan_results.json等は従来通りindent=2を維持し
    人間可読性を優先する。ロード側(json.load)の互換性には影響しない)。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    # Streamlit静的配信(/app/static/scan_results.json)用のコピー。
    # Claude定期リサーチルーチンが公開URL経由で読むために置く(集計統計のみ・秘密情報なし)。
    try:
        static_dir = path.parent / "static"
        static_dir.mkdir(exist_ok=True)
        import shutil

        shutil.copyfile(path, static_dir / path.name)
    except OSError:
        pass  # 静的コピー失敗は本体を止めない


def _read_env_file(path: Path) -> dict[str, str]:
    """.envファイルをdictへ(engulf_paper/notifier.pyと同じ簡易パーサ。D-015準拠)。"""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def resolve_webhook_url(env_path: Path = ENV_PATH) -> tuple[Optional[str], Optional[str]]:
    """DISCORD_WEBHOOK_SESSION_SCAN優先、無ければDISCORD_WEBHOOK_NEW_TESTにフォールバック。
    OS環境変数を.envファイルより優先。戻り値(url, used_env_key)。両方無ければ(None, None)
    (=送信スキップでログのみ。仕様§4⑤)。"""
    import os
    for key in (PRIMARY_ENV_KEY, FALLBACK_ENV_KEY):
        v = os.environ.get(key)
        if v:
            return v, key
    file_map = _read_env_file(env_path)
    for key in (PRIMARY_ENV_KEY, FALLBACK_ENV_KEY):
        v = file_map.get(key)
        if v:
            return v, key
    return None, None



# =====================================================================================
# 経済指標カレンダー(仕様§4)。無料ソースとしてapi.nasdaq.com/api/calendar/economiceventsを
# 実測選定(2026-07-17実測: date=YYYY-MM-DD・Mozilla風UAで200・rows[].gmt/country/eventName)。
# 重要度フィールドがAPI応答に無いため、主要中銀/雇用/物価/GDP系イベント名のキーワード heuristic で
# high/medium/lowを分類する(user_trading_method調査必須項目=FRB/ECB/BOJ・CPI/雇用統計/GDPに整合)。
# =====================================================================================

MAJOR_COUNTRIES = {"United States", "Euro Area", "United Kingdom", "Japan", "China"}

HIGH_IMPORTANCE_KEYWORDS = [
    "nonfarm payrolls", "non-farm payrolls", "unemployment rate", "cpi", "pce price index",
    "core pce", "gdp", "fomc", "fed interest rate decision", "interest rate decision",
    "federal funds rate", "ecb interest rate", "boj interest rate", "bank of japan",
    "retail sales", "ppi", "ism manufacturing pmi", "ism services pmi", "adp employment change",
]
MEDIUM_IMPORTANCE_KEYWORDS = [
    "pmi", "consumer confidence", "durable goods", "trade balance", "industrial production",
    "housing starts", "building permits", "jobless claims", "consumer sentiment",
]


def classify_event_importance(event_name: str, country: str) -> str:
    """イベント名+国からimportance(high/medium/low)を推定するheuristic(純ロジック・selftest対象)。

    API応答に重要度フィールドが無いための代替分類。主要国(米/ユーロ圏/英/日/中)かつ
    中銀政策・雇用・物価・GDP系キーワードのみhigh。それ以外の主要国キーワード一致はmedium。
    一致なしはlow。大文字小文字は無視。
    """
    name_lower = (event_name or "").lower()
    is_major = country in MAJOR_COUNTRIES
    if is_major and any(kw in name_lower for kw in HIGH_IMPORTANCE_KEYWORDS):
        return "high"
    if any(kw in name_lower for kw in HIGH_IMPORTANCE_KEYWORDS + MEDIUM_IMPORTANCE_KEYWORDS):
        return "medium"
    return "low"


def build_calendar_events_from_rows(rows: list[dict[str, Any]], event_date_gmt: date) -> list[dict[str, Any]]:
    """nasdaq economiceventsの生rows(1日分・gmt="HH:MM"文字列)をevents形式へ変換する(純ロジック)。

    出力各要素: {"datetime": pd.Timestamp(JST tz付き), "name": str, "importance": str, "country": str}
    (app.match_calendar_events_for_cell / annotate_calendar_flags が期待する形式)。
    gmt解析に失敗した行はスキップする(壊れた1行のために全体を落とさない)。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue  # 想定外の行形状は1行スキップ(仕様§4: 壊れた1行のために全体を落とさない)
        gmt_str = str(row.get("gmt", "")).strip()
        if ":" not in gmt_str:
            continue
        try:
            hh, mm = gmt_str.split(":")
            hh_i, mm_i = int(hh), int(mm)
        except ValueError:
            continue
        # ⚠️ フィールド名はgmtだがNasdaq APIの実体は米東部時間(ET・DST変動)。UTCではない。
        # 実測4/4一致(米ADP08:15=8:15AM ET・加IPPI08:30・英CPI02:00=英7時・豪失業率21:30)。
        # America/New_YorkでDST込み解釈しJSTへ(夏EDT=+13h/冬EST=+14h)。26-07-18 時差バグ修正。
        ts_et = pd.Timestamp(event_date_gmt.year, event_date_gmt.month, event_date_gmt.day,
                             hh_i, mm_i, tz="America/New_York")
        ts_jst = ts_et.tz_convert("Asia/Tokyo")
        name = str(row.get("eventName", "")).strip() or "指標"
        country = str(row.get("country", "")).strip()
        out.append({"datetime": ts_jst, "name": name,
                    "importance": classify_event_importance(name, country), "country": country})
    return out



def fetch_calendar_day(d: date, timeout: int = 10) -> Optional[list[dict[str, Any]]]:
    """指定日(GMT日付として解釈)のnasdaq経済指標カレンダーを取得しevents形式で返す。
    失敗時はNone(仕様§4: 失敗時はスキップし本体を止めない。呼び出し側で握りつぶす前提)。"""
    url = f"{CALENDAR_API_URL}?date={d.isoformat()}"
    req = urllib.request.Request(url, headers={"User-Agent": CALENDAR_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 - ネットワーク/形式エラーは全てスキップ対象
        log(f"  [calendar] {d.isoformat()} 取得失敗スキップ: {type(e).__name__}")
        return None
    # 想定外レスポンス形状(トップレベルがdict以外等)でもクラッシュさせず照合スキップにする
    # (仕様§4: 失敗時は照合スキップで本体を止めない。無料の非公式APIのためスキーマ変化は起こり得る)。
    if not isinstance(payload, dict):
        log(f"  [calendar] {d.isoformat()} 想定外レスポンス形状のためスキップ: {type(payload).__name__}")
        return None
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    if not isinstance(rows, list):
        rows = []
    return build_calendar_events_from_rows(rows, d)


def load_calendar_cache(path: Path = CALENDAR_CACHE_PATH,
                        ttl_sec: int = CALENDAR_CACHE_TTL_SEC) -> Optional[list[dict[str, Any]]]:
    """6h以内のキャッシュがあればevents(dictのdatetimeはpd.Timestampへ復元)を返す。無ければNone。"""
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return None
    age_sec = (datetime.now(JST) - pd.Timestamp(fetched_at).tz_convert("Asia/Tokyo")).total_seconds()
    if age_sec < 0 or age_sec > ttl_sec:
        return None
    events = []
    for ev in cache.get("events", []):
        events.append({**ev, "datetime": pd.Timestamp(ev["datetime"])})
    return events


def save_calendar_cache(events: list[dict[str, Any]], path: Path = CALENDAR_CACHE_PATH) -> None:
    """events(datetime=pd.Timestamp)をJSON安全な形でキャッシュへ書出す(6hキャッシュ・仕様§4)。"""
    serializable = [{**ev, "datetime": pd.Timestamp(ev["datetime"]).isoformat()} for ev in events]
    atomic_write_json(path, {"fetched_at": datetime.now(JST).isoformat(), "events": serializable})



def get_calendar_events(reference_date_jst: date, use_cache: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """reference_date_jstを含む前後(CALENDAR_DAYS_BEFORE〜CALENDAR_DAYS_AFTER)のGMT日付分を
    キャッシュ優先で取得する。1日でも取得成功すればそのeventsを使う(全滅時のみ空リスト+ok=False)。
    仕様§4: 失敗時は照合スキップ(空フラグ)で本体を止めない。戻り値は(events, meta)。
    """
    meta: dict[str, Any] = {"source": CALENDAR_API_URL, "window_minutes": app.CALENDAR_WINDOW_MINUTES_DEFAULT,
                             "min_importance": app.CALENDAR_MIN_IMPORTANCE_DEFAULT, "ok": False,
                             "used_cache": False, "fetched_at": None, "n_events": 0}
    if use_cache:
        cached = load_calendar_cache()
        if cached is not None:
            meta.update(ok=True, used_cache=True, fetched_at=datetime.now(JST).isoformat(), n_events=len(cached))
            log(f"  [calendar] キャッシュ使用(6h以内): {len(cached)}件")
            return cached, meta

    events: list[dict[str, Any]] = []
    any_ok = False
    for offset in range(-CALENDAR_DAYS_BEFORE, CALENDAR_DAYS_AFTER + 1):
        d = reference_date_jst + timedelta(days=offset)
        day_events = fetch_calendar_day(d)
        if day_events is not None:
            any_ok = True
            events.extend(day_events)
    if not any_ok:
        log("  [calendar] 全日程取得失敗。照合スキップ(空フラグ)で続行")
        return [], meta
    save_calendar_cache(events)
    meta.update(ok=True, used_cache=False, fetched_at=datetime.now(JST).isoformat(), n_events=len(events))
    log(f"  [calendar] 新規取得: {len(events)}件(±{CALENDAR_DAYS_BEFORE}〜{CALENDAR_DAYS_AFTER}日)")
    return events, meta



# =====================================================================================
# ①増分1m取得(backfill_1m.pyのlast_saved_ts/fetch_rangeを流用。仕様§4)
# =====================================================================================

def incremental_update_symbol(label: str) -> int:
    """labelの最終保存ts以降を増分取得する(backfill_1m.py --update相当)。
    保存データが無ければ取得せず0を返す(先に--backfillが必要。scan_jobの責務外)。
    ネットワーク失敗はfetch_range内部でリトライ済み・最終失敗時はそこまでの分で打ち切られる
    (例外は投げない設計を踏襲)。"""
    symbol = backfill_1m.SYMBOLS[label]
    saved = backfill_1m.last_saved_ts(label)
    if saved is None:
        log(f"  [{label}] 保存データなし。増分取得をスキップ(先に--backfillが必要)")
        return 0
    now_ms = int(time.time() * 1000)
    since_ms = (saved + 60) * 1000
    if since_ms >= now_ms:
        log(f"  [{label}] 増分取得: 既に最新")
        return 0
    n = backfill_1m.fetch_range(label, symbol, since_ms, now_ms)
    log(f"  [{label}] 増分取得: +{n}本")
    return n



# =====================================================================================
# 上位候補フィルタ+Discord embed組立(純ロジック・selftest対象)
# =====================================================================================

def _filter_sort_candidates(cells: pd.DataFrame, q_threshold: float, min_n: int) -> pd.DataFrame:
    """FDR q<q_threshold かつ n>=min_n のセルをq昇順(タイはn降順)で抽出する(仕様§4)。"""
    if cells.empty:
        return cells
    sub = cells[(cells["fdr_q"] < q_threshold) & (cells["n"] >= min_n)]
    return sub.sort_values(["fdr_q", "n"], ascending=[True, False]).reset_index(drop=True)


def build_top_candidates(symbol_cells: dict[str, pd.DataFrame], q_threshold: float = FDR_Q_THRESHOLD,
                          min_n: int = MIN_N_CANDIDATE) -> list[dict[str, Any]]:
    """銘柄横断の上位候補リスト(JSON安全dict・symbol列付き)をq昇順で返す(仕様§4③)。"""
    records: list[dict[str, Any]] = []
    for label, cells in symbol_cells.items():
        sub = _filter_sort_candidates(cells, q_threshold, min_n)
        for rec in sub.to_dict(orient="records"):
            safe = {k: app._scan_json_safe(v) for k, v in rec.items()}
            safe["symbol"] = label
            records.append(safe)
    records.sort(key=lambda r: (r["fdr_q"] if r["fdr_q"] is not None else 1.0, -r["n"]))
    return records


def _fmt_candidate_line(rec: dict[str, Any]) -> str:
    """1候補セルをDiscord embed用の1行テキストへ整形(事前整形方式・仕様§5踏襲)。"""
    direction = rec.get("dominant_direction", "?")
    p_col = {"上昇": "p_up", "下降": "p_down", "レンジ": "p_range"}.get(direction, "p_up")
    p_val = rec.get(p_col)
    ci_lo = rec.get(f"{p_col}_ci_low")
    ci_hi = rec.get(f"{p_col}_ci_high")
    p_str = f"{p_val:.0%}" if isinstance(p_val, (int, float)) else "n/a"
    ci_str = (f"[{ci_lo:.0%}-{ci_hi:.0%}]" if isinstance(ci_lo, (int, float)) and isinstance(ci_hi, (int, float))
              else "")
    q_val = rec.get("fdr_q")
    q_str = f"{q_val:.3f}" if isinstance(q_val, (int, float)) else "n/a"
    flag = rec.get("calendar_flag") or ""
    return (f"{rec.get('weekday', '?')} {rec.get('time_range', '?')} {direction} "
            f"P={p_str}{ci_str} n={rec.get('n', '?')} q={q_str} {flag}".rstrip())



def build_scan_discord_embed(symbol_cells: dict[str, pd.DataFrame], updated_at_jst: datetime,
                              top_n: int = TOP_N_DISCORD_PER_SYMBOL) -> dict[str, Any]:
    """銘柄別トップ3候補セル+免責文のDiscord embedを組み立てる(純ロジック・仕様§4⑤)。"""
    fields: list[dict[str, Any]] = []
    any_candidate = False
    for label, cells in symbol_cells.items():
        top = _filter_sort_candidates(cells, FDR_Q_THRESHOLD, MIN_N_CANDIDATE).head(top_n)
        if top.empty:
            fields.append({"name": f"{label}", "value": "候補なし(q<0.10かつn≥30を満たすセルなし)",
                            "inline": False})
            continue
        any_candidate = True
        lines = [_fmt_candidate_line(rec) for rec in top.to_dict(orient="records")]
        fields.append({"name": f"{label} トップ{len(lines)}候補", "value": "\n".join(lines), "inline": False})
    fields.append({"name": "免責", "value": f"{PROB_DISCLAIMER}\n{CALENDAR_DISCLAIMER}", "inline": False})
    return {
        "title": "⚡ 確率スキャナー 定期更新",
        "description": f"更新日時: {updated_at_jst.strftime('%Y-%m-%d %H:%M JST')}"
                        + ("" if any_candidate else "\n(全銘柄でFDR候補なし)"),
        "color": 0x3498DB,
        "fields": fields,
        "footer": {"text": "session_dashboard scan_job.py（掟5=発注機能なし・研究用）"},
    }


def send_discord_embed(embed: dict[str, Any], dry_run: bool = False, timeout: int = 10) -> bool:
    """webhook解決→embed送信。未設定/失敗はFalseを返すのみ(例外は投げず本体を止めない)。
    dry_run=Trueならwebhook解決すら行わずスキップしたことだけログしてFalseを返す(仕様§4: --dryは⑤なし)。
    """
    if dry_run:
        log("  [discord] --dryのため通知スキップ")
        return False
    url, used_key = resolve_webhook_url()
    if not url:
        log(f"  [discord] {PRIMARY_ENV_KEY}/{FALLBACK_ENV_KEY} 共に未設定。送信スキップ")
        return False
    try:
        payload = {"username": "確率スキャナー", "embeds": [embed]}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json", "User-Agent": CALENDAR_UA})
        urllib.request.urlopen(req, timeout=timeout)
        log(f"  [discord] 送信成功({used_key})")
        return True
    except Exception as e:  # noqa: BLE001 - 通知失敗でBotを止めない
        log(f"  [discord] 送信失敗: {type(e).__name__}")
        return False



# =====================================================================================
# 本体パイプライン(①増分取得 ②逐次スキャン ③json組立 ④カレンダー照合)
# =====================================================================================

def run_scan(symbols: list[str] = SYMBOLS, do_fetch: bool = True, do_calendar: bool = True,
             freq_minutes: int = 30) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    """全銘柄を逐次スキャンしscan_results.json用dictを返す。戻り値2つ目はcells(DataFrame・
    calendar_flag付き)のdict(Discord embed組立でも再利用するため呼び出し側へ返す)。
    銘柄ごとにdel+gcしてメモリ規律(仕様§4メモリ規律)を守る。
    """
    today_jst = datetime.now(JST).date()
    events: list[dict[str, Any]] = []
    calendar_meta: dict[str, Any] = {"ok": False, "skipped": True}
    if do_calendar:
        log("[calendar] 経済指標カレンダー照合を開始")
        events, calendar_meta = get_calendar_events(today_jst)
    else:
        log("[calendar] --no-calendarのためスキップ")

    symbol_cells: dict[str, pd.DataFrame] = {}
    symbol_meta: dict[str, dict[str, Any]] = {}
    for label in symbols:
        log(f"[{label}] スキャン開始")
        if do_fetch:
            incremental_update_symbol(label)
        df = app.load_1m(label)
        log(f"  [{label}] load_1m: {len(df):,}本")
        cells, meta = app.compute_scan_cells(df, label=label, freq_minutes=freq_minutes, month=None)
        del df
        gc.collect()
        cells = app.annotate_calendar_flags(cells, events, freq_minutes, today_jst) if not cells.empty \
            else cells.assign(calendar_flag=pd.Series(dtype=str))
        symbol_cells[label] = cells
        symbol_meta[label] = meta
        log(f"  [{label}] セル数={len(cells)} n_occ={meta['n_occurrences_used']:,} "
            f"期間={meta['period_start']}〜{meta['period_end']}")

    updated_at = datetime.now(JST)
    meta_global = {
        "updated_at": updated_at.isoformat(),
        "generated_by": "scan_job.py", "freq_minutes": freq_minutes,
        "fee_pct": app.SCAN_DEFAULT_FEE_PCT, "tail_thresholds": list(app.SCAN_DEFAULT_TAIL_THRESHOLDS),
        "threshold_mode": "adaptive", "k": 1.0,
        "fdr_q_threshold": FDR_Q_THRESHOLD, "min_n_candidate": MIN_N_CANDIDATE,
        "calendar": calendar_meta, "prob_disclaimer": PROB_DISCLAIMER,
        "calendar_disclaimer": CALENDAR_DISCLAIMER,
    }
    symbols_json = {label: app.scan_cells_to_json_dict(symbol_cells[label], symbol_meta[label])
                    for label in symbols}
    top_candidates = build_top_candidates(symbol_cells)
    result = {"meta": meta_global, "symbols": symbols_json, "top_candidates": top_candidates}
    return result, symbol_cells



# =====================================================================================
# 追補v7.2§1.2: calendar_cells.json組立(3銘柄×粒度30/60分×月(全月+1〜12月)の全セル)
# =====================================================================================

def build_calendar_cells_snapshot(
    symbols: list[str] = SYMBOLS,
    freqs: list[int] = CALENDAR_CELLS_FREQS,
    months: list[Optional[int]] = CALENDAR_CELLS_MONTHS,
    load_fn: Optional[Callable[[str], pd.DataFrame]] = None,
) -> dict[str, Any]:
    """app.load_calendar_cells_snapshot/calendar_cells_snapshot_lookupが読む形式のdictを組み立てる
    (仕様§1.2)。銘柄ごとに逐次load_1m→粒度ごとにbuild_scan_occurrencesを1回だけ実行し、
    月はprecomputed_occで使い回す(app.compute_month_calendar_cellsの再計算回避と同じ作法)。
    銘柄処理後にdel+gcしメモリ規律(仕様§4)を維持する。load_fnはselftest用の差し替え口
    (省略時はapp.load_1m=実parquet読込)。
    """
    load = load_fn if load_fn is not None else app.load_1m
    symbols_json: dict[str, Any] = {}
    for label in symbols:
        log(f"[calendar-cells][{label}] 開始")
        df = load(label)
        log(f"  [calendar-cells][{label}] 読込: {len(df):,}本")
        freq_json: dict[str, Any] = {}
        if not df.empty:
            for freq in freqs:
                occ, occ_meta = app.build_scan_occurrences(df, freq)
                month_json: dict[str, Any] = {}
                for month in months:
                    cells, meta = app.compute_month_calendar_cells(
                        df, month=month, freq_minutes=freq, precomputed_occ=(occ, occ_meta))
                    month_key = "all" if month is None else str(month)
                    month_json[month_key] = app.scan_cells_to_json_dict(cells, meta)
                del occ
                freq_json[str(freq)] = month_json
                log(f"  [calendar-cells][{label}] 粒度{freq}分: {len(months)}ヶ月分完了")
        symbols_json[label] = freq_json
        del df
        gc.collect()
        log(f"[calendar-cells][{label}] 完了")
    return {
        "meta": {
            "updated_at": datetime.now(JST).isoformat(),
            "generated_by": "scan_job.py --calendar-cells",
            "symbols": symbols, "freqs": freqs,
        },
        "symbols": symbols_json,
    }



def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="store_true", help="増分取得→スキャン→json書出→カレンダー→Discord通知")
    ap.add_argument("--dry", action="store_true", help="Discord通知(⑤)なしでjson生成まで")
    ap.add_argument("--selftest", action="store_true", help="scan_job.py固有の純ロジックを検証")
    ap.add_argument("--no-fetch", action="store_true", help="増分1m取得(①)をスキップ(既存データのみ使用)")
    ap.add_argument("--no-calendar", action="store_true", help="経済指標カレンダー照合(④)をスキップ")
    ap.add_argument("--symbols", default=",".join(SYMBOLS), help="対象銘柄(カンマ区切り表示名)")
    ap.add_argument("--calendar-cells", action="store_true",
                     help="calendar_cells.json(仕様§1.2データ不在環境向けスナップショット)を書出")
    ap.add_argument("--freqs", default=",".join(str(f) for f in CALENDAR_CELLS_FREQS),
                     help="--calendar-cells対象の粒度(分)カンマ区切り(既定30,60。動作確認は30のみ等に絞れる)")
    args = ap.parse_args()

    if args.selftest:
        ok = run_selftest()
        sys.exit(0 if ok else 1)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    if args.calendar_cells:
        freqs = [int(f.strip()) for f in args.freqs.split(",") if f.strip()]
        t0 = time.time()
        log(f"カレンダーセル書出開始(symbols={symbols} freqs={freqs} "
            f"months={len(CALENDAR_CELLS_MONTHS)}件)")
        snapshot = build_calendar_cells_snapshot(symbols, freqs, CALENDAR_CELLS_MONTHS)
        atomic_write_json(CALENDAR_CELLS_PATH, snapshot, compact=True)
        log(f"calendar_cells.json 書出完了: {CALENDAR_CELLS_PATH}(経過{time.time() - t0:.1f}秒)")
        sys.exit(0)

    if not args.run and not args.dry:
        ap.error("--run か --dry か --selftest か --calendar-cells のいずれかを指定してください")
    t0 = time.time()
    log(f"スキャンジョブ開始(symbols={symbols} fetch={not args.no_fetch} "
        f"calendar={not args.no_calendar} mode={'run' if args.run else 'dry'})")

    result, symbol_cells = run_scan(symbols, do_fetch=not args.no_fetch, do_calendar=not args.no_calendar)
    atomic_write_json(RESULTS_PATH, result)
    n_candidates = len(result["top_candidates"])
    log(f"scan_results.json 書出完了: {RESULTS_PATH} (上位候補{n_candidates}件・"
        f"経過{time.time() - t0:.1f}秒)")

    if args.run:
        embed = build_scan_discord_embed(symbol_cells, datetime.now(JST))
        send_discord_embed(embed, dry_run=False)
    else:
        log("--dryのためDiscord通知(⑤)は実行しません")
    log("完了")



# =====================================================================================
# run_selftest() … scan_job.py固有の純ロジック(app.py --selftestとは別枠・ネットワーク非依存)
# =====================================================================================

def run_selftest() -> bool:
    import os
    import tempfile

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
    print("SCAN_JOB SELFTEST START")
    print("=" * 78)

    # -----------------------------------------------------------------
    # SJ-1: webhook解決(環境変数優先・.envフォールバック・両方無しでNone)
    # -----------------------------------------------------------------
    print("\n--- SJ-1. webhook解決 ---")
    saved_env = {k: os.environ.pop(k, None) for k in (PRIMARY_ENV_KEY, FALLBACK_ENV_KEY)}
    try:
        with tempfile.TemporaryDirectory() as td:
            envp = Path(td) / ".env"
            envp.write_text(f"{FALLBACK_ENV_KEY}=https://example.com/fallback\n", encoding="utf-8")
            url, key = resolve_webhook_url(envp)
            check("SJ-1a ファイルのみ(fallbackのみ設定)でfallbackが解決される",
                  url == "https://example.com/fallback" and key == FALLBACK_ENV_KEY, f"got=({url},{key})")

            envp.write_text(f"{PRIMARY_ENV_KEY}=https://example.com/primary\n"
                             f"{FALLBACK_ENV_KEY}=https://example.com/fallback\n", encoding="utf-8")
            url2, key2 = resolve_webhook_url(envp)
            check("SJ-1b primary/fallback両方設定でprimaryが優先される",
                  url2 == "https://example.com/primary" and key2 == PRIMARY_ENV_KEY, f"got=({url2},{key2})")

            empty_envp = Path(td) / "empty.env"
            empty_envp.write_text("", encoding="utf-8")
            url3, key3 = resolve_webhook_url(empty_envp)
            check("SJ-1c 両方未設定は(None,None)=送信スキップ判定可能", url3 is None and key3 is None,
                  f"got=({url3},{key3})")

            os.environ[PRIMARY_ENV_KEY] = "https://example.com/env-primary"
            url4, key4 = resolve_webhook_url(envp)
            check("SJ-1d OS環境変数が.envファイルより優先される",
                  url4 == "https://example.com/env-primary" and key4 == PRIMARY_ENV_KEY, f"got=({url4},{key4})")
            del os.environ[PRIMARY_ENV_KEY]

            check("SJ-1e send_discord_embed(dry_run=True)はwebhook未解決でも常にFalse",
                  send_discord_embed({"title": "t"}, dry_run=True) is False)
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v

    # -----------------------------------------------------------------
    # SJ-2: 重要度分類heuristic・カレンダー行パース(gmt文字列→JST変換・不正行スキップ)
    # -----------------------------------------------------------------
    print("\n--- SJ-2. カレンダー行パース/重要度分類 ---")
    check("SJ-2a 米国雇用統計はhigh", classify_event_importance("Nonfarm Payrolls", "United States") == "high")
    check("SJ-2b 米国CPIはhigh", classify_event_importance("CPI", "United States") == "high")
    check("SJ-2c 非主要国のCPIはmedium(主要国限定high)",
          classify_event_importance("CPI", "Indonesia") == "medium")
    check("SJ-2d 主要国だがキーワード不一致な小指標はlow",
          classify_event_importance("Foreign Bond Investment", "Japan") == "low")
    check("SJ-2e PMI系(非主要中銀/雇用/物価/GDP)はmedium",
          classify_event_importance("Manufacturing PMI", "United States") == "medium")

    rows_sj2 = [
        {"gmt": "13:30", "country": "United States", "eventName": "Nonfarm Payrolls"},
        {"gmt": "bogus", "country": "United States", "eventName": "壊れた行"},
        {"gmt": "23:59", "country": "Japan", "eventName": "BOJ Interest Rate Decision"},
    ]
    events_sj2 = build_calendar_events_from_rows(rows_sj2, date(2026, 7, 17))
    check("SJ-2f 不正gmt行はスキップされ2件のみ残る", len(events_sj2) == 2, f"n={len(events_sj2)}")
    ev0 = events_sj2[0]
    # gmt="13:30"はET(夏EDT=UTC-4)。13:30 EDT=17:30 UTC=翌02:30 JST。
    expected_jst_sj2 = pd.Timestamp("2026-07-17 13:30", tz="America/New_York").tz_convert("Asia/Tokyo")
    check("SJ-2g ET→JST変換(13:30 EDT = 翌02:30 JST)が正しい",
          ev0["datetime"] == expected_jst_sj2 and ev0["importance"] == "high",
          f"got={ev0['datetime']} importance={ev0['importance']}")
    ev1 = events_sj2[1]
    # gmt="23:59"はET。23:59 EDT=翌03:59 UTC=翌12:59 JST。
    check("SJ-2h 日跨ぎ(23:59 EDT)は翌日12:59 JSTへ正しく変換",
          ev1["datetime"].hour == 12 and ev1["datetime"].minute == 59
          and ev1["datetime"].date() == date(2026, 7, 18) and ev1["importance"] == "high",
          f"got={ev1['datetime']}")

    # -----------------------------------------------------------------
    # SJ-3: 上位候補フィルタ(FDR q<0.10かつn>=30)+銘柄横断ソート(q昇順・タイはn降順)
    # -----------------------------------------------------------------
    print("\n--- SJ-3. 上位候補フィルタ/銘柄横断ソート ---")

    def _row_sj3(n, q, direction="上昇", weekday="月", slot=10):
        return {"weekday": weekday, "slot": slot, "time_range": "05:00–05:30", "session_band": "NY中盤 (1-4)",
                "n": n, "p_up": 0.7, "p_up_ci_low": 0.6, "p_up_ci_high": 0.8,
                "p_down": 0.2, "p_down_ci_low": 0.1, "p_down_ci_high": 0.3,
                "p_range": 0.1, "p_range_ci_low": 0.05, "p_range_ci_high": 0.2,
                "fdr_q": q, "dominant_direction": direction, "calendar_flag": ""}

    cells_aaa_sj3 = pd.DataFrame([_row_sj3(50, 0.05), _row_sj3(20, 0.01), _row_sj3(40, 0.15)])
    cells_bbb_sj3 = pd.DataFrame([_row_sj3(60, 0.05), _row_sj3(35, 0.02)])
    top_sj3 = build_top_candidates({"AAA": cells_aaa_sj3, "BBB": cells_bbb_sj3}, 0.10, 30)
    check("SJ-3a n<30・q>=0.10のセルは除外され3件のみ残る", len(top_sj3) == 3, f"n={len(top_sj3)}")
    order_sj3 = [(r["symbol"], r["n"], r["fdr_q"]) for r in top_sj3]
    expected_order_sj3 = [("BBB", 35, 0.02), ("BBB", 60, 0.05), ("AAA", 50, 0.05)]
    check("SJ-3b q昇順・タイはn降順で銘柄横断ソートされる", order_sj3 == expected_order_sj3,
          f"got={order_sj3}")

    line_sj3 = _fmt_candidate_line(top_sj3[0])
    check("SJ-3c 候補行テキストに曜日/時刻/方向/n/qが含まれる",
          all(s in line_sj3 for s in ("月", "05:00", "上昇", "n=35", "q=0.020")), f"line={line_sj3!r}")

    empty_top_sj3 = build_top_candidates({"AAA": pd.DataFrame(columns=cells_aaa_sj3.columns)}, 0.10, 30)
    check("SJ-3d 空セルDataFrameは候補0件", empty_top_sj3 == [])

    # -----------------------------------------------------------------
    # SJ-4: Discord embed組立(銘柄別トップN・候補なし表記・免責文常設)
    # -----------------------------------------------------------------
    print("\n--- SJ-4. Discord embed組立 ---")
    embed_sj4 = build_scan_discord_embed(
        {"AAA": cells_aaa_sj3, "BBB": cells_bbb_sj3, "CCC": pd.DataFrame(columns=cells_aaa_sj3.columns)},
        datetime(2026, 7, 17, 12, 0, tzinfo=JST), top_n=3)
    field_names_sj4 = [f["name"] for f in embed_sj4["fields"]]
    check("SJ-4a 3銘柄分+免責フィールドが存在", len(embed_sj4["fields"]) == 4, f"names={field_names_sj4}")
    check("SJ-4b 候補ゼロ銘柄(CCC)は「候補なし」表記",
          any("候補なし" in f["value"] for f in embed_sj4["fields"] if f["name"].startswith("CCC")))
    check("SJ-4c 免責フィールドに確率/カレンダー両方の注記を含む",
          any(PROB_DISCLAIMER in f["value"] and CALENDAR_DISCLAIMER in f["value"]
              for f in embed_sj4["fields"] if f["name"] == "免責"))
    check("SJ-4d 更新日時(JST)がdescriptionに含まれる", "2026-07-17 12:00 JST" in embed_sj4["description"],
          f"desc={embed_sj4['description']!r}")

    # -----------------------------------------------------------------
    # SJ-5: JSONアトミック書出roundtrip・カレンダーキャッシュTTL(6h以内は使用/超過は失効)
    # -----------------------------------------------------------------
    print("\n--- SJ-5. JSON書出/カレンダーキャッシュTTL ---")
    with tempfile.TemporaryDirectory() as td:
        p_sj5 = Path(td) / "roundtrip.json"
        payload_sj5 = {"a": 1, "b": [1, 2, 3], "ja": "確率スキャナー"}
        atomic_write_json(p_sj5, payload_sj5)
        check("SJ-5a atomic_write_json書出後、一時ファイルが残らない",
              not p_sj5.with_suffix(".json.tmp").exists())
        with open(p_sj5, encoding="utf-8") as f:
            loaded_sj5 = json.load(f)
        check("SJ-5b 書出→読込で内容が一致(日本語含む)", loaded_sj5 == payload_sj5, f"got={loaded_sj5}")

        # SJ-5b2: compact=True(#5指摘対処)は内容はindent=2版と一致しつつファイルサイズが縮む
        p_sj5c = Path(td) / "compact.json"
        atomic_write_json(p_sj5c, payload_sj5, compact=True)
        with open(p_sj5c, encoding="utf-8") as f:
            loaded_sj5c = json.load(f)
        check("SJ-5b2a compact=True書出も内容はindent=2版と一致", loaded_sj5c == payload_sj5,
              f"got={loaded_sj5c}")
        check("SJ-5b2b compact=Trueはindent=2よりファイルサイズが小さい",
              p_sj5c.stat().st_size < p_sj5.stat().st_size,
              f"compact={p_sj5c.stat().st_size} indent2={p_sj5.stat().st_size}")

        cache_p_sj5 = Path(td) / "calendar_cache.json"
        events_sj5 = [{"datetime": pd.Timestamp("2026-07-17 10:00", tz="Asia/Tokyo"),
                       "name": "テスト指標", "importance": "high", "country": "United States"}]
        save_calendar_cache(events_sj5, cache_p_sj5)
        fresh_sj5 = load_calendar_cache(cache_p_sj5, ttl_sec=CALENDAR_CACHE_TTL_SEC)
        check("SJ-5c 保存直後(0秒経過)はキャッシュ有効でイベント1件復元",
              fresh_sj5 is not None and len(fresh_sj5) == 1
              and fresh_sj5[0]["datetime"] == events_sj5[0]["datetime"], f"got={fresh_sj5}")
        stale_sj5 = load_calendar_cache(cache_p_sj5, ttl_sec=-1)
        check("SJ-5d ttl_sec=-1(即失効)はキャッシュ無効でNone", stale_sj5 is None)
        missing_sj5 = load_calendar_cache(Path(td) / "no_such_cache.json")
        check("SJ-5e キャッシュファイル不在はNone(例外を投げない)", missing_sj5 is None)

    # -----------------------------------------------------------------
    # SJ-6: fetch_calendar_day防御(想定外レスポンス形状でクラッシュしない。仕様§4)
    # -----------------------------------------------------------------
    print("\n--- SJ-6. fetch_calendar_day 想定外レスポンス防御 ---")

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return self._body

    orig_urlopen_sj6 = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda req, timeout=10: _FakeResp(json.dumps([1, 2, 3]).encode())
        r_sj6a = fetch_calendar_day(date(2026, 7, 17))
        check("SJ-6a トップレベルがlist(dict以外)のレスポンスはクラッシュせずNone",
              r_sj6a is None, f"got={r_sj6a!r}")

        urllib.request.urlopen = lambda req, timeout=10: _FakeResp(json.dumps(
            {"data": {"rows": [{"gmt": "13:30", "country": "United States", "eventName": "OK"},
                                "壊れた行", None]}}).encode())
        r_sj6b = fetch_calendar_day(date(2026, 7, 17))
        check("SJ-6b rows内にdict以外の要素が混在してもクラッシュせず正常行のみ抽出",
              r_sj6b is not None and len(r_sj6b) == 1 and r_sj6b[0]["name"] == "OK", f"got={r_sj6b!r}")

        urllib.request.urlopen = lambda req, timeout=10: _FakeResp(
            json.dumps({"data": {"rows": "bogus"}}).encode())
        r_sj6c = fetch_calendar_day(date(2026, 7, 17))
        check("SJ-6c dataはdictだがrowsがlist以外でもクラッシュせず空リスト", r_sj6c == [], f"got={r_sj6c!r}")
    finally:
        urllib.request.urlopen = orig_urlopen_sj6

    # -----------------------------------------------------------------
    # SJ-7: build_calendar_cells_snapshot(仕様§1.2)。合成1分足(load_fn差し替えでparquet非依存)
    # →書出→app.load_calendar_cells_snapshot/calendar_cells_snapshot_lookupで読込むend-to-end
    # roundtripを検証する(scan_job=書き手・app.py=読み手の両側を1本でつなぐ)。
    # -----------------------------------------------------------------
    print("\n--- SJ-7. calendar_cells.json組立+roundtrip(仕様§1.2) ---")

    def _bars_sj7(day: pd.Timestamp, hour: int, n: int = 60, base: float = 100.0) -> pd.DataFrame:
        bidx = pd.date_range(day + pd.Timedelta(hours=hour), periods=n, freq="1min")
        bopen = base + np.arange(n) * 0.01
        bclose = bopen + 0.01
        bhigh = np.maximum(bopen, bclose) + 0.02
        blow = np.minimum(bopen, bclose) - 0.02
        return pd.DataFrame({"open": bopen, "high": bhigh, "low": blow, "close": bclose,
                              "volume": np.full(n, 3.0)}, index=bidx)

    base_day_sj7 = pd.Timestamp("2026-01-05")  # 3オカレンス: 同曜日×1/12(+1週=1月)・2/2(+4週=2月)
    dates_sj7 = [base_day_sj7, base_day_sj7 + pd.Timedelta(weeks=1), base_day_sj7 + pd.Timedelta(weeks=4)]
    df_sj7 = pd.concat([_bars_sj7(d, 9) for d in dates_sj7]).sort_index()  # 09:00-10:00=slot18,19(30分)

    snap_sj7 = build_calendar_cells_snapshot(
        symbols=["BTC"], freqs=[30], months=[None, 1, 2], load_fn=lambda label: df_sj7)
    check("SJ-7a meta.symbols/freqsが指定どおり記録される",
          snap_sj7["meta"]["symbols"] == ["BTC"] and snap_sj7["meta"]["freqs"] == [30],
          f"meta={snap_sj7['meta']}")
    freq30_sj7 = snap_sj7["symbols"]["BTC"]["30"]
    check("SJ-7b 月キーがall/1/2の3つ揃っている", set(freq30_sj7.keys()) == {"all", "1", "2"},
          f"keys={list(freq30_sj7.keys())}")

    cells_all_sj7, _ = app.scan_cells_from_json_dict(freq30_sj7["all"])
    cells_jan_sj7, _ = app.scan_cells_from_json_dict(freq30_sj7["1"])
    cells_feb_sj7, _ = app.scan_cells_from_json_dict(freq30_sj7["2"])
    n_all_sj7 = int(cells_all_sj7.loc[cells_all_sj7["slot"] == 18, "n"].iloc[0])
    n_jan_sj7 = int(cells_jan_sj7.loc[cells_jan_sj7["slot"] == 18, "n"].iloc[0])
    n_feb_sj7 = int(cells_feb_sj7.loc[cells_feb_sj7["slot"] == 18, "n"].iloc[0])
    check("SJ-7c slot18(09:00-09:30)のnが all=3/1月=2/2月=1 と月フィルタどおり",
          (n_all_sj7, n_jan_sj7, n_feb_sj7) == (3, 2, 1),
          f"got=({n_all_sj7},{n_jan_sj7},{n_feb_sj7})")

    with tempfile.TemporaryDirectory() as td:
        p_sj7 = Path(td) / "calendar_cells.json"
        atomic_write_json(p_sj7, snap_sj7)
        loaded_sj7 = app.load_calendar_cells_snapshot(p_sj7)
        check("SJ-7d 書出→app.load_calendar_cells_snapshotで読込復元できる", loaded_sj7 is not None)
        cells_rt_sj7, meta_rt_sj7 = app.calendar_cells_snapshot_lookup(loaded_sj7, "BTC", None, 30)
        n_rt_sj7 = int(cells_rt_sj7.loc[cells_rt_sj7["slot"] == 18, "n"].iloc[0])
        check("SJ-7e end-to-endでslot18のn=3が一致(scan_job書出→app.py読込)",
              n_rt_sj7 == 3 and meta_rt_sj7.get("calendar_freq_minutes") == 30,
              f"n={n_rt_sj7} meta={meta_rt_sj7}")
        missing_sj7, _ = app.calendar_cells_snapshot_lookup(loaded_sj7, "ETH", None, 30)
        check("SJ-7f 未収録銘柄(ETH)の照会は空", missing_sj7.empty)

    empty_snap_sj7 = build_calendar_cells_snapshot(
        symbols=["ETH"], freqs=[30], months=[None], load_fn=lambda label: pd.DataFrame(columns=app.SCAN_1M_COLS))
    check("SJ-7g 空の1分足(load_fn)を渡した銘柄はfreq_jsonが空dictで書出クラッシュしない",
          empty_snap_sj7["symbols"]["ETH"] == {}, f"got={empty_snap_sj7['symbols']['ETH']}")

    print("\n" + "=" * 78)
    if all_ok:
        print("SCAN_JOB SELFTEST: ALL PASS")
    else:
        print("SCAN_JOB SELFTEST: FAILED")
        for d in fail_details:
            print(f"  - {d}")
    print("=" * 78)
    return all_ok


if __name__ == "__main__":
    main()
