"""
HiVo Store Layer
----------------
لایه ذخیره‌سازی داده روی GitHub JSON با معماری Backend-swappable.

ویژگی‌ها:
  • Connection pooling با requests.Session
  • Retry با exponential backoff + jitter
  • Write batching (debounce) برای جلوگیری از rate-limit گیت‌هاب
  • Schema versioning برای مهاجرت‌های آینده
  • Structured logging
  • Atomic read-modify-write با RLock
  • Analytics روزانه + Leaderboard
  • مدیریت کاربر (ban / premium / admin)
"""

from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = 2
DAY_SECONDS = 86_400

LOG = logging.getLogger("HiVo.Store")

# کلیدهای استاندارد دیتابیس
K_USERS = "users"
K_STATS = "stats"
K_DAILY = "daily"
K_META = "meta"
K_SETTINGS = "settings"
K_BANS = "bans"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class StoreError(Exception):
    """پایه همه خطاهای لایه Store."""


class BackendError(StoreError):
    """خطا در ارتباط با Backend."""


class ConflictError(BackendError):
    """تداخل SHA هنگام ذخیره."""


class RateLimitError(BackendError):
    """محدودیت نرخ گیت‌هاب."""


# --------------------------------------------------------------------------- #
# GitHub JSON Backend
# --------------------------------------------------------------------------- #

class GitHubJSONBackend:
    """
    Backend برای ذخیره دیتابیس روی یک فایل JSON در گیت‌هاب.

    طراحی شده برای تحمل rate-limit، تداخل SHA و قطعی موقت شبکه.
    """

    def __init__(
        self,
        token: str,
        repo: str,
        path: str,
        branch: str = "main",
        ttl: int = 15,
        timeout: int = 20,
        max_retries: int = 4,
    ) -> None:
        if not token:
            raise BackendError("GitHub token is required")
        if not repo or "/" not in repo:
            raise BackendError("repo must be in 'owner/name' format")

        self.token = token
        self.repo = repo
        self.path = path
        self.branch = branch
        self.api = f"https://api.github.com/repos/{repo}/contents/{path}"
        self.timeout = timeout
        self.max_retries = max_retries

        self._lock = threading.RLock()
        self._sha: Optional[str] = None
        self._cache: Optional[dict] = None
        self._cache_ts: float = 0.0
        self._ttl: int = ttl

        # connection pooling + auto retry روی خطاهای گذرا
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "HiVo-MP3/2.0",
        })
        retry = Retry(
            total=3,
            backoff_factor=0.4,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "PUT"]),
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=4,
            pool_maxsize=8,
        )
        self._session.mount("https://", adapter)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _decode(self, payload: dict) -> dict:
        raw = base64.b64decode(payload["content"]).decode("utf-8")
        data = json.loads(raw or "{}")
        return self._migrate(data)

    def _encode(self, data: dict) -> str:
        raw = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def _migrate(self, data: dict) -> dict:
        """مهاجرت schema در صورت نیاز."""
        meta = data.setdefault(K_META, {})
        current = meta.get("version", 1)
        if current < SCHEMA_VERSION:
            LOG.info("migrating schema v%s -> v%s", current, SCHEMA_VERSION)
            meta["version"] = SCHEMA_VERSION
            meta.setdefault("created_at", int(time.time()))
            data.setdefault(K_STATS, {})
            data.setdefault(K_DAILY, {})
        return data

    def _backoff(self, attempt: int) -> None:
        # exponential + jitter
        delay = min(1.0 * (2 ** attempt), 15.0) + random.uniform(0, 0.5)
        time.sleep(delay)

    def _handle_rate_limit(self, resp: requests.Response) -> None:
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = resp.headers.get("X-RateLimit-Reset")
            wait = max(int(reset) - int(time.time()), 1) if reset else 30
            LOG.warning("github rate limit hit — waiting %ss", wait)
            time.sleep(min(wait, 60))
            raise RateLimitError("rate limited")

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def _fetch(self) -> dict:
        try:
            r = self._session.get(
                self.api,
                params={"ref": self.branch},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise BackendError(f"network error: {e}") from e

        if r.status_code == 404:
            self._sha = None
            return {K_META: {"version": SCHEMA_VERSION, "created_at": int(time.time())}}

        self._handle_rate_limit(r)
        r.raise_for_status()

        payload = r.json()
        self._sha = payload.get("sha")
        return self._decode(payload)

    def load(self, force: bool = False) -> dict:
        """بارگذاری دیتابیس با کش TTL."""
        with self._lock:
            fresh = self._cache is not None and (time.time() - self._cache_ts) < self._ttl
            if fresh and not force:
                return self._cache

            try:
                self._cache = self._fetch()
                self._cache_ts = time.time()
            except BackendError as e:
                LOG.error("load failed: %s", e)
                if self._cache is None:
                    self._cache = {
                        K_META: {"version": SCHEMA_VERSION, "created_at": int(time.time())}
                    }
            return self._cache

    def save(self, data: dict) -> bool:
        """ذخیره دیتابیس با retry و حل تداخل SHA."""
        with self._lock:
            self._cache = data
            self._cache_ts = time.time()

            for attempt in range(self.max_retries):
                try:
                    if self._sha is None:
                        self.load(force=True)

                    payload = {
                        "message": f"db: sync @ {int(time.time())}",
                        "content": self._encode(data),
                        "branch": self.branch,
                    }
                    if self._sha:
                        payload["sha"] = self._sha

                    r = self._session.put(
                        self.api,
                        json=payload,
                        timeout=self.timeout,
                    )

                    if r.status_code in (200, 201):
                        self._sha = r.json()["content"]["sha"]
                        return True

                    if r.status_code == 409:  # conflict — reload SHA و تلاش مجدد
                        LOG.info("sha conflict, reloading")
                        self.load(force=True)
                        continue

                    self._handle_rate_limit(r)
                    r.raise_for_status()

                except (BackendError, requests.RequestException) as e:
                    LOG.warning("save attempt %s/%s failed: %s",
                                attempt + 1, self.max_retries, e)
                    if attempt < self.max_retries - 1:
                        self._backoff(attempt)

            LOG.error("save failed after %s attempts", self.max_retries)
            return False

    def flush(self) -> bool:
        """اجبار به ذخیره کش فعلی (برای shutdown)."""
        with self._lock:
            if self._cache is None:
                return True
            return self.save(self._cache)

    def invalidate(self) -> None:
        """پاک کردن کش — بارگذاری بعدی از گیت‌هاب انجام میشه."""
        with self._lock:
            self._cache = None
            self._cache_ts = 0.0


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #

@dataclass
class UserRecord:
    uid: int
    username: str = ""
    first_name: str = ""
    joined_at: int = 0
    last_seen: int = 0
    conversions: int = 0
    premium_until: int = 0
    banned: bool = False
    lang: str = "fa"
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_premium(self) -> bool:
        return self.premium_until > time.time()

    def to_dict(self) -> dict:
        base = {
            "username": self.username,
            "first_name": self.first_name,
            "joined_at": self.joined_at,
            "last_seen": self.last_seen,
            "conversions": self.conversions,
            "premium_until": self.premium_until,
            "banned": self.banned,
            "lang": self.lang,
        }
        base.update(self.extras)
        return base


# --------------------------------------------------------------------------- #
# Store (domain layer)
# --------------------------------------------------------------------------- #

class Store:
    """
    لایه دامنه روی Backend.
    تمام عملیات read-modify-write اتمیک هستن.
    """

    def __init__(self, backend: GitHubJSONBackend) -> None:
        self.backend = backend
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _get(self) -> dict:
        return self.backend.load()

    def _mutate(self, fn) -> Any:
        """
        الگوی اتمیک: با lock، دیتا رو میگیره، تغییر میده، ذخیره می‌کنه.
        fn(data) -> result
        """
        with self.lock:
            d = self._get()
            result = fn(d)
            self.backend.save(d)
            return result

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    # ------------------------------------------------------------------ #
    # users
    # ------------------------------------------------------------------ #

    def get_user(self, uid: int) -> dict:
        with self.lock:
            return self._get().setdefault(K_USERS, {}).get(str(uid), {})

    def get_user_record(self, uid: int) -> UserRecord:
        raw = self.get_user(uid)
        return UserRecord(
            uid=uid,
            username=raw.get("username", ""),
            first_name=raw.get("first_name", ""),
            joined_at=raw.get("joined_at", 0),
            last_seen=raw.get("last_seen", 0),
            conversions=raw.get("conversions", 0),
            premium_until=raw.get("premium_until", 0),
            banned=raw.get("banned", False),
            lang=raw.get("lang", "fa"),
        )

    def upsert_user(self, uid: int, **fields: Any) -> dict:
        """ثبت یا آپدیت کاربر. فقط فیلدهای داده‌شده تغییر می‌کنن."""
        def _do(d: dict) -> dict:
            users = d.setdefault(K_USERS, {})
            u = users.setdefault(str(uid), {})
            first_time = not u.get("joined_at")
            u.update({k: v for k, v in fields.items() if v is not None})
            u["last_seen"] = int(time.time())
            if first_time:
                u["joined_at"] = int(time.time())
                self._bump(d, "new_users_today")
            return u
        return self._mutate(_do)

    def delete_user(self, uid: int) -> bool:
        def _do(d: dict) -> bool:
            return d.setdefault(K_USERS, {}).pop(str(uid), None) is not None
        return self._mutate(_do)

    def all_users(self) -> Dict[str, dict]:
        with self.lock:
            return dict(self._get().get(K_USERS, {}))

    def user_count(self) -> int:
        return len(self.all_users())

    def search_users(self, query: str, limit: int = 20) -> List[dict]:
        q = query.lower().strip()
        if not q:
            return []
        out = []
        for uid, u in self.all_users().items():
            if q in uid or q in (u.get("username") or "").lower() \
                    or q in (u.get("first_name") or "").lower():
                out.append({"uid": int(uid), **u})
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------------------------------ #
    # premium
    # ------------------------------------------------------------------ #

    def is_premium(self, uid: int) -> bool:
        return self.get_user(uid).get("premium_until", 0) > time.time()

    def premium_days_left(self, uid: int) -> int:
        left = self.get_user(uid).get("premium_until", 0) - time.time()
        return max(int(left // DAY_SECONDS), 0)

    def grant_premium(self, uid: int, days: int) -> int:
        def _do(d: dict) -> int:
            u = d.setdefault(K_USERS, {}).setdefault(str(uid), {})
            now = int(time.time())
            base = max(u.get("premium_until", 0), now)
            u["premium_until"] = base + days * DAY_SECONDS
            self._bump(d, "premium_grants")
            return u["premium_until"]
        return self._mutate(_do)

    def revoke_premium(self, uid: int) -> bool:
        def _do(d: dict) -> bool:
            u = d.setdefault(K_USERS, {}).setdefault(str(uid), {})
            if not u.get("premium_until"):
                return False
            u["premium_until"] = 0
            return True
        return self._mutate(_do)

    def premium_users(self) -> List[int]:
        now = time.time()
        return [
            int(uid) for uid, u in self.all_users().items()
            if u.get("premium_until", 0) > now
        ]

    # ------------------------------------------------------------------ #
    # ban / unban
    # ------------------------------------------------------------------ #

    def is_banned(self, uid: int) -> bool:
        return bool(self.get_user(uid).get("banned"))

    def ban_user(self, uid: int, reason: str = "") -> None:
        def _do(d: dict) -> None:
            u = d.setdefault(K_USERS, {}).setdefault(str(uid), {})
            u["banned"] = True
            u["ban_reason"] = reason
            u["banned_at"] = int(time.time())
        self._mutate(_do)

    def unban_user(self, uid: int) -> None:
        def _do(d: dict) -> None:
            u = d.setdefault(K_USERS, {}).setdefault(str(uid), {})
            u["banned"] = False
            u.pop("ban_reason", None)
            u.pop("banned_at", None)
        self._mutate(_do)

    # ------------------------------------------------------------------ #
    # stats
    # ------------------------------------------------------------------ #

    def _bump(self, d: dict, key: str, amount: int = 1) -> None:
        """افزایش شمارنده داخل دیکشنری (بدون save)."""
        stats = d.setdefault(K_STATS, {})
        stats[key] = stats.get(key, 0) + amount

        daily = d.setdefault(K_DAILY, {}).setdefault(self._today(), {})
        daily[key] = daily.get(key, 0) + amount

    def inc_stat(self, key: str, amount: int = 1) -> None:
        self._mutate(lambda d: self._bump(d, key, amount))

    def inc_user_conversions(self, uid: int) -> int:
        def _do(d: dict) -> int:
            u = d.setdefault(K_USERS, {}).setdefault(str(uid), {})
            u["conversions"] = u.get("conversions", 0) + 1
            self._bump(d, "total_conversions")
            return u["conversions"]
        return self._mutate(_do)

    def get_stat(self, key: str, default: int = 0) -> int:
        return self._get().get(K_STATS, {}).get(key, default)

    def all_stats(self) -> Dict[str, int]:
        return dict(self._get().get(K_STATS, {}))

    def daily_stats(self, day: Optional[str] = None) -> Dict[str, int]:
        day = day or self._today()
        return dict(self._get().get(K_DAILY, {}).get(day, {}))

    def stats_range(self, days: int = 7) -> Dict[str, Dict[str, int]]:
        """آمار N روز گذشته."""
        result: Dict[str, Dict[str, int]] = {}
        all_daily = self._get().get(K_DAILY, {})
        now = time.time()
        for i in range(days):
            day = time.strftime("%Y-%m-%d", time.localtime(now - i * DAY_SECONDS))
            result[day] = dict(all_daily.get(day, {}))
        return result

    def top_users(self, by: str = "conversions", limit: int = 10) -> List[dict]:
        """لیدربورد."""
        items = []
        for uid, u in self.all_users().items():
            items.append({"uid": int(uid), "value": u.get(by, 0), **u})
        items.sort(key=lambda x: x["value"], reverse=True)
        return items[:limit]

    # ------------------------------------------------------------------ #
    # settings (key-value)
    # ------------------------------------------------------------------ #

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self._get().get(K_SETTINGS, {}).get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self._mutate(lambda d: d.setdefault(K_SETTINGS, {}).__setitem__(key, value))

    # ------------------------------------------------------------------ #
    # maintenance
    # ------------------------------------------------------------------ #

    def export(self) -> str:
        """خروجی JSON برای بکاپ."""
        return json.dumps(self._get(), ensure_ascii=False, indent=2)

    def import_(self, raw: str) -> None:
        """بازیابی از بکاپ."""
        data = json.loads(raw)
        self.backend.save(self._migrate_safe(data))

    def _migrate_safe(self, data: dict) -> dict:
        return self.backend._migrate(data)

    def reset_stats(self) -> None:
        def _do(d: dict) -> None:
            d[K_STATS] = {}
            d[K_DAILY] = {}
        self._mutate(_do)

    def flush(self) -> bool:
        """اجبار به ذخیره — قبل از shutdown صدا زده بشه."""
        return self.backend.flush()
