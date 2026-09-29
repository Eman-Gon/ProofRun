# Gauntlet static check — September 21, 2026

Checked `Eman-Gon/Gauntlet` at [`a31d316f9b60241e56420156bbfa3e4979ff541c`](https://github.com/Eman-Gon/Gauntlet/tree/a31d316f9b60241e56420156bbfa3e4979ff541c).

**Result: 55 source/manifest files inspected; 77 dependency declarations (64 distinct packages); no matches for Hackday Idea's three migration rules.**

This does not establish that the application works or that dependency upgrades are compatible. No repository code or test suites were run. The rules cover pandas uppercase hourly frequencies, Pydantic nullable fields without defaults, and legacy web-vitals imports. Lockfiles are not parsed, so declarations do not establish installed versions.

The dashboard's initial unauthenticated GitHub request was refused with a possible API rate-limit error. A separate fallback used the existing GitHub CLI connection for public repository metadata and its current default-branch SHA, then downloaded the pinned public archive without credentials. It passed the actual source through Hackday Idea's unchanged archive reader, limits, dependency parser, and rule functions. This fallback is separate from the failed dashboard entry.

Dependency manifests inspected:

- `backend/pyproject.toml`
- `backend/requirements.txt`
- `frontend/package.json`

Example declarations: Pydantic `==2.13.4`, FastAPI `==0.136.3`, OpenAI `==2.41.0` in backend requirements; Next.js `^14.2.30`, React `^18.3.1`, TypeScript `^5.5.3` in frontend/package.json. Most of these packages have no migration rules in this scanner.

Evidence:

- [Successful fallback scan, dependencies, file inventory and hashes](/Users/emanschool/secondlook/.commit-watch/gauntlet-test-20260922T025545Z/cli-metadata-scan.json)
- [Original dashboard error](/Users/emanschool/secondlook/.commit-watch/gauntlet-test-20260922T025545Z/scan.json)
- [Reproduction script](/Users/emanschool/secondlook/.commit-watch/gauntlet-test-20260922T025545Z/check_with_cli_metadata.py)

No application source or upstream repository was changed. JSON timestamps use UTC; the report date uses America/Los_Angeles.
