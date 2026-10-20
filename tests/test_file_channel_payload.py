import asyncio
import base64
import json
import stat

import pytest
from atlas_gate.gate.store import Store
from atlas_gate.gate.node_channel import Channel
from atlas_gate.gate.errors import GateError


async def test_large_file_queue_metadata_stays_small_and_payload_survives_replay(tmp_path):
    store=Store(str(tmp_path/'gate.db'))
    channel=Channel(store)
    raw=b'large original request' + b'x'*(33*1024*1024)
    task=asyncio.create_task(channel.request('a','POST','/v1/sessions/s/files',content=raw,
                                           headers={'Idempotency-Key':'upload'}))
    try:
        await asyncio.sleep(0)
        row=store._one('SELECT * FROM node_work')
        assert len(row['request']) < 1024
        pointer=json.loads(row['request'])['body_file']
        path=channel.payloads/(pointer+'.bin')
        assert path.read_bytes()==raw
        assert stat.S_IMODE(path.stat().st_mode)==0o600
        work=await channel.poll('a',wait=0)
        assert base64.b64decode(work['body'])==raw
        with pytest.raises(GateError):channel.complete('b',work['id'],200,b'{}')
        assert path.exists()
        store._exec('UPDATE node_work SET lease=0')
        restored=Channel(store)
        assert await restored.poll('a',wait=0)==work
        restored.complete('a',work['id'],200,b'{}')
        assert (await task).status_code==200
        assert not path.exists()
        restored.complete('a',work['id'],200,b'{}')  # lost ACK: immutable replay
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True);store.close()
