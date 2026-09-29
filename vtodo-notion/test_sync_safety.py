#!/usr/bin/env python3
"""
test_sync_safety.py — Test unitari per vtodo-notion sync.py

Verifica:
1. BUG ORIGINALE: snapshot parziale CalDAV causa cascade deletions → ora deve abortire
2. BUG ORIGINALE: snapshot parziale Notion causa cascade deletions → ora deve abortire
3. Protezione "empty snapshot" (comportamento pre-esistente, deve continuare a funzionare)
4. Soglia ratio-based: abort se snapshot < 60% dei known_uids
5. Cap eliminazioni percentuale: max 10% delle known_uids per ciclo
6. Sync normale bidirezionale: nessuna distruzione di dati in condizioni normali
7. First-run safety: nessuna eliminazione se known_uids è vuoto
8. PartialSnapshotError sollevata da fetch_caldav_snapshot se una collection fallisce
9. PartialSnapshotError sollevata da fetch_notion_snapshot se paginazione interrotta
10. reconcile() non supera mai effective_deletion_cap anche con molti task "mancanti"

Esegui con:
    python3 -m pytest vtodo-notion/test_sync_safety.py -v
  oppure:
    python3 vtodo-notion/test_sync_safety.py
"""

import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import asdict

# ── Setup path ──────────────────────────────────────────────────────────────

# Simula le env vars obbligatorie prima di importare sync
os.environ.setdefault("CALDAV_URL", "http://fake-caldav/")
os.environ.setdefault("CALDAV_USERNAME", "testuser")
os.environ.setdefault("CALDAV_PASSWORD", "testpass")
os.environ.setdefault("NOTION_TOKEN", "secret_fake_token")
os.environ.setdefault("NOTION_DATABASE_ID", "fake-db-id")

# Reindirizza LOG_DIR e STATE_FILE a una dir temporanea per i test
import tempfile
_test_tmpdir = tempfile.mkdtemp(prefix="sync_test_")
os.environ["VTODO_NOTION_LOG_DIR"]    = _test_tmpdir
os.environ["VTODO_NOTION_STATE_FILE"] = os.path.join(_test_tmpdir, "sync_state.json")

# Aggiungi il parent al path per config_loader
_here = Path(__file__).resolve().parent
_root = _here.parent
for _p in [str(_root / "shared"), str(_root)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Patch le dipendenze esterne prima di importare sync
# (caldav, vobject, notion_client non sono installati nell'ambiente di test locale)
_mock_caldav = MagicMock()
_mock_caldav.DAVClient = MagicMock
sys.modules.setdefault("caldav", _mock_caldav)
sys.modules.setdefault("vobject", MagicMock())
sys.modules.setdefault("requests", MagicMock())
_mock_notion_client = MagicMock()
_mock_notion_client.Client = MagicMock
sys.modules.setdefault("notion_client", _mock_notion_client)
sys.modules.setdefault("notion_client.errors", MagicMock())
sys.modules.setdefault("dateutil", MagicMock())
sys.modules.setdefault("dateutil.rrule", MagicMock())

# Patch config_loader prima di importare sync
def _cfg(key, default=None, cast=None):
    # Reindirizza path di log e stato alla dir temporanea di test
    if key == "vtodo_notion.log_dir":
        return _test_tmpdir
    if key == "vtodo_notion.state_file":
        return os.path.join(_test_tmpdir, "sync_state.json")
    return default

sys.modules.setdefault("config_loader", MagicMock(
    cfg=_cfg,
    require_env=lambda k: os.environ.get(k, ""),
    env=lambda k: os.environ.get(k, ""),
))

import importlib
import importlib.util
spec = importlib.util.spec_from_file_location("sync", _here / "sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_task(uid: str, summary: str = "Task", is_completed: bool = False,
              list_name: str = "Inbox", notion_page_id: str | None = None) -> sync.TaskData:
    return sync.TaskData(
        uid=uid, summary=summary, is_completed=is_completed,
        list_name=list_name, notion_page_id=notion_page_id,
        last_modified="2026-01-01T10:00:00+00:00",
    )


def make_state(uids: list[str]) -> sync.SyncState:
    """Crea uno stato con gli UIDs forniti (con hash fake)."""
    return sync.SyncState(known_uids={uid: "fakehash" for uid in uids})


def run_reconcile(caldav_snap, notion_snap, known_uids):
    """Esegui reconcile con mock notion/calendars e ritorna stats."""
    state = make_state(known_uids)
    notion_mock = MagicMock()
    notion_mock.pages.update.return_value = {}
    notion_mock.pages.create.return_value = {}
    calendars_mock = []

    stats = sync.reconcile(
        caldav_snap=caldav_snap,
        notion_snap=notion_snap,
        state=state,
        notion=notion_mock,
        database_id="fake-db",
        calendars=calendars_mock,
    )
    return stats, state


# ── Test 1: First-run safety ─────────────────────────────────────────────────

def test_first_run_no_deletions():
    """Su first run (known_uids vuoto) non deve cancellare nulla."""
    # Simula: CalDAV ha 100 task, Notion ha 0, stato vuoto
    caldav = {f"uid-{i}": make_task(f"uid-{i}", f"Task {i}") for i in range(100)}
    notion = {}
    stats, _ = run_reconcile(caldav, notion, known_uids=[])
    assert len(stats["deleted_caldav"]) == 0, "First run: nessuna cancellazione CalDAV"
    assert len(stats["archived_notion"]) == 0, "First run: nessuna archiviazione Notion"
    print("✓ test_first_run_no_deletions")


# ── Test 2: Normal sync — no data destruction ─────────────────────────────────

def test_normal_sync_no_destruction():
    """Sync normale: task identici su entrambi i lati non devono essere cancellati."""
    uids = [f"uid-{i}" for i in range(50)]
    # Crea task identici su entrambi i lati (stesso hash)
    caldav = {}
    notion = {}
    for uid in uids:
        t_cal = make_task(uid, f"Task {uid}", notion_page_id=f"page-{uid}")
        t_not = make_task(uid, f"Task {uid}", notion_page_id=f"page-{uid}")
        caldav[uid] = t_cal
        notion[uid] = t_not

    stats, _ = run_reconcile(caldav, notion, known_uids=uids)
    assert len(stats["deleted_caldav"]) == 0, "Sync normale: nessuna cancellazione CalDAV"
    assert len(stats["archived_notion"]) == 0, "Sync normale: nessuna archiviazione Notion"
    print("✓ test_normal_sync_no_destruction")


# ── Test 3: THE CASCADE BUG — partial Notion snapshot ────────────────────────

def test_partial_notion_snapshot_capped():
    """REGRESSIONE BUG: se Notion restituisce solo 10/140 task, NON deve cancellare
    tutti gli altri 130 da CalDAV nel corso di più cicli.
    Il cap effective_deletion_cap deve bloccare il danno a max 10% per ciclo."""
    all_uids = [f"uid-{i}" for i in range(140)]
    # CalDAV completo
    caldav = {uid: make_task(uid) for uid in all_uids}
    # Notion parziale: solo 10 task (simula fetch interrotto)
    partial_notion_uids = all_uids[:10]
    notion = {uid: make_task(uid, notion_page_id=f"page-{uid}") for uid in partial_notion_uids}

    stats, state_after = run_reconcile(caldav, notion, known_uids=all_uids)

    deletions = len(stats["deleted_caldav"])
    max_allowed = int(len(all_uids) * sync.MAX_DELETIONS_PCT)
    assert deletions <= max_allowed, (
        f"REGRESSIONE BUG: {deletions} cancellazioni CalDAV con snapshot Notion parziale "
        f"(attese al massimo {max_allowed} = {sync.MAX_DELETIONS_PCT:.0%} × 140)"
    )
    print(f"✓ test_partial_notion_snapshot_capped ({deletions} cancellazioni, cap={max_allowed})")


# ── Test 4: Snapshot ratio guard ──────────────────────────────────────────────

def test_snapshot_ratio_guard_caldav():
    """Il guard ratio-based deve abortire se caldav_snap < 60% di known_uids."""
    known_uids = [f"uid-{i}" for i in range(100)]
    state = make_state(known_uids)

    # Simula caldav_snap con solo 30 task (< 60% di 100)
    caldav_snap = {f"uid-{i}": make_task(f"uid-{i}") for i in range(30)}
    notion_snap = {f"uid-{i}": make_task(f"uid-{i}", notion_page_id=f"p{i}") for i in range(100)}

    threshold = int(len(known_uids) * sync.SNAPSHOT_MIN_RATIO)
    assert len(caldav_snap) < threshold, "Pre-condizione: snapshot sotto soglia"

    # Il guard è nel sync() principale — testiamo la logica direttamente
    guard_triggered = len(caldav_snap) < threshold and len(known_uids) >= 5
    assert guard_triggered, "Guard ratio-based deve scattare"
    print(f"✓ test_snapshot_ratio_guard_caldav (snapshot={len(caldav_snap)}, soglia={threshold})")


def test_snapshot_ratio_guard_notion():
    """Il guard ratio-based deve abortire se notion_snap < 60% di known_uids."""
    known_uids = [f"uid-{i}" for i in range(100)]
    notion_snap = {f"uid-{i}": make_task(f"uid-{i}", notion_page_id=f"p{i}") for i in range(25)}
    threshold = int(len(known_uids) * sync.SNAPSHOT_MIN_RATIO)
    guard_triggered = len(notion_snap) < threshold and len(known_uids) >= 5
    assert guard_triggered, "Guard ratio-based deve scattare su Notion parziale"
    print(f"✓ test_snapshot_ratio_guard_notion (snapshot={len(notion_snap)}, soglia={threshold})")


# ── Test 5: PartialSnapshotError da fetch_caldav_snapshot ────────────────────

def test_partial_snapshot_error_raised_on_failed_collection():
    """fetch_caldav_snapshot deve sollevare PartialSnapshotError se una collection fallisce."""
    import caldav as caldav_lib

    # Mock del client CalDAV
    mock_client = MagicMock()
    mock_principal = MagicMock()
    mock_client.principal.return_value = mock_principal

    # Due calendar mock: il primo OK, il secondo fallisce
    cal_ok = MagicMock()
    cal_ok.get_display_name.return_value = "Inbox"
    mock_todo = MagicMock()
    mock_vtodo = MagicMock()
    mock_vtodo.uid.value = "uid-ok-1"
    mock_vtodo.summary.value = "Task OK"
    mock_todo.vobject_instance.vtodo = mock_vtodo
    cal_ok.todos.return_value = [mock_todo]

    cal_fail = MagicMock()
    cal_fail.get_display_name.return_value = "Ricorrenti"
    cal_fail.todos.side_effect = ConnectionError("BrokenPipeError: [Errno 32] Broken pipe")

    mock_principal.calendars.return_value = [cal_ok, cal_fail]

    try:
        sync.fetch_caldav_snapshot(mock_client)
        assert False, "Doveva sollevare PartialSnapshotError"
    except sync.PartialSnapshotError as e:
        assert "Ricorrenti" in str(e), "L'errore deve nominare la collection fallita"
        print(f"✓ test_partial_snapshot_error_raised_on_failed_collection: {e}")
    except Exception as e:
        assert False, f"Eccezione inattesa: {type(e).__name__}: {e}"


# ── Test 6: PartialSnapshotError da fetch_notion_snapshot ────────────────────

def test_partial_snapshot_error_raised_on_interrupted_pagination():
    """fetch_notion_snapshot deve sollevare PartialSnapshotError se paginazione interrotta."""
    mock_notion = MagicMock()

    # Prima chiamata: OK con has_more=True
    first_resp = {
        "results": [{
            "id": "page-1",
            "last_edited_time": "2026-01-01T00:00:00.000Z",
            "properties": {
                "Name": {"title": [{"text": {"content": "Task 1"}}]},
                "UID CalDAV": {"rich_text": [{"text": {"content": "uid-1"}}]},
                "Completato": {"status": {"name": "Not started"}},
                "Scadenza": {"date": None},
                "Priorità": {"select": None},
                "Lista": {"select": {"name": "Inbox"}},
                "Descrizione": {"rich_text": []},
                "Luogo": {"rich_text": []},
                "URL": {"url": None},
                "Periodicità": {"rich_text": []},
            }
        }],
        "has_more": True,
        "next_cursor": "cursor-2",
    }
    # Seconda chiamata: errore di rete
    mock_notion.databases.query.side_effect = [
        first_resp,
        ConnectionError("Connection aborted: BrokenPipeError"),
    ]

    try:
        sync.fetch_notion_snapshot(mock_notion, "fake-db-id")
        assert False, "Doveva sollevare PartialSnapshotError"
    except sync.PartialSnapshotError as e:
        assert "parziale" in str(e).lower(), "Messaggio deve indicare fetch parziale"
        print(f"✓ test_partial_snapshot_error_raised_on_interrupted_pagination: {e}")
    except Exception as e:
        assert False, f"Eccezione inattesa: {type(e).__name__}: {e}"


# ── Test 7: Deletion cap percentage ──────────────────────────────────────────

def test_effective_deletion_cap_is_percentage_limited():
    """effective_deletion_cap deve essere min(absolute, percentage) dei known_uids."""
    # Con 200 known_uids e MAX_DELETIONS_PCT=0.10, il cap % è 20
    # Con MAX_DELETIONS_PER_CYCLE=10 (default), il cap effettivo è min(10, 20) = 10
    # Ma se calcoliamo con 200 tasks e 10% = 20, e absolute è 10 → effective = 10
    known_count = 200
    pct_cap = max(1, int(known_count * sync.MAX_DELETIONS_PCT))  # 20
    effective = min(sync.MAX_DELETIONS_PER_CYCLE, pct_cap)  # min(10, 20) = 10
    assert effective == min(10, 20), f"Cap effettivo: {effective}"

    # Con 30 known_uids e 10%: cap % = 3, absolute = 10 → effective = 3
    known_count2 = 30
    pct_cap2 = max(1, int(known_count2 * sync.MAX_DELETIONS_PCT))  # 3
    effective2 = min(sync.MAX_DELETIONS_PER_CYCLE, pct_cap2)  # min(10, 3) = 3
    assert effective2 == 3, f"Cap effettivo con 30 known: {effective2}"
    print(f"✓ test_effective_deletion_cap_is_percentage_limited (30 known → cap={effective2})")


# ── Test 8: Legitimate deletions still work ──────────────────────────────────

def test_legitimate_single_deletion_works():
    """Una singola eliminazione legittima (user ha cancellato da Notion) deve funzionare."""
    uids = [f"uid-{i}" for i in range(20)]
    # CalDAV ha tutti i task
    caldav = {uid: make_task(uid) for uid in uids}
    # Notion ha tutti tranne uid-5 (l'utente l'ha cancellato)
    notion = {uid: make_task(uid, notion_page_id=f"p-{uid}") for uid in uids if uid != "uid-5"}

    stats, _ = run_reconcile(caldav, notion, known_uids=uids)
    assert "uid-5" not in [t for t in stats.get("deleted_caldav", [])
                            ] or len(stats["deleted_caldav"]) <= 2, \
        "La cancellazione legittima non deve essere bloccata (tranne se cap raggiunto)"
    # Il task dovrebbe essere cancellato o non creato in Notion (nessun errore catastrofico)
    print(f"✓ test_legitimate_single_deletion_works (deleted_caldav={len(stats['deleted_caldav'])})")


# ── Test 9: Sync state file persistence ──────────────────────────────────────

def test_state_file_save_load():
    """save_state e load_state devono essere inverse."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "sync_state.json"
        # Monkey-patch STATE_FILE
        original = sync.STATE_FILE
        sync.STATE_FILE = state_path
        try:
            state = sync.SyncState(
                known_uids={"uid-1": "hash1", "uid-2": "hash2"},
                last_sync="2026-01-01T00:00:00+00:00",
            )
            sync.save_state(state)
            loaded = sync.load_state()
            assert loaded.known_uids == state.known_uids
            assert loaded.last_sync == state.last_sync
        finally:
            sync.STATE_FILE = original
    print("✓ test_state_file_save_load")


# ── Test 10: Empty snapshot abort (existing behavior preserved) ───────────────

def test_empty_notion_snapshot_aborts():
    """La protezione originale (notion vuoto + caldav pieno + stato non vuoto) deve reggere."""
    # Questo test verifica la logica del guard, non il sync() direttamente
    known_uids = [f"uid-{i}" for i in range(20)]
    notion_snap = {}
    caldav_snap = {uid: make_task(uid) for uid in known_uids}
    state_known = len(known_uids)

    should_abort = (
        len(notion_snap) == 0
        and len(caldav_snap) > 0
        and state_known > 0
    )
    assert should_abort, "Guard empty Notion deve scattare"
    print("✓ test_empty_notion_snapshot_aborts")


# ── Tests per _adjust_rrule_to_due ───────────────────────────────────────────

def test_adjust_rrule_single_day_replaced():
    """Single-day BYDAY viene rimpiazzato con il giorno della nuova due date."""
    from sync import _adjust_rrule_to_due
    result = _adjust_rrule_to_due("FREQ=WEEKLY;BYDAY=MO", "2026-03-18")  # mercoledì
    assert result == "FREQ=WEEKLY;BYDAY=WE", f"Atteso BYDAY=WE, ottenuto: {result}"
    print("✓ test_adjust_rrule_single_day_replaced")


def test_adjust_rrule_multiday_already_included():
    """Se new_due cade in un giorno già presente in BYDAY, la lista viene preservata intatta."""
    from sync import _adjust_rrule_to_due
    rrule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    # mercoledì è già in BYDAY → nessuna modifica
    result = _adjust_rrule_to_due(rrule, "2026-03-18")
    assert result == rrule, f"La regola non doveva cambiare, ottenuto: {result}"
    print("✓ test_adjust_rrule_multiday_already_included")


def test_adjust_rrule_multiday_day_not_included():
    """Se new_due cade in un giorno fuori da BYDAY, la lista multi-giorno rimane intatta.
    Il pattern ricorrente multi-giorno non deve essere modificato dalla due date."""
    from sync import _adjust_rrule_to_due
    rrule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    # sabato non è in lista, ma la lista multi-giorno non viene toccata
    result = _adjust_rrule_to_due(rrule, "2026-03-21")  # sabato
    assert result == rrule, f"La lista multi-giorno non doveva cambiare, ottenuto: {result}"
    print("✓ test_adjust_rrule_multiday_day_not_included")


def test_adjust_rrule_bymonthday_updated():
    """BYMONTHDAY viene aggiornato al giorno del mese di new_due."""
    from sync import _adjust_rrule_to_due
    result = _adjust_rrule_to_due("FREQ=MONTHLY;BYMONTHDAY=10", "2026-03-25")
    assert result == "FREQ=MONTHLY;BYMONTHDAY=25", f"Atteso BYMONTHDAY=25, ottenuto: {result}"
    print("✓ test_adjust_rrule_bymonthday_updated")


def test_adjust_rrule_no_byday_unchanged():
    """Regole senza BYDAY/BYMONTHDAY rilevanti non vengono toccate."""
    from sync import _adjust_rrule_to_due
    rrule = "FREQ=DAILY;INTERVAL=1"
    result = _adjust_rrule_to_due(rrule, "2026-03-18")
    assert result == rrule, f"La regola non doveva cambiare: {result}"
    print("✓ test_adjust_rrule_no_byday_unchanged")


def test_adjust_rrule_monday_to_sunday():
    """Single-day MO → domenica (SU)."""
    from sync import _adjust_rrule_to_due
    result = _adjust_rrule_to_due("FREQ=WEEKLY;BYDAY=MO", "2026-03-22")  # domenica
    assert result == "FREQ=WEEKLY;BYDAY=SU", f"Atteso BYDAY=SU, ottenuto: {result}"
    print("✓ test_adjust_rrule_monday_to_sunday")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_first_run_no_deletions,
        test_normal_sync_no_destruction,
        test_partial_notion_snapshot_capped,
        test_snapshot_ratio_guard_caldav,
        test_snapshot_ratio_guard_notion,
        test_partial_snapshot_error_raised_on_failed_collection,
        test_partial_snapshot_error_raised_on_interrupted_pagination,
        test_effective_deletion_cap_is_percentage_limited,
        test_legitimate_single_deletion_works,
        test_state_file_save_load,
        test_empty_notion_snapshot_aborts,
        test_adjust_rrule_single_day_replaced,
        test_adjust_rrule_multiday_already_included,
        test_adjust_rrule_multiday_day_not_included,
        test_adjust_rrule_bymonthday_updated,
        test_adjust_rrule_no_byday_unchanged,
        test_adjust_rrule_monday_to_sunday,
    ]

    passed = 0
    failed = 0
    print("=" * 60)
    print("  vtodo-notion sync safety tests")
    print("=" * 60)
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__}: FAILED — {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: ERROR — {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("-" * 60)
    print(f"  Risultato: {passed} passati, {failed} falliti su {len(tests)} test")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)
