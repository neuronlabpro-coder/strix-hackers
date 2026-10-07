# Security Penetration Test Report

**Generated:** 2026-10-07 09:07:43 UTC

# Executive Summary

An external black-box security assessment of **mindguard.site** (the marketing site for the Mindguard AI-security platform by Shynt AI Consulting) found **no exploitable vulnerabilities**. The overall risk posture for the in-scope property is **low**.

**What the target is:** a fully static, brochure-style single-page application (Vite + React 18.3.1) served through Hostinger's CDN. It has no backend, no API, no authentication, no user accounts, no forms, and stores no user data — the only client-side storage is a visitor's own language preference.

**Key results**
- No injection surface of any kind: the application performs no server-side processing, and the client-side audit (dynamic and static, covering every HTML/JS sink class) confirmed that no attacker-controllable input reaches the DOM unescaped.
- No subdomain takeover risk: the entire subdomain inventory is four names; the two non-resolving `redteam.*` labels are fully deleted DNS records (nothing for an attacker to claim), and `www` points to Hostinger's own CDN infrastructure.
- Transport security is healthy: TLS 1.2/1.3 with grade-A cipher suites, a certificate matching exactly the live hostnames, and enforced HTTPS redirects.
- Hardening-only observations: several standard security headers (HSTS, `X-Content-Type-Options`, `Referrer-Policy`, etc.) are absent; these carry no demonstrable exploit path for a static site with no cookies or authentication and are listed as best-practice improvements.

**Business impact:** no exposure of customer data, no service-interruption risk, and no path to brand-impersonation via DNS was identified on the in-scope property.

**Adjacent surface worth noting:** the operator's sibling domain, `mindguardredteam.com`, hosts the only real server-side code in the estate — an unauthenticated PHP contact-form handler. It sits outside this engagement's authorized scope and was not payload-tested; it is flagged as the highest-value target for a follow-up engagement covering that domain.

**Remediation theme:** no urgent remediation is required for `mindguard.site`. Recommendations focus on cheap defense-in-depth header additions now, and on extending a future assessment to the sibling property where the estate's actual dynamic attack surface lives.

# Methodology

Conducted per the **OWASP Web Security Testing Guide (WSTG)**, adapted to a fully static target, with a three-stage multi-agent pipeline: reconnaissance and attack-surface mapping, parallel specialized testing, and coverage reconciliation.

**Engagement type:** Black-box external assessment (no source code or credentials provided).

**Scope:** `https://mindguard.site` and `*.mindguard.site`. The sibling domain `mindguardredteam.com` (linked from the target) was mapped for context only during reconnaissance and was **not** actively tested, as it falls outside the authorized target domain.

**Activities:**
- **Reconnaissance:** live-service fingerprinting (HTTP probing, technology and CDN/WAF identification), bounded crawling, common-list content discovery, JavaScript bundle analysis for secrets, hidden routes, and API references; passive DNS and certificate-transparency enumeration; port scanning (top 100).
- **Client-side security testing:** full static sink audit of the React bundle (HTML/JS injection sinks, dynamic script injection, postMessage handlers, hash/parameter-driven rendering) plus live browser-based payload validation of every reachable sink class.
- **DNS and infrastructure testing:** subdomain takeover assessment (authoritative NS queries, resolver triangulation, CT log history), TLS protocol/cipher enumeration, and HTTP response-header posture review.
- **Validation discipline:** every candidate closed as confirmed, ruled out (with the specific control named and located), or left as an explicit follow-up item — no candidate was silently dropped. All testing was low-volume and non-destructive; no other users' data was accessed.

# Technical Analysis

No exploitable vulnerabilities were identified in the authorized scope (`mindguard.site` and `*.mindguard.site`). All 17 in-scope coverage entries closed with a negative outcome. **Severity model:** each candidate was closed per the confirmed/rule-out/proof-gap discipline; no severity was assigned above the informational hardening notes because no PoC-backed issue exists.

**What was assessed and how it closed**

1. **Application layer** — Static Vite + React 18.3.1 SPA, single 209 KB bundle. Every HTML/JS injection sink class was audited statically (all 13 `dangerouslySetInnerHTML` + 5 `innerHTML` occurrences are React library internals; zero app-level sinks, zero `eval`/`new Function`/`document.write`) and dynamically (encoded and raw hash payloads, `?lang=` parameter injection, poisoned `localStorage` language value, `postMessage` probes — all inert). The only URL parameter (`lang`) is strictly allowlisted to `es`/`en` before entering React state. Ruled out: DOM-XSS, open redirect (all link targets are hardcoded constants), postMessage-based attacks, service-worker persistence, and client-side secret exposure (single allowlisted `localStorage` key, no cookies).

2. **DNS / subdomain takeover** — Complete `*.mindguard.site` inventory is 4 names. `redteam.mindguard.site` + `www` appear in passive DNS but are **fully deleted records**: authoritative Hostinger nameservers return NXDOMAIN with zero records (no A/CNAME/NS), verified from three public resolvers and direct NS queries. A nonexistent label is not claimable, so takeover is ruled out — the passive-DNS hits were stale artifacts. `www.mindguard.site` CNAMEs to Hostinger's own CDN (`cdn.hstgr.net`), which is not attacker-claimable.

3. **Network / transport** — Only ports 80/443 open; HTTP→HTTPS redirect enforced; TLS 1.2/1.3 only with grade-A cipher suites (ECDHE/DHE AES-GCM, ChaCha20-Poly1305); certificate SANs exactly match the two live hostnames. No exposed services, no wildcard exposure.

4. **Informational observations (hardening only, no report filed):** response headers lack HSTS, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, and `Permissions-Policy`; CSP is limited to `upgrade-insecure-requests`. On a static brochure site with no cookies, no authentication, and no PII, these carry no demonstrated exploit path and are defense-in-depth items. Hostinger's edge serves JS-challenge pages on sensitive-looking paths (403) and branded "not found" pages via 502 — reconnaissance artifacts documented and accounted for, not origin behavior.

**Adjacent surfaces outside this engagement's scope:** the sibling property `mindguardredteam.com` hosts the operator's only server-side endpoint (`POST /contact.php`, unauthenticated PHP mailer with `name/email/company/service/message` + honeypot fields). Recon mapped it but no payload testing was performed since it is a separate domain from the authorized target. It is the single highest-value dynamic surface in the operator's estate and is recorded as an open coverage row (`needs_follow_up`) for a future engagement covering that domain. Related: `shyntai.io` (operator main site) and a Cal.com booking embed (`shyntai/1hora`).

**Systemic themes:** the target is a low-complexity, static-only property — the strongest possible posture for the class of bugs normally driving web compromise (injection, authz, IDOR, SSRF all structurally impossible with no backend). Residual risk concentrates in (a) the sibling PHP backend outside this scope, and (b) DNS hygiene: a `redteam.mindguard.site` label recently existed (CT cert issued 2026-09-22) and was deleted — if recreated against a third-party service in the future, ownership verification must be enabled to avoid a genuine takeover exposure.

# Recommendations

**Immediate**

1. No remediation required — no exploitable vulnerability exists within the authorized scope.

**Short-term**

2. Add security headers to `mindguard.site` responses (at the CDN/Hostinger layer): `Strict-Transport-Security: max-age=31536000; includeSubDomains`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and a content-security-policy with `script-src 'self' https://*.cal.com https://fonts.googleapis.com` and `default-src 'self'`. Low effort, closes the defense-in-depth gap identified in the transport/header posture review.
3. Purge the deleted `redteam.mindguard.site` DNS labels from any operator documentation and DNS tooling to avoid re-creating them accidentally. If a `redteam` subdomain is ever re-created and pointed at a third-party hosting service, enable that service's ownership/domain-verification controls first (verified claimability of third-party platforms is the only takeover vector this estate could plausibly develop).

**Medium-term**

4. Extend a future authorized engagement to the sibling property **`mindguardredteam.com`** — specifically `POST /contact.php`, the operator's only server-side code. Recommended test areas, in order: email header injection (CRLF in `name`/`email`/`message` feeding `mail()` additional-headers), spam-relay abuse (absence of rate limiting), verbose error disclosure, and server-side validation gaps. It is the single highest-value target in the operator's estate and was intentionally not tested here because it falls outside the authorized domain.
5. If the contact handler is ever refactored, enforce server-side input validation independent of the client, strip/encode CR and LF bytes from every field passed into mail headers, and rate-limit by IP. Keeping the honeypot field is good practice and should be retained.

**Retest & validation:** if the header changes in recommendation 2 are deployed, a single `curl -sI https://mindguard.site` re-check confirms presence. If a future engagement adds `mindguardredteam.com` to scope, re-run the coverage ledger from this assessment to avoid duplicating the recon already banked here.

