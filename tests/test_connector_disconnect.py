"""Long-poll после закрытия клиента не резервирует следующую команду."""

import asyncio


async def test_disconnected_poll_does_not_lease_new_work(tmp_path):
    from atlas_gate.gate.node_channel import Channel
    from atlas_gate.gate.store import Store
    store=Store(str(tmp_path/'channel.db'))
    channel=Channel(store)
    task=asyncio.create_task(channel.request('node','GET','/v1/node'))
    await asyncio.sleep(0)
    async def disconnected(): return True
    try:
        assert await channel.poll('node',wait=0,disconnected=disconnected) is None
        assert store._one('SELECT state FROM node_work')['state']=='queued'
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        store.close()
