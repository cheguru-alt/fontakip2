from flask import Flask, render_template, jsonify
import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime
import threading

app = Flask(__name__)

# Playwright kullanılabilir mi kontrol et
PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
    print("[INFO] Playwright kullanilabilir - tarayici tabanli scraping aktif")
except ImportError:
    print("[INFO] Playwright bulunamadi - sadece API verisi kullanilacak")

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

# Cache
fund_cache = {
    "data": [],
    "last_updated": None,
    "loading": False
}
cache_lock = threading.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://fvt.com.tr/"
}


def scrape_estimates_with_playwright():
    """Playwright ile tüm fonların 'Günün Tahmini' değerlerini çeker"""
    estimates = {}
    
    if not PLAYWRIGHT_AVAILABLE:
        return estimates
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            for fund in FUNDS:
                try:
                    page = context.new_page()
                    page.goto(fund["url"], wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(3000)
                    
                    # JavaScript ile tahmin verisini çek
                    estimate = page.evaluate("""
                        () => {
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
                                        if (match) return match[0];
                                    }
                                    
                                    const parent = el.parentElement;
                                    if (parent) {
                                        const parentText = parent.innerText;
                                        const idx = parentText.indexOf('Günün Tahmini');
                                        if (idx !== -1) {
                                            const after = parentText.substring(idx + 13);
                                            const match = after.match(/[+-]?\\d+[.,]\\d+/);
                                            if (match) return match[0];
                                        }
                                    }
                                }
                            }
                            
                            // Yedek: tüm sayfada ara
                            const bodyText = document.body.innerText;
                            const tahminiIdx = bodyText.indexOf('Günün Tahmini');
                            if (tahminiIdx !== -1) {
                                const after = bodyText.substring(tahminiIdx, tahminiIdx + 100);
                                const match = after.match(/[+-]?\\d+[.,]\\d+/);
                                if (match) return match[0];
                            }
                            
                            return null;
                        }
                    """)
                    
                    if estimate:
                        estimates[fund["code"]] = estimate.replace(',', '.')
                    
                    page.close()
                    
                except Exception as e:
                    print(f"Playwright scrape error for {fund['code']}: {e}")
            
            browser.close()
            
    except Exception as e:
        print(f"Playwright browser error: {e}")
    
    return estimates


def get_fund_api_data(fund_code):
    """FVT API'den fon bilgilerini çeker"""
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


def fetch_all_funds():
    """Tüm fonların verilerini çeker"""
    results = []
    
    # Playwright varsa tahmin verilerini çek
    estimates = scrape_estimates_with_playwright()
    
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
        
        try:
            # API'den detayları çek
            api_data = get_fund_api_data(fund["code"])
            fund_data.update({k: v for k, v in api_data.items() if v is not None})
            
            # Playwright'tan gelen tahmin varsa kullan
            if fund["code"] in estimates:
                fund_data["estimate"] = estimates[fund["code"]]
            elif fund_data.get("daily_return"):
                # Playwright yoksa API'deki günlük getiriyi kullan (yedek)
                fund_data["estimate"] = fund_data["daily_return"]
                
        except Exception as e:
            fund_data["error"] = str(e)
        
        results.append(fund_data)
    
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
        results = fetch_all_funds()
        
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
