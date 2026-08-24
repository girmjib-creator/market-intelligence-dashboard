# -*- coding: utf-8 -*-
"""
관심종목 시장 인텔리전스 대시보드 자동 빌드 스크립트.
GitHub Actions에서 실행되어 index.html을 생성한다.
- 시세: FinanceDataReader (한국/미국), 보조로 yfinance (지수/원자재/VIX)
- 뉴스: Google News RSS (when:1d, KST 오늘 발행분, 종목당 3건, 없으면 '없음')
- 번역: deep-translator (미국 헤드라인 → 한글)
모든 외부 호출은 try/except로 감싸 실패해도 '—'로 표기하고 계속 진행한다.
"""
import datetime, urllib.parse, html, sys

KST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime.now(KST)
TODAY = NOW.date()

# ------------------------------------------------------------------ 관심종목 설정
# (name, code_or_ticker, market)  market: 'KR' | 'US'
GROUPS = [
    ("해외 · 에너지 · 전력", [
        ("GE베르노바", "GEV", "US"),
        ("블룸에너지", "BE", "US"),
        ("두산에너빌리티", "034020", "KR"),
        ("한국전력", "015760", "KR"),
    ]),
    ("반도체 · 기타", [
        ("삼성전자", "005930", "KR"),
        ("SK하이닉스", "000660", "KR"),
        ("LG전자", "066570", "KR"),
        ("엔알비", "475230", "KR"),
        ("퓨쳐켐", "220100", "KR"),
    ]),
]
# 뉴스 검색어 (종목명 → 쿼리, 언어, 지역)
NEWS_Q = {
    "삼성전자": ("삼성전자 주가", "ko", "KR"),
    "SK하이닉스": ("SK하이닉스 주가", "ko", "KR"),
    "두산에너빌리티": ("두산에너빌리티", "ko", "KR"),
    "한국전력": ("한국전력", "ko", "KR"),
    "LG전자": ("LG전자 주가", "ko", "KR"),
    "엔알비": ("엔알비 NRB 모듈러", "ko", "KR"),
    "퓨쳐켐": ("퓨쳐켐", "ko", "KR"),
    "GE베르노바": ("GE Vernova stock", "en", "US"),
    "블룸에너지": ("Bloom Energy stock", "en", "US"),
}

# ------------------------------------------------------------------ 데이터 수집
def pct_from_closes(df):
    try:
        c = df["Close"].dropna()
        if len(c) < 2:
            return float(c.iloc[-1]), None
        return float(c.iloc[-1]), (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100.0
    except Exception:
        return None, None

def get_quote(code, market):
    """(price, pct) 반환. 실패 시 (None, None)."""
    # 1) FinanceDataReader
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(code)
        p, r = pct_from_closes(df)
        if p is not None:
            return p, r
    except Exception:
        pass
    # 2) yfinance (미국/지수 백업)
    try:
        import yfinance as yf
        t = yf.Ticker(code if market == "US" else code)
        h = t.history(period="5d")
        if len(h):
            last = float(h["Close"].iloc[-1])
            prev = float(h["Close"].iloc[-2]) if len(h) > 1 else last
            return last, (last - prev) / prev * 100.0 if prev else None
    except Exception:
        pass
    return None, None

def get_index(candidates):
    """여러 심볼 후보를 순서대로 시도."""
    for sym in candidates:
        p, r = get_quote(sym, "US")
        if p is not None:
            return p, r
    return None, None

def fetch_news(query, lang, gl, limit=3):
    """Google News RSS, KST 오늘 발행분만."""
    try:
        import feedparser
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(query + " when:1d")
               + f"&hl={lang}&gl={gl}&ceid={gl}:{lang}")
        feed = feedparser.parse(url)
    except Exception:
        return []
    out = []
    for e in feed.entries:
        try:
            pub = datetime.datetime(*e.published_parsed[:6],
                                    tzinfo=datetime.timezone.utc).astimezone(KST)
        except Exception:
            continue
        if pub.date() != TODAY:
            continue
        title = e.title
        src = ""
        if " - " in title:
            title, src = title.rsplit(" - ", 1)
        out.append({"time": pub.strftime("%H:%M"), "title": title.strip(),
                    "src": src.strip(), "link": e.link})
        if len(out) >= limit:
            break
    out.sort(key=lambda x: x["time"], reverse=True)
    return out

_translator = None
def to_ko(text):
    global _translator
    try:
        from deep_translator import GoogleTranslator
        if _translator is None:
            _translator = GoogleTranslator(source="auto", target="ko")
        return _translator.translate(text)
    except Exception:
        return text  # 실패 시 원문 유지

# ------------------------------------------------------------------ 포맷 헬퍼
def fmt_price(p, market):
    if p is None:
        return "—"
    if market == "US":
        return "$" + format(p, ",.2f")
    return format(int(round(p)), ",")

def fmt_pct(r):
    if r is None:
        return ("flat", "—")
    sign = "+" if r > 0 else ("−" if r < 0 else "")
    cls = "up" if r > 0 else ("down" if r < 0 else "flat")
    return (cls, f"{sign}{abs(r):.2f}%")

def esc(s):
    return html.escape(s or "")

# ------------------------------------------------------------------ 수집 실행
quotes = {}
for _, items in GROUPS:
    for name, code, market in items:
        quotes[name] = (get_quote(code, market), market, code)

idx = {
    "코스피": get_index(["KS11"]),
    "코스닥": get_index(["KQ11"]),
    "S&P 500": get_index(["US500", "^GSPC"]),
    "나스닥": get_index(["IXIC", "^IXIC"]),
    "다우": get_index(["DJI", "^DJI"]),
    "VIX": get_index(["^VIX", "VIX"]),
}
macro = {
    "USD/KRW": (get_index(["USD/KRW", "KRW=X"]), ""),
    "WTI 유가": (get_index(["CL=F"]), ""),
    "금(Gold)": (get_index(["GC=F"]), ""),
}

news = {}
for name in quotes:
    q, lang, gl = NEWS_Q.get(name, (name, "ko", "KR"))
    arts = fetch_news(q, lang, gl)
    if gl == "US":
        for a in arts:
            a["title"] = to_ko(a["title"])
    news[name] = arts

# 데이터 기반 핵심/포인트
def biggest(direction):
    best = None
    for name, ((p, r), mkt, code) in quotes.items():
        if r is None:
            continue
        if best is None or (r > best[1] if direction == "up" else r < best[1]):
            best = (name, r)
    return best

top_up = biggest("up")
top_dn = biggest("down")
news_cnt = sum(len(v) for v in news.values())
kospi = idx["코스피"]
kospi_txt = ""
if kospi and kospi[0] is not None:
    kc, kt = fmt_pct(kospi[1])
    kospi_txt = f"코스피 {format(int(round(kospi[0])),',')}({kt}) · "
headline = kospi_txt
if top_up:
    headline += f"최대 상승 {esc(top_up[0])} +{abs(top_up[1]):.1f}% · "
if top_dn:
    headline += f"최대 하락 {esc(top_dn[0])} −{abs(top_dn[1]):.1f}% · "
headline += f"오늘 관련 기사 {news_cnt}건 수집"

points = []
for name, ((p, r), mkt, code) in quotes.items():
    flags = []
    if r is not None and abs(r) >= 3:
        flags.append(f"{'급등' if r>0 else '급락'} {r:+.1f}%")
    if news.get(name):
        flags.append(f"신규뉴스 {len(news[name])}건")
    if flags:
        points.append((name, " · ".join(flags)))

# ------------------------------------------------------------------ HTML 렌더
CSS = """
:root{--ground:#0E1420;--surface:#161E2C;--surface-2:#1C2636;--border:#28344A;--border-soft:#20293A;--text:#E7ECF4;--muted:#8A96A9;--faint:#5C6A80;--accent:#E3A94A;--accent-soft:rgba(227,169,74,.14);--up:#F0616D;--down:#4D93F0;--flat:#8A96A9;--up-soft:rgba(240,97,109,.13);--down-soft:rgba(77,147,240,.13);--shadow:0 1px 0 rgba(255,255,255,.03) inset,0 8px 24px -12px rgba(0,0,0,.6);--sans:"IBM Plex Sans KR",system-ui,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace}
:root:not([data-theme="dark"]){--ground:#F3F5F9;--surface:#FFFFFF;--surface-2:#F7F9FC;--border:#DDE3EC;--border-soft:#E8ECF3;--text:#141C2A;--muted:#5D6B80;--faint:#9AA6B8;--accent:#B57E1E;--accent-soft:rgba(181,126,30,.10);--up:#D23948;--down:#2C6BD4;--flat:#5D6B80;--up-soft:rgba(210,57,72,.09);--down-soft:rgba(44,107,212,.09);--shadow:0 1px 2px rgba(20,28,42,.04),0 10px 26px -16px rgba(20,28,42,.28)}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){--ground:#F3F5F9;--surface:#FFFFFF;--surface-2:#F7F9FC;--border:#DDE3EC;--border-soft:#E8ECF3;--text:#141C2A;--muted:#5D6B80;--faint:#9AA6B8;--accent:#B57E1E;--accent-soft:rgba(181,126,30,.10);--up:#D23948;--down:#2C6BD4;--flat:#5D6B80;--up-soft:rgba(210,57,72,.09);--down-soft:rgba(44,107,212,.09)}}
:root[data-theme="dark"]{--ground:#0E1420;--surface:#161E2C;--surface-2:#1C2636;--border:#28344A;--border-soft:#20293A;--text:#E7ECF4;--muted:#8A96A9;--faint:#5C6A80;--accent:#E3A94A;--accent-soft:rgba(227,169,74,.14);--up:#F0616D;--down:#4D93F0;--flat:#8A96A9;--up-soft:rgba(240,97,109,.13);--down-soft:rgba(77,147,240,.13)}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ground);color:var(--text);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased;padding:0 20px 64px;font-size:15px}
.wrap{max-width:960px;margin:0 auto}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--flat)}a{color:inherit}
header{position:sticky;top:0;z-index:5;background:var(--ground);padding:24px 0 16px;margin-bottom:6px;border-bottom:1px solid var(--border-soft)}
.kicker{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:9px}
h1{font-size:clamp(23px,4.2vw,32px);font-weight:700;letter-spacing:-.01em}
.stamp{margin-top:11px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;font-size:12.5px;color:var(--muted)}
.stamp .dot{width:5px;height:5px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:7px}
.stamp b{color:var(--text);font-weight:600}
section{margin-top:28px}
.sec-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:13px}
.sec-title{font-size:13px;font-weight:600;letter-spacing:.03em;color:var(--text);display:flex;align-items:center;gap:8px}
.sec-title::before{content:"";width:3px;height:14px;background:var(--accent);border-radius:2px;display:inline-block}
.sec-note{font-size:11.5px;color:var(--muted)}
.headline{background:linear-gradient(180deg,var(--accent-soft),transparent);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:14px;padding:18px 20px;box-shadow:var(--shadow);margin-top:22px}
.headline .tag{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.headline p{font-size:16px;line-height:1.6;font-weight:500;margin-top:9px}
.points{display:flex;flex-wrap:wrap;gap:8px}
.point{font-size:12.5px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:6px 13px;box-shadow:var(--shadow)}
.point b{font-weight:700}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:720px){.grid{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:13px;padding:15px;box-shadow:var(--shadow);position:relative;overflow:hidden}
.card .label{font-size:12px;color:var(--muted);font-weight:500}
.card .val{font-size:22px;font-weight:600;margin-top:9px}
.card .chg{font-size:12.5px;font-weight:500;margin-top:3px}
.card .rail{position:absolute;left:0;top:0;bottom:0;width:3px}
.card.up .rail{background:var(--up)}.card.down .rail{background:var(--down)}.card.flat .rail{background:var(--flat)}
.macro{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:720px){.macro{grid-template-columns:repeat(2,1fr)}}
.chip{background:var(--surface-2);border:1px solid var(--border-soft);border-radius:11px;padding:12px 13px}
.chip .label{font-size:11.5px;color:var(--muted)}
.chip .val{font-size:17px;font-weight:600;margin-top:5px}
.chip .chg{font-size:11.5px;margin-top:2px;font-weight:500}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}
.tbl-scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:460px}
thead th{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:600;text-align:right;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface-2)}
thead th:first-child{text-align:left}
tbody td{padding:12px 16px;border-bottom:1px solid var(--border-soft);text-align:right;font-size:14.5px}
tbody tr:last-child td{border-bottom:0}
tbody td:first-child{text-align:left}
.nm{font-weight:600}
.tick{font-size:11.5px;color:var(--faint);font-family:var(--mono);margin-left:7px}
.grp td{background:var(--surface-2);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600;padding:9px 16px;text-align:left}
.pct{display:inline-block;min-width:62px;padding:2px 8px;border-radius:6px;font-weight:600;font-size:13px}
.pct.up{background:var(--up-soft)}.pct.down{background:var(--down-soft)}.pct.flat{background:var(--surface-2);color:var(--muted)}
.stk{background:var(--surface);border:1px solid var(--border);border-radius:13px;box-shadow:var(--shadow);overflow:hidden;margin-bottom:10px}
.stk .sh{display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--surface-2);border-bottom:1px solid var(--border-soft)}
.stk .sh .snm{font-weight:700;font-size:14px}
.stk .sh .stk-tick{font-size:11px;color:var(--faint);font-family:var(--mono)}
.stk .sh .smv{margin-left:auto;font-family:var(--mono);font-weight:600;font-size:13px}
.stk ul{list-style:none;padding:6px 0}
.stk li a{display:flex;gap:11px;padding:10px 16px;text-decoration:none;align-items:baseline}
.stk li a:hover{background:var(--surface-2)}
.stk li .tm{flex:none;font-family:var(--mono);font-size:11.5px;color:var(--accent);font-weight:600;width:42px}
.stk li .ht{font-size:13.5px;line-height:1.5;color:var(--text)}
.stk li .so{font-size:11px;color:var(--muted);white-space:nowrap}
.stk .none{padding:14px 16px;font-size:13px;color:var(--faint)}
.badge-en{font-size:10px;font-weight:700;color:var(--down);background:var(--down-soft);border-radius:4px;padding:1px 6px;margin-left:2px}
footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--border-soft);font-size:11.5px;color:var(--faint);line-height:1.7}
"""

def card(label, pair, market="US"):
    (p, r) = pair if pair else (None, None)
    cls, txt = fmt_pct(r)
    val = fmt_price(p, market) if label in ("VIX",) or market == "US" else fmt_price(p, "KR")
    if label in ("코스피", "코스닥"):
        val = fmt_price(p, "KR")
    if label == "VIX":
        val = format(p, ",.2f") if p is not None else "—"
    return f'<div class="card {cls}"><div class="rail"></div><div class="label">{esc(label)}</div><div class="val num">{val}</div><div class="chg {cls}">{txt}</div></div>'

indices_html = "".join([
    card("S&P 500", idx["S&P 500"]), card("나스닥", idx["나스닥"]), card("다우", idx["다우"]),
    card("코스피", idx["코스피"]), card("코스닥", idx["코스닥"]), card("VIX", idx["VIX"]),
])

def chip(label, pair):
    (p, r) = pair if pair else (None, None)
    cls, txt = fmt_pct(r)
    if p is None:
        val = "—"
    elif label == "USD/KRW":
        val = format(p, ",.2f")
    else:
        val = format(p, ",.2f")
    return f'<div class="chip"><div class="label">{esc(label)}</div><div class="val num">{val}</div><div class="chg {cls}">{txt}</div></div>'

macro_html = "".join([chip(k, v[0]) for k, v in macro.items()])

rows = []
for gname, items in GROUPS:
    rows.append(f'<tr class="grp"><td colspan="3">{esc(gname)}</td></tr>')
    for name, code, market in items:
        (p, r), mkt, _ = quotes[name]
        cls, txt = fmt_pct(r)
        rows.append(f'<tr><td class="nm">{esc(name)}<span class="tick">{esc(code)}</span></td>'
                    f'<td class="num">{fmt_price(p, market)}</td>'
                    f'<td><span class="pct {cls} num">{txt}</span></td></tr>')
watchlist_html = "".join(rows)

news_blocks = []
for gname, items in GROUPS:
    for name, code, market in items:
        (p, r), mkt, _ = quotes[name]
        cls, txt = fmt_pct(r)
        en = '<span class="badge-en">번역</span>' if market == "US" else ""
        head = (f'<div class="sh"><span class="snm">{esc(name)}</span>'
                f'<span class="stk-tick">{esc(code)}</span>{en}'
                f'<span class="smv {cls}">{txt}</span></div>')
        arts = news.get(name, [])
        if arts:
            lis = "".join([
                f'<li><a href="{esc(a["link"])}" target="_blank" rel="noopener">'
                f'<span class="tm">{esc(a["time"])}</span>'
                f'<span><span class="ht">{esc(a["title"])}</span> '
                f'<span class="so">· {esc(a["src"])}</span></span></a></li>'
                for a in arts])
            body = f"<ul>{lis}</ul>"
        else:
            body = '<div class="none">— 오늘(KST) 발행된 관련 기사 없음</div>'
        news_blocks.append(f'<div class="stk">{head}{body}</div>')
news_html = "".join(news_blocks)

points_html = "".join([f'<span class="point"><b>{esc(n)}</b> {esc(d)}</span>' for n, d in points]) \
    or '<span class="point">특이 변동·신규 뉴스 없음</span>'

stamp = NOW.strftime("%Y. %m. %d (%a) %H:%M KST")

PAGE = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>아침 시장 인텔리전스</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body><div class="wrap">
<header>
  <div class="kicker">Morning Market Intelligence</div>
  <h1>아침 시장 인텔리전스</h1>
  <div class="stamp">
    <span><span class="dot"></span>마지막 업데이트 <b>{esc(stamp)}</b></span>
    <span>뉴스 · <b>오늘 발행분(KST)</b></span>
    <span>🔄 <b>평일 07·14시 자동 갱신</b></span>
  </div>
</header>
<div class="headline"><span class="tag">오늘의 핵심</span><p>{esc(headline)}</p></div>
<section><div class="sec-head"><div class="sec-title">오늘의 관찰 포인트</div><div class="sec-note">±3% 이상 변동·신규 뉴스 자동 감지</div></div><div class="points">{points_html}</div></section>
<section><div class="sec-head"><div class="sec-title">주요 지수</div><div class="sec-note">상승 <span class="up">빨강</span> · 하락 <span class="down">파랑</span></div></div><div class="grid">{indices_html}</div></section>
<section><div class="sec-head"><div class="sec-title">매크로 · 환율 · 원자재</div></div><div class="macro">{macro_html}</div></section>
<section><div class="sec-head"><div class="sec-title">관심종목 시세</div><div class="sec-note">관심종목 {sum(len(i) for _,i in GROUPS)}종</div></div><div class="panel"><div class="tbl-scroll"><table><thead><tr><th>종목</th><th>현재가</th><th>등락</th></tr></thead><tbody>{watchlist_html}</tbody></table></div></div></section>
<section><div class="sec-head"><div class="sec-title">종목별 오늘자 뉴스</div><div class="sec-note">KST 오늘 발행분 · 종목당 최대 3건 · 클릭 시 원문</div></div>{news_html}</section>
<footer>이 페이지는 GitHub Actions가 평일 오전 7시·오후 2시(KST)에 <b>자동 갱신</b>합니다. 고정 URL이라 기존 링크로 항상 최신 화면이 열리고, 갱신 이력은 GitHub 커밋으로 누적 보관됩니다.<br>
시세는 조회 시점 값(한국 당일·미국 직전 거래일 종가), 뉴스는 Google News RSS 오늘(KST) 발행분, 미국 헤드라인은 자동 번역본입니다. 데이터·번역 오류 가능성이 있으므로 투자판단 전 원문·원자료를 반드시 재확인하십시오. 본 페이지는 정보 제공용이며 최종 투자판단·손익은 본인 책임입니다.</footer>
</div></body></html>"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(PAGE)
print(f"built index.html  ({len(PAGE)} bytes)  news={news_cnt}  points={len(points)}")
