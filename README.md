# 미국 주식 스코어보드

S&P 500 종목에 종합 점수(0~100)와 근거를 보여주는 판단 보조 도구.
GitHub Pages(정적 사이트) + GitHub Actions(데이터 수집)로 돌아가며 서버가 없다.

> 투자 판단의 책임은 본인에게 있으며, 본 정보는 투자 권유가 아니다.

## 지금 구현된 범위 (기획서 1단계 + 실시간 화면)

| 항목 | 상태 |
|------|------|
| 펀더멘털·밸류에이션 점수 (섹터 내 백분위) | 구현 |
| 실시간 주가 (브라우저 → Finnhub 웹소켓) | 구현 |
| 뉴스 헤드라인 수집 (RSS 8곳 + Finnhub), 종목 매핑, 중복 묶기 | 구현 |
| 투자자 13F / 수급 / 뉴스 감성(Claude) / 시장 국면 / 백테스트 | 다음 단계 — 점수에서 제외하고 가중치 재분배 |

## 갱신 주기

| 데이터 | 방식 | 지연 |
|--------|------|------|
| 주가 | 브라우저가 사용자 키로 Finnhub 웹소켓 구독 (화면에 보이는 50개) | 체결 즉시 |
| 이슈(뉴스) | 키가 있으면 브라우저가 1분마다 직접 확인 + Actions 가 10분마다 RSS 수집 | 1분 / 10분 이상 |
| 점수 | Actions 가 평일 21:30 UTC(미국 장 마감 후)에 재계산 | 하루 1회 |

GitHub Actions 의 예약 실행은 정시가 보장되지 않아 몇 분에서 수십 분 늦을 수 있다.
그래서 "바로바로"가 필요한 주가와 이슈는 브라우저가 직접 받도록 했다.

## 배포 (GitHub)

1. GitHub 에 **공개(public)** 저장소를 만들고 이 폴더를 `main` 브랜치로 올린다.
   (비공개 저장소는 무료 플랜에서 Pages 를 쓸 수 없고, 10분 주기 실행이 Actions 무료 시간을 다 쓴다.)
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret**
   이름 `FINNHUB_API_KEY`, 값은 [finnhub.io](https://finnhub.io/register) 에서 받은 무료 키.
3. 올리면 워크플로가 자동으로 돌아 `gh-pages` 브랜치를 만든다. 첫 실행은 S&P 500 전체를 수집하므로 약 35분 걸린다.
4. **Settings → Pages → Source: Deploy from a branch → `gh-pages` / `(root)`** 로 지정한다.
5. 사이트를 열고 **설정** 탭에 같은 Finnhub 키를 넣으면 실시간 주가가 켜진다.
   키는 그 브라우저에만 저장되고 저장소나 사이트에는 올라가지 않는다.

참고: 공개 저장소에서 60일간 커밋이 없으면 GitHub 가 예약 실행을 꺼 버린다. Actions 탭에서 다시 켜면 된다.

## 로컬 실행

```bash
pip install -r requirements.txt
```

```bash
python pipeline.py sample
```

```bash
python -m http.server 8765 --directory web
```

실제 데이터로 돌리려면 `.env.example` 을 `.env` 로 복사해 키를 넣은 뒤:

```bash
python pipeline.py news
```

```bash
python pipeline.py scores --limit 20
```

`scores` 는 끝에 지표별 데이터 보유 종목 수를 출력한다. 어떤 지표가 0 이면
`config/weights.yaml` 의 `keys`(Finnhub 필드 이름)를 고친다.

테스트:

```bash
python -m unittest discover -s tests -t .
```

## 구조

```
collectors/   finnhub.py(시세·재무), news.py(RSS·매핑·중복 제거), universe.py(S&P 500 목록)
analysis/     scoring.py (백분위 정규화, 가중 평균, 결측 재분배, 국면 계수)
config/       weights.yaml, media.yaml, investors.yaml, settings.yaml
pipeline.py   news | scores | auto | sample
web/          index.html, app.js, style.css, data/*.json (수집 결과)
tests/
.github/workflows/update.yml
```

기획서와 다른 점: 서버가 없는 GitHub Pages 배포라 DB·백엔드 API 대신 `web/data/*.json` 을 저장소로 쓴다.
백테스트(6단계)에 과거 점수가 필요해지면 그때 일별 스냅샷 저장을 추가한다.

## 설정 바꾸기

- 점수 가중치·지표: `config/weights.yaml` (가중치는 출발점으로 잡은 임의값)
- 언론사 RSS: `config/media.yaml` (막힌 피드는 `enabled: false`)
- 투자자·CIK: `config/investors.yaml` (2단계에서 사용, CIK 는 EDGAR 에서 재확인 필요)

## 알아 둘 한계

- 뉴스는 헤드라인·요약·원문 링크만 저장한다. 유료 본문은 수집하지 않는다.
- 헤드라인 → 종목 매핑은 회사명 일치 방식이라 누락·오탐이 있다.
- 장 상태 표시는 미국 공휴일을 반영하지 않는다.
- 다른 사람에게 공개하거나 유료로 제공하면 한국에서는 유사투자자문업 신고 등 규제 대상이 될 수 있다. 공개 전 전문가 확인이 필요하다.
