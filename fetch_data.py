#!/usr/bin/env python3
"""
fetch_data.py — 從證交所（上市）與櫃買中心（上櫃）抓月底收盤價與除權息資料，
產生 data.js 給 index.html 使用。

用法：
    pip install requests
    python fetch_data.py                 # 第一次約 20~40 分鐘，之後只補新月份
    python fetch_data.py --start 2010-01 # 只抓 2010 年之後
    python fetch_data.py --rebuild       # 不抓網路，只用快取重建 data.js

說明：
- 原始回應會存在 cache/ 資料夾，已抓過的日期不會再抓，中斷後重跑會接續。
- 證交所會封鎖抓太快的 IP，所以每次請求間隔約 3 秒，請耐心等。
- 輸出 data.js 是「原始股價 + 每月現金股利 + 股數乘數（配股／分割）」，
  網頁端用這三樣東西模擬實際持股，所以除息、分割都會正確反映在總報酬裡。
"""
import argparse
import calendar
import datetime as dt
import json
import random
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("請先安裝 requests：pip install requests")

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
DELAY = {"twse": 3.2, "tpex": 2.0}
TODAY = dt.date.today()
_last_call = {"twse": 0.0, "tpex": 0.0}
_session = requests.Session()
_session.headers.update(UA)
TPEX_INSECURE = False


# ---------------------------------------------------------------- 小工具
LOG_FILE = HERE / "fetch_log.txt"


def log(*a):
    msg = " ".join(str(x) for x in a)
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # 舊版 Windows 主控台
        print(msg.encode("ascii", "replace").decode(), flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def num(s):
    """'1,234.5' -> 1234.5；'--'、''、'X' -> None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    if s in ("", "-", "--", "---", "----", "X", "除權息", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s):
    """支援 '113年01月02日'、'113/01/02'、'2024/01/02'、'20240102'"""
    s = re.sub(r"<[^>]+>", "", str(s)).strip()
    nums = re.findall(r"\d+", s)
    if len(nums) == 1 and len(nums[0]) == 8:
        y, m, d = int(nums[0][:4]), int(nums[0][4:6]), int(nums[0][6:])
    elif len(nums) >= 3:
        y, m, d = int(nums[0]), int(nums[1]), int(nums[2])
        if y < 1911:
            y += 1911
    else:
        return None
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def field_index(fields, *cands, exclude=()):
    """在欄位名稱裡找第一個符合的欄位（先完全相符，再部分相符）"""
    clean = [re.sub(r"\s|<[^>]+>", "", str(f)) for f in fields]
    for c in cands:
        for i, f in enumerate(clean):
            if f == c:
                return i
    for c in cands:
        for i, f in enumerate(clean):
            if c in f and not any(x in f for x in exclude):
                return i
    return None


def iter_tables(js):
    """證交所/櫃買的 JSON 有新舊兩種格式：tables[] 或 fieldsN/dataN，統一成 (fields, rows)"""
    if not isinstance(js, dict):
        return
    for t in js.get("tables") or []:
        if isinstance(t, dict) and t.get("fields") and t.get("data") is not None:
            yield t["fields"], t["data"]
    if js.get("fields") and js.get("data") is not None:
        yield js["fields"], js["data"]
    for k, v in js.items():
        m = re.fullmatch(r"fields(\d+)", k)
        if m and js.get("data" + m.group(1)) is not None:
            yield v, js["data" + m.group(1)]
    if js.get("aaData") is not None:  # 櫃買舊版
        yield ["代號", "名稱", "收盤"], js["aaData"]


def get_json(market, url, params):
    """有節流、重試的 GET。回傳 dict，失敗丟例外。"""
    tries = 12 if market == "tpex" else 6
    for attempt in range(1, tries + 1):
        wait = DELAY[market] + random.uniform(0, 1.0) - (time.time() - _last_call[market])
        if wait > 0:
            time.sleep(wait)
        _last_call[market] = time.time()
        try:
            r = _session.get(url, params=params, timeout=30,
                             verify=not (market == "tpex" and TPEX_INSECURE))
            if r.status_code in (403, 429, 503) or "text/html" in r.headers.get("Content-Type", ""):
                backoff = 30 * attempt
                log(f"    ⚠ {market} 回應 {r.status_code}（可能被暫時封鎖），{backoff} 秒後重試…")
                time.sleep(backoff)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError as e:
            # 櫃買中心部分主機的憑證鏈不完整，多試幾次通常會碰到正常的主機
            if attempt == tries:
                raise RuntimeError(
                    f"{market} SSL 驗證一直失敗：{e}\n"
                    "  櫃買中心有些主機漏送中繼憑證。可以稍後重跑（已抓的會保留），"
                    "或加上 --tpex-insecure 略過櫃買中心的憑證驗證（只影響櫃買中心的公開行情資料）。")
            time.sleep(1)
        except (requests.exceptions.RequestException, ValueError) as e:
            if attempt == tries:
                raise
            log(f"    ⚠ {market} 請求失敗（{e.__class__.__name__}），{5 * attempt} 秒後重試…")
            time.sleep(5 * attempt)
    raise RuntimeError(f"{market} 多次重試仍失敗：{url} {params}")


def resp_date_mismatch(js, want):
    """有些端點查假日會回傳前一個交易日的資料，用回應裡的日期確認"""
    for key in ("date", "reportDate"):
        if isinstance(js, dict) and js.get(key):
            d = parse_date(js[key])
            if d and d != want:
                return True
    return False


# ---------------------------------------------------------------- 每日全市場收盤
def cache_path(kind, name):
    p = CACHE / kind / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def fetch_day(market, d):
    """回傳 {code: [name, close]}；非交易日回傳 {}。結果會快取。"""
    p = cache_path(f"{market}_day", d.strftime("%Y%m%d") + ".json")
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    if market == "twse":
        js = get_json("twse", "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                      {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    else:
        js = get_json("tpex", "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                      {"date": d.strftime("%Y/%m/%d"), "type": "EW", "response": "json"})
    out = {}
    if not resp_date_mismatch(js, d):
        for fields, rows in iter_tables(js):
            ic = field_index(fields, "證券代號", "代號")
            iclose = field_index(fields, "收盤價", "收盤")
            iname = field_index(fields, "證券名稱", "名稱")
            if ic is None or iclose is None:
                continue
            for row in rows:
                if not isinstance(row, list) or len(row) <= max(ic, iclose):
                    continue
                code = re.sub(r"<[^>]+>", "", str(row[ic])).strip()
                name = str(row[iname]).strip() if iname is not None and iname < len(row) else ""
                out[code] = [name, num(row[iclose])]
    # 太近的日期（可能還沒收盤）不快取空結果
    if out or (TODAY - d).days > 3:
        p.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    return out


def month_end(market, y, m):
    """找出該月最後一個有資料的交易日。回傳 (date, quotes) 或 (None, {})"""
    d = dt.date(y, m, calendar.monthrange(y, m)[1])
    if d > TODAY:
        d = TODAY
    tried = 0
    while d.month == m and tried < 9:
        if d.weekday() < 5:
            q = fetch_day(market, d)
            tried += 1
            if q:
                return d, q
        d -= dt.timedelta(days=1)
    return None, {}


# ---------------------------------------------------------------- 除權息
def fetch_ex_year(market, year):
    """回傳該年除權息事件 list[dict]（每年一個快取檔，今年每次重抓）"""
    p = cache_path(f"{market}_ex", f"{year}.json")
    if p.exists() and year < TODAY.year:
        return json.loads(p.read_text("utf-8"))
    events = []
    # 先試整年，失敗再改用每季
    ranges = [(dt.date(year, 1, 1), dt.date(year, 12, 31))]
    for attempt in range(2):
        events, ok = [], True
        for a, b in ranges:
            b = min(b, TODAY)
            if a > b:
                continue
            try:
                if market == "twse":
                    js = get_json("twse", "https://www.twse.com.tw/rwd/zh/exRight/TWT49U",
                                  {"startDate": a.strftime("%Y%m%d"), "endDate": b.strftime("%Y%m%d"),
                                   "response": "json"})
                else:
                    js = get_json("tpex", "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
                                  {"startDate": a.strftime("%Y/%m/%d"), "endDate": b.strftime("%Y/%m/%d"),
                                   "response": "json"})
            except Exception as e:  # noqa
                log(f"    ⚠ {market} {year} 除權息抓取失敗：{e}")
                ok = False
                break
            events += parse_ex(js)
        if ok and (events or attempt == 1):
            break
        ranges = [(dt.date(year, q * 3 + 1, 1),
                   dt.date(year, q * 3 + 3, calendar.monthrange(year, q * 3 + 3)[1])) for q in range(4)]
    p.write_text(json.dumps(events, ensure_ascii=False), "utf-8")
    return events


def parse_ex(js):
    out = []
    for fields, rows in iter_tables(js):
        idate = field_index(fields, "資料日期", "除權息日期", "除權除息日期", "日期")
        icode = field_index(fields, "股票代號", "代號", "證券代號")
        iprev = field_index(fields, "除權息前收盤價", "前收盤")
        iref = field_index(fields, "除權息參考價", "參考價", exclude=("減除",))
        ikind = field_index(fields, "權/息", "權或息")
        icash = field_index(fields, "息值", "現金股利")
        iboth = field_index(fields, "權值+息值", "權值加息值")
        if None in (idate, icode, iprev, iref):
            continue
        for row in rows:
            try:
                d = parse_date(row[idate])
                prev, ref = num(row[iprev]), num(row[iref])
                if not d or not prev or not ref:
                    continue
                kind = re.sub(r"<[^>]+>|\s", "", str(row[ikind])) if ikind is not None else ""
                cash = num(row[icash]) if icash is not None else None
                both = num(row[iboth]) if iboth is not None else None
                out.append({"code": re.sub(r"<[^>]+>", "", str(row[icode])).strip(),
                            "date": d.isoformat(), "prev": prev, "ref": ref,
                            "kind": kind, "cash": cash, "both": both})
            except (IndexError, TypeError):
                continue
    return out


def event_effect(ev):
    """回傳 (每股現金股利, 股數乘數)"""
    prev, ref, raw = ev["prev"], ev["ref"], ev["kind"]
    kind = ("權息" if "權" in raw and "息" in raw else
            "權" if "權" in raw else "息" if "息" in raw else "")
    if kind == "息" or (kind == "" and ev.get("cash")):
        cash = ev["cash"] if ev.get("cash") is not None else (ev["both"] if ev.get("both") is not None else prev - ref)
        return max(cash, 0.0), 1.0
    if kind == "權":
        return 0.0, prev / ref
    # 權息：櫃買有分開的息值；證交所只有合計，現金部分無法拆開，整筆視為配股（總報酬不變）
    cash = ev.get("cash")
    if cash is not None and ev.get("both") is not None and cash != ev["both"]:
        return cash, (prev - cash) / ref
    if cash is not None and kind == "權息" and ev.get("both") is None:
        return cash, (prev - cash) / ref
    return 0.0, prev / ref


# ---------------------------------------------------------------- 主流程
def month_list(start, end):
    y, m = start
    out = []
    while (y, m) <= end:
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def main():
    global TPEX_INSECURE
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2004-02", help="起始月份，預設 2004-02（證交所全市場資料起點）")
    ap.add_argument("--rebuild", action="store_true", help="不連網，只用快取重建 data.js")
    ap.add_argument("--tpex-insecure", action="store_true", help="櫃買中心憑證鏈有問題時略過驗證")
    ap.add_argument("--out", default=str(HERE / "data.js"))
    args = ap.parse_args()
    TPEX_INSECURE = args.tpex_insecure
    if TPEX_INSECURE:
        import urllib3
        urllib3.disable_warnings()

    cfg = json.loads((HERE / "tickers.json").read_text("utf-8"))
    tickers = cfg["tickers"]
    split_cfg = cfg.get("splits", {})
    markets = sorted({t.get("market", "twse") for t in tickers})
    sy, sm = map(int, args.start.split("-"))
    months = month_list((sy, sm), (TODAY.year, TODAY.month))

    if args.rebuild:
        global fetch_day, fetch_ex_year

        def fetch_day(market, d):  # noqa
            p = CACHE / f"{market}_day" / (d.strftime("%Y%m%d") + ".json")
            return json.loads(p.read_text("utf-8")) if p.exists() else {}

        def fetch_ex_year(market, year):  # noqa
            p = CACHE / f"{market}_ex" / f"{year}.json"
            return json.loads(p.read_text("utf-8")) if p.exists() else []

    # 1) 月底收盤
    closes = {mk: {} for mk in markets}  # market -> {(y,m): (date, quotes)}
    for mk in markets:
        log(f"\n== {mk.upper()} 月底收盤（{len(months)} 個月）==")
        # 從最近的月份往回抓；連續 3 個月整個市場都沒資料，就視為該端點的歷史起點
        empty = 0
        for i, (y, m) in enumerate(reversed(months)):
            d, q = month_end(mk, y, m)
            closes[mk][(y, m)] = (d, q)
            empty = 0 if q else empty + 1
            if i % 12 == 0 or i == len(months) - 1:
                log(f"  {y}-{m:02d} {'✓ ' + d.isoformat() if d else '（無資料）'}  [{i + 1}/{len(months)}]")
            if empty >= 3 and i > 3:
                log(f"  {y}-{m:02d} 之前已無資料，停止往回抓")
                break
        for ym in months:
            closes[mk].setdefault(ym, (None, {}))

    # 2) 除權息
    ex = {mk: [] for mk in markets}
    for mk in markets:
        log(f"\n== {mk.upper()} 除權息 ==")
        for y in range(sy, TODAY.year + 1):
            evs = fetch_ex_year(mk, y)
            ex[mk] += evs
            log(f"  {y}: {len(evs)} 筆")

    # 3) 組裝每檔標的
    month_keys = [f"{y}-{m:02d}" for y, m in months]
    month_end_dates = []
    out_tickers, notes = {}, []
    for t in tickers:
        code, mk = t["code"], t.get("market", "twse")
        order = [mk] + [x for x in markets if x != mk]
        src = None
        for cand in order:
            if any(code in q for _, q in closes[cand].values()):
                src = cand
                break
        if src is None:
            msg = f"{code}：在{'/'.join(order)}都找不到資料，請確認代號或 market 設定"
            log("  ⚠ " + msg)
            notes.append(msg)
            continue
        if src != mk:
            notes.append(f"{code}：設定為 {mk} 但在 {src} 找到資料")
        name, close, last_dates = "", [], []
        last = None
        gap = 0
        started = False
        for (y, m) in months:
            d, q = closes[src][(y, m)]
            v = q.get(code, [None, None])
            if v[0]:
                name = v[0]
            px = v[1]
            if px is None and started and last is not None and gap < 2:
                px, gap = last, gap + 1  # 月底剛好沒成交或暫停交易：沿用上月，最多 2 個月
            elif px is not None:
                gap = 0
                started = True
            elif started:
                # 停止交易太久（下市或長期暫停）→ 之後不再有資料
                pass
            close.append(round(px, 4) if px is not None else None)
            last = px if px is not None else last
            last_dates.append(d.isoformat() if d else None)
        if not month_end_dates:
            month_end_dates = last_dates

        # 除權息 → 每月現金股利與股數乘數
        cash, mult = {}, {}
        idx_of = {k: i for i, k in enumerate(month_keys)}
        for ev in ex[src]:
            if ev["code"] != code:
                continue
            k = ev["date"][:7]
            if k not in idx_of:
                continue
            i = idx_of[k]
            c, f = event_effect(ev)
            if c:
                cash[i] = round(cash.get(i, 0) + c, 6)
            if abs(f - 1) > 1e-9:
                mult[i] = round(mult.get(i, 1) * f, 8)

        # 分割（設定檔）
        known = {}
        for s in split_cfg.get(code, []):
            if s["month"] in idx_of:
                known[idx_of[s["month"]]] = float(s["ratio"])
        for i, r in known.items():
            mult[i] = round(mult.get(i, 1) * r, 8)

        # 自動偵測疑似分割（月報酬極端且接近整數倍）
        for i in range(1, len(close)):
            a, b = close[i - 1], close[i]
            if not a or not b or i in known:
                continue
            total = (b * mult.get(i, 1) + cash.get(i, 0)) / a
            if 0.4 <= total <= 2.5:
                continue
            ratio = 1 / total if total < 1 else total
            n = round(ratio)
            if n >= 2 and abs(ratio / n - 1) < 0.25:
                f = n if total < 1 else 1 / n
                mult[i] = round(mult.get(i, 1) * f, 8)
                msg = (f"{code}：{month_keys[i]} 月報酬 {total - 1:+.0%}，自動視為"
                       f"{'分割 1→' + str(n) if total < 1 else '反分割 ' + str(n) + '→1'}，"
                       f"請對照公告確認後寫進 tickers.json 的 splits")
            else:
                msg = f"{code}：{month_keys[i]} 月報酬 {total - 1:+.0%} 異常但無法判斷是否為分割，請人工確認"
            log("  ⚠ " + msg)
            notes.append(msg)

        kind = "bond" if code.endswith("B") else ("etf" if code.startswith("00") else "stock")
        out_tickers[code] = {"name": name or code, "market": src, "kind": kind,
                             "proxy": t.get("proxy", ""), "close": close,
                             "cash": {str(k): v for k, v in sorted(cash.items())},
                             "mult": {str(k): v for k, v in sorted(mult.items())}}

        # 健檢：總報酬年化
        first = next((i for i, v in enumerate(close) if v), None)
        lastI = max((i for i, v in enumerate(close) if v), default=None)
        if first is not None and lastI and lastI - first >= 12:
            # 股利再投入的總報酬
            sh = 1.0
            for i in range(first + 1, lastI + 1):
                if close[i] is None:
                    continue
                c = cash.get(i, 0) * sh
                sh *= mult.get(i, 1)
                sh += c / close[i]
            yrs = (lastI - first) / 12
            cagr = (sh * close[lastI] / close[first]) ** (1 / yrs) - 1
            log(f"  {code:7s} {name[:10]:10s} {month_keys[first]}~{month_keys[lastI]}  含息年化 {cagr:+.1%}")

    data = {"generated": dt.datetime.now().isoformat(timespec="seconds"),
            "months": month_keys, "monthEndDates": month_end_dates,
            "tickers": out_tickers, "notes": notes}
    Path(args.out).write_text("window.STOCK_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n", "utf-8")
    log(f"\n完成：{args.out}（{len(out_tickers)} 檔）。用瀏覽器打開 index.html 即可。")
    if notes:
        log(f"有 {len(notes)} 則提醒，網頁上也看得到。")


if __name__ == "__main__":
    LOG_FILE.write_text(f"開始：{dt.datetime.now().isoformat(timespec='seconds')}\n", "utf-8")
    try:
        main()
    except KeyboardInterrupt:
        log("已中斷（已抓的資料都在 cache/，重跑會接續）")
    except Exception:
        import traceback
        log("發生錯誤：\n" + traceback.format_exc())
        sys.exit(1)
