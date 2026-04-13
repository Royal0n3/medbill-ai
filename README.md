# MedBill AI — Medical Billing Error Detection

Detects errors, upcoding, duplicate charges, and compliance issues in medical bills using Claude AI.

## Project Structure

```
medbill-ai/
├── app/
│   ├── __init__.py      # Flask app factory
│   └── routes.py        # Blueprint with HTTP routes
├── prompts/             # System prompt templates for Claude
├── uploads/             # Incoming bill PDFs (gitignored)
├── outputs/             # Analysis results (gitignored)
├── logs/                # Application logs (gitignored)
├── run.py               # Development entry point
├── requirements.txt
├── .env.example
└── README.md
```

## Setup

### 1. Prerequisites

- Python 3.11+
- pip

### 2. Clone and create a virtual environment

```bash
git clone <repo-url>
cd medbill-ai
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (from console.anthropic.com) |
| `FLASK_SECRET_KEY` | Random secret string used to sign sessions |
| `BREVO_API_KEY` | Brevo (Sendinblue) API key for email notifications |

### 5. Run the development server

```bash
python run.py
```

The API will be available at `http://localhost:5000`.

### 6. Verify the server is running

```bash
curl http://localhost:5000/health
# {"status": "ok"}
```

## API Overview

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |

More endpoints are added as the service is built out.

## Notes

- `uploads/`, `outputs/`, and `logs/` are created automatically at startup and should be added to `.gitignore`.
- `sqlite3` ships with Python's standard library — no separate install is required.
- The `MAX_CONTENT_LENGTH` is set to 16 MB to limit upload size; adjust in `app/__init__.py` if needed.
