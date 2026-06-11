"""
make_pdf.py — INSTALL.md → 디자인 PDF 변환
실행: python3 make_pdf.py
"""
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
import re, textwrap

# ── 폰트 등록 ────────────────────────────────────────────────────────────────
FONT_DIR = '/usr/share/fonts/truetype/nanum/'
pdfmetrics.registerFont(TTFont('Nanum',     FONT_DIR + 'NanumGothic.ttf'))
pdfmetrics.registerFont(TTFont('NanumBold', FONT_DIR + 'NanumGothicBold.ttf'))

# ── 컬러 팔레트 ──────────────────────────────────────────────────────────────
C_BG      = HexColor('#0f1117')   # 배경(표지)
C_ACCENT  = HexColor('#f7931a')   # 비트코인 오렌지
C_USDT    = HexColor('#26a17b')   # 테더 초록
C_DARK    = HexColor('#1a1d23')   # 카드 배경
C_BORDER  = HexColor('#2a2d35')
C_TEXT    = HexColor('#e0e0e0')
C_MUTED   = HexColor('#888888')
C_HEAD    = HexColor('#1e2330')   # 섹션 헤더 배경
C_INNER   = HexColor('#4f8ef7')   # 1σ 내부존
C_ROW1    = HexColor('#f5f7ff')
C_ROW2    = HexColor('#eef1f9')
C_WARN    = HexColor('#d4b44a')
C_POS     = HexColor('#2e7d32')
C_POSBG   = HexColor('#e8f5e9')

W, H = A4

# ── 스타일 팩토리 ────────────────────────────────────────────────────────────
def sty(name, **kw):
    defaults = dict(fontName='Nanum', fontSize=10, leading=16,
                    textColor=black, alignment=TA_LEFT)
    defaults.update(kw)
    return ParagraphStyle(name, **defaults)

S_TITLE   = sty('title',  fontName='NanumBold', fontSize=20, leading=28, textColor=white, alignment=TA_CENTER)
S_SUBTITLE= sty('sub',    fontName='Nanum',      fontSize=11, leading=16, textColor=HexColor('#cccccc'), alignment=TA_CENTER)
S_H2      = sty('h2',     fontName='NanumBold', fontSize=14, leading=20, textColor=C_ACCENT, spaceBefore=10, spaceAfter=4)
S_H3      = sty('h3',     fontName='NanumBold', fontSize=11, leading=16, textColor=C_INNER,  spaceBefore=8, spaceAfter=2)
S_BODY    = sty('body',   fontSize=9.5, leading=15, textColor=HexColor('#222222'), spaceAfter=3)
S_CODE    = sty('code',   fontName='Courier', fontSize=8.5, leading=13,
                textColor=HexColor('#1a1d23'), backColor=HexColor('#f0f2f5'),
                borderPad=6, leftIndent=10, spaceAfter=4)
S_NOTE    = sty('note',   fontSize=8.5, leading=13, textColor=HexColor('#555555'),
                backColor=HexColor('#fffde7'), borderPad=4, leftIndent=8)
S_WARN    = sty('warn',   fontName='NanumBold', fontSize=9.5, leading=14,
                textColor=HexColor('#7f4f00'), backColor=HexColor('#fff8e1'),
                borderPad=6, leftIndent=8, spaceAfter=4)
S_TCELL   = sty('tc',     fontSize=9,  leading=13, textColor=HexColor('#222'))
S_TCELL_H = sty('tch',    fontName='NanumBold', fontSize=9, leading=13, textColor=white)
S_BULLET  = sty('bullet', fontSize=9.5, leading=15, textColor=HexColor('#222'), leftIndent=14, spaceAfter=2)
S_FOOTER  = sty('footer', fontSize=7.5, textColor=C_MUTED, alignment=TA_CENTER)

# ── 표지 그리기 (canvas 직접 사용) ───────────────────────────────────────────
def draw_cover(c):
    # 배경
    c.setFillColor(C_BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # 상단 오렌지 바
    c.setFillColor(C_ACCENT)
    c.rect(0, H - 22*mm, W, 8*mm, fill=1, stroke=0)

    # 하단 초록 바
    c.setFillColor(C_USDT)
    c.rect(0, 0, W, 6*mm, fill=1, stroke=0)

    # 중앙 원형 장식
    cx, cy = W/2, H*0.62
    for r, alpha in [(55*mm, 0.08),(42*mm, 0.12),(30*mm, 0.20)]:
        c.setFillColor(HexColor('#f7931a'))
        c.setFillAlpha(alpha)
        c.circle(cx, cy, r, fill=1, stroke=0)
    c.setFillAlpha(1)

    # 비트코인 ₿ 심볼
    c.setFillColor(C_ACCENT)
    c.setFont('NanumBold', 64)
    c.drawCentredString(cx, cy - 22*mm, '₿')

    # 타이틀
    c.setFillColor(white)
    c.setFont('NanumBold', 26)
    c.drawCentredString(cx, H*0.82, 'BTC Grid Prediction System')
    c.setFont('Nanum', 14)
    c.setFillColor(HexColor('#f7931a'))
    c.drawCentredString(cx, H*0.76, '빗썸 BTC/USDT 그리드 매매 전략 시스템')

    # 배지 배경
    badge_y = H*0.70
    bw, bh = 50*mm, 8*mm
    c.setFillColor(HexColor('#1e2a3a'))
    c.roundRect(cx - bw/2, badge_y, bw, bh, 3*mm, fill=1, stroke=0)
    c.setFillColor(HexColor('#4ec94e'))
    c.setFont('NanumBold', 10)
    c.drawCentredString(cx, badge_y + 2.5*mm, 'Beta v0.3')

    # 구분선
    c.setStrokeColor(HexColor('#2a3a4a'))
    c.setLineWidth(0.8)
    c.line(20*mm, H*0.50, W - 20*mm, H*0.50)

    # 메타 정보
    meta = [
        ('대상 거래소', '빗썸 (Bithumb)'),
        ('지원 종목',   'BTC/KRW  ·  USDT/KRW'),
        ('전략 구조',   '4전략 레이어드 그리드 (1σ/2σ)'),
        ('배포 일자',   '2026년 6월'),
    ]
    my = H*0.46
    for label, val in meta:
        c.setFillColor(C_MUTED)
        c.setFont('Nanum', 9)
        c.drawString(W/2 - 55*mm, my, label)
        c.setFillColor(white)
        c.setFont('NanumBold', 9)
        c.drawString(W/2, my, val)
        my -= 7*mm

    # 면책 박스
    bx, by = 18*mm, 12*mm
    bw2, bh2 = W - 36*mm, 22*mm
    c.setFillColor(HexColor('#1a0000'))
    c.setStrokeColor(HexColor('#8b0000'))
    c.setLineWidth(0.6)
    c.roundRect(bx, by, bw2, bh2, 3*mm, fill=1, stroke=1)
    c.setFillColor(HexColor('#ff6b6b'))
    c.setFont('NanumBold', 8.5)
    c.drawCentredString(cx, by + 14*mm, '⚠  투자 면책 고지')
    c.setFillColor(HexColor('#ddbbbb'))
    c.setFont('Nanum', 7.5)
    c.drawCentredString(cx, by + 7*mm,
        '이 앱의 모든 수치는 통계적 추정치로 미래 수익을 보장하지 않습니다.')
    c.drawCentredString(cx, by + 2.5*mm,
        '그리드 봇 운용에 따른 손실 책임은 전적으로 사용자 본인에게 있습니다.')


# ── 본문 빌더 ────────────────────────────────────────────────────────────────
def make_story():
    story = []

    def add(el):
        story.append(el)

    def sp(h=4):
        add(Spacer(1, h*mm))

    def hr(color=C_BORDER, thickness=0.6):
        add(HRFlowable(width='100%', thickness=thickness, color=color, spaceAfter=3))

    def h2(txt):
        add(sp(3))
        add(Paragraph(txt, S_H2))
        hr(C_ACCENT, 1.2)

    def h3(txt):
        add(Paragraph(txt, S_H3))

    def body(txt):
        # Escape HTML specials, handle **bold** and `code`
        txt = txt.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        txt = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', txt)
        txt = re.sub(r'`([^`]+)`', r'<font face="Courier" size="8.5">\1</font>', txt)
        txt = re.sub(r'\[([^\]]+)\]\(https?://[^)]+\)', r'\1', txt)
        add(Paragraph(txt, S_BODY))

    def code(txt):
        txt = txt.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        add(Paragraph(txt, S_CODE))

    def warn(txt):
        txt = txt.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        txt = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', txt)
        add(Paragraph(txt, S_WARN))

    def note(txt):
        txt = txt.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        txt = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', txt)
        add(Paragraph(txt, S_NOTE))

    def bullet(items):
        for item in items:
            item = item.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
            item = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', item)
            item = re.sub(r'`([^`]+)`', r'<font face="Courier" size="8.5">\1</font>', item)
            add(Paragraph('• ' + item, S_BULLET))

    def tbl(headers, rows, col_widths=None):
        th = [Paragraph(h, S_TCELL_H) for h in headers]
        data = [th]
        for row in rows:
            data.append([Paragraph(str(c), S_TCELL) for c in row])
        cw = col_widths or [(W - 40*mm) / len(headers)] * len(headers)
        t = Table(data, colWidths=cw, repeatRows=1)
        style = TableStyle([
            ('BACKGROUND',  (0,0), (-1,0),  C_HEAD),
            ('TEXTCOLOR',   (0,0), (-1,0),  white),
            ('FONTNAME',    (0,0), (-1,0),  'NanumBold'),
            ('FONTSIZE',    (0,0), (-1,-1), 9),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [C_ROW1, C_ROW2]),
            ('GRID',        (0,0), (-1,-1), 0.4, HexColor('#cccccc')),
            ('TOPPADDING',  (0,0), (-1,-1), 4),
            ('BOTTOMPADDING',(0,0),(-1,-1), 4),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('ROUNDEDCORNERS', [4]),
        ])
        t.setStyle(style)
        add(t)
        sp(2)

    # ── 섹션 1: 시스템 요구사항 ──────────────────────────────────────────────
    h2('1.  시스템 요구 사항')
    tbl(
        ['항목', '최소 사양', '권장'],
        [
            ['OS',     'Windows 10', 'Windows 11'],
            ['Python', '3.10 이상', '3.11 또는 3.12'],
            ['RAM',    '512 MB', '2 GB 이상'],
            ['디스크', '200 MB', '1 GB 이상'],
            ['포트',   '8000 (API) · 8501 (Streamlit)', '방화벽 미차단'],
            ['인터넷', '선택 — 없어도 캐시로 동작', '있으면 실시간 가격 수신'],
        ],
        col_widths=[25*mm, 85*mm, 45*mm]
    )

    # ── 섹션 2: Python 설치 ───────────────────────────────────────────────────
    h2('2.  Python 설치 (없는 경우)')

    h3('Windows')
    bullet([
        'https://www.python.org/downloads/ 접속',
        '**Download Python 3.12.x** 클릭',
        '설치 시 **"Add Python to PATH"** 반드시 체크 ✔',
    ])
    sp(1)


    # ── 섹션 3: 앱 설치 ───────────────────────────────────────────────────────
    h2('3.  앱 설치')

    h3('3-1.  압축 파일 해제')
    body('`btc-grid-prediction-beta.zip` 을 원하는 폴더에 압축 해제하세요.')
    code(
        'btc-grid-prediction-beta/\n'
        '├── index.html          ← 대시보드 (브라우저로 열기)\n'
        '├── start.bat           ← Windows 실행 스크립트\n'
                '├── requirements.txt    ← Python 의존성 목록\n'
        '├── api_server.py       ← 실시간 API 서버\n'
        '├── data/\n'
        '│   └── btc_history.json  ← 사전 로드 BTC 히스토리 캐시\n'
        '└── INSTALL.txt         ← 이 안내서'
    )
    sp(1)

    h3('3-2.  최초 실행 (자동 환경 설정)')
    body('**Windows** — `start.bat` 파일을 **더블클릭**')
    sp(1)
    body('실행 시 다음이 자동으로 진행됩니다:')
    bullet([
        '[1/4] 가상환경 생성 (.venv)',
        '[2/4] 의존성 설치 (requests, fastapi, plotly 등)',
        '[3/4] API 서버 시작 (http://localhost:8000)',
        '[4/4] 대시보드 브라우저 열기 (index.html)',
    ])
    note('첫 실행 시 의존성 다운로드에 1~2분이 소요될 수 있습니다.')

    # ── 섹션 4: 실행 방법 비교 ────────────────────────────────────────────────
    h2('4.  실행 방법 비교')
    tbl(
        ['방법', '특징', '명령 / 방법'],
        [
            ['start.bat', '원클릭 실행 (권장)', '더블클릭'],
            ['수동 API 서버',         '로그·디버깅 확인',  'python api_server.py'],
            ['Streamlit 대시보드',    '추가 분석 화면',    'streamlit run gui/app.py'],
            ['CLI 예측 엔진',         '텍스트 리포트 생성','python main.py'],
            ['백테스터',              '전략 수익 검증',    'python backtest.py'],
        ],
        col_widths=[40*mm, 55*mm, 60*mm]
    )

    # ── 섹션 5: 대시보드 사용법 ───────────────────────────────────────────────
    h2('5.  대시보드 사용법')

    h3('탭 구성')
    tbl(
        ['탭', '주요 내용'],
        [
            ['📊 포트폴리오', '4전략 자본 배분 · 월간 P&L 합계 (슬라이더 연동)'],
            ['₿ BTC',        'BTC/KRW 예측 차트 · 박스권 · 그리드 상세'],
            ['💵 USDT',       'USDT/KRW 차트 · 간격 시뮬레이터 · 존별 상세'],
            ['📡 모니터',     '실시간 포지션 현황 · 박스권 이탈 경보'],
            ['💬 피드백',     '피드백 작성'],
        ],
        col_widths=[30*mm, 125*mm]
    )
    sp(1)

    h3('파라미터 조정 (왼쪽 사이드바)')
    tbl(
        ['항목', '설명'],
        [
            ['투입 자본',       '총 운용 자본 (원 단위 직접 입력)'],
            ['직전월 거래량',   '전월 계정 전체 거래량 → 당월 리워드율 자동 결정 (0.003~0.02%)'],
            ['거래량 목표',     '0~200억 직접입력·버튼·슬라이더 연동. 목표 설정 시 3개 변수 자동 추천'],
            ['BTC 비중',        '40~70% (나머지는 USDT 배분)'],
            ['내부존(1σ) 비율', '내부존 배분 비율 — 외부존은 자동 계산'],
            ['KRW 예비금',      '총 자본 중 현금 보유 비율 (20~50%)'],
        ],
        col_widths=[42*mm, 113*mm]
    )
    sp(1)

    h3('USDT 그리드 간격 시뮬레이터')
    bullet([
        'USDT 탭에서 매수·매도 간격(1~5원)을 ＋／－ 버튼으로 조정',
        '조정 즉시 **존별 그리드 상세 테이블** 실시간 업데이트',
        '최적 간격 확인 후 **💾 포트폴리오에 적용** 버튼 클릭',
        '포트폴리오 탭의 4전략 상세 · 월간 P&amp;L 합계에 반영',
    ])

    # ── 섹션 6: 실시간 가격 ───────────────────────────────────────────────────
    h2('6.  실시간 가격 수신')
    body('API 서버(`start.bat`)가 실행 중이면 대시보드가 **60초 주기로 자동 갱신**됩니다.')
    sp(1)
    tbl(
        ['상태', '의미'],
        [
            ['🟢 실시간', '업비트 / 빗썸 API에서 현재가 수신 중'],
            ['🟡 폴백',   '인터넷 미연결 시 사전 캐시 데이터 사용 (기능 정상)'],
        ],
        col_widths=[30*mm, 125*mm]
    )
    note('외부 API가 일시 차단되거나 요청 한도를 초과하면 자동으로 캐시로 전환됩니다.')

    # ── 섹션 7: CLI 예측 엔진 ────────────────────────────────────────────────
    h2('7.  CLI 예측 엔진 사용법')
    code(
        'python main.py                             # 기본 실행 (40M 자본)\n'
        'python main.py --capital 60000000          # 자본 변경\n'
        'python main.py --month 2026-07             # 대상 월 지정\n'
        'python main.py --aggressiveness aggressive # 공격성 조절'
    )
    body('리포트는 `reports/YYYY-MM_report.txt` 에 저장됩니다.')
    tbl(
        ['공격성 레벨', '그리드 간격', '특징'],
        [
            ['conservative', '1.0%', '수수료 절약 · 안정 운용'],
            ['balanced (기본)', '0.5%', '매매 수익 · 리워드 균형'],
            ['aggressive',    '0.3%', '거래량 / 리워드 극대화'],
        ],
        col_widths=[40*mm, 30*mm, 85*mm]
    )

    # ── 섹션 8: API 엔드포인트 ───────────────────────────────────────────────
    h2('8.  API 엔드포인트 (localhost:8000)')
    tbl(
        ['엔드포인트', '설명'],
        [
            ['GET /api/health',   '서버 상태 확인'],
            ['GET /api/price',    'BTC/KRW + USDT/KRW 현재가'],
            ['GET /api/history',  'BTC 일봉 데이터 (캐시 우선, ?days=N)'],
            ['GET /api/predict',  '익월 박스권 예측 (?as_of=&capital=&krw_hold=&aggressiveness=)'],
        ],
        col_widths=[55*mm, 100*mm]
    )

    # ── 섹션 9: FAQ ───────────────────────────────────────────────────────────
    h2('9.  자주 묻는 질문 (FAQ)')

    faqs = [
        ('차트가 안 보여요',
         'start.bat 로 API 서버를 먼저 실행하세요. 서버 없이 파일을 직접 열어도 차트는 표시되며, 실시간 가격만 폴백 데이터로 표시됩니다.'),
        ('포트 8000이 이미 사용 중이에요',
         'start.bat 는 자동으로 기존 프로세스를 종료하고 재시작합니다. Windows 에서는 기존 "API Server" 창을 닫고 다시 실행하세요.'),
        ('패키지 설치 중 에러가 나요',
         'python --version 으로 버전 확인 후 3.10 미만이면 업그레이드가 필요합니다. 사내 네트워크 제한 시 IT 담당자에게 pip 프록시 설정을 문의하세요.'),
        ('가격이 계속 폴백으로 나와요',
         '업비트·빗썸 API는 사내 방화벽에서 차단될 수 있습니다. 일반 가정 인터넷에서는 대부분 실시간 수신됩니다.'),
        ('예측 박스권이 실제와 많이 달라요',
         '본 예측은 과거 변동성 기반 통계 모델(1σ/2σ)로 익월 박스권을 추정합니다. 급등락·뉴스 이벤트는 반영되지 않으므로 참고 지표로만 활용하세요.'),
        ('Streamlit 에서 pandas 에러가 나요',
         '가상환경 활성화 후 pip install pandas 를 실행하거나, start.bat 으로 환경을 재설치하세요.'),
    ]
    for q, a in faqs:
        add(KeepTogether([
            Paragraph(f'Q.  {q}', sty('fq', fontName='NanumBold', fontSize=9.5, leading=14,
                                       textColor=HexColor('#1a3a5f'), spaceBefore=5)),
            Paragraph(f'→  {a}', sty('fa', fontSize=9, leading=14,
                                      textColor=HexColor('#333'), leftIndent=12, spaceAfter=4)),
        ]))
    sp(2)

    # ── 섹션 10: 문제 발생 시 로그 ───────────────────────────────────────────
    h2('10. 문제 발생 시 로그 확인')
    code('python api_server.py         # 서버 로그 터미널 출력\ncurl http://localhost:8000/api/health   # 서버 상태 확인')

    # ── 섹션 11: 디렉토리 구조 ───────────────────────────────────────────────
    h2('11. 디렉토리 구조 (참고)')
    code(
        'btc-grid-prediction-beta/\n'
        '├── index.html              대시보드 (브라우저 단일 파일)\n'
        '├── start.bat / start.bat    실행 스크립트\n'
        '├── api_server.py           FastAPI 실시간 API 서버\n'
        '├── main.py                 오케스트레이터 (CLI)\n'
        '├── backtest.py             백테스터\n'
        '├── config.py               전략 파라미터\n'
        '├── requirements.txt        의존성 목록\n'
        '├── agents/                 예측 · 최적화 에이전트\n'
        '├── services/               예측 파이프라인 · 모니터링\n'
        '├── utils/                  데이터 수집 · 캐시 · 통계\n'
        '└── data/btc_history.json   오프라인 폴백 캐시'
    )

    sp(4)
    add(HRFlowable(width='100%', thickness=0.5, color=C_BORDER))
    add(Paragraph(
        'BTC Grid Prediction System  Beta v0.3  ·  2026.06  ·  '
        '이 문서의 모든 수치는 추정치이며 투자 권유가 아닙니다.',
        S_FOOTER
    ))

    return story


# ── 페이지 레이아웃 콜백 ────────────────────────────────────────────────────
def on_page(canvas, doc):
    pg = doc.page
    if pg == 1:
        return  # 표지는 번호 없음
    canvas.saveState()
    # 헤더 라인
    canvas.setStrokeColor(C_ACCENT)
    canvas.setLineWidth(1.5)
    canvas.line(15*mm, H - 12*mm, W - 15*mm, H - 12*mm)
    canvas.setFillColor(C_ACCENT)
    canvas.setFont('NanumBold', 8)
    canvas.drawString(15*mm, H - 10*mm, 'BTC Grid Prediction System')
    canvas.setFillColor(C_MUTED)
    canvas.setFont('Nanum', 8)
    canvas.drawRightString(W - 15*mm, H - 10*mm, f'{pg - 1}')
    # 하단
    canvas.setStrokeColor(C_BORDER)
    canvas.setLineWidth(0.4)
    canvas.line(15*mm, 10*mm, W - 15*mm, 10*mm)
    canvas.restoreState()


# ── 빌드 ────────────────────────────────────────────────────────────────────
OUT = '/home/user/crypto-predictor/btc-grid-prediction-install.pdf'

doc = SimpleDocTemplate(
    OUT,
    pagesize=A4,
    leftMargin=18*mm, rightMargin=18*mm,
    topMargin=20*mm, bottomMargin=16*mm,
    title='BTC Grid Prediction System — 설치 안내서',
    author='BTC Grid Prediction',
    subject='설치 및 사용 가이드 Beta v0.3',
)

# 표지는 직접 캔버스로 그리고, 이후 페이지에 story 삽입
from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, NextPageTemplate, PageBreak

class CoverDoc(BaseDocTemplate):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        cover_frame  = Frame(0, 0, W, H, leftPadding=0, bottomPadding=0,
                             rightPadding=0, topPadding=0, id='cover')
        body_frame   = Frame(18*mm, 16*mm, W-36*mm, H-36*mm, id='body')
        self.addPageTemplates([
            PageTemplate(id='Cover', frames=[cover_frame],
                         onPage=lambda c,d: draw_cover(c)),
            PageTemplate(id='Body',  frames=[body_frame], onPage=on_page),
        ])

doc2 = CoverDoc(
    OUT,
    pagesize=A4,
    title='BTC Grid Prediction System — 설치 안내서',
)

story = [NextPageTemplate('Body'), PageBreak()] + make_story()
doc2.build(story)

import os
sz = os.path.getsize(OUT)
print(f"✔ PDF 생성 완료: {OUT}  ({sz/1024:.0f} KB)")
PYEOF