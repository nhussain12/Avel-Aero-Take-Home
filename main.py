"""
Avel take-home: a tiny workflow execution engine for a charter inquiry.

Run:
    python main.py                 # interactive approval, 1 simulated timeout
    python main.py --approve       # auto-approve (for demos / CI)
    python main.py --reject        # auto-reject (proves nothing is sent)
    python main.py --fail-times 5  # availability API never recovers -> workflow FAILED
"""
import argparse
import json
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# 1. MOCK DATA: a small fleet, each aircraft with a timeline for the day.
#    Each entry = (time it starts, where the aircraft is, what it is doing).
#    The aircraft's state at any moment = the latest entry at or before it.
# ---------------------------------------------------------------------------
FLEET = {
    "N101AV": {"type": "Phenom 300", "seats": 6, "hourly_rate": 3800, "timeline": [
        ("06:00", "KVNY", "parked"),
        ("09:00", "KVNY->KSFO", "in flight"),
        ("10:30", "KSFO", "parked"),
        ("13:00", "KSFO->KVNY", "in flight"),
        ("14:30", "KVNY", "parked"),
    ]},
    "N202AV": {"type": "Citation XLS", "seats": 8, "hourly_rate": 4500, "timeline": [
        ("06:00", "KSNA", "parked"),
    ]},
    "N303AV": {"type": "Challenger 350", "seats": 9, "hourly_rate": 7200, "timeline": [
        ("06:00", "KVNY", "parked"),
        ("10:00", "KVNY", "maintenance"),
        ("13:00", "KVNY", "parked"),
    ]},
    "N404AV": {"type": "Citation CJ3", "seats": 5, "hourly_rate": 3200, "timeline": [
        ("06:00", "KVNY", "parked"),
    ]},
}

# Block times (hours) between airports, used for the trip and repositioning ("ferry") legs.
FLIGHT_HOURS = {
    ("KVNY", "KLAS"): 1.0, ("KSNA", "KVNY"): 0.5,
    ("KSFO", "KVNY"): 1.3, ("KSNA", "KLAS"): 1.0,
}

INQUIRY = {
    "id": "REQ-1042", "client": "J. Rivera",
    "origin": "KVNY", "destination": "KLAS",
    "departure": "14:00", "passengers": 6,
}


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def hours(a, b):
    return FLIGHT_HOURS.get((a, b)) or FLIGHT_HOURS.get((b, a))


def state_at(aircraft, hhmm):
    """Where is this aircraft, and what is it doing, at a given time?"""
    current = None
    for start, loc, activity in aircraft["timeline"]:
        if to_min(start) <= to_min(hhmm):
            current = (loc, activity)
    return current


# ---------------------------------------------------------------------------
# 2. MOCK TOOLS: stand-ins for external services (Avinode/FL3XX, email...).
# ---------------------------------------------------------------------------
class TransientError(Exception):
    """A failure worth retrying (timeouts, rate limits)."""


class FlakyAvailabilityAPI:
    """Fails the first `fail_times` calls with a timeout, then works."""

    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def check(self, inquiry):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TransientError("Temporary API timeout")
        options = []
        for tail, ac in FLEET.items():
            loc, activity = state_at(ac, inquiry["departure"])
            option = {"tail": tail, "type": ac["type"], "seats": ac["seats"],
                      "at_departure": f"{activity} @ {loc}", "ferry_from": None}
            if ac["seats"] < inquiry["passengers"]:
                option["available"], option["reason"] = False, "too few seats"
            elif activity != "parked":
                option["available"], option["reason"] = False, activity
            elif loc != inquiry["origin"]:
                # Parked elsewhere: can it reposition in time?
                ferry_h = hours(loc, inquiry["origin"])
                leave_by = to_min(inquiry["departure"]) - int(ferry_h * 60)
                ok = state_at(ac, f"{leave_by // 60:02d}:{leave_by % 60:02d}")[1] == "parked"
                option.update(available=ok, ferry_from=loc,
                              reason=f"needs {ferry_h}h ferry from {loc}" if ok else "can't reposition in time")
            else:
                option["available"], option["reason"] = True, "on site"
            options.append(option)
        return options


def mock_send_email(to, body):
    return {"status": "sent (mock)", "to": to, "chars": len(body)}


# ---------------------------------------------------------------------------
# 3. THE ENGINE: runs steps in order, retries transient failures,
#    pauses for approval, and records every state change in a history log.
# ---------------------------------------------------------------------------
class Step:
    def __init__(self, name, fn, max_attempts=1, needs_approval=False):
        self.name, self.fn = name, fn
        self.max_attempts, self.needs_approval = max_attempts, needs_approval


class Workflow:
    def __init__(self, workflow_id, steps, approver):
        self.id, self.steps, self.approver = workflow_id, steps, approver
        self.context = {}          # shared data passed between steps
        self.history = []          # readable audit trail
        self.status = "PENDING"

    def log(self, state, message):
        self.history.append({"time": datetime.now().strftime("%H:%M:%S"),
                             "state": state, "message": message})
        print(f"  [{state}]".ljust(14) + message)

    def run(self):
        self.status = "RUNNING"
        self.log("START", f"Workflow {self.id}")
        for step in self.steps:
            if step.needs_approval:
                if not self.wait_for_approval():
                    return self.finish("CANCELLED", "Quote rejected; nothing was sent")
                continue
            if not self.run_step(step):
                return self.finish("FAILED", f"Gave up on '{step.name}'")
        return self.finish("COMPLETE", "Workflow finished")

    def run_step(self, step):
        for attempt in range(1, step.max_attempts + 1):
            if attempt > 1:
                self.log("RETRY", f"Attempt {attempt}")
            else:
                self.log("RUNNING", step.name)
            try:
                summary = step.fn(self.context)
                self.log("SUCCESS", summary)
                return True
            except TransientError as e:
                self.log("FAILED", str(e))
                if attempt < step.max_attempts:
                    time.sleep(0.2 * 2 ** (attempt - 1))   # exponential backoff
        return False

    def wait_for_approval(self):
        self.status = "PAUSED"
        self.log("PAUSED", "Waiting for human approval")
        approved = self.approver(self.context["quote_text"])
        self.context["approved"] = approved
        self.status = "RUNNING"
        self.log("APPROVED" if approved else "REJECTED",
                 "Human approves draft" if approved else "Human rejects draft")
        return approved

    def finish(self, status, message):
        self.status = status
        self.log(status, message)
        with open(f"history_{self.id}.json", "w") as f:
            json.dump({"workflow": self.id, "status": status,
                       "history": self.history}, f, indent=2)
        return status


# ---------------------------------------------------------------------------
# 4. THE FOUR STEPS: each reads from and writes to the shared context.
# ---------------------------------------------------------------------------
def make_steps(api, inquiry):
    def check_availability(ctx):
        options = api.check(inquiry)
        ctx["options"] = options
        print(f"\n    Fleet at {inquiry['departure']} ({inquiry['origin']} -> "
              f"{inquiry['destination']}, {inquiry['passengers']} pax):")
        for o in options:
            mark = "OK " if o["available"] else "-- "
            print(f"    {mark}{o['tail']} {o['type']:<15} {o['at_departure']:<24} {o['reason']}")
        print()
        n = sum(o["available"] for o in options)
        return f"Aircraft availability received ({n} of {len(options)} available)"

    def prepare_quote(ctx):
        trip_h = hours(inquiry["origin"], inquiry["destination"])
        priced = []
        for o in (o for o in ctx["options"] if o["available"]):
            rate = FLEET[o["tail"]]["hourly_rate"]
            ferry_h = hours(o["ferry_from"], inquiry["origin"]) if o["ferry_from"] else 0
            subtotal = (trip_h + ferry_h) * rate
            total = round(subtotal * 1.075 + 300)    # 7.5% excise tax + $300 fees
            priced.append({**o, "trip_h": trip_h, "ferry_h": ferry_h, "total": total})
        best = min(priced, key=lambda p: p["total"])
        ctx["quote"] = best
        ctx["quote_text"] = (
            f"Quote for {inquiry['client']} ({inquiry['id']}): {inquiry['origin']} -> "
            f"{inquiry['destination']} at {inquiry['departure']}, {inquiry['passengers']} pax\n"
            f"Aircraft: {best['type']} ({best['tail']})"
            + (f", repositioning {best['ferry_h']}h from {best['ferry_from']}" if best["ferry_from"] else "")
            + f"\nTotal: ${best['total']:,} (incl. taxes and fees)")
        others = ", ".join(f"{p['type']} ${p['total']:,}" for p in priced if p is not best)
        return f"Draft prepared: {best['type']} ${best['total']:,}" + (f" (alt: {others})" if others else "")

    def send_quote(ctx):
        # Guard in the step itself, so sending is safe even if the engine has a bug.
        if ctx.get("approved") is not True:
            raise PermissionError("Refusing to send without explicit approval")
        mock_send_email(inquiry["client"], ctx["quote_text"])
        return "Mock send completed"

    return [
        Step("Check availability", check_availability, max_attempts=3),
        Step("Prepare quote", prepare_quote),
        Step("Wait for human approval", None, needs_approval=True),
        Step("Send quote (mock)", send_quote),
    ]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--fail-times", type=int, default=1)
    args = p.parse_args()

    def approver(quote_text):
        print("\n" + "\n".join("    | " + line for line in quote_text.splitlines()) + "\n")
        if args.approve or args.reject:
            return args.approve
        return input("    Approve and send this quote? [y/N] ").strip().lower() == "y"

    api = FlakyAvailabilityAPI(fail_times=args.fail_times)
    wf = Workflow(INQUIRY["id"], make_steps(api, INQUIRY), approver)
    status = wf.run()
    print(f"\nFinal status: {status}  (history saved to history_{wf.id}.json)")


if __name__ == "__main__":
    main()
