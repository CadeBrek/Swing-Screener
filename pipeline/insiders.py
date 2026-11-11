"""Insider read from SEC EDGAR Form 4 filings (free, official).

Implements the screener's v1 insider-flag rules exactly:
  Strong buy  2+ distinct insiders made open-market purchases, OR a CEO/CFO/President bought >= $100k total
  Buy         otherwise at least 1 insider bought
  Caution     no buys, and 3+ distinct officers/directors made open-market sales totalling > $10M,
              or a CEO/CFO sold > 25% of their holdings
  Neutral     everything else
  Unavailable the lookup failed or returned nothing usable (never guessed)
Open-market purchase = code P with price > 0. Grants (A), exercises (M), tax withholding (F),
gifts (G) and zero-price rows are ignored. Information only: never changes score, rank, entry or stop.
"""
import time, datetime as dt, xml.etree.ElementTree as ET
import requests

UA = {"User-Agent": "CadeBrek swing-screener CadeBrek@users.noreply.github.com"}
_ticker_map = None


def _get(url, **kw):
    for attempt in range(3):
        r = requests.get(url, headers=UA, timeout=30, **kw)
        time.sleep(0.15)  # SEC asks for <= 10 requests a second
        if r.status_code == 200:
            return r
        if r.status_code in (429, 503):
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
    r.raise_for_status()


def cik_for(sym):
    global _ticker_map
    if _ticker_map is None:
        js = _get("https://www.sec.gov/files/company_tickers.json").json()
        _ticker_map = {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in js.values()}
    return _ticker_map.get(sym.upper().replace(".", "-"))


def _txt(node, p):
    x = node.find(p)
    return x.text.strip() if x is not None and x.text else None


def _num(node, p):
    t = _txt(node, p)
    try:
        return float(t) if t is not None else None
    except ValueError:
        return None


def _parse_form4(xml_text):
    root = ET.fromstring(xml_text)
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        title = (_txt(rel, "officerTitle") or "") if rel is not None else ""
        is_dir = (_txt(rel, "isDirector") or "0") in ("1", "true") if rel is not None else False
        is_off = (_txt(rel, "isOfficer") or "0") in ("1", "true") if rel is not None else False
        owners.append({"name": _txt(o, "reportingOwnerId/rptOwnerName") or "?", "title": title,
                       "insider": is_dir or is_off})
    txns = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        txns.append({
            "date": _txt(t, "transactionDate/value"),
            "code": _txt(t, "transactionCoding/transactionCode"),
            "shares": _num(t, "transactionAmounts/transactionShares/value") or 0.0,
            "price": _num(t, "transactionAmounts/transactionPricePerShare/value") or 0.0,
            "after": _num(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
        })
    return owners, txns


def _role(title):
    t = title.upper()
    if "CHIEF EXECUTIVE" in t or "CEO" in t.split() or t.startswith("CEO"):
        return "CEO"
    if "CHIEF FINANCIAL" in t or "CFO" in t:
        return "CFO"
    if "PRESIDENT" in t and "VICE" not in t:
        return "President"
    return None


def _money(x):
    return f"${x/1e6:.1f}M" if x >= 1e6 else f"${x/1e3:.0f}k"


def insider_read(sym, today):
    """Returns dict(insider_flag, insider_buys_90d, insider_note); on failure also "_error" (the reason)."""
    unavailable = {"insider_flag": "Unavailable", "insider_buys_90d": None,
                   "insider_note": "Insider data unavailable this week."}
    form_errors = []
    try:
        cik = cik_for(sym)
        if not cik:
            return {**unavailable, "_error": "no SEC CIK for this ticker"}
        sub = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
        rec = sub["filings"]["recent"]
        since = today - dt.timedelta(days=90)
        buys, sells = {}, {}   # name -> {"value", "role", "shares", "after"}
        n_forms = 0
        for form, fdate, acc, doc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"]):
            if form != "4":
                continue
            if dt.date.fromisoformat(fdate) < since:
                break  # newest first
            raw = doc.split("/")[-1]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{raw}"
            try:
                owners, txns = _parse_form4(_get(url).text)
            except Exception as e:
                form_errors.append(f"{type(e).__name__}: {e}"[:160])
                continue
            n_forms += 1
            if not owners:
                continue
            o = owners[0]
            role = _role(o["title"])
            for t in txns:
                if not t["date"] or dt.date.fromisoformat(t["date"][:10]) < since:
                    continue
                if t["code"] == "P" and t["price"] > 0:
                    b = buys.setdefault(o["name"], {"value": 0.0, "role": role, "dates": set()})
                    b["value"] += t["shares"] * t["price"]; b["dates"].add(t["date"][:10])
                elif t["code"] == "S" and t["price"] > 0 and o["insider"]:
                    s = sells.setdefault(o["name"], {"value": 0.0, "role": role, "shares": 0.0, "after": None,
                                                      "after_date": "", "dates": set()})
                    s["value"] += t["shares"] * t["price"]; s["shares"] += t["shares"]; s["dates"].add(t["date"][:10])
                    if t["after"] is not None and t["date"][:10] >= s["after_date"]:
                        s["after"], s["after_date"] = t["after"], t["date"][:10]  # holdings after the latest sale
        if n_forms == 0 and form_errors:
            return {**unavailable, "_error": "every Form 4 failed to load, e.g. " + form_errors[0]}
    except Exception as e:
        return {**unavailable, "_error": f"{type(e).__name__}: {e}"[:200]}

    n_buyers = len(buys)
    top_buy = any(b["role"] in ("CEO", "CFO", "President") and b["value"] >= 100_000 for b in buys.values())
    sell_total = sum(s["value"] for s in sells.values())
    big_exec_sale = any(s["role"] in ("CEO", "CFO") and s["after"] is not None and
                        s["shares"] / (s["after"] + s["shares"]) > 0.25 for s in sells.values() if s["shares"])
    if n_buyers >= 2 or top_buy:
        flag = "Strong buy"
    elif n_buyers == 1:
        flag = "Buy"
    elif (len(sells) >= 3 and sell_total > 10_000_000) or big_exec_sale:
        flag = "Caution"
    else:
        flag = "Neutral"

    def who(name, d):
        r = d["role"] or "insider"
        return f"{r} {name.title()}"
    if buys:
        parts = [f"{who(n, b)} bought {_money(b['value'])} ({min(b['dates'])})" for n, b in
                 sorted(buys.items(), key=lambda kv: -kv[1]["value"])[:3]]
        note = "; ".join(parts) + "."
    elif sells:
        note = (f"No open-market buys in 90 days; {len(sells)} officer/director seller(s) sold about "
                f"{_money(sell_total)} in total.")
    else:
        note = "No open-market buys or sales by insiders in 90 days."
    return {"insider_flag": flag, "insider_buys_90d": n_buyers, "insider_note": note}
