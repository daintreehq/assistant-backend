"""The durable orchestration runtime (Run Supervisor).

The backend-owned turn loop: a durable event log per run, tool-call leases the
CLI claims and fulfils, server-side approvals for background mutation, durable
wakeups, and exact prompt replay. See ``docs/RUN_SUPERVISOR.md``.
"""
