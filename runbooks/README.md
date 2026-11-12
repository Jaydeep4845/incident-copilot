# Runbooks

Operational playbooks for the shop system. The Incident Copilot searches them semantically
(`search_runbooks`, using the fine-tuned embedding model from DealScope) and cites the section it used.
Each runbook: symptoms, what to check, likely causes, safe actions. Remediation always needs human approval.
