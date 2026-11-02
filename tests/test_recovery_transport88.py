import asyncio
import json

import pytest

from atlas_gate.gate.errors import GateError
from atlas_gate.gate.node_channel import Channel
from atlas_gate.gate.receipts import completed_response
from atlas_gate.gate.store import Store


async def test_legacy_channel_and_header_receipt_survive_upgrade(tmp_path):
    store = Store(str(tmp_path / 'gate.db'))
    channel = Channel(store)
    task = asyncio.create_task(channel.request('node', 'GET', '/v1/sessions/s/sdk-tools'))
    try:
        await asyncio.sleep(0)
        work = await channel.poll('node', wait=0)  # old worker remains supported
        channel.complete('node', work['id'], 429, b'{"error":"busy"}', {
            'Retry-After': 'Thu, 05 Nov 2026 12:00:00 GMT', 'X-Atlas-Command-State': 'complete',
            'Set-Cookie': 'private', 'Authorization': 'must-drop'})
        response = await task
        assert response.headers['Retry-After'] == 'Thu, 05 Nov 2026 12:00:00 GMT'
        assert response.headers['X-Atlas-Command-State'] == 'complete'
        assert 'Set-Cookie' not in response.headers and 'Authorization' not in response.headers
        # Lost connector ACK reads the exact persisted headers, never recomputed time.
        channel.complete('node', work['id'], 429, b'{"error":"busy"}', {
            'retry-after': response.headers['Retry-After'], 'x-atlas-command-state': 'complete'})
        with pytest.raises(GateError):
            channel.complete('node', work['id'], 429, b'{"error":"busy"}', {'Retry-After': '1'})
        fresh = Channel(store)
        assert json.loads(store._one('SELECT response_headers FROM node_work')['response_headers'])['retry-after'] == response.headers['Retry-After']
    finally:
        task.cancel(); await asyncio.gather(task, return_exceptions=True); store.close()


async def test_protocol2_work_cannot_be_consumed_by_old_connector(tmp_path):
    store = Store(str(tmp_path / 'gate.db')); channel = Channel(store)
    task = asyncio.create_task(channel.request('node', 'POST', '/v1/recovery/cases',
        content=b'{}', headers={'Idempotency-Key': 'case'}))
    try:
        await asyncio.sleep(0)
        assert await channel.poll('node', wait=0, protocol=1) is None
        work = await channel.poll('node', wait=0, protocol=2)
        with pytest.raises(GateError):
            channel.complete('node', work['id'], 200, b'{}')
        channel.complete('node', work['id'], 200, b'{}', {'X-Atlas-Transport-Contract': '2'})
        assert (await task).headers['x-atlas-transport-contract'] == '2'
    finally:
        task.cancel(); await asyncio.gather(task, return_exceptions=True); store.close()


def test_command_receipt_keeps_protocol_headers_without_secrets():
    saved = {'state': 'complete', 'status': 503, 'requestId': 'r', 'response': {'error': 'busy'},
             'headers': {'Retry-After': '9', 'Authorization': 'must-drop'}}
    response = completed_response(saved, 'r')
    assert response.headers['Retry-After'] == '9' and 'Authorization' not in response.headers
    assert completed_response({**saved, 'headers': {'Retry-After': '1\r\nInjected: yes'}}, 'r') is None
