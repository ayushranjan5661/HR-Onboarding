// Single source of truth for the backend URL, shared by both portals and the
// login page. If you start uvicorn on a different port, change it HERE only.
// Must match the origin the backend allows in FRONTEND_ORIGINS (.env).
const API_BASE = "https://hr-onboarding-bazm.onrender.com";
