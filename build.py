# -*- coding: utf-8 -*-
"""
관심종목 시장 인텔리전스 대시보드 + 누적 추적 빌드 스크립트 (GitHub Actions용).
생성물:
  - index.html   : 오늘 스냅샷 (시세/뉴스/핵심/관찰포인트)
  - trends.html  : 누적 추세(주가 스파크라인) + 종목별 이슈 타임라인
  - data/history.csv    : 실행마다 종목별 시세 1행씩 append (시계열 축적)
  - data/news_log.jsonl : 새로 등장한 뉴스만 append (링크 기준 dedup, 이슈 로그)
데이터: 시세 FinanceDataReader/yfinance · 뉴스 Google News RSS(when:1d) · 번역 deep-translator
모든 외부호출은 try/except로 감싸 실패해도 '—' 처리 후 계속 진행한다.
"""
import datetime, urllib.parse, html, os, csv, json, time

KST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime.now(KST)
TODAY = NOW.date()
DATA = "data"
HIST = os.path.join(DATA, "history.csv")
NEWSLOG = os.path.join(DATA, "news_log.jsonl")

# 번역·본문추출 총 시간 예산(초). 초과 시 번역 생략하고 빌드를 마쳐 무한정 지연 방지.
_START = time.time()
BUDGET_SEC = 540
def over_budget():
    return (time.time() - _START) > BUDGET_SEC

# Gemini(무료) 요약용 키·모델
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_DIAG = []          # 진단 로그(원인 파악용) → data/gemini_debug.json 로 커밋
GEMINI_DIAG_MAX = 12      # 앞쪽 몇 건의 상세만 기록(용량 절약)
HL_MARK = "※ 제목 기반 추정(본문 미확인)"   # 제목기반 보조요약 식별 표시
GEM_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
# ── 무료 한도(≈15 RPM / 1,500 RPD) 준수용 전역 속도제한 ─────────────
GEMINI_MIN_INTERVAL = 4.5   # 호출 간 최소 간격(초) → 분당 ≈13회(15 미만 안전)
GEMINI_MAX_CALLS = 80       # 한 실행당 총 호출 상한(RPD·시간예산 보호)
_GEM_LAST = [0.0]           # 마지막 호출 시각
_GEM_CALLS = [0]            # 누적 호출 수
_GEM_DEAD = [False]         # 지속 429(할당량 소진) 감지 시 이후 호출 중단

def gem_ready():
    return (bool(GEMINI_KEY) and not os.environ.get("DASH_OFFLINE")
            and not over_budget() and not _GEM_DEAD[0] and _GEM_CALLS[0] < GEMINI_MAX_CALLS)

def _gem_throttle():
    dt = time.time() - _GEM_LAST[0]
    if dt < GEMINI_MIN_INTERVAL:
        time.sleep(GEMINI_MIN_INTERVAL - dt)
    _GEM_LAST[0] = time.time()
    _GEM_CALLS[0] += 1

def gem_post(payload, timeout=45):
    """throttle + 429 1회 재시도 후 (status, json_dict) 반환. 지속 429면 _GEM_DEAD 세팅."""
    import requests
    for attempt in range(2):
        _gem_throttle()
        try:
            r = requests.post(GEM_URL, params={"key": GEMINI_KEY}, json=payload, timeout=timeout)
        except Exception as e:
            return None, {"_err": repr(e)[:200]}
        try:
            body = r.json()
        except Exception:
            body = {"_body": (r.text or "")[:300]}
        if r.status_code == 429:
            if attempt == 0 and not over_budget():
                time.sleep(25)      # 분당 한도 창 회복 대기 후 1회 재시도
                continue
            _GEM_DEAD[0] = True      # 재시도에도 429 → 할당량 소진으로 보고 이후 전부 중단
            return 429, body
        return r.status_code, body

# ------------------------------------------------------------------ 관심종목
# (표시명, 코드/티커, market)  market: 'KR' | 'US' | 'PRIVATE'(비상장·뉴스만)
GROUPS = [
    ("빅테크 · AI · 우주 (미국)", [
        ("애플", "AAPL", "US"), ("마이크로소프트", "MSFT", "US"),
        ("알파벳", "GOOGL", "US"), ("아마존", "AMZN", "US"),
        ("메타", "META", "US"), ("엔비디아", "NVDA", "US"),
        ("테슬라", "TSLA", "US"), ("팔란티어", "PLTR", "US"),
        ("스페이스X", "SPCX", "US"),
    ]),
    ("에너지 · 전력", [
        ("GE베르노바", "GEV", "US"), ("블룸에너지", "BE", "US"),
        ("두산에너빌리티", "034020", "KR"), ("한국전력", "015760", "KR"),
    ]),
    ("반도체 · 전자 (한국)", [
        ("삼성전자", "005930", "KR"), ("SK하이닉스", "000660", "KR"),
        ("LG전자", "066570", "KR"), ("삼성전기", "009150", "KR"),
    ]),
    ("자동차 · 플랫폼 · 바이오 · 기타 (한국)", [
        ("현대차", "005380", "KR"), ("네이버", "035420", "KR"),
        ("SK바이오사이언스", "302440", "KR"),
        ("엔알비", "475230", "KR"), ("퓨쳐켐", "220100", "KR"),
    ]),
]
NEWS_Q = {
    "애플": ("Apple stock", "en", "US"), "마이크로소프트": ("Microsoft stock", "en", "US"),
    "알파벳": ("Alphabet Google stock", "en", "US"), "아마존": ("Amazon stock", "en", "US"),
    "메타": ("Meta Platforms stock", "en", "US"), "엔비디아": ("Nvidia stock", "en", "US"),
    "테슬라": ("Tesla stock", "en", "US"), "팔란티어": ("Palantir stock", "en", "US"),
    "GE베르노바": ("GE Vernova stock", "en", "US"), "블룸에너지": ("Bloom Energy stock", "en", "US"),
    "스페이스X": ("SpaceX", "en", "US"),
    "두산에너빌리티": ("두산에너빌리티", "ko", "KR"), "한국전력": ("한국전력", "ko", "KR"),
    "삼성전자": ("삼성전자 주가", "ko", "KR"), "SK하이닉스": ("SK하이닉스 주가", "ko", "KR"),
    "LG전자": ("LG전자 주가", "ko", "KR"), "삼성전기": ("삼성전기 주가", "ko", "KR"),
    "현대차": ("현대차 주가", "ko", "KR"), "네이버": ("네이버 NAVER 주가", "ko", "KR"),
    "SK바이오사이언스": ("SK바이오사이언스", "ko", "KR"),
    "엔알비": ("엔알비 NRB 모듈러", "ko", "KR"), "퓨쳐켐": ("퓨쳐켐", "ko", "KR"),
}
NAME_MARKET = {name: market for _, its in GROUPS for (name, code, market) in its}

# 제목에서 '이 종목을 실제로 다루는지' 판정용 별칭(관련도 점수). 짧고 오탐 잦은 티커(BE·GEV)는 제외.
NEWS_ALIAS = {
    "애플": ["apple", "aapl", "iphone", "ipad", "tim cook", "vision pro"],
    "마이크로소프트": ["microsoft", "msft", "azure", "copilot", "satya nadella"],
    "알파벳": ["alphabet", "google", "googl", "goog", "gemini", "youtube", "waymo"],
    "아마존": ["amazon", "amzn", "aws", "jassy", "bezos"],
    "메타": ["meta platforms", "meta", "facebook", "instagram", "whatsapp", "zuckerberg"],
    "엔비디아": ["nvidia", "nvda", "jensen huang", "blackwell"],
    "테슬라": ["tesla", "tsla", "musk", "cybertruck", "robotaxi", "optimus"],
    "팔란티어": ["palantir", "pltr", "karp"],
    "GE베르노바": ["ge vernova", "vernova"], "블룸에너지": ["bloom energy"],
    "스페이스X": ["spacex", "space x", "starlink", "starship"],
    "두산에너빌리티": ["두산에너빌리티", "두산에너", "두산"], "한국전력": ["한국전력", "한전"],
    "삼성전자": ["삼성전자", "삼전"], "SK하이닉스": ["sk하이닉스", "하이닉스"],
    "LG전자": ["lg전자"], "삼성전기": ["삼성전기"],
    "현대차": ["현대차", "현대자동차", "아이오닉", "제네시스"], "네이버": ["네이버", "naver", "라인"],
    "SK바이오사이언스": ["sk바이오사이언스", "sk바이오"], "엔알비": ["엔알비", "nrb"],
    "퓨쳐켐": ["퓨쳐켐", "futurechem"],
}
# 주가에 영향 주는 '가치 이슈' 키워드(가점) / 클릭베이트·나열형 잡음(감점)
IMPACT_EN = ["earnings", "revenue", "profit", "guidance", "forecast", "outlook", "upgrade",
    "downgrade", "price target", "analyst", "rating", "acquisition", "merger", "acquire",
    "deal", "lawsuit", "sues", "sued", "regulat", "antitrust", "fine", "penalt", "layoff",
    "job cut", "ceo", "resign", "steps down", "launch", "unveil", "contract", "order",
    "partnership", "invest", "buyback", "dividend", "results", "beats", "miss", "surge",
    "plunge", "soar", "tumble", "halt", "recall", " ban", "subpoena", "probe", "chip",
    "data center", "capacity", "factory", "plant", "backlog", "stake", "raises", "cuts"]
NOISE_EN = ["how to buy", "should you buy", "is it too late", "reasons to buy", "stocks to buy",
    "best stock", "top 3", "top 5", "top 10", "3 stock", "5 stock", "7 stock", "10 stock",
    "stocks to watch", "prediction", "could make you", "millionaire", "here's why you",
    "zacks rank", "better buy", " vs ", " vs.", "motley", "if you invested", "would be worth",
    "dividend stocks", "wall street bets", "reddit", "undervalued stock", "growth stocks to",
    "stocks that", "magnificent seven stock", "3 reasons", "these stocks"]
IMPACT_KO = ["실적", "영업이익", "매출", "순이익", "어닝", "가이던스", "목표주가", "상향", "하향",
    "인수", "합병", "지분", "계약", "수주", "규제", "소송", "과징금", "리콜", "신제품", "출시",
    "배당", "자사주", "증설", "감산", "급등", "급락", "신고가", "신저가", "공시", "유상증자",
    "무상증자", "특허", "임상", "승인", "적자", "흑자", "실적발표", "투자의견"]
NOISE_KO = ["사는 법", "매수 타이밍", "지금 사도", "추천주", "유망주", "관심주", "총정리", "이 종목",
    "테마주", "급등락 주의", "놓치면", "주목할 종목", "오늘의 종목", "급등 종목", "종목 정리"]
AUTH_US = ["Reuters", "Bloomberg", "Wall Street Journal", "WSJ", "Financial Times",
           "CNBC", "AP", "Associated Press", "Barron"]

# ------------------------------------------------------------------ 수집 함수
def pct_from_closes(df):
    try:
        c = df["Close"].dropna()
        if len(c) < 2:
            return float(c.iloc[-1]), None
        return float(c.iloc[-1]), (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100.0
    except Exception:
        return None, None

def get_quote(code, market):
    if market == "PRIVATE" or os.environ.get("DASH_OFFLINE"):
        return None, None
    try:
        import FinanceDataReader as fdr
        p, r = pct_from_closes(fdr.DataReader(code))
        if p is not None:
            return p, r
    except Exception:
        pass
    try:
        import yfinance as yf
        h = yf.Ticker(code).history(period="5d")
        if len(h):
            last = float(h["Close"].iloc[-1])
            prev = float(h["Close"].iloc[-2]) if len(h) > 1 else last
            return last, (last - prev) / prev * 100.0 if prev else None
    except Exception:
        pass
    return None, None

def get_index(cands):
    for s in cands:
        p, r = get_quote(s, "US")
        if p is not None:
            return p, r
    return None, None

def fetch_news(query, lang, gl, limit=3):
    if os.environ.get("DASH_OFFLINE"):
        return []
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
        title = e.title; src = ""
        if " - " in title:
            title, src = title.rsplit(" - ", 1)
        out.append({"time": pub.strftime("%H:%M"), "title": title.strip(),
                    "src": src.strip(), "link": e.link})
        if len(out) >= limit:
            break
    out.sort(key=lambda x: x["time"], reverse=True)
    return out

def has_hangul(s):
    return any('가' <= ch <= '힣' for ch in (s or ""))

def has_latin(s):
    return any('a' <= ch.lower() <= 'z' for ch in (s or ""))

# 번역 API가 반환하는 에러 페이지(쓰레기) 탐지
ERROR_MARKERS = ["error 500", "server error", "that's an error", "that’s an error",
                 "please try again later", "that's all we know", "that’s all we know",
                 "1500.", "too many requests", "service unavailable"]
def is_garbage(t):
    t = (t or "").lower()
    return any(m in t for m in ERROR_MARKERS)

_tr = None
def to_ko(text):
    """영문 → 한글. 실패 시 재시도(간격 둠), 한글 포함·비쓰레기 결과만 채택."""
    global _tr
    if not text or not has_latin(text) or over_budget():
        return text
    for attempt in range(2):
        try:
            from deep_translator import GoogleTranslator
            if _tr is None:
                _tr = GoogleTranslator(source="auto", target="ko")
            out = _tr.translate(text)
            if out and has_hangul(out) and not is_garbage(out):
                time.sleep(0.2)  # 레이트리밋 회피용 스로틀
                return out
        except Exception:
            _tr = None  # 실패 시 번역기 재생성
        time.sleep(0.8)  # 재시도 전 백오프
    return text

# ------------------------------------------------------------------ 신뢰 매체 + 본문번역
# 국내 16개 신문사(종합 10 + 경제 6). 별칭 포함.
KR_SOURCES = ["조선일보", "중앙일보", "동아일보", "한겨레", "경향신문", "한국일보",
              "서울신문", "국민일보", "세계일보", "문화일보",
              "매일경제", "매경", "한국경제", "한경", "서울경제",
              "파이낸셜뉴스", "헤럴드경제", "아시아경제"]
# 미국 주요 경제·통신 매체(선호).
US_SOURCES = ["Reuters", "Bloomberg", "CNBC", "Wall Street Journal", "WSJ",
              "Financial Times", "MarketWatch", "Barron", "Forbes",
              "Yahoo Finance", "Business Insider", "Motley Fool",
              "Investing.com", "Investor's Business Daily", "Seeking Alpha", "Nasdaq", "AP",
              "Associated Press", "Fortune", "Axios", "Quartz", "TechCrunch", "The Verge",
              "CNN", "The Motley Fool", "Benzinga", "TipRanks"]
# 봇 접근·본문 공개가 잘 되는 매체(Gemini가 실제로 읽어 본문요약 성공률이 높음) → 우선 선택.
ACCESSIBLE_US = ["Yahoo Finance", "Business Insider", "AP", "Associated Press", "CNBC",
                 "MarketWatch", "Investing.com", "Nasdaq", "Motley Fool", "Forbes",
                 "Fortune", "Axios", "Quartz", "TechCrunch", "The Verge", "Benzinga",
                 "TipRanks", "CNN"]

def source_ok(src, whitelist):
    s = (src or "").lower()
    return any(w.lower() in s for w in whitelist)

def src_rank(src):
    """접근 잘 되는 매체=0(우선), 그 외=1. 안정정렬로 관련도 순서 유지."""
    s = (src or "").lower()
    return 0 if any(w.lower() in s for w in ACCESSIBLE_US) else 1

def relevance_score(a, aliases, gl):
    """제목 기반 영향력·관련도 점수. (score, why리스트, 회사명포함여부) 반환."""
    t = (a.get("title", "") or "")
    tl = t.lower()
    src = a.get("src", "") or ""
    hit_name = any(al.lower() in tl for al in aliases)
    score = 3 if hit_name else 0
    imp = IMPACT_EN if gl == "US" else IMPACT_KO
    noise = NOISE_EN if gl == "US" else NOISE_KO
    base = tl if gl == "US" else t
    ih = [k.strip() for k in imp if k in base]
    if ih:
        score += min(6, 2 * len(ih))
    if any(k in base for k in noise):
        score -= 6
    if source_ok(src, AUTH_US):
        score += 2
    elif source_ok(src, ACCESSIBLE_US):
        score += 1
    return score, ih[:2], hit_name

def gemini_triage(name, ticker, cands):
    """후보 제목들 중 '주가에 실제 영향 있는 가치 기사'만 최대 3개 선별.
    반환: [{"i":idx,"tag":"실적"}...] (빈 배열 가능) 또는 실패 시 None(→휴리스틱 폴백)."""
    if not cands or not gem_ready():
        return None
    lst = "\n".join(f"{i}. {a.get('title','')} ({a.get('src','')})" for i, a in enumerate(cands))
    prompt = (f"당신은 주식 애널리스트다. 아래는 {name}({ticker}) 관련 후보 뉴스 제목 목록이다. "
              "각 제목이 이 종목의 '주가에 실제로 영향을 줄 수 있는 가치 있는 기사'인지 평가하라. "
              "실적·가이던스·목표주가/투자의견·M&A·수주/계약·규제/소송·경영진·신제품·주가 급변 등 "
              "주가 영향 이슈를 우선한다. 단순 '주식 사는 법', 여러 종목 나열 클릭베이트, "
              "무관하거나 일반론적 기사는 제외한다. 영향력이 큰 순서로 최대 3개만 골라 "
              "아래 JSON만 출력하라(설명·코드펜스 금지):\n"
              '{"keep":[{"i":0,"tag":"실적"},{"i":2,"tag":"목표주가"}]}\n'
              "tag는 실적/가이던스/목표주가/M&A/수주/규제/소송/경영/제품/주가/기타 중 하나. "
              "가치 있는 기사가 없으면 keep을 빈 배열로 하라.\n목록:\n" + lst)
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300}}
    try:
        import re as _re
        status, d = gem_post(payload, timeout=30)
        if status != 200:
            _diag("triage_http_error", status=status, name=name)
            return None
        cand = (d.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
        m = _re.search(r"\{.*\}", text, _re.S)
        if not m:
            _diag("triage_nojson", name=name, snippet=text[:120])
            return None
        obj = json.loads(m.group(0))
        keep = obj.get("keep", [])
        out = []
        for it in keep:
            if isinstance(it, dict) and isinstance(it.get("i"), int):
                out.append({"i": it["i"], "tag": str(it.get("tag", ""))[:8]})
        _diag("triage_ok", name=name, kept=len(out))
        return out[:3]
    except Exception as e:
        _diag("triage_exception", name=name, err=repr(e)[:200])
        return None

def chunk_translate(text, limit=4000):
    """긴 본문을 조각내어 한글 번역 후 결합."""
    text = (text or "").strip()
    if not text:
        return ""
    parts, cur = [], ""
    for seg in text.replace("\r", "").split("\n"):
        while len(seg) > limit:
            if cur:
                parts.append(cur); cur = ""
            parts.append(seg[:limit]); seg = seg[limit:]
        if len(cur) + len(seg) + 1 > limit and cur:
            parts.append(cur); cur = ""
        cur += (("\n" if cur else "") + seg)
    if cur:
        parts.append(cur)
    return "\n".join(to_ko(p) if has_latin(p) else p for p in parts)

def resolve_gnews(link):
    """구글뉴스 리다이렉트 링크 → 실제 기사 URL. 실패 시 원링크."""
    try:
        from googlenewsdecoder import gnewsdecoder
        dec = gnewsdecoder(link, interval=0)
        if isinstance(dec, dict) and dec.get("status") and dec.get("decoded_url"):
            return dec["decoded_url"]
    except Exception:
        pass
    return link

GEMINI_COUNTS = {}
def _diag(branch, **kw):
    """진단 기록: 카운트는 전건, 상세 샘플은 앞쪽 GEMINI_DIAG_MAX 건만."""
    GEMINI_COUNTS[branch] = GEMINI_COUNTS.get(branch, 0) + 1
    if len(GEMINI_DIAG) < GEMINI_DIAG_MAX:
        rec = {"branch": branch}
        rec.update(kw)
        GEMINI_DIAG.append(rec)

def gemini_headline_note(title):
    """기사 본문을 못 열 때, 번역된 제목만으로 한국어 배경·맥락 2~3문장(보조요약).
    앞줄에 HL_MARK 표시. 실패 시 None."""
    if not gem_ready():
        return None
    prompt = ("다음은 미국 경제뉴스 기사의 한국어 번역 제목이다. 기사 본문은 접근하지 못했다. "
              "이 제목이 다루는 사안의 배경·맥락을 '일반적으로 알려진 사실'에 근거해 한국어 2~3문장으로 설명해줘. "
              "확인되지 않은 구체 수치·발언·인용은 절대 지어내지 말고, 모르면 일반적 맥락만 간단히. "
              f"맨 앞 줄에 정확히 '{HL_MARK}' 라고 먼저 쓰고 줄바꿈해줘.\n제목: {title}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 300},
    }
    try:
        status, d = gem_post(payload, timeout=30)
        if status != 200:
            _diag("hl_http_error", status=status)
            return None
        cand = (d.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
        if not text or not has_hangul(text) or is_garbage(text):
            _diag("hl_empty")
            return None
        if HL_MARK not in text:          # 표시 누락 시 앞에 강제로 붙임
            text = HL_MARK + "\n" + text
        _diag("hl_ok", snippet=text[:60])
        return text
    except Exception as e:
        _diag("hl_exception", err=repr(e)[:200])
        return None

def gemini_summary_ko(title, link, allow_fallback=True):
    """Gemini가 실제 기사 URL을 읽어 한국어로 요약. 못 열면 제목기반 보조요약으로 폴백.
    실패/키없음/예산초과 시 None. 원인은 GEMINI_DIAG → data/gemini_debug.json 커밋."""
    if not GEMINI_KEY:
        _diag("no_key", key_len=len(GEMINI_KEY))
        return None
    if os.environ.get("DASH_OFFLINE") or over_budget() or _GEM_DEAD[0] or _GEM_CALLS[0] >= GEMINI_MAX_CALLS:
        _diag("offline_or_budget")
        return None
    real = resolve_gnews(link)
    prompt = ("아래 영어 뉴스 기사를 한국어로 4~6문장으로 요약해줘. 투자 판단에 도움되는 핵심 사실·수치·전망 위주로 "
              "과장 없이 사실만 쓰고, 맨 끝 줄에 '▶ 투자 관점:' 으로 시작하는 한 줄 코멘트를 덧붙여줘. "
              "기사에 접근이 안 되면 정확히 '요약불가' 라고만 답해.\n"
              f"제목: {title}\n기사 URL: {real}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"url_context": {}}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700},
    }
    try:
        status, d = gem_post(payload, timeout=45)
        if status != 200:
            err = (d or {}).get("error", {})
            _diag("http_error", status=status,
                  msg=str(err.get("message", ""))[:300], code=err.get("code"),
                  estatus=err.get("status"), key_prefix=GEMINI_KEY[:4])
            return gemini_headline_note(title) if allow_fallback else None
        cand = (d.get("candidates") or [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            _diag("empty_text", status=status,
                  finish=cand.get("finishReason"),
                  urlmeta=str(cand.get("url_context_metadata", ""))[:300])
            return gemini_headline_note(title) if allow_fallback else None
        if "요약불가" in text:
            _diag("summary_refused", snippet=text[:150])
            return gemini_headline_note(title) if allow_fallback else None
        if not has_hangul(text) or is_garbage(text):
            _diag("no_hangul_or_garbage", snippet=text[:200])
            return gemini_headline_note(title) if allow_fallback else None
        _diag("ok", snippet=text[:60])
        return text
    except Exception as e:
        _diag("exception", err=repr(e)[:300])
        return gemini_headline_note(title) if allow_fallback else None

# ------------------------------------------------------------------ 포맷
def fmt_price(p, market):
    if p is None:
        return "—"
    return ("$" + format(p, ",.2f")) if market == "US" else format(int(round(p)), ",")

def fmt_pct(r):
    if r is None:
        return ("flat", "—")
    sign = "+" if r > 0 else ("−" if r < 0 else "")
    cls = "up" if r > 0 else ("down" if r < 0 else "flat")
    return (cls, f"{sign}{abs(r):.2f}%")

def esc(s):
    return html.escape(str(s) if s is not None else "")

# ------------------------------------------------------------------ 수집 실행
quotes = {}
for _, items in GROUPS:
    for name, code, market in items:
        quotes[name] = (get_quote(code, market), market, code)

idx = {
    "코스피": get_index(["KS11"]), "코스닥": get_index(["KQ11"]),
    "S&P 500": get_index(["US500", "^GSPC"]), "나스닥": get_index(["IXIC", "^IXIC"]),
    "다우": get_index(["DJI", "^DJI"]), "VIX": get_index(["^VIX", "VIX"]),
}
macro = {
    "USD/KRW": get_index(["USD/KRW", "KRW=X"]),
    "WTI 유가": get_index(["CL=F"]), "금(Gold)": get_index(["GC=F"]),
}
news = {}
for gname, items in GROUPS:
    for name, code, market in items:
        q, lang, gl = NEWS_Q.get(name, (name, "ko", "KR"))
        pool = fetch_news(q, lang, gl, limit=25)
        wl = KR_SOURCES if gl == "KR" else US_SOURCES
        aliases = NEWS_ALIAS.get(name, [name])
        cands = [a for a in pool if source_ok(a["src"], wl)]
        # 1) 휴리스틱 관련도·영향력 점수 → 명백한 잡음 제거 + 점수순 정렬
        scored = []
        for a in cands:
            s, why, hitn = relevance_score(a, aliases, gl)
            if s >= 2:                    # 잡음(클릭베이트·나열형)·무관 기사 컷
                a["_s"], a["_why"] = s, why
                scored.append(a)
        scored.sort(key=lambda a: (-a["_s"], src_rank(a["src"])))
        # 2) 미국: 상위 후보를 Gemini 트리아지로 '가치 기사'만 선별(실패 시 휴리스틱)
        if gl == "US":
            top = scored[:8]
            tri = gemini_triage(name, code, top)
            if tri is not None:
                picked = []
                for it in tri[:3]:
                    if 0 <= it["i"] < len(top):
                        art = top[it["i"]]
                        art["impact"] = it.get("tag") or (art.get("_why") or [""])[0]
                        picked.append(art)
            else:
                picked = scored[:3]
                for a in picked:
                    a["impact"] = (a.get("_why") or [""])[0]
            for a in picked:            # 선별된 기사만 번역·요약(비용 절약)
                a["title"] = to_ko(a["title"])
                a["summary_ko"] = gemini_summary_ko(a["title"], a["link"])
        else:
            picked = scored[:3]
            for a in picked:
                a["impact"] = (a.get("_why") or [""])[0]
                a["summary_ko"] = None
        news[name] = picked

os.makedirs(DATA, exist_ok=True)

# ------------------------------------------------------------------ 누적 저장
# 1) history.csv append
newfile = not os.path.exists(HIST)
with open(HIST, "a", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    if newfile:
        w.writerow(["ts", "date", "time", "symbol", "name", "market", "price", "pct"])
    for gname, items in GROUPS:
        for name, code, market in items:
            (p, r), mkt, _ = quotes[name]
            if p is None:
                continue
            w.writerow([NOW.isoformat(), str(TODAY), NOW.strftime("%H:%M"),
                        code, name, market, f"{p:.4f}", "" if r is None else f"{r:.4f}"])

# 2) news_log.jsonl append (링크 기준 dedup)
seen = set()
if os.path.exists(NEWSLOG):
    with open(NEWSLOG, encoding="utf-8") as f:
        for line in f:
            try:
                o = json.loads(line)
                if is_garbage(o.get("title", "")) or is_garbage(o.get("summary_ko", "")):
                    continue  # 쓰레기는 dedup 제외 → 정상본 재수집 허용(heal이 쓰레기 삭제)
                seen.add(o["link"])
            except Exception:
                pass
with open(NEWSLOG, "a", encoding="utf-8") as f:
    for name, arts in news.items():
        for a in arts:
            if a["link"] in seen:
                continue
            seen.add(a["link"])
            f.write(json.dumps({"logged": NOW.isoformat(), "date": str(TODAY),
                                "symbol": name, "time": a["time"], "title": a["title"],
                                "src": a["src"], "link": a["link"], "impact": a.get("impact", ""),
                                "summary_ko": a.get("summary_ko")}, ensure_ascii=False) + "\n")

# ------------------------------------------------------------------ 축적 데이터 로드(추세용)
def load_history():
    series = {}
    if not os.path.exists(HIST):
        return series
    with open(HIST, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                series.setdefault(row["symbol"], []).append(
                    (row["ts"], float(row["price"]), row.get("pct", ""), row["name"], row["market"]))
            except Exception:
                pass
    for k in series:
        series[k].sort(key=lambda x: x[0])
    return series

def load_newslog():
    log = {}
    if not os.path.exists(NEWSLOG):
        return log
    with open(NEWSLOG, encoding="utf-8") as f:
        for line in f:
            try:
                o = json.loads(line)
                log.setdefault(o["symbol"], []).append(o)
            except Exception:
                pass
    for k in log:
        log[k].sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    return log

def heal_newslog(max_body=3):
    """저장된 미국 뉴스의 (1) 영어 제목 재번역, (2) 누락된 본문번역 backfill(실행당 최대 max_body건)."""
    if not os.path.exists(NEWSLOG):
        return
    rows, changed, filled = [], False, 0
    with open(NEWSLOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            # 번역 에러페이지(Error 500 등) 쓰레기 제목/요약은 삭제
            if is_garbage(o.get("title", "")) or is_garbage(o.get("summary_ko", "")):
                changed = True
                continue
            if NAME_MARKET.get(o.get("symbol")) == "US":
                if not has_hangul(o.get("title", "")):
                    ko = to_ko(o["title"])
                    if ko != o["title"] and has_hangul(ko):
                        o["title"] = ko
                        changed = True
                if not o.get("summary_ko") and filled < max_body and o.get("link"):
                    s = gemini_summary_ko(o.get("title", ""), o["link"])
                    if s:
                        o["summary_ko"] = s
                        changed = True
                        filled += 1
            rows.append(o)
    if changed:
        with open(NEWSLOG, "w", encoding="utf-8") as f:
            for o in rows:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")

heal_newslog()
HISTORY = load_history()
NEWSLOG_D = load_newslog()

# ------------------------------------------------------------------ 신호 감지 엔진
def daysago(dstr):
    try:
        return (TODAY - datetime.date.fromisoformat(dstr)).days
    except Exception:
        return 999

SIGNALS = []
for gname, items in GROUPS:
    for name, code, market in items:
        (p, r), mkt, _ = quotes[name]
        logs = NEWSLOG_D.get(name, [])
        prices = [x[1] for x in HISTORY.get(code, [])]
        n3 = sum(1 for o in logs if daysago(o["date"]) <= 2)          # 최근 3일 기사 수
        days_active = len({o["date"] for o in logs if daysago(o["date"]) <= 6})  # 최근 7일 뉴스발생 일수
        wchg = ((prices[-1] - prices[0]) / prices[0] * 100) if len(prices) >= 2 and prices[0] else None
        fl = []
        # 1) 급변
        if r is not None and abs(r) >= (5 if market == "US" else 3):
            fl.append((3 if abs(r) >= 7 else 2, "급변", f"{r:+.1f}% 급{'등' if r > 0 else '락'}"))
        # 2) 이슈 누적·연속성
        if days_active >= 3 or n3 >= 4:
            fl.append((2 if days_active >= 4 else 1, "이슈가열", f"최근 뉴스 {days_active}일 활발·3일 {n3}건"))
        # 3) 주가-뉴스 괴리
        if n3 >= 3 and wchg is not None:
            if wchg <= -3:
                fl.append((2, "괴리", f"뉴스 활발한데 주가 {wchg:+.1f}% 눌림(역발상 관심)"))
            elif wchg >= 8:
                fl.append((2, "괴리", f"뉴스+주가 {wchg:+.1f}% 동반(과열 경계)"))
        # 4) 모멘텀 전환 / 신고·신저
        if len(prices) >= 6:
            if prices[-1] >= max(prices):
                fl.append((2, "모멘텀", "누적구간 신고가 경신"))
            elif prices[-1] <= min(prices):
                fl.append((2, "모멘텀", "누적구간 신저가"))
            else:
                sma_s = sum(prices[-3:]) / 3
                sma_l = sum(prices[-10:]) / len(prices[-10:])
                psma_s = sum(prices[-4:-1]) / 3
                if psma_s <= sma_l < sma_s:
                    fl.append((1, "모멘텀", "단기 이평 상향 돌파(상승 전환 조짐)"))
                elif psma_s >= sma_l > sma_s:
                    fl.append((1, "모멘텀", "단기 이평 하향 이탈(하락 전환 조짐)"))
        for st, typ, detail in fl:
            SIGNALS.append({"name": name, "code": code, "market": market,
                            "type": typ, "detail": detail, "strength": st})
SIGNALS.sort(key=lambda x: (-x["strength"], x["name"]))
with open(os.path.join(DATA, "signals.json"), "w", encoding="utf-8") as f:
    json.dump({"generated": NOW.isoformat(), "date": str(TODAY), "signals": SIGNALS},
              f, ensure_ascii=False, indent=1)

# ------------------------------------------------------------------ 데이터기반 핵심/포인트
def biggest(direction):
    best = None
    for name, ((p, r), mkt, code) in quotes.items():
        if r is None:
            continue
        if best is None or (r > best[1] if direction == "up" else r < best[1]):
            best = (name, r)
    return best
top_up, top_dn = biggest("up"), biggest("down")
news_cnt = sum(len(v) for v in news.values())
kospi = idx["코스피"]
headline = ""
if kospi and kospi[0] is not None:
    _, kt = fmt_pct(kospi[1])
    headline += f"코스피 {format(int(round(kospi[0])),',')}({kt}) · "
if top_up:
    headline += f"최대 상승 {top_up[0]} +{abs(top_up[1]):.1f}% · "
if top_dn:
    headline += f"최대 하락 {top_dn[0]} −{abs(top_dn[1]):.1f}% · "
headline += f"오늘 관련 기사 {news_cnt}건 수집"

points = []
for name, ((p, r), mkt, code) in quotes.items():
    fl = []
    if r is not None and abs(r) >= 3:
        fl.append(f"{'급등' if r>0 else '급락'} {r:+.1f}%")
    if news.get(name):
        fl.append(f"신규뉴스 {len(news[name])}건")
    if fl:
        points.append((name, " · ".join(fl)))

# ------------------------------------------------------------------ CSS
CSS = """
:root{--ground:#0E1420;--surface:#161E2C;--surface-2:#1C2636;--border:#28344A;--border-soft:#20293A;--text:#E7ECF4;--muted:#8A96A9;--faint:#5C6A80;--accent:#E3A94A;--accent-soft:rgba(227,169,74,.14);--up:#F0616D;--down:#4D93F0;--flat:#8A96A9;--up-soft:rgba(240,97,109,.13);--down-soft:rgba(77,147,240,.13);--shadow:0 1px 0 rgba(255,255,255,.03) inset,0 8px 24px -12px rgba(0,0,0,.6);--sans:"IBM Plex Sans KR",system-ui,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace}
:root:not([data-theme="dark"]){--ground:#F3F5F9;--surface:#FFFFFF;--surface-2:#F7F9FC;--border:#DDE3EC;--border-soft:#E8ECF3;--text:#141C2A;--muted:#5D6B80;--faint:#9AA6B8;--accent:#B57E1E;--accent-soft:rgba(181,126,30,.10);--up:#D23948;--down:#2C6BD4;--flat:#5D6B80;--up-soft:rgba(210,57,72,.09);--down-soft:rgba(44,107,212,.09);--shadow:0 1px 2px rgba(20,28,42,.04),0 10px 26px -16px rgba(20,28,42,.28)}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){--ground:#F3F5F9;--surface:#FFFFFF;--surface-2:#F7F9FC;--border:#DDE3EC;--border-soft:#E8ECF3;--text:#141C2A;--muted:#5D6B80;--faint:#9AA6B8;--accent:#B57E1E;--accent-soft:rgba(181,126,30,.10);--up:#D23948;--down:#2C6BD4;--flat:#5D6B80;--up-soft:rgba(210,57,72,.09);--down-soft:rgba(44,107,212,.09)}}
:root[data-theme="dark"]{--ground:#0E1420;--surface:#161E2C;--surface-2:#1C2636;--border:#28344A;--border-soft:#20293A;--text:#E7ECF4;--muted:#8A96A9;--faint:#5C6A80;--accent:#E3A94A;--accent-soft:rgba(227,169,74,.14);--up:#F0616D;--down:#4D93F0;--flat:#8A96A9;--up-soft:rgba(240,97,109,.13);--down-soft:rgba(77,147,240,.13)}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ground);color:var(--text);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased;padding:0 20px 64px;font-size:15px}
.wrap{max-width:980px;margin:0 auto}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--flat)}a{color:inherit}
header{position:sticky;top:0;z-index:5;background:var(--ground);padding:24px 0 16px;margin-bottom:6px;border-bottom:1px solid var(--border-soft)}
.kicker{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:9px}
h1{font-size:clamp(23px,4.2vw,32px);font-weight:700;letter-spacing:-.01em}
.stamp{margin-top:11px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;font-size:12.5px;color:var(--muted)}
.stamp .dot{width:5px;height:5px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:7px}
.stamp b{color:var(--text);font-weight:600}
.navlink{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:var(--accent);background:var(--accent-soft);border:1px solid var(--border);border-radius:20px;padding:6px 14px;text-decoration:none}
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
.card .val{font-size:21px;font-weight:600;margin-top:9px}
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
table{width:100%;border-collapse:collapse;min-width:480px}
thead th{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:600;text-align:right;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface-2)}
thead th:first-child{text-align:left}
tbody td{padding:11px 16px;border-bottom:1px solid var(--border-soft);text-align:right;font-size:14.5px}
tbody tr:last-child td{border-bottom:0}
tbody td:first-child{text-align:left}
.nm{font-weight:600}
.tick{font-size:11.5px;color:var(--faint);font-family:var(--mono);margin-left:7px}
.grp td{background:var(--surface-2);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600;padding:9px 16px;text-align:left}
.pct{display:inline-block;min-width:62px;padding:2px 8px;border-radius:6px;font-weight:600;font-size:13px}
.pct.up{background:var(--up-soft)}.pct.down{background:var(--down-soft)}.pct.flat{background:var(--surface-2);color:var(--muted)}
.naq{color:var(--faint);font-size:12.5px}
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
.stk li .itag{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;font-size:10.5px;font-weight:700;color:#fff;background:var(--accent);vertical-align:middle;white-space:nowrap}
.stk .none{padding:14px 16px;font-size:13px;color:var(--faint)}
.trx{margin:2px 16px 8px 53px}
.trx summary{cursor:pointer;font-size:11.5px;font-weight:600;color:var(--accent);list-style:none;padding:4px 0}
.trx summary::-webkit-details-marker{display:none}
.trx-body{background:var(--surface-2);border:1px solid var(--border-soft);border-radius:9px;padding:12px 14px;margin-top:4px}
.trx-body p{font-size:13px;line-height:1.65;color:var(--text);margin:0 0 8px}
.trx-body p:last-child{margin-bottom:0}
.trx.hl summary{color:#b45309}
.trx.hl .trx-body{border-style:dashed;border-color:#f59e0b55}
.trx.hl .trx-body p{color:var(--muted)}
.trx-fail{margin:2px 16px 8px 53px;font-size:11.5px;color:var(--faint)}
.badge-en{font-size:10px;font-weight:700;color:var(--down);background:var(--down-soft);border-radius:4px;padding:1px 6px;margin-left:2px}
.badge-pv{font-size:10px;font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:4px;padding:1px 6px;margin-left:2px}
/* analyst */
.an-mwrap{background:linear-gradient(180deg,var(--accent-soft),var(--surface));border:1px solid var(--border);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow);margin-bottom:12px}
.an-mlabel{font-size:11.5px;font-weight:700;color:var(--accent);letter-spacing:.02em;margin-bottom:6px}
.an-market p{font-size:13.5px;line-height:1.7;color:var(--text);margin:0 0 8px}
.an-market p:last-child{margin-bottom:0}
.an-market.dim{color:var(--faint);font-size:12.5px}
.an-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
@media(max-width:720px){.an-grid{grid-template-columns:1fr}}
.an-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:14px 15px;box-shadow:var(--shadow)}
.an-h{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:9px}
.an-nm{font-weight:700;font-size:15px}
.an-nm .tick{font-size:10.5px;color:var(--faint);font-family:var(--mono);margin-left:6px;font-weight:500}
.an-sig{font-size:10px;font-weight:700;color:#fff;background:var(--accent);border-radius:999px;padding:1px 8px}
.an-sig.s3{background:var(--up)}.an-sig.s2{background:var(--accent)}.an-sig.s1{background:var(--muted)}
.an-facts{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.afct{font-size:11px;color:var(--text);background:var(--surface-2);border:1px solid var(--border-soft);border-radius:7px;padding:3px 8px}
.afct.dim{color:var(--faint)}
.an-note{background:var(--surface-2);border:1px solid var(--border-soft);border-radius:10px;padding:11px 13px}
.an-note p{font-size:12.5px;line-height:1.65;color:var(--text);margin:0 0 7px}
.an-note p:last-child{margin-bottom:0}
.an-note.dim{color:var(--faint);font-size:11.5px}
/* signals */
.siggrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
@media(max-width:640px){.siggrid{grid-template-columns:1fr}}
.sig{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:12px;padding:13px 15px;box-shadow:var(--shadow)}
.sig.s3{border-left-color:var(--up)}.sig.s2{border-left-color:var(--accent)}.sig.s1{border-left-color:var(--muted)}
.sig-h{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.sig-nm{font-weight:700;font-size:14px}
.sig-t{font-size:10.5px;font-weight:700;letter-spacing:.04em;color:var(--accent);background:var(--accent-soft);border-radius:5px;padding:2px 7px}
.sig-str{margin-left:auto;font-size:10px;color:var(--accent);letter-spacing:1px}
.sig-d{font-size:13px;color:var(--muted);line-height:1.5}
/* trends */
.trow{display:grid;grid-template-columns:180px 1fr auto;gap:14px;align-items:center;padding:13px 16px;border-bottom:1px solid var(--border-soft)}
.trow:last-child{border-bottom:0}
@media(max-width:640px){.trow{grid-template-columns:1fr auto}.trow .spark{grid-column:1/-1;order:3}}
.trow .tnm{font-weight:600;font-size:14px}
.trow .tnm .tk{font-size:11px;color:var(--faint);font-family:var(--mono);margin-left:6px}
.trow .tp{text-align:right;font-family:var(--mono);font-size:14px}
.trow .tp .d{font-size:11.5px;margin-top:2px}
.spark{width:100%;height:38px}
.tl{background:var(--surface);border:1px solid var(--border);border-radius:13px;box-shadow:var(--shadow);overflow:hidden;margin-bottom:10px}
.tl .sh{padding:11px 16px;background:var(--surface-2);border-bottom:1px solid var(--border-soft);font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px}
.tl .day{padding:9px 16px 3px;font-size:11px;font-weight:700;color:var(--accent);letter-spacing:.03em}
.tl a{display:flex;gap:10px;padding:6px 16px;text-decoration:none;align-items:baseline}
.tl a:hover{background:var(--surface-2)}
.tl .tm{flex:none;font-family:var(--mono);font-size:11px;color:var(--muted);width:38px}
.tl .ht{font-size:13px;line-height:1.5}
.tl .none{padding:12px 16px;color:var(--faint);font-size:13px}
footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--border-soft);font-size:11.5px;color:var(--faint);line-height:1.7}
"""

# ------------------------------------------------------------------ 스파크라인 SVG
def sparkline(points, market):
    vals = [p[1] for p in points][-40:]
    if len(vals) < 2:
        return '<div class="spark" style="color:var(--faint);font-size:11px;display:flex;align-items:center">데이터 축적 중…</div>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    W, H, pad = 240, 38, 3
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (W - 2 * pad) * i / (n - 1)
        y = pad + (H - 2 * pad) * (1 - (v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    up = vals[-1] >= vals[0]
    col = "var(--up)" if up else "var(--down)"
    return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{col}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(pts)}"/>'
            f'<circle cx="{pts[-1].split(",")[0]}" cy="{pts[-1].split(",")[1]}" r="2.2" fill="{col}"/></svg>')

# ------------------------------------------------------------------ 🎓 애널리스트 관점 (전문가급 종합)
def yf_fundamentals(code, market):
    """yfinance로 밸류에이션·목표주가·투자의견·실적일. 실패 시 {}."""
    if os.environ.get("DASH_OFFLINE") or over_budget():
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}
    syms = [code] if market == "US" else [code + ".KS", code + ".KQ"]
    for sym in syms:
        try:
            t = yf.Ticker(sym)
            try:
                info = t.info or {}
            except Exception:
                info = {}
            if not info:
                continue
            out = {}
            for k_src, k in [("targetMeanPrice", "target"), ("forwardPE", "fpe"),
                             ("trailingPE", "tpe"), ("fiftyTwoWeekHigh", "hi52"),
                             ("fiftyTwoWeekLow", "lo52"), ("recommendationKey", "rec"),
                             ("numberOfAnalystOpinions", "nanal")]:
                v = info.get(k_src)
                if v not in (None, "", "none", "None"):
                    out[k] = v
            try:
                cal = t.calendar
                ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if isinstance(ed, (list, tuple)) and ed:
                    ed = ed[0]
                if ed is not None:
                    out["earnings"] = str(ed)[:10]
            except Exception:
                pass
            if out:
                return out
        except Exception:
            continue
    return {}

def yf_levels(code, market, cur):
    """6개월 종가로 이평·모멘텀·고저 레벨. 실패 시 {}."""
    if os.environ.get("DASH_OFFLINE") or over_budget():
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}
    syms = [code] if market == "US" else [code + ".KS", code + ".KQ"]
    for sym in syms:
        try:
            h = yf.Ticker(sym).history(period="6mo")["Close"].dropna()
            c = [float(x) for x in h.values]
            if len(c) < 20:
                continue
            ma20 = sum(c[-20:]) / 20
            ma50 = sum(c[-50:]) / len(c[-50:])
            last = cur if cur else c[-1]
            return {"ma20": ma20, "ma50": ma50, "hi6m": max(c), "lo6m": min(c),
                    "vs_ma20": (last - ma20) / ma20 * 100 if ma20 else None,
                    "mom20": (c[-1] - c[-20]) / c[-20] * 100 if c[-20] else None}
        except Exception:
            continue
    return {}

def gemini_market_note():
    """펀드매니저 관점 '오늘의 전략 관점' 1건."""
    if not gem_ready():
        return None
    lines = []
    for k in ["S&P 500", "나스닥", "다우", "코스피", "코스닥", "VIX"]:
        v = idx.get(k)
        if v and v[0] is not None:
            lines.append(f"{k} {v[0]:.0f}" + (f"({v[1]:+.1f}%)" if v[1] is not None else ""))
    for k in ["USD/KRW", "WTI 유가", "금(Gold)"]:
        v = macro.get(k)
        if v and v[0] is not None:
            lines.append(f"{k} {v[0]:.1f}")
    topsig = [f"{s['name']} {s['type']}({s['strength']})" for s in SIGNALS[:6]]
    prompt = ("너는 펀드매니저다. 아래 오늘 지표와 관심 포트폴리오 신호를 근거로 '오늘의 전략 관점'을 한국어로 작성하라. "
              "①오늘 시장의 리스크온/오프 성격과 매크로(금리·달러·유가·VIX·반도체) 해석 "
              "②관심 포트폴리오(빅테크·AI·반도체·에너지/전력·플랫폼)에 주는 함의 "
              "③오늘 특히 주목/경계할 종목·구간 ④향후 며칠 관전 포인트(예정 이벤트). "
              "5~7문장, 데이터 근거로만, 과장·단정적 매매지시 금지(참고용).\n\n"
              "지표: " + " / ".join(lines) + "\n신호: " + (", ".join(topsig) or "특이신호 없음"))
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.35, "maxOutputTokens": 650}}
    try:
        status, d = gem_post(payload, timeout=40)
        if status != 200:
            _diag("market_http", status=status)
            return None
        cand = (d.get("candidates") or [{}])[0]
        txt = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
        if not txt or not has_hangul(txt) or is_garbage(txt):
            _diag("market_empty")
            return None
        _diag("market_ok")
        return txt
    except Exception as e:
        _diag("market_exc", err=repr(e)[:150])
        return None

def gemini_analyst_note(name, code, market, r, price, funda, lv, arts):
    """바이사이드 애널리스트 관점, 혼합(단기 촉매+중기 밸류) 종목 노트."""
    if not gem_ready():
        return None
    ctx = [f"종목: {name}({code}) {'미국' if market == 'US' else '한국'}"]
    ctx.append(f"현재가/등락: {price} ({r:+.1f}%)" if (price and r is not None) else f"현재가: {price}")
    if funda.get("target") and price:
        try:
            gap = (funda["target"] - price) / price * 100
            ctx.append(f"컨센 목표주가: {funda['target']} (현재가 대비 {gap:+.0f}%)")
        except Exception:
            pass
    if funda.get("fpe"):
        ctx.append(f"선행PER: {funda['fpe']}")
    if funda.get("rec"):
        ctx.append(f"애널 투자의견: {funda['rec']} (n={funda.get('nanal', '?')})")
    if funda.get("earnings"):
        ctx.append(f"다음 실적발표(예정): {funda['earnings']}")
    if funda.get("hi52") and funda.get("lo52"):
        ctx.append(f"52주 범위: {funda['lo52']}~{funda['hi52']}")
    if lv.get("ma20"):
        ctx.append(f"20일선 대비 {lv['vs_ma20']:+.1f}%, 20일 모멘텀 {lv.get('mom20') or 0:+.1f}%, "
                   f"6개월 고저 {lv.get('lo6m', 0):.0f}~{lv.get('hi6m', 0):.0f}")
    sg = [f"{s['type']}({s['strength']}): {s['detail']}" for s in SIGNALS if s["name"] == name]
    if sg:
        ctx.append("감지 신호: " + "; ".join(sg))
    heads = [f"- {a['title']}" + (f" [{a['impact']}]" if a.get("impact") else "") for a in arts[:3]]
    if heads:
        ctx.append("오늘 주요 뉴스:\n" + "\n".join(heads))
    prompt = ("너는 바이사이드 주식 애널리스트다. 아래 한 종목 데이터를 근거로 '혼합 관점'(단기 촉매·수급·기술적 레벨 + "
              "중기 실적·밸류에이션)에서 전문가 브리핑을 한국어로 작성하라. 반드시 포함: "
              "①핵심 논지 한 줄 ②단기 촉매/이벤트와 주목 레벨(지지·저항·이평) "
              "③밸류에이션 위치(목표가 갭·PER 해석) ④지금 관찰/경계 포인트와 리스크(손익비). "
              "5~7문장, 데이터에 근거해서만, 과장·단정적 매매지시 금지(참고용). 없는 데이터는 지어내지 말 것.\n\n"
              + "\n".join(ctx))
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.3, "maxOutputTokens": 650}}
    try:
        status, d = gem_post(payload, timeout=40)
        if status != 200:
            _diag("analyst_http", status=status, name=name)
            return None
        cand = (d.get("candidates") or [{}])[0]
        txt = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
        if not txt or not has_hangul(txt) or is_garbage(txt):
            _diag("analyst_empty", name=name)
            return None
        _diag("analyst_ok", name=name)
        return txt
    except Exception as e:
        _diag("analyst_exc", name=name, err=repr(e)[:150])
        return None

# 주목 종목 선별: 신호 강도 + 당일 급변 + 중요 뉴스(impact 태그) 있는 종목 상위 (최대 6, 한도 보호)
def _notability(name):
    (p, r), _, _ = quotes[name]
    sc = 0.0
    strengths = [s["strength"] for s in SIGNALS if s["name"] == name]
    if strengths:
        sc += 10 * max(strengths)
    if r is not None:
        sc += abs(r)
    if any(a.get("impact") for a in news.get(name, [])):
        sc += 3
    return sc

_allnames = [(nm, cd, mk) for _, its in GROUPS for (nm, cd, mk) in its if mk != "PRIVATE"]
_notable = sorted(_allnames, key=lambda t: -_notability(t[0]))
_notable = [t for t in _notable if _notability(t[0]) > 0][:6]

MARKET_NOTE = gemini_market_note()      # 전략 관점(가장 중요) 먼저
ANALYST = []
for _nm, _cd, _mk in _notable:
    (_p, _r), _, _ = quotes[_nm]
    _funda = yf_fundamentals(_cd, _mk)
    _lv = yf_levels(_cd, _mk, _p)
    _note = gemini_analyst_note(_nm, _cd, _mk, _r, _p, _funda, _lv, news.get(_nm, []))
    ANALYST.append({"name": _nm, "code": _cd, "market": _mk, "r": _r, "price": _p,
                    "funda": _funda, "levels": _lv, "note": _note})

# ------------------------------------------------------------------ Gemini 진단 덤프(모든 호출 이후)
try:
    with open(os.path.join(DATA, "gemini_debug.json"), "w", encoding="utf-8") as _f:
        json.dump({
            "generated": NOW.isoformat(timespec="seconds"),
            "model": GEMINI_MODEL,
            "key_present": bool(GEMINI_KEY),
            "key_prefix": GEMINI_KEY[:4] if GEMINI_KEY else "",
            "gem_calls_made": _GEM_CALLS[0],
            "gem_call_cap": GEMINI_MAX_CALLS,
            "gem_quota_exhausted": _GEM_DEAD[0],
            "elapsed_sec": round(time.time() - _START, 1),
            "notable": [t[0] for t in _notable],
            "market_note": bool(MARKET_NOTE),
            "analyst_notes_ok": sum(1 for a in ANALYST if a["note"]),
            "branch_counts": GEMINI_COUNTS,
            "samples": GEMINI_DIAG,
        }, _f, ensure_ascii=False, indent=2)
except Exception:
    pass

# ------------------------------------------------------------------ index.html 조립
def card(label, pair):
    (p, r) = pair if pair else (None, None)
    cls, txt = fmt_pct(r)
    if label == "VIX":
        val = format(p, ",.2f") if p is not None else "—"
    elif label in ("코스피", "코스닥"):
        val = format(int(round(p)), ",") if p is not None else "—"
    else:
        val = format(p, ",.2f") if p is not None else "—"
    return f'<div class="card {cls}"><div class="rail"></div><div class="label">{esc(label)}</div><div class="val num">{val}</div><div class="chg {cls}">{txt}</div></div>'

indices_html = "".join([card("S&P 500", idx["S&P 500"]), card("나스닥", idx["나스닥"]), card("다우", idx["다우"]),
                        card("코스피", idx["코스피"]), card("코스닥", idx["코스닥"]), card("VIX", idx["VIX"])])

def chip(label, pair):
    (p, r) = pair if pair else (None, None)
    cls, txt = fmt_pct(r)
    val = "—" if p is None else format(p, ",.2f")
    return f'<div class="chip"><div class="label">{esc(label)}</div><div class="val num">{val}</div><div class="chg {cls}">{txt}</div></div>'
macro_html = "".join([chip(k, v) for k, v in macro.items()])

rows = []
for gname, items in GROUPS:
    rows.append(f'<tr class="grp"><td colspan="3">{esc(gname)}</td></tr>')
    for name, code, market in items:
        (p, r), mkt, _ = quotes[name]
        if market == "PRIVATE":
            rows.append(f'<tr><td class="nm">{esc(name)}<span class="tick">{esc(code)}</span></td><td class="naq" colspan="2">비상장 · 뉴스만</td></tr>')
            continue
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
        badge = '<span class="badge-en">번역</span>' if market == "US" else ('<span class="badge-pv">비상장</span>' if market == "PRIVATE" else "")
        mv = txt if market != "PRIVATE" else ""
        head = (f'<div class="sh"><span class="snm">{esc(name)}</span>'
                f'<span class="stk-tick">{esc(code)}</span>{badge}'
                f'<span class="smv {cls}">{mv}</span></div>')
        arts = news.get(name, [])
        if arts:
            lis = []
            for a in arts:
                itag = f'<span class="itag">{esc(a["impact"])}</span>' if a.get("impact") else ""
                link = (f'<a href="{esc(a["link"])}" target="_blank" rel="noopener">'
                        f'<span class="tm">{esc(a["time"])}</span>'
                        f'<span><span class="ht">{esc(a["title"])}</span>{itag} <span class="so">· {esc(a["src"])}</span></span></a>')
                extra = ""
                if market == "US":
                    sk = a.get("summary_ko")
                    if sk:
                        is_hl = HL_MARK in sk
                        shown = sk.replace(HL_MARK, "").strip() if is_hl else sk
                        paras = "".join(f"<p>{esc(x)}</p>" for x in shown.split("\n") if x.strip())
                        if is_hl:
                            label = "▼ 한글 요약 (제목 기반 추정 · 본문 미확인)"
                            extra = (f'<details class="trx hl"><summary>{label}</summary>'
                                     f'<div class="trx-body">{paras}</div></details>')
                        else:
                            extra = (f'<details class="trx"><summary>▼ 한글 요약 (AI · 본문)</summary>'
                                     f'<div class="trx-body">{paras}</div></details>')
                    else:
                        extra = '<div class="trx-fail">AI 요약 없음 — 원문 링크 참조</div>'
                lis.append(f'<li>{link}{extra}</li>')
            body = f'<ul>{"".join(lis)}</ul>'
        else:
            src_label = "주요 신문사" if market == "KR" else "주요 매체"
            body = f'<div class="none">— 오늘(KST) {src_label}에서 주가 영향 있는 주목 기사 없음</div>'
        news_blocks.append(f'<div class="stk">{head}{body}</div>')
news_html = "".join(news_blocks)
points_html = "".join([f'<span class="point"><b>{esc(n)}</b> {esc(d)}</span>' for n, d in points]) or '<span class="point">특이 변동·신규 뉴스 없음</span>'
stamp = NOW.strftime("%Y. %m. %d (%a) %H:%M KST")
n_stocks = sum(1 for _, its in GROUPS for _ in its)
n_priv = sum(1 for _, its in GROUPS for (_, _, m) in its if m == "PRIVATE")
n_trade = n_stocks - n_priv
wl_note = f"시세 {n_trade}종" + (f" · 비상장 {n_priv}(뉴스만)" if n_priv else "")

def _an_facts(a):
    f, lv, price, market = a["funda"], a["levels"], a["price"], a["market"]
    chips = []
    def pc(t):
        chips.append(f'<span class="afct">{t}</span>')
    if f.get("target") and price:
        try:
            gap = (f["target"] - price) / price * 100
            gcls = "up" if gap > 0 else ("down" if gap < 0 else "")
            pc(f'목표가 {fmt_price(f["target"], market)} <b class="{gcls}">({gap:+.0f}%)</b>')
        except Exception:
            pass
    if f.get("fpe"):
        try:
            pc(f'선행PER {float(f["fpe"]):.1f}')
        except Exception:
            pass
    if f.get("rec"):
        pc(f'투자의견 {esc(str(f["rec"]).upper())}')
    if f.get("earnings"):
        pc(f'실적발표 {esc(f["earnings"])}')
    if lv.get("vs_ma20") is not None:
        vcls = "up" if lv["vs_ma20"] > 0 else "down"
        pc(f'20일선 <b class="{vcls}">{lv["vs_ma20"]:+.1f}%</b>')
    if lv.get("hi6m") and lv.get("lo6m"):
        pc(f'6개월 {lv["lo6m"]:.0f}~{lv["hi6m"]:.0f}')
    return "".join(chips) or '<span class="afct dim">밸류에이션 데이터 조회 실패</span>'

_an_cards = []
for a in ANALYST:
    _cls, _txt = fmt_pct(a["r"])
    _facts = _an_facts(a)
    if a["note"]:
        _nh = "".join(f"<p>{esc(x)}</p>" for x in a["note"].split("\n") if x.strip())
        _note_block = f'<div class="an-note">{_nh}</div>'
    else:
        _note_block = '<div class="an-note dim">AI 전문 노트 미생성(무료 한도·데이터 제약) — 위 지표 참고</div>'
    _sigtags = "".join(f'<span class="an-sig s{s["strength"]}">{esc(s["type"])}</span>'
                       for s in SIGNALS if s["name"] == a["name"])
    _an_cards.append(
        f'<div class="an-card"><div class="an-h"><span class="an-nm">{esc(a["name"])}'
        f'<span class="tick">{esc(a["code"])}</span></span>'
        f'<span class="pct {_cls} num">{_txt}</span>{_sigtags}</div>'
        f'<div class="an-facts">{_facts}</div>{_note_block}</div>')
analyst_cards_html = "".join(_an_cards) if _an_cards else \
    '<div class="none" style="padding:14px 16px">오늘은 특별히 주목할 신호가 있는 종목이 없습니다 — 관망 구간.</div>'
if MARKET_NOTE:
    _mn = "".join(f"<p>{esc(x)}</p>" for x in MARKET_NOTE.split("\n") if x.strip())
    market_note_html = f'<div class="an-market">{_mn}</div>'
else:
    market_note_html = '<div class="an-market dim">전략 관점 미생성(무료 한도·데이터 제약). 지표·신호는 아래 참고.</div>'
analyst_section = (
    '<section><div class="sec-head"><div class="sec-title">🎓 애널리스트 관점</div>'
    '<div class="sec-note">혼합(단기 촉매+중기 밸류) · AI 종합 · 참고용(투자책임 본인)</div></div>'
    f'<div class="an-mwrap"><div class="an-mlabel">오늘의 전략 관점 (PM)</div>{market_note_html}</div>'
    f'<div class="an-grid">{analyst_cards_html}</div></section>')

HEAD = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
        f'<style>{CSS}</style>')

INDEX = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>아침 시장 인텔리전스</title>{HEAD}</head><body><div class="wrap">
<header><div class="kicker">Morning Market Intelligence</div><h1>아침 시장 인텔리전스</h1>
<div class="stamp"><span><span class="dot"></span>마지막 업데이트 <b>{esc(stamp)}</b></span>
<span>뉴스 · <b>오늘 발행분(KST)</b></span><span>🔄 <b>평일 07·14시 자동 갱신</b></span>
<a class="navlink" href="trends.html">📈 추세 · 이슈 타임라인 →</a></div></header>
<div class="headline"><span class="tag">오늘의 핵심</span><p>{esc(headline)}</p></div>
{analyst_section}
<section><div class="sec-head"><div class="sec-title">오늘의 관찰 포인트</div><div class="sec-note">±3% 이상 변동·신규 뉴스 자동 감지</div></div><div class="points">{points_html}</div></section>
<section><div class="sec-head"><div class="sec-title">주요 지수</div><div class="sec-note">상승 <span class="up">빨강</span> · 하락 <span class="down">파랑</span></div></div><div class="grid">{indices_html}</div></section>
<section><div class="sec-head"><div class="sec-title">매크로 · 환율 · 원자재</div></div><div class="macro">{macro_html}</div></section>
<section><div class="sec-head"><div class="sec-title">관심종목 시세</div><div class="sec-note">{wl_note}</div></div><div class="panel"><div class="tbl-scroll"><table><thead><tr><th>종목</th><th>현재가</th><th>등락</th></tr></thead><tbody>{watchlist_html}</tbody></table></div></div></section>
<section><div class="sec-head"><div class="sec-title">종목별 오늘자 뉴스</div><div class="sec-note">KST 오늘 발행분 · 최대 3건 · 클릭 시 원문</div></div>{news_html}</section>
<footer>이 페이지는 GitHub Actions가 평일 오전 7시·오후 2시(KST)에 <b>자동 갱신</b>합니다. 매 실행마다 시세·뉴스가 <b>data/</b>에 누적 저장되어 <a href="trends.html">추세·이슈 타임라인</a>으로 축적 관찰됩니다.<br>
시세는 조회 시점 값(한국 당일·미국 직전 거래일 종가), 뉴스는 Google News RSS 오늘(KST) 발행분, 미국 헤드라인은 자동 번역본입니다. 정보 제공용이며 최종 투자판단·손익은 본인 책임입니다.</footer>
</div></body></html>"""
with open("index.html", "w", encoding="utf-8") as f:
    f.write(INDEX)

# ------------------------------------------------------------------ trends.html 조립
trend_rows = []
for gname, items in GROUPS:
    inner = []
    for name, code, market in items:
        if market == "PRIVATE":
            continue
        ser = HISTORY.get(code, [])
        latest = ser[-1][1] if ser else None
        # window change
        if len(ser) >= 2:
            wchg = (ser[-1][1] - ser[0][1]) / ser[0][1] * 100 if ser[0][1] else None
        else:
            wchg = None
        wcls, wtxt = fmt_pct(wchg)
        price_txt = fmt_price(latest, market) if latest is not None else "—"
        npts = len(ser)
        inner.append(f'<div class="trow"><div class="tnm">{esc(name)}<span class="tk">{esc(code)}</span></div>'
                     f'<div class="spark">{sparkline(ser, market)}</div>'
                     f'<div class="tp">{price_txt}<div class="d {wcls}">{wtxt} · {npts}p</div></div></div>')
    if inner:
        trend_rows.append(f'<div class="sec-head" style="margin-top:22px"><div class="sec-title">{esc(gname)}</div><div class="sec-note">누적 구간 등락 · 데이터포인트</div></div><div class="panel">{"".join(inner)}</div>')
trends_price_html = "".join(trend_rows)

# 이슈 타임라인
tl_blocks = []
for gname, items in GROUPS:
    for name, code, market in items:
        all_logs = NEWSLOG_D.get(name, [])
        da = len({o["date"] for o in all_logs if daysago(o["date"]) <= 6})
        badge = f'<span class="badge-pv" style="margin-left:auto">최근7일 {da}일 뉴스</span>' if da else ''
        logs = all_logs[:8]
        head = f'<div class="sh">{esc(name)} <span class="tick" style="color:var(--faint);font-family:var(--mono);font-size:11px">{esc(code)}</span>{badge}</div>'
        if not logs:
            tl_blocks.append(f'<div class="tl">{head}<div class="none">— 축적된 이슈 없음(수집 시작 후 누적)</div></div>')
            continue
        body = []
        cur = None
        for o in logs:
            if o["date"] != cur:
                cur = o["date"]
                body.append(f'<div class="day">{esc(cur)}</div>')
            body.append(f'<a href="{esc(o["link"])}" target="_blank" rel="noopener"><span class="tm">{esc(o["time"])}</span><span class="ht">{esc(o["title"])} <span style="color:var(--muted);font-size:11px">· {esc(o["src"])}</span></span></a>')
        tl_blocks.append(f'<div class="tl">{head}{"".join(body)}</div>')
timeline_html = "".join(tl_blocks)

if SIGNALS:
    sig_cards = "".join([
        f'<div class="sig s{s["strength"]}"><div class="sig-h"><span class="sig-nm">{esc(s["name"])}</span>'
        f'<span class="sig-t">{esc(s["type"])}</span><span class="sig-str">{"●"*s["strength"]}</span></div>'
        f'<div class="sig-d">{esc(s["detail"])}</div></div>' for s in SIGNALS])
    signals_html = f'<div class="siggrid">{sig_cards}</div>'
    sig_note = f"{len(SIGNALS)}건 감지 · 참고용(투자책임 본인)"
else:
    signals_html = '<div class="panel"><div style="padding:16px;color:var(--faint)">현재 감지된 신호 없음 — 데이터가 쌓일수록 정확해집니다.</div></div>'
    sig_note = "누적 데이터 기반 자동 감지"

TRENDS = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>추세 · 이슈 타임라인</title>{HEAD}</head><body><div class="wrap">
<header><div class="kicker">Trend & Issue Tracking</div><h1>추세 · 이슈 타임라인</h1>
<div class="stamp"><span><span class="dot"></span>마지막 업데이트 <b>{esc(stamp)}</b></span>
<span>📈 <b>실행마다 시세·뉴스 누적</b></span>
<a class="navlink" href="index.html">← 오늘 대시보드</a></div></header>
<section><div class="sec-head"><div class="sec-title">🎯 주목 신호</div><div class="sec-note">{esc(sig_note)}</div></div>{signals_html}</section>
<section><div class="sec-head"><div class="sec-title">종목별 주가 추세</div><div class="sec-note">누적된 데이터포인트 기반 스파크라인(최근 40개)</div></div>{trends_price_html or '<div class="panel"><div style="padding:16px;color:var(--faint)">데이터 축적 중 — 며칠 실행되면 추세가 그려집니다.</div></div>'}</section>
<section><div class="sec-head"><div class="sec-title">종목별 이슈 타임라인</div><div class="sec-note">날짜순 누적 헤드라인(종목당 최근 8건)</div></div>{timeline_html}</section>
<footer>이 페이지는 <b>data/history.csv</b>(시세 시계열)와 <b>data/news_log.jsonl</b>(뉴스 누적 로그)을 읽어 매 실행마다 다시 그립니다. 실행이 쌓일수록 추세선과 이슈 흐름이 길어집니다. 정보 제공용이며 최종 투자판단·손익은 본인 책임입니다.</footer>
</div></body></html>"""
with open("trends.html", "w", encoding="utf-8") as f:
    f.write(TRENDS)

print(f"built index.html + trends.html | stocks={n_stocks} news={news_cnt} "
      f"hist_symbols={len(HISTORY)} log_symbols={len(NEWSLOG_D)}")
