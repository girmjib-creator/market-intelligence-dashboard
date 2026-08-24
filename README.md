# 관심종목 시장 인텔리전스 대시보드

GitHub Pages로 배포되는, 매일 자동 갱신되는 개인 시장 대시보드입니다.

- **시세**: 한국·미국 관심종목 9종 + 주요 지수 + 환율·원자재
- **뉴스**: 종목별 오늘(KST) 발행 기사 3건씩 (없으면 "없음"), 미국 헤드라인 자동 한글 번역
- **자동 갱신**: GitHub Actions가 **평일 오전 7시·오후 2시(KST)** 에 재빌드 → 커밋(이력 누적) → Pages 배포
- **고정 URL**: 링크가 바뀌지 않아 카톡·공유에 그대로 사용

## 처음 한 번만 설정하기

### 1. 저장소 만들기
github.com/girmjib-creator 에서 우측 상단 **`+` → New repository**
- Repository name: `market-intelligence-dashboard`
- **Public** 선택 → **Create repository**

### 2. 파일 올리기
새 저장소 화면에서 **Add file → Upload files** →
이 폴더의 파일 전부(`index.html`, `build.py`, `requirements.txt`, `README.md`, 그리고 `.github` 폴더)를 드래그해서 올리고 **Commit changes**.
> `.github/workflows/update.yml` 경로가 유지되어야 자동 실행이 켜집니다.

### 3. GitHub Pages 켜기
저장소 **Settings → Pages** →
- Source: **Deploy from a branch**
- Branch: **main** / **/(root)** → **Save**
- 1~2분 뒤 주소가 나옵니다: **https://girmjib-creator.github.io/market-intelligence-dashboard/**

### 4. (선택) 지금 바로 한 번 돌려보기
저장소 **Actions** 탭 → **Update dashboard** → **Run workflow** 버튼.
1~2분 뒤 최신 데이터로 `index.html`이 갱신됩니다.

## 관심종목 바꾸기
`build.py` 상단 `GROUPS`(종목·코드)와 `NEWS_Q`(뉴스 검색어)만 수정해 커밋하면 됩니다.

## 참고
- 시세: FinanceDataReader / yfinance · 뉴스: Google News RSS · 번역: deep-translator
- 정보 제공용이며 최종 투자판단·손익은 본인 책임입니다.
