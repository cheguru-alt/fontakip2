from flask import Flask, render_template, jsonify
import requests
import json
import re
from datetime import datetime
import threading
import time

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
        "url": "https://fvt.com.tr/fonlar/yatirim-fonlari/PBR2"
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
    "Accept": "application/json",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Referer": "https://fvt.com.tr/"
}

# JavaScript kodu - tahmin değerini bulan script
EXTRACT_ESTIMATE_JS = """
() => {
    // Yöntem 1: span'ları tara
    const spans = document.querySelectorAll('span');
    for (let i = 0; i < spans.length; i++) {
        const text = spans[i].textContent.trim();
        if (text === 'Günün Tahmini' || text.includes('Günün Tahmini')) {
            let nextEl = spans[i].nextElementSibling;
            while (nextEl) {
                const val = nextEl.textContent.trim();
                const match = val.match(/^[+-]?\\d+[.,]\\d+\\s*%?$/);
                if (match) return val.replace('%', '').trim();
                nextEl = nextEl.nextElementSibling;
            }
            const parent = spans[i].parentElement;
            if (parent) {
                const childSpans = parent.querySelectorAll('span');
                let found = false;
                for (const cs of childSpans) {
                    if (cs.textContent.includes('Günün Tahmini')) { found = true; continue; }
                    if (found) {
                        const val = cs.textContent.trim();
                        const m = val.match(/[+-]?\\d+[.,]\\d+/);
                        if (m) return m[0];
                    }
                }
            }
        }
    }
    // Yöntem 2: body text
    const bodyText = document.body.innerText;
    const idx = bodyText.indexOf('Günün Tahmini');
    if (idx !== -1) {
        const after = bodyText.substring(idx + 13, idx + 60);
        const m = after.match(/[+-]?\\d+[.,]\\d+/);
        if (m) return m[0];
    }
    return null;
}
"""


def scrape_estimates_with_playwright():
    """Playwright ile tüm fonların 'Günün Tahmini' değerlerini PARALEL çeker"""
    estimates = {}
    
    if not PLAYWRIGHT_AVAILABLE:
        return estimates
    
    start_time = time.time()
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            # TÜM SAYFALARI AYNI ANDA AÇ (paralel)
            pages = {}
            for fund in FUNDS:
                page = context.new_page()
                page.goto(fund["url"], wait_until="domcontentloaded", timeout=30000)
                pages[fund["code"]] = page
                print(f"[LOAD] {fund['code']}: sayfa acildi")
            
            # Tüm sayfaların networkidle olmasını bekle (paralel bekleme)
            for code, page in pages.items():
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except:
                    pass
            
            # "Günün Tahmini" metninin görünmesini bekle
            for code, page in pages.items():
                try:
                    page.wait_for_function(
                        "() => document.body.innerText.includes('Günün Tahmini')",
                        timeout=10000
                    )
                except:
                    pass
            
            # Kısa ek bekleme (JS render tamamlansın)
            list(pages.values())[0].wait_for_timeout(2000)
            
            # Tüm sayfalardan değerleri çek
            for code, page in pages.items():
                try:
                    result = page.evaluate(EXTRACT_ESTIMATE_JS)
                    if result:
                        estimates[code] = result.replace(',', '.')
                        print(f"[OK] {code}: Gunun Tahmini = {estimates[code]}")
                    else:
                        # Retry: 3 saniye daha bekle ve tekrar dene
                        page.wait_for_timeout(3000)
                        result = page.evaluate(EXTRACT_ESTIMATE_JS)
                        if result:
                            estimates[code] = result.replace(',', '.')
                            print(f"[OK] {code}: Gunun Tahmini = {estimates[code]} (retry)")
                        else:
                            print(f"[WARN] {code}: Tahmin bulunamadi")
                except Exception as e:
                    print(f"[ERR] {code}: {e}")
                
                page.close()
            
            browser.close()
            
    except Exception as e:
        print(f"[ERR] Playwright browser error: {e}")
    
    elapsed = time.time() - start_time
    print(f"[INFO] Playwright tamamlandi: {len(estimates)}/{len(FUNDS)} fon, {elapsed:.1f}sn")
    return estimates


def get_fund_api_data(fund_code):
    """FVT API'den fon bilgilerini çeker"""
    try:
        resp = requests.get(
            f"https://fvt.com.tr/api/funds/{fund_code}",
            headers=HEADERS,
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


def get_all_api_data():
    """Tüm fonların API verilerini paralel çeker"""
    results = {}
    threads = []
    
    def fetch_one(code):
        results[code] = get_fund_api_data(code)
    
    for fund in FUNDS:
        t = threading.Thread(target=fetch_one, args=(fund["code"],))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join(timeout=15)
    
    return results


def fetch_all_funds():
    """Tüm fonların verilerini çeker - API paralel + Playwright paralel"""
    results = []
    start = time.time()
    
    # API ve Playwright'ı aynı anda başlat
    api_results = {}
    estimates = {}
    
    def run_api():
        nonlocal api_results
        api_results = get_all_api_data()
    
    def run_playwright():
        nonlocal estimates
        estimates = scrape_estimates_with_playwright()
    
    api_thread = threading.Thread(target=run_api)
    pw_thread = threading.Thread(target=run_playwright)
    
    api_thread.start()
    pw_thread.start()
    
    api_thread.join(timeout=15)
    pw_thread.join(timeout=120)
    
    # Sonuçları birleştir
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
        
        # API verileri
        api_data = api_results.get(fund["code"], {})
        fund_data.update({k: v for k, v in api_data.items() if v is not None})
        
        # Playwright tahmin
        if fund["code"] in estimates:
            fund_data["estimate"] = estimates[fund["code"]]
        elif fund_data.get("daily_return"):
            fund_data["estimate"] = fund_data["daily_return"]
        
        results.append(fund_data)
    
    elapsed = time.time() - start
    print(f"[INFO] Toplam sure: {elapsed:.1f}sn")
    return results


# === ARKA PLAN YENİLEME ===
def background_refresh():
    """Arka planda periyodik veri yenileme"""
    while True:
        try:
            print("[BG] Arka plan yenileme basliyor...")
            results = fetch_all_funds()
            
            with cache_lock:
                fund_cache["data"] = results
                fund_cache["last_updated"] = datetime.now()
                fund_cache["loading"] = False
            
            print(f"[BG] Yenileme tamamlandi: {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[BG] Yenileme hatasi: {e}")
        
        # 2 dakika bekle
        time.sleep(120)


# Arka plan thread'ini başlat
bg_thread = threading.Thread(target=background_refresh, daemon=True)
bg_started = False


@app.route("/")
def index():
    global bg_started
    if not bg_started:
        bg_thread.start()
        bg_started = True
    return render_template("index.html", funds=FUNDS)


@app.route("/api/funds")
def get_funds():
    """Tüm fonların tahmin verilerini döndürür"""
    global bg_started
    if not bg_started:
        bg_thread.start()
        bg_started = True
    
    with cache_lock:
        if fund_cache["loading"]:
            return jsonify({
                "status": "loading",
                "message": "Veriler yükleniyor, lütfen bekleyin...",
                "data": fund_cache.get("data", []),
                "last_updated": None
            })
    
    # Cache'de veri varsa hemen dön
    if fund_cache["last_updated"] and fund_cache["data"]:
        return jsonify({
            "status": "success",
            "data": fund_cache["data"],
            "last_updated": fund_cache["last_updated"].strftime("%H:%M:%S"),
            "cached": True
        })
    
    # İlk yüklemede - veri henüz yoksa bekle
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


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
