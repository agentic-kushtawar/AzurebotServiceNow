# Dev Testing Matrix

| Input                     | Channel | Expected reply (summary)                              |
|--------------------------|---------|------------------------------------------------------|
| hello                    | Teams   | Greeting: capabilities                               |
| reset my password        | Teams   | Offer **instructions** or **open a ticket**          |
| instructions             | Teams   | Step-by-step reset (KB later)                        |
| open a ticket            | CLI     | (Stub for now; real SNOW on Day 4)                   |
| status INC0010002        | CLI     | “Share the incident number...” (real status Day 4)   |

## Useful dev endpoints
- `GET /healthz` → `{"status":"ok"}`
- `GET /metrics` → counters (messages, intents)
- `POST /dev/messages` → quick local tests (if you kept Option A)
- `POST /api/messages` → Bot Framework/Teams endpoint (through Azure Bot)
