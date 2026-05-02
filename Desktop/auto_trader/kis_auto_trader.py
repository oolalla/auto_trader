"""
============================================================
한국투자증권 API 자동매매 시스템
전략: 52주 신고가 + 이동평균선 상승추세 종목 매수
손절: 평균단가 -8% / 일일 손실한도: 전체 자본금 -5%
============================================================
"""

# ============================================================
# [1] 라이브러리 임포트
# - 각 라이브러리가 어떤 역할을 하는지 명시
# ============================================================
import os                          # 환경변수 읽기 (API 키 보안 관리)
import time                        # 시간 지연 처리 (API 호출 간격 조절)
import logging                     # 로그 기록 (매매 내역, 오류 추적)
import requests                    # HTTP 요청 (KIS REST API 호출)
import json                        # JSON 데이터 파싱
from datetime import datetime, timedelta  # 날짜/시간 처리
from dotenv import load_dotenv     # .env 파일에서 API 키 로드
import pandas as pd                # 주가 데이터 분석 (이동평균 계산)
import schedule                    # 정해진 시간에 함수 자동 실행

# ============================================================
# [2] 환경변수 로드 및 로깅 설정
# ============================================================

# .env 파일에서 API 키 등 민감정보를 불러옴
# .env 파일 예시:
#   APP_KEY=PSxxx...
#   APP_SECRET=xxx...
#   ACCOUNT_NO=50123456-01
#   IS_PAPER=true   # 모의투자 여부
load_dotenv()

# 로그 설정: 터미널 출력 + 파일 저장 동시 기록
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                        # 터미널 출력
        logging.FileHandler("trading_log.txt", encoding="utf-8")  # 파일 저장
    ]
)
log = logging.getLogger(__name__)


# ============================================================
# [3] 설정값 (전략 파라미터)
# - 여기서만 수정하면 전체 전략이 바뀜
# ============================================================
CONFIG = {
    # API 인증 정보 (.env 파일에서 읽어옴)
    "APP_KEY":    os.getenv("APP_KEY"),
    "APP_SECRET": os.getenv("APP_SECRET"),
    "ACCOUNT_NO": os.getenv("ACCOUNT_NO"),   # 예: "50123456-01"
    "IS_PAPER":   os.getenv("IS_PAPER", "true").lower() == "true",  # True=모의투자

    # API 엔드포인트
    "BASE_URL_PAPER": "https://openapivts.koreainvestment.com:29443",  # 모의투자
    "BASE_URL_REAL":  "https://openapi.koreainvestment.com:9443",       # 실전투자

    # 전략 파라미터
    "STOP_LOSS_RATE":     -0.08,   # 손절선: 평균단가 대비 -8%
    "DAILY_LOSS_LIMIT":   -0.05,   # 일일 손실한도: 전체 자본금 대비 -5%
    "MA_SHORT":           5,        # 단기 이동평균 (5일)
    "MA_MID":             20,       # 중기 이동평균 (20일)
    "MA_LONG":            60,       # 장기 이동평균 (60일)

    # 대형주 코스피 시가총액 상위 종목 (필요시 추가/수정)
    "LARGE_CAP_STOCKS": [
        "005930",  # 삼성전자
        "000660",  # SK하이닉스
        "005380",  # 현대차
        "035420",  # NAVER
        "005490",  # POSCO홀딩스
        "051910",  # LG화학
        "006400",  # 삼성SDI
        "035720",  # 카카오
        "003550",  # LG
        "068270",  # 셀트리온
        "028260",  # 삼성물산
        "066570",  # LG전자
        "032830",  # 삼성생명
        "096770",  # SK이노베이션
        "017670",  # SK텔레콤
        "030200",  # KT
        "055550",  # 신한지주
        "105560",  # KB금융
        "086790",  # 하나금융지주
        "316140",  # 우리금융지주
    ],

    # 매수당 투자금 비율 (전체 자본금의 10%씩 분산 투자)
    "INVEST_RATIO_PER_STOCK": 0.10,

    # 최대 보유 종목 수 (분산투자)
    "MAX_POSITIONS": 5,
}


# ============================================================
# [4] KIS API 클라이언트 클래스
# - 토큰 발급, 주가 조회, 주문 실행 등 API 통신 담당
# ============================================================
class KISClient:
    """한국투자증권 REST API 래퍼 클래스"""

    def __init__(self):
        # 모의/실전 투자에 따라 URL 분기
        self.base_url = (
            CONFIG["BASE_URL_PAPER"] if CONFIG["IS_PAPER"]
            else CONFIG["BASE_URL_REAL"]
        )
        self.app_key    = CONFIG["APP_KEY"]
        self.app_secret = CONFIG["APP_SECRET"]
        self.account_no = CONFIG["ACCOUNT_NO"]

        # 액세스 토큰 (API 호출에 필요, 1일 유효)
        self.access_token = None
        self.token_expired_at = None

        log.info(f"KIS 클라이언트 초기화 완료 ({'모의투자' if CONFIG['IS_PAPER'] else '실전투자'})")

    # ----------------------------------------------------------
    # [4-1] 액세스 토큰 발급
    # - API 호출 전 반드시 필요한 인증 토큰
    # - 유효기간: 약 24시간
    # ----------------------------------------------------------
    def get_access_token(self):
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type":  "client_credentials",
            "appkey":      self.app_key,
            "appsecret":   self.app_secret,
        }
        resp = requests.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()

        self.access_token = data["access_token"]
        # 만료 시각 계산 (현재시각 + 23시간 50분으로 여유 있게 설정)
        self.token_expired_at = datetime.now() + timedelta(hours=23, minutes=50)
        log.info("액세스 토큰 발급 완료")

    # ----------------------------------------------------------
    # [4-2] 토큰 유효성 확인 및 자동 갱신
    # ----------------------------------------------------------
    def ensure_token(self):
        """토큰이 없거나 만료 직전이면 재발급"""
        if (self.access_token is None or
                datetime.now() >= self.token_expired_at):
            self.get_access_token()

    # ----------------------------------------------------------
    # [4-3] 공통 헤더 생성
    # - 모든 API 요청에 공통으로 들어가는 헤더
    # ----------------------------------------------------------
    def _headers(self, tr_id: str) -> dict:
        self.ensure_token()
        return {
            "Content-Type":   "application/json; charset=utf-8",
            "authorization":  f"Bearer {self.access_token}",
            "appkey":         self.app_key,
            "appsecret":      self.app_secret,
            "tr_id":          tr_id,   # 거래ID (조회/주문별로 다름)
            "custtype":       "P",     # 개인 투자자
        }

    # ----------------------------------------------------------
    # [4-4] 일봉 데이터 조회 (이동평균 계산용)
    # - 최근 N일간의 OHLCV 데이터 반환
    # ----------------------------------------------------------
    def fetch_daily_prices(self, stock_code: str, days: int = 70) -> pd.DataFrame:
        """
        일봉 데이터 조회
        stock_code: 종목코드 (예: "005930")
        days: 조회 기간 (이동평균 60일 계산을 위해 70일 이상 권장)
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"

        end_date   = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        # *2 하는 이유: 주말/공휴일 제외하면 실제 거래일 수가 절반이므로

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",          # J=코스피, Q=코스닥
            "FID_INPUT_ISCD":         stock_code,
            "FID_INPUT_DATE_1":       start_date,
            "FID_INPUT_DATE_2":       end_date,
            "FID_PERIOD_DIV_CODE":    "D",           # D=일봉
            "FID_ORG_ADJ_PRC":        "0",           # 0=수정주가
        }
        # 일봉 조회 거래ID (모의/실전 동일)
        resp = requests.get(url, headers=self._headers("FHKST03010100"), params=params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            log.warning(f"[{stock_code}] 일봉 조회 실패: {data.get('msg1')}")
            return pd.DataFrame()

        # API 응답을 DataFrame으로 변환
        rows = data.get("output2", [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df[["stck_bsop_date", "stck_clpr", "stck_hgpr", "stck_lwpr", "acml_vol"]]
        df.columns = ["date", "close", "high", "low", "volume"]

        # 문자열 → 숫자형 변환
        for col in ["close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 날짜 오름차순 정렬 (오래된 날짜 → 최신 날짜)
        df = df.sort_values("date").reset_index(drop=True)

        return df.tail(days)  # 최근 N일만 반환

    # ----------------------------------------------------------
    # [4-5] 현재가 조회
    # ----------------------------------------------------------
    def fetch_current_price(self, stock_code: str) -> dict:
        """현재가, 전일 종가 등 실시간 시세 조회"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         stock_code,
        }
        resp = requests.get(url, headers=self._headers("FHKST01010100"), params=params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            return {}

        output = data.get("output", {})
        return {
            "stock_code":    stock_code,
            "current_price": int(output.get("stck_prpr", 0)),   # 현재가
            "prev_close":    int(output.get("stck_sdpr", 0)),    # 전일 종가
            "high_52w":      int(output.get("stck_hgpr", 0)),    # 52주 최고가
            "low_52w":       int(output.get("stck_lwpr", 0)),    # 52주 최저가
            "open":          int(output.get("stck_oprc", 0)),    # 당일 시가
        }

    # ----------------------------------------------------------
    # [4-6] 잔고 조회 (보유 종목 + 예수금)
    # ----------------------------------------------------------
    def fetch_balance(self) -> dict:
        """
        계좌 잔고 조회
        반환값: {
            "holdings": [{"stock_code", "qty", "avg_price", "eval_profit_loss"}, ...],
            "deposit":  예수금 (원),
            "total_eval": 총 평가금액
        }
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"

        # 모의/실전 거래ID 다름
        tr_id = "VTTC8434R" if CONFIG["IS_PAPER"] else "TTTC8434R"

        account_prefix = self.account_no.split("-")[0]   # "50123456"
        account_suffix = self.account_no.split("-")[1]   # "01"

        params = {
            "CANO":                 account_prefix,
            "ACNT_PRDT_CD":         account_suffix,
            "AFHR_FLPR_YN":         "N",    # 시간외 단가 여부
            "OFL_YN":               "",
            "INQR_DVSN":            "02",   # 02=종목별
            "UNPR_DVSN":            "01",
            "FUND_STTL_ICLD_YN":    "N",
            "FNCG_AMT_AUTO_RDPT_YN":"N",
            "PRCS_DVSN":            "00",
            "CTX_AREA_FK100":       "",
            "CTX_AREA_NK100":       "",
        }

        resp = requests.get(url, headers=self._headers(tr_id), params=params)
        resp.raise_for_status()
        data = resp.json()

        holdings = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            holdings.append({
                "stock_code":        item.get("pdno"),            # 종목코드
                "stock_name":        item.get("prdt_name"),       # 종목명
                "qty":               qty,                          # 보유수량
                "avg_price":         float(item.get("pchs_avg_pric", 0)),  # 평균단가
                "current_price":     int(item.get("prpr", 0)),    # 현재가
                "eval_profit_loss":  float(item.get("evlu_pfls_amt", 0)),  # 평가손익
                "profit_loss_rate":  float(item.get("evlu_pfls_rt", 0)),   # 수익률(%)
            })

        output2 = data.get("output2", [{}])[0]
        return {
            "holdings":   holdings,
            "deposit":    int(output2.get("dnca_tot_amt", 0)),       # 예수금
            "total_eval": int(output2.get("tot_evlu_amt", 0)),       # 총 평가금액
        }

    # ----------------------------------------------------------
    # [4-7] 시장가 매수 주문
    # ----------------------------------------------------------
    def place_market_buy(self, stock_code: str, qty: int) -> bool:
        """
        시장가 매수 주문
        stock_code: 종목코드
        qty: 매수 수량
        반환: 성공 여부
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = "VTTC0802U" if CONFIG["IS_PAPER"] else "TTTC0802U"  # 시장가 매수

        account_prefix = self.account_no.split("-")[0]
        account_suffix = self.account_no.split("-")[1]

        body = {
            "CANO":         account_prefix,
            "ACNT_PRDT_CD": account_suffix,
            "PDNO":         stock_code,      # 종목코드
            "ORD_DVSN":     "01",            # 01=시장가
            "ORD_QTY":      str(qty),        # 주문수량
            "ORD_UNPR":     "0",             # 시장가는 0으로 입력
        }

        resp = requests.post(url, headers=self._headers(tr_id), json=body)
        resp.raise_for_status()
        result = resp.json()

        if result.get("rt_cd") == "0":
            log.info(f"[매수 성공] {stock_code} {qty}주 시장가 매수")
            return True
        else:
            log.error(f"[매수 실패] {stock_code}: {result.get('msg1')}")
            return False

    # ----------------------------------------------------------
    # [4-8] 시장가 매도 주문 (손절 포함)
    # ----------------------------------------------------------
    def place_market_sell(self, stock_code: str, qty: int, reason: str = "매도") -> bool:
        """
        시장가 매도 주문
        reason: 매도 사유 (로그 기록용, 예: "손절", "일일손실한도초과")
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = "VTTC0801U" if CONFIG["IS_PAPER"] else "TTTC0801U"  # 시장가 매도

        account_prefix = self.account_no.split("-")[0]
        account_suffix = self.account_no.split("-")[1]

        body = {
            "CANO":         account_prefix,
            "ACNT_PRDT_CD": account_suffix,
            "PDNO":         stock_code,
            "ORD_DVSN":     "01",    # 시장가
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     "0",
        }

        resp = requests.post(url, headers=self._headers(tr_id), json=body)
        resp.raise_for_status()
        result = resp.json()

        if result.get("rt_cd") == "0":
            log.info(f"[{reason}] {stock_code} {qty}주 시장가 매도")
            return True
        else:
            log.error(f"[매도 실패] {stock_code}: {result.get('msg1')}")
            return False


# ============================================================
# [5] 매매 전략 분석 클래스
# - 52주 신고가 판단, 이동평균 상승추세 판단
# ============================================================
class StrategyAnalyzer:
    """전략 분석: 52주 신고가 + 이동평균선 상승추세"""

    # ----------------------------------------------------------
    # [5-1] 52주 신고가 여부 판단
    # - 현재가가 최근 52주(252거래일) 최고가와 동일하거나 근접하면 신고가로 판단
    # ----------------------------------------------------------
    @staticmethod
    def is_52week_high(price_info: dict, threshold: float = 0.99) -> bool:
        """
        52주 신고가 여부 확인
        threshold: 현재가가 52주 최고가의 몇 % 이상이면 신고가로 볼지 (기본 99%)
                   (당일 최고가 갱신이 아닌 근접도로도 판단 가능)
        """
        current = price_info.get("current_price", 0)
        high_52w = price_info.get("high_52w", 0)

        if high_52w <= 0:
            return False

        ratio = current / high_52w
        is_high = ratio >= threshold

        if is_high:
            log.info(
                f"[52주 신고가] {price_info['stock_code']} "
                f"현재가:{current:,} / 52주최고:{high_52w:,} ({ratio:.1%})"
            )
        return is_high

    # ----------------------------------------------------------
    # [5-2] 이동평균선 상승추세 판단
    # - 5일 > 20일 > 60일 순으로 정배열되어 있으면 상승추세
    # - 각 이동평균선도 상승 기울기여야 함
    # ----------------------------------------------------------
    @staticmethod
    def is_ma_uptrend(df: pd.DataFrame) -> bool:
        """
        이동평균선 상승추세 확인
        조건:
          1. 현재 MA5 > MA20 > MA60 (정배열)
          2. MA5, MA20, MA60 각각 직전 대비 상승 중 (기울기 양수)
        """
        # 데이터 부족 시 판단 불가
        required = CONFIG["MA_LONG"] + 1  # 61일 이상 필요
        if len(df) < required:
            log.warning("데이터 부족으로 이동평균 판단 불가")
            return False

        # 이동평균 계산
        df = df.copy()
        df["ma5"]  = df["close"].rolling(CONFIG["MA_SHORT"]).mean()
        df["ma20"] = df["close"].rolling(CONFIG["MA_MID"]).mean()
        df["ma60"] = df["close"].rolling(CONFIG["MA_LONG"]).mean()

        # 최근 2일 값 추출 (오늘 vs 어제)
        today = df.iloc[-1]
        yesterday = df.iloc[-2]

        ma5_now,  ma5_prev  = today["ma5"],  yesterday["ma5"]
        ma20_now, ma20_prev = today["ma20"], yesterday["ma20"]
        ma60_now, ma60_prev = today["ma60"], yesterday["ma60"]

        # 조건 1: 정배열 (MA5 > MA20 > MA60)
        is_aligned = (ma5_now > ma20_now > ma60_now)

        # 조건 2: 각 이동평균선의 기울기가 양수 (상승 중)
        is_ma5_rising  = ma5_now  > ma5_prev
        is_ma20_rising = ma20_now > ma20_prev
        is_ma60_rising = ma60_now > ma60_prev

        result = is_aligned and is_ma5_rising and is_ma20_rising and is_ma60_rising

        log.info(
            f"이동평균 분석 | "
            f"MA5:{ma5_now:.0f}(↑{is_ma5_rising}) "
            f"MA20:{ma20_now:.0f}(↑{is_ma20_rising}) "
            f"MA60:{ma60_now:.0f}(↑{is_ma60_rising}) | "
            f"정배열:{is_aligned} | 최종:{result}"
        )
        return result


# ============================================================
# [6] 리스크 관리 클래스
# - 손절 판단, 일일 손실한도 초과 판단
# ============================================================
class RiskManager:
    """리스크 관리: 손절 / 일일 손실한도"""

    def __init__(self, initial_capital: int):
        """
        initial_capital: 당일 시작 시 총 평가금액 (손실한도 기준)
        """
        self.initial_capital = initial_capital
        self.daily_loss_triggered = False  # 일일 손실한도 초과 플래그
        log.info(f"리스크 관리 초기화 | 기준 자본금: {initial_capital:,}원")

    # ----------------------------------------------------------
    # [6-1] 개별 종목 손절 판단
    # ----------------------------------------------------------
    def should_stop_loss(self, holding: dict) -> bool:
        """
        손절 여부 판단
        holding: fetch_balance()에서 반환된 보유 종목 딕셔너리
        조건: 현재가 < 평균단가 × (1 - 0.08)
        """
        avg_price     = holding["avg_price"]
        current_price = holding["current_price"]

        if avg_price <= 0:
            return False

        loss_rate = (current_price - avg_price) / avg_price
        stop_loss_price = avg_price * (1 + CONFIG["STOP_LOSS_RATE"])  # 평균단가 × 0.92

        triggered = current_price <= stop_loss_price

        if triggered:
            log.warning(
                f"[손절 신호] {holding['stock_code']} {holding.get('stock_name', '')} | "
                f"평균단가:{avg_price:,.0f} → 현재가:{current_price:,} | "
                f"손실률:{loss_rate:.2%} (기준: {CONFIG['STOP_LOSS_RATE']:.0%})"
            )
        return triggered

    # ----------------------------------------------------------
    # [6-2] 일일 손실한도 초과 판단
    # ----------------------------------------------------------
    def check_daily_loss_limit(self, current_total_eval: int) -> bool:
        """
        일일 손실한도 초과 여부 확인
        current_total_eval: 현재 시점의 총 평가금액
        조건: 총 평가금액이 기준 자본금 × (1 - 0.05) 미만이면 매매 정지
        """
        if self.daily_loss_triggered:
            return True  # 이미 트리거된 경우 계속 차단

        loss_rate  = (current_total_eval - self.initial_capital) / self.initial_capital
        loss_limit = CONFIG["DAILY_LOSS_LIMIT"]

        if loss_rate <= loss_limit:
            log.warning(
                f"[일일 손실한도 초과] 당일 손실률:{loss_rate:.2%} | "
                f"기준 자본금:{self.initial_capital:,} → 현재:{current_total_eval:,}"
            )
            self.daily_loss_triggered = True
            return True

        return False


# ============================================================
# [7] 자동매매 메인 엔진
# - 전체 루프를 관리하며 매매 실행
# ============================================================
class AutoTrader:
    """자동매매 메인 엔진"""

    def __init__(self):
        self.kis      = KISClient()
        self.analyzer = StrategyAnalyzer()
        self.risk     = None          # 장 시작 시 초기화
        self.is_running = True        # 매매 활성 여부

        # 당일 매수 완료된 종목 (중복 매수 방지)
        self.bought_today: set = set()

    # ----------------------------------------------------------
    # [7-1] 장 시작 초기화
    # - 매일 오전 8:50에 실행
    # ----------------------------------------------------------
    def morning_init(self):
        """장 시작 전 초기화 루틴"""
        log.info("=" * 60)
        log.info(f"[장 시작 초기화] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # 토큰 갱신
        self.kis.get_access_token()

        # 현재 잔고 조회 → 일일 손실한도 기준 자본금 설정
        balance = self.kis.fetch_balance()
        initial_capital = balance["total_eval"]

        self.risk = RiskManager(initial_capital)
        self.bought_today = set()
        self.is_running = True

        log.info(f"기준 자본금: {initial_capital:,}원 | 예수금: {balance['deposit']:,}원")
        log.info("=" * 60)

    # ----------------------------------------------------------
    # [7-2] 조건 검색: 매수 후보 종목 찾기
    # - 대형주 중 52주 신고가 + 이동평균 상승추세 종목 선별
    # ----------------------------------------------------------
    def scan_buy_candidates(self) -> list:
        """매수 후보 종목 스캔"""
        candidates = []
        log.info(f"[종목 스캔 시작] 대상: {len(CONFIG['LARGE_CAP_STOCKS'])}개 대형주")

        for stock_code in CONFIG["LARGE_CAP_STOCKS"]:
            try:
                # API 과호출 방지 (초당 5회 제한)
                time.sleep(0.3)

                # 현재가 조회
                price_info = self.kis.fetch_current_price(stock_code)
                if not price_info or price_info["current_price"] == 0:
                    continue

                # 조건 1: 52주 신고가 여부
                if not StrategyAnalyzer.is_52week_high(price_info):
                    continue

                # 일봉 데이터 조회 (이동평균 계산용)
                df = self.kis.fetch_daily_prices(stock_code, days=70)
                if df.empty:
                    continue

                # 조건 2: 이동평균 상승추세 여부
                if not StrategyAnalyzer.is_ma_uptrend(df):
                    continue

                # 두 조건 모두 통과 → 후보에 추가
                candidates.append({
                    "stock_code":    stock_code,
                    "current_price": price_info["current_price"],
                    "high_52w":      price_info["high_52w"],
                })
                log.info(f"[후보 선정] {stock_code} 현재가:{price_info['current_price']:,}")

            except Exception as e:
                log.error(f"[스캔 오류] {stock_code}: {e}")

        log.info(f"[스캔 완료] 후보 종목 {len(candidates)}개 발견")
        return candidates

    # ----------------------------------------------------------
    # [7-3] 매수 실행
    # - 후보 종목 중 아직 매수하지 않은 종목을 투자금 비율에 맞게 매수
    # ----------------------------------------------------------
    def execute_buy(self, candidates: list):
        """후보 종목 매수 실행"""

        # 현재 잔고 확인
        balance = self.kis.fetch_balance()
        deposit = balance["deposit"]      # 사용 가능 예수금
        total   = balance["total_eval"]   # 총 평가금액
        current_positions = len(balance["holdings"])

        for stock in candidates:
            stock_code    = stock["stock_code"]
            current_price = stock["current_price"]

            # 이미 오늘 매수한 종목은 건너뜀 (중복 매수 방지)
            if stock_code in self.bought_today:
                continue

            # 이미 보유 중인 종목도 건너뜀
            held_codes = [h["stock_code"] for h in balance["holdings"]]
            if stock_code in held_codes:
                continue

            # 최대 보유 종목 수 초과 시 매수 중단
            if current_positions >= CONFIG["MAX_POSITIONS"]:
                log.info(f"최대 보유 종목 수 도달 ({CONFIG['MAX_POSITIONS']}개), 매수 중단")
                break

            # 종목당 투자금액 계산 (총 평가금액의 10%)
            invest_amount = int(total * CONFIG["INVEST_RATIO_PER_STOCK"])

            # 예수금 부족 시 건너뜀
            if deposit < invest_amount:
                log.warning(f"예수금 부족: {deposit:,}원 < 투자금 {invest_amount:,}원")
                continue

            # 매수 수량 계산 (소수점 이하 버림)
            qty = invest_amount // current_price
            if qty <= 0:
                log.warning(f"[{stock_code}] 투자금 부족으로 1주도 매수 불가")
                continue

            # 실제 매수 주문
            log.info(
                f"[매수 시도] {stock_code} | "
                f"투자금:{invest_amount:,}원 | 현재가:{current_price:,} | {qty}주"
            )
            success = self.kis.place_market_buy(stock_code, qty)

            if success:
                self.bought_today.add(stock_code)
                deposit -= invest_amount        # 예수금 차감 (추정)
                current_positions += 1
                time.sleep(0.5)                 # 주문 후 잠시 대기

    # ----------------------------------------------------------
    # [7-4] 손절 모니터링 및 실행
    # - 보유 종목 중 -8% 이하 종목 자동 손절
    # ----------------------------------------------------------
    def monitor_stop_loss(self):
        """보유 종목 손절 모니터링"""
        balance = self.kis.fetch_balance()

        for holding in balance["holdings"]:
            # 손절 조건 확인
            if self.risk.should_stop_loss(holding):
                # 보유 수량 전량 매도
                self.kis.place_market_sell(
                    holding["stock_code"],
                    holding["qty"],
                    reason="손절(-8%)"
                )
                time.sleep(0.5)

    # ----------------------------------------------------------
    # [7-5] 일일 손실한도 모니터링
    # - 전체 자본금 -5% 초과 시 전체 청산 후 매매 정지
    # ----------------------------------------------------------
    def monitor_daily_loss(self):
        """일일 손실한도 모니터링"""
        balance = self.kis.fetch_balance()
        current_total = balance["total_eval"]

        if self.risk.check_daily_loss_limit(current_total):
            log.warning("[매매 정지] 일일 손실한도(-5%) 초과! 전체 포지션 청산 시작")
            self.is_running = False  # 추가 매수 차단

            # 보유 종목 전량 매도
            for holding in balance["holdings"]:
                self.kis.place_market_sell(
                    holding["stock_code"],
                    holding["qty"],
                    reason="일일손실한도초과 강제청산"
                )
                time.sleep(0.5)

            log.warning("[매매 정지] 오늘 더 이상 신규 매수하지 않습니다.")

    # ----------------------------------------------------------
    # [7-6] 정기 실행 루틴 (5분마다)
    # ----------------------------------------------------------
    def run_cycle(self):
        """매매 사이클 실행 (5분 간격)"""

        # 매매 정지 상태면 손절 모니터링만 유지
        if not self.is_running:
            log.info("[매매 정지 상태] 신규 매수 없음, 손절 모니터링 유지")
            self.monitor_stop_loss()
            return

        try:
            log.info(f"\n--- 사이클 실행: {datetime.now().strftime('%H:%M:%S')} ---")

            # Step 1: 일일 손실한도 확인 (최우선)
            self.monitor_daily_loss()
            if not self.is_running:
                return

            # Step 2: 손절 체크
            self.monitor_stop_loss()

            # Step 3: 매수 후보 스캔 (장 시작 후 ~ 14:30까지만 신규 매수)
            now = datetime.now().time()
            buy_start = datetime.strptime("09:10", "%H:%M").time()
            buy_end   = datetime.strptime("14:30", "%H:%M").time()

            if buy_start <= now <= buy_end:
                candidates = self.scan_buy_candidates()
                if candidates:
                    self.execute_buy(candidates)
            else:
                log.info("매수 시간 외 (신규 매수 없음)")

        except Exception as e:
            log.error(f"[사이클 오류] {e}", exc_info=True)

    # ----------------------------------------------------------
    # [7-7] 장 마감 정리 (15:30)
    # ----------------------------------------------------------
    def end_of_day(self):
        """장 마감 후 결과 정리"""
        log.info("=" * 60)
        log.info("[장 마감 정리]")

        balance = self.kis.fetch_balance()
        final_total = balance["total_eval"]
        profit_loss = final_total - self.risk.initial_capital
        profit_rate = profit_loss / self.risk.initial_capital

        log.info(f"기준 자본금  : {self.risk.initial_capital:>15,}원")
        log.info(f"최종 평가금액: {final_total:>15,}원")
        log.info(f"당일 손익    : {profit_loss:>+15,}원 ({profit_rate:+.2%})")
        log.info(f"보유 종목 수 : {len(balance['holdings'])}개")
        log.info("=" * 60)


# ============================================================
# [8] 스케줄러 등록 및 메인 실행
# - 시간대별로 함수를 자동 실행
# ============================================================
def main():
    log.info("자동매매 시스템 시작")

    trader = AutoTrader()

    # 토큰 초기 발급
    trader.kis.get_access_token()

    # ─── 스케줄 등록 ───────────────────────────────────────────
    # 08:50 → 장 시작 전 초기화 (자본금 기준 설정)
    schedule.every().day.at("08:50").do(trader.morning_init)

    # 09:05 ~ 15:20 → 5분마다 매매 사이클 실행
    # (schedule 라이브러리는 반복 간격 설정, 시간 범위는 run_cycle 내부에서 필터)
    schedule.every(5).minutes.do(trader.run_cycle)

    # 15:30 → 장 마감 결과 정리
    schedule.every().day.at("15:30").do(trader.end_of_day)

    log.info("스케줄 등록 완료. 루프 시작...")
    log.info("  · 08:50 장 시작 초기화")
    log.info("  · 5분 간격 매매 사이클")
    log.info("  · 15:30 장 마감 정리")

    # ─── 메인 루프 ─────────────────────────────────────────────
    # Ctrl+C로 종료 가능
    try:
        while True:
            schedule.run_pending()   # 예약된 작업 중 실행 시각 된 것 실행
            time.sleep(30)           # 30초 간격으로 스케줄 확인
    except KeyboardInterrupt:
        log.info("사용자 종료 (Ctrl+C)")


# ============================================================
# [9] 진입점
# ============================================================
if __name__ == "__main__":
    main()
