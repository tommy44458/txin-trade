import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from trade_helper import worker
from trade_helper.agent import fallback_analysis
from trade_helper.api import app
from trade_helper.db import connect, init_db
from trade_helper.prompts_artifacts import load_prompt_artifact

from .test_analysis import sample_candles


def test_language_preference_persists_without_changing_trading_preferences_or_credentials():
    with TestClient(app) as client:
        original = client.get('/api/v1/settings').json()
        assert original['ui_locale'] == 'zh-TW'
        changed = client.patch('/api/v1/settings', json={'ui_locale': 'en-US'}).json()
        assert changed['ui_locale'] == 'en-US'
        for key in ('trading_preferences', 'favorite_market_ids', 'integrations', 'model_provider', 'model'):
            assert changed[key] == original[key]
        assert client.patch('/api/v1/settings', json={'ui_locale': 'fr-FR'}).status_code == 422
        assert client.get('/api/v1/settings').json()['ui_locale'] == 'en-US'
        client.put('/api/v1/settings', json={'model_provider': 'codex'})
        assert client.get('/api/v1/settings').json()['ui_locale'] == 'en-US'


def test_analysis_locale_is_an_idempotent_input_and_artifact_is_private_and_immutable():
    with TestClient(app) as client:
        body = {'market_id': 'binance:perp:BTCUSDT', 'timeframe': '12h',
                'risk_tolerance': 'high', 'trading_style': 'left', 'output_locale': 'en-US'}
        headers = {'Idempotency-Key': 'bilingual-stable-input'}
        first = client.post('/api/v1/analyses', json=body, headers=headers)
        assert first.status_code == 202
        assert first.json()['output_locale'] == 'en-US'
        again = client.post('/api/v1/analyses', json=body, headers=headers)
        assert again.json()['id'] == first.json()['id']
        assert client.post('/api/v1/analyses', json=body | {'output_locale': 'zh-TW'}, headers=headers).status_code == 409
        assert client.post('/api/v1/analyses', json=body | {'output_locale': 'fr'}, headers={'Idempotency-Key': 'invalid-locale-input'}).status_code == 422
        assert 'instructions' not in first.text and 'metadata_json' not in first.text
        artifact_id = first.json()['prompt_artifact_id']
        with connect(readonly=True) as db:
            bundle = load_prompt_artifact(db, artifact_id)
            count = db.execute('SELECT count(*) AS n FROM prompt_artifacts').fetchone()['n']
        assert bundle.prompt_locale == 'en-US' and bundle.response_locale == 'en-US'
        assert count == 1
        with pytest.raises(sqlite3.IntegrityError, match='immutable'), connect() as db:
            db.execute('UPDATE prompt_artifacts SET instructions=? WHERE id=?', ('changed', artifact_id))
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'), connect() as db:
            db.execute('DELETE FROM prompt_artifacts WHERE id=?', (artifact_id,))


def test_worker_uses_submitted_instructions_when_locale_and_registry_change(monkeypatch):
    with TestClient(app) as client:
        body = {'kind': 'market', 'market_id': 'binance:perp:BTCUSDT', 'timeframe': '1h',
                'output_locale': 'en-US', 'risk_tolerance': 'high', 'trading_style': 'left'}
        queued = client.post('/api/v1/analyses', json=body, headers={'Idempotency-Key': 'frozen-english-analysis'}).json()
        with connect(readonly=True) as db:
            saved = load_prompt_artifact(db, queued['prompt_artifact_id'])
        client.patch('/api/v1/settings', json={'ui_locale': 'zh-TW'})
        monkeypatch.setattr('trade_helper.prompts.resolve_prompt', lambda *_args, **_kwargs: pytest.fail('Queued job must not compile current prompts'))
        monkeypatch.setattr(worker, 'fetch_candles', lambda _, timeframe, *, limit: sample_candles(recent=True, timeframe=timeframe))
        monkeypatch.setattr(worker, 'fetch_forming_candle', lambda *_: None)
        monkeypatch.setattr(worker, 'fetch_order_book', lambda *_: None)
        monkeypatch.setattr(worker, 'fetch_quote', lambda *_: {'price': '120', 'mark_price': '120', 'observed_at': datetime.now(UTC).isoformat()})
        monkeypatch.setattr(worker, 'fetch_tick_size', lambda *_: Decimal('.1'))
        received = []

        def model(request, candles, quote, context, positions, *, prepared_trace, prompt_bundle, on_text=None):
            assert request['output_locale'] == 'en-US'
            assert prompt_bundle.to_dict() == saved.to_dict()
            received.append(prompt_bundle.rendered_sha256)
            decision = fallback_analysis(request, candles, quote, context_candles=context, positions=positions)
            decision['mode'] = 'openai_assisted'
            decision['tool_trace'] = prepared_trace
            return decision

        monkeypatch.setattr(worker, 'analyze_with_tools', model)
        assert worker.run_once()
        completed = client.get('/api/v1/analyses/' + queued['id']).json()
        assert completed['status'] == 'completed', completed['error']
        assert completed['report']['output_locale'] == 'en-US'
        assert completed['report']['analysis_execution']['prompt_bundle']['rendered_sha256'] == saved.rendered_sha256
        assert received == [saved.rendered_sha256]
        assert client.get('/api/v1/settings').json()['ui_locale'] == 'zh-TW'


def test_legacy_report_read_and_idempotent_replay_do_not_rewrite_it():
    init_db()
    request = {'kind': 'market', 'market_id': 'binance:perp:BTCUSDT', 'timeframe': '1h',
               'directional_bias': None, 'risk_tolerance': None, 'trading_style': None,
               'leverage': 5, 'position_ids': [], 'account_equity_usdt': None}
    report = {'summary': '保留原本中文報告', 'historical_version': 1}
    with connect() as db:
        db.execute("""INSERT INTO analyses(id,user_id,idempotency_key,request_hash,request_json,
                   status,phase,report_json,created_at) VALUES('old','local-demo','legacy-bilingual-key',
                   'old-hash',?,'completed','done',?,'2026-09-30T00:00:00+00:00')""", (json.dumps(request), json.dumps(report)))
    with TestClient(app) as client:
        client.patch('/api/v1/settings', json={'ui_locale': 'en-US'})
        replay = client.post('/api/v1/analyses', json=request, headers={'Idempotency-Key': 'legacy-bilingual-key'}).json()
        assert replay['id'] == 'old' and replay['output_locale'] == 'zh-TW'
        assert replay['report'] == report and replay['submitted_input'] == request
        assert replay['prompt_artifact_id'] is None
        with connect(readonly=True) as db:
            assert db.execute('SELECT count(*) AS n FROM prompt_artifacts').fetchone()['n'] == 0


def test_translation_queue_gets_priority_even_when_market_and_discussion_queues_are_busy(monkeypatch):
    from trade_helper import macro_interpretation

    calls = []
    monkeypatch.setattr(worker, 'run_once', lambda: calls.append('analysis') or True)
    monkeypatch.setattr(worker, 'run_discussion_once', lambda: calls.append('discussion') or True)
    monkeypatch.setattr(macro_interpretation, 'run_macro_translation_once', lambda: calls.append('translation') or True)
    for options in ({}, {'discussions_first': True}, {'translations_first': True}):
        assert worker.run_work_once(**options)
    assert calls == ['analysis', 'discussion', 'translation']
