# HydraBytes — Vercel-ready project

This folder contains the HydraBytes Flask backend and dashboard, prepared for GitHub and Vercel.

## Files
- `app.py` — Flask backend and API routes
- `index.html` — dashboard frontend
- `requirements.txt` — Python dependencies
- `vercel.json` — routes requests to the Flask application
- `.gitignore` — excludes local environments and secrets from Git

## Run locally on macOS
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```
Open http://127.0.0.1:5001

## Deploy
1. Create a new GitHub repository named `HydraBytes`.
2. Upload/push the contents of this folder to the repository root (not an extra nested folder).
3. In Vercel, choose Add New → Project, import the GitHub repository, and deploy.
4. Test `/`, `/api/health`, and `/api/regions` on the deployed domain.

## Important project limitations
- Weather values come from Open-Meteo for six representative city points; they are not state-wide averages or direct station observations.
- The rainfall thresholds are illustrative screening indicators, not validated flood predictions or official warnings.
- `/api/predict` is a manual-input rule-based demonstration, not a trained ML model.
- River gauge levels are unavailable because no verified official gauge feed is configured.
- The in-memory cache is temporary and may not be shared across Vercel serverless instances.
- Follow official IMD, CWC and local disaster-management advisories for real-world decisions.
# MDC-project
