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
from typing import Dict, List, Any, Optional, Set
from aiohttp import web
from proxy_manager import proxy_mgr, parse_proxy_string

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
            return max(1, value)
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
        self.error_accounts: Dict[str, Dict[str, Any]] = {}
        self.account_id_map: Dict[str, Set[str]] = {}
        self.deleted_uids: Set[str] = set()
        self.account_stop_events: Dict[str, asyncio.Event] = {}
        self.active_writers: Dict[str, List[Any]] = {}
        self.game_workers: int = _load_game_worker_count()
        self.worker_event: asyncio.Event = asyncio.Event()

    def link_account_ids(self, *ids):
        valid = set(str(i).strip() for i in ids if i and str(i).strip())
        if not valid:
            return
        merged = set(valid)
        for i in valid:
            if i in self.account_id_map:
                merged.update(self.account_id_map[i])
        for i in merged:
            self.account_id_map[i] = merged
        # Propagate auth_uid to registered account objects
        for acc_id, acc in self.accounts.items():
            if acc_id in merged:
                for candidate in merged:
                    if candidate != acc_id and candidate.isdigit() and len(candidate) <= 12:
                        acc["auth_uid"] = candidate

    def get_all_linked_ids(self, uid: str) -> Set[str]:
        uid_str = str(uid).strip()
        if not uid_str:
            return set()
        group = set(self.account_id_map.get(uid_str, {uid_str}))
        group.add(uid_str)

        # Search registered accounts
        for acc_id, acc in list(self.accounts.items()):
            a_uid = str(acc.get("uid", "")).strip()
            a_auth = str(acc.get("auth_uid", "")).strip()
            a_acc = str(acc.get("account_id", "")).strip()
            a_tok = str(acc.get("token", "")).strip()
            candidates = {acc_id, a_uid, a_auth, a_acc, a_tok, a_tok[:20], a_tok[:10]} - {""}
            if uid_str in candidates or any(c in group for c in candidates):
                group.update(candidates)

        # Search credentials
        for cred_id, cred in list(self.account_credentials.items()):
            c_uid = str(cred.get("auth_uid", "")).strip()
            c_acc = str(cred.get("account_id", "")).strip()
            c_tok = str(cred.get("token", "")).strip()
            candidates = {cred_id, c_uid, c_acc, c_tok, c_tok[:20], c_tok[:10]} - {""}
            if uid_str in candidates or any(c in group for c in candidates):
                group.update(candidates)

        # Cross-reference token_cache.json on disk to bridge auth_uid and in-game account_id
        try:
            if os.path.exists("token_cache.json"):
                with open("token_cache.json", "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                if isinstance(tdata, dict):
                    for k, v in tdata.items():
                        if isinstance(v, dict):
                            k_str = str(k).strip()
                            v_acc = str(v.get("account_id", "")).strip()
                            v_auth = str(v.get("auth_uid", "")).strip()
                            v_tok = str(v.get("access_token", "")).strip()
                            entry_ids = {k_str, v_acc, v_auth, v_tok, v_tok[:20], v_tok[:10]} - {""}
                            if uid_str in entry_ids or any(e in group for e in entry_ids):
                                group.update(entry_ids)
        except Exception:
            pass

        return group

    def is_deleted(self, uid: str) -> bool:
        uid_str = str(uid).strip()
        if not uid_str:
            return False
        if uid_str in self.deleted_uids:
            return True
        for linked in self.get_all_linked_ids(uid_str):
            if linked in self.deleted_uids:
                return True
        return False

    def revive_account(self, uid: str):
        uid_str = str(uid).strip()
        if not uid_str:
            return
        all_ids = self.get_all_linked_ids(uid_str)
        all_ids.add(uid_str)
        for i in all_ids:
            self.deleted_uids.discard(i)
            self.account_stop_events.pop(i, None)

    def register_stop_event(self, uid: str, event: asyncio.Event):
        uid_str = str(uid).strip()
        self.account_stop_events[uid_str] = event
        for linked in self.get_all_linked_ids(uid_str):
            self.account_stop_events[linked] = event

    def register_writer(self, uid: str, writer: Any):
        uid_str = str(uid).strip()
        if uid_str not in self.active_writers:
            self.active_writers[uid_str] = []
        self.active_writers[uid_str].append(writer)

    def unregister_writer(self, uid: str, writer: Any):
        uid_str = str(uid).strip()
        if uid_str in self.active_writers:
            try:
                self.active_writers[uid_str].remove(writer)
            except ValueError:
                pass

    def stop_and_purge_account(self, uid: str) -> Set[str]:
        uid_str = str(uid).strip()
        all_ids = self.get_all_linked_ids(uid_str)
        all_ids.add(uid_str)

        # 1. Mark as permanently deleted
        self.deleted_uids.update(all_ids)

        # 2. Trigger stop events immediately
        for i in all_ids:
            if i in self.account_stop_events:
                try:
                    self.account_stop_events[i].set()
                except Exception:
                    pass

        # 3. Close any active network writers/sockets immediately
        for i in all_ids:
            writers = self.active_writers.pop(i, [])
            for w in writers:
                try:
                    w.close()
                except Exception:
                    pass

        # 4. Cancel all worker tasks registered under ANY linked id or prefix
        for i in all_ids:
            task = self.account_workers.pop(i, None)
            if task and not task.done():
                task.cancel()

        for w_key, task in list(self.account_workers.items()):
            if w_key in all_ids or any(w_key.startswith(i) or i.startswith(w_key) for i in all_ids if len(i) >= 6):
                if not task.done():
                    task.cancel()
                self.account_workers.pop(w_key, None)

        # 5. Remove from runtime dictionaries
        for i in all_ids:
            self.accounts.pop(i, None)
            self.error_accounts.pop(i, None)
            self.account_credentials.pop(i, None)
            self.account_stop_events.pop(i, None)

        self.recalc_totals()
        return all_ids

    def register_error_account(self, uid: str, reason: str, auth_type: str = "guest", details: Optional[Dict[str, Any]] = None):
        uid_str = str(uid).strip()
        if not uid_str:
            return
        now_str = time.strftime("%H:%M:%S")
        existing = self.error_accounts.get(uid_str)
        err_count = (existing.get("error_count", 0) + 1) if existing else 1
        entry = {
            "uid": uid_str,
            "reason": str(reason),
            "auth_type": auth_type,
            "error_count": err_count,
            "first_seen": existing.get("first_seen", now_str) if existing else now_str,
            "last_seen": now_str,
            "details": details or {}
        }
        self.error_accounts[uid_str] = entry
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = "ERROR"
            self.accounts[uid_str]["error_reason"] = str(reason)
            self.accounts[uid_str]["last_updated"] = now_str
        self.log(f"⚠️ [Error Node Quarantine] UID {uid_str}: {reason}", "error", uid_str)

    def clear_error_account(self, uid: str):
        uid_str = str(uid).strip()
        if uid_str in self.error_accounts:
            del self.error_accounts[uid_str]
        if uid_str in self.accounts:
            self.accounts[uid_str].pop("error_reason", None)

    def set_game_workers(self, count: int) -> int:
        self.game_workers = max(1, int(count))
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
    err_list = list(bot_state.error_accounts.values())
    err_list.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "total_active_matches": bot_state.get_total_active_matches(),
        "game_workers": getattr(bot_state, "game_workers", 3),
        "exp_per_hour": bot_state.get_exp_per_hour(),
        "accounts": accounts_data,
        "error_accounts": err_list,
        "total_error_accounts": len(err_list),
        "logs": bot_state.logs[-60:],
        "uptime": int(time.time() - bot_state.start_time),
        "proxy": proxy_mgr.get_summary()
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


async def handle_get_proxy(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "proxy": proxy_mgr.get_summary()
    })


async def handle_update_proxy(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        proxy_str = data.get("proxy_str")
        enabled = data.get("enabled")
        if proxy_str:
            summary = proxy_mgr.update_from_string(proxy_str, enabled=enabled)
        else:
            host = data.get("host", "")
            port = data.get("port", 8080)
            user = data.get("user", "")
            password = data.get("password", "")
            protocol = data.get("protocol", "http")
            summary = proxy_mgr.update_config(host, port, user, password, protocol, enabled=enabled)
        
        status_txt = "ENABLED (Login Fallback)" if summary.get("enabled") else "DISABLED (Direct Only)"
        bot_state.log(f"🛡️ Proxy configuration updated: {summary.get('host')}:{summary.get('port')} [{status_txt}]", "info")
        return web.json_response({"status": "ok", "proxy": summary})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_toggle_proxy(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        enabled = bool(data.get("enabled", False))
        res = proxy_mgr.set_enabled(enabled)
        status_txt = "ON (Fallback Active)" if res else "OFF (Direct Only - 0 Proxy Usage)"
        bot_state.log(f"⚡ Proxy Guard switched {status_txt}", "success" if res else "warning")
        return web.json_response({"status": "ok", "enabled": res, "proxy": proxy_mgr.get_summary()})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_test_proxy(request: web.Request) -> web.Response:
    try:
        data = await request.json() if request.can_read_body else {}
        test_str = data.get("proxy_str")
        if test_str:
            # Temporarily apply or parse string for test
            parsed = parse_proxy_string(test_str)
            if parsed.get("host"):
                proxy_mgr.update_from_string(test_str)
        result = await proxy_mgr.test_connection()
        if result.get("ok"):
            bot_state.log(f"✅ Proxy test passed! IP: {result.get('ip')} ({result.get('latency_ms')}ms)", "success")
        else:
            bot_state.log(f"❌ Proxy test failed: {result.get('error')}", "error")
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})


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
                bot_state.revive_account(uid)
                existing = [acc for acc in existing if not isinstance(acc, dict) or str(acc.get("uid")) != uid]
                existing.append({"uid": uid, "password": pwd})
            elif "token" in data:
                token = str(data["token"]).strip()
                if not token:
                    return web.json_response({"status": "error", "error": "Token required"})
                bot_state.revive_account(token)
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
        uid = str(data.get("uid", "")).strip()
        auth_uid = str(data.get("auth_uid", "")).strip()

        all_ids = set()
        if uid:
            all_ids.update(bot_state.stop_and_purge_account(uid))
        if auth_uid:
            all_ids.update(bot_state.stop_and_purge_account(auth_uid))
        if not all_ids and uid:
            all_ids.add(uid)

        # Invalidate credential cache for all linked identifiers
        if "on_cache_invalidate" in bot_state.refresh_callbacks:
            for i in all_ids:
                try:
                    bot_state.refresh_callbacks["on_cache_invalidate"](i)
                except Exception:
                    pass

        # Clean up disk cache (token_cache.json)
        try:
            if os.path.exists("token_cache.json"):
                with open("token_cache.json", "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                if isinstance(tdata, dict):
                    mod = False
                    for tk in list(tdata.keys()):
                        entry = tdata.get(tk)
                        match = False
                        if tk in all_ids:
                            match = True
                        elif isinstance(entry, dict):
                            e_acc = str(entry.get("account_id", "")).strip()
                            e_auth = str(entry.get("auth_uid", "")).strip()
                            if e_acc in all_ids or e_auth in all_ids:
                                match = True
                        if match:
                            del tdata[tk]
                            mod = True
                    if mod:
                        with open("token_cache.json", "w", encoding="utf-8") as f:
                            json.dump(tdata, f, indent=2)
        except Exception as te:
            bot_state.log(f"Cache file purge note: {te}", "info")

        # Serialize removal from accounts.json across all aliases (auth_uid, acc_id, token)
        async with request.app["accounts_lock"]:
            exists = await _run_dashboard_io(request.app, os.path.exists, ACCOUNTS_FILE)
            if exists:
                existing = await _run_dashboard_io(request.app, _read_accounts_file)
                new_list = []
                for acc in existing:
                    if not isinstance(acc, dict):
                        continue
                    a_uid = str(acc.get("uid", "")).strip()
                    a_tok = str(acc.get("token", "")).strip()
                    # Check if entry matches any linked alias or original input
                    if (a_uid and (a_uid in all_ids or a_uid == uid or a_uid == auth_uid)) or \
                       (a_tok and (a_tok in all_ids or a_tok[:20] in all_ids or a_tok[:10] in all_ids or a_tok == uid or a_tok == auth_uid)):
                        continue
                    new_list.append(acc)
                await _run_dashboard_io(request.app, _write_accounts_file, new_list)

        bot_state.log(f"Account {uid} permanently killed & removed from backend (1000% stopped)", "warning", uid)
        return web.json_response({"status": "ok", "deleted_uid": uid, "all_ids": list(all_ids)})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_error_accounts(request: web.Request) -> web.Response:
    """Permanently delete all quarantined error accounts from accounts.json, runtime state, and workers."""
    try:
        error_uids = list(bot_state.error_accounts.keys())
        if not error_uids:
            return web.json_response({"status": "ok", "deleted_count": 0, "message": "No error accounts to delete"})

        all_purged_ids = set()
        for e_uid in error_uids:
            purged = bot_state.stop_and_purge_account(e_uid)
            all_purged_ids.update(purged)

        if "on_cache_invalidate" in bot_state.refresh_callbacks:
            for i in all_purged_ids:
                try:
                    bot_state.refresh_callbacks["on_cache_invalidate"](i)
                except Exception:
                    pass

        # Clean disk cache
        try:
            if os.path.exists("token_cache.json"):
                with open("token_cache.json", "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                if isinstance(tdata, dict):
                    mod = False
                    for tk in list(tdata.keys()):
                        entry = tdata.get(tk)
                        match = False
                        if tk in all_purged_ids:
                            match = True
                        elif isinstance(entry, dict):
                            e_acc = str(entry.get("account_id", "")).strip()
                            e_auth = str(entry.get("auth_uid", "")).strip()
                            if e_acc in all_purged_ids or e_auth in all_purged_ids:
                                match = True
                        if match:
                            del tdata[tk]
                            mod = True
                    if mod:
                        with open("token_cache.json", "w", encoding="utf-8") as f:
                            json.dump(tdata, f, indent=2)
        except Exception:
            pass

        async with request.app["accounts_lock"]:
            exists = await _run_dashboard_io(request.app, os.path.exists, ACCOUNTS_FILE)
            if exists:
                existing = await _run_dashboard_io(request.app, _read_accounts_file)
                new_list = []
                for acc in existing:
                    if not isinstance(acc, dict):
                        continue
                    a_uid = str(acc.get("uid", "")).strip()
                    a_tok = str(acc.get("token", "")).strip()
                    if (a_uid and a_uid in all_purged_ids) or (a_tok and (a_tok in all_purged_ids or a_tok[:20] in all_purged_ids or a_tok[:10] in all_purged_ids)):
                        continue
                    new_list.append(acc)
                await _run_dashboard_io(request.app, _write_accounts_file, new_list)

        deleted_count = len(error_uids)
        bot_state.error_accounts.clear()
        bot_state.log(f"🗑️ Realtime Deletion: {deleted_count} error accounts purged from disk and memory (1000% stopped)", "success")
        return web.json_response({"status": "ok", "deleted_count": deleted_count})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_clear_error_accounts(request: web.Request) -> web.Response:
    """Clear error status without removing from accounts.json (retry attempt)."""
    try:
        count = len(bot_state.error_accounts)
        bot_state.error_accounts.clear()
        for acc in bot_state.accounts.values():
            if acc.get("status") == "ERROR":
                acc["status"] = "ONLINE"
                acc.pop("error_reason", None)
        return web.json_response({"status": "ok", "cleared_count": count})
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
    app.router.add_get("/api/proxy", handle_get_proxy)
    app.router.add_post("/api/proxy/update", handle_update_proxy)
    app.router.add_post("/api/proxy/toggle", handle_toggle_proxy)
    app.router.add_post("/api/proxy/test", handle_test_proxy)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/delete_errors", handle_delete_error_accounts)
    app.router.add_post("/api/account/clear_errors", handle_clear_error_accounts)
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