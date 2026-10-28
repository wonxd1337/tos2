# config.py
import os
import tempfile

# Konfigurasi Scanner
class Config:
    # Thread settings
    MAX_THREADS_REVERSE = 30  # Thread untuk reverse IP (pakai proxy)
    MAX_THREADS_SCAN = 100     # Thread untuk scan domain (tanpa proxy)
    
    # Proxy settings
    PREFER_SOCKS5 = True
    SOCKS5_PRIORITY = 0.7
    PROXY_REFRESH_INTERVAL = 28800  # 8 jam dalam detik (sebagian list TheSpeedX
                                      # statis, refresh terlalu sering buang bandwidth)
    PROXY_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/all/data.txt"
    # PATCH PROXY 10-28: sumber proxy utama (TheSpeedX) — terverifikasi bisa tembus
    # ke api.hackertarget.com (hit ±10%/30 proxy, 0% utk proxifly). Refresh 8 jam.
    PROXY_LISTS = [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/refs/heads/master/socks5.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/refs/heads/master/socks4.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/refs/heads/master/http.txt",
    ]
    # Fallback Tier 2: proxifly — aktif HANYA jika Tier 1 (TheSpeedX) habis
    # (< PROXY_FALLBACK_THRESHOLD proxy non-dead). Volume gede meski hit rate rendah.
    PROXY_FALLBACK_LIST = "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/all/data.txt"
    # Jika proxy non-dead < threshold, aktifkan fallback
    PROXY_FALLBACK_THRESHOLD = 200
    # Persist cache: proxy + stats disimpan ke file, muat lagi saat boot / kalau
    # download kedua sumber gagal. Bisa jalan berhari-hari.
    PROXY_CACHE_FILE = "proxy_cache.json"
    
    # RNG settings
    MAX_VALID_RNG = 50
    MAX_VALID_RNG_LIMIT = 200  # Batas maksimal
    
    # Cache settings
    MAX_CACHE_SIZE = 10000  # Maksimal entries di cache
    CACHE_CLEANUP_INTERVAL = 3600  # Bersihkan cache setiap 1 jam
    
    # Memory management
    TEMP_DIR = os.path.expanduser("~/.mt_scanner_cache/")
    MAX_MEMORY_ITEMS = 5000  # Maksimal item dalam memory
    
    # ====== MEMORY OPTIMIZATION (patch 10-28) ======
    # Tier L1 in-memory cache (LRU) — hit sebelum SQLite
    L1_REVERSE_CACHE_MAX = 2000        # entry reverse-ip cache in-memory
    L1_PROCESSED_MAX = 20000          # IP diproses in-memory (set LRU)
    L1_WRITEBACK_BATCH = 500          # simpan ke SQLite tiap N entri baru
    L1_PURGE_CHECK_INTERVAL = 5000    # cek purge tiap N operasi
    REVERSE_CACHE_TTL = 21600         # 6 jam — domain reverse IP basi cepat;
                                      # lebih lama malah tabrakan dgn skip-logic
    PROCESSED_TTL = 0                 # 0 = permanen (skip logika butuh)
    PROCESSED_FILE_FLUSH = 500        # buffer flush ke processed_ips.txt
    FOUND_URLS_MAX = 10000            # bound set found_urls (LRU)
    PROXY_STATS_MAX = 500             # bound stats per-proxy (LRU by last_used)

    # ====== PROXY NO-POOL & RETRY (patch 10-28) ======
    # Per-request session utk proxied requests — TIDAK pakai pool shared,
    # menghindari ProxyError "Cannot connect to proxy" akibat pool exhaustion
    PROXY_REQUEST_PER_SESSION = True   # True = buat session baru per proxied request
    # Retry: coba proxy berbeda sampai dapat domain, atau mencapai limit.
    # Hit rate proxy TheSpeedX ke hackertarget ±10%/30 proxy (tes nyata 10-28),
    # jadi 30 attempt ≈ 96% IP dapat domain. 50 attempt ≈ 99.4%.
    REVERSE_MAX_ATTEMPTS = 50          # banyak proxy berbeda yg dicoba utk 1 reverse IP
    REVERSE_RETRY_BASE_DELAY = 2       # delay awal antar retry (detik, backoff exponential)
    REVERSE_RETRY_MAX_DELAY = 30       # cap delay max per retry
    # PATCH PROXY 10-28: timeout per attempt proxy (8s cukup utk proxy mati/keblokir,
    # proxy hidup ke hackertarget = 2-8s. 30s = buang waktu saat proxy dead).
    REVERSE_PROXY_TIMEOUT = 8
    # Total budget waktu utk SEMUA attempt 1 IP reverse (detik).
    # 300s = 5 menit/IP worst case. Dengan REVERSE_MAX_ATTEMPTS=50 & 
    # REVERSE_RETRY_BASE_DELAY=2s, budget 300s ≈ cukup utk 45-50 attempt.
    REVERSE_RETRY_TOTAL_BUDGET = 300
    # Domain scan tanpa proxy: retry loop terbatas
    DOMAIN_MAX_ATTEMPTS = 5
    
    # File output
    OUTPUT_FILES = {
        'movable_type': 'mv.txt',
        'movable_type_v4': 'mv4.txt',
        'processed_ips': 'processed_ips.txt',
        'cache': 'cache.db'
    }
    
    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAY = 2
    
    # Timeout settings
    TIMEOUT_REVERSE = 30
    TIMEOUT_SCAN = 10
    
    # ========== LEVEL 2 SMART SCAN SETTINGS ==========
    SAMPLE_SIZE = 10  # Jumlah sample IP untuk testing base
    MIN_DOMAINS_PER_IP = 20  # Minimal domain dari reverse IP
    MIN_GOOD_SAMPLES = 1
    
    @staticmethod
    def ensure_temp_dir():
        """Pastikan direktori temporary ada"""
        if not os.path.exists(Config.TEMP_DIR):
            os.makedirs(Config.TEMP_DIR)