# OGC Solver Agent Workflow

Use these role cards as a lightweight multi-agent process for OGC solver work.
They are project-local guidance for Codex, not submitted solver code.

Default sequence:

1. Problem Definition Agent
2. Problem Definition Critic Agent
3. Code Writing Agent
4. Code Critic Agent
5. Result Check Agent
6. Result And Final Critic Agent

Each agent should leave a compact handoff: decision, evidence, risks, and next
action. Keep scratch outputs under `.codex_workspace/`.
