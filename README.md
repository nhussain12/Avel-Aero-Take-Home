# Avel take-home: charter-inquiry workflow engine

A small Python CLI that runs a four-step charter workflow (check availability, prepare quote, wait for human approval, send quote). It retries failed tool calls, pauses before any outbound action, and records every state change. All external services are mocked. No dependencies beyond Python 3.8+.

## Run

```bash
python main.py                  # interactive: type y/n at the approval prompt
python main.py --approve        # auto-approve (demo)
python main.py --reject         # auto-reject: proves nothing is sent
python main.py --fail-times 5   # availability API never recovers -> FAILED
```

Each run also writes a machine-readable audit log to `history_REQ-1042.json`.

## Architecture

- **Mock data:** a fleet of four aircraft, each with a **timeline** for the day (where it is and whether it is parked, in flight or in maintenance). The availability check works out each aircraft's state at departure time and checks seats. If an aircraft is parked at another airport, it checks whether there is time to reposition (ferry) it.
- **Mock tools:** `FlakyAvailabilityAPI` raises a `TransientError` (timeout) for its first N calls. `mock_send_email` only returns a receipt.
- **Engine (`Workflow`, `Step`):** runs steps in order over a shared `context` dict, so each step reads earlier results (options → quote → approval → send). Each step declares `max_attempts`. Transient errors are retried with exponential backoff, and when retries run out the workflow ends as `FAILED`. A step marked `needs_approval` pauses the workflow (`PAUSED`) and calls an approver callback. Rejecting it ends the workflow as `CANCELLED` before the send step.
- **History:** every transition (`START`, `RUNNING`, `FAILED`, `RETRY`, `SUCCESS`, `PAUSED`, `APPROVED`/`REJECTED`, `COMPLETE`/`CANCELLED`/`FAILED`) is printed and saved with a timestamp.

## Key decision

**Approval is enforced in two places.** The engine won't move past the approval step without a "yes", and the send step itself refuses to run unless `context["approved"] is True`. A bug in the engine's control flow (e.g. a reordered step list) therefore can't send a quote to a customer. For an agent that acts on a customer's behalf, the irreversible action should defend itself.

## Limitation

State lives in memory, and the approval is a blocking prompt. If the process crashes mid-run, or a human takes hours to approve, the workflow can't resume. In production I'd persist the context and step position after every transition (e.g. a database row per workflow), so a paused workflow can be picked up later by an approval event. I'd also add idempotency keys so a retried send can't go out twice.

## Evidence

### 1. Simulated timeout → retry → approval → send
```
  [START]     Workflow REQ-1042
  [RUNNING]   Check availability
  [FAILED]    Temporary API timeout
  [RETRY]     Attempt 2

    Fleet at 14:00 (KVNY -> KLAS, 6 pax):
    -- N101AV Phenom 300      in flight @ KSFO->KVNY   in flight
    OK N202AV Citation XLS    parked @ KSNA            needs 0.5h ferry from KSNA
    OK N303AV Challenger 350  parked @ KVNY            on site
    -- N404AV Citation CJ3    parked @ KVNY            too few seats

  [SUCCESS]   Aircraft availability received (2 of 4 available)
  [RUNNING]   Prepare quote
  [SUCCESS]   Draft prepared: Citation XLS $7,556 (alt: Challenger 350 $8,040)
  [PAUSED]    Waiting for human approval

    | Quote for J. Rivera (REQ-1042): KVNY -> KLAS at 14:00, 6 pax
    | Aircraft: Citation XLS (N202AV), repositioning 0.5h from KSNA
    | Total: $7,556 (incl. taxes and fees)

  [APPROVED]  Human approves draft
  [RUNNING]   Send quote (mock)
  [SUCCESS]   Mock send completed
  [COMPLETE]  Workflow finished

Final status: COMPLETE  (history saved to history_REQ-1042.json)
```

### 2. Human rejects: nothing is sent
```
  [START]     Workflow REQ-1042
  [RUNNING]   Check availability
  [FAILED]    Temporary API timeout
  [RETRY]     Attempt 2

    Fleet at 14:00 (KVNY -> KLAS, 6 pax):
    -- N101AV Phenom 300      in flight @ KSFO->KVNY   in flight
    OK N202AV Citation XLS    parked @ KSNA            needs 0.5h ferry from KSNA
    OK N303AV Challenger 350  parked @ KVNY            on site
    -- N404AV Citation CJ3    parked @ KVNY            too few seats

  [SUCCESS]   Aircraft availability received (2 of 4 available)
  [RUNNING]   Prepare quote
  [SUCCESS]   Draft prepared: Citation XLS $7,556 (alt: Challenger 350 $8,040)
  [PAUSED]    Waiting for human approval

    | Quote for J. Rivera (REQ-1042): KVNY -> KLAS at 14:00, 6 pax
    | Aircraft: Citation XLS (N202AV), repositioning 0.5h from KSNA
    | Total: $7,556 (incl. taxes and fees)

  [REJECTED]  Human rejects draft
  [CANCELLED] Quote rejected; nothing was sent

Final status: CANCELLED  (history saved to history_REQ-1042.json)
```

### 3. Retries exhausted: workflow fails safely
```
  [START]     Workflow REQ-1042
  [RUNNING]   Check availability
  [FAILED]    Temporary API timeout
  [RETRY]     Attempt 2
  [FAILED]    Temporary API timeout
  [RETRY]     Attempt 3
  [FAILED]    Temporary API timeout
  [FAILED]    Gave up on 'Check availability'

Final status: FAILED  (history saved to history_REQ-1042.json)
```
