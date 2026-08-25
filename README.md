# FreeCord

A self-hosted Discord server backup and member restore platform with zero member caps and no monthly subscriptions.

Other backup bots charge $20–$40/month or cut you off at 25 members while keeping your tokens on their private servers. If their service goes down, you lose your community. FreeCord runs on your own machine or VPS, stores encrypted tokens locally in your own SQLite database, and gives you full control.

| Feature | Typical Hosted Bots | FreeCord |
| :--- | :--- | :--- |
| **Pricing** | $20–$40 / month | Free & open source |
| **Member Limit** | 25–100 free (paywalled higher) | Unlimited |
| **Token Storage** | Third-party cloud servers | Encrypted on your machine (AES) |
| **Server Backups** | Paid add-on | Full structure, roles, emojis |
| **Network Ingress** | Paid dedicated IP | Built-in Cloudflare, Ngrok, Custom |

---

## Quickstart

### 1. Clone and Run

**Windows:**
```cmd
git clone https://github.com/zyrexdz/freecord.git
cd freecord
run.bat
```

**Linux / macOS:**
```bash
git clone https://github.com/zyrexdz/freecord.git
cd freecord
chmod +x run.sh
./run.sh
```

The script automatically sets up a virtual environment, installs dependencies, and launches FreeCord.

### 2. Open the Dashboard

Navigate to `http://localhost:8000` in your browser.

- **Default Username:** `admin`
- **Default Password:** `admin123` *(change this under Settings)*

---

## Setting Up Your Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. Under **Bot**:
   - Reset and copy your **Bot Token**.
   - Enable **Server Members Intent** and **Message Content Intent**.
   - Make sure **Requires OAuth2 Code Grant** is toggled off.
3. Under **OAuth2**:
   - Copy your **Client Secret**.
   - Add your redirect URI: `http://localhost:8000/api/oauth/callback` (or your tunnel URL).
4. Head to `http://localhost:8000/bots/new`, paste your credentials, and save.

---

## Commands & Usage

FreeCord registers slash commands on connected bots automatically:

| Slash Command | Description |
| :--- | :--- |
| `/verify_setup` | Sets up the verification role and logging channel for the server |
| `/verify_send` | Posts an embed with a "Verify" button into a channel |
| `/backup_create` | Creates a full server backup (channels, roles, permissions, emojis) |
| `/backup_restore` | Restores a saved backup snapshot into the server |
| `/pull_start` | Starts restoring verified members into the server |
| `/pull_status` | Shows status of recent member restoration jobs |
| `/blacklist_add` | Blocks a specific User ID or IP from verifying |
| `/credits` | Shows project info and repository links |

---

## Configuration

Settings can be customized via `.env`:

```bash
cp .env.example .env
```

| Variable | Description | Default |
| :--- | :--- | :--- |
| `HOST` | Bind address for the web server | `0.0.0.0` |
| `PORT` | Web port | `8000` |
| `BASE_URL` | Public URL override (e.g. `https://verify.example.com`) | *Auto-detected* |
| `DATABASE_URL` | SQLite database path | `sqlite+aiosqlite:///data/freecord.db` |
| `ENCRYPTION_KEY` | Fernet key for encrypting stored bot & OAuth tokens | *Auto-generated* |
| `JWT_SECRET` | Secret key for dashboard session cookies | *Auto-generated* |
| `ADMIN_USERNAME` | Initial admin username | `admin` |
| `ADMIN_PASSWORD` | Initial admin password | `admin123` |

---

## Running on a VPS (systemd)

To keep FreeCord running in the background on Linux, create `/etc/systemd/system/freecord.service`:

```ini
[Unit]
Description=FreeCord Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/freecord
ExecStart=/opt/freecord/.venv/bin/python main.py --no-prompt
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now freecord
```

---

## Project Structure

```text
freecord/
├── core/
│   ├── config.py               # Settings and network detection
│   ├── security.py             # AES encryption, bcrypt, JWT, and IP lookups
│   ├── network_detector.py     # IP, CGNAT, and VPS detection
│   ├── network_bootstrapper.py # Cloudflare, Ngrok, and Localtunnel runners
│   ├── proxy_manager.py        # Proxy scraper and health checker
│   └── access_control.py       # Bot permissions and collaborator checks
├── database/
│   ├── models.py               # SQLAlchemy database schema
│   └── session.py              # Async engine and migrations
├── services/
│   ├── backup_service.py       # Guild backup snapshot & restore engine
│   ├── bot_manager.py          # Discord bot lifecycle and slash commands
│   ├── migration_engine.py     # Member restoration worker with rate limit handling
│   ├── migration_service.py    # Restoration queue facade
│   ├── security_service.py     # Firewall, CAPTCHA, and VPN validation
│   └── webhook_service.py      # Embed notifications and milestone logging
├── web/
│   ├── app.py                  # FastAPI application setup
│   └── routes/                 # Dashboard, bot, OAuth, and backup routes
├── templates/                  # Dashboard HTML templates
├── static/                     # CSS styles and dashboard assets
├── main.py                     # Entry point & CLI launcher
└── requirements.txt
```

---

## Contributing

Contributions and PRs are welcome! If you run into a bug or have a feature idea, feel free to open an issue or submit a pull request.

If this saved you time, leave a ⭐ to help others find it.
