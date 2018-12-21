import os
import json
import pathlib
import contextlib

import synapse.glob as s_glob
import synapse.common as s_common
import synapse.cortex as s_cortex
import synapse.telepath as s_telepath

import synapse.lib.base as s_base
import synapse.lib.cmdr as s_cmdr
import synapse.lib.node as s_node
import synapse.lib.msgpack as s_msgpack

def getDocFile(fn, root=None):  # pragma: no cover
    '''
    Helper for getting a documentation data file paths.
    Mainly used for loading data into temporary cortexes.
    '''
    cwd = pathlib.Path(os.getcwd())
    if root:
        cwd = pathlib.Path(root)
    # Walk up a directory until you find '...d./data'
    while True:
        dpath = cwd.joinpath('docdata')
        if dpath.is_dir():
            break
        parent = cwd.parent
        if parent == cwd:
            raise ValueError(f'Unable to find data directory from {os.getwcd()}.')
        cwd = parent

    # Protect against traversal
    fpath = os.path.abspath(os.path.join(dpath.as_posix(), fn))
    if not fpath.startswith(dpath.as_posix()):
        raise ValueError(f'Path escaping detected: {fn}')

    # Existence
    if not os.path.isfile(fpath):
        raise ValueError(f'File does not exist: {fn}')

    return fpath

def getDocData(fp, root=None):
    '''
    Helper for getting a data from documentation data files.
    Mainly used for loading data into temporary cortexes.

    Will detect json/jsonl/yaml/mpk extensions and automatically
    decode that data if found; otherwise it returns bytes.
    '''
    fpath = getDocFile(fp, root)
    if fpath.endswith('.yaml'):
        return s_common.yamlload(fpath)
    if fpath.endswith('.json'):
        return s_common.jsload(fpath)
    with s_common.genfile(fpath) as fd:
        if fpath.endswith('.mpk'):
            return s_msgpack.un(fd.read())
        if fpath.endswith('.jsonl'):
            recs = []
            for line in fd.readlines():
                recs.append(json.loads(line.decode()))
            return recs
        return fd.read()


@contextlib.asynccontextmanager
async def genTempCoreProxy(mods=None):
    '''Get a temporary cortex proxy.'''
    with s_common.getTempDir() as dirn:
        async with await s_cortex.Cortex.anit(dirn) as core:
            if mods:
                for mod in mods:
                    await core.loadCoreModule(mod)
            async with core.getLocalProxy() as prox:
                yield prox

async def getItemCmdr(prox, outp=None, locs=None):
    '''Get a Cmdr instance with prepopulated locs'''
    cmdr = await s_cmdr.getItemCmdr(prox, outp=outp)
    cmdr.echoline = True
    if locs:
        cmdr.locs.update(locs)
    return cmdr

class CmdrCore(s_base.Base):
    '''
    A helper for jupyter/storm CLI interaction
    '''
    async def __anit__(self, core, outp=None):
        await s_base.Base.__anit__(self)
        self.prefix = 'storm'  # Eventually we may remove or change this
        self.core = core
        locs = {'storm:hide-unknown': True}
        self.cmdr = await getItemCmdr(self.core, outp=outp, locs=locs)
        self.onfini(self._onCmdrCoreFini)
        self.acm = None  # A placeholder for the context manager

    async def addFeedData(self, name, items, seqn=None):
        return await self.core.addFeedData(name, items, seqn)

    async def _runStorm(self, text, opts=None, cmdr=False):
        mesgs = []

        if cmdr:

            if self.prefix:
                text = ' '.join((self.prefix, text))

            def onEvent(event):
                mesg = event[1].get('mesg')
                mesgs.append(mesg)

            with self.cmdr.onWith('storm:mesg', onEvent):
                await self.cmdr.runCmdLine(text)

        else:
            async for mesg in await self.core.storm(text, opts=opts):
                mesgs.append(mesg)

        return mesgs

    async def storm(self, text, opts=None, num=None, cmdr=False):
        '''
        A helper for executing a storm command and getting a list of storm messages.

        Args:
            text (str): Storm command to execute.
            opts (dict): Opt to pass to the cortex during execution.
            num (int): Number of nodes to expect in the output query. Checks that with an assert statement.
            cmdr (bool): If True, executes the line via the Cmdr CLI and will send output to outp.

        Notes:
            The opts dictionary will not be used if cmdr=True.

        Returns:
            list: A list of storm messages.
        '''
        mesgs = await self._runStorm(text, opts, cmdr)
        if num is not None:
            nodes = [m for m in mesgs if m[0] == 'node']
            assert len(nodes) == num

        return mesgs

    async def eval(self, text, opts=None, num=None, cmdr=False):
        '''
        A helper for executing a storm command and getting a list of packed nodes.

        Args:
            text (str): Storm command to execute.
            opts (dict): Opt to pass to the cortex during execution.
            num (int): Number of nodes to expect in the output query. Checks that with an assert statement.
            cmdr (bool): If True, executes the line via the Cmdr CLI and will send output to outp.

        Notes:
            The opts dictionary will not be used if cmdr=True.

        Returns:
            list: A list of packed nodes.
        '''
        mesgs = await self._runStorm(text, opts, cmdr)

        nodes = [m[1] for m in mesgs if m[0] == 'node']

        if num is not None:
            assert len(nodes) == num

        return nodes

    async def _onCmdrCoreFini(self):
        self.cmdr.fini()
        # await self.core.fini()
        # If self.acm is set, acm.__aexit should handle the self.core fini.
        if self.acm:
            await self.acm.__aexit__(None, None, None)

async def getTempCoreProx(mods=None):
    '''
    Get a Telepath Proxt to a Cortex instance which is backed by a temporary Cortex.

    Args:
        mods (list): A list of additional CoreModules to load in the Cortex.

    Notes:
        The Proxy returned by this should be fini()'d to tear down the temporary Cortex.

    Returns:
        s_telepath.Proxy
    '''
    acm = genTempCoreProxy(mods)
    core = await acm.__aenter__()
    # Use object.__setattr__ to hulk smash and avoid proxy getattr magick
    object.__setattr__(core, '_acm', acm)
    async def onfini():
        await core._acm.__aexit__(None, None, None)
    core.onfini(onfini)
    return core

async def getTempCoreCmdr(mods=None, outp=None):
    '''
    Get a CmdrCore instance which is backed by a temporary Cortex.

    Args:
        mods (list): A list of additional CoreModules to load in the Cortex.
        outp: A output helper.  Will be used for the Cmdr instance.

    Notes:
        The CmdrCore returned by this should be fini()'d to tear down the temporary Cortex.

    Returns:
        CmdrCore: A CmdrCore instance.
    '''
    acm = genTempCoreProxy(mods)
    prox = await acm.__aenter__()
    cmdrcore = await CmdrCore.anit(prox, outp=outp)
    cmdrcore.acm = acm
    return cmdrcore
