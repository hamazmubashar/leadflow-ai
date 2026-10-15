"""Workflow runs (M8): preview vs live, mandatory human approval, one running run,
interruption, per-lead failure recovery, no duplicate drafts and honest completion.

Races and crashes are made deterministic by wrapping the service's own steps
(``_begin``, ``_process_lead``, ``_item``, ``_candidates``) so a competing change
happens at an exact point, with no sleeps or thread timing.
"""

import ast
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

import leadflow_ai.services.workflows as service
from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.db.models import (
    LeadDraftRecord,
    LeadEventRecord,
    LeadRecord,
    OutboxMessageRecord,
    WorkflowRunItemRecord,
    WorkflowRunRecord,
)
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.scoring.engine import score_lead
from leadflow_ai.scoring.rules import ScoringRules, load_rules
from leadflow_ai.services.drafts import approve_draft, create_draft, get_draft
from leadflow_ai.services.leads import create_lead
from leadflow_ai.services.lifecycle import StatusChangeRequest, change_status
from leadflow_ai.services.workflows import (
    RunInProgressError,
    get_run,
    list_items,
    list_runs,
    run_counts,
    run_workflow,
)
from leadflow_ai.workflows import (
    MAX_LEADS_PER_RUN,
    STALE_RUN_AFTER,
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    Decision,
    RunMode,
)
from leadflow_ai.workflows.outreach import DRAFT_TEMPLATE

PREVIEW = RunMode.PREVIEW
LIVE = RunMode.LIVE
_counter = iter(range(1, 100_000))


@pytest.fixture
def rules() -> ScoringRules:
    return load_rules(None)


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def other_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    """Sessions on a SEPARATE engine: another request or process."""
    engine = create_db_engine(DatabaseSettings(_env_file=None, database_url=database_url))
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


def new_lead(factory: sessionmaker[Session], kind: str = "hot") -> str:
    """A lead the default rules score as ``kind``: hot 85, warm 55, cold 15."""
    n = next(_counter)
    profiles: dict[str, dict[str, object]] = {
        "hot": {
            "full_name": f"Hot {n}",
            "email": f"hot{n}@example.com",
            "company": "Acme",
            "source": "referral",
        },
        "warm": {"full_name": f"Warm {n}", "email": f"warm{n}@example.com", "source": "web_form"},
        "cold": {"full_name": f"Cold {n}", "phone": f"+44 7700 9{n:05d}", "source": "other"},
    }
    with factory() as s:
        return create_lead(s, profiles[kind]).id


def move(factory: sessionmaker[Session], lead_id: str, target: LeadStatus) -> None:
    with factory() as s:
        lead = s.get(LeadRecord, lead_id)
        assert lead is not None
        change_status(s, lead, StatusChangeRequest(target, LeadStatus(lead.status), None))


def results(factory: sessionmaker[Session], run_id: str) -> dict[str, tuple[str, str | None]]:
    """lead_id -> (outcome, reason) for one run."""
    with factory() as s:
        return {
            item.lead_id: (item.outcome, item.reason)
            for item in s.scalars(
                select(WorkflowRunItemRecord).where(WorkflowRunItemRecord.run_id == run_id)
            )
        }


def drafts_of(factory: sessionmaker[Session], lead_id: str) -> list[LeadDraftRecord]:
    with factory() as s:
        return list(s.scalars(select(LeadDraftRecord).where(LeadDraftRecord.lead_id == lead_id)))


def count(factory: sessionmaker[Session], model: type[Any]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def wrap(monkeypatch: pytest.MonkeyPatch, name: str, after: Callable[[int], None]) -> list[int]:
    """Call ``after(n)`` after the n-th call (0-based) of the service function ``name``."""
    original = getattr(service, name)
    calls: list[int] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        calls.append(len(calls))
        after(calls[-1])
        return result

    monkeypatch.setattr(service, name, wrapper)
    return calls


def insert_run(
    factory: sessionmaker[Session], *, heartbeat_age: timedelta, mode: RunMode = LIVE
) -> str:
    now = datetime.now(UTC)
    run = WorkflowRunRecord(
        id=str(uuid.uuid4()),
        workflow=WORKFLOW_NAME,
        workflow_version=WORKFLOW_VERSION,
        rules_version="r",
        mode=mode.value,
        status="running",
        planned_count=0,
        error_code=None,
        started_at=now - heartbeat_age,
        heartbeat_at=now - heartbeat_age,
        finished_at=None,
    )
    with factory() as s:
        s.add(run)
        s.commit()
        return run.id


# --- preview ---------------------------------------------------------------------------------


def test_preview_reports_what_would_happen_and_writes_no_drafts(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    hot, warm, cold = new_lead(factory), new_lead(factory, "warm"), new_lead(factory, "cold")
    run = run_workflow(session, rules, PREVIEW)
    assert (run.mode, run.status, run.planned_count, run.error_code) == (
        "preview",
        "completed",
        3,
        None,
    )
    assert run.finished_at is not None
    assert results(factory, run.id) == {
        hot: ("would_create_draft", None),
        warm: ("skipped", "not_hot"),
        cold: ("skipped", "not_hot"),
    }
    assert count(factory, LeadDraftRecord) == 0
    # A preview never counts as processing: a live run still processes every lead.
    again = run_workflow(session, rules, PREVIEW)
    assert again.planned_count == 3
    live = run_workflow(session, rules, LIVE)
    assert live.planned_count == 3
    assert results(factory, live.id)[hot] == ("draft_created", None)


def test_preview_outcomes_never_look_like_live_ones(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    for kind in ("hot", "hot", "warm"):
        new_lead(factory, kind)
    preview = run_workflow(session, rules, PREVIEW)
    live = run_workflow(session, rules, LIVE)
    with factory() as s:
        preview_items = list(
            s.scalars(
                select(WorkflowRunItemRecord).where(WorkflowRunItemRecord.run_id == preview.id)
            )
        )
        live_items = list(
            s.scalars(select(WorkflowRunItemRecord).where(WorkflowRunItemRecord.run_id == live.id))
        )
    assert {i.outcome for i in preview_items} == {"would_create_draft", "skipped"}
    assert all(i.draft_id is None and i.mode == "preview" for i in preview_items)
    assert {i.outcome for i in live_items} == {"draft_created", "skipped"}
    assert all(i.mode == "live" for i in live_items)
    assert run_counts(session, [preview.id])[preview.id].would_create_draft == 2
    assert run_counts(session, [preview.id])[preview.id].draft_created == 0


def test_preview_reports_an_existing_pending_draft(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    hot = new_lead(factory)
    with factory() as s:
        lead = s.get(LeadRecord, hot)
        assert lead is not None
        create_draft(s, lead, DRAFT_TEMPLATE)
    run = run_workflow(session, rules, PREVIEW)
    assert results(factory, run.id) == {hot: ("skipped", "pending_draft_exists")}


# --- live: drafts only, human approval stays mandatory ----------------------------------------


def test_live_run_creates_pending_drafts_and_nothing_else(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    hot, warm = new_lead(factory), new_lead(factory, "warm")
    events_before = count(factory, LeadEventRecord)
    run = run_workflow(session, rules, LIVE)
    assert (run.status, run.planned_count) == ("completed", 2)
    [draft] = drafts_of(factory, hot)
    assert (draft.status, draft.template, draft.decided_at) == (
        "pending_review",
        DRAFT_TEMPLATE.value,
        None,
    )
    with factory() as s:
        item = s.scalar(select(WorkflowRunItemRecord).where(WorkflowRunItemRecord.lead_id == hot))
        assert item is not None
        assert (item.outcome, item.draft_id, item.score, item.tier) == (
            "draft_created",
            draft.id,
            85,
            "hot",
        )
    assert drafts_of(factory, warm) == []
    # Nothing approved, rejected, sent or changed on the leads.
    assert count(factory, OutboxMessageRecord) == 0
    assert count(factory, LeadEventRecord) == events_before
    with factory() as s:
        assert {lead.status for lead in s.scalars(select(LeadRecord))} == {"new"}


def test_the_workflow_service_has_no_way_to_decide_or_send() -> None:
    source = Path(service.__file__).read_text(encoding="utf-8")
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name | ast.Attribute)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    forbidden = {
        "approve_draft",
        "reject_draft",
        "_mark_decided",
        "OutboxMessageRecord",
        "smtplib",
        "requests",
        "httpx",
        "urllib",
        "socket",
    }
    assert not names & forbidden


def test_workflow_drafts_are_still_decided_by_a_person(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    hot = new_lead(factory)
    run_workflow(session, rules, LIVE)
    [draft] = drafts_of(factory, hot)
    with factory() as s:
        stored = get_draft(s, draft.id)
        assert stored is not None
        approve_draft(s, stored)  # the human decision, through M6
    assert count(factory, OutboxMessageRecord) == 1


# --- processed once; retries; the cap ---------------------------------------------------------


def test_a_second_live_run_skips_processed_leads(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    first_hot, warm = new_lead(factory), new_lead(factory, "warm")
    run_workflow(session, rules, LIVE)
    second = run_workflow(session, rules, LIVE)
    assert (second.status, second.planned_count) == ("completed", 0)
    assert results(factory, second.id) == {}
    later = new_lead(factory)
    third = run_workflow(session, rules, LIVE)
    assert list(results(factory, third.id)) == [later]
    assert len(drafts_of(factory, first_hot)) == 1
    assert drafts_of(factory, warm) == []


def test_runs_are_capped_and_the_rest_wait_for_the_next_run(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    leads = [new_lead(factory, "warm") for _ in range(3)]
    first = run_workflow(session, rules, LIVE, max_leads=2)
    assert first.planned_count == 2
    assert set(results(factory, first.id)) == set(leads[:2])  # oldest first
    second = run_workflow(session, rules, LIVE, max_leads=2)
    assert set(results(factory, second.id)) == {leads[2]}


@pytest.mark.parametrize("max_leads", [0, MAX_LEADS_PER_RUN + 1, True])
def test_the_cap_is_validated(session: Session, rules: ScoringRules, max_leads: int) -> None:
    with pytest.raises(ValueError, match="max_leads"):
        run_workflow(session, rules, LIVE, max_leads=max_leads)


def test_a_lead_whose_status_changed_after_selection_is_skipped(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = new_lead(factory)
    wrap(monkeypatch, "_begin", lambda _n: move(other_factory, hot, LeadStatus.CONTACTED))
    run = run_workflow(session, rules, LIVE)
    assert results(factory, run.id) == {hot: ("skipped", "not_new")}
    assert drafts_of(factory, hot) == []


def test_a_lead_that_gets_a_pending_draft_after_selection_is_skipped(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = new_lead(factory)

    def manual_draft(_n: int) -> None:
        with other_factory() as s:
            lead = s.get(LeadRecord, hot)
            assert lead is not None
            create_draft(s, lead, DRAFT_TEMPLATE)

    wrap(monkeypatch, "_begin", manual_draft)
    run = run_workflow(session, rules, LIVE)
    assert results(factory, run.id) == {hot: ("skipped", "pending_draft_exists")}
    assert len(drafts_of(factory, hot)) == 1


# --- failures: per lead, recorded, retried; never a draft without its result -----------------


def test_a_failing_lead_is_recorded_and_the_run_continues(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, ok = new_lead(factory), new_lead(factory)
    original = score_lead

    def score(lead: Any, rules: ScoringRules) -> Any:
        if lead.id == broken:
            raise ValueError("Lead source is not covered by the scoring rules")
        return original(lead, rules)

    monkeypatch.setattr(service, "score_lead", score)
    run = run_workflow(session, rules, LIVE)
    assert run.status == "completed"
    assert results(factory, run.id) == {
        broken: ("failed", "scoring_error"),
        ok: ("draft_created", None),
    }
    counts = run_counts(session, [run.id])[run.id]
    assert (counts.draft_created, counts.failed, counts.processed) == (1, 1, 2)

    monkeypatch.setattr(service, "score_lead", original)
    retry = run_workflow(session, rules, LIVE)  # failed leads are retried
    assert results(factory, retry.id) == {broken: ("draft_created", None)}


def test_a_draft_is_never_kept_without_its_result(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = new_lead(factory)
    original = service._item

    def item(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("draft_id"):
            raise RuntimeError("injected failure after the draft was staged")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_item", item)
    run = run_workflow(session, rules, LIVE)
    assert results(factory, run.id) == {hot: ("failed", "unexpected_error")}
    assert drafts_of(factory, hot) == []  # rolled back with the failed result

    monkeypatch.setattr(service, "_item", original)
    retry = run_workflow(session, rules, LIVE)
    assert results(factory, retry.id) == {hot: ("draft_created", None)}
    assert len(drafts_of(factory, hot)) == 1


def test_the_database_stops_a_lead_being_processed_twice(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = new_lead(factory)
    run_workflow(session, rules, LIVE)
    [draft] = drafts_of(factory, hot)
    with factory() as s:
        stored = get_draft(s, draft.id)
        assert stored is not None
        approve_draft(s, stored)  # no pending draft any more; the lead is still "new"
    # Bypass the service's "already processed" filter: only the database is left.
    monkeypatch.setattr(
        service,
        "_candidates",
        lambda: select(LeadRecord.id).where(LeadRecord.status == "new"),
    )
    run = run_workflow(session, rules, LIVE)
    assert results(factory, run.id) == {hot: ("failed", "concurrent_update")}
    assert [d.id for d in drafts_of(factory, hot)] == [draft.id]  # no second draft


def test_a_result_that_cannot_be_stored_fails_the_run(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, broken, never = new_lead(factory, "warm"), new_lead(factory), new_lead(factory)
    original = service._item

    def item(run: Any, lead_id: str, *args: Any, **kwargs: Any) -> Any:
        if lead_id == broken:
            raise RuntimeError("storage failure")
        return original(run, lead_id, *args, **kwargs)

    monkeypatch.setattr(service, "_item", item)
    run = run_workflow(session, rules, LIVE)
    assert (run.status, run.error_code, run.planned_count) == ("failed", "recording_failed", 3)
    assert results(factory, run.id) == {first: ("skipped", "not_hot")}
    assert drafts_of(factory, broken) == [] and drafts_of(factory, never) == []
    assert run_counts(session, [run.id])[run.id].processed == 1


def test_completion_is_refused_unless_every_lead_has_a_result(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_lead(factory, "warm")
    new_lead(factory, "warm")
    original = service._process_lead
    calls: list[str] = []

    def lossy(session: Session, run: Any, lead_id: str, *args: Any) -> bool:
        calls.append(lead_id)
        if len(calls) == 2:
            return True  # a bug that "forgets" to record this lead
        return original(session, run, lead_id, *args)

    monkeypatch.setattr(service, "_process_lead", lossy)
    run = run_workflow(session, rules, LIVE)
    # The 0004 trigger refuses "completed"; the service records the truth instead.
    assert (run.status, run.error_code) == ("failed", "recording_failed")
    assert run_counts(session, [run.id])[run.id].processed == 1


# --- one running run; interruption -------------------------------------------------------------


def test_a_second_run_cannot_start_while_one_is_running(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(2):
        new_lead(factory)
    refused: list[RunInProgressError] = []

    def start_another(n: int) -> None:
        if n == 0:
            with other_factory() as other:
                with pytest.raises(RunInProgressError) as exc_info:
                    run_workflow(other, rules, PREVIEW)
                refused.append(exc_info.value)

    wrap(monkeypatch, "_process_lead", start_another)
    run = run_workflow(session, rules, LIVE)
    assert run.status == "completed"
    assert refused[0].run_id == run.id
    assert count(factory, WorkflowRunRecord) == 1


def test_the_index_is_the_final_safeguard_for_one_running_run(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = insert_run(factory, heartbeat_age=timedelta(0))
    monkeypatch.setattr(session, "scalar", lambda *_a, **_k: None)  # "nothing is running"
    with pytest.raises(RunInProgressError):
        run_workflow(session, rules, LIVE)
    with factory() as s:
        assert [r.id for r in s.scalars(select(WorkflowRunRecord))] == [running]


def test_a_stale_running_run_is_marked_interrupted_by_the_next_run(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    stale = insert_run(factory, heartbeat_age=STALE_RUN_AFTER + timedelta(seconds=1))
    fresh_enough = timedelta(seconds=5)
    new_lead(factory)
    run = run_workflow(session, rules, LIVE)
    assert run.status == "completed"
    with factory() as s:
        old = s.get(WorkflowRunRecord, stale)
        assert old is not None
        assert (old.status, old.error_code) == ("interrupted", "stale_heartbeat")
        assert old.finished_at is not None
    # A run with a recent heartbeat is not touched.
    recent = insert_run(factory, heartbeat_age=fresh_enough)
    with pytest.raises(RunInProgressError) as exc_info:
        run_workflow(session, rules, LIVE)
    assert exc_info.value.run_id == recent


def test_an_interrupted_run_stops_and_never_claims_completion(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leads = [new_lead(factory) for _ in range(3)]

    def take_over(n: int) -> None:
        if n == 0:  # another process found this run stale and marked it interrupted
            with other_factory() as s:
                s.execute(
                    update(WorkflowRunRecord)
                    .where(WorkflowRunRecord.status == "running")
                    .values(
                        status="interrupted",
                        finished_at=datetime.now(UTC),
                        error_code="stale_heartbeat",
                    )
                )
                s.commit()

    wrap(monkeypatch, "_process_lead", take_over)
    run = run_workflow(session, rules, LIVE)
    assert (run.status, run.planned_count) == ("interrupted", 3)
    assert list(results(factory, run.id)) == [leads[0]]
    assert sum(len(drafts_of(factory, lead)) for lead in leads) == 1  # one draft, one result
    # The remaining leads are picked up by the next run, with no duplicate drafts.
    resumed = run_workflow(session, rules, LIVE)
    assert set(results(factory, resumed.id)) == set(leads[1:])
    assert all(len(drafts_of(factory, lead)) == 1 for lead in leads)


# --- reads -------------------------------------------------------------------------------------


def test_reads_list_runs_newest_first_and_results_in_order(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = new_lead(factory), new_lead(factory, "warm")
    ticks = iter(range(1, 1000))
    start = datetime.now(UTC)
    # A strictly increasing clock, so ordering never depends on timestamp resolution.
    monkeypatch.setattr(service, "_utc_now", lambda: start + timedelta(seconds=next(ticks)))
    first = run_workflow(session, rules, PREVIEW)
    second = run_workflow(session, rules, LIVE)
    assert [r.id for r in list_runs(session)] == [second.id, first.id]
    assert [r.id for r in list_runs(session, limit=1, offset=1)] == [first.id]
    assert [i.lead_id for i in list_items(session, second.id)] == [a, b]
    assert get_run(session, second.id) is not None
    assert get_run(session, second.id.upper()) is None
    assert get_run(session, "not-a-uuid") is None
    assert run_counts(session, []) == {}
    unknown = str(uuid.uuid4())
    assert run_counts(session, [unknown]) == {unknown: service.RunCounts()}


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (101, 0), (1, -1), (True, 0)])
def test_paging_is_validated(session: Session, limit: int, offset: int) -> None:
    with pytest.raises(ValueError):
        list_runs(session, limit=limit, offset=offset)
    with pytest.raises(ValueError):
        list_items(session, str(uuid.uuid4()), limit=limit, offset=offset)


def test_records_never_expose_contact_data(
    session: Session, factory: sessionmaker[Session], rules: ScoringRules
) -> None:
    hot = new_lead(factory)
    run = run_workflow(session, rules, LIVE)
    [item] = list_items(session, run.id)
    text = repr(run) + repr(item)
    with factory() as s:
        lead = s.get(LeadRecord, hot)
        assert lead is not None and lead.email is not None
        assert lead.email not in text and lead.full_name not in text


# --- defence in depth: the database and the M6 service still decide --------------------------


def test_m6_eligibility_is_still_enforced_when_creating_the_draft(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = new_lead(factory)
    move(factory, hot, LeadStatus.LOST)
    # Bypass the workflow's own checks: only the M6 draft service is left.
    monkeypatch.setattr(service, "_candidates", lambda: select(LeadRecord.id))
    monkeypatch.setattr(service, "decide", lambda **_k: Decision(True, None))
    run = run_workflow(session, rules, LIVE)
    assert results(factory, run.id) == {hot: ("skipped", "not_eligible")}
    assert drafts_of(factory, hot) == []


def test_the_pending_draft_index_is_the_final_safeguard(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = new_lead(factory)
    with factory() as s:
        lead = s.get(LeadRecord, hot)
        assert lead is not None
        existing = create_draft(s, lead, DRAFT_TEMPLATE).id
    monkeypatch.setattr(session, "scalar", lambda *_a, **_k: None)  # "no pending draft"
    run = run_workflow(session, rules, LIVE)
    assert results(factory, run.id) == {hot: ("skipped", "pending_draft_exists")}
    assert [d.id for d in drafts_of(factory, hot)] == [existing]


def _raising(exc: Exception) -> Callable[..., Any]:
    def score(*_args: Any) -> Any:
        raise exc

    return score


def _sqlite_error(name: str | None) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    if name is not None:
        error.sqlite_errorname = name
    return error


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (OperationalError("stmt", {}, _sqlite_error("SQLITE_BUSY")), "database_busy"),
        (OperationalError("stmt", {}, _sqlite_error(None)), "unexpected_error"),
        (IntegrityError("stmt", {}, sqlite3.IntegrityError("CHECK failed: x")), "unexpected_error"),
        (RuntimeError("anything"), "unexpected_error"),
    ],
)
def test_unexpected_errors_are_recorded_with_fixed_codes(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    reason: str,
) -> None:
    hot = new_lead(factory)
    monkeypatch.setattr(service, "score_lead", _raising(exc))
    run = run_workflow(session, rules, LIVE)
    assert run.status == "completed"
    assert results(factory, run.id) == {hot: ("failed", reason)}
    assert drafts_of(factory, hot) == []


@pytest.mark.parametrize("failing_score", [False, True], ids=["while-recording", "via-record"])
def test_a_run_interrupted_between_heartbeat_and_insert_stops(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
    failing_score: bool,
) -> None:
    """The insert trigger is the final safeguard if the run is interrupted mid-transaction."""
    hot = new_lead(factory)

    def interrupt(_n: int) -> None:
        with other_factory() as s:
            s.execute(
                update(WorkflowRunRecord).values(
                    status="interrupted",
                    finished_at=datetime.now(UTC),
                    error_code="stale_heartbeat",
                )
            )
            s.commit()

    wrap(monkeypatch, "_begin", interrupt)
    monkeypatch.setattr(service, "_touch", lambda *_a: True)  # the heartbeat "succeeded"
    if failing_score:
        monkeypatch.setattr(service, "score_lead", _raising(ValueError("x")))
    run = run_workflow(session, rules, LIVE)
    assert (run.status, run.error_code) == ("interrupted", "stale_heartbeat")
    assert results(factory, run.id) == {}
    assert drafts_of(factory, hot) == []


def test_a_rejected_result_row_fails_the_run(
    session: Session,
    factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_lead(factory)
    original = service._item

    def bad_item(*args: Any, **kwargs: Any) -> WorkflowRunItemRecord:
        item = original(*args, **kwargs)
        item.tier = "lukewarm"  # breaks a CHECK constraint
        return item

    monkeypatch.setattr(service, "score_lead", _raising(ValueError("x")))
    monkeypatch.setattr(service, "_item", bad_item)
    run = run_workflow(session, rules, LIVE)
    assert (run.status, run.error_code) == ("failed", "recording_failed")


def test_a_run_interrupted_before_a_failure_is_recorded_stops(
    session: Session,
    factory: sessionmaker[Session],
    other_factory: sessionmaker[Session],
    rules: ScoringRules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat check in ``_record`` stops a run taken over between two transactions."""
    hot = new_lead(factory)
    original = service._touch
    calls: list[int] = []

    def touch(session: Session, run_id: str) -> bool:
        calls.append(1)
        if len(calls) == 2:  # after the failed attempt rolled back, before its result
            with other_factory() as s:
                s.execute(
                    update(WorkflowRunRecord).values(
                        status="interrupted",
                        finished_at=datetime.now(UTC),
                        error_code="stale_heartbeat",
                    )
                )
                s.commit()
        return original(session, run_id)

    monkeypatch.setattr(service, "_touch", touch)
    monkeypatch.setattr(service, "score_lead", _raising(ValueError("x")))
    run = run_workflow(session, rules, LIVE)
    assert (run.status, run.error_code) == ("interrupted", "stale_heartbeat")
    assert results(factory, run.id) == {}
    assert drafts_of(factory, hot) == []
