from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, date
import os
from dotenv import load_dotenv
import requests
from pathlib import Path

# Load .env from the same directory as this file, regardless of where uvicorn is run from
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

app = FastAPI(title="TaxNudge API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

client = AsyncIOMotorClient(MONGO_URL)
db = client.taxnudge

# ─── Models ───────────────────────────────────────────────────
class UserCreate(BaseModel):
    name: str
    email: EmailStr
    profession: str  # auto_driver, freelancer, shopkeeper, plumber, other
    monthly_income: float

class IncomeLog(BaseModel):
    user_id: str
    amount: float
    source: str
    date: str = str(date.today())

class TaxNudge(BaseModel):
    user_id: str
    daily_earning: float

class ChatRequest(BaseModel):
    message: str
    system: str = ""
    max_tokens: int = 1000

# ─── Tax Calculation Logic ─────────────────────────────────────
def calculate_tax_nudge(monthly_income: float, daily_earning: float):
    annual_income = monthly_income * 12
    # India tax slabs (New Regime FY 2024-25)
    if annual_income <= 300000:
        tax = 0
    elif annual_income <= 700000:
        tax = (annual_income - 300000) * 0.05
    elif annual_income <= 1000000:
        tax = 20000 + (annual_income - 700000) * 0.10
    elif annual_income <= 1200000:
        tax = 50000 + (annual_income - 1000000) * 0.15
    elif annual_income <= 1500000:
        tax = 80000 + (annual_income - 1200000) * 0.20
    else:
        tax = 140000 + (annual_income - 1500000) * 0.30

    daily_tax_liability = tax / 365
    nudge_amount = round(daily_tax_liability * 1.1, 0)
    advance_tax_quarterly = round(tax / 4, 0)

    return {
        "annual_tax": round(tax, 2),
        "daily_save_amount": nudge_amount,
        "quarterly_advance_tax": advance_tax_quarterly,
        "effective_rate": round((tax / annual_income) * 100, 1) if annual_income > 0 else 0,
        "message": get_nudge_message(nudge_amount, daily_earning)
    }

def get_nudge_message(nudge: float, earning: float):
    if earning == 0:
        return "Aaj ki kamai log karo!"
    pct = (nudge / earning) * 100
    if pct < 5:
        return f"Aaj sirf ₹{int(nudge)} side rakh do — ITR tension-free rahegi! 🎯"
    elif pct < 10:
        return f"₹{int(nudge)} bacha lo aaj — future-you thank karega! 💪"
    else:
        return f"Thoda tight hai, par ₹{int(nudge)} zaroor bachao tax ke liye! 🙏"

# ─── Routes ───────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "TaxNudge API running 🚀"}

@app.post("/api/users/register")
async def register_user(user: UserCreate):
    existing = await db.users.find_one({"email": user.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    result = await db.users.insert_one({
        **user.dict(),
        "created_at": datetime.utcnow(),
        "total_saved": 0
    })
    return {"user_id": str(result.inserted_id), "message": "Registered successfully!"}

@app.get("/api/users/{user_id}")
async def get_user(user_id: str):
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user["_id"] = str(user["_id"])
    return user

@app.post("/api/nudge")
async def get_nudge(data: TaxNudge):
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(data.user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    nudge = calculate_tax_nudge(user["monthly_income"], data.daily_earning)
    await db.nudge_logs.insert_one({
        "user_id": data.user_id,
        "daily_earning": data.daily_earning,
        "nudge_amount": nudge["daily_save_amount"],
        "date": str(date.today()),
        "created_at": datetime.utcnow()
    })
    return nudge

@app.post("/api/income/log")
async def log_income(income: IncomeLog):
    result = await db.income_logs.insert_one({
        **income.dict(),
        "created_at": datetime.utcnow()
    })
    return {"id": str(result.inserted_id), "message": "Income logged!"}

@app.get("/api/income/{user_id}/history")
async def income_history(user_id: str):
    logs = await db.income_logs.find({"user_id": user_id}).sort("created_at", -1).limit(30).to_list(30)
    for log in logs:
        log["_id"] = str(log["_id"])
    return logs

@app.get("/api/tax/estimate")
async def tax_estimate(monthly_income: float, daily_earning: float = 0):
    return calculate_tax_nudge(monthly_income, daily_earning)

@app.get("/api/stats/{user_id}")
async def user_stats(user_id: str):
    logs = await db.nudge_logs.find({"user_id": user_id}).to_list(365)
    total_saved = sum(log.get("nudge_amount", 0) for log in logs)
    total_days = len(logs)
    return {
        "total_saved": round(total_saved, 2),
        "total_days_tracked": total_days,
        "avg_daily_save": round(total_saved / total_days, 2) if total_days > 0 else 0
    }

# ─── Unified AI Chat Endpoint (proxies ALL frontend AI calls) ──
@app.post("/api/chat")
async def chat_ai(chat: ChatRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": chat.max_tokens,
        "messages": [
            {"role": "user", "content": chat.message}
        ]
    }

    if chat.system:
        payload["system"] = chat.system

    print("=== SENDING TO ANTHROPIC ===")
    print("Model:", payload["model"])
    print("Key prefix:", ANTHROPIC_API_KEY[:15])
    print("Payload:", payload)

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json=payload,
            timeout=30
        )
        print("=== ANTHROPIC RESPONSE ===")
        print("Status:", res.status_code)
        print("Body:", res.text)  # ← This shows the EXACT error
        res.raise_for_status()
        return res.json()

    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"AI service error: {e.response.text}")