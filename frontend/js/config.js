// Single source of truth for the backend URL, shared by both portals and the
// login page. If you start uvicorn on a different port, change it HERE only.
// Must match the origin the backend allows in FRONTEND_ORIGINS (.env).
const API_BASE = "http://127.0.0.1:8000";
