---
kind: external_dependency
name: MongoDB — Task queue / task storage backend
slug: mongodb
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

- Qlib ships with a MongoDB-backed task store configured via the `mongo` config block (`task_url`, `task_db_name`).
- Default points to a local MongoDB instance on port 27017 with database `default_task_db`.
- Used by the workflow/task subsystem to persist tasks; verify exact collection/schema against the code that reads/writes it.