// Single source of truth for the backend URL, shared by both portals and the
// login page.
//
// Local dev  -> falls back to the uvicorn default on this machine.
// Deployed   -> uses RENDER_API_BASE below.
//
// !! Set RENDER_API_BASE to your Render backend service URL (the one that
//    serves FastAPI), e.g. "https://hr-onboarding-api.onrender.com".
//    It must be https, with NO trailing slash.
//
// Whatever origin this page is served from must also be listed in the
// backend's FRONTEND_ORIGINS environment variable, or the browser will block
// the response as a CORS failure (which also surfaces as "Failed to fetch").
const RENDER_API_BASE = "https://REPLACE-ME.onrender.com";

const LOCAL_API_BASE = "http://127.0.0.1:8000";

const _isLocal = ["localhost", "127.0.0.1", ""].includes(window.location.hostname);

const API_BASE = _isLocal ? LOCAL_API_BASE : RENDER_API_BASE;
