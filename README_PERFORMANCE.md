# ⚡ Realtime Multi-Worker Concurrency Update

This update introduces an advanced **Real-Time Worker Thread Engine** allowing the bot to run multiple Free Fire Lone Wolf matches concurrently at the same time, multiplying EXP gain rate per hour, with 100% realtime dynamic controls directly from the Web Dashboard.

---

## 🚀 Key Features

### 1. Multi-Match Concurrency Engine (`game_workers`)
- The bot can now run multiple game matches simultaneously in parallel background tasks (Worker Threads).
- Match tasks run concurrently across independent UDP sockets while maintaining safe human-like intervals on the matchmaker gateway.
- As soon as any match finishes, the worker slot is instantly freed and filled with the next match immediately, keeping your desired concurrency at 100% capacity.

### 2. Live Web Dashboard Realtime Scaling (No Restart Needed)
- Adjust worker threads live from the Web Dashboard (`http://localhost:20030`).
- **Interactive Stepper (`[-]` & `[+]`)**: Scale up or down with a single click.
- **Quick Scale Presets**: `1x Normal`, `2x Fast`, `3x Turbo`, `5x Ultra`, `8x Overdrive`, `10x Extreme`.
- Changes take effect instantly without restarting the bot or terminating ongoing matches.
- All changes are automatically and atomically saved to `worker_config.json`.

### 3. Rock-Solid Reliability & Zero Bug Guarantee
- **Slot Capacity Guard**: Prevents runaway match spamming or gateway rate limit violations.
- **Graceful Dynamic Downscaling**: If workers are decreased, active games finish cleanly with full EXP credit before dropping down to the new limit.
- **Event-Driven Wakeup**: Uses `asyncio.Event` and `asyncio.wait` instead of CPU-wasting loops.
- **Account Pause Awareness**: Accurately suspends matchmaking when an account is paused from the dashboard.

---

## ⚙️ Configuration (`worker_config.json`)

```json
{
  "game_workers": 3,
  "dashboard_io_workers": 2
}
```

- `game_workers`: Number of simultaneous matches to run concurrently (range: `1` to `20`). Default: `3`.
- `dashboard_io_workers`: Thread pool size for dashboard file operations (range: `1` to `3`). Default: `2`.

---

## 🌐 API Endpoints

- `GET /api/workers` - Retrieve current worker threads and active games count.
- `POST /api/workers/set` - Update worker threads in real time (`{"workers": 5}`).
- `GET /api/stats` - System stats including live worker state and active matches.

---

## 🎮 How to Run

1. Ensure requirements are installed:
   ```bash
   python -m pip install -r requirements.txt
   ```
2. Start the bot:
   ```bash
   python master.py
   ```
3. Open Dashboard in your browser:
   ```
   http://localhost:20030
   ```
4. Adjust worker threads in realtime using the **WORKER THREADS ENGINE** control panel!
