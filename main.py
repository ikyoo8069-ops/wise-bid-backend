# ============================================
# 슬기로운 입찰생활 - Backend v3.4
# + 조달청 가격정보 API 연동
# + 공종별 비율 DB
# + 개략원가 자동 산출
# + N2B 참여 판정
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
from datetime import date, datetime

app = FastAPI(title="N2B Backend v3.4", description="wise-bid + 가격정보API + 개략원가산출")

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
    "기타": {"재료비": 50, "노무비": 30, "경비": 20, "description": "일반 공사"}
}

# 간접비 비율 (직접공사비 대비)
INDIRECT_RATIOS = {
    "간접노무비": 12.0,  # 직접노무비의 12%
    "산재보험료": 3.7,
    "고용보험료": 1.05,
    "건강보험료": 3.545,
    "연금보험료": 4.5,
    "퇴직공제부금": 2.3,
    "안전관리비": 1.97,  # 공사 규모별 상이
    "환경보전비": 0.5,
    "일반관리비": 6.0,   # 직접공사비의 6%
    "이윤": 15.0         # 직접공사비+일반관리비의 15%
}

# ============================================
# 일일 사용 제한
# ============================================
LIMITS = {
    "biz": {"normal": 10, "premium": 200},
    "proposal": {"normal": 10, "premium": 200},
    "agency": {"normal": 100},
    "bid": {"normal": 10, "premium": 200},
    "cost": {"normal": 20, "premium": 200}  # 원가분석용
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
    """개략원가 산출 요청"""
    base_price: int  # 기초금액
    work_type: str = "기타"  # 공종
    material_discount: float = 0  # 재료비 절감률 (%)
    labor_discount: float = 0  # 노무비 절감률 (%)
    equipment_discount: float = 0  # 경비 절감률 (%)
    
class N2BDecisionRequest(BaseModel):
    """N2B 참여 판정 요청"""
    base_price: int  # 기초금액
    estimated_cost: int  # 예상 원가
    work_type: str = "기타"
    min_profit_rate: float = 10  # 최소 요구 수익률 (%)
    company_strength: List[str] = []  # 회사 강점
    company_weakness: List[str] = []  # 회사 약점

class PriceSearchRequest(BaseModel):
    """자재/시공 단가 검색"""
    keyword: str
    category: str = "all"  # all, material, labor, equipment

# ============================================
# 조달청 가격정보 API
# ============================================
async def fetch_material_prices(keyword: str, category: str = "토목") -> list:
    """시설공통자재 가격 조회"""
    base_url = "http://apis.data.go.kr/1230000/PriceInfoService"
    
    # 카테고리별 엔드포인트
    endpoints = {
        "토목": "getCmmnFcltyMtrlCivilInfo",
        "건축": "getCmmnFcltyMtrlBldgInfo", 
        "기계": "getCmmnFcltyMtrlMachInfo",
        "전기": "getCmmnFcltyMtrlElcInfo"
    }
    
    endpoint = endpoints.get(category, "getCmmnFcltyMtrlCivilInfo")
    
    params = {
        "serviceKey": PUBLIC_DATA_API_KEY,
        "numOfRows": "20",
        "pageNo": "1",
        "type": "json",
        "prdctClsfcNoNm": keyword  # 품명 검색
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
                        "spec": item.get("dtilPrdctClsfcNoNm", ""),
                        "unit": item.get("unt", ""),
                        "price": int(item.get("prc", 0)),
                        "date": item.get("rgstDt", ""),
                        "region": item.get("splyAreaNm", "전국")
                    })
                return results
    except Exception as e:
        print(f"가격정보 API 오류: {e}")
    
    return []

async def fetch_market_prices(keyword: str, category: str = "토목") -> list:
    """시장시공가격 조회"""
    base_url = "http://apis.data.go.kr/1230000/PriceInfoService"
    
    endpoints = {
        "토목": "getMrktCnsttnPrcCivilInfo",
        "건축": "getMrktCnsttnPrcBldgInfo",
        "기계": "getMrktCnsttnPrcMachInfo"
    }
    
    endpoint = endpoints.get(category, "getMrktCnsttnPrcCivilInfo")
    
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
    """
    개략원가 산출
    
    기초금액에서 역산하여 직접공사비 추정 후,
    업체별 절감률을 적용하여 실제원가 계산
    """
    
    # 1. 공종별 비율 가져오기
    ratios = COST_RATIOS.get(work_type, COST_RATIOS["기타"])
    
    # 2. 기초금액 구성 역산 (일반적인 설계금액 구조)
    # 기초금액 = 직접공사비 + 간접공사비 + 일반관리비 + 이윤
    # 대략 직접공사비 = 기초금액 / 1.35 (35% 간접비 가정)
    direct_cost_ratio = 0.74  # 직접공사비가 기초금액의 약 74%
    
    estimated_direct_cost = int(base_price * direct_cost_ratio)
    
    # 3. 직접공사비 구성
    material_cost = int(estimated_direct_cost * ratios["재료비"] / 100)
    labor_cost = int(estimated_direct_cost * ratios["노무비"] / 100)
    equipment_cost = int(estimated_direct_cost * ratios["경비"] / 100)
    
    # 4. 표준원가 (설계기준)
    standard_cost = {
        "재료비": material_cost,
        "노무비": labor_cost,
        "경비": equipment_cost,
        "직접공사비": estimated_direct_cost,
        "간접비": int(base_price * 0.26),  # 26% 간접비
        "합계": base_price
    }
    
    # 5. 실제원가 (업체 절감률 적용)
    actual_material = int(material_cost * (1 - material_discount / 100))
    actual_labor = int(labor_cost * (1 - labor_discount / 100))
    actual_equipment = int(equipment_cost * (1 - equipment_discount / 100))
    actual_direct = actual_material + actual_labor + actual_equipment
    
    # 간접비도 비례 감소 (직접비 감소에 따라)
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
    
    # 6. 거품률 계산
    bubble_rate = ((base_price - actual_total) / base_price * 100) if base_price > 0 else 0
    
    # 7. 투찰 범위 계산 (예정가격 ±3% 범위 고려)
    min_expected_price = int(base_price * 0.97)
    max_expected_price = int(base_price * 1.03)
    
    # 최저 투찰가 = 원가 + 최소이익 (5%)
    min_bid_price = int(actual_total * 1.05)
    
    # 권장 투찰률 = 원가/기초금액 + 10% 마진
    recommended_rate = (actual_total / base_price * 100) + 10 if base_price > 0 else 88
    recommended_rate = min(recommended_rate, 95)  # 최대 95%
    recommended_rate = max(recommended_rate, 75)  # 최소 75%
    
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
    """
    N2B 프레임워크 기반 참여 판정
    """
    
    # 1. 기본 지표 계산
    bubble_rate = ((base_price - estimated_cost) / base_price * 100) if base_price > 0 else 0
    potential_profit = base_price - estimated_cost
    profit_rate = (potential_profit / estimated_cost * 100) if estimated_cost > 0 else 0
    
    # 2. 참여 점수 계산 (100점 만점)
    score = 50  # 기본점수
    
    # 거품률 점수 (0-30점)
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
    else:
        score += 0
    
    # 수익률 점수 (0-20점)
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
    
    # 회사 강점/약점 반영
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
    
    # 점수 범위 제한
    score = max(0, min(100, score))
    
    # 3. 판정
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
    
    # 4. N2B 분석문 생성
    n2b = {
        "not": f"단순히 기초금액 {base_price:,}원이 커서 참여하는 것이 아니다",
        "but": f"실제원가 {estimated_cost:,}원 대비 거품률 {bubble_rate:.1f}%가 판단 기준이다",
        "because": f"거품률이 {'충분하여' if bubble_rate >= 15 else '부족하여'} 예상수익률 {profit_rate:.1f}%{'로 참여 가치가 있다' if profit_rate >= min_profit_rate else '로 리스크가 있다'}"
    }
    
    # 5. 리스크 분석
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
        "service": "wise-bid API v3.4",
        "features": [
            "가격정보 API 연동",
            "공종별 비율 DB",
            "개략원가 자동 산출",
            "N2B 참여 판정"
        ],
        "endpoints": {
            "/api/cost-ratios": "공종별 비율 조회",
            "/api/price-search": "자재/시공 단가 검색",
            "/api/cost-estimate": "개략원가 산출",
            "/api/n2b-decision": "N2B 참여 판정"
        }
    }

@app.get("/api/cost-ratios")
async def get_cost_ratios():
    """공종별 원가 비율 조회"""
    return {
        "공종별비율": COST_RATIOS,
        "간접비비율": INDIRECT_RATIOS
    }

@app.get("/api/cost-ratio/{work_type}")
async def get_cost_ratio(work_type: str):
    """특정 공종 비율 조회"""
    if work_type in COST_RATIOS:
        return COST_RATIOS[work_type]
    return {"error": f"공종 '{work_type}' 없음", "available": list(COST_RATIOS.keys())}

@app.post("/api/price-search")
async def search_prices(req: PriceSearchRequest, request: Request):
    """자재/시공 단가 검색"""
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
    """개략원가 산출"""
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
    """N2B 참여 판정"""
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
    """빠른 개략원가 산출 (GET)"""
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
    """빠른 N2B 판정 (GET)"""
    result = analyze_n2b_decision(
        base_price=base_price,
        estimated_cost=estimated_cost,
        work_type=work_type,
        min_profit_rate=min_profit_rate
    )
    return result

# ============================================
# 통합 분석 엔드포인트
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
    """
    통합 분석: 개략원가 + N2B 판정 + 투찰 전략
    """
    
    # 1. 개략원가 산출
    cost_result = calculate_rough_cost(
        base_price=base_price,
        work_type=work_type,
        material_discount=material_discount,
        labor_discount=labor_discount,
        equipment_discount=equipment_discount
    )
    
    estimated_cost = cost_result["실제원가"]["합계"]
    
    # 2. N2B 판정
    decision_result = analyze_n2b_decision(
        base_price=base_price,
        estimated_cost=estimated_cost,
        work_type=work_type,
        min_profit_rate=min_profit_rate
    )
    
    # 3. 투찰 전략
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
    """사용량 조회"""
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
