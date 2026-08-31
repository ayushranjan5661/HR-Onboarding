// Single source of truth for the backend URL, shared by both portals and the
// login page. If you start uvicorn on a different port, change it HERE only.
// Must match the origin the backend allows in FRONTEND_ORIGINS (.env).
// Local dev (Live Server / http.server) talks to local uvicorn; anything else
// (the deployed site) talks to Render.
const API_BASE = ["localhost", "127.0.0.1"].includes(location.hostname)
  ? "http://127.0.0.1:8000"
  : "https://hr-onboarding-bazm.onrender.com";
