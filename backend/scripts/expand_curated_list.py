"""Curated list — Veeva-pattern dominant (~70%).

Target distribution (~120 total = original 10 + 110 new):
  Veeva-pattern (84, 70%):
    healthcare 20, insurance 10, banking/finance 13, construction/industrial 11,
    legal 8, education 7, regulatory/compliance 6, logistics 5, telecom 1, other 3
  CrowdStrike-pattern (30, 25%):
    security 7, observability 6, data infra 7, dev infra 6, payments 5
  Ouster-pattern (6, 5%):
    perception 4, smart infra 2

Cut from prior round: Tenable, Rapid7, Zscaler, Lacework, Orca, Abnormal (security
megacaps too hot), Datadog (already-flagged ship-fast culture), Sentry,
Databricks (hot/scaling), Astronomer, SingleStore, PostHog, Fivetran,
CircleCI, Netlify, Postman, Retool, Sourcegraph, Stripe (intense), Eagle Eye.

Usage:  python scripts/expand_curated_list.py
"""
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / "config" / "companies.yaml"


CANDIDATES: list[tuple[str, str, str, str, list[str], list[str]]] = [
    # =================================================================
    # VEEVA-PATTERN: HEALTHCARE / LIFE SCIENCES (20)
    # =================================================================
    ("Definitive Healthcare", "healthcare-saas", "500-5000", "Public; healthcare analytics.", ["definitivehealthcare"], ["definitivehealthcare"]),
    ("Schrödinger", "life-sciences-saas", "500-5000", "Public; computational drug discovery.", ["schrodinger"], ["schrodinger"]),
    ("Benchling", "life-sciences-saas", "500-5000", "R&D platform; closest direct Veeva analog.", ["benchling"], ["benchling"]),
    ("PointClickCare", "healthcare-saas", "500-5000", "Senior care SaaS; sustainable culture.", ["pointclickcare"], ["pointclickcare"]),
    ("NextGen Healthcare", "healthcare-saas", "500-5000", "EHR / practice management.", ["nextgenhealthcare", "nextgen"], ["nextgenhealthcare"]),
    ("WellSky", "healthcare-saas", "500-5000", "Care continuum SaaS.", ["wellsky"], ["wellsky"]),
    ("Phreesia", "healthcare-saas", "500-5000", "Public; patient intake.", ["phreesia"], ["phreesia"]),
    ("Medable", "clinical-trials-saas", "500-5000", "Clinical trial platform.", ["medable"], ["medable"]),
    ("Komodo Health", "healthcare-data", "500-5000", "Healthcare data platform.", ["komodohealth"], ["komodohealth"]),
    ("HealthEquity", "healthcare-finance", "500-5000", "Public; HSA admin.", ["healthequity"], ["healthequity"]),
    ("Inovalon", "healthcare-data", "500-5000", "Healthcare data + analytics (Nordic PE).", ["inovalon"], ["inovalon"]),
    ("Innovaccer", "healthcare-data", "500-5000", "Healthcare data activation.", ["innovaccer"], ["innovaccer"]),
    ("Privia Health", "healthcare-saas", "500-5000", "Public; physician practice mgmt.", ["priviahealth", "privia"], ["privia"]),
    ("Castor", "clinical-trials-saas", "40-500", "Clinical trials data.", ["castor", "castoredc"], ["castoredc"]),
    ("Aetion", "real-world-evidence", "40-500", "Real-world evidence analytics for pharma.", ["aetion"], ["aetion"]),
    ("TigerConnect", "healthcare-comms", "40-500", "Clinical comms for providers.", ["tigerconnect"], ["tigerconnect"]),
    ("Datavant", "healthcare-data", "500-5000", "Healthcare data linkage platform.", ["datavant"], ["datavant"]),
    ("Health Catalyst", "healthcare-analytics", "500-5000", "Public; healthcare analytics.", ["healthcatalyst"], ["healthcatalyst"]),
    ("MultiPlan", "healthcare-saas", "500-5000", "Public; healthcare cost mgmt.", ["multiplan"], ["multiplan"]),
    ("OptimizeRx", "life-sciences-saas", "40-500", "Public; life-sciences digital engagement.", ["optimizerx"], ["optimizerx"]),

    # =================================================================
    # VEEVA-PATTERN: INSURANCE VERTICAL SAAS (10) — Guidewire + 9 new
    # =================================================================
    ("Duck Creek Technologies", "insurance-saas", "500-5000", "P&C insurance core (Vista PE).", ["duckcreek", "duckcreektechnologies"], ["duckcreek"]),
    ("Applied Systems", "insurance-saas", "500-5000", "Agency management.", ["appliedsystems"], ["appliedsystems"]),
    ("Sapiens", "insurance-saas", "500-5000", "Insurance core platform.", ["sapiens"], ["sapiens"]),
    ("Origami Risk", "risk-management-saas", "500-5000", "Risk + insurance GRC.", ["origamirisk"], ["origamirisk"]),
    ("Vertafore", "insurance-saas", "500-5000", "Agency management (PE).", ["vertafore"], ["vertafore"]),
    ("Majesco", "insurance-saas", "500-5000", "Insurance core systems (PE).", ["majesco"], ["majesco"]),
    ("Insurity", "insurance-saas", "500-5000", "Insurance software (TA PE).", ["insurity"], ["insurity"]),
    ("Cover Genius", "embedded-insurance", "500-5000", "Embedded insurance distribution.", ["covergenius"], ["covergenius"]),
    ("Verisk Analytics", "insurance-data", "5000-15000", "Public; insurance + risk data.", ["verisk"], ["verisk"]),

    # =================================================================
    # VEEVA-PATTERN: BANKING / FINANCE VERTICAL SAAS (13) — nCino + 12 new
    # =================================================================
    ("Q2 Holdings", "banking-saas", "500-5000", "Public; digital banking platform.", ["q2", "q2ebanking"], ["q2"]),
    ("Alkami Technology", "banking-saas", "500-5000", "Public; digital banking.", ["alkami"], ["alkami"]),
    ("Intapp", "professional-services-saas", "500-5000", "Public; legal + finance vertical.", ["intapp"], ["intapp"]),
    ("BlackLine", "finance-saas", "500-5000", "Public; finance close automation.", ["blackline"], ["blackline"]),
    ("Workiva", "regulatory-reporting", "500-5000", "Public; SEC/ESG/regulatory reporting — pure Veeva-pattern.", ["workiva"], ["workiva"]),
    ("Anaplan", "enterprise-planning", "5000-15000", "Enterprise planning (Thoma Bravo PE).", ["anaplan"], ["anaplan"]),
    ("AvidXchange", "ap-automation", "5000-15000", "Public; AP automation for mid-market.", ["avidxchange"], ["avidxchange"]),
    ("Tipalti", "payments-finance", "500-5000", "B2B payments + finance ops.", ["tipalti"], ["tipalti"]),
    ("Jack Henry", "banking-saas", "5000-15000", "Public; community bank core.", ["jackhenry"], ["jackhenry"]),
    ("FIS", "financial-services-tech", "5000-15000", "Public; financial services tech megacap.", ["fis", "fisglobal"], ["fis"]),
    ("Temenos", "banking-saas", "5000-15000", "Public; banking software platform.", ["temenos"], ["temenos"]),
    ("Bill.com", "finance-saas", "500-5000", "Public; B2B AP/AR automation.", ["billcom", "bill"], ["bill"]),

    # =================================================================
    # VEEVA-PATTERN: LEGAL VERTICAL SAAS (8)
    # =================================================================
    ("Clio", "legal-saas", "500-5000", "Law firm management.", ["clio"], ["clio"]),
    ("Mitratech", "legal-grc-saas", "500-5000", "Legal/GRC (PE).", ["mitratech"], ["mitratech"]),
    ("Relativity", "legal-tech", "500-5000", "E-discovery platform.", ["relativity"], ["relativity"]),
    ("Ironclad", "contract-lifecycle", "500-5000", "CLM platform.", ["ironclad"], ["ironclad"]),
    ("LinkSquares", "contract-lifecycle", "40-500", "AI-native CLM.", ["linksquares"], ["linksquares"]),
    ("Litera", "legal-document-saas", "500-5000", "Legal document workflow.", ["litera"], ["litera"]),
    ("NetDocuments", "legal-document-saas", "500-5000", "Legal document management.", ["netdocuments"], ["netdocuments"]),
    ("Onit", "legal-ops-saas", "500-5000", "Legal operations + CLM.", ["onit"], ["onit"]),

    # =================================================================
    # VEEVA-PATTERN: EDUCATION VERTICAL SAAS (7)
    # =================================================================
    ("Instructure", "education-saas", "500-5000", "Public; Canvas LMS.", ["instructure"], ["instructure"]),
    ("PowerSchool", "education-saas", "500-5000", "K-12 SIS (Bain PE).", ["powerschool"], ["powerschool"]),
    ("Anthology", "education-saas", "500-5000", "Higher ed (Blackboard merger; PE).", ["anthology"], ["anthology"]),
    ("D2L Brightspace", "education-saas", "500-5000", "Public; LMS.", ["d2l"], ["d2l"]),
    ("Ellucian", "education-saas", "500-5000", "Higher ed ERP (Blackstone PE).", ["ellucian"], ["ellucian"]),
    ("Frontline Education", "education-saas", "500-5000", "K-12 operations SaaS.", ["frontlineeducation"], ["frontlineeducation"]),
    ("EAB", "education-saas", "500-5000", "Higher ed enrollment + success.", ["eab"], ["eab"]),

    # =================================================================
    # VEEVA-PATTERN: CONSTRUCTION / INDUSTRIAL VERTICAL SAAS (11)
    # =================================================================
    ("Procore", "construction-saas", "500-5000", "Public; construction PM.", ["procoretechnologies", "procore"], ["procore"]),
    ("AppFolio", "real-estate-saas", "500-5000", "Public; property management.", ["appfolio"], ["appfolio"]),
    ("Autodesk", "design-engineering", "5000-15000", "Public; design + construction cloud.", ["autodesk"], ["autodesk"]),
    ("PTC", "industrial-design-saas", "5000-15000", "Public; CAD/PLM + IoT.", ["ptc"], ["ptc"]),
    ("Aspen Technology", "industrial-saas", "5000-15000", "Public; process industries software.", ["aspentech"], ["aspentech"]),
    ("Bentley Systems", "infrastructure-engineering", "5000-15000", "Public; infrastructure engineering.", ["bentley"], ["bentley"]),
    ("AVEVA", "industrial-saas", "5000-15000", "Public; industrial software (Schneider Electric).", ["aveva"], ["aveva"]),
    ("Hexagon", "industrial-design-saas", "5000-15000", "Public; geospatial + industrial software.", ["hexagon"], ["hexagon"]),
    ("Augury", "predictive-maintenance", "500-5000", "Industrial predictive maintenance.", ["augury"], ["augury"]),
    ("Tulip Interfaces", "manufacturing-saas", "40-500", "Manufacturing apps platform.", ["tulip"], ["tulip"]),
    ("Plex Systems", "manufacturing-erp", "500-5000", "Cloud ERP for manufacturing (Rockwell).", ["plex"], ["plex"]),

    # =================================================================
    # VEEVA-PATTERN: REGULATORY / COMPLIANCE / GRC (6)
    # =================================================================
    ("Drata", "compliance-automation", "500-5000", "SOC2/ISO compliance automation.", ["drata"], ["drata"]),
    ("Vanta", "compliance-automation", "500-5000", "Compliance automation; Veeva-pattern depth.", ["vanta"], ["vanta"]),
    ("Diligent", "governance-saas", "500-5000", "Board governance + GRC (Insight PE).", ["diligent"], ["diligent"]),
    ("AuditBoard", "audit-grc-saas", "500-5000", "Audit + risk + compliance.", ["auditboard"], ["auditboard"]),
    ("OneTrust", "privacy-grc-saas", "500-5000", "Privacy + GRC platform.", ["onetrust"], ["onetrust"]),
    ("NAVEX Global", "ethics-compliance-saas", "500-5000", "Ethics + compliance SaaS (PE).", ["navex", "navexglobal"], ["navex"]),

    # =================================================================
    # VEEVA-PATTERN: LOGISTICS / SUPPLY CHAIN B2B SAAS (5)
    # =================================================================
    ("project44", "logistics-visibility", "500-5000", "Real-time supply chain visibility.", ["project44"], ["project44"]),
    ("FourKites", "logistics-visibility", "500-5000", "Supply chain visibility.", ["fourkites"], ["fourkites"]),
    ("Manhattan Associates", "supply-chain-saas", "5000-15000", "Public; supply chain commerce.", ["manhattanassociates"], ["manhattanassociates"]),
    ("o9 Solutions", "supply-chain-planning", "5000-15000", "Enterprise supply chain planning.", ["o9solutions"], ["o9solutions"]),
    ("Blue Yonder", "supply-chain-saas", "5000-15000", "Supply chain SaaS (Panasonic-owned).", ["blueyonder"], ["blueyonder"]),

    # =================================================================
    # VEEVA-PATTERN: TELECOM B2B SAAS (1)
    # =================================================================
    ("CSG International", "telecom-bss", "5000-15000", "Public; telecom BSS / revenue mgmt.", ["csgi", "csginternational"], ["csgi"]),

    # =================================================================
    # CROWDSTRIKE-PATTERN: SECURITY (4 new + 3 seed = 7)
    # Calmer / mission-critical only
    # =================================================================
    ("Snyk", "developer-security", "500-5000", "Developer-first security; calmer than the hot-security crowd.", ["snyk"], ["snyk"]),
    ("Sysdig", "runtime-security", "500-5000", "Runtime + cloud security.", ["sysdig"], ["sysdig"]),
    ("Cloudflare", "network-security", "5000-15000", "Public; security + edge infra; mature.", ["cloudflare"], ["cloudflare"]),
    ("Okta", "identity", "5000-15000", "Public; enterprise identity; mature.", ["okta"], ["okta"]),

    # =================================================================
    # CROWDSTRIKE-PATTERN: OBSERVABILITY (4 new + 2 seed = 6) — calmer
    # =================================================================
    ("Honeycomb", "observability", "40-500", "Calm engineering culture; distributed tracing.", ["honeycomb"], ["honeycomb"]),
    ("Chronosphere", "observability", "500-5000", "Cloud-native observability; calm culture.", ["chronosphere"], ["chronosphere"]),
    ("Cribl", "observability-pipeline", "500-5000", "Observability data pipeline.", ["cribl"], ["cribl"]),
    ("PagerDuty", "incident-response", "500-5000", "Public; incident response.", ["pagerduty"], ["pagerduty"]),

    # =================================================================
    # CROWDSTRIKE-PATTERN: DATA INFRA (6 new + MongoDB seed = 7)
    # =================================================================
    ("Snowflake", "data-warehouse", "5000-15000", "Public; data cloud.", ["snowflake"], ["snowflake"]),
    ("Confluent", "streaming", "500-5000", "Public; Kafka cloud.", ["confluent"], ["confluent"]),
    ("Cockroach Labs", "distributed-database", "500-5000", "CockroachDB.", ["cockroachlabs"], ["cockroachlabs"]),
    ("dbt Labs", "data-transformation", "500-5000", "dbt; calm technical culture.", ["dbtlabs"], ["dbtlabs"]),
    ("Redis", "in-memory", "500-5000", "Public; in-memory data.", ["redis"], ["redis"]),
    ("ClickHouse", "olap-database", "500-5000", "Commercial ClickHouse cloud.", ["clickhouse"], ["clickhouse"]),

    # =================================================================
    # CROWDSTRIKE-PATTERN: DEV INFRA (6) — engineering buyers, calmer
    # =================================================================
    ("GitLab", "devops", "500-5000", "Public; full DevOps platform.", ["gitlab"], ["gitlab"]),
    ("LaunchDarkly", "feature-flags", "500-5000", "Feature management for engineering teams.", ["launchdarkly"], ["launchdarkly"]),
    ("Vercel", "frontend-platform", "500-5000", "Next.js / FE-native hosting.", ["vercel"], ["vercel"]),
    ("Linear", "engineering-pm", "40-500", "Engineering PM; calm culture.", ["linear"], ["linear"]),
    ("HashiCorp", "infrastructure-automation", "500-5000", "Terraform/Vault/Consul (IBM).", ["hashicorp"], ["hashicorp"]),
    ("Buildkite", "ci-cd", "40-500", "CI/CD; calm engineering culture.", ["buildkite"], ["buildkite"]),

    # =================================================================
    # CROWDSTRIKE-PATTERN: B2B PAYMENTS / FINANCIAL INFRA (5)
    # =================================================================
    ("Plaid", "fintech-infra", "500-5000", "Financial data infra.", ["plaid"], ["plaid"]),
    ("Adyen", "payments-infra", "5000-15000", "Public; enterprise payments.", ["adyen"], ["adyen"]),
    ("Marqeta", "card-issuing", "500-5000", "Public; programmable card issuing.", ["marqeta"], ["marqeta"]),
    ("Modern Treasury", "payments-infra", "40-500", "Payments ops platform.", ["moderntreasury"], ["moderntreasury"]),
    ("Brex", "fintech", "500-5000", "B2B financial platform.", ["brex"], ["brex"]),

    # =================================================================
    # OUSTER-PATTERN: PERCEPTION (2 new + 2 seed = 4)
    # =================================================================
    ("Innoviz Technologies", "lidar", "40-500", "Public; lidar for automotive/industrial.", ["innoviz", "innoviztechnologies"], ["innoviz"]),
    ("Mobileye", "perception", "5000-15000", "Public (Intel-subsidiary); ADAS/AV perception.", ["mobileye"], ["mobileye"]),

    # =================================================================
    # OUSTER-PATTERN: SMART INFRA / IoT (2)
    # =================================================================
    ("Verkada", "physical-security", "500-5000", "Cloud-managed physical security.", ["verkada"], ["verkada"]),
    ("Samsara", "fleet-iot", "5000-15000", "Public; connected operations for industrial fleets.", ["samsara"], ["samsara"]),
]


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:64]


def probe_greenhouse(client: httpx.Client, slug: str) -> int:
    try:
        r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=4.0)
        if r.status_code == 200:
            return len(r.json().get("jobs", []))
    except (httpx.HTTPError, ValueError):
        pass
    return 0


def probe_lever(client: httpx.Client, slug: str) -> int:
    try:
        r = client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=4.0)
        if r.status_code == 200:
            j = r.json()
            return len(j) if isinstance(j, list) else 0
    except (httpx.HTTPError, ValueError):
        pass
    return 0


def probe_one(name: str, gh_candidates: list[str], lv_candidates: list[str]) -> tuple[str | None, str | None, int, int]:
    with httpx.Client() as client:
        for s in gh_candidates:
            n = probe_greenhouse(client, s)
            if n > 0:
                return s, None, n, 0
        for s in lv_candidates:
            n = probe_lever(client, s)
            if n > 0:
                return None, s, 0, n
    return None, None, 0, 0


def load_existing_slugs() -> set[str]:
    if not YAML_PATH.exists():
        return set()
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f) or {}
    return {c["slug"] for c in (data.get("companies") or [])}


def yaml_block(entry: dict) -> str:
    lines = [f"  - name: {entry['name']}"]
    lines.append(f"    slug: {entry['slug']}")
    lines.append(f"    segment: {entry['segment']}")
    lines.append(f"    headcount_band: {entry['headcount_band']}")
    gh = entry.get("greenhouse_slug")
    lines.append(f"    greenhouse_slug: {gh if gh else 'null'}")
    lv = entry.get("lever_slug")
    lines.append(f"    lever_slug: {lv if lv else 'null'}")
    lines.append(f"    enabled: {'true' if entry.get('enabled', True) else 'false'}")
    notes = entry.get("notes", "").replace('"', '\\"')
    lines.append(f"    notes: \"{notes}\"")
    return "\n".join(lines)


def main() -> int:
    existing = load_existing_slugs()
    print(f"Existing curated slugs: {len(existing)}")

    to_probe = []
    for name, segment, hb, notes, gh_cands, lv_cands in CANDIDATES:
        slug = slugify(name)
        if slug in existing:
            print(f"  SKIP (already curated): {name}")
            continue
        to_probe.append((name, segment, hb, notes, slug, gh_cands, lv_cands))

    print(f"\nProbing ATS for {len(to_probe)} candidates...")
    results: dict[str, tuple[str | None, str | None, int, int]] = {}
    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = {pool.submit(probe_one, t[0], t[5], t[6]): t[0] for t in to_probe}
        done = 0
        for fut in as_completed(futs):
            name = futs[fut]
            results[name] = fut.result()
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(to_probe)} probed")

    gh_hits = sum(1 for r in results.values() if r[0])
    lv_hits = sum(1 for r in results.values() if r[1])
    no_ats = len(results) - gh_hits - lv_hits
    print(f"\nATS coverage: greenhouse={gh_hits}  lever={lv_hits}  adzuna-only={no_ats}\n")

    new_entries = []
    for name, segment, hb, notes, slug, _, _ in to_probe:
        gh_slug, lv_slug, gh_n, lv_n = results.get(name, (None, None, 0, 0))
        ats_note = ""
        if gh_slug:
            ats_note = f" GH board has {gh_n} jobs."
        elif lv_slug:
            ats_note = f" Lever board has {lv_n} jobs."
        new_entries.append({
            "name": name, "slug": slug, "segment": segment,
            "headcount_band": hb,
            "greenhouse_slug": gh_slug, "lever_slug": lv_slug,
            "enabled": True, "notes": notes + ats_note,
        })

    if not new_entries:
        print("Nothing to add.")
        return 0

    block = "\n  # === Veeva-weighted curated list (70/25/5) ===\n"
    block += "\n\n".join(yaml_block(e) for e in new_entries) + "\n"

    with open(YAML_PATH, "a", encoding="utf-8") as f:
        f.write(block)

    print(f"Appended {len(new_entries)} new entries to {YAML_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
