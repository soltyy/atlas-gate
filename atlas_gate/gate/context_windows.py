"""Авторитетные окна Router: порядок наблюдений, свежесть и проекция route/model."""
from __future__ import annotations
import asyncio
import time
from urllib.parse import quote

REFRESH_SECONDS = 30
FRESH_SECONDS = 300


def positive(value):
    return value if type(value) is int and value > 0 else None


class ContextWindows:
    def __init__(self, state):
        self.state = state
        state.store._exec("""CREATE TABLE IF NOT EXISTS router_context_windows (
            node_id TEXT NOT NULL, model TEXT NOT NULL, boot_id TEXT NOT NULL,
            effective INTEGER, full INTEGER, source TEXT NOT NULL,
            binding_created REAL NOT NULL, turn_created REAL NOT NULL, request_order INTEGER NOT NULL,
            revision INTEGER, observed REAL NOT NULL, PRIMARY KEY(node_id,model))""")

    def save(self, node_id, model, boot, effective, full, source, epoch, order, revision=None):
        state = self.state
        info = state.nodes.snapshots.get(node_id)
        if not info or info.get('bootId') != boot:
            return False
        with state.store._lock:
            db = state.store._db
            old = db.execute('SELECT * FROM router_context_windows WHERE node_id=? AND model=?', (node_id,model)).fetchone()
            if old and old['boot_id'] == boot:
                if old['source'] == 'router-catalog' and source != 'router-catalog' and old['effective'] is not None and time.time()-old['observed'] <= FRESH_SECONDS:
                    return False
                if source == old['source'] == 'router-catalog' and revision < old['revision']:
                    return False
                if source == old['source'] and (epoch[0],epoch[1],order) <= (old['binding_created'],old['turn_created'],old['request_order']):
                    return False
            db.execute('INSERT INTO router_context_windows VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(node_id,model) DO UPDATE SET boot_id=excluded.boot_id,effective=excluded.effective,full=excluded.full,source=excluded.source,binding_created=excluded.binding_created,turn_created=excluded.turn_created,request_order=excluded.request_order,revision=excluded.revision,observed=excluded.observed',
                (node_id,model,boot,effective,full,source,*epoch,order,revision,time.time()))
        return True

    def observe(self, binding, context, order, turn_created=None):
        if not isinstance(context,dict):
            return False
        model = binding['model']
        info = self.state.nodes.snapshots.get(binding['node_id']) or {}
        names = {model} | {m.get('resolved') for m in info.get('models',[]) if m.get('value') == model}
        if context.get('model') not in names:
            return False
        # maxTokens is the effective capacity already normalized by Router, never total usage.
        value = positive(context.get('maxTokens'))
        if turn_created is None:
            row = self.state.store._one('SELECT MAX(created) AS ts FROM router_operations WHERE session_id=? AND turn_id IS NOT NULL AND finished=1', (binding['id'],))
            turn_created = (row or {}).get('ts') or 0
        return self.save(binding['node_id'],model,binding['boot_id'],value,None,'session-sdk',
                         (binding['created'],turn_created),order)

    def catalog(self):
        for node_id, info in self.state.nodes.snapshots.items():
            for model in info.get('models',[]):
                data = model.get('contextWindow')
                if not isinstance(data,dict) or data.get('source') != 'sdk':
                    continue
                revision = data.get('revision')
                if type(revision) is not int or revision < 0:
                    continue
                effective = positive(data.get('effectiveTokens'))
                full = positive(data.get('fullTokens'))
                if full is not None and effective is not None and full < effective:
                    effective, full = None, None
                self.save(node_id,model['value'],info['bootId'],effective,full,'router-catalog',(0,0),time.time_ns(),revision)

    def project(self, org, route, model):
        nodes = self.state.nodes
        candidates = [n for n in nodes.config.values() if n.enabled and not nodes.revoked(n.node_id)
            and org in n.orgs and route in n.routes and model in {m.get('value') for m in nodes.catalog.get(n.node_id,[])}]
        values, fulls, sources = [], [], []
        status = 'unknown'
        for node in candidates:
            row = self.state.store._one('SELECT * FROM router_context_windows WHERE node_id=? AND model=?',(node.node_id,model))
            info = nodes.snapshots.get(node.node_id)
            if not row:
                return {'context_window':None,'context_full_window':None,'context_status':'unknown','limits_source':'unknown','context_source':'unknown'}
            if not info or row['boot_id'] != info.get('bootId') or time.monotonic()-info.get('observed',0) > FRESH_SECONDS:
                status = 'unavailable'
            elif time.time()-row['observed'] > FRESH_SECONDS:
                status = 'stale'
            elif row['effective'] is None:
                status = 'unknown'
            else:
                values.append(row['effective']);fulls.append(row['full']);sources.append(row['source']);continue
            return {'context_window':None,'context_full_window':None,'context_status':status,'limits_source':'unknown','context_source':'unknown'}
        return {'context_window':min(values) if values else None,
                'context_full_window':min(fulls) if fulls and None not in fulls else None,
                'context_status':'current' if values else status,'limits_source':'router-sdk' if values else 'unknown',
                'context_source':'router-catalog' if sources and all(s=='router-catalog' for s in sources) else 'session-sdk' if values else 'unknown'}

    async def poll(self):
        await asyncio.wait_for(self.state.nodes.refresh(),20)
        self.catalog()
        rows = self.state.store._all("SELECT * FROM router_bindings WHERE status='ready' ORDER BY created DESC")
        selected = {}
        for row in rows:
            selected.setdefault((row['node_id'],row['model']),row)
        async def sample(binding):
            order = time.time_ns()
            try:
                info = self.state.nodes.snapshots.get(binding['node_id'])
                if not info or info['bootId'] != binding['boot_id']:
                    return
                r = await asyncio.wait_for(self.state.nodes.request(binding['node_id'],'GET','/v1/sessions/'+quote(binding['local_id'],safe='')+'/context',expected_boot=binding['boot_id']),20)
                if r.status_code == 200:
                    self.observe(binding,r.json(),order)
            except Exception:
                # A missing session is not permission to start/resume an SDK process.
                return
        await asyncio.gather(*(sample(b) for b in selected.values()))
        await self.state.publish_contexts()

    async def run(self):
        import logging
        await self.state.ready.wait()
        while True:
            try:
                await self.poll()
            except Exception:
                logging.getLogger('atlas_gate').exception('context synchronization failed')
                await self.state.publish_contexts()
            await asyncio.sleep(REFRESH_SECONDS)
