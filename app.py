from flask import Flask, render_template, jsonify
from playwright.sync_api import sync_playwright
import requests
import json
import time
from datetime import datetime
import threading

app = Flask(__name__)

# Fon listesi
FUNDS = [
    {
        "code": "DOH",
        "name": "TERA PORTFÖY DÖRDÜNCÜ HİSSE SENEDİ SERBEST FON",
        "short_name": "Tera Portföy 4. Hisse",
        "url": "https://fvt.com.tr/fonlar/yatirim-fonlari/DOH"
    },
    {
        "code": "PBR",
        "name": "PUSULA PORTFÖY BİRİNCİ DEĞİŞKEN FON",
        "short_name": "Pusula Portföy Değişken",
        "url": "https://fvt.com.tr/fonlar/yatirim-fonlari/PBR"
    },
    {
        "code": "DFI",
        "name": "ATLAS PORTFÖY SERBEST FON",
        "short_name": "Atlas Portföy Serbest",
        "url": "https://fvt.com.tr/fonlar/yatirim-fonlari/DFI"
    },
    {
        "code": "PHE2",
        "name": "FVT KS HSYF",
        "short_name": "FVT KS Hisse",
        "url": "https://fvt.com.tr/fonlar/yatirim-fonlari/PHE2"
    }
]

# Cache for fund data
fund_cache = {
    "data": [],
    "last_updated": None,
    "loading": False
}
cache_lock = threading.Lock()


def get_fund_api_data(fund_code):
    """FVT API'den fon bilgilerini çeker (fiyat, getiri vs.)"""
    try:
        resp = requests.get(
            f"https://fvt.com.tr/api/funds/{fund_code}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Referer": "https://fvt.com.tr/"
            },
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("data", {}).get("fund"):
                fund = data["data"]["fund"]
                returns = data["data"].get("returns", {})
                return {
                    "price": fund.get("fiyat"),
                    "daily_return": fund.get("getiri"),
                    "category": fund.get("kategori"),
                    "risk": fund.get("risk"),
                    "weekly_return": returns.get("haftalikGetiri"),
                    "monthly_return": returns.get("aylikGetiri"),
                    "ytd_return": returns.get("ytdGetiri"),
                    "yearly_return": returns.get("birYillikGetiri"),
                    "last_update": fund.get("sonGuncelleme"),
                    "total_value": fund.get("toplamDeger"),
                    "investors": fund.get("yatirimci")
                }
    except Exception as e:
        print(f"API error for {fund_code}: {e}")
    return {}


def scrape_all_funds():
    """Tüm fonların verilerini çeker - tek browser instance ile"""
    results = []
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            for fund in FUNDS:
                fund_data = {
                    "code": fund["code"],
                    "name": fund["name"],
                    "short_name": fund["short_name"],
                    "url": fund["url"],
                    "estimate": None,
                    "price": None,
                    "daily_return": None,
                    "category": None,
                    "risk": None,
                    "weekly_return": None,
                    "monthly_return": None,
                    "ytd_return": None,
                    "yearly_return": None,
                    "error": None
                }
                
                # Önce API'den sabit verileri çek
                api_data = get_fund_api_data(fund["code"])
                fund_data.update({k: v for k, v in api_data.items() if v is not None})
                
                try:
                    page = context.new_page()
                    page.goto(fund["url"], wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(3000)
                    
                    # JavaScript ile tahmin verisini çek
                    data = page.evaluate("""
                        () => {
                            const result = { estimate: null };
                            
                            // Günün Tahmini değerini bul
                            const allElements = document.querySelectorAll('*');
                            for (const el of allElements) {
                                const directText = Array.from(el.childNodes)
                                    .filter(n => n.nodeType === 3)
                                    .map(n => n.textContent.trim())
                                    .join('');
                                    
                                if (directText.includes('Günün Tahmini')) {
                                    const nextSib = el.nextElementSibling;
                                    if (nextSib) {
                                        const sibText = nextSib.innerText.trim();
                                        const match = sibText.match(/[+-]?\\d+[.,]\\d+/);
                                        if (match) {
                                            result.estimate = match[0];
                                        }
                                    }
                                    
                                    const parent = el.parentElement;
                                    if (parent && !result.estimate) {
                                        const parentText = parent.innerText;
                                        const matches = parentText.match(/[%]\\s*([+-]?\\d+[.,]\\d+)|([+-]?\\d+[.,]\\d+)\\s*[%]/g);
                                        if (matches && matches.length > 0) {
                                            result.estimate = matches[0].replace('%', '').trim();
                                        }
                                    }
                                }
                            }
                            
                            // Yedek yaklaşım
                            if (!result.estimate) {
                                const bodyText = document.body.innerText;
                                const tahminiIndex = bodyText.indexOf('Günün Tahmini');
                                if (tahminiIndex !== -1) {
                                    const afterText = bodyText.substring(tahminiIndex, tahminiIndex + 100);
                                    const match = afterText.match(/[+-]?\\d+[.,]\\d+/);
                                    if (match) {
                                        result.estimate = match[0];
                                    }
                                }
                            }
                            
                            return result;
                        }
                    """)
                    
                    if data.get("estimate"):
                        fund_data["estimate"] = data["estimate"]
                    
                    page.close()
                    
                except Exception as e:
                    fund_data["error"] = str(e)
                    
                results.append(fund_data)
            
            browser.close()
            
    except Exception as e:
        for fund in FUNDS:
            api_data = get_fund_api_data(fund["code"])
            results.append({
                "code": fund["code"],
                "name": fund["name"],
                "short_name": fund["short_name"],
                "url": fund["url"],
                "estimate": None,
                "price": api_data.get("price"),
                "daily_return": api_data.get("daily_return"),
                "error": str(e)
            })
    
    return results


@app.route("/")
def index():
    return render_template("index.html", funds=FUNDS)


@app.route("/api/funds")
def get_funds():
    """Tüm fonların tahmin verilerini döndürür"""
    with cache_lock:
        if fund_cache["loading"]:
            return jsonify({
                "status": "loading",
                "message": "Veriler yükleniyor, lütfen bekleyin...",
                "data": fund_cache.get("data", []),
                "last_updated": fund_cache.get("last_updated")
            })
    
    # Cache kontrolü - 2 dakikadan yeni ise cache'den dön
    if fund_cache["last_updated"]:
        elapsed = (datetime.now() - fund_cache["last_updated"]).total_seconds()
        if elapsed < 120 and fund_cache["data"]:
            return jsonify({
                "status": "success",
                "data": fund_cache["data"],
                "last_updated": fund_cache["last_updated"].strftime("%H:%M:%S"),
                "cached": True
            })
    
    with cache_lock:
        fund_cache["loading"] = True
    
    try:
        results = scrape_all_funds()
        
        with cache_lock:
            fund_cache["data"] = results
            fund_cache["last_updated"] = datetime.now()
            fund_cache["loading"] = False
        
        return jsonify({
            "status": "success",
            "data": results,
            "last_updated": datetime.now().strftime("%H:%M:%S"),
            "cached": False
        })
    except Exception as e:
        with cache_lock:
            fund_cache["loading"] = False
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/api/refresh")
def refresh():
    """Cache'i temizleyip yeni veri çeker"""
    with cache_lock:
        fund_cache["last_updated"] = None
    return get_funds()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
