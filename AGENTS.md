# OpenTale development contract

This directory is the sole Git repository. Do not change anything outside it.

- Never read, print, commit, upload or return `.env.local`, environment dumps, credentials or raw provider error bodies. Secret-bearing configuration is loaded only by the backend. Fixtures use dummy values. Logs are sanitized and contain no Authorization headers or reasoning content.
- Do not use paid API calls without the user's provided configuration and explicit test budget. Offline verification is the default.
- Runtime schemas and APIs are defined in `docs/CONTRACT.md`. Coordinate schema changes before editing another owner's files.
- Root owns `backend/opentale/{domain,kernel,storage,runner,api,presets}.py`, runtime tests, project integration.
- Provider agent owns `backend/opentale/providers/`, `backend/opentale/config.py`, and provider/config tests.
- UI agent owns `frontend/` and its tests.
- Verification agent owns `scripts/`, `docs/` except CONTRACT, README and CHANGELOG. Inspect implementation before documenting claims.
- Keep HTTP/LLM concerns outside the deterministic kernel. Store versioned snapshots and executable events. Test batch order independence, stale proposals, recovery, real dependency changes and secrecy.
- Use third-person narrative. Initial profiles are seeds, not immutable repeated instructions. Preserve scoped, conflicting bindings; never silently choose a universal fiction history.
- Actor views must not include technical creator provenance, API settings, hidden reasoning or debugging references. Public prose and diagnostic traces are separate projections.
- Offline fixtures demonstrate mechanics, not proven LLM literary/semantic performance. Document limits honestly.
