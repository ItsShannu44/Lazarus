# Lazarus dashboard (Next.js)

Live view of the agents: failed runs, agent timeline, proposed fix (diff, confidence,
tests), and Approve & merge / Reject.

```powershell
cd dashboard
npm install
copy .env.local.example .env.local   # macOS/Linux: cp
npm run dev                          # http://localhost:3000
```

Start the backend first (`uvicorn app.main:app --port 8000` in `backend/`).
`.env.local`:
- `NEXT_PUBLIC_API_URL` – backend URL (default http://localhost:8000; your Render URL when deployed)
- `NEXT_PUBLIC_DEFAULT_REPO` – prefilled repo for the "Run on latest failure" button

Deploy on Vercel: import the repo, set Root Directory to `dashboard`, add the two env vars.
