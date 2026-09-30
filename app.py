"""
app.py — Flask application entry point for TrialGuard.

Blueprint Section 46 routes:
  GET  /                                   → dashboard
  GET  /participants/<id>                  → participant timeline
  GET  /participants/<id>/checkin          → check-in form
  POST /participants/<id>/analyze          → run agent, save, redirect to result
  GET  /runs/<run_id>                      → result / action brief page
  GET  /runs/<run_id>/trace                → memory trace / audit drawer
  GET  /api/stats                          → JSON stats (for dashboard counters)

Blueprint Section 65 — every route has graceful error fallbacks.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # must be first — before any module reads os.environ

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import db
import agent as ag
import memory as mem
from safety import is_urgent

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "trialguard-dev-secret")

# Jinja2 extras
app.jinja_env.globals["enumerate"] = enumerate

# ── App startup ───────────────────────────────────────────────────────────────

def _ensure_demo_seed() -> None:
    """Seed the demo participants on serverless (Vercel) cold starts.

    Vercel's /tmp SQLite is wiped on every cold start, so without this the
    dashboard would show an empty participant list after each new container.
    Mirrors seed_db.py: only runs on Vercel, and only when the participants
    table is empty. Failures are non-fatal.
    """
    if os.environ.get("VERCEL") != "1":
        return
    try:
        if db.list_participants():
            return
        demo_file = Path(__file__).parent / "data" / "demo_data.json"
        if not demo_file.exists():
            return
        for p in json.loads(demo_file.read_text(encoding="utf-8")).get("participants", []):
            db.upsert_participant(
                participant_id=p["id"],
                display_name=p.get("display_name", p["id"]),
                trial_id=p.get("trial_id", "TRIAL-001"),
                status=p.get("status", "Active"),
            )
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Demo auto-seed failed (non-fatal): %s", exc)




import autoseed

@app.before_request
def _startup():
    db.init_db()
    autoseed.seed_if_empty()
# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_participant_or_404(participant_id: str):
    p = db.get_participant(participant_id)
    if not p:
        abort(404, description=f"Participant '{participant_id}' not found.")
    return p


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    """
    Dashboard — active participants, recent check-ins, memory stats.
    Blueprint Sections 32, 57.
    """
    stats        = db.get_stats()
    participants = db.list_participants()
    recent_runs  = db.recent_agent_runs(limit=5)

    # Hindsight health check — reset client so it reconnects fresh each page load
    hindsight_ok = True
    hindsight_version = "unknown"
    mem._reset_client()
    try:
        hindsight_version = mem.get_version()
    except Exception as e:
        app.logger.warning("Hindsight health check failed: %s: %s", type(e).__name__, e)
        hindsight_ok = False

    return render_template(
        "dashboard.html",
        stats=stats,
        participants=participants,
        recent_runs=recent_runs,
        hindsight_ok=hindsight_ok,
        hindsight_version=hindsight_version,
    )


@app.route("/participants/<participant_id>")
def participant(participant_id: str):
    """
    Participant timeline page — all check-ins and outcomes.
    Blueprint Section 32 (participant page).
    """
    p        = _get_participant_or_404(participant_id)
    checkins = db.list_checkins(participant_id)
    outcomes = db.list_outcomes(participant_id)

    # Attach runs to each check-in for inline display
    for ci in checkins:
        ci["runs"] = db.list_agent_runs(ci["id"])

    return render_template(
        "participant.html",
        participant=p,
        checkins=checkins,
        outcomes=outcomes,
    )


@app.route("/participants/<participant_id>/checkin")
def checkin_form(participant_id: str):
    """Render the check-in submission form. Blueprint Section 33."""
    p = _get_participant_or_404(participant_id)
    return render_template(
        "checkin.html",
        participant=p,
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )


@app.route("/participants/<participant_id>/analyze", methods=["POST"])
def analyze(participant_id: str):
    """
    Core route — Blueprint Section 47 full request lifecycle:
      1. Validate input
      2. Save check-in to SQLite
      3. Retain check-in in Hindsight
      4. Run agent (tool call → recall → structured output)
      5. Save agent run
      6. Redirect to result page

    Blueprint Section 23: participant_id is taken from the URL path,
    never from form data or LLM output.
    """
    p = _get_participant_or_404(participant_id)

    raw_text    = (request.form.get("checkin_text") or "").strip()
    occurred_at = (request.form.get("occurred_at")  or _now_iso()).strip()
    mode        = request.form.get("mode", "hindsight")

    if not raw_text:
        return render_template(
            "checkin.html",
            participant=p,
            today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            error="Check-in text cannot be empty.",
        ), 400

    # ── Step 1: generate stable IDs ──────────────────────────────────────────
    checkin_id = f"checkin:{participant_id}:{uuid.uuid4().hex[:8]}"
    run_id     = f"run:{uuid.uuid4().hex[:12]}"

    # ── Step 2: save to SQLite ───────────────────────────────────────────────
    db.save_checkin(
        checkin_id=checkin_id,
        participant_id=participant_id,
        occurred_at=occurred_at,
        raw_text=raw_text,
    )

    # ── Step 3: retain in Hindsight ──────────────────────────────────────────
    hindsight_retain_ok = True
    try:
        mem.retain(
            content=raw_text,
            participant_id=participant_id,
            document_id=checkin_id,
            metadata={
                "participant_id": participant_id,
                "trial_id":       p.get("trial_id", "TRIAL-001"),
                "event_type":     "checkin",
                "source":         "live_input",
            },
            extra_tags=[f"trial:{p.get('trial_id', 'TRIAL-001')}"],
        )
    except Exception as exc:
        app.logger.warning("Hindsight retain failed (non-fatal): %s", exc)
        hindsight_retain_ok = False

    # ── Step 4: run agent ────────────────────────────────────────────────────
    result = ag.run_agent(
        checkin_text=raw_text,
        participant_id=participant_id,
        trial_id=p.get("trial_id", "TRIAL-001"),
        occurred_at=occurred_at,
        mode=mode,
    )

    # ── Step 5: save agent run ───────────────────────────────────────────────
    db.save_agent_run(
        run_id=run_id,
        checkin_id=checkin_id,
        mode=result.get("mode", mode),
        final_output=result["brief"],
        hindsight_query=result.get("hindsight_query"),
        retrieved_memories=result.get("retrieved_memories", []),
    )

    # ── Step 6: retain outcome for future retrieval (blueprint Section 28) ───
    brief = result["brief"]
    outcome_text = (
        f"Check-in analysis for Participant {participant_id} on {occurred_at}.\n"
        f"Current issue: {brief.get('current_issue', '')}\n"
        f"Historical pattern: {brief.get('historical_pattern', '')}\n"
        f"Previous outcome: {brief.get('previous_outcome', '')}\n"
        f"Coordinator action: {brief.get('recommended_coordinator_action', '')}"
    )
    try:
        mem.retain(
            content=outcome_text,
            participant_id=participant_id,
            document_id=f"outcome:{run_id}",
            metadata={
                "participant_id": participant_id,
                "trial_id":       p.get("trial_id", "TRIAL-001"),
                "event_type":     "agent_outcome",
                "run_id":         run_id,
            },
            extra_tags=[f"trial:{p.get('trial_id', 'TRIAL-001')}"],
        )
    except Exception as exc:
        app.logger.warning("Hindsight outcome retain failed (non-fatal): %s", exc)

    return redirect(url_for("result", run_id=run_id))


@app.route("/runs/<run_id>")
def result(run_id: str):
    """
    Result page — Coordinator Action Brief.
    Blueprint Sections 31, 34, 35.
    """
    run = db.get_agent_run(run_id)
    if not run:
        abort(404, description=f"Run '{run_id}' not found.")

    checkin = db.get_checkin(run["checkin_id"]) if run.get("checkin_id") else None
    participant = None
    if checkin:
        participant = db.get_participant(checkin["participant_id"])

    brief = run.get("final_output", {})

    return render_template(
        "result.html",
        run=run,
        brief=brief,
        checkin=checkin,
        participant=participant,
    )


@app.route("/runs/<run_id>/trace")
def memory_trace(run_id: str):
    """
    Memory trace / audit drawer — blueprint Sections 37, 55.
    Shows exactly what Hindsight returned and how the brief was constructed.
    """
    run = db.get_agent_run(run_id)
    if not run:
        abort(404, description=f"Run '{run_id}' not found.")

    checkin     = db.get_checkin(run["checkin_id"]) if run.get("checkin_id") else None
    participant = db.get_participant(checkin["participant_id"]) if checkin else None

    return render_template(
        "trace.html",
        run=run,
        checkin=checkin,
        participant=participant,
        memories=run.get("retrieved_memories", []),
    )


@app.route("/api/stats")
def api_stats():
    """JSON stats endpoint for dashboard live counters."""
    return jsonify(db.get_stats())


@app.route("/api/participants")
def api_participants():
    """JSON participant list."""
    return jsonify(db.list_participants())


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message=str(e)), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Internal server error."), 500


# ── Dev server ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5000)
