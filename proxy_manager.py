# -*- coding: utf-8 -*-
"""
MASTER OFFICIAL - Realtime Proxy Fallback Manager
Ensures proxy is ONLY used as a fallback during failed login attempts.
Automatically randomizes the SID per request to obtain fresh IPs.
Zero leakage to match sockets or routine traffic.
"""

import json
import os
import re
import time
import random
import secrets
import tempfile
import asyncio
from typing import Optional, Tuple, Dict, Any
import httpx

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_config.json")


def parse_proxy_string(raw: str) -> Dict[str, Any]:
    """Parse standard proxy formats:
    - host:port:user:pass
    - host:port
    - http://user:pass@host:port
    - socks5://user:pass@host:port
    """
    s = raw.strip()
    if not s:
        return {}

    protocol = "http"
    if "://" in s:
        proto, s = s.split("://", 1)
        protocol = proto.lower()

    if "@" in s:
        userpass, hostport = s.split("@", 1)
        user, pwd = userpass.split(":", 1) if ":" in userpass else (userpass, "")
        host, port = hostport.split(":", 1) if ":" in hostport else (hostport, "8080")
        try:
            port_num = int(port)
        except ValueError:
            port_num = 8080
        return {
            "host": host.strip(),
            "port": port_num,
            "user": user.strip(),
            "password": pwd.strip(),
            "protocol": protocol
        }

    parts = s.split(":")
    if len(parts) == 4:
        try:
            p = int(parts[1])
        except ValueError:
            p = 8080
        return {
            "host": parts[0].strip(),
            "port": p,
            "user": parts[2].strip(),
            "password": parts[3].strip(),
            "protocol": protocol
        }
    elif len(parts) == 2:
        try:
            p = int(parts[1])
        except ValueError:
            p = 8080
        return {
            "host": parts[0].strip(),
            "port": p,
            "user": "",
            "password": "",
            "protocol": protocol
        }

    return {"host": s, "port": 8080, "user": "", "password": "", "protocol": protocol}


class ProxyManager:
    def __init__(self):
        self.enabled: bool = False
        self.host: str = ""
        self.port: int = 8080
        self.user: str = ""
        self.password: str = ""
        self.protocol: str = "http"
        self.stats = {
            "total_fallback_attempts": 0,
            "total_fallback_successes": 0,
            "last_used_time": None,
            "last_assigned_ip": None,
            "last_used_sid": None,
            "last_test_result": None
        }
        self.load()

    def load(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.enabled = bool(data.get("enabled", False))
            self.host = str(data.get("host", "")).strip()
            self.port = int(data.get("port", 8080))
            self.user = str(data.get("user", "")).strip()
            self.password = str(data.get("password", "")).strip()
            self.protocol = str(data.get("protocol", "http")).strip().lower()
            if "stats" in data and isinstance(data["stats"], dict):
                self.stats.update(data["stats"])
        except Exception:
            pass

    def save(self):
        data = {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "protocol": self.protocol,
            "stats": self.stats
        }
        dir_name = os.path.dirname(CONFIG_PATH) or "."
        fd, temp_p = tempfile.mkstemp(prefix=".proxy_cfg-", suffix=".tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_p, CONFIG_PATH)
        finally:
            try:
                if os.path.exists(temp_p):
                    os.remove(temp_p)
            except OSError:
                pass

    def is_enabled(self) -> bool:
        """Check if proxy is enabled and has valid host and port."""
        return self.enabled and bool(self.host) and self.port > 0

    def set_enabled(self, state: bool) -> bool:
        self.enabled = bool(state)
        self.save()
        return self.enabled

    def update_config(self, host: str, port: int, user: str, password: str,
                      protocol: str = "http", enabled: Optional[bool] = None) -> Dict[str, Any]:
        self.host = str(host).strip()
        try:
            self.port = int(port)
        except Exception:
            self.port = 8080
        self.user = str(user).strip()
        self.password = str(password).strip()
        self.protocol = str(protocol).strip().lower() or "http"
        if enabled is not None:
            self.enabled = bool(enabled)
        self.save()
        return self.get_summary()

    def update_from_string(self, proxy_str: str, enabled: Optional[bool] = None) -> Dict[str, Any]:
        parsed = parse_proxy_string(proxy_str)
        if not parsed.get("host"):
            raise ValueError("Invalid proxy string: Host cannot be empty")
        return self.update_config(
            host=parsed.get("host", ""),
            port=parsed.get("port", 8080),
            user=parsed.get("user", ""),
            password=parsed.get("password", ""),
            protocol=parsed.get("protocol", "http"),
            enabled=enabled
        )

    def generate_proxy_url(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Generate a fresh proxy URL with a newly randomized Session ID (SID).
        Every single invocation produces a unique SID to guarantee fresh IP allocation.
        """
        if not self.is_enabled():
            return None, None

        # Generate fresh 8-digit random SID
        rand_sid = str(secrets.randbelow(90000000) + 10000000)

        # Inject randomized SID into username
        user = self.user
        if "_sid_" in user:
            user_with_sid = re.sub(r'_sid_[^_]+', f'_sid_{rand_sid}', user)
        elif "-sid-" in user:
            user_with_sid = re.sub(r'-sid-[^-]+', f'-sid_{rand_sid}', user)
        elif user:
            user_with_sid = f"{user}-sid-{rand_sid}"
        else:
            user_with_sid = ""

        if user_with_sid and self.password:
            url = f"{self.protocol}://{user_with_sid}:{self.password}@{self.host}:{self.port}"
        elif user_with_sid:
            url = f"{self.protocol}://{user_with_sid}@{self.host}:{self.port}"
        else:
            url = f"{self.protocol}://{self.host}:{self.port}"

        self.stats["total_fallback_attempts"] += 1
        self.stats["last_used_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.stats["last_used_sid"] = rand_sid

        return url, rand_sid

    def record_success(self, sid: str, assigned_ip: Optional[str] = None):
        self.stats["total_fallback_successes"] += 1
        self.stats["last_used_sid"] = sid
        if assigned_ip:
            self.stats["last_assigned_ip"] = assigned_ip
        self.save()

    async def test_connection(self) -> Dict[str, Any]:
        """Test proxy connectivity with a freshly generated random SID."""
        if not self.host or not self.port:
            return {"ok": False, "error": "Proxy host or port not configured"}

        url, sid = self.generate_proxy_url()
        if not url:
            # Temporary generate if not enabled
            rand_sid = str(secrets.randbelow(90000000) + 10000000)
            user = self.user
            if "_sid_" in user:
                u = re.sub(r'_sid_[^_]+', f'_sid_{rand_sid}', user)
            elif "-sid-" in user:
                u = re.sub(r'-sid-[^-]+', f'-sid_{rand_sid}', user)
            elif user:
                u = f"{user}-sid-{rand_sid}"
            else:
                u = ""
            if u and self.password:
                url = f"{self.protocol}://{u}:{self.password}@{self.host}:{self.port}"
            elif u:
                url = f"{self.protocol}://{u}@{self.host}:{self.port}"
            else:
                url = f"{self.protocol}://{self.host}:{self.port}"
            sid = rand_sid

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=url, verify=False, timeout=8.0) as test_client:
                resp = await test_client.get("http://api.ipify.org?format=json")
                latency_ms = int((time.perf_counter() - start) * 1000)
                if resp.status_code == 200:
                    data = resp.json()
                    ext_ip = data.get("ip", "Unknown")
                    self.stats["last_assigned_ip"] = ext_ip
                    self.stats["last_test_result"] = {
                        "ok": True,
                        "ip": ext_ip,
                        "sid": sid,
                        "latency_ms": latency_ms,
                        "time": time.strftime("%H:%M:%S")
                    }
                    self.save()
                    return {
                        "ok": True,
                        "ip": ext_ip,
                        "sid": sid,
                        "latency_ms": latency_ms,
                        "message": f"Connected successfully! External IP: {ext_ip} ({latency_ms}ms)"
                    }
                else:
                    err_msg = f"HTTP {resp.status_code}: {resp.text[:100]}"
                    self.stats["last_test_result"] = {"ok": False, "error": err_msg, "time": time.strftime("%H:%M:%S")}
                    self.save()
                    return {"ok": False, "error": err_msg}
        except Exception as e:
            err_msg = str(e)
            self.stats["last_test_result"] = {"ok": False, "error": err_msg, "time": time.strftime("%H:%M:%S")}
            self.save()
            return {"ok": False, "error": err_msg}

    def get_summary(self) -> Dict[str, Any]:
        pwd_display = f"{self.password[:3]}***{self.password[-2:]}" if len(self.password) > 5 else ("***" if self.password else "")
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": pwd_display,
            "raw_password": self.password,
            "protocol": self.protocol,
            "formatted_string": f"{self.host}:{self.port}:{self.user}:{self.password}" if self.host else "",
            "is_ready": bool(self.host and self.port),
            "stats": self.stats
        }


# Global Singleton Instance
proxy_mgr = ProxyManager()
