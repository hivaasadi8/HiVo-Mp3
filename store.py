import json, time, threading, base64, requests


class GitHubJSONBackend:
    """ذخیره داده روی یه فایل JSON تو گیت‌هاب. با RLock و retry."""

    def __init__(self, token, repo, path, branch="main"):
        self.token = token
        self.repo = repo
        self.path = path
        self.branch = branch
        self.api = f"https://api.github.com/repos/{repo}/contents/{path}"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        self._lock = threading.RLock()
        self._sha = None
        self._cache = None
        self._last_fetch = 0
        self._ttl = 15

    def _fetch(self):
        r = requests.get(self.api, headers=self.headers,
                         params={"ref": self.branch}, timeout=15)
        if r.status_code == 404:
            self._sha = None
            return {}
        r.raise_for_status()
        data = r.json()
        self._sha = data["sha"]
        content = base64.b64decode(data["content"]).decode()
        return json.loads(content or "{}")

    def load(self, force=False):
        with self._lock:
            now = time.time()
            if not force and self._cache is not None and now - self._last_fetch < self._ttl:
                return self._cache
            try:
                self._cache = self._fetch()
                self._last_fetch = now
            except Exception as e:
                print("[store] load error:", e)
                if self._cache is None:
                    self._cache = {}
            return self._cache

    def save(self, data):
        with self._lock:
            self._cache = data
            self._last_fetch = time.time()
            for attempt in range(4):
                try:
                    if self._sha is None:
                        self.load(force=True)
                    payload = {
                        "message": f"update db {int(time.time())}",
                        "content": base64.b64encode(
                            json.dumps(data, ensure_ascii=False, indent=2).encode()
                        ).decode(),
                        "branch": self.branch,
                    }
                    if self._sha:
                        payload["sha"] = self._sha
                    r = requests.put(self.api, headers=self.headers,
                                     json=payload, timeout=20)
                    if r.status_code in (200, 201):
                        self._sha = r.json()["content"]["sha"]
                        return True
                    if r.status_code == 409:
                        self.load(force=True)
                        continue
                    r.raise_for_status()
                except Exception as e:
                    print(f"[store] save attempt {attempt+1} failed:", e)
                    time.sleep(1.5 * (attempt + 1))
            return False


class Store:
    """لایه دامنه: کاربران، پریمیوم، آمار، تنظیمات."""

    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.RLock()

    def _get(self):
        return self.backend.load()

    def get_user(self, uid):
        with self.lock:
            d = self._get()
            return d.setdefault("users", {}).get(str(uid), {})

    def upsert_user(self, uid, **fields):
        with self.lock:
            d = self._get()
            users = d.setdefault("users", {})
            u = users.setdefault(str(uid), {})
            u.update(fields)
            u["last_seen"] = int(time.time())
            self.backend.save(d)
            return u

    def is_premium(self, uid):
        u = self.get_user(uid)
        return u.get("premium_until", 0) > time.time()

    def grant_premium(self, uid, days):
        with self.lock:
            d = self._get()
            u = d.setdefault("users", {}).setdefault(str(uid), {})
            now = time.time()
            base = max(u.get("premium_until", 0), now)
            u["premium_until"] = base + days * 86400
            self.backend.save(d)
            return u["premium_until"]

    def inc_stat(self, key, amount=1):
        with self.lock:
            d = self._get()
            stats = d.setdefault("stats", {})
            stats[key] = stats.get(key, 0) + amount
            self.backend.save(d)

    def inc_user_conversions(self, uid):
        with self.lock:
            d = self._get()
            u = d.setdefault("users", {}).setdefault(str(uid), {})
            u["conversions"] = u.get("conversions", 0) + 1
            self.backend.save(d)
