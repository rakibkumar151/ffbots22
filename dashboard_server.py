# -*- @MASTER_FF_01 -*-
"""
MASTER OFFICIAL - Dashboard Server
Full Web Dashboard with Blue Ninja Theme
"""

import asyncio
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Dict, List, Any, Optional
from aiohttp import web

# ==================== PATHS ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(TEMPLATES_DIR, "static")
TEMPLATE_PATH = os.path.join(TEMPLATES_DIR, "index.html")
ACCOUNTS_FILE = "accounts.json"
WORKER_CONFIG_FILE = os.path.join(BASE_DIR, "worker_config.json")


def _load_game_worker_count() -> int:
    default_workers = 3
    try:
        if os.path.exists(WORKER_CONFIG_FILE):
            with open(WORKER_CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            value = int(config.get("game_workers", default_workers))
            return max(1, min(20, value))
    except Exception:
        pass
    return default_workers


# ==================== GLOBAL STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.game_workers: int = _load_game_worker_count()
        self.worker_event: asyncio.Event = asyncio.Event()

    def set_game_workers(self, count: int) -> int:
        self.game_workers = max(1, min(20, int(count)))
        try:
            self.worker_event.set()
        except Exception:
            pass
        try:
            cfg = {}
            if os.path.exists(WORKER_CONFIG_FILE):
                with open(WORKER_CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["game_workers"] = self.game_workers
            tmp = WORKER_CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, WORKER_CONFIG_FILE)
        except Exception as e:
            self.log(f"Worker config save error: {e}", "warning")
        self.log(f"⚡ Worker threads updated to {self.game_workers} (Realtime)", "success")
        return self.game_workers

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid: str, nickname: str, region: str, level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "is_paused": False,
                "uptime_seconds": int(time.time()),
                "match_history": []
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(f"{acc['nickname']} gained +{diff} EXP! Total: +{acc['gained_exp']}", "success", uid_str)
            self.recalc_totals()

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            acc["matches_played"] += 1
            acc["last_match_time"] = time.strftime("%H:%M:%S")
            acc["last_updated"] = time.strftime("%H:%M:%S")
            # Match history
            history = acc.get("match_history", [])
            history.append({
                "time": time.strftime("%H:%M:%S"),
                "msg": f"Match #{acc['matches_played']} completed"
            })
            if len(history) > 20:
                history.pop(0)
            acc["match_history"] = history
            self.log(f"{acc['nickname']} finished Match #{acc['matches_played']}", "info", uid_str)

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())

    def get_exp_per_hour(self) -> int:
        elapsed = max(1, time.time() - self.start_time)
        return int(self.total_gained_exp / (elapsed / 3600))

    def get_total_active_matches(self) -> int:
        return sum(acc.get("active_matches", 0) for acc in self.accounts.values())


bot_state = BotState()





def _load_dashboard_io_worker_count() -> int:
    """Read a safe 1-3 worker setting for dashboard filesystem I/O only."""
    default_workers = 2
    try:
        with open(WORKER_CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        value = int(config.get("dashboard_io_workers", default_workers))
    except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        value = default_workers
    return max(1, min(3, value))


DASHBOARD_IO_WORKERS = _load_dashboard_io_worker_count()


async def _run_dashboard_io(app: web.Application, func, *args):
    """Run independent blocking dashboard I/O off the asyncio event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        app["dashboard_io_executor"], partial(func, *args)
    )


async def _cleanup_dashboard_io_executor(app: web.Application) -> None:
    executor = app.get("dashboard_io_executor")
    if executor is not None:
        executor.shutdown(wait=False)


# ==================== HTTP HANDLERS ====================

def _load_index_content() -> str:
    """Load the dashboard template once so requests do not repeatedly read disk."""
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return """
    <html><body style="background:#050a18;color:#e8f4ff;font-family:sans-serif;padding:40px;text-align:center">
    <h1>⚠️ templates/index.html not found!</h1>
    <p>Please create templates/index.html</p>
    </body></html>
    """


def _read_accounts_file() -> List[Dict[str, Any]]:
    """Read account configuration defensively; malformed files fail closed."""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_accounts_file(accounts: List[Dict[str, Any]]) -> None:
    """Atomically replace accounts.json to avoid a partial file on interruption."""
    target = os.path.abspath(ACCOUNTS_FILE)
    directory = os.path.dirname(target) or os.curdir
    fd, temp_path = tempfile.mkstemp(prefix=".accounts-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


async def handle_index(request: web.Request) -> web.Response:
    content = _load_index_content()
    return web.Response(
        text=content,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "total_active_matches": bot_state.get_total_active_matches(),
        "game_workers": getattr(bot_state, "game_workers", 3),
        "exp_per_hour": bot_state.get_exp_per_hour(),
        "accounts": accounts_data,
        "logs": bot_state.logs[-60:],
        "uptime": int(time.time() - bot_state.start_time)
    })


async def handle_get_workers(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "game_workers": getattr(bot_state, "game_workers", 3),
        "total_active_matches": bot_state.get_total_active_matches()
    })


async def handle_set_workers(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        val = data.get("workers")
        if val is None:
            return web.json_response({"status": "error", "error": "Missing workers field"})
        new_count = bot_state.set_game_workers(val)
        return web.json_response({
            "status": "ok",
            "game_workers": new_count,
            "total_active_matches": bot_state.get_total_active_matches()
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_add_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()

        # Keep read-modify-write atomic across overlapping HTTP requests, while
        # sending the blocking filesystem work to a worker thread so it does
        # not stall unrelated asyncio tasks.
        async with request.app["accounts_lock"]:
            existing = await _run_dashboard_io(request.app, _read_accounts_file)

            if "uid" in data and "password" in data:
                uid = str(data["uid"]).strip()
                pwd = str(data["password"]).strip()
                if not uid or not pwd:
                    return web.json_response({"status": "error", "error": "UID and Password required"})
                existing = [acc for acc in existing if not isinstance(acc, dict) or str(acc.get("uid")) != uid]
                existing.append({"uid": uid, "password": pwd})
            elif "token" in data:
                token = str(data["token"]).strip()
                if not token:
                    return web.json_response({"status": "error", "error": "Token required"})
                existing = [acc for acc in existing if not isinstance(acc, dict) or acc.get("token") != token]
                existing.append({"token": token})
            else:
                return web.json_response({"status": "error", "error": "Invalid payload"})

            await _run_dashboard_io(request.app, _write_accounts_file, existing)

        bot_state.log(f"New account added: {data.get('uid') or 'Token'}", "success")

        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](data))

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()

        # Serialize file mutations to prevent concurrent add/delete requests
        # from overwriting each other's changes. Disk I/O itself is off-loop.
        async with request.app["accounts_lock"]:
            exists = await _run_dashboard_io(request.app, os.path.exists, ACCOUNTS_FILE)
            if exists:
                existing = await _run_dashboard_io(request.app, _read_accounts_file)
                existing = [
                    acc for acc in existing
                    if not isinstance(acc, dict)
                    or (str(acc.get("uid")) != uid and str(acc.get("token", ""))[:20] != uid)
                ]
                await _run_dashboard_io(request.app, _write_accounts_file, existing)

        if uid in bot_state.accounts:
            del bot_state.accounts[uid]

        if uid in bot_state.account_workers:
            bot_state.account_workers[uid].cancel()
            del bot_state.account_workers[uid]

        bot_state.log(f"Account {uid} removed", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_pause_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if uid in bot_state.accounts:
            acc = bot_state.accounts[uid]
            acc["is_paused"] = not acc.get("is_paused", False)
            if acc["is_paused"]:
                acc["status"] = "PAUSED"
            else:
                acc["status"] = "ONLINE"
            return web.json_response({"status": "ok", "is_paused": acc["is_paused"]})
        return web.json_response({"status": "error", "error": "Account not found"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_pause_all(request: web.Request) -> web.Response:
    try:
        all_paused = all(acc.get("is_paused", False) for acc in bot_state.accounts.values()) if bot_state.accounts else False
        new_state = not all_paused
        for acc in bot_state.accounts.values():
            acc["is_paused"] = new_state
            acc["status"] = "PAUSED" if new_state else "ONLINE"
        return web.json_response({"status": "ok", "all_paused": new_state})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if uid in bot_state.account_workers:
            bot_state.account_workers[uid].cancel()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_clear_logs(request: web.Request) -> web.Response:
    try:
        bot_state.logs.clear()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


# ==================== SERVER START ====================

async def start_web_dashboard(host: str = "0.0.0.0", port: int = 20333):
    app = web.Application(client_max_size=64 * 1024)
    # A small, configurable thread pool for independent dashboard file I/O.
    # This pool does not control or increase live-game match concurrency.
    app["dashboard_io_executor"] = ThreadPoolExecutor(
        max_workers=DASHBOARD_IO_WORKERS,
        thread_name_prefix="dashboard-io",
    )
    app.on_cleanup.append(_cleanup_dashboard_io_executor)
    print(f"[+] Dashboard I/O workers: {DASHBOARD_IO_WORKERS} (range 1-3)")
    # Preload the HTML once rather than doing blocking disk I/O per page request.
    app["index_html"] = _load_index_content()
    # Per-app lock prevents overlapping account file updates from losing data.
    app["accounts_lock"] = asyncio.Lock()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_get("/api/workers", handle_get_workers)
    app.router.add_post("/api/workers/set", handle_set_workers)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/pause", handle_pause_account)
    app.router.add_post("/api/account/pause_all", handle_pause_all)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/logs/clear", handle_clear_logs)

    # Ensure the folder exists before registering the static route, including
    # on a fresh install where templates/static/ did not exist yet.
    os.makedirs(STATIC_DIR, exist_ok=True)
    app.router.add_static("/static", STATIC_DIR)
    print(f"\033[92m[+] Static folder: {STATIC_DIR}\033[0m")
    if not os.path.exists(os.path.join(STATIC_DIR, "logo.png")):
        print(f"\033[93m[!] Put your logo.png inside templates/static/\033[0m")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard: http://localhost:{port}\033[0m")
    print(f"\033[92m[+] Logo path: {os.path.join(STATIC_DIR, 'logo.png')}\033[0m")