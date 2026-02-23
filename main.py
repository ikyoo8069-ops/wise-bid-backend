# ============================================
# 슬기로운 입찰생활 - Backend v3.5
# + 조달청 가격정보 API 연동
# + 공종별 비율 DB
# + 개략원가 자동 산출
# + N2B 참여 판정
# + 입찰공고 조회/매칭 (NEW!)
# + 회사 프로필 매칭 (NEW!)
# + 낙찰률 조회 API (NEW!)
# ============================================

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import httpx
import xml.etree.ElementTree as ET
import anthropic
import os
import json
import asyncio
import re
from datetime import date, datetime, timedelta

app = FastAPI(title="N2B Backend v3.5", description="wise-bid + 가격정보API + 개략원가산출 + 공고매칭 + 낙찰률")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# API 키
# ============================================
BIZINFO_API_KEY = os.getenv("BIZINFO_API_KEY", "f41G7V")
KSTARTUP_API_KEY = os.getenv("KSTARTUP_API_KEY", "47bd938c975a8989c5561a813fe66fcd68b76bfc4b4d54ca33345923b5b51897")
PUBLIC_DATA_API_KEY = os.getenv("PUBLIC_DATA_API_KEY", "47bd938c975a8989c5561a813fe66fcd68b76bfc4b4d54ca33345923b5b51897")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
PREMIUM_KEY = os.getenv("PREMIUM_KEY", "wise2025")

# ============================================
# 공종별 표준 비율 DB (핵심!)
# ============================================
COST_RATIOS = {
    "도로": {"재료비": 55, "노무비": 25, "경비": 20, "description": "도로포장, 아스팔트"},
    "토목": {"재료비": 40, "노무비": 35, "경비": 25, "description": "토공, 기초"},
    "건축": {"재료비": 50, "노무비": 30, "경비": 20, "description": "건물 신축/개보수"},
    "설비": {"재료비": 60, "노무비": 25, "경비": 15, "description": "기계설비, 배관"},
    "전기": {"재료비": 55, "노무비": 30, "경비": 15, "description": "전기, 통신"},
    "조경": {"재료비": 45, "노무비": 35, "경비": 20, "description": "조경, 식재"},
    "상하수도": {"재료비": 50, "노무비": 30, "경비": 20, "description": "상하수도, 관로"},
    "포장": {"재료비": 55, "노무비": 25, "경비": 20, "description": "포장 전문"},
    "철근콘크리트": {"재료비": 50, "노무비": 35, "경비": 15, "description": "RC 구조물"},
    "철골": {"재료비": 60, "노무비": 25, "경비": 15, "description": "철골 구조물"},
    "기타": {"재료비": 50, "노무비": 30, "경비": 20, "description": "일반 공사"},
    # 용역
    "설계": {"재료비": 10, "노무비": 65, "경비": 25, "description": "설계용역", "category": "용역"},
    "감리": {"재료비": 10, "노무비": 65, "경비": 25, "description": "감리용역", "category": "용역"},
    "엔지니어링": {"재료비": 15, "노무비": 60, "경비": 25, "description": "엔지니어링", "category": "용역"},
    "SW개발": {"재료비": 15, "노무비": 60, "경비": 25, "description": "소프트웨어 개발", "category": "용역"},
    "시설관리": {"재료비": 20, "노무비": 55, "경비": 25, "description": "시설관리/청소", "category": "용역"},
    "일반용역": {"재료비": 15, "노무비": 60, "경비": 25, "description": "일반 용역", "category": "용역"},
    # 물품
    "전산장비": {"재료비": 75, "노무비": 10, "경비": 15, "description": "전산장비/IT", "category": "물품"},
    "사무용품": {"재료비": 70, "노무비": 10, "경비": 20, "description": "사무용품/소모품", "category": "물품"},
    "자재": {"재료비": 75, "노무비": 10, "경비": 15, "description": "건축/전기 자재", "category": "물품"},
    "일반물품": {"재료비": 70, "노무비": 10, "경비": 20, "description": "일반 물품", "category": "물품"}
}

# 간접비 비율 (직접공사비 대비)
INDIRECT_RATIOS = {
    "간접노무비": 12.0,
    "산재보험료": 3.7,
    "고용보험료": 1.05,
    "건강보험료": 3.545,
    "연금보험료": 4.5,
    "퇴직공제부금": 2.3,
    "안전관리비": 1.97,
    "환경보전비": 0.5,
    "일반관리비": 6.0,
    "이윤": 15.0
}

# ============================================
# 일일 사용 제한
# ============================================
LIMITS = {
    "biz": {"normal": 10, "premium": 200},
    "proposal": {"normal": 10, "premium": 200},
    "agency": {"normal": 100},
    "bid": {"normal": 10, "premium": 200},
    "cost": {"normal": 20, "premium": 200}
}

daily_usage: dict = {}

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def check_rate_limit(ip: str, app_type: str, is_premium: bool = False) -> dict:
    today = str(date.today())
    if today not in daily_usage:
        daily_usage.clear()
        daily_usage[today] = {}
    if ip not in daily_usage[today]:
        daily_usage[today][ip] = {"biz": 0, "proposal": 0, "agency": 0, "bid": 0, "cost": 0}
    
    usage = daily_usage[today][ip]
    current = usage.get(app_type, 0)
    
    if app_type in LIMITS:
        limit = LIMITS[app_type].get("premium" if is_premium else "normal", 10)
    else:
        limit = 10
    
    remaining = limit - current
    if remaining <= 0:
        raise HTTPException(status_code=429, detail=f"일일 사용 한도({limit}회) 초과")
    
    usage[app_type] = current + 1
    return {"used": current + 1, "limit": limit, "remaining": remaining - 1}

# ============================================
# 요청 모델
# ============================================
class CostEstimateRequest(BaseModel):
    base_price: int
    work_type: str = "기타"
    material_discount: float = 0
    labor_discount: float = 0
    equipment_discount: float = 0
    
class N2BDecisionRequest(BaseModel):
    base_price: int
    estimated_cost: int
    work_type: str = "기타"
    min_profit_rate: float = 10
    company_strength: List[str] = []
    company_weakness: List[str] = []

class PriceSearchRequest(BaseModel):
    keyword: str
    category: str = "all"

# ============================================
# 조달청 가격정보 API
# ============================================
async def fetch_material_prices(keyword: str, category: str = "토목") -> list:
    base_url = "https://apis.data.go.kr/1230000/ao/PriceInfoService"
    
    endpoints = {
        "토목": "getPriceInfoListFcltyCmmnMtrilEngrk",
        "건축": "getPriceInfoListFcltyCmmnMtrilBildng", 
        "기계": "getPriceInfoListFcltyCmmnMtrilMchnEqp",
        "전기": "getPriceInfoListFcltyCmmnMtrilElctyIrmc"
    }
    
    endpoint = endpoints.get(category, "getPriceInfoListFcltyCmmnMtrilEngrk")
    
    params = {
        "serviceKey": PUBLIC_DATA_API_KEY,
        "numOfRows": "20",
        "pageNo": "1",
        "type": "json",
        "prdctClsfcNoNm": keyword
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{base_url}/{endpoint}", params=params)
            
            if response.status_code == 200:
                data = response.json()
                items = data.get("response", {}).get("body", {}).get("items", [])
                if isinstance(items, dict):
                    items = items.get("item", [])
                if isinstance(items, dict):
                    items = [items]
                
                results = []
                for item in items:
                    results.append({
                        "name": item.get("prdctClsfcNoNm", ""),
                        "spec": item.get("krnPrdctNm", ""),
                        "unit": item.get("unit", ""),
                        "price": int(item.get("prce", 0) or 0),
                        "date": item.get("nticeDt", ""),
                        "region": item.get("splyJrsdctRgnNm", "전국")
                    })
                return results
    except Exception as e:
        print(f"가격정보 API 오류: {e}")
    
    return []

async def fetch_market_prices(keyword: str, category: str = "토목") -> list:
    base_url = "https://apis.data.go.kr/1230000/ao/PriceInfoService"
    
    endpoints = {
        "토목": "getPriceInfoListMrktCnstrctPcEngrk",
        "건축": "getPriceInfoListMrktCnstrctPcBildng",
        "기계": "getPriceInfoListMrktCnstrctPcMchnEqp"
    }
    
    endpoint = endpoints.get(category, "getPriceInfoListMrktCnstrctPcEngrk")
    
    params = {
        "serviceKey": PUBLIC_DATA_API_KEY,
        "numOfRows": "20",
        "pageNo": "1",
        "type": "json",
        "prdctClsfcNoNm": keyword
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{base_url}/{endpoint}", params=params)
            if response.status_code == 200:
                data = response.json()
                items = data.get("response", {}).get("body", {}).get("items", [])
                if isinstance(items, dict):
                    items = items.get("item", [])
                if isinstance(items, dict):
                    items = [items]
                
                results = []
                for item in items:
                    results.append({
                        "name": item.get("wrkDivNm", ""),
                        "spec": item.get("wrkDtlDivNm", ""),
                        "unit": item.get("unt", ""),
                        "price": int(item.get("mrktPrc", 0)),
                        "date": item.get("applyDt", ""),
                        "type": "시공가격"
                    })
                return results
    except Exception as e:
        print(f"시장시공가격 API 오류: {e}")
    
    return []

# ============================================
# 개략원가 산출 로직
# ============================================
def calculate_rough_cost(
    base_price: int,
    work_type: str,
    material_discount: float = 0,
    labor_discount: float = 0,
    equipment_discount: float = 0
) -> dict:
    ratios = COST_RATIOS.get(work_type, COST_RATIOS["기타"])
    
    direct_cost_ratio = 0.74
    estimated_direct_cost = int(base_price * direct_cost_ratio)
    
    material_cost = int(estimated_direct_cost * ratios["재료비"] / 100)
    labor_cost = int(estimated_direct_cost * ratios["노무비"] / 100)
    equipment_cost = int(estimated_direct_cost * ratios["경비"] / 100)
    
    standard_cost = {
        "재료비": material_cost,
        "노무비": labor_cost,
        "경비": equipment_cost,
        "직접공사비": estimated_direct_cost,
        "간접비": int(base_price * 0.26),
        "합계": base_price
    }
    
    actual_material = int(material_cost * (1 - material_discount / 100))
    actual_labor = int(labor_cost * (1 - labor_discount / 100))
    actual_equipment = int(equipment_cost * (1 - equipment_discount / 100))
    actual_direct = actual_material + actual_labor + actual_equipment
    
    direct_reduction_rate = actual_direct / estimated_direct_cost if estimated_direct_cost > 0 else 1
    actual_indirect = int(base_price * 0.26 * direct_reduction_rate)
    
    actual_total = actual_direct + actual_indirect
    
    actual_cost = {
        "재료비": actual_material,
        "노무비": actual_labor,
        "경비": actual_equipment,
        "직접공사비": actual_direct,
        "간접비": actual_indirect,
        "합계": actual_total
    }
    
    bubble_rate = ((base_price - actual_total) / base_price * 100) if base_price > 0 else 0
    
    min_expected_price = int(base_price * 0.97)
    max_expected_price = int(base_price * 1.03)
    min_bid_price = int(actual_total * 1.05)
    
    recommended_rate = (actual_total / base_price * 100) + 10 if base_price > 0 else 88
    recommended_rate = min(recommended_rate, 95)
    recommended_rate = max(recommended_rate, 75)
    
    return {
        "기초금액": base_price,
        "공종": work_type,
        "공종설명": ratios["description"],
        "비율": {
            "재료비": ratios["재료비"],
            "노무비": ratios["노무비"],
            "경비": ratios["경비"]
        },
        "절감률": {
            "재료비": material_discount,
            "노무비": labor_discount,
            "경비": equipment_discount
        },
        "표준원가": standard_cost,
        "실제원가": actual_cost,
        "절감금액": standard_cost["합계"] - actual_cost["합계"],
        "거품률": round(bubble_rate, 1),
        "투찰분석": {
            "예정가격범위": {"최저": min_expected_price, "최고": max_expected_price},
            "최저투찰가": min_bid_price,
            "권장투찰률": round(recommended_rate, 1),
            "권장투찰가": int(base_price * recommended_rate / 100)
        }
    }

# ============================================
# N2B 참여 판정 로직
# ============================================
def analyze_n2b_decision(
    base_price: int,
    estimated_cost: int,
    work_type: str,
    min_profit_rate: float = 10,
    company_strength: List[str] = [],
    company_weakness: List[str] = []
) -> dict:
    bubble_rate = ((base_price - estimated_cost) / base_price * 100) if base_price > 0 else 0
    potential_profit = base_price - estimated_cost
    profit_rate = (potential_profit / estimated_cost * 100) if estimated_cost > 0 else 0
    
    score = 50
    
    if bubble_rate >= 25:
        score += 30
    elif bubble_rate >= 20:
        score += 25
    elif bubble_rate >= 15:
        score += 20
    elif bubble_rate >= 10:
        score += 15
    elif bubble_rate >= 5:
        score += 10
    
    if profit_rate >= min_profit_rate + 10:
        score += 20
    elif profit_rate >= min_profit_rate + 5:
        score += 15
    elif profit_rate >= min_profit_rate:
        score += 10
    elif profit_rate >= min_profit_rate - 5:
        score += 5
    else:
        score -= 10
    
    strength_keywords = {
        "재료": ["거래처", "직거래", "자재", "재료"],
        "인력": ["직영", "숙련", "인력", "노무"],
        "장비": ["자가", "장비", "보유"]
    }
    
    for strength in company_strength:
        for category, keywords in strength_keywords.items():
            if any(kw in strength for kw in keywords):
                score += 5
    
    for weakness in company_weakness:
        if any(kw in weakness for kw in ["미경험", "부족", "없음", "처음"]):
            score -= 5
    
    score = max(0, min(100, score))
    
    if score >= 75:
        decision = "적극 참여"
        recommendation = "수익성 높음, 적극 참여 권장"
    elif score >= 60:
        decision = "참여 권장"
        recommendation = "적정 수익 예상, 참여 고려"
    elif score >= 45:
        decision = "조건부 참여"
        recommendation = "원가 재검토 후 참여 결정"
    elif score >= 30:
        decision = "신중 검토"
        recommendation = "리스크 높음, 신중한 검토 필요"
    else:
        decision = "불참 권장"
        recommendation = "수익성 낮음, 불참 권장"
    
    n2b = {
        "not": f"단순히 기초금액 {base_price:,}원이 커서 참여하는 것이 아니다",
        "but": f"실제원가 {estimated_cost:,}원 대비 거품률 {bubble_rate:.1f}%가 판단 기준이다",
        "because": f"거품률이 {'충분하여' if bubble_rate >= 15 else '부족하여'} 예상수익률 {profit_rate:.1f}%{'로 참여 가치가 있다' if profit_rate >= min_profit_rate else '로 리스크가 있다'}"
    }
    
    risks = []
    if bubble_rate < 10:
        risks.append("거품률 낮음 - 경쟁 심화 시 손실 가능")
    if profit_rate < 5:
        risks.append("수익률 낮음 - 원가 상승 시 손실 가능")
    if not company_strength:
        risks.append("회사 강점 미확인 - 경쟁력 검토 필요")
    
    opportunities = []
    if bubble_rate >= 20:
        opportunities.append("높은 거품률 - 가격 경쟁력 확보 가능")
    if company_strength:
        opportunities.append(f"회사 강점 활용 - {', '.join(company_strength[:2])}")
    
    return {
        "decision": decision,
        "score": score,
        "recommendation": recommendation,
        "n2b": n2b,
        "분석": {
            "기초금액": base_price,
            "예상원가": estimated_cost,
            "거품률": round(bubble_rate, 1),
            "예상수익": potential_profit,
            "수익률": round(profit_rate, 1),
            "요구수익률": min_profit_rate
        },
        "risks": risks,
        "opportunities": opportunities
    }

# ============================================
# API 엔드포인트
# ============================================
@app.get("/")
async def root():
    return {
        "service": "wise-bid API v4.0",
        "features": [
            "가격정보 API 연동",
            "공종별 비율 DB",
            "개략원가 자동 산출",
            "N2B 참여 판정",
            "입찰공고 조회/매칭",
            "회사 프로필 매칭",
            "낙찰률 조회 API - 공사/용역/물품 (NEW!)"
        ],
        "endpoints": {
            "/api/cost-ratios": "공종별 비율 조회",
            "/api/price-search": "자재/시공 단가 검색",
            "/api/cost-estimate": "개략원가 산출",
            "/api/n2b-decision": "N2B 참여 판정",
            "/api/quick-match/{profile}": "샘플 프로필 매칭",
            "/api/custom-match": "커스텀 조건 매칭",
            "/api/bid-rate": "낙찰률 조회 (NEW!)",
            "/api/bid-rate/summary": "전체 공종별 낙찰률 요약 (NEW!)",
            "/api/debug/bid-api": "입찰공고 API 테스트",
            "/api/debug/price-api": "가격정보 API 테스트",
            "/api/debug/bid-result-api": "낙찰정보 API 테스트 (NEW!)"
        }
    }

@app.get("/api/cost-ratios")
async def get_cost_ratios():
    return {
        "공종별비율": COST_RATIOS,
        "간접비비율": INDIRECT_RATIOS
    }

@app.get("/api/cost-ratio/{work_type}")
async def get_cost_ratio(work_type: str):
    if work_type in COST_RATIOS:
        return COST_RATIOS[work_type]
    return {"error": f"공종 '{work_type}' 없음", "available": list(COST_RATIOS.keys())}

@app.post("/api/price-search")
async def search_prices(req: PriceSearchRequest, request: Request):
    ip = get_client_ip(request)
    is_premium = request.headers.get("x-premium-key") == PREMIUM_KEY
    check_rate_limit(ip, "cost", is_premium)
    
    material_prices = await fetch_material_prices(req.keyword, "토목")
    market_prices = await fetch_market_prices(req.keyword, "토목")
    
    return {
        "keyword": req.keyword,
        "자재단가": material_prices,
        "시공단가": market_prices,
        "total": len(material_prices) + len(market_prices)
    }

@app.post("/api/cost-estimate")
async def estimate_cost(req: CostEstimateRequest, request: Request):
    ip = get_client_ip(request)
    is_premium = request.headers.get("x-premium-key") == PREMIUM_KEY
    check_rate_limit(ip, "cost", is_premium)
    
    result = calculate_rough_cost(
        base_price=req.base_price,
        work_type=req.work_type,
        material_discount=req.material_discount,
        labor_discount=req.labor_discount,
        equipment_discount=req.equipment_discount
    )
    
    return result

@app.post("/api/n2b-decision")
async def n2b_decision(req: N2BDecisionRequest, request: Request):
    ip = get_client_ip(request)
    is_premium = request.headers.get("x-premium-key") == PREMIUM_KEY
    check_rate_limit(ip, "cost", is_premium)
    
    result = analyze_n2b_decision(
        base_price=req.base_price,
        estimated_cost=req.estimated_cost,
        work_type=req.work_type,
        min_profit_rate=req.min_profit_rate,
        company_strength=req.company_strength,
        company_weakness=req.company_weakness
    )
    
    return result

@app.get("/api/quick-estimate")
async def quick_estimate(
    base_price: int,
    work_type: str = "기타",
    material_discount: float = 0,
    labor_discount: float = 0,
    equipment_discount: float = 0
):
    result = calculate_rough_cost(
        base_price=base_price,
        work_type=work_type,
        material_discount=material_discount,
        labor_discount=labor_discount,
        equipment_discount=equipment_discount
    )
    return result

@app.get("/api/quick-decision")
async def quick_decision(
    base_price: int,
    estimated_cost: int,
    work_type: str = "기타",
    min_profit_rate: float = 10
):
    result = analyze_n2b_decision(
        base_price=base_price,
        estimated_cost=estimated_cost,
        work_type=work_type,
        min_profit_rate=min_profit_rate
    )
    return result

# ============================================
# 통합 분석 엔드포인트 (GET)
# ============================================
@app.get("/api/full-analysis")
async def full_analysis(
    base_price: int,
    work_type: str = "기타",
    material_discount: float = 10,
    labor_discount: float = 15,
    equipment_discount: float = 10,
    min_profit_rate: float = 10,
    request: Request = None
):
    cost_result = calculate_rough_cost(
        base_price=base_price,
        work_type=work_type,
        material_discount=material_discount,
        labor_discount=labor_discount,
        equipment_discount=equipment_discount
    )
    
    estimated_cost = cost_result["실제원가"]["합계"]
    
    decision_result = analyze_n2b_decision(
        base_price=base_price,
        estimated_cost=estimated_cost,
        work_type=work_type,
        min_profit_rate=min_profit_rate
    )
    
    bubble_rate = cost_result["거품률"]
    
    if bubble_rate >= 25:
        strategy = "공격적 투찰: 낙찰률 85-88% 권장"
    elif bubble_rate >= 20:
        strategy = "적정 투찰: 낙찰률 87-90% 권장"
    elif bubble_rate >= 15:
        strategy = "보수적 투찰: 낙찰률 89-92% 권장"
    else:
        strategy = "신중 투찰: 원가 재검토 필요"
    
    return {
        "summary": {
            "기초금액": f"{base_price:,}원",
            "예상원가": f"{estimated_cost:,}원",
            "거품률": f"{bubble_rate}%",
            "판정": decision_result["decision"],
            "점수": f"{decision_result['score']}점",
            "전략": strategy
        },
        "원가분석": cost_result,
        "참여판정": decision_result,
        "n2b": decision_result["n2b"]
    }

@app.get("/api/usage")
async def get_usage(request: Request):
    ip = get_client_ip(request)
    today = str(date.today())
    is_premium = request.headers.get("x-premium-key") == PREMIUM_KEY
    usage = daily_usage.get(today, {}).get(ip, {"cost": 0, "bid": 0})
    
    cost_limit = LIMITS["cost"]["premium"] if is_premium else LIMITS["cost"]["normal"]
    
    return {
        "date": today,
        "cost": {
            "used": usage.get("cost", 0),
            "limit": cost_limit,
            "remaining": cost_limit - usage.get("cost", 0),
            "tier": "premium" if is_premium else "normal"
        }
    }

# ============================================
# 입찰공고 조회 (나라장터 API)
# ============================================
async def fetch_bid_announcements(keyword: str, bid_type: str = "공사", count: int = 20) -> list:
    type_endpoints = {
        "물품": "getBidPblancListInfoThng",
        "공사": "getBidPblancListInfoCnstwk", 
        "용역": "getBidPblancListInfoServc",
        "외자": "getBidPblancListInfoFrgcpt"
    }
    
    endpoint = type_endpoints.get(bid_type, "getBidPblancListInfoCnstwk")
    url = f"https://apis.data.go.kr/1230000/ad/BidPublicInfoService/{endpoint}"
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    
    params = {
        "ServiceKey": PUBLIC_DATA_API_KEY,
        "pageNo": 1,
        "numOfRows": count,
        "type": "json",
        "inqryDiv": "1",
        "inqryBgnDt": start_date.strftime("%Y%m%d") + "0000",
        "inqryEndDt": end_date.strftime("%Y%m%d") + "2359"
    }
    
    if keyword and keyword.strip():
        params["bidNm"] = keyword

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            
            response.raise_for_status()
            data = response.json()
            
            response_data = data.get("response", {})
            body = response_data.get("body", {})
            items = body.get("items", [])
            
            if isinstance(items, dict):
                items = items.get("item", [])
            if not isinstance(items, list):
                items = [items] if items else []
            
            if not items:
                return []
            
            bids = []
            for item in items:
                bid = {
                    "bid_no": item.get("bidNtceNo", ""),
                    "bid_name": item.get("bidNtceNm", ""),
                    "agency": item.get("ntceInsttNm", ""),
                    "demand_agency": item.get("dminsttNm", ""),
                    "estimated_price": item.get("presmptPrce", 0),
                    "base_price": item.get("asignBdgtAmt", 0),
                    "bid_method": item.get("bidMethdNm", ""),
                    "contract_method": item.get("cntrctCnclsMthdNm", ""),
                    "deadline": item.get("bidClseDt", ""),
                    "open_date": item.get("opengDt", ""),
                    "region": item.get("ntceInsttOfclAddr", ""),
                    "url": item.get("bidNtceDtlUrl", ""),
                    "bid_type": bid_type,
                    "main_cnstty": item.get("mainCnsttyNm", ""),
                    "cnstty_list": item.get("cnsttyAccotShreRateList", "")
                }
                bids.append(bid)
            
            return bids
    except Exception as e:
        print(f"[조달청 입찰공고 오류] {e}")
        import traceback
        traceback.print_exc()
        return []

# ============================================
# 낙찰정보 조회
# ============================================
async def fetch_winning_bids(keyword: str, bid_type: str = "공사", count: int = 20) -> list:
    type_endpoints = {
        "물품": "getOpengResultListInfoThngPPSSrch",
        "공사": "getOpengResultListInfoCnstwkPPSSrch",
        "용역": "getOpengResultListInfoServcPPSSrch",
        "외자": "getOpengResultListInfoFrgcptPPSSrch"
    }
    
    endpoint = type_endpoints.get(bid_type, "getOpengResultListInfoCnstwkPPSSrch")
    url = f"https://apis.data.go.kr/1230000/ScsbidInfoService/{endpoint}"
    
    params = {
        "ServiceKey": PUBLIC_DATA_API_KEY,
        "pageNo": 1,
        "numOfRows": count,
        "type": "json",
        "bidNm": keyword,
        "inqryDiv": "1"
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            items = data.get("response", {}).get("body", {}).get("items", [])
            if not items:
                return []
            # items가 dict일 수 있음
            if isinstance(items, dict):
                items = items.get("item", [])
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                items = []
            
            results = []
            for item in items:
                estimated = float(item.get("presmptPrce", 0) or 0)
                winning = float(item.get("sucsfbidAmt", 0) or 0)
                rate = (winning / estimated * 100) if estimated > 0 else 0
                result = {
                    "bid_no": item.get("bidNtceNo", ""),
                    "bid_name": item.get("bidNtceNm", ""),
                    "agency": item.get("ntceInsttNm", ""),
                    "estimated_price": estimated,
                    "winning_price": winning,
                    "winning_rate": round(rate, 2),
                    "winner": item.get("sucsfbidCorpNm", ""),
                    "open_date": item.get("opengDt", ""),
                    "participant_count": item.get("prtcptCnum", 0)
                }
                results.append(result)
            return results
    except Exception as e:
        print(f"[조달청 낙찰정보 오류] {e}")
        return []

# ============================================
# 회사 프로필 모델
# ============================================
class CompanyProfile(BaseModel):
    company_name: str = ""
    business_type: str = "전문건설"
    work_types: List[str] = []
    regions: List[str] = []
    min_price: int = 0
    max_price: int = 10000000000
    licenses: List[str] = []
    experiences: List[str] = []

# ============================================
# 샘플 회사 프로필
# ============================================
SAMPLE_PROFILES = {
    "road": CompanyProfile(
        company_name="(주)한길도로",
        business_type="전문건설",
        work_types=["도로", "포장", "아스팔트", "아스콘"],
        regions=["서울", "경기", "인천"],
        min_price=50000000,
        max_price=2000000000,
        licenses=["도로포장공사업", "비계구조물해체공사업"],
        experiences=["도로포장 50건", "아스팔트포장 30건", "보도블럭 20건"]
    ),
    "general": CompanyProfile(
        company_name="대한종합건설(주)",
        business_type="종합건설",
        work_types=["건축", "토목", "도로", "상하수도"],
        regions=["서울", "경기", "인천", "충남", "충북"],
        min_price=500000000,
        max_price=50000000000,
        licenses=["토목건축공사업", "토목공사업", "건축공사업"],
        experiences=["공공건축 30건", "도로공사 25건", "하수관로 15건"]
    ),
    "electric": CompanyProfile(
        company_name="(주)밝은전기",
        business_type="전문건설",
        work_types=["전기", "통신", "소방", "설비"],
        regions=["서울", "경기"],
        min_price=30000000,
        max_price=1000000000,
        licenses=["전기공사업", "정보통신공사업", "소방시설공사업"],
        experiences=["전기설비 100건", "통신공사 50건", "소방설비 30건"]
    )
}

@app.get("/api/sample-profiles")
async def get_sample_profiles():
    return {
        "success": True,
        "profiles": {
            name: {
                "company_name": profile.company_name,
                "business_type": profile.business_type,
                "work_types": profile.work_types,
                "regions": profile.regions,
                "min_price": profile.min_price,
                "max_price": profile.max_price,
                "min_price_formatted": f"{profile.min_price:,}원",
                "max_price_formatted": f"{profile.max_price:,}원",
                "licenses": profile.licenses,
                "experiences": profile.experiences
            }
            for name, profile in SAMPLE_PROFILES.items()
        }
    }

@app.get("/api/sample-profiles/{profile_name}")
async def get_sample_profile(profile_name: str):
    if profile_name not in SAMPLE_PROFILES:
        raise HTTPException(status_code=404, detail=f"프로필 '{profile_name}' 없음")
    
    profile = SAMPLE_PROFILES[profile_name]
    return {
        "success": True,
        "name": profile_name,
        "profile": {
            "company_name": profile.company_name,
            "business_type": profile.business_type,
            "work_types": profile.work_types,
            "regions": profile.regions,
            "min_price": profile.min_price,
            "max_price": profile.max_price,
            "licenses": profile.licenses,
            "experiences": profile.experiences
        }
    }

# ============================================
# 커스텀 프로필로 공고 매칭
# ============================================
class CustomMatchRequest(BaseModel):
    work_types: list = ["도로포장"]
    min_price: int = 50000000
    max_price: int = 2000000000
    regions: list = ["서울", "경기"]
    keyword: str = ""
    bid_type: str = "공사"

@app.post("/api/custom-match")
async def custom_match(req: CustomMatchRequest):
    REGION_MAP = {
        "전국": ["전국"],
        "수도권": ["서울", "경기", "인천"],
        "충청권": ["대전", "세종", "충북", "충남"],
        "영남권": ["부산", "대구", "울산", "경북", "경남"],
        "호남권": ["광주", "전북", "전남"],
        "강원제주": ["강원", "제주"]
    }
    
    expanded_regions = []
    for r in req.regions:
        if r in REGION_MAP:
            expanded_regions.extend(REGION_MAP[r])
        else:
            expanded_regions.append(r)
    
    search_keyword = req.keyword if req.keyword else req.work_types[0] if req.work_types else ""
    
    bids = await fetch_bid_announcements(search_keyword, req.bid_type, 100)
    
    today = datetime.now()
    
    matched = []
    for bid in bids:
        score = 0
        reasons = []
        
        deadline = bid.get("deadline", "")
        if deadline:
            try:
                deadline_clean = deadline.replace("-", "").replace(" ", "").replace(":", "")
                if len(deadline_clean) >= 8:
                    deadline_date = datetime.strptime(deadline_clean[:8], "%Y%m%d")
                    yesterday = today - timedelta(days=1)
                    if deadline_date < yesterday:
                        continue
            except:
                pass
        
        price = bid.get("base_price", 0) or bid.get("estimated_price", 0)
        if price:
            price = int(price)
            if req.min_price <= price <= req.max_price:
                score += 30
                reasons.append(f"금액 적합 ({price:,}원)")
            else:
                continue
        
        bid_name = bid.get("bid_name", "")
        main_cnstty = bid.get("main_cnstty", "")
        cnstty_list = bid.get("cnstty_list", "")
        search_text = f"{bid_name} {main_cnstty} {cnstty_list}"
        
        work_type_matched = False
        matched_work_type = ""
        for work_type in req.work_types:
            if work_type in search_text:
                score += 25
                matched_work_type = work_type
                work_type_matched = True
                break
        
        if work_type_matched:
            reasons.append(f"공종: {matched_work_type}" + (f" ({main_cnstty})" if main_cnstty else ""))
        
        if not work_type_matched:
            continue
        
        region = bid.get("region", "") or bid.get("agency", "")
        region_matched = False
        if "전국" in expanded_regions:
            region_matched = True
            score += 20
            reasons.append("지역: 전국")
        else:
            for r in expanded_regions:
                if r in region:
                    score += 20
                    reasons.append(f"지역: {r}")
                    region_matched = True
                    break
        
        bid["match_score"] = score
        bid["match_reasons"] = reasons
        
        if score >= 25:
            matched.append(bid)
    
    matched.sort(key=lambda x: (x.get("deadline", "9999"), -x.get("match_score", 0)))
    
    return {
        "success": True,
        "work_types": req.work_types,
        "price_range": f"{req.min_price:,} ~ {req.max_price:,}",
        "regions": req.regions,
        "search_keyword": search_keyword,
        "total_found": len(bids),
        "matched_count": len(matched),
        "match_rate": round(len(matched) / len(bids) * 100, 1) if bids else 0,
        "matched": matched[:20],
        "n2b": {
            "not": f"모든 공고가 조건에 맞는 게 아닙니다",
            "but": f"{len(matched)}건이 매칭되었습니다",
            "because": f"금액({req.min_price:,}~{req.max_price:,}원), 공종({', '.join(req.work_types[:2])}), 지역({', '.join(req.regions[:3])}) 조건 충족"
        }
    }

# ============================================
# 샘플 프로필로 공고 매칭 (원클릭)
# ============================================
@app.get("/api/quick-match/{profile_name}")
async def quick_match(profile_name: str, keyword: str = "", bid_type: str = "공사", request: Request = None):
    if profile_name not in SAMPLE_PROFILES:
        raise HTTPException(status_code=404, detail=f"프로필 '{profile_name}' 없음")
    
    profile = SAMPLE_PROFILES[profile_name]
    search_keyword = keyword if keyword else profile.work_types[0] if profile.work_types else ""
    
    bids = await fetch_bid_announcements(search_keyword, bid_type, 100)
    
    today = datetime.now()
    
    matched = []
    for bid in bids:
        score = 0
        reasons = []
        
        deadline = bid.get("deadline", "")
        if deadline:
            try:
                deadline_clean = deadline.replace("-", "").replace(" ", "").replace(":", "")
                if len(deadline_clean) >= 8:
                    deadline_date = datetime.strptime(deadline_clean[:8], "%Y%m%d")
                    yesterday = today - timedelta(days=1)
                    if deadline_date < yesterday:
                        continue
            except:
                pass
        
        price = bid.get("base_price", 0) or bid.get("estimated_price", 0)
        if price:
            price = int(price)
            if profile.min_price <= price <= profile.max_price:
                score += 30
                reasons.append(f"금액 적합 ({price:,}원)")
            else:
                continue
        
        bid_name = bid.get("bid_name", "")
        main_cnstty = bid.get("main_cnstty", "")
        cnstty_list = bid.get("cnstty_list", "")
        search_text = f"{bid_name} {main_cnstty} {cnstty_list}"
        
        work_type_matched = False
        matched_work_type = ""
        for work_type in profile.work_types:
            if work_type in search_text:
                score += 25
                matched_work_type = work_type
                work_type_matched = True
                break
        
        if work_type_matched:
            reasons.append(f"공종: {matched_work_type} ({main_cnstty})" if main_cnstty else f"공종: {matched_work_type}")
        
        if not work_type_matched:
            continue
        
        region = bid.get("region", "") or bid.get("agency", "")
        for r in profile.regions:
            if r in region:
                score += 20
                reasons.append(f"지역: {r}")
                break
        
        bid["match_score"] = score
        bid["match_reasons"] = reasons
        
        if score >= 25:
            matched.append(bid)
    
    matched.sort(key=lambda x: (x.get("deadline", "9999"), -x.get("match_score", 0)))
    
    return {
        "success": True,
        "profile_name": profile_name,
        "company": profile.company_name,
        "search_keyword": search_keyword,
        "total_found": len(bids),
        "matched_count": len(matched),
        "match_rate": round(len(matched) / len(bids) * 100, 1) if bids else 0,
        "matched": matched[:20],
        "debug": {
            "work_types": profile.work_types,
            "price_range": f"{profile.min_price:,} ~ {profile.max_price:,}",
            "sample_bids": [
                {
                    "name": b.get("bid_name", "")[:40],
                    "main_cnstty": b.get("main_cnstty", "없음"),
                    "price": b.get("base_price", 0) or b.get("estimated_price", 0),
                    "deadline": b.get("deadline", "없음")
                } for b in bids[:5]
            ]
        },
        "n2b": {
            "not": f"모든 공고가 {profile.company_name}에 적합한 게 아닙니다",
            "but": f"{len(matched)}건이 매칭되었습니다",
            "because": f"금액({profile.min_price:,}~{profile.max_price:,}원), 공종({', '.join(profile.work_types[:2])}), 지역({', '.join(profile.regions[:2])}) 조건 충족"
        }
    }

class BidSearchRequest(BaseModel):
    keyword: str = ""
    bid_type: str = "공사"
    count: int = 20
    profile: Optional[CompanyProfile] = None

@app.post("/api/bid-search")
async def search_bids(req: BidSearchRequest, request: Request):
    ip = get_client_ip(request)
    is_premium = request.headers.get("x-premium-key") == PREMIUM_KEY
    check_rate_limit(ip, "bid", is_premium)
    
    bids = await fetch_bid_announcements(req.keyword, req.bid_type, req.count)
    
    return {
        "success": True,
        "count": len(bids),
        "keyword": req.keyword,
        "bid_type": req.bid_type,
        "bids": bids
    }

@app.get("/api/bid-search")
async def search_bids_get(
    keyword: str = "",
    bid_type: str = "공사",
    count: int = 20,
    request: Request = None
):
    bids = await fetch_bid_announcements(keyword, bid_type, count)
    
    return {
        "success": True,
        "count": len(bids),
        "keyword": keyword,
        "bid_type": bid_type,
        "bids": bids
    }

@app.post("/api/bid-match")
async def match_bids(req: BidSearchRequest, request: Request):
    ip = get_client_ip(request)
    is_premium = request.headers.get("x-premium-key") == PREMIUM_KEY
    check_rate_limit(ip, "bid", is_premium)
    
    bids = await fetch_bid_announcements(req.keyword, req.bid_type, req.count)
    
    if not req.profile:
        return {
            "success": True,
            "matched": bids,
            "total": len(bids),
            "filtered": 0,
            "message": "프로필 없음 - 전체 공고 반환"
        }
    
    today = datetime.now()
    
    matched = []
    for bid in bids:
        score = 0
        reasons = []
        
        deadline = bid.get("deadline", "")
        if deadline:
            try:
                deadline_clean = deadline.replace("-", "").replace(" ", "").replace(":", "")
                if len(deadline_clean) >= 8:
                    deadline_date = datetime.strptime(deadline_clean[:8], "%Y%m%d")
                    yesterday = today - timedelta(days=1)
                    if deadline_date < yesterday:
                        continue
            except:
                pass
        
        price = bid.get("base_price", 0) or bid.get("estimated_price", 0)
        if price:
            price = int(price)
            if req.profile.min_price <= price <= req.profile.max_price:
                score += 30
                reasons.append("금액 적합")
            else:
                continue
        
        bid_name = bid.get("bid_name", "")
        main_cnstty = bid.get("main_cnstty", "")
        cnstty_list = bid.get("cnstty_list", "")
        search_text = f"{bid_name} {main_cnstty} {cnstty_list}"
        
        work_type_matched = False
        matched_work_type = ""
        for work_type in req.profile.work_types:
            if work_type in search_text:
                score += 25
                matched_work_type = work_type
                work_type_matched = True
                break
        
        if work_type_matched:
            reasons.append(f"공종: {matched_work_type} ({main_cnstty})" if main_cnstty else f"공종: {matched_work_type}")
        
        if not work_type_matched:
            continue
        
        region = bid.get("region", "") or bid.get("agency", "")
        for r in req.profile.regions:
            if r in region:
                score += 20
                reasons.append(f"지역 매칭: {r}")
                break
        
        bid["match_score"] = score
        bid["match_reasons"] = reasons
        
        if score >= 25:
            matched.append(bid)
    
    matched.sort(key=lambda x: (x.get("deadline", "9999"), -x.get("match_score", 0)))
    
    return {
        "success": True,
        "matched": matched,
        "total": len(bids),
        "filtered": len(matched),
        "profile": {
            "company": req.profile.company_name,
            "work_types": req.profile.work_types,
            "regions": req.profile.regions,
            "price_range": f"{req.profile.min_price:,} ~ {req.profile.max_price:,}"
        }
    }

# ============================================
# 낙찰가율 분석 API
# ============================================
@app.get("/api/winning-rate")
async def get_winning_rate(
    keyword: str = "",
    bid_type: str = "공사",
    count: int = 30,
    request: Request = None
):
    results = await fetch_winning_bids(keyword, bid_type, count)
    
    if not results:
        return {
            "success": False,
            "message": "낙찰 데이터 없음",
            "avg_rate": 87.5,
            "data": []
        }
    
    rates = [r["winning_rate"] for r in results if r["winning_rate"] > 0]
    avg_rate = sum(rates) / len(rates) if rates else 87.5
    
    return {
        "success": True,
        "keyword": keyword,
        "count": len(results),
        "avg_rate": round(avg_rate, 2),
        "min_rate": round(min(rates), 2) if rates else 0,
        "max_rate": round(max(rates), 2) if rates else 0,
        "recommendation": f"투찰가율 {avg_rate - 0.5:.1f}% ~ {avg_rate + 0.5:.1f}% 권장",
        "data": results[:10]
    }

# ============================================
# 통합 분석 API (POST - 공고 + 원가 + N2B)
# ============================================
@app.post("/api/full-analysis")
async def full_analysis_post(
    bid_no: str,
    base_price: int,
    work_type: str = "기타",
    material_discount: float = 0,
    labor_discount: float = 0,
    equipment_discount: float = 0,
    min_profit_rate: float = 5.0,
    request: Request = None
):
    ratios = COST_RATIOS.get(work_type, COST_RATIOS["기타"])
    
    direct_cost = int(base_price * 0.74)
    material = int(direct_cost * ratios["재료비"] / 100)
    labor = int(direct_cost * ratios["노무비"] / 100)
    equipment = int(direct_cost * ratios["경비"] / 100)
    
    actual_material = int(material * (1 - material_discount / 100))
    actual_labor = int(labor * (1 - labor_discount / 100))
    actual_equipment = int(equipment * (1 - equipment_discount / 100))
    actual_direct = actual_material + actual_labor + actual_equipment
    
    indirect = int(actual_direct * 0.26 / 0.74)
    actual_total = actual_direct + indirect
    
    bubble_rate = round((1 - actual_total / base_price) * 100, 1)
    
    if bubble_rate >= 20:
        decision = "적극 참여"
        score = 95
    elif bubble_rate >= 15:
        decision = "참여 권장"
        score = 85
    elif bubble_rate >= 10:
        decision = "조건부 참여"
        score = 70
    elif bubble_rate >= 5:
        decision = "신중 검토"
        score = 55
    else:
        decision = "참여 불가"
        score = 30
    
    recommend_rate = round(100 - bubble_rate + min_profit_rate, 1)
    recommend_price = int(base_price * recommend_rate / 100)
    expected_profit = int(recommend_price - actual_total)
    
    return {
        "bid_no": bid_no,
        "summary": {
            "기초금액": f"{base_price:,}원",
            "예상원가": f"{actual_total:,}원",
            "거품률": f"{bubble_rate}%",
            "판정": decision,
            "점수": f"{score}점"
        },
        "cost_analysis": {
            "직접공사비": actual_direct,
            "간접공사비": indirect,
            "총원가": actual_total,
            "절감액": base_price - actual_total
        },
        "n2b_decision": {
            "decision": decision,
            "score": score,
            "bubble_rate": bubble_rate
        },
        "strategy": {
            "권장투찰률": f"{recommend_rate}%",
            "권장투찰가": f"{recommend_price:,}원",
            "예상이익": f"{expected_profit:,}원",
            "예상이익률": f"{round(expected_profit/recommend_price*100, 1)}%"
        },
        "n2b": {
            "not": f"이 공고는 단순히 {work_type} 공사가 아닙니다",
            "but": f"거품률 {bubble_rate}%의 {'참여 적합' if bubble_rate >= 10 else '신중 검토'} 공고입니다",
            "because": f"원가 {actual_total:,}원 대비 기초금액 {base_price:,}원으로, 절감 여력이 {'충분' if bubble_rate >= 15 else '제한적'}합니다"
        }
    }

# ============================================
# 디버그 API
# ============================================
@app.get("/api/debug/bid-api")
async def debug_bid_api(keyword: str = "", bid_type: str = "공사"):
    type_endpoints = {
        "물품": "getBidPblancListInfoThng",
        "공사": "getBidPblancListInfoCnstwk", 
        "용역": "getBidPblancListInfoServc",
    }
    
    endpoint = type_endpoints.get(bid_type, "getBidPblancListInfoCnstwk")
    url = f"https://apis.data.go.kr/1230000/ad/BidPublicInfoService/{endpoint}"
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    
    params = {
        "ServiceKey": PUBLIC_DATA_API_KEY,
        "pageNo": 1,
        "numOfRows": 10,
        "type": "json",
        "inqryDiv": "1",
        "inqryBgnDt": start_date.strftime("%Y%m%d") + "0000",
        "inqryEndDt": end_date.strftime("%Y%m%d") + "2359"
    }
    
    if keyword:
        params["bidNm"] = keyword
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            data = response.json() if response.status_code == 200 else None
            
            total_count = 0
            if data:
                total_count = data.get("response", {}).get("body", {}).get("totalCount", 0)
            
            return {
                "url": url,
                "bid_type": bid_type,
                "keyword": keyword,
                "params": {k: v for k, v in params.items() if k != "ServiceKey"},
                "api_key_preview": PUBLIC_DATA_API_KEY[:10] + "...",
                "status_code": response.status_code,
                "total_count": total_count,
                "response_preview": response.text[:500],
                "response_json": data
            }
    except Exception as e:
        return {
            "error": str(e),
            "url": url
        }

@app.get("/api/debug/price-api")
async def debug_price_api(keyword: str = "복공판"):
    url = "https://apis.data.go.kr/1230000/ao/PriceInfoService/getPriceInfoListFcltyCmmnMtrilEngrk"
    
    params = {
        "serviceKey": PUBLIC_DATA_API_KEY,
        "numOfRows": "5",
        "pageNo": "1",
        "type": "json",
        "prdctClsfcNoNm": keyword
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            return {
                "url": url,
                "keyword": keyword,
                "status_code": response.status_code,
                "response_preview": response.text[:1000],
                "response_json": response.json() if response.status_code == 200 else None
            }
    except Exception as e:
        return {
            "url": url,
            "error": str(e)
        }


# ============================================
# 낙찰정보 API 디버그 (NEW!)
# ============================================
@app.get("/api/debug/bid-result-api")
async def debug_bid_result_api(bid_type: str = "공사", keyword: str = ""):
    """조달청 낙찰정보서비스 API 직접 테스트"""
    
    # 낙찰정보 엔드포인트
    url = "https://apis.data.go.kr/1230000/as/ScsbidInfoService/getScsbidListSttusCnstwk"
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    
    params = {
        "ServiceKey": PUBLIC_DATA_API_KEY,
        "pageNo": 1,
        "numOfRows": 10,
        "type": "json",
        "inqryDiv": "1",
        "inqryBgnDt": start_date.strftime("%Y%m%d") + "0000",
        "inqryEndDt": end_date.strftime("%Y%m%d") + "2359"
    }
    
    if keyword:
        params["bidNtceNm"] = keyword
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            data = response.json() if response.status_code == 200 else None
            
            total_count = 0
            sample_items = []
            field_names = []
            
            if data:
                body = data.get("response", {}).get("body", {})
                total_count = body.get("totalCount", 0)
                items = body.get("items", [])
                
                # items 정규화
                if isinstance(items, dict):
                    items = items.get("item", [])
                if isinstance(items, dict):
                    items = [items]
                if not isinstance(items, list):
                    items = []
                
                # 첫 번째 아이템의 필드명 추출
                if items:
                    field_names = list(items[0].keys())
                    
                    # 샘플 아이템에서 핵심 필드만 추출
                    for item in items[:3]:
                        sample_items.append({
                            "bidNtceNm": item.get("bidNtceNm", ""),
                            "presmptPrce": item.get("presmptPrce", ""),
                            "sucsfbidAmt": item.get("sucsfbidAmt", ""),
                            "sucsBidLwetRate": item.get("sucsBidLwetRate", ""),
                            "sucsfbidCorpNm": item.get("sucsfbidCorpNm", ""),
                            "bidNtceNo": item.get("bidNtceNo", ""),
                            "opengDt": item.get("opengDt", ""),
                        })
            
            return {
                "url": url,
                "status_code": response.status_code,
                "total_count": total_count,
                "field_names": field_names,
                "sample_items": sample_items,
                "api_key_preview": PUBLIC_DATA_API_KEY[:10] + "...",
                "response_preview": response.text[:1000]
            }
    except Exception as e:
        return {
            "url": url,
            "error": str(e)
        }


# ============================================
# 낙찰률 조회 API - 조달청 낙찰정보서비스
# ★★★ 수정됨: 실제 API 응답 필드명에 맞춤 ★★★
# ============================================
@app.get("/api/bid-rate")
async def get_bid_rate(
    work_type: str = "도로",
    min_price: int = 30000000,
    max_price: int = 1000000000,
    days: int = 30
):
    """공종별/금액별 평균 낙찰률 조회"""
    
    # 공종 키워드 매핑
    work_type_keywords = {
        "도로": ["도로", "포장", "아스콘", "아스팔트"],
        "토목": ["토목", "토공", "기초", "굴착"],
        "건축": ["건축", "건물", "신축", "리모델링"],
        "전기": ["전기", "조명", "배선", "통신"],
        "설비": ["설비", "기계", "배관", "소방"],
        "조경": ["조경", "식재", "녹지", "공원"],
        "상하수도": ["상하수도", "관로", "배수", "급수"],
        "설계": ["설계", "기본설계", "실시설계"],
        "감리": ["감리", "건설사업관리", "CM"],
        "엔지니어링": ["엔지니어링", "기술용역", "조사"],
        "SW개발": ["소프트웨어", "SW", "정보시스템", "전산"],
        "시설관리": ["시설관리", "청소", "경비", "위탁"],
        "일반용역": ["용역"],
        "전산장비": ["전산", "컴퓨터", "서버", "네트워크"],
        "사무용품": ["사무", "용품", "소모품"],
        "자재": ["자재", "건축자재", "전기자재"],
        "일반물품": ["물품", "납품"]
    }
    
    keywords = work_type_keywords.get(work_type, [work_type])
    
    # 조달청 낙찰정보서비스 API - 업무별 엔드포인트
    bid_category = COST_RATIOS.get(work_type, {}).get("category", "공사")
    endpoint_map = {
        "공사": "getScsbidListSttusCnstwk",
        "용역": "getScsbidListSttusServc",
        "물품": "getScsbidListSttusThng"
    }
    api_endpoint = endpoint_map.get(bid_category, "getScsbidListSttusCnstwk")
    url = f"https://apis.data.go.kr/1230000/as/ScsbidInfoService/{api_endpoint}"
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    params = {
        "ServiceKey": PUBLIC_DATA_API_KEY,
        "pageNo": 1,
        "numOfRows": 100,
        "type": "json",
        "inqryDiv": "1",
        "inqryBgnDt": start_date.strftime("%Y%m%d") + "0000",
        "inqryEndDt": end_date.strftime("%Y%m%d") + "2359"
    }
    
    bid_rates = []
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            
            if response.status_code != 200:
                return get_default_bid_rate(work_type, min_price, max_price, error=f"API status: {response.status_code}")
            
            data = response.json()
            body = data.get("response", {}).get("body", {})
            items = body.get("items", [])
            
            # ★ items 정규화 (dict → list 변환)
            if isinstance(items, dict):
                items = items.get("item", [])
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                items = []
            
            if not items:
                return get_default_bid_rate(work_type, min_price, max_price, error="No items in response")
            
            for item in items:
                try:
                    # ★★★ 실제 API 필드명으로 수정 ★★★
                    bid_name = item.get("bidNtceNm", "") or item.get("bidNm", "")
                    
                    # 추정가격 (presmptPrce)
                    base_price_val = item.get("presmptPrce", 0)
                    try:
                        base_price_val = int(float(base_price_val or 0))
                    except:
                        base_price_val = 0
                    
                    # 낙찰금액 (sucsfbidAmt)
                    bid_price_val = item.get("sucsfbidAmt", 0)
                    try:
                        bid_price_val = int(float(bid_price_val or 0))
                    except:
                        bid_price_val = 0
                    
                    # 낙찰하한율 (sucsBidLwetRate) - 직접 제공되는 경우
                    direct_rate = item.get("sucsBidLwetRate", None)
                    
                    # 필터링: 금액 범위
                    if base_price_val > 0 and (base_price_val < min_price or base_price_val > max_price):
                        continue
                    
                    # 필터링: 공종 키워드
                    if not any(kw in bid_name for kw in keywords):
                        continue
                    
                    # 낙찰률 계산
                    rate = 0
                    if direct_rate:
                        # API에서 직접 낙찰률을 제공하는 경우
                        try:
                            rate = float(direct_rate)
                        except:
                            rate = 0
                    elif base_price_val > 0 and bid_price_val > 0:
                        # 직접 계산
                        rate = (bid_price_val / base_price_val) * 100
                    
                    if 70 <= rate <= 100:
                        bid_rates.append({
                            "name": bid_name[:50],
                            "base_price": base_price_val,
                            "bid_price": bid_price_val,
                            "rate": round(rate, 2),
                            "source": "API직접" if direct_rate else "계산"
                        })
                except Exception as item_err:
                    print(f"[bid-rate] Item parse error: {item_err}")
                    continue
            
            if not bid_rates:
                return get_default_bid_rate(work_type, min_price, max_price, 
                                           error=f"No matching data (total items: {len(items)}, keywords: {keywords})")
            
            # 평균 계산
            avg_rate = sum(r["rate"] for r in bid_rates) / len(bid_rates)
            min_rate = min(r["rate"] for r in bid_rates)
            max_rate = max(r["rate"] for r in bid_rates)
            
            return {
                "work_type": work_type,
                "period": f"최근 {days}일",
                "price_range": f"{min_price//10000000}천만원 ~ {max_price//10000000}천만원",
                "sample_count": len(bid_rates),
                "avg_bid_rate": round(avg_rate, 2),
                "min_bid_rate": round(min_rate, 2),
                "max_bid_rate": round(max_rate, 2),
                "samples": bid_rates[:10],
                "source": "조달청 낙찰정보서비스",
                "경쟁가": {
                    "설명": "경쟁가 = 기초금액 × 평균 낙찰률",
                    "평균낙찰률": f"{round(avg_rate, 2)}%"
                }
            }
            
    except Exception as e:
        return get_default_bid_rate(work_type, min_price, max_price, error=str(e))


def get_default_bid_rate(work_type: str, min_price: int, max_price: int, error: str = None):
    """기본 낙찰률 (API 실패 시 또는 데이터 부족 시)"""
    
    default_rates = {
        "도로": 84.5,
        "토목": 83.2,
        "건축": 86.1,
        "전기": 85.3,
        "설비": 84.8,
        "조경": 83.7,
        "상하수도": 84.2,
        "설계": 88.5,
        "감리": 87.8,
        "엔지니어링": 88.0,
        "SW개발": 89.2,
        "시설관리": 86.5,
        "일반용역": 87.5,
        "전산장비": 90.5,
        "사무용품": 91.0,
        "자재": 89.5,
        "일반물품": 90.0,
        "기타": 85.0
    }
    
    if max_price <= 100000000:
        adjustment = -1.5
    elif max_price <= 500000000:
        adjustment = -0.5
    else:
        adjustment = 0.5
    
    base_rate = default_rates.get(work_type, 85.0)
    adjusted_rate = base_rate + adjustment
    
    result = {
        "work_type": work_type,
        "period": "경험적 기준값",
        "price_range": f"{min_price//10000000}천만원 ~ {max_price//10000000}천만원",
        "sample_count": 0,
        "avg_bid_rate": round(adjusted_rate, 2),
        "min_bid_rate": round(adjusted_rate - 3, 2),
        "max_bid_rate": round(adjusted_rate + 3, 2),
        "samples": [],
        "source": "경험적 기준값 (API 데이터 부족)"
    }
    
    if error:
        result["error"] = error
    
    return result


@app.get("/api/bid-rate/summary")
async def get_bid_rate_summary():
    """전체 공종별 평균 낙찰률 요약"""
    
    work_types = ["도로", "토목", "건축", "전기", "설비", "조경", "상하수도", "설계", "감리", "SW개발", "전산장비"]
    results = {}
    
    for wt in work_types:
        result = await get_bid_rate(work_type=wt, days=90)
        results[wt] = {
            "avg_rate": result["avg_bid_rate"],
            "sample_count": result["sample_count"],
            "source": result["source"]
        }
    
    return {
        "summary": results,
        "updated": datetime.now().isoformat(),
        "note": "낙찰률 = 낙찰금액 / 기초금액 × 100% (= 경쟁가율)"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
