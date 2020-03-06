import os
import copy
import glob
import json
import shutil
import itertools
import contextlib

import synapse.exc as s_exc
import synapse.cortex as s_cortex
import synapse.common as s_common

import synapse.tests.utils as s_t_utils

import synapse.lib.cell as s_cell
import synapse.lib.msgpack as s_msgpack
import synapse.lib.stormsvc as s_stormsvc

import synapse.tools.migrate_020 as s_migr

REGR_VER = '0.1.51-migr'

# Nodes that are expected to be unmigratable
NOMIGR_NDEF = [
    ["migr:test", 22],
    ["test:int", 10],
]

# Error log to be generated by migr:test
# .created val removed since this will update when the regression repo is updated
MIGR_ERR = {
    'migrop': 'nodes',
    'logtyp': 'error',
    'key': b'\xc8ak\x08\xb8\x8fJ\xcd\xad/\xc4F\x08\x7f@\xc9k\xf8\xc4\xca\x807|\xacK\xe5\xff<\x88+7\xef',
    'val': {
        'mesg': "Unable to determine stortype for migr:test: 'NoneType' object has no attribute 'type'",
        'node': (
            b'\xc8ak\x08\xb8\x8fJ\xcd\xad/\xc4F\x08\x7f@\xc9k\xf8\xc4\xca\x807|\xacK\xe5\xff<\x88+7\xef',
            {
                'ndef': ('*migr:test', 22),
                'props': {'bar': 'spam'},
                'tags': {}, 'tagprops': {}
            }
        )
    }
}

def getAssetBytes(*paths):
    fp = os.path.join(*paths)
    assert os.path.isfile(fp)
    with open(fp, 'rb') as f:
        byts = f.read()
    return byts

def getAssetJson(*paths):
    byts = getAssetBytes(*paths)
    obj = json.loads(byts.decode())
    return obj

def tupleize(obj):
    '''
    Convert list objects to tuples in a nested python struct.
    '''
    if isinstance(obj, (list, tuple)):
        return tuple([tupleize(o) for o in obj])
    if isinstance(obj, dict):
        return {k: tupleize(v) for k, v in obj.items()}
    return obj

# NOTE: This service is identical to the regression repo except the confdef has been
# updated for jsonschema and a new test value
class MigrSvcApi(s_stormsvc.StormSvc, s_cell.CellApi):
    _storm_svc_name = 'turtle'
    _storm_svc_pkgs = ({  # type: ignore
        'name': 'turtle',
        'version': (0, 0, 1),
        'commands': ({'name': 'newcmd', 'storm': '[ inet:fqdn=$lib.service.get($cmdconf.svciden).test() ]'},),
    },)

    async def test(self):
        return await self.cell.test()

class MigrStormsvc(s_cell.Cell):
    cellapi = MigrSvcApi
    confdefs = (
        ('myfqdn', {'type': 'string', 'default': 'snoop.io', 'description': 'A test fqdn'}),  # type: ignore
    )

    async def __anit__(self, dirn, conf=None):
        await s_cell.Cell.__anit__(self, dirn, conf=conf)
        self.myfqdn = self.conf.get('myfqdn')

    async def test(self):
        return self.myfqdn

class MigrationTest(s_t_utils.SynTest):

    @contextlib.asynccontextmanager
    async def _getTestMigrCore(self, conf):
        '''
        Use regression cortex as the base migration dirn.

        Args:
            conf (dict): Migration tool configuration

        Yields:
            (list): List of podes in the cortex
            (s_migr.Migrator): Migrator service object
            (tuple): test data, dest dirn, dest local layers, Migrator
        '''
        # get test data
        tdata = {}
        with self.getRegrDir('assets', REGR_VER) as assetdir:
            podesj = getAssetJson(assetdir, 'podes.json')
            ndj = getAssetJson(assetdir, 'nodedata.json')

        # strip out data we don't expect to migrate
        podesj = [p for p in podesj if p[0] not in NOMIGR_NDEF]
        ndj = [nd for nd in ndj if nd[0] not in NOMIGR_NDEF]

        tdata['podes'] = tupleize(podesj)
        tdata['nodedata'] = tupleize(ndj)

        # initialize migration tool
        with self.getRegrDir('cortexes', REGR_VER) as src:
            with self.getTestDir(copyfrom=conf.get('dest')) as dest:
                tconf = copy.deepcopy(conf)
                tconf['src'] = src
                tconf['dest'] = dest

                locallyrs = os.listdir(os.path.join(src, 'layers'))

                async with await s_migr.Migrator.anit(tconf) as migr:
                    yield tdata, dest, locallyrs, migr

    async def _checkStats(self, tdata, migr, locallyrs):
        '''
        Verify that the stats for what data has been migrated matches the test data
        '''
        tpodes = tdata['podes']
        tnodedata = tdata['nodedata']

        ipv4_cnt = len([x for x in tpodes if x[0][0] == 'inet:ipv4'])
        bytes_cnt = len([x for x in tpodes if x[0][0] == 'file:bytes'])
        tag_cnt = len([x for x in tpodes if x[0][0] == 'syn:tag'])

        nodedata_cnt = sum([len(x[1]) for x in tnodedata])

        ipv4_migr = [0, 0]
        bytes_migr = [0, 0]
        tag_migr = [0, 0]
        totnodes_migr = 0

        totnodedata_migr = 0

        for iden in locallyrs:
            stats = [log async for log in migr._migrlogGet('nodes', 'stat', f'{iden}:form')]
            self.gt(len(stats), 0)
            for stat in stats:
                skey = stat['key']
                sval = stat['val']  # (src_cnt, dest_cnt)

                if skey.endswith('inet:ipv4'):
                    ipv4_migr[0] += sval[0]
                    ipv4_migr[1] += sval[1]
                elif skey.endswith('file:bytes'):
                    bytes_migr[0] += sval[0]
                    bytes_migr[1] += sval[1]
                elif skey.endswith('syn:tag'):
                    tag_migr[0] += sval[0]
                    tag_migr[1] += sval[1]

            totnodes = [log async for log in migr._migrlogGet('nodes', 'stat', f'{iden}:totnodes')]
            totnodes_migr += totnodes[0]['val'][1]  # dest cnt

            totnodedata = [log async for log in migr._migrlogGet('nodedata', 'stat', f'{iden}:totnodes')]
            totnodedata_migr += totnodedata[0]['val'][1]  # dest cnt

        # check that sums from the layer migrations add up
        self.eq((ipv4_cnt, ipv4_cnt), ipv4_migr)
        self.eq((bytes_cnt, bytes_cnt), bytes_migr)
        self.eq((tag_cnt, tag_cnt), tag_migr)
        self.eq(len(tpodes), totnodes_migr)

        self.eq(nodedata_cnt, totnodedata_migr)

    async def _checkCore(self, core, tdata):
        '''
        Verify data in the migrated to 0.2.x cortex.
        '''

        # test validation data
        tpodes = tdata['podes']
        tnodedata = tdata['nodedata']

        # check all nodes
        nodes = await core.nodes('.created -meta:source:name=test')

        podes = [n.pack(dorepr=True) for n in nodes]
        self.gt(len(podes), 0)

        try:
            self.eq(podes, tpodes)
        except AssertionError:
            # print a more useful diff on error
            notincore = list(itertools.filterfalse(lambda x: x in podes, tpodes))
            self.eq([], notincore)
            notintest = list(itertools.filterfalse(lambda x: x in tpodes, podes))
            self.eq([], notintest)
            raise

        nodedata = []
        for n in nodes:
            nodedata.append([n.ndef, [nd async for nd in n.iterData()]])
        nodedata = tupleize(nodedata)
        try:
            self.eq(nodedata, tnodedata)
        except AssertionError:
            # print a more useful diff on error
            notincore = list(itertools.filterfalse(lambda x: x in nodedata, tnodedata))
            self.eq([], notincore)
            notintest = list(itertools.filterfalse(lambda x: x in tnodedata, nodedata))
            self.eq([], notintest)
            raise

        # manually check node subset
        self.len(1, await core.nodes('inet:ipv4=1.2.3.4'))
        self.len(2, await core.nodes('inet:dns:a:ipv4=1.2.3.4'))

        # check that triggers are active
        await core.auth.getUserByName('root')
        self.len(2, await core.eval('syn:trigger').list())
        tnodes = await core.nodes('[ inet:ipv4=9.9.9.9 ]')
        self.nn(tnodes[0].tags.get('trgtag'))

    async def _checkAuth(self, core):
        defview = await core.hive.get(('cellinfo', 'defaultview'))
        deflyr = core.getLayer().iden
        secview = [k for k in core.views.keys() if k != defview][0]
        seclyr = core.views[secview].layers[0].iden
        self.ne(deflyr, seclyr)  # check to make sure we got the second layer

        # data check auth layout (users, passwords, rules, admin, etc.)
        self.sorteq(list(core.auth.usersbyname.keys()), ['root', 'fred', 'bobo'])
        self.sorteq(list(core.auth.rolesbyname.keys()), ['all', 'cowboys', 'ninjas', 'friends'])

        self.len(12, core.auth.authgates)  # 2 views, 2 layers, 2 triggers, 1 cortex, 3 queues, 2 crons
        self.len(2, [iden for iden, gate in core.auth.authgates.items() if gate.type == 'view'])
        self.len(2, [iden for iden, gate in core.auth.authgates.items() if gate.type == 'layer'])
        self.len(1, [iden for iden, gate in core.auth.authgates.items() if gate.type == 'cortex'])
        self.len(3, [iden for iden, gate in core.auth.authgates.items() if gate.type == 'queue'])
        self.len(2, [iden for iden, gate in core.auth.authgates.items() if gate.type == 'cronjob'])

        # user attributes
        root = core.auth.usersbyname['root']
        self.true(root.info.get('admin'))
        self.sorteq(['all'], [core.auth.rolesbyiden[riden].name for riden in root.info.get('roles')])
        self.len(0, root.info.get('rules'))

        bobo = core.auth.usersbyname['bobo']
        self.false(bobo.info.get('admin'))
        self.sorteq(['all', 'friends'], [core.auth.rolesbyiden[riden].name for riden in bobo.info.get('roles')])
        boborules = [(True, ('node', 'tag', 'add', 'bobotag')), (True, ('queue', 'get')), (True, ('queue', 'boboq')),
                     (True, ('queue', 'put'))]
        self.sorteq(boborules, bobo.info.get('rules', []))

        fred = core.auth.usersbyname['fred']
        self.false(fred.info.get('admin'))
        self.sorteq(['all', 'ninjas'], [core.auth.rolesbyiden[riden].name for riden in fred.info.get('roles')])
        fredrules = [(True, ('node', 'tag', 'add', 'trgtag')), (True, ('trigger', 'add')), (True, ('trigger', 'get')),
                     (True, ('queue', 'get')), (True, ('queue', 'add')), (True, ('cron',))]
        self.sorteq(fredrules, fred.info.get('rules', []))

        # role attributes
        friends = core.auth.rolesbyname['friends']
        friendrules = [(True, ('queue', 'fredq', 'get')), (True, ('cron', 'get'))]
        self.sorteq(friendrules, friends.info.get('rules', []))
        friendrules_seclyr = [(True, ('node', 'add')), (True, ('node', 'prop', 'set')), (True, ('layer', 'lift'))]
        self.sorteq(friendrules_seclyr, friends.authgates[seclyr].get('rules'))

        # load vals for user perm tests
        tagtrg = (await core.nodes('syn:trigger:cond=tag:add'))[0].ndef[1]
        nodetrg = (await core.nodes('syn:trigger:cond=node:add'))[0].ndef[1]

        crons = await core.listCronJobs()
        fredcron = [c for c in crons if c['creator'] == fred.iden][0]
        bobocron = [c for c in crons if c['creator'] == bobo.iden][0]

        # user permissions
        # bobo
        # - read main view
        # - read/write to forked view via role
        # - no access to triggers
        # - can add/del bobotag but not trgtag
        # - has queue get/put but not queue add
        # - cron get via friends role
        async with core.getLocalProxy(user='bobo') as proxy:
            self.gt(await proxy.count('inet:ipv4'), 0)
            await self.asyncraises(s_exc.AuthDeny, proxy.count('[inet:ipv4=10.10.10.10]'))

            self.eq(2, await proxy.count('syn:trigger'))
            await self.asyncraises(s_exc.StormRuntimeError, proxy.count(f'trigger.del {tagtrg}'))
            await self.asyncraises(s_exc.StormRuntimeError, proxy.count(f'trigger.del {nodetrg}'))
            trigadd = 'trigger.add node:add --form file:bytes --query {[+#bobotag]}'
            await self.asyncraises(s_exc.AuthDeny, proxy.count(f'{trigadd}'))
            self.eq(2, await proxy.count('syn:trigger'))

            self.gt(await proxy.count('inet:ipv4 [+#bobotag]'), 0)
            await self.asyncraises(s_exc.AuthDeny, proxy.count('#trgtag [-#trgtag]'))
            await self.asyncraises(s_exc.AuthDeny, proxy.count('inet:ipv4 [+#newbobotag]'))

            self.gt(await proxy.count('inet:ipv4', opts={'view': secview}), 0)
            self.gt(await proxy.count('[inet:ipv4=10.9.10.1]', opts={'view': secview}), 0)

            await self.asyncraises(s_exc.AuthDeny, proxy.count(f'queue.add newboboq'))

            await proxy.count(f'$q = $lib.queue.get(boboq) inet:ipv4=1.2.3.4 $q.put($node.repr())')
            self.eq(1, await proxy.count(f'$q = $lib.queue.get(boboq) ($offs, $ipv4) = $q.get(0) inet:ipv4=$ipv4'))

            await proxy.count(f'$q = $lib.queue.get(fredq) inet:ipv4=1.2.3.4 $q.put($node.repr())')
            self.eq(1, await proxy.count(f'$q = $lib.queue.get(fredq) ($offs, $ipv4) = $q.get(0) inet:ipv4=$ipv4'))

            self.len(2, await proxy.listCronJobs())
            await proxy.count(f'cron.disable {bobocron["iden"]}')

            await self.asyncraises(s_exc.AuthDeny, proxy.count('cron.add --hour +8 {file:bytes}'))
            await self.asyncraises(s_exc.StormRuntimeError, proxy.count(f'cron.del {fredcron["iden"]}'))
            await self.asyncraises(s_exc.StormRuntimeError, proxy.count(f'cron.disable {fredcron["iden"]}'))

        # fred
        # - read to main view
        # - read access to forked view via rule
        # - get/add all triggers, but can only delete his own
        # - can add/del trgtag but not bobotag
        # - has queue add/get, and admin on fredq
        # - full cron rights
        async with core.getLocalProxy(user='fred') as proxy:
            self.gt(await proxy.count('inet:ipv4'), 0)
            await self.asyncraises(s_exc.AuthDeny, proxy.count('[inet:ipv4=10.10.10.10]'))

            self.eq(2, await proxy.count('syn:trigger'))

            await self.asyncraises(s_exc.AuthDeny, proxy.count(f'trigger.del {tagtrg}'))
            await proxy.count(f'trigger.del {nodetrg}')
            self.eq(1, await proxy.count('syn:trigger'))

            await proxy.eval('trigger.add node:add --form file:bytes --query {[+#trgtag]}').list()
            self.eq(2, await proxy.count('syn:trigger'))

            self.gt(await proxy.count('inet:ipv4 [+#trgtag]'), 0)
            await self.asyncraises(s_exc.AuthDeny, proxy.count('#trgtag [-#bobotag]'))
            await self.asyncraises(s_exc.AuthDeny, proxy.count('inet:ipv4 [+#newfredtag]'))

            self.gt(await proxy.count('inet:ipv4', opts={'view': secview}), 0)
            await self.asyncraises(s_exc.AuthDeny, proxy.count('[inet:ipv4=10.9.10.1]', opts={'view': secview}))

            await proxy.count(f'queue.add newfredq')

            await proxy.count(f'$q = $lib.queue.get(fredq) inet:ipv4=9.9.9.9 $q.put($node.repr())')
            self.eq(1, await proxy.count(f'$q = $lib.queue.get(fredq) ($offs, $ipv4) = $q.get(0) inet:ipv4=$ipv4'))

            await self.asyncraises(s_exc.AuthDeny,
                                   proxy.count(f'$q = $lib.queue.get(boboq) inet:ipv4=1.2.3.4 $q.put($node.repr())'))
            await self.asyncraises(s_exc.AuthDeny,
                                   proxy.count(f'$q = $lib.queue.get(rootq) inet:ipv4=1.2.3.4 $q.put($node.repr())'))

            self.len(2, await proxy.listCronJobs())
            await proxy.count(f'cron.del {bobocron["iden"]}')
            self.len(1, await proxy.listCronJobs())

            await proxy.count(f'cron.disable {fredcron["iden"]}')

            await proxy.count('cron.add --hour +8 {file:bytes}')
            self.len(2, await proxy.listCronJobs())

        # root
        # - read/write to main view
        # - read/write to forked view
        # - get/del triggers
        # - full rights to all queues
        # - full rights to cronjobs
        async with core.getLocalProxy(user='root') as proxy:
            self.gt(await proxy.count('inet:ipv4'), 0)
            self.gt(await proxy.count('[inet:ipv4=10.10.10.11]'), 0)

            self.eq(2, await proxy.count('syn:trigger'))
            await proxy.eval(f'$lib.trigger.del({tagtrg})').list()
            self.eq(1, await proxy.count('syn:trigger'))

            self.gt(await proxy.count('inet:ipv4', opts={'view': secview}), 0)
            self.gt(await proxy.count('[inet:ipv4=10.9.10.1]', opts={'view': secview}), 0)

            await proxy.count(f'$q = $lib.queue.get(boboq) inet:ipv4=9.9.9.9 $q.put($node.repr())')
            self.eq(1, await proxy.count(f'$q = $lib.queue.get(boboq) ($offs, $ipv4) = $q.get(0) inet:ipv4=$ipv4'))

            await proxy.count(f'$q = $lib.queue.get(fredq) inet:ipv4=9.9.9.9 $q.put($node.repr())')
            self.eq(1, await proxy.count(f'$q = $lib.queue.get(boboq) ($offs, $ipv4) = $q.get(0) inet:ipv4=$ipv4'))

            await proxy.count(f'$q = $lib.queue.get(rootq) inet:ipv4=9.9.9.9 $q.put($node.repr())')
            self.eq(1, await proxy.count(f'$q = $lib.queue.get(rootq) ($offs, $ipv4) = $q.get(0) inet:ipv4=$ipv4'))

            crons = await proxy.listCronJobs()
            self.len(2, crons)

            await proxy.count('cron.add --hour +8 {inet:email}')
            self.len(3, await proxy.listCronJobs())

            await proxy.count(f'cron.del {fredcron["iden"]}')
            self.len(2, await proxy.listCronJobs())

    async def test_migr_nexus(self):
        conf = {
            'src': None,
            'dest': None,
            'migrops': None,
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            await migr.migrate()

            await self._checkStats(tdata, migr, locallyrs)

            # test dump errors
            dumpf = await migr.dumpErrors()
            self.nn(dumpf)

            errs = []
            with open(dumpf, 'rb') as fd:
                errs = s_msgpack.un(fd.read())

            self.len(2, errs)
            for err in errs:
                err['val']['node'][1]['props'].pop('.created')
            self.isin(MIGR_ERR, errs)

            await migr.fini()

            # startup 0.2.0 core
            async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                # check that nexus root has offsets from migration
                self.gt(await core.getNexusOffs(), 1)

                # check core data
                await self._checkCore(core, tdata)
                await self._checkAuth(core)

    async def test_migr_nexusoff(self):
        conf = {
            'src': None,
            'dest': None,
            'addmode': 'nonexus',
            'safetyoff': True,
            'migrops': None,
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            await migr.migrate()

            await self._checkStats(tdata, migr, locallyrs)

            await migr.fini()

            # startup 0.2.0 core
            async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                # check that nexus root has *no* offsets from migration
                self.eq(await core.getNexusOffs(), 0)

                # check core data
                await self._checkCore(core, tdata)
                await self._checkAuth(core)

    async def test_migr_editor(self):
        conf = {
            'src': None,
            'dest': None,
            'addmode': 'editor',
            'migrops': None,
            'fairiter': 1,
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            await migr.migrate()

            await self._checkStats(tdata, migr, locallyrs)

            await migr.fini()

            # startup 0.2.0 core
            async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                # check that nexus root has *no* offsets from migration
                self.eq(await core.getNexusOffs(), 0)

                # check core data
                await self._checkCore(core, tdata)
                await self._checkAuth(core)

    async def test_migr_restart(self):
        conf = {
            'src': None,
            'dest': None,
            'migrops': None,
            'fairiter': 1,
        }

        async with self._getTestMigrCore(conf) as (tdata0, dest0, locallyrs0, migr0):
            await migr0.migrate()

            await self._checkStats(tdata0, migr0, locallyrs0)

            # get layer offsets
            offslogs0 = []
            for iden in locallyrs0:
                offslogs0 += [log async for log in migr0._migrlogGet('nodes', 'nextoffs', iden)]

            self.len(2, offslogs0)  # one entry for each layer
            self.gt(offslogs0[0]['val'][0], 0)
            self.gt(offslogs0[1]['val'][0], 0)

            # check the saved file
            offsyaml = s_common.yamlload(dest0, 'migration', 'lyroffs.yaml')
            offslogdict = {x['key']: {'nextoffs': x['val'][0], 'created': x['val'][1]} for x in offslogs0}
            self.eq(offsyaml, offslogdict)

            await migr0.fini()

            # run migration again
            conf['dest'] = dest0

            async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
                # check form counts
                fcntprnt = await migr.formCounts()
                self.len(2, fcntprnt)
                fnum = len([x for x in tdata['podes'] if x[0][0] == 'inet:fqdn'])
                self.isin(f'inet:fqdn{fnum}{fnum}0', '_'.join(fcntprnt).replace(' ', ''))

                # check that destination is populated before starting migration
                iden = locallyrs[0]
                lyrslab = os.path.join(dest, 'layers', iden, 'layer_v2.lmdb')
                self.true(os.path.exists(lyrslab))

                await migr.migrate()

                await self._checkStats(tdata, migr, locallyrs)

                # get layer offsets
                offslogs = []
                for iden in locallyrs:
                    offslogs += [log async for log in migr._migrlogGet('nodes', 'nextoffs', iden)]

                self.len(2, offslogs)  # one entry for each layer
                self.gt(offslogs[0]['val'][0], 0)
                self.gt(offslogs[1]['val'][0], 0)
                self.gt(offslogs[0]['val'][1], offslogs0[0]['val'][1])  # timestamp should be updated
                self.gt(offslogs[1]['val'][1], offslogs0[1]['val'][1])

                # check the saved file
                offsyaml = s_common.yamlload(dest, 'migration', 'lyroffs.yaml')
                self.eq(offsyaml, {x['key']: {'nextoffs': x['val'][0], 'created': x['val'][1]} for x in offslogs})

                await migr.fini()

                # startup 0.2.0 core
                async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                    # check core data
                    await self._checkCore(core, tdata)
                    await self._checkAuth(core)

    async def test_migr_assvr_defaults(self):
        '''
        Test that migration service is being properly initialized from default cmdline args.
        '''
        with self.getRegrDir('cortexes', REGR_VER) as src:
            # sneak in test for missing splice slab - no impact to migration
            for root, dirs, files in os.walk(src, topdown=True):
                for dname in dirs:
                    if dname == 'splices.lmdb':
                        shutil.rmtree(os.path.join(root, dname))

            # check defaults
            with self.getTestDir() as dest:
                argv = [
                    '--src', src,
                    '--dest', dest,
                ]

                async with await s_migr.main(argv) as migr:
                    self.eq(migr.src, src)
                    self.eq(migr.dest, dest)
                    self.sorteq(migr.migrops, s_migr.ALL_MIGROPS)
                    self.eq(migr.addmode, 'nexus')
                    self.eq(migr.editbatchsize, 100)
                    self.eq(migr.fairiter, 100)
                    self.none(migr.nodelim)
                    self.false(migr.safetyoff)
                    self.false(migr.srcdedicated)
                    self.false(migr.destdedicated)

                    # check the saved file
                    offsyaml = s_common.yamlload(dest, 'migration', 'lyroffs.yaml')
                    self.true(all(v['nextoffs'] == 0 for v in offsyaml.values()))

                # startup 0.2.0 core
                async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                    nodes = await core.nodes('inet:ipv4=1.2.3.4')
                    self.len(1, nodes)
                    nodes = await core.nodes('[inet:ipv4=9.9.9.9]')
                    self.len(1, nodes)

    async def test_migr_assvr_opts(self):
        '''
        Test that migration service is being properly initialized from user cmdline args.
        '''
        with self.getRegrDir('cortexes', REGR_VER) as src:
            # check user opts
            with self.getTestDir() as destp:
                dest = os.path.join(destp, 'woot')  # verify svc is creating dir if it doesn't exist

                argv = [
                    '--src', src,
                    '--dest', dest,
                    '--add-mode', 'editor',
                    '--nodelim', '1000',
                    '--edit-batchsize', '1000',
                    '--fair-iter', '5',
                    '--src-dedicated',
                    '--dest-dedicated',
                    '--safety-off',
                    '--migr-ops', 'dirn', 'dmodel', 'cell',
                ]

                async with await s_migr.main(argv) as migr:
                    self.eq(migr.src, src)
                    self.eq(migr.dest, dest)
                    self.sorteq(migr.migrops, ['dirn', 'dmodel', 'cell'])
                    self.eq(migr.addmode, 'editor')
                    self.eq(migr.editbatchsize, 1000)
                    self.eq(migr.fairiter, 5)
                    self.eq(migr.nodelim, 1000)
                    self.true(migr.safetyoff)
                    self.true(migr.srcdedicated)
                    self.true(migr.destdedicated)

    async def test_migr_errconf(self):
        with self.getRegrDir('cortexes', REGR_VER) as src:
            with self.getTestDir() as dest:

                conf = {
                    'src': src,
                    'dest': None,
                }

                async with await s_migr.Migrator.anit(conf) as migr:
                    res = await migr.formCounts()
                    fullprnt = ' '.join(res)
                    locallyrs = os.listdir(os.path.join(src, 'layers'))
                    self.isin(locallyrs[0], fullprnt)
                    self.isin(locallyrs[1], fullprnt)

                    await self.asyncraises(Exception, migr.migrate())

                conf = {
                    'src': src,
                    'dest': dest,
                    'addmode': 'foobar',
                    'editbatchsize': 3,
                }
                await self.asyncraises(Exception, s_migr.Migrator.anit(conf))

                conf = {
                    'src': src,
                    'dest': dest,
                    'addmode': 'nexus',
                    'editbatchsize': 3,
                }

                async with await s_migr.Migrator.anit(conf) as migr:
                    migr.addmode = 'foobar'
                    await migr.migrate()

                    errs = [log async for log in migr._migrlogGet('nodes', 'error')]
                    self.isin('Unrecognized addmode foobar', [x.get('val', {}).get('mesg') for x in errs])

                    # migration dir gets deleted
                    shutil.rmtree(os.path.join(dest, 'migration'))
                    res = await migr.dumpErrors()
                    self.none(res)

                # startup 0.2.0 core - with no nodes
                async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                    nodes = await core.nodes('inet:ipv4=1.2.3.4')
                    self.len(0, nodes)

    async def test_migr_missingidens(self):
        with self.getRegrDir('cortexes', REGR_VER) as src:
            with self.getTestDir() as dest:

                conf = {
                    'src': src,
                    'dest': dest,
                }

                async with await s_migr.Migrator.anit(conf) as migr:
                    await migr._migrDirn()
                    await migr._initStors()

                    # replace user idens in hive
                    root = None
                    fred = None
                    newroot = s_common.guid()
                    newfred = s_common.guid()

                    users = await migr.hive.open(('auth', 'users'))
                    usersd = await users.dict()
                    for uiden, uname in usersd.items():
                        if uname == 'root':
                            root = uiden
                        elif uname == 'fred':
                            fred = uiden

                    await migr.hive.rename(('auth', 'users', root), ('auth', 'users', newroot))
                    await migr.hive.rename(('auth', 'users', fred), ('auth', 'users', newfred))

                    migr.migrops = [op for op in migr.migrops if op != 'dirn']

                    await migr.migrate()

                # startup 0.2.0 core
                async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                    nodes = await core.nodes('inet:ipv4=1.2.3.4')
                    self.len(1, nodes)
                    nodes = await core.nodes('[inet:ipv4=9.9.9.9]')
                    self.len(1, nodes)

    async def test_migr_yamlmod(self):
        with self.getRegrDir('cortexes', REGR_VER) as src:
            with self.getTestDir() as dest:
                locallyrs = os.listdir(os.path.join(src, 'layers'))

                mods = {'modules': ['synapse.tests.utils.TestModule']}
                s_common.yamlsave(mods, src, 'cell.yaml')

                conf = {
                    'src': src,
                    'dest': dest,
                    'migrops': ['dmodel', 'hivelyr', 'nodes'],
                    'editbatchsize': 1,
                    'nodelim': 2,
                }

                async with await s_migr.Migrator.anit(conf) as migr:
                    # test that we can skip dirn migration if it already exists
                    migr.migrops.append('dirn')
                    await migr._migrDirn()
                    del migr.migrops[-1]

                    # verify that test:int was loaded and migrated
                    await migr.migrate()
                    stats = []
                    for iden in locallyrs:
                        statkey = f'{iden}:form:test:int'
                        stats.extend([log async for log in migr._migrlogGet('nodes', 'stat', statkey)])

                    self.eq((1, 1), stats[0]['val'])

    async def test_migr_stormsvc(self):
        conf = {
            'src': None,
            'dest': None,
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            await migr.migrate()
            await migr.fini()

            # startup 0.2.0 core
            async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                # check core data
                await self._checkCore(core, tdata)

                # add in service
                svcpath = os.path.join(dest, 'stormsvc')
                async with await MigrStormsvc.anit(svcpath) as svc:
                    svc.dmon.share('turtle', svc)
                    root = await svc.auth.getUserByName('root')
                    await root.setPasswd('root')
                    info = await svc.dmon.listen('tcp://127.0.0.1:0/')
                    host, port = info

                    await core.nodes(f'service.add turtle tcp://root:root@127.0.0.1:{port}/turtle')
                    await core.nodes('$lib.service.wait(turtle)')

                    await core.nodes('newcmd')
                    self.len(1, await core.nodes('inet:fqdn=snoop.io'))

    async def test_migr_invalid(self):
        conf = {
            'src': None,
            'dest': None,
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            os.remove(os.path.join(migr.src, 'cell.yaml'))
            await migr._migrDirn()
            await migr._initStors()

            # modify a layer to be of type=remote in the hive
            lyrnode = await migr.hive.open(('cortex', 'layers', locallyrs[0]))
            layrinfo = await lyrnode.dict()
            await layrinfo.set('type', 'remote')

            migr.migrops = [op for op in migr.migrops if op != 'dirn']

            await migr.migrate()
            await migr.fini()

            # An error will be generated and migration halted
            # so we can check that the cortex is not startable as 020
            await self.asyncraises(s_exc.BadStorageVersion, s_cortex.Cortex.anit(dest, conf=None))

    async def test_migr_cell(self):
        conf = {
            'src': None,
            'dest': None,
            'migrops': [op for op in s_migr.ALL_MIGROPS if op != 'cell'],
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            await migr.migrate()
            await migr.fini()

            await self.asyncraises(s_exc.BadConfValu, s_cortex.Cortex.anit(dest, conf=None))

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            migr.migrops.append('cell')
            os.remove(os.path.join(migr.src, 'cell.yaml'))

            await migr.migrate()
            await migr.fini()

            # startup 0.2.0 core
            async with await s_cortex.Cortex.anit(dest, conf=None) as core:
                # check core data
                await self._checkCore(core, tdata)

    async def test_migr_dirn(self):
        with self.getRegrDir('cortexes', REGR_VER) as src:
            with self.getTestDir() as dest:
                os.listdir(os.path.join(src, 'layers'))

                conf = {
                    'src': src,
                    'dest': dest,
                }

                async with await s_migr.Migrator.anit(conf) as migr:
                    srcglob = [x for x in glob.glob(src + '/**', recursive=True) if 'lock' not in x]
                    nexpath = os.path.join(dest, 'slabs', 'nexus.lmdb')
                    self.false(os.path.exists(nexpath))

                    await migr._migrDirn()
                    await migr._initStors()
                    self.sorteq(srcglob, [x for x in glob.glob(src + '/**', recursive=True) if 'lock' not in x])
                    self.true(os.path.exists(nexpath))

                    await migr._migrDirn()
                    await migr._initStors()
                    self.sorteq(srcglob, [x for x in glob.glob(src + '/**', recursive=True) if 'lock' not in x])
                    self.true(os.path.exists(nexpath))

    async def test_migr_migrTriggers(self):
        conf = {
            'src': None,
            'dest': None,
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            await migr._migrDirn()
            await migr._initStors()

            # remove useridens
            for iden, valu in migr.cellslab.scanByFull(db=migr.trigdb):
                ruledict = s_msgpack.un(valu)
                ruledict.pop('useriden')
                migr.cellslab.put(iden, s_msgpack.en(ruledict), overwrite=True, db=migr.trigdb)

            await migr._migrTriggers()

            errs = [log async for log in migr._migrlogGet('triggers', 'error')]
            self.isin('Missing iden values for trigger', errs[0].get('val', {}).get('err', ''))

    async def test_migr_trnNodeToNodeedit(self):
        conf = {
            'src': None,
            'dest': None,
        }

        async with self._getTestMigrCore(conf) as (tdata, dest, locallyrs, migr):
            await migr._migrDirn()
            await migr._initStors()
            await migr._migrDatamodel()

            ndef = ('foo:bar', '1.2.3.3')
            buid = s_common.buid(ndef)
            node = await migr._srcPackNode(buid, ndef, {}, {}, {})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(ne)
            self.isin('Unable to parse form name', err['mesg'])

            ndef = ('*inet:fqdn', 123)
            buid = s_common.buid(ndef)
            node = await migr._srcPackNode(buid, ndef, {}, {}, {})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(ne)
            self.isin('Buid/norming exception', err['mesg'])

            ndef = ('*inet:fqdn', 'foo.com.')
            buid = s_common.buid(('inet:fqdn', 'foo.com.'))
            node = await migr._srcPackNode(buid, ndef, {}, {}, {})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(ne)
            self.isin('Normed form val does not match inbound', err['mesg'])

            ndef = ('*inet:fqdn', 'foo.com.')
            buid = s_common.buid(('inet:fqdn', 'foo.com'))
            node = await migr._srcPackNode(buid, ndef, {}, {}, {})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(ne)
            self.isin('Calculated buid does not match inbound', err['mesg'])

            ndef = ('*inet:fqdn', 'foo.com')
            buid = s_common.buid(('inet:fqdn', 'foo.com'))
            node = await migr._srcPackNode(buid, ndef, {'foo:bar': 'ham'}, {}, {})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(ne)
            self.isin('Unable to determine stortype for sprop foo:bar', err['mesg'])

            ndef = ('*inet:fqdn', 'foo.com')
            buid = s_common.buid(('inet:fqdn', 'foo.com'))
            node = await migr._srcPackNode(buid, ndef, {'inet:ipv4': 'ham'}, {}, {})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(ne)
            self.isin('Unable to determine stortype for sprop inet:ipv4', err['mesg'])

            ndef = ('*inet:fqdn', 'foo.com')
            buid = s_common.buid(('inet:fqdn', 'foo.com'))
            node = await migr._srcPackNode(buid, ndef, {}, {}, {'newp': {'ahh': 37}})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(ne)
            self.isin('Unable to determine stortype for tagprop ahh', err['mesg'])

            # continue on bad nodeedits
            ndef = ('*inet:fqdn', 'foo.com')
            buid = s_common.buid(('inet:fqdn', 'foo.com'))
            node = await migr._srcPackNode(buid, ndef, {}, {}, {})
            err, ne = await migr._trnNodeToNodeedit(node, migr.model, chknodes=True)
            self.none(err)
            ne = (ne[0], 'foo:bar', ne[2])

            lyrinfo = await migr._migrHiveLayerInfo(locallyrs[0])
            wlyr = await migr._destGetWlyr(migr.dest, locallyrs[0], lyrinfo)
            res = await migr._destAddNodes(wlyr, ne, 'nexus')
            self.isin('Unable to store nodeedits', res.get('mesg', ''))
