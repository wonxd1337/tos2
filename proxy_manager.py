# proxy_manager.py
import threading
import time
import random
import os
import json
import requests
from queue import Queue
from collections import defaultdict
from config import Config

class ProxyManager:
    def __init__(self):
        self.proxy_list = []
        self.socks5_proxies = []  # List khusus SOCKS5
        self.http_proxies = []    # List HTTP/HTTPS
        self.proxy_stats = defaultdict(lambda: {
            'success': 0, 'fail': 0, 'total_time': 0,
            'avg_time': 1.0, 'weight': 1.0, 'last_used': 0,
            'proxy_type': 'http',
            'consec_fail': 0, 'last_fail_time': 0  # PATCH PROXY 10-28
        })
        self.lock = threading.Lock()
        self.running = True
        self.last_refresh = 0

        # ====== MULTI-TIER PROXY (patch 10-28) ======
        # Tier 1: TheSpeedX (primer, refresh 8 jam)
        # Tier 2: proxifly (fallback, aktif jika Tier1 non-dead < threshold)
        # Tier 3: premium pool (proxy TheSpeedX yang pernah sukses, persisten)
        self.cache_file = os.path.join(Config.TEMP_DIR, Config.PROXY_CACHE_FILE)
        self._sig_file = os.path.join(Config.TEMP_DIR, "proxy_list_signature.json")
        self._list_line_counts = {}       # jumlah baris tiap list terakhir (update-detect)
        self.fallback_loaded = False      # proxifly sudah pernah di-download?
        self.premium_pool = set()         # proxy yg pernah success>=1 (persisten)
        self._cache_load()                # muat cache/last-snapshot saat boot

        # Weight settings
        self.min_weight = 0.1
        self.max_weight = 3.0

        # SOCKS5 priority (higher = prefer SOCKS5)
        self.socks5_priority = 0.7  # 70% chance to use SOCKS5 if available

        # Mulai thread auto-refresh
        self.start_auto_refresh()
    
    def parse_proxy_string(self, proxy_str, default_type='socks5'):
        """Parse proxy string dan deteksi tipe.
        PATCH PROXY 10-28: TheSpeedX list formatnya "ip:port" polos,
        jadi harus prepend skema sesuai konteks."""
        proxy_str = proxy_str.strip()

        # Sudah ada prefix -> pakai apa adanya
        if '://' in proxy_str:
            if 'socks5' in proxy_str.lower():
                return {'url': proxy_str, 'type': 'socks5'}
            elif 'socks4' in proxy_str.lower():
                return {'url': proxy_str, 'type': 'socks4'}
            return {'url': proxy_str, 'type': 'http'}

        # "ip:port" polos: prepend skema sesuai default_type
        if default_type == 'socks5':
            return {'url': f'socks5://{proxy_str}', 'type': 'socks5'}
        elif default_type == 'socks4':
            return {'url': f'socks4a://{proxy_str}', 'type': 'socks4'}
        else:
            return {'url': f'http://{proxy_str}', 'type': 'http'}

    def download_proxies(self):
        """PATCH PROXY 10-28: download dari SEMUA Config.PROXY_LISTS (TheSpeedX),
        dedup, tiap list prepend skema sesuai tipe (socks5/socks4/http).
        PATCH 10-28 update-detect: simpan jumlah baris tiap list utk deteksi update.
        Hanya request ke raw.githubusercontent.com, TIDAK ke API target
        (hackertarget) — jadi tidak melanggar prinsip 'no direct' utk data."""
        headers = {"User-Agent": "Mozilla/5.0"}
        proxies = []
        socks5_temp = []
        http_temp = []
        line_counts = {}

        for list_url in Config.PROXY_LISTS:
            default_type = 'http'
            if 'socks5' in list_url.lower():
                default_type = 'socks5'
            elif 'socks4' in list_url.lower():
                default_type = 'socks4'
            try:
                response = requests.get(list_url, headers=headers,
                                         timeout=30, verify=False)
                if response.status_code != 200:
                    line_counts[list_url] = -1
                    continue
                lines = [l.strip() for l in response.text.split('\n') if l.strip()]
                line_counts[list_url] = len(lines)
                for line in lines:
                    parsed = self.parse_proxy_string(line, default_type)
                    proxies.append(parsed['url'])
                    if parsed['type'] in ('socks5', 'socks4'):
                        socks5_temp.append(parsed['url'])
                    else:
                        http_temp.append(parsed['url'])
                print(f"[+] list {default_type}: {len(lines)} baris", flush=True)
            except Exception:
                line_counts[list_url] = -1
                pass  # 1 list gagal -> lanjut, jangan mati total

        # PATCH 10-28 update-detect: bandingkan dgn jumlah baris terakhir
        changed = self._compare_line_counts(line_counts)
        if changed:
            print("[i] TheSpeedX list sudah di-update (jumlah baris berubah)", flush=True)

        proxies = list(dict.fromkeys(proxies))
        socks5_temp = list(dict.fromkeys(socks5_temp))
        http_temp = list(dict.fromkeys(http_temp))

        with self.lock:
            # Persist old stats (premium pool sudah di-update update_stats)
            old_proxies = set(self.proxy_list)
            new_proxies = set(proxies)
            for proxy in old_proxies - new_proxies:
                if proxy in self.proxy_stats:
                    del self.proxy_stats[proxy]
            self.socks5_proxies = socks5_temp
            self.http_proxies = http_temp
            self.proxy_list = proxies
            for proxy in new_proxies - old_proxies:
                self.proxy_stats[proxy] = {
                    'success': 0, 'fail': 0, 'total_time': 0,
                    'avg_time': 1.0, 'weight': 1.0, 'last_used': 0,
                    'proxy_type': 'socks5' if proxy in socks5_temp else 'http',
                    'consec_fail': 0, 'last_fail_time': 0
                }

        print(f"[+] Proxy Manager (Tier 1 TheSpeedX): {len(self.socks5_proxies)} SOCKS/4, "
              f"{len(self.http_proxies)} HTTP (total {len(proxies)})", flush=True)

        # ====== MULTI-TIER LOGIC (patch 10-28) ======
        # Tier 2: if Tier 1 non-dead < threshold, load proxifly as fallback
        # (hanya sekali, jangan download lagi tiap refresh)
        self._maybe_load_fallback()

        # Persist snapshot: last-known-good proxy list + stats + premium pool
        # (Tier 3: if download Tier 1 & 2 fail, tool muat dari sini)
        self._cache_save()

        return proxies

    def _maybe_load_fallback(self):
        """Tier 2: load proxifly HANYA jika TheSpeedX non-dead < threshold.
        Hasil append ke pool utama (dedup), bukan ganti."""
        threshold = getattr(Config, 'PROXY_FALLBACK_THRESHOLD', 200)
        now = time.time()
        alive_t1 = sum(
            1 for p, s in self.proxy_stats.items()
            if s.get('consec_fail', 0) < self.DEAD_PROXY_THRESHOLD
            or now - s.get('last_fail_time', 0) >= self.DEAD_PROXY_EXPIRY
        )
        if alive_t1 >= threshold:
            print(f"[i] Tier 1 healthy ({alive_t1} non-dead >= {threshold}), skip fallback", flush=True)
            return
        if self.fallback_loaded:
            print(f"[i] Fallback sudah pernah di-load ({alive_t1} non-dead < {threshold})", flush=True)
            return

        print(f"[!] Tier 1 low ({alive_t1} non-dead < {threshold}), load proxifly (Tier 2)...", flush=True)
        try:
            response = requests.get(Config.PROXY_FALLBACK_LIST,
                                   headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=30, verify=False)
            if response.status_code != 200:
                print(f"[!] Fallback download gagal ({response.status_code}), pakai cache/Tier 3", flush=True)
                return
            lines = [l.strip() for l in response.text.split('\n') if l.strip()]
            added = 0
            with self.lock:
                for line in lines:
                    purl = line if '://' in line else f'http://{line}'
                    purl = purl if not purl.startswith('http') else \
                           (purl if purl.startswith('http://') else f'http://{purl}')
                    # dedup: skip if sudah ada
                    if purl in self.proxy_stats:
                        continue
                    self.proxy_stats[purl] = {
                        'success': 0, 'fail': 0, 'total_time': 0,
                        'avg_time': 1.0, 'weight': 1.0, 'last_used': 0,
                        'proxy_type': 'http',
                        'consec_fail': 0, 'last_fail_time': 0
                    }
                    self.proxy_list.append(purl)
                    self.http_proxies.append(purl)
                    added += 1
                # update premium pool: tidak berubah (hanya append, tidak hapus)
            self.fallback_loaded = True
            print(f"[+] Fallback proxifly: {added} proxy ditambahkan (total {len(self.proxy_list)})", flush=True)
        except Exception as e:
            print(f"[!] Fallback load error: {e} — lanjut pakai cache/Tier 3", flush=True)

    def _cache_save(self):
        """Persist current state (last-known-good). Dipakai saat boot/download gagal."""
        try:
            Config.ensure_temp_dir()
            snapshot = {
                'ts': time.time(),
                'proxy_list': list(self.proxy_list),
                'premium_pool': sorted(self.premium_pool),
                'stats': {p: {k: v for k, v in s.items() if k != 'last_used' or v}
                           for p, s in self.proxy_stats.items()},
            }
            with open(self.cache_file, 'w') as f:
                json.dump(snapshot, f)
        except Exception as e:
            print(f"[!] cache save error: {e}", flush=True)

    def _cache_load(self):
        """Muat last-known-good dari file saat boot (Tier 3).
        Ini bikin pool TIDAK kosong even jika TheSpeedX/proxifly download gagal.
        Premium pool + proxy_list + stats dipulihkan dari snapshot terakhir."""
        try:
            if not os.path.exists(self.cache_file):
                print("[i] No proxy cache yet (fresh run)", flush=True)
                return
            with open(self.cache_file) as f:
                snap = json.load(f)

            # 1. Premium pool dulu (Tier 3 — proxy terverifikasi)
            for p in snap.get('premium_pool', []):
                self.premium_pool.add(p)

            # 2. Stats (proxy yang persisten antar-run)
            for p, s in snap.get('stats', {}).items():
                if p not in self.proxy_stats:
                    self.proxy_stats[p] = s

            # 3. proxy_list + socks5/http split — HANYA jika pool masih kosong
            #    (jangan overwrite list fresh yang sudah di-download)
            if not self.proxy_list:
                cached_list = snap.get('proxy_list', [])
                for p in cached_list:
                    if p not in self.proxy_stats:
                        self.proxy_stats[p] = {
                            'success': 0, 'fail': 0, 'total_time': 0,
                            'avg_time': 1.0, 'weight': 1.0, 'last_used': 0,
                            'proxy_type': 'socks5' if p.startswith('socks5') else 'http',
                            'consec_fail': 0, 'last_fail_time': 0
                        }
                    self.proxy_list.append(p)
                    if p.startswith('socks'):
                        self.socks5_proxies.append(p)
                    else:
                        self.http_proxies.append(p)
                print(f"[+] Cache: {len(cached_list)} proxy dipulihkan (pool masih kosong)", flush=True)

            print(f"[+] Cache: {len(self.premium_pool)} premium, "
                  f"{len(snap.get('stats', {}))} stats, "
                  f"saved {time.time()-snap.get('ts', 0):.0f}s ago", flush=True)
        except Exception as e:
            print(f"[!] cache load error: {e}", flush=True)

    def _compare_line_counts(self, new_counts):
        """PATCH 10-28 update-detect: bandingkan jumlah baris tiap list
        dgn signature terakhir. Return True jika ada perubahan.
        - File signature belum ada (run pertama / baru di-patch) -> True
          (pool perlu dire-merge utk proxy mati/baru)
        - Identik -> False (tiada yg berubah)"""
        old = {}
        try:
            if os.path.exists(self._sig_file):
                old = {k: v.get('lines', -1) for k, v in json.load(open(self._sig_file)).items()}
        except Exception:
            pass
        changed = False
        if not old:
            changed = True
        else:
            for url, n in new_counts.items():
                if old.get(url, -1) != n:
                    changed = True
                    print(f"[i] list berubah: {url.split('/')[-1]} "
                          f"{old.get(url, '?')} -> {n} baris", flush=True)
        # Simpan signature baru utk perbandingan berikutnya
        try:
            Config.ensure_temp_dir()
            with open(self._sig_file, 'w') as f:
                json.dump({u: {"lines": n} for u, n in new_counts.items()}, f)
        except Exception as e:
            print(f"[!] save signature error: {e}", flush=True)
        self._list_line_counts = new_counts
        return changed

    def stop(self):
        """Save cache & stop refresh thread (dipanggil main.cleanup())."""
        self.running = False
        self._cache_save()

    def refresh_proxies(self):
        """Refresh proxy list"""
        while self.running:
            time.sleep(Config.PROXY_REFRESH_INTERVAL)
            print("\n[*] Refreshing proxy list...")
            new_proxies = self.download_proxies()
            
            # PATCH MEMORY 10-28: cap proxy_stats ke PROXY_STATS_MAX.
            # Dulu: hapus 50% terburuk hanya saat > MAX_CACHE_SIZE(10000).
            # Sekarang: bound keras — evict paling lama tak terpakai / terburuk.
            with self.lock:
                if len(self.proxy_stats) > Config.PROXY_STATS_MAX:
                    # Prioritaskan hapus: (a) proxy tak ada di list aktif, (b) last_used terlama
                    active = set(self.proxy_list)
                    def sort_key(item):
                        p, s = item
                        if p in active:
                            return (0, s.get('last_used', 0))
                        return (1, s.get('last_used', 0))
                    sorted_proxies = sorted(
                        self.proxy_stats.items(),
                        key=sort_key
                    )
                    to_remove = sorted_proxies[:len(self.proxy_stats) - Config.PROXY_STATS_MAX]
                    for proxy, _ in to_remove:
                        del self.proxy_stats[proxy]
            
            print(f"[+] Proxy refreshed: {len(self.socks5_proxies)} SOCKS5, {len(self.http_proxies)} HTTP")
    
    def start_auto_refresh(self):
        """Mulai thread auto-refresh"""
        # Download initial
        self.download_proxies()
        
        # Start refresh thread
        refresh_thread = threading.Thread(target=self.refresh_proxies, daemon=True)
        refresh_thread.start()
    
    # PATCH HANG-FIX 10-28: _cache_save dipanggil LUAR lock proxy.
    # Dulu _cache_save() (write JSON ~1.5MB) jalan di dalam `with self.lock`,
    # bikin semua thread scanner nunggu lock proxy -> scan macet (terlihat
    # seperti "nyangkut setelah Auto cleanup").
    _last_cache_save = 0          # ts terakhir _cache_save
    _CACHE_SAVE_MIN_INTERVAL = 60 # throttle: max 1x/menit
    _cache_save_dirty = False

    def _cache_save_outside_lock(self):
        """Call setelah release lock: flush JSON hanya jika dirty + interval lewat."""
        now = time.time()
        if not self._cache_save_dirty or now - self._last_cache_save < self._CACHE_SAVE_MIN_INTERVAL:
            return
        self._last_cache_save = now
        self._cache_save_dirty = False
        self._cache_save()

    def update_stats(self, proxy, success, response_time=None):
        """Update statistik proxy"""
        if proxy not in self.proxy_stats:
            return

        with self.lock:
            stats = self.proxy_stats[proxy]
            stats['last_used'] = time.time()

            if success:
                stats['success'] += 1
                # PATCH PROXY 10-28: reset fail streak
                stats['consec_fail'] = 0
                # PATCH 10-28 multi-tier: proxy pertama kali sukses -> premium pool
                # (Tier 3: proxy yg terbukti work, persisten, dipakai terakhir)
                if stats['success'] == 1:
                    self.premium_pool.add(proxy)
                    # HANG-FIX: tandai dirty, flush LUAR lock (throttled 60s)
                    self._cache_save_dirty = True
                if response_time:
                    total = stats['total_time'] + response_time
                    count = stats['success'] + stats['fail']
                    stats['avg_time'] = total / count if count > 0 else response_time
                    stats['total_time'] = total
                    
                # Bonus weight untuk SOCKS5 yang sukses
                if stats['proxy_type'] == 'socks5':
                    stats['weight'] *= 1.1  # 10% bonus untuk SOCKS5
            else:
                stats['fail'] += 1
                # PATCH PROXY 10-28: tambah consecutive fail + catat waktu
                stats['consec_fail'] += 1
                stats['last_fail_time'] = time.time()
                # Penalty lebih kecil untuk SOCKS5 (lebih tahan lama)
                if stats['proxy_type'] == 'socks5':
                    stats['weight'] *= 0.8
                else:
                    stats['weight'] *= 0.5
            
            # Hitung ulang bobot
            total_reqs = stats['success'] + stats['fail']
            if total_reqs > 0:
                success_rate = stats['success'] / total_reqs
                speed_score = 1.0 / stats['avg_time'] if stats['avg_time'] > 0 else 1.0
                speed_score = min(speed_score, 2.0)
                
                weight = (success_rate * 0.6 + speed_score * 0.4) * 2
                stats['weight'] = max(self.min_weight, min(self.max_weight, weight))
                
                # SOCKS5 additional bonus
                if stats['proxy_type'] == 'socks5':
                    stats['weight'] = min(self.max_weight, stats['weight'] * 1.2)

        # HANG-FIX: flush cache di luar lock (throttled)
        self._cache_save_outside_lock()

    def get_proxy(self, prefer_socks5=True):
        """Dapatkan proxy dengan prioritas SOCKS5"""
        with self.lock:
            if not self.proxy_list:
                return None
            
            # Prioritaskan SOCKS5 jika ada dan prefer_socks5=True
            if prefer_socks5 and self.socks5_proxies:
                # 70% chance to use SOCKS5 if available
                if random.random() < self.socks5_priority:
                    # Pilih dari SOCKS5 proxies dengan weighted selection
                    socks5_with_stats = [
                        p for p in self.socks5_proxies 
                        if p in self.proxy_stats
                    ]
                    
                    if socks5_with_stats:
                        total_weight = sum(self.proxy_stats[p]['weight'] for p in socks5_with_stats)
                        if total_weight > 0:
                            r = random.uniform(0, total_weight)
                            cumulative = 0
                            for proxy in socks5_with_stats:
                                cumulative += self.proxy_stats[proxy]['weight']
                                if r <= cumulative:
                                    return {"http": proxy, "https": proxy}
                    
                    # Fallback ke random SOCKS5
                    proxy = random.choice(self.socks5_proxies)
                    return {"http": proxy, "https": proxy}
            
            # Jika tidak ada SOCKS5 atau prefer_socks5=False, gunakan HTTP
            http_with_stats = [
                p for p in self.http_proxies 
                if p in self.proxy_stats
            ]
            
            if http_with_stats:
                total_weight = sum(self.proxy_stats[p]['weight'] for p in http_with_stats)
                if total_weight > 0:
                    r = random.uniform(0, total_weight)
                    cumulative = 0
                    for proxy in http_with_stats:
                        cumulative += self.proxy_stats[proxy]['weight']
                        if r <= cumulative:
                            return {"http": proxy, "https": proxy}
            
            # Final fallback
            if self.proxy_list:
                proxy = random.choice(self.proxy_list)
                return {"http": proxy, "https": proxy}
            
            return None
    
    def get_different_proxy(self, last_proxy=None):
        """Dapatkan proxy yang berbeda dari yang terakhir digunakan"""
        with self.lock:
            if not self.proxy_list:
                return None
            
            available = [p for p in self.proxy_list if p != last_proxy]
            if not available:
                available = self.proxy_list
            
            # Prioritaskan SOCKS5
            socks5_available = [p for p in available if p in self.socks5_proxies]
            if socks5_available and random.random() < self.socks5_priority:
                proxy = random.choice(socks5_available)
                return {"http": proxy, "https": proxy}
            
            proxy = random.choice(available)
            return {"http": proxy, "https": proxy}
    
    # Set proxy yg gagal beruntun utk di-skip (PATCH PROXY 10-28)
    DEAD_PROXY_THRESHOLD = 5   # N kegagalan beruntun -> dead
    DEAD_PROXY_EXPIRY = 600    # dead proxy hidup lagi setelah N detik (mungkin sembuh)

    def get_proxy_for_retry(self, retry_count, last_proxy=None, avoid_set=None):
        """PATCH 10-28 multi-tier: prioritas Tier 3 (premium, terbukti sukses)
        -> Tier 1/2 (non-dead general) -> random last resort."""
        with self.lock:
            if not self.proxy_list:
                return None

            avoid = set(avoid_set or [])
            if last_proxy:
                avoid.add(last_proxy)

            now = time.time()

            # Tier 3: premium pool dulu (sudah terverifikasi sukses ke hackertarget)
            premium = [p for p in self.premium_pool
                       if p not in avoid
                       and (self.proxy_stats[p].get('consec_fail', 0) < self.DEAD_PROXY_THRESHOLD
                            or now - self.proxy_stats[p].get('last_fail_time', 0) >= self.DEAD_PROXY_EXPIRY)]
            if premium:
                proxy = random.choice(premium)
                return {"http": proxy, "https": proxy}

            # Tier 1/2: non-dead general
            sample_size = min(20, len(self.proxy_list))
            candidates = random.sample(self.proxy_list, sample_size)
            alive = [p for p in candidates
                     if p not in avoid
                     and (self.proxy_stats[p].get('consec_fail', 0) < self.DEAD_PROXY_THRESHOLD
                          or now - self.proxy_stats[p].get('last_fail_time', 0) >= self.DEAD_PROXY_EXPIRY)]
            if alive:
                if retry_count < 2:
                    socks5_alive = [p for p in alive if p in self.socks5_proxies]
                    if socks5_alive:
                        proxy = random.choice(socks5_alive)
                        return {"http": proxy, "https": proxy}
                proxy = random.choice(alive)
                return {"http": proxy, "https": proxy}

            # Last resort: random
            fallback = [p for p in random.sample(self.proxy_list, min(10, len(self.proxy_list)))
                       if p not in avoid]
            if not fallback:
                fallback = list(avoid.symmetric_difference(self.proxy_list))[:10] or self.proxy_list[:10]
            proxy = random.choice(fallback)
            return {"http": proxy, "https": proxy}

    def get_alive_proxies(self):
        """PATCH PROXY 10-28: daftar proxy yang non-dead ( utk logging / filter)."""
        with self.lock:
            now = time.time()
            dead = set()
            for p, s in self.proxy_stats.items():
                consec = s.get('consec_fail', 0) if isinstance(s, dict) else 0
                last_fail = s.get('last_fail_time', 0) if isinstance(s, dict) else 0
                if consec >= self.DEAD_PROXY_THRESHOLD and \
                   (now - last_fail) < self.DEAD_PROXY_EXPIRY:
                    dead.add(p)
            return [p for p in self.proxy_list if p not in dead]
    
    def print_stats(self):
        """Tampilkan statistik proxy"""
        print("\n" + "="*60)
        print("PROXY STATISTICS (Top 10)")
        print("="*60)
        print(f"📡 SOCKS5 Available: {len(self.socks5_proxies)} | HTTP: {len(self.http_proxies)}")
        print("-"*60)
        
        with self.lock:
            # Sort by weight
            sorted_proxies = sorted(
                [(p, s) for p, s in self.proxy_stats.items() if p in self.proxy_list],
                key=lambda x: x[1]['weight'],
                reverse=True
            )[:10]
            
            for proxy, stats in sorted_proxies:
                total = stats['success'] + stats['fail']
                if total > 0:
                    success_rate = (stats['success'] / total) * 100
                    proxy_type_icon = "🔒" if stats['proxy_type'] == 'socks5' else "🌐"
                    print(f"{proxy_type_icon} {proxy[:60]}")
                    print(f"  ✅ Success: {stats['success']} | ❌ Fail: {stats['fail']}")
                    print(f"  📊 Rate: {success_rate:.1f}% | Time: {stats['avg_time']:.2f}s")
                    print(f"  ⚖️ Weight: {stats['weight']:.2f}")
                    print()
    
    def stop(self):
        """Hentikan thread auto-refresh (PATCH MEMORY 10-28: dipakai main.cleanup)"""
        self.running = False
        self.cleanup()

    def cleanup(self):
        """Bersihkan resources"""
        self.running = False