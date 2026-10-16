# Safe performance / reliability update

This package preserves `master.py` and its game/network protocol and match timing logic unchanged. It does **not** speed up live-game automation or increase parallel match/EXP farming.

## What changed

- Added a configurable 1-3 thread pool for independent dashboard filesystem I/O. Set it in `worker_config.json`, then restart the app. This pool is dashboard-only and does not increase live-game match concurrency.

- Dashboard HTML is loaded once at startup instead of performing blocking disk I/O on every page request.
- The static-files route is registered on fresh installs too.
- Account configuration writes use an atomic temporary-file replacement to reduce the chance of a truncated `accounts.json`.
- Dashboard account-file reads/writes are offloaded from the asyncio event loop, and an async lock protects concurrent add/delete requests from losing updates.
- Dashboard account-file reads handle missing or invalid JSON more safely.
- Dashboard requests have a small JSON request-size limit.
- The browser skips a stats poll if the previous poll is still running, and stats requests bypass stale browser cache.
- Independent dashboard requests can overlap while slow file I/O runs in a worker thread; account mutations remain serialized to preserve correctness.
- The uploaded account credentials, access tokens, cached tokens, and device identifiers were **not included**; the shipped data files are clean placeholders. Re-add accounts locally after rotating any exposed credentials.

## Worker setting

Edit `worker_config.json`:

```json
{
  "dashboard_io_workers": 2
}
```

Choose `1`, `2`, or `3`, then restart `master.py`. Higher values allow more independent dashboard file-I/O jobs to run at once; account add/delete mutations still use a lock to preserve correctness. This setting does **not** control concurrent game matches or promise faster in-game execution.

## Run

1. Install dependencies: `python -m pip install -r requirements.txt`
2. Add your own account data through the dashboard, or fill `accounts.json` locally.
3. Run: `python master.py`

The dashboard template is packaged at `templates/index.html`, which is the path expected by `dashboard_server.py`.

## Important

This is not a benchmark claim that the game's match cycle is faster. Match start/end timing and the game networking code were intentionally left unchanged to avoid logic regressions. The `MAX_CONCURRENT_MATCHES` constant in the current source is not referenced elsewhere; match tasks are already created with `asyncio.create_task`, so changing that constant alone would not create a reliable two-worker limit. For network I/O, asyncio concurrency is generally more appropriate than multiprocessing; adding processes around shared live session state can introduce races and timing errors.
