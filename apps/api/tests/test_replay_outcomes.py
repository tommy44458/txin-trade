import json
from datetime import timedelta

import pytest
import replay_outcomes

from trade_helper import outcome_worker
from trade_helper.db import connect, init_db

from .test_outcome_worker import OBSERVED, add_report, fake_market, market_report

pytestmark = pytest.mark.usefixtures("pg_schema")


@pytest.fixture(autouse=True)
def schema():
    init_db()


def test_a_replay_judges_the_candidate_on_the_same_candles_as_the_original(monkeypatch, tmp_path):
    losing = {"action": "open_now", "side": "long", "entry_price": "100", "stop_loss": "99", "take_profit": "104"}
    add_report("ana_replay", market_report(losing))
    with connect() as db:
        db.execute("UPDATE analyses SET snapshot_json=? WHERE id='ana_replay'", (json.dumps({
            "candles": [], "quote": {"price": "100", "observed_at": OBSERVED.isoformat()}, "order_book": None}),))
        db.commit()
    # A dip to 98.5, then a rise to 105: the tight stop loses, a wider one wins.
    fetch, _ = fake_market(lambda index: (100.5, 98.5, 99) if index < 6 else (105, 99.5, 104.5))
    outcome_worker.run_once(now=OBSERVED + timedelta(hours=2), fetch=fetch)
    monkeypatch.setattr(replay_outcomes, "fetch_candles_range", fetch)
    wider = market_report(losing | {"stop_loss": "98"})

    def candidate(case, response_locale):
        assert case["snapshot"]["quote"]["order_book_evidence"]  # Rebuilt like the worker.
        return {"contract_valid": True, "report": wider}

    monkeypatch.setattr(replay_outcomes, "evaluate_case", candidate)
    output = tmp_path / "replay.json"
    monkeypatch.setattr("sys.argv", ["replay", "--live", "--output", str(output)])
    replay_outcomes.main()
    result = json.loads(output.read_text())
    assert result["summary"]["original"] == {"wins": 0, "losses": 1, "average_r": "-1.00", "total_r": "-1.00"}
    assert result["summary"]["candidate"] == {"wins": 1, "losses": 0, "average_r": "2.00", "total_r": "2.00"}
    assert result["summary"]["changed"] == 1


def test_a_replay_never_calls_the_model_without_live(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["replay", "--output", str(tmp_path / "x.json")])
    with pytest.raises(SystemExit):
        replay_outcomes.main()
