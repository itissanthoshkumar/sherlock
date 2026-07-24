"""Roles & permissions catalog for the DPD Early-Warning app.

Permissions are fine-grained capability keys. Roles map to a set of them and are
seeded into the `roles` collection on startup (see db.ensure). Enforcement is via
app.require_permission(<perm>).
"""

# Capability keys
P_CHECK_RUN = "check:run"
P_CONSENT_FETCH = "consent:fetch"
P_REPORT_VIEW = "report:view"
P_HISTORY_VIEW = "history:view"
P_MASTER_VIEW = "master:view"
P_DATA_VIEW = "data:view"
P_USER_MANAGE = "user:manage"
P_ROLE_MANAGE = "role:manage"
P_DBCONFIG_MANAGE = "dbconfig:manage"
P_CYCLE_RUN = "cycle:run"            # start the monthly cycle, retry an item's pull
P_CYCLE_VIEW = "cycle:view"          # view cycles, buckets, processed data, dashboard
P_OVERRIDE = "classify:override"     # supervisor: move a customer between buckets
P_EXPORT = "export:data"             # CSV downloads of bucket lists
P_DISPOSE = "case:dispose"           # record call/visit outcomes on worklist cases
P_JOBS_MANAGE = "jobs:manage"        # run/toggle scheduled jobs

ALL_PERMISSIONS = [
    P_CHECK_RUN, P_CONSENT_FETCH, P_REPORT_VIEW, P_HISTORY_VIEW,
    P_MASTER_VIEW, P_DATA_VIEW, P_USER_MANAGE, P_ROLE_MANAGE, P_DBCONFIG_MANAGE,
    P_CYCLE_RUN, P_CYCLE_VIEW, P_OVERRIDE, P_EXPORT, P_DISPOSE, P_JOBS_MANAGE,
]

# Role -> permissions
ROLE_PERMISSIONS = {
    "admin": list(ALL_PERMISSIONS),
    "operator": [P_CHECK_RUN, P_CONSENT_FETCH, P_REPORT_VIEW, P_HISTORY_VIEW,
                 P_MASTER_VIEW, P_DATA_VIEW, P_CYCLE_RUN, P_CYCLE_VIEW, P_EXPORT,
                 P_DISPOSE, P_JOBS_MANAGE],
    "viewer": [P_REPORT_VIEW, P_HISTORY_VIEW, P_MASTER_VIEW, P_CYCLE_VIEW],
    # Worklist roles: see the cycle + reports, record dispositions on their queue.
    "telecaller": [P_CYCLE_VIEW, P_REPORT_VIEW, P_DISPOSE, P_CONSENT_FETCH],
    "field": [P_CYCLE_VIEW, P_REPORT_VIEW, P_DISPOSE, P_CONSENT_FETCH],
}

# Which bucket each worklist role works (UI default filter + queue banner).
ROLE_WORKLIST = {"telecaller": "WATCH", "field": "SHORTFALL"}

ROLE_DESC = {
    "admin": "Full access — cycles, overrides, jobs, checks, consents, master data, user management.",
    "operator": "Run cycles and checks, retry pulls, fetch consent, dispose cases, run jobs, export.",
    "viewer": "Read-only — view cycles, dashboard, reports, history and master data.",
    "telecaller": "Works the Stretched queue — call customers, record dispositions, request consent.",
    "field": "Works the Shortfall queue — field visits, record dispositions, request consent.",
}
