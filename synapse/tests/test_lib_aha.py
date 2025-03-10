import os
import asyncio

from unittest import mock

import synapse.exc as s_exc
import synapse.axon as s_axon
import synapse.common as s_common
import synapse.telepath as s_telepath

import synapse.lib.aha as s_aha
import synapse.lib.base as s_base
import synapse.lib.cell as s_cell

import synapse.tools.aha.list as s_a_list
import synapse.tools.backup as s_tools_backup

import synapse.tools.aha.enroll as s_tools_enroll
import synapse.tools.aha.provision.user as s_tools_provision_user
import synapse.tools.aha.provision.service as s_tools_provision_service

import synapse.tests.utils as s_test

realaddsvc = s_aha.AhaCell.addAhaSvc
async def mockaddsvc(self, name, info, network=None):
    if getattr(self, 'testerr', False):
        raise s_exc.SynErr(mesg='newp')
    return await realaddsvc(self, name, info, network=network)

class ExecTeleCallerApi(s_cell.CellApi):
    async def exectelecall(self, url, meth, *args, **kwargs):
        return await self.cell.exectelecall(url, meth, *args, **kwargs)

class ExecTeleCaller(s_cell.Cell):
    cellapi = ExecTeleCallerApi

    async def exectelecall(self, url, meth, *args, **kwargs):

        async with await s_telepath.openurl(url) as prox:
            meth = getattr(prox, meth)
            resp = await meth(*args, **kwargs)
            return resp

class AhaTest(s_test.SynTest):

    async def test_lib_aha_mirrors(self):

        with self.getTestDir() as dirn:
            dir0 = s_common.gendir(dirn, 'aha0')
            dir1 = s_common.gendir(dirn, 'aha1')

            conf = {'nexslog:en': True}

            async with self.getTestAha(conf={'nexslog:en': True}, dirn=dir0) as aha0:
                user = await aha0.auth.addUser('reguser', passwd='secret')
                await user.setAdmin(True)

            s_tools_backup.backup(dir0, dir1)

            async with self.getTestAha(conf=conf, dirn=dir0) as aha0:
                upstream_url = aha0.getLocalUrl()

                mirrorconf = {
                    'nexslog:en': True,
                    'mirror': upstream_url,
                }

                async with self.getTestAha(conf=mirrorconf, dirn=dir1) as aha1:
                    # CA is nexus-fied
                    cabyts = await aha0.genCaCert('mirrorca')
                    await aha1.sync()
                    mirbyts = await aha1.genCaCert('mirrorca')
                    self.eq(cabyts, mirbyts)
                    iden = s_common.guid()
                    # Adding, downing, and removing service is also nexusified
                    info = {'urlinfo': {'host': '127.0.0.1', 'port': 8080,
                                        'scheme': 'tcp'},
                            'online': iden}
                    await aha0.addAhaSvc('test', info, network='example.net')
                    await aha1.sync()
                    mnfo = await aha1.getAhaSvc('test.example.net')
                    self.eq(mnfo.get('name'), 'test.example.net')

                    wait00 = aha0.waiter(1, 'aha:svcdown')
                    await aha0.setAhaSvcDown('test', iden, network='example.net')
                    self.isin(len(await wait00.wait(timeout=6)), (1, 2))

                    await aha1.sync()
                    mnfo = await aha1.getAhaSvc('test.example.net')
                    self.notin('online', mnfo)

                    await aha0.delAhaSvc('test', network='example.net')
                    await aha1.sync()
                    mnfo = await aha1.getAhaSvc('test.example.net')
                    self.none(mnfo)

    async def test_lib_aha_offon(self):
        with self.getTestDir() as dirn:
            cryo0_dirn = s_common.gendir(dirn, 'cryo0')
            conf = {'auth:passwd': 'secret'}
            async with self.getTestAha(conf=conf.copy(), dirn=dirn) as aha:
                host, port = await aha.dmon.listen('tcp://127.0.0.1:0')

                wait00 = aha.waiter(1, 'aha:svcadd')
                cryo_conf = {
                    'aha:name': '0.cryo.mynet',
                    'aha:admin': 'root@cryo.mynet',
                    'aha:registry': f'tcp://root:secret@127.0.0.1:{port}',
                    'dmon:listen': 'tcp://0.0.0.0:0/',
                }
                async with self.getTestCryo(dirn=cryo0_dirn, conf=cryo_conf) as cryo:
                    self.isin(len(await wait00.wait(timeout=6)), (1, 2))

                    svc = await aha.getAhaSvc('0.cryo.mynet')
                    linkiden = svc.get('svcinfo', {}).get('online')
                    self.nn(linkiden)

                    # Tear down the Aha cell.
                    await aha.__aexit__(None, None, None)

            async with self.getTestAha(conf=conf.copy(), dirn=dirn) as aha:
                wait01 = aha.waiter(1, 'aha:svcdown')
                await wait01.wait(timeout=6)
                svc = await aha.getAhaSvc('0.cryo.mynet')
                self.notin('online', svc.get('svcinfo'))

                # Try setting something down a second time
                await aha.setAhaSvcDown('0.cryo.mynet', linkiden, network=None)
                svc = await aha.getAhaSvc('0.cryo.mynet')
                self.notin('online', svc.get('svcinfo'))

    async def test_lib_aha(self):

        with self.raises(s_exc.NoSuchName):
            await s_telepath.getAhaProxy({})

        with self.raises(s_exc.NotReady):
            await s_telepath.getAhaProxy({'host': 'hehe.haha'})

        # We do inprocess reference counting for urls and clients.
        urls = ['newp://newp@newp', 'newp://newp@newp']
        info = await s_telepath.addAhaUrl(urls)
        self.eq(info.get('refs'), 1)
        # There is not yet a telepath client which is using these urls.
        self.none(info.get('client'))
        info = await s_telepath.addAhaUrl(urls)
        self.eq(info.get('refs'), 2)

        await s_telepath.delAhaUrl(urls)
        self.len(1, s_telepath.aha_clients)
        await s_telepath.delAhaUrl(urls)
        self.len(0, s_telepath.aha_clients)

        self.eq(0, await s_telepath.delAhaUrl('newp'))

        async with self.getTestAha() as aha:

            cryo0_dirn = s_common.gendir(aha.dirn, 'cryo0')

            host, port = await aha.dmon.listen('tcp://127.0.0.1:0')
            await aha.auth.rootuser.setPasswd('hehehaha')

            wait00 = aha.waiter(1, 'aha:svcadd')
            conf = {
                'aha:name': '0.cryo.mynet',
                'aha:leader': 'cryo.mynet',
                'aha:admin': 'root@cryo.mynet',
                'aha:registry': [f'tcp://root:hehehaha@127.0.0.1:{port}',
                                 f'tcp://root:hehehaha@127.0.0.1:{port}'],
                'dmon:listen': 'tcp://0.0.0.0:0/',
            }
            async with self.getTestCryo(dirn=cryo0_dirn, conf=conf) as cryo:

                await cryo.auth.rootuser.setPasswd('secret')

                ahaadmin = await cryo.auth.getUserByName('root@cryo.mynet')
                self.nn(ahaadmin)
                self.true(ahaadmin.isAdmin())

                await wait00.wait(timeout=2)

                with self.raises(s_exc.NoSuchName):
                    await s_telepath.getAhaProxy({'host': 'hehe.haha'})

                async with await s_telepath.openurl('aha://root:secret@cryo.mynet') as proxy:
                    self.nn(await proxy.getCellIden())

                with self.raises(s_exc.BadArg):
                    await cryo.ahaclient.waitready(timeout=2)
                    await cryo.ahaclient.modAhaSvcInfo('cryo.mynet', {'newp': 'newp'})

                async with await s_telepath.openurl('aha://root:secret@0.cryo.mynet') as proxy:
                    self.nn(await proxy.getCellIden())

                # force a reconnect...
                waiter = aha.waiter(1, 'aha:svcadd')
                proxy = await cryo.ahaclient.proxy(timeout=2)
                await proxy.fini()
                self.nn(await waiter.wait(timeout=6))

                async with await s_telepath.openurl('aha://root:secret@cryo.mynet') as proxy:
                    self.nn(await proxy.getCellIden())

                waiter = aha.waiter(1, 'aha:svcadd')
                # force the service into passive mode...
                await cryo.setCellActive(False)

                with self.raises(s_exc.NoSuchName):
                    async with await s_telepath.openurl('aha://root:secret@cryo.mynet') as proxy:
                        pass

                self.nn(await waiter.wait(timeout=6))

                async with await s_telepath.openurl('aha://root:secret@0.cryo.mynet') as proxy:
                    self.nn(await proxy.getCellIden())

                await cryo.setCellActive(True)

                async with await s_telepath.openurl('aha://root:secret@cryo.mynet') as proxy:
                    self.nn(await proxy.getCellIden())

                # some coverage edge cases...
                cryo.conf.pop('aha:leader', None)
                await cryo.setCellActive(False)

                # lock the aha:admin account so we can confirm it is unlocked upon restart
                # remove the admin flag from the account.
                self.false(ahaadmin.isLocked())
                await ahaadmin.setLocked(True, logged=False)
                self.true(ahaadmin.isLocked())
                # remove the admin status so we can confirm its an admin upon restart
                await ahaadmin.setAdmin(False, logged=False)
                self.false(ahaadmin.isAdmin())

            async with self.getTestCryo(dirn=cryo0_dirn, conf=conf) as cryo:
                ahaadmin = await cryo.auth.getUserByName('root@cryo.mynet')
                # And we should be unlocked and admin now
                self.false(ahaadmin.isLocked())
                self.true(ahaadmin.isAdmin())

            wait01 = aha.waiter(1, 'aha:svcadd')
            conf = {
                'aha:name': '0.cryo',
                'aha:leader': 'cryo',
                'aha:network': 'foo',
                'aha:registry': f'tcp://root:hehehaha@127.0.0.1:{port}',
                'dmon:listen': 'tcp://0.0.0.0:0/',
            }
            async with self.getTestCryo(conf=conf) as cryo:

                await cryo.auth.rootuser.setPasswd('secret')

                await wait01.wait(timeout=2)

                async with await s_telepath.openurl('aha://root:secret@cryo.foo') as proxy:
                    self.nn(await proxy.getCellIden())

                async with await s_telepath.openurl('aha://root:secret@0.cryo.foo') as proxy:
                    self.nn(await proxy.getCellIden())
                    await proxy.puts('hehe', ('hehe', 'haha'))

                async with await s_telepath.openurl('aha://root:secret@0.cryo.foo/*/hehe') as proxy:
                    self.nn(await proxy.iden())

                async with await s_telepath.openurl(f'tcp://root:hehehaha@127.0.0.1:{port}') as ahaproxy:
                    svcs = [x async for x in ahaproxy.getAhaSvcs('foo')]
                    self.len(2, svcs)
                    names = [s['name'] for s in svcs]
                    self.sorteq(('cryo.foo', '0.cryo.foo'), names)

                    self.none(await ahaproxy.getCaCert('vertex.link'))
                    cacert0 = await ahaproxy.genCaCert('vertex.link')
                    cacert1 = await ahaproxy.genCaCert('vertex.link')
                    self.nn(cacert0)
                    self.eq(cacert0, cacert1)
                    self.eq(cacert0, await ahaproxy.getCaCert('vertex.link'))

                    csrpem = cryo.certdir.genHostCsr('cryo.vertex.link').decode()

                    hostcert00 = await ahaproxy.signHostCsr(csrpem)
                    hostcert01 = await ahaproxy.signHostCsr(csrpem)

                    self.nn(hostcert00)
                    self.nn(hostcert01)
                    self.ne(hostcert00, hostcert01)

                    csrpem = cryo.certdir.genUserCsr('visi@vertex.link').decode()

                    usercert00 = await ahaproxy.signUserCsr(csrpem)
                    usercert01 = await ahaproxy.signUserCsr(csrpem)

                    self.nn(usercert00)
                    self.nn(usercert01)
                    self.ne(usercert00, usercert01)

            async with await s_telepath.openurl(f'tcp://root:hehehaha@127.0.0.1:{port}') as ahaproxy:
                await ahaproxy.delAhaSvc('cryo', network='foo')
                await ahaproxy.delAhaSvc('0.cryo', network='foo')
                self.none(await ahaproxy.getAhaSvc('cryo.foo'))
                self.none(await ahaproxy.getAhaSvc('0.cryo.foo'))
                self.len(2, [s async for s in ahaproxy.getAhaSvcs()])

                with self.raises(s_exc.BadArg):
                    info = {'urlinfo': {'host': '127.0.0.1', 'port': 8080, 'scheme': 'tcp'}}
                    await ahaproxy.addAhaSvc('newp', info, network=None)

            # We can use HTTP API to get the registered services
            await aha.addUser('lowuser', passwd='lowuser')
            await aha.auth.rootuser.setPasswd('secret')
            host, httpsport = await aha.addHttpsPort(0)
            svcsurl = f'https://localhost:{httpsport}/api/v1/aha/services'

            async with self.getHttpSess(auth=('root', 'secret'), port=httpsport) as sess:
                async with sess.get(svcsurl) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'ok')
                    result = info.get('result')
                    self.len(2, result)
                    self.eq({'0.cryo.mynet', 'cryo.mynet'},
                            {svcinfo.get('name') for svcinfo in result})

                async with sess.get(svcsurl, json={'network': 'mynet'}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'ok')
                    result = info.get('result')
                    self.len(1, result)
                    self.eq('cryo.mynet', result[0].get('name'))

                async with sess.get(svcsurl, json={'network': 'newp'}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'ok')
                    result = info.get('result')
                    self.len(0, result)

                # Sad path
                async with sess.get(svcsurl, json={'newp': 'hehe'}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')

                async with sess.get(svcsurl, json={'network': 'mynet', 'newp': 'hehe'}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')

            # Sad path
            async with self.getHttpSess(auth=('lowuser', 'lowuser'), port=httpsport) as sess:
                async with sess.get(svcsurl) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'AuthDeny')

        # The aha service can also be configured with a set of URLs that could represent itself.
        urls = ('cell://home0', 'cell://home1')
        conf = {'aha:urls': urls}
        async with self.getTestAha(conf=conf) as aha:
            async with aha.getLocalProxy() as ahaproxy:
                aurls = await ahaproxy.getAhaUrls()
                self.eq(urls, aurls)

        with self.getTestDir() as dirn:
            conf = {
                'aha:name': '0.test',
                'aha:leader': 'test',
                'aha:network': 'foo',
                'aha:registry': f'tcp://root:hehehaha@127.0.0.1:{port}',
                'dmon:listen': f'unix://{dirn}/sock'
            }
            async with self.getTestAha(conf=conf) as aha:
                ahainfo = await aha.getAhaInfo()
                uinfo = ahainfo.get('urlinfo', {})
                self.eq(uinfo.get('scheme'), 'unix')
                self.none(uinfo.get('port'))
                self.none(aha._getAhaUrls())

            conf['dmon:listen'] = 'tcp://0.0.0.0:0/'
            async with self.getTestAha(conf=conf) as aha:
                ahainfo = await aha.getAhaInfo()
                uinfo = ahainfo.get('urlinfo', {})
                self.eq(uinfo.get('scheme'), 'tcp')
                self.gt(uinfo.get('port'), 0)
                self.eq(aha._getAhaUrls()[0], f'ssl://0.test.foo:{aha.sockaddr[1]}')

    async def test_lib_aha_loadenv(self):

        with self.getTestDir() as dirn:

            async with self.getTestAha() as aha:
                host, port = await aha.dmon.listen('tcp://127.0.0.1:0')
                await aha.auth.rootuser.setPasswd('hehehaha')

                conf = {
                    'version': 1,
                    'aha:servers': [
                        f'tcp://root:hehehaha@127.0.0.1:{port}/',
                    ],
                }

                path = s_common.genpath(dirn, 'telepath.yaml')
                s_common.yamlsave(conf, path)

                # No clients have been loaded yet.
                with self.raises(s_exc.NotReady) as cm:
                    await s_telepath.openurl('aha://visi@foo.bar.com')
                self.eq(cm.exception.get('mesg'),
                        'No aha servers registered to lookup foo.bar.com')

                fini = await s_telepath.loadTeleEnv(path)

                # Should be one uninitialized aha client
                self.len(1, s_telepath.aha_clients)
                [info] = s_telepath.aha_clients.values()
                self.none(info.get('client'))

                with self.raises(s_exc.NoSuchName):
                    await s_telepath.openurl('aha://visi@foo.bar.com')

                # Connecting to an aha url should have initialized the client
                self.len(1, s_telepath.aha_clients)
                self.nn(info.get('client'))
                await fini()

    async def test_lib_aha_finid_cell(self):

        async with self.getTestAha() as aha:

            cryo0_dirn = s_common.gendir(aha.dirn, 'cryo0')

            host, port = await aha.dmon.listen('tcp://127.0.0.1:0')
            await aha.auth.rootuser.setPasswd('hehehaha')

            aharegistry = [f'tcp://root:hehehaha@127.0.0.1:{port}',
                          f'tcp://root:hehehaha@127.0.0.1:{port}']
            atup = tuple(aharegistry)

            wait00 = aha.waiter(1, 'aha:svcadd')
            conf = {
                'aha:name': '0.cryo.mynet',
                'aha:admin': 'root@cryo.mynet',
                'aha:registry': aharegistry,
                'dmon:listen': 'tcp://0.0.0.0:0/',
            }
            async with self.getTestCryo(dirn=cryo0_dirn, conf=conf) as cryo:

                await cryo.auth.rootuser.setPasswd('secret')

                ahaadmin = await cryo.auth.getUserByName('root@cryo.mynet')
                self.nn(ahaadmin)
                self.true(ahaadmin.isAdmin())

                await wait00.wait(timeout=2)

                self.isin(atup, s_telepath.aha_clients)

                async with await s_telepath.openurl('aha://root:secret@0.cryo.mynet') as proxy:
                    self.nn(await proxy.getCellIden())

                _ahaclient = s_telepath.aha_clients.get(atup).get('client')
                _aprx = await _ahaclient.proxy()

                await aha.fini()

                self.true(await _aprx.waitfini(timeout=10))

                orig = s_telepath.Client.proxy
                async def quickproxy(self, timeout):
                    return await orig(self, timeout=0.1)

                with mock.patch('synapse.telepath.Client.proxy', quickproxy):
                    with self.raises(asyncio.TimeoutError):

                        async with await s_telepath.openurl('aha://root:secret@0.cryo.mynet') as proxy:
                            self.fail('Should never reach a connection.')

    async def test_lib_aha_onlink_fail(self):

        with mock.patch('synapse.lib.aha.AhaCell.addAhaSvc', mockaddsvc):

            async with self.getTestAha() as aha:

                cryo0_dirn = s_common.gendir(aha.dirn, 'cryo0')

                host, port = await aha.dmon.listen('tcp://127.0.0.1:0')
                await aha.auth.rootuser.setPasswd('secret')

                aha.testerr = True

                wait00 = aha.waiter(1, 'aha:svcadd')
                conf = {
                    'aha:name': '0.cryo.mynet',
                    'aha:admin': 'root@cryo.mynet',
                    'aha:registry': f'tcp://root:secret@127.0.0.1:{port}',
                    'dmon:listen': 'tcp://0.0.0.0:0/',
                }
                async with self.getTestCryo(dirn=cryo0_dirn, conf=conf) as cryo:

                    await cryo.auth.rootuser.setPasswd('secret')

                    self.none(await wait00.wait(timeout=2))

                    svc = await aha.getAhaSvc('0.cryo.mynet')
                    self.none(svc)

                    wait01 = aha.waiter(1, 'aha:svcadd')
                    aha.testerr = False

                    self.nn(await wait01.wait(timeout=2))

                    svc = await aha.getAhaSvc('0.cryo.mynet')
                    self.nn(svc)
                    self.nn(svc.get('svcinfo', {}).get('online'))

                    async with await s_telepath.openurl('aha://root:secret@0.cryo.mynet') as proxy:
                        self.nn(await proxy.getCellIden())

    async def test_lib_aha_bootstrap(self):

        with self.getTestDir() as dirn:
            certdirn = s_common.gendir('certdir')
            with self.getTestCertDir(certdirn):

                conf = {
                    'aha:name': 'aha',
                    'aha:admin': 'root@do.vertex.link',
                    'aha:network': 'do.vertex.link',
                }

                async with self.getTestAha(dirn=dirn, conf=conf) as aha:
                    self.true(os.path.isfile(os.path.join(dirn, 'certs', 'cas', 'do.vertex.link.crt')))
                    self.true(os.path.isfile(os.path.join(dirn, 'certs', 'cas', 'do.vertex.link.key')))
                    self.true(os.path.isfile(os.path.join(dirn, 'certs', 'hosts', 'aha.do.vertex.link.crt')))
                    self.true(os.path.isfile(os.path.join(dirn, 'certs', 'hosts', 'aha.do.vertex.link.key')))
                    self.true(os.path.isfile(os.path.join(dirn, 'certs', 'users', 'root@do.vertex.link.crt')))
                    self.true(os.path.isfile(os.path.join(dirn, 'certs', 'users', 'root@do.vertex.link.key')))

                    host, port = await aha.dmon.listen('ssl://127.0.0.1:0?hostname=aha.do.vertex.link&ca=do.vertex.link')

                    async with await s_telepath.openurl(f'ssl://root@127.0.0.1:{port}?hostname=aha.do.vertex.link') as proxy:
                        await proxy.getCellInfo()

    async def test_lib_aha_noconf(self):

        async with self.getTestAha() as aha:

            with self.raises(s_exc.NeedConfValu):
                await aha.addAhaSvcProv('hehe')

            aha.conf['aha:urls'] = 'tcp://127.0.0.1:0/'

            with self.raises(s_exc.NeedConfValu):
                await aha.addAhaSvcProv('hehe')

            with self.raises(s_exc.NeedConfValu):
                await aha.addAhaUserEnroll('hehe')

            aha.conf['provision:listen'] = 'tcp://127.0.0.1:27272'

            with self.raises(s_exc.NeedConfValu):
                await aha.addAhaSvcProv('hehe')

            with self.raises(s_exc.NeedConfValu):
                await aha.addAhaUserEnroll('hehe')

            aha.conf['aha:network'] = 'haha'
            await aha.addAhaSvcProv('hehe')

    async def test_lib_aha_provision(self):

        with self.getTestDir() as dirn:

            conf = {
                'aha:name': 'aha',
                'aha:network': 'loop.vertex.link',
                'provision:listen': 'ssl://aha.loop.vertex.link:0'
            }
            async with self.getTestAha(dirn=dirn, conf=conf) as aha:

                addr, port = aha.provdmon.addr
                # update the config to reflect the dynamically bound port
                aha.conf['provision:listen'] = f'ssl://aha.loop.vertex.link:{port}'

                # do this config ex-post-facto due to port binding...
                host, ahaport = await aha.dmon.listen('ssl://0.0.0.0:0?hostname=aha.loop.vertex.link&ca=loop.vertex.link')
                aha.conf['aha:urls'] = f'ssl://aha.loop.vertex.link:{ahaport}'

                url = aha.getLocalUrl()

                outp = self.getTestOutp()
                await s_tools_provision_service.main(('--url', aha.getLocalUrl(), 'foobar'), outp=outp)
                self.isin('one-time use URL: ', str(outp))

                provurl = str(outp).split(':', 1)[1].strip()

                async with await s_telepath.openurl(provurl) as prov:
                    provinfo = await prov.getProvInfo()
                    self.isinstance(provinfo, dict)
                    conf = provinfo.get('conf')
                    # Default https port is not set; dmon is port 0
                    self.notin('https:port', conf)
                    dmon_listen = conf.get('dmon:listen')
                    parts = s_telepath.chopurl(dmon_listen)
                    self.eq(parts.get('port'), 0)
                    self.nn(await prov.getCaCert())

                with self.raises(s_exc.NoSuchName):
                    await s_telepath.openurl(provurl)

                async with aha.getLocalProxy() as proxy:
                    onebork = await proxy.addAhaSvcProv('bork')
                    await proxy.delAhaSvcProv(onebork)

                    onenewp = await proxy.addAhaSvcProv('newp')
                    async with await s_telepath.openurl(onenewp) as provproxy:

                        byts = aha.certdir.genHostCsr('lalala')
                        with self.raises(s_exc.BadArg):
                            await provproxy.signHostCsr(byts)

                        byts = aha.certdir.genUserCsr('lalala')
                        with self.raises(s_exc.BadArg):
                            await provproxy.signUserCsr(byts)

                    onebork = await proxy.addAhaUserEnroll('bork00')
                    await proxy.delAhaUserEnroll(onebork)

                    onebork = await proxy.addAhaUserEnroll('bork01')
                    async with await s_telepath.openurl(onebork) as provproxy:

                        byts = aha.certdir.genUserCsr('zipzop')
                        with self.raises(s_exc.BadArg):
                            await provproxy.signUserCsr(byts)

                onetime = await aha.addAhaSvcProv('00.axon')

                axonpath = s_common.gendir(dirn, 'axon')
                axonconf = {
                    'aha:provision': onetime,
                }
                s_common.yamlsave(axonconf, axonpath, 'cell.yaml')

                argv = (axonpath, '--auth-passwd', 'rootbeer')
                async with await s_axon.Axon.initFromArgv(argv) as axon:

                    # opts were copied through successfully
                    self.true(await axon.auth.rootuser.tryPasswd('rootbeer'))

                    # test that nobody set aha:admin
                    self.none(await axon.auth.getUserByName('root@loop.vertex.link'))
                    self.none(await axon.auth.getUserByName('axon@loop.vertex.link'))

                    self.true(os.path.isfile(s_common.genpath(axon.dirn, 'prov.done')))
                    self.true(os.path.isfile(s_common.genpath(axon.dirn, 'certs', 'cas', 'loop.vertex.link.crt')))
                    self.true(os.path.isfile(s_common.genpath(axon.dirn, 'certs', 'hosts', '00.axon.loop.vertex.link.crt')))
                    self.true(os.path.isfile(s_common.genpath(axon.dirn, 'certs', 'hosts', '00.axon.loop.vertex.link.key')))
                    self.true(os.path.isfile(s_common.genpath(axon.dirn, 'certs', 'users', 'root@loop.vertex.link.crt')))
                    self.true(os.path.isfile(s_common.genpath(axon.dirn, 'certs', 'users', 'root@loop.vertex.link.key')))

                    yamlconf = s_common.yamlload(axon.dirn, 'cell.yaml')
                    self.eq('axon', yamlconf.get('aha:leader'))
                    self.eq('00.axon', yamlconf.get('aha:name'))
                    self.eq('loop.vertex.link', yamlconf.get('aha:network'))
                    self.none(yamlconf.get('aha:admin'))
                    self.eq((f'ssl://root@aha.loop.vertex.link:{ahaport}',), yamlconf.get('aha:registry'))
                    self.eq(f'ssl://0.0.0.0:0?hostname=00.axon.loop.vertex.link&ca=loop.vertex.link', yamlconf.get('dmon:listen'))

                    unfo = await axon.addUser('visi')

                    outp = self.getTestOutp()
                    await s_tools_provision_user.main(('--url', aha.getLocalUrl(), 'visi'), outp=outp)
                    self.isin('one-time use URL:', str(outp))

                    provurl = str(outp).split(':', 1)[1].strip()
                    with self.getTestSynDir() as syndir:

                        capath = s_common.genpath(syndir, 'certs', 'cas', 'loop.vertex.link.crt')
                        crtpath = s_common.genpath(syndir, 'certs', 'users', 'visi@loop.vertex.link.crt')
                        keypath = s_common.genpath(syndir, 'certs', 'users', 'visi@loop.vertex.link.key')

                        for path in (capath, crtpath, keypath):
                            s_common.genfile(path)

                        outp = self.getTestOutp()
                        await s_tools_enroll.main((provurl,), outp=outp)

                        for path in (capath, crtpath, keypath):
                            self.gt(os.path.getsize(path), 0)

                        teleyaml = s_common.yamlload(syndir, 'telepath.yaml')
                        self.eq(teleyaml.get('version'), 1)
                        self.eq(teleyaml.get('aha:servers'), (f'ssl://visi@aha.loop.vertex.link:{ahaport}',))

                        certdir = s_telepath.s_certdir.CertDir(os.path.join(syndir, 'certs'))
                        async with await s_telepath.openurl('aha://visi@axon...', certdir=certdir) as prox:
                            self.eq(axon.iden, await prox.getCellIden())

                        # Lock the user
                        await axon.setUserLocked(unfo.get('iden'), True)

                        with self.raises(s_exc.AuthDeny) as cm:
                            async with await s_telepath.openurl('aha://visi@axon...', certdir=certdir) as prox:
                                self.eq(axon.iden, await prox.getCellIden())
                        self.isin('locked', cm.exception.get('mesg'))

                    outp = self.getTestOutp()
                    await s_tools_provision_user.main(('--url', aha.getLocalUrl(), 'visi'), outp=outp)
                    self.isin('Need --again', str(outp))

                    outp = self.getTestOutp()
                    await s_tools_provision_user.main(('--url', aha.getLocalUrl(), '--again', 'visi'), outp=outp)
                    self.isin('one-time use URL:', str(outp))

                onetime = await aha.addAhaSvcProv('00.axon')
                axonconf = {
                    'https:port': None,
                    'aha:provision': onetime,
                }
                s_common.yamlsave(axonconf, axonpath, 'cell.yaml')

                # Populate data in the overrides file that will be removed from the
                # provisioning data
                overconf = {
                    'dmon:listen': 'tcp://0.0.0.0:0',  # This is removed
                    'nexslog:async': True,  # just set as a demonstrative value
                }
                s_common.yamlsave(overconf, axonpath, 'cell.mods.yaml')

                # force a re-provision... (because the providen is different)
                with self.getAsyncLoggerStream('synapse.lib.cell',
                                               'Provisioning axon from AHA service') as stream:
                    async with await s_axon.Axon.initFromArgv((axonpath,)) as axon:
                        self.true(await stream.wait(6))
                        self.ne(axon.conf.get('dmon:listen'),
                                'tcp://0.0.0.0:0')
                overconf2 = s_common.yamlload(axonpath, 'cell.mods.yaml')
                self.eq(overconf2, {'nexslog:async': True})

                # tests startup logic that recognizes it's already done
                with self.getAsyncLoggerStream('synapse.lib.cell', ) as stream:
                    async with await s_axon.Axon.initFromArgv((axonpath,)) as axon:
                        pass
                    stream.seek(0)
                    self.notin('Provisioning axon from AHA service', stream.read())

                async with await s_axon.Axon.initFromArgv((axonpath,)) as axon:
                    # testing second run...
                    pass

                # With one axon up, we can provision a mirror of him.
                axn2path = s_common.genpath(dirn, 'axon2')

                argv = ['--url', aha.getLocalUrl(), '01.axon', '--mirror', 'axon', '--only-url']
                outp = self.getTestOutp()
                retn = await s_tools_provision_service.main(argv, outp=outp)
                self.eq(0, retn)
                provurl = str(outp).strip()
                self.notin('one-time use URL: ', provurl)
                self.isin('ssl://', provurl)
                urlinfo = s_telepath.chopurl(provurl)
                providen = urlinfo.get('path').strip('/')

                async with await s_axon.Axon.initFromArgv((axonpath,)) as axon:
                    with s_common.genfile(axonpath, 'prov.done') as fd:
                        axonproviden = fd.read().decode().strip()
                    self.ne(axonproviden, providen)

                    # Punch the provisioning URL in like a environment variable
                    with self.setTstEnvars(SYN_AXON_AHA_PROVISION=provurl):

                        async with await s_axon.Axon.initFromArgv((axn2path,)) as axon2:
                            await axon2.sync()
                            self.true(axon.isactive)
                            self.false(axon2.isactive)
                            self.eq('aha://root@axon.loop.vertex.link', axon2.conf.get('mirror'))

                            with s_common.genfile(axn2path, 'prov.done') as fd:
                                axon2providen = fd.read().decode().strip()
                            self.eq(providen, axon2providen)

                        # Turn the mirror back on with the provisioning url in the config
                        async with await s_axon.Axon.initFromArgv((axn2path,)) as axon2:
                            await axon2.sync()
                            self.true(axon.isactive)
                            self.false(axon2.isactive)
                            self.eq('aha://root@axon.loop.vertex.link', axon2.conf.get('mirror'))

                # Provision a mirror using aha:provision in the mirror cell.yaml as well.
                # This is similar to the previous test block.
                axn3path = s_common.genpath(dirn, 'axon3')

                argv = ['--url', aha.getLocalUrl(), '02.axon', '--mirror', 'axon']
                outp = self.getTestOutp()
                await s_tools_provision_service.main(argv, outp=outp)
                self.isin('one-time use URL: ', str(outp))
                provurl = str(outp).split(':', 1)[1].strip()
                urlinfo = s_telepath.chopurl(provurl)
                providen = urlinfo.get('path').strip('/')

                async with await s_axon.Axon.initFromArgv((axonpath,)) as axon:

                    with s_common.genfile(axonpath, 'prov.done') as fd:
                        axonproviden = fd.read().decode().strip()
                    self.ne(axonproviden, providen)

                    axon2conf = {
                        'aha:provision': provurl,
                    }
                    s_common.yamlsave(axon2conf, axn3path, 'cell.yaml')
                    async with await s_axon.Axon.initFromArgv((axn3path,)) as axon03:
                        await axon03.sync()
                        self.true(axon.isactive)
                        self.false(axon03.isactive)
                        self.eq('aha://root@axon.loop.vertex.link', axon03.conf.get('mirror'))

                        with s_common.genfile(axn3path, 'prov.done') as fd:
                            axon3providen = fd.read().decode().strip()
                        self.eq(providen, axon3providen)

                    # Ensure that the aha:provision value was popped from the cell.yaml file,
                    # since that would have mismatched what was used to provision the mirror.
                    copied_conf = s_common.yamlload(axn3path, 'cell.yaml')
                    self.notin('aha:provision', copied_conf)

                    # Turn the mirror back on with the provisioning url removed from the config
                    async with await s_axon.Axon.initFromArgv((axn3path,)) as axon3:
                        await axon3.sync()
                        self.true(axon.isactive)
                        self.false(axon3.isactive)
                        self.eq('aha://root@axon.loop.vertex.link', axon03.conf.get('mirror'))

                        retn, outp = await self.execToolMain(s_a_list._main, [aha.getLocalUrl()])
                        self.eq(retn, 0)
                        outp.expect('Service              network                        leader')
                        outp.expect('00.axon              loop.vertex.link               True')
                        outp.expect('01.axon              loop.vertex.link               False')
                        outp.expect('02.axon              loop.vertex.link               False')
                        outp.expect('axon                 loop.vertex.link               True')

                # Ensure we can provision a service on a given listening ports
                outp.clear()
                args = ('--url', aha.getLocalUrl(), 'bazfaz', '--dmon-port', '123456')
                ret = await s_tools_provision_service.main(args, outp=outp)
                self.eq(1, ret)
                outp.expect('ERROR: Invalid dmon port: 123456')

                outp.clear()
                args = ('--url', aha.getLocalUrl(), 'bazfaz', '--https-port', '123456')
                ret = await s_tools_provision_service.main(args, outp=outp)
                outp.expect('ERROR: Invalid HTTPS port: 123456')
                self.eq(1, ret)

                outp.clear()
                bad_conf_path = s_common.genpath(dirn, 'badconf.yaml')
                s_common.yamlsave({'aha:network': 'aha.newp.net'}, bad_conf_path)
                args = ('--url', aha.getLocalUrl(), 'bazfaz', '--cellyaml', bad_conf_path)
                ret = await s_tools_provision_service.main(args, outp=outp)
                outp.expect('ERROR: Provisioning aha:network must be equal to the Aha servers network')
                self.eq(1, ret)

                outp = self.getTestOutp()
                argv = ('--url', aha.getLocalUrl(), 'bazfaz', '--dmon-port', '1234', '--https-port', '443')
                await s_tools_provision_service.main(argv, outp=outp)
                self.isin('one-time use URL: ', str(outp))
                provurl = str(outp).split(':', 1)[1].strip()
                async with await s_telepath.openurl(provurl) as proxy:
                    provconf = await proxy.getProvInfo()
                    conf = provconf.get('conf')
                    dmon_listen = conf.get('dmon:listen')
                    parts = s_telepath.chopurl(dmon_listen)
                    self.eq(parts.get('port'), 1234)
                    https_port = conf.get('https:port')
                    self.eq(https_port, 443)

                # provisioning against a network that differs from the aha network fails.
                bad_netw = 'stuff.goes.beep'
                provinfo = {'conf': {'aha:network': bad_netw}}
                with self.raises(s_exc.BadConfValu) as cm:
                    async with self.addSvcToAha(aha, '00.exec', ExecTeleCaller,
                                                provinfo=provinfo) as conn:
                        pass
                self.isin('Provisioning aha:network must be equal to the Aha servers network',
                          cm.exception.get('mesg'))

    async def test_aha_httpapi(self):

        conf = {
            'aha:name': 'aha',
            'aha:network': 'loop.vertex.link',
            'provision:listen': 'ssl://aha.loop.vertex.link:0'
        }
        async with self.getTestAha(conf=conf) as aha:
            await aha.auth.rootuser.setPasswd('secret')

            addr, port = aha.provdmon.addr
            # update the config to reflect the dynamically bound port
            aha.conf['provision:listen'] = f'ssl://aha.loop.vertex.link:{port}'

            # do this config ex-post-facto due to port binding...
            host, ahaport = await aha.dmon.listen('ssl://0.0.0.0:0?hostname=aha.loop.vertex.link&ca=loop.vertex.link')
            aha.conf['aha:urls'] = f'ssl://aha.loop.vertex.link:{ahaport}'

            host, httpsport = await aha.addHttpsPort(0)
            url = f'https://localhost:{httpsport}/api/v1/aha/provision/service'

            async with self.getHttpSess(auth=('root', 'secret'), port=httpsport) as sess:
                # Simple request works
                async with sess.post(url, json={'name': '00.foosvc'}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'ok')
                    result = info.get('result')
                    provurl = result.get('url')

                async with await s_telepath.openurl(provurl) as prox:
                    provconf = await prox.getProvInfo()
                    self.isin('iden', provconf)
                    conf = provconf.get('conf')
                    self.eq(conf.get('aha:user'), 'root')
                    dmon_listen = conf.get('dmon:listen')
                    parts = s_telepath.chopurl(dmon_listen)
                    self.eq(parts.get('port'), 0)
                    self.none(conf.get('https:port'))

                # Full api works as well
                data = {'name': '01.foosvc',
                        'provinfo': {
                            'dmon:port': 12345,
                            'https:port': 8443,
                            'mirror': 'foosvc',
                            'conf': {
                                'aha:user': 'test',
                            }
                        }
                        }
                async with sess.post(url, json=data) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'ok')
                    result = info.get('result')
                    provurl = result.get('url')
                async with await s_telepath.openurl(provurl) as prox:
                    provconf = await prox.getProvInfo()
                    conf = provconf.get('conf')
                    self.eq(conf.get('aha:user'), 'test')
                    dmon_listen = conf.get('dmon:listen')
                    parts = s_telepath.chopurl(dmon_listen)
                    self.eq(parts.get('port'), 12345)
                    self.eq(conf.get('https:port'), 8443)

                # Sad path
                async with sess.post(url) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')
                async with sess.post(url, json={}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')
                async with sess.post(url, json={'name': 1234}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')
                async with sess.post(url, json={'name': ''}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')
                async with sess.post(url, json={'name': '00.newp', 'provinfo': 5309}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')
                async with sess.post(url, json={'name': '00.newp', 'provinfo': {'dmon:port': -1}}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'SchemaViolation')

                # Break the Aha cell - not will provision after this.
                _network = aha.conf.pop('aha:network')
                async with sess.post(url, json={'name': '00.newp'}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'NeedConfValu')

            # Not an admin
            await aha.addUser('lowuser', passwd='lowuser')
            async with self.getHttpSess(auth=('lowuser', 'lowuser'), port=httpsport) as sess:
                async with sess.post(url, json={'name': '00.newp'}) as resp:
                    info = await resp.json()
                    self.eq(info.get('status'), 'err')
                    self.eq(info.get('code'), 'AuthDeny')

    async def test_aha_connect_back(self):
        async with self.getTestAhaProv() as aha:  # type: s_aha.AhaCell

            async with self.addSvcToAha(aha, '00.exec', ExecTeleCaller) as conn:

                ahaurl = aha.conf.get('aha:urls')[0]
                ahaurl = s_telepath.modurl(ahaurl, user='root')

                # This adminapi fails if the ssl://root@aha.loop.vertex.link
                # session is not an admin user.
                await conn.exectelecall(ahaurl, 'getNexsIndx')

            self.true(conn.ahaclient.isfini)

    async def test_aha_util_helpers(self):

        # Mainly for test helper coverage.

        async with self.getTestAhaProv(conf={'auth:passwd': 'secret'}) as aha:  # type: s_aha.AhaCell
            root = await aha.auth.getUserByName('root')
            self.true(await root.tryPasswd('secret'))

            import synapse.cortex as s_cortex

            with self.getTestDir() as dirn:
                cdr0 = s_common.genpath(dirn, 'core00')
                cdr1 = s_common.genpath(dirn, 'core01')

                async with self.addSvcToAha(aha, '00.core', s_cortex.Cortex, dirn=cdr0) as core00:
                    async with self.addSvcToAha(aha, '01.core', s_cortex.Cortex, dirn=cdr1,
                                                provinfo={'mirror': 'core'}) as core01:
                        self.len(1, await core00.nodes('[inet:asn=0]'))
                        await core01.sync()
                        self.len(1, await core01.nodes('inet:asn=0'))

        # Simple test setups should work without issue
        async with self.getTestAhaProv() as aha:
            async with self.addSvcToAha(aha, '00.cell', s_cell.Cell) as cell00:  # type: s_cell.Cell
                async with self.addSvcToAha(aha, '01.cell', s_cell.Cell,
                                            provinfo={'mirror': 'cell'}) as cell01:  # type: s_cell.Cell
                    await cell01.sync()
                    # This should teardown cleanly.

    async def test_aha_restart(self):
        with self.withNexusReplay() as stack:

            with self.getTestDir() as dirn:
                ahadirn = s_common.gendir(dirn, 'aha')
                svc0dirn = s_common.gendir(dirn, 'svc00')
                svc1dirn = s_common.gendir(dirn, 'svc01')
                async with await s_base.Base.anit() as cm:
                    aconf = {
                        'aha:name': 'aha',
                        'aha:network': 'loop.vertex.link',
                        'provision:listen': 'ssl://aha.loop.vertex.link:0'
                    }
                    name = aconf.get('aha:name')
                    netw = aconf.get('aha:network')
                    dnsname = f'{name}.{netw}'

                    aha = await s_aha.AhaCell.anit(ahadirn, conf=aconf)
                    await cm.enter_context(aha)

                    addr, port = aha.provdmon.addr
                    # update the config to reflect the dynamically bound port
                    aha.conf['provision:listen'] = f'ssl://{dnsname}:{port}'

                    # do this config ex-post-facto due to port binding...
                    host, ahaport = await aha.dmon.listen(f'ssl://0.0.0.0:0?hostname={dnsname}&ca={netw}')
                    aha.conf['aha:urls'] = (f'ssl://{dnsname}:{ahaport}',)

                    onetime = await aha.addAhaSvcProv('00.svc', provinfo=None)
                    sconf = {'aha:provision': onetime}
                    s_common.yamlsave(sconf, svc0dirn, 'cell.yaml')
                    svc0 = await s_cell.Cell.anit(svc0dirn, conf=sconf)
                    await cm.enter_context(svc0)

                    onetime = await aha.addAhaSvcProv('01.svc', provinfo={'mirror': 'svc'})
                    sconf = {'aha:provision': onetime}
                    s_common.yamlsave(sconf, svc1dirn, 'cell.yaml')
                    svc1 = await s_cell.Cell.anit(svc1dirn, conf=sconf)
                    await cm.enter_context(svc1)

                    # Ensure that services have connected
                    await asyncio.wait_for(svc1.nexsroot._mirready.wait(), timeout=6)
                    await svc1.sync()

                    # Get Aha services
                    snfo = await aha.getAhaSvc('01.svc.loop.vertex.link')
                    svcinfo = snfo.get('svcinfo')
                    ready = svcinfo.get('ready')
                    self.true(ready)

                    # Fini the Aha service.
                    await aha.fini()

                    # Reuse our listening port we just deployed services with
                    aconf = {
                        'aha:name': 'aha',
                        'aha:network': 'loop.vertex.link',
                        'provision:listen': 'ssl://aha.loop.vertex.link:0',  # we do not care about provisioning
                        'dmon:listen': f'ssl://{dnsname}:{ahaport}?hostname={dnsname}&ca={netw}'
                    }

                    # Restart aha
                    aha = await s_aha.AhaCell.anit(ahadirn, conf=aconf)
                    await cm.enter_context(aha)

                    # services are cleared
                    snfo = await aha.getAhaSvc('01.svc.loop.vertex.link')
                    svcinfo = snfo.get('svcinfo')
                    ready = svcinfo.get('ready')
                    online = svcinfo.get('online')
                    self.none(online)
                    self.true(ready)  # Ready is not cleared upon restart

                    n = 3
                    if len(stack._exit_callbacks) > 0:
                        n = n * 2

                    waiter = aha.waiter(n, 'aha:svcadd')
                    self.ge(len(await waiter.wait(timeout=12)), n)

                    # svc01 has reconnected and the ready state has been re-registered
                    snfo = await aha.getAhaSvc('01.svc.loop.vertex.link')
                    svcinfo = snfo.get('svcinfo')
                    ready = svcinfo.get('ready')
                    online = svcinfo.get('online')
                    self.nn(online)
                    self.true(ready)

    async def test_aha_service_pools(self):

        async with self.getTestAhaProv() as aha:

            import synapse.cortex as s_cortex

            async with await s_base.Base.anit() as base:

                with self.getTestDir() as dirn:

                    dirn00 = s_common.genpath(dirn, 'cell00')
                    dirn01 = s_common.genpath(dirn, 'cell01')
                    dirn02 = s_common.genpath(dirn, 'cell02')

                    cell00 = await base.enter_context(self.addSvcToAha(aha, '00', s_cell.Cell, dirn=dirn00))
                    cell01 = await base.enter_context(self.addSvcToAha(aha, '01', s_cell.Cell, dirn=dirn01))

                    core00 = await base.enter_context(self.addSvcToAha(aha, 'core', s_cortex.Cortex, dirn=dirn02))

                    msgs = await core00.stormlist('aha.pool.list')
                    self.stormHasNoWarnErr(msgs)
                    self.stormIsInPrint('0 pools', msgs)

                    msgs = await core00.stormlist('aha.pool.add pool00...')
                    self.stormHasNoWarnErr(msgs)
                    self.stormIsInPrint('Created AHA service pool: pool00.loop.vertex.link', msgs)

                    with self.raises(s_exc.BadArg):
                        await s_telepath.open('aha://pool00...')

                    msgs = await core00.stormlist('aha.pool.svc.add pool00... 00...')
                    self.stormHasNoWarnErr(msgs)
                    self.stormIsInPrint('AHA service (00...) added to service pool (pool00.loop.vertex.link)', msgs)

                    poolinfo = await aha.getAhaPool('pool00...')
                    self.len(1, poolinfo['services'])

                    msgs = await core00.stormlist('aha.pool.list')
                    self.stormIsInPrint('Pool: pool00.loop.vertex.link', msgs)
                    self.stormIsInPrint('    00.loop.vertex.link', msgs)
                    self.stormIsInPrint('1 pools', msgs)

                    async with await s_telepath.open('aha://pool00...') as pool:

                        waiter = pool.waiter('svc:add', 2)

                        msgs = await core00.stormlist('aha.pool.svc.add pool00... 01...')
                        self.stormHasNoWarnErr(msgs)
                        self.stormIsInPrint('AHA service (01...) added to service pool (pool00.loop.vertex.link)', msgs)

                        await waiter.wait(timeout=3)

                        poolinfo = await aha.getAhaPool('pool00...')
                        self.len(2, poolinfo['services'])

                        self.nn(poolinfo['created'])
                        self.nn(poolinfo['services']['00.loop.vertex.link']['created'])
                        self.nn(poolinfo['services']['01.loop.vertex.link']['created'])

                        self.eq(core00.auth.rootuser.iden, poolinfo['creator'])
                        self.eq(core00.auth.rootuser.iden, poolinfo['services']['00.loop.vertex.link']['creator'])
                        self.eq(core00.auth.rootuser.iden, poolinfo['services']['01.loop.vertex.link']['creator'])

                        proxy00 = await pool.proxy(timeout=3)
                        run00 = await (await pool.proxy(timeout=3)).getCellRunId()
                        run01 = await (await pool.proxy(timeout=3)).getCellRunId()
                        self.ne(run00, run01)

                        waiter = pool.waiter('pool:reset', 1)

                        ahaproxy = await pool.aha.proxy()
                        await ahaproxy.fini()

                        await waiter.wait(timeout=3)

                        # wait for the pool to be notified of the topology change
                        waiter = pool.waiter('svc:del', 1)

                        msgs = await core00.stormlist('aha.pool.svc.del pool00... 00...')
                        self.stormHasNoWarnErr(msgs)
                        self.stormIsInPrint('AHA service (00...) removed from service pool (pool00.loop.vertex.link)', msgs)

                        await waiter.wait(timeout=3)
                        run00 = await (await pool.proxy(timeout=3)).getCellRunId()
                        self.eq(run00, await (await pool.proxy(timeout=3)).getCellRunId())

                        poolinfo = await aha.getAhaPool('pool00...')
                        self.len(1, poolinfo['services'])

                    msgs = await core00.stormlist('aha.pool.del pool00...')
                    self.stormHasNoWarnErr(msgs)
                    self.stormIsInPrint('Removed AHA service pool: pool00.loop.vertex.link', msgs)

    async def test_aha_reprovision(self):
        with self.withNexusReplay() as stack:
            with self.getTestDir() as dirn:
                aha00dirn = s_common.gendir(dirn, 'aha00')
                aha01dirn = s_common.gendir(dirn, 'aha01')
                svc0dirn = s_common.gendir(dirn, 'svc00')
                svc1dirn = s_common.gendir(dirn, 'svc01')
                async with await s_base.Base.anit() as cm:
                    aconf = {
                        'aha:name': 'aha',
                        'aha:network': 'loop.vertex.link',
                        'provision:listen': 'ssl://aha.loop.vertex.link:0'
                    }
                    name = aconf.get('aha:name')
                    netw = aconf.get('aha:network')
                    dnsname = f'{name}.{netw}'

                    aha = await s_aha.AhaCell.anit(aha00dirn, conf=aconf)
                    await cm.enter_context(aha)

                    addr, port = aha.provdmon.addr
                    # update the config to reflect the dynamically bound port
                    aha.conf['provision:listen'] = f'ssl://{dnsname}:{port}'

                    # do this config ex-post-facto due to port binding...
                    host, ahaport = await aha.dmon.listen(f'ssl://0.0.0.0:0?hostname={dnsname}&ca={netw}')
                    aha.conf['aha:urls'] = (f'ssl://{dnsname}:{ahaport}',)

                    onetime = await aha.addAhaSvcProv('00.svc', provinfo=None)
                    sconf = {'aha:provision': onetime}
                    s_common.yamlsave(sconf, svc0dirn, 'cell.yaml')
                    svc0 = await s_cell.Cell.anit(svc0dirn, conf=sconf)
                    await cm.enter_context(svc0)

                    onetime = await aha.addAhaSvcProv('01.svc', provinfo={'mirror': 'svc'})
                    sconf = {'aha:provision': onetime}
                    s_common.yamlsave(sconf, svc1dirn, 'cell.yaml')
                    svc1 = await s_cell.Cell.anit(svc1dirn, conf=sconf)
                    await cm.enter_context(svc1)

                    # Ensure that services have connected
                    await asyncio.wait_for(svc1.nexsroot._mirready.wait(), timeout=6)
                    await svc1.sync()

                    # Get Aha services
                    snfo = await aha.getAhaSvc('01.svc.loop.vertex.link')
                    svcinfo = snfo.get('svcinfo')
                    ready = svcinfo.get('ready')
                    self.true(ready)

                    await aha.fini()

                # Now re-deploy the AHA Service and re-provision the two cells
                # with the same AHA configuration
                async with await s_base.Base.anit() as cm:
                    aconf = {
                        'aha:name': 'aha',
                        'aha:network': 'loop.vertex.link',
                        'provision:listen': 'ssl://aha.loop.vertex.link:0'
                    }
                    name = aconf.get('aha:name')
                    netw = aconf.get('aha:network')
                    dnsname = f'{name}.{netw}'

                    aha = await s_aha.AhaCell.anit(aha01dirn, conf=aconf)
                    await cm.enter_context(aha)

                    addr, port = aha.provdmon.addr
                    # update the config to reflect the dynamically bound port
                    aha.conf['provision:listen'] = f'ssl://{dnsname}:{port}'

                    # do this config ex-post-facto due to port binding...
                    host, ahaport = await aha.dmon.listen(f'ssl://0.0.0.0:0?hostname={dnsname}&ca={netw}')
                    aha.conf['aha:urls'] = (f'ssl://{dnsname}:{ahaport}',)

                    onetime = await aha.addAhaSvcProv('00.svc', provinfo=None)
                    sconf = {'aha:provision': onetime}
                    s_common.yamlsave(sconf, svc0dirn, 'cell.yaml')
                    svc0 = await s_cell.Cell.anit(svc0dirn, conf=sconf)
                    await cm.enter_context(svc0)

                    onetime = await aha.addAhaSvcProv('01.svc', provinfo={'mirror': 'svc'})
                    sconf = {'aha:provision': onetime}
                    s_common.yamlsave(sconf, svc1dirn, 'cell.yaml')
                    svc1 = await s_cell.Cell.anit(svc1dirn, conf=sconf)
                    await cm.enter_context(svc1)

                    # Ensure that services have connected
                    await asyncio.wait_for(svc1.nexsroot._mirready.wait(), timeout=6)
                    await svc1.sync()

                    # Get Aha services
                    snfo = await aha.getAhaSvc('01.svc.loop.vertex.link')
                    svcinfo = snfo.get('svcinfo')
                    ready = svcinfo.get('ready')
                    self.true(ready)
