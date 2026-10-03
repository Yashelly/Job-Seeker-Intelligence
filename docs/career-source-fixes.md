# Career source repairs

The collectors use source-specific listing data, public job APIs, and authoritative feeds to read complete lists and validate pagination. Cognizant uses its official XML feed; Vilniaus vandenys uses the embedded Teamdash careers feed. LinkedIn vacancy links on the ConnectPay and PAYSTRAX career pages are read from the employer page without scraping LinkedIn.

The repository does not contain the local registry database. To apply the verified public URL and ATS corrections to an existing database on the deployment host, run:

```powershell
python scripts/apply_career_source_fixes.py --db config/job_seeker.db
```

This owner maintenance command updates eleven known existing source configurations. It skips missing companies, preserves collection enablement and owner notes, and is idempotent. Configuration changes go through `CompanyRegistry.save`, which updates source revisions and invalidates stale source-check results. It does not import or create company records.

The live collection on 2026-10-03 used a 50,000-job budget and up to 250 pages per company. It discovered 14,677 unique vacancy URLs across 1,229 fetched pages. Of the 112 enabled sources, 110 completed and two failed: iDenfy and Revolut returned HTTP 403 to the collector. Both pages opened in the local browser; that observation does not verify access from the deployment server. The home server must run its own collection to confirm those two sources. Failed access remains failed rather than being recorded as an empty successful scan.

Cognizant returned 1,991 jobs, Vilniaus vandenys 14, ConnectPay two, and PAYSTRAX nine. Budget exhaustion marks unchecked companies partial, preventing results from a prior scan from appearing to be current successful checks.
