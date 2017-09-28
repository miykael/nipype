# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""Common graph operations for execution
"""
from __future__ import print_function, division, unicode_literals, absolute_import
from builtins import range, object, open

from copy import deepcopy
from glob import glob
import os
import shutil
import sys
from time import sleep, time
from traceback import format_exc

import numpy as np
import scipy.sparse as ssp

from ... import logging
from ...utils.filemanip import loadpkl
from ...utils.misc import str2bool
from ..engine.utils import (nx, dfs_preorder, topological_sort)
from ..engine import MapNode
from .tools import report_crash, report_nodes_not_run, create_pyscript

logger = logging.getLogger('workflow')


class PluginBase(object):
    """
    Base class for plugins

    Execution plugin API
    ====================

    Current status::

        class plugin_runner(PluginBase):

            def run(graph, config, updatehash)

    """

    def __init__(self, plugin_args=None):
        if plugin_args is None:
            plugin_args = {}
        self.plugin_args = plugin_args
        self._config = None

        self._status_callback = plugin_args.get('status_callback')
        return

    def run(self, graph, config, updatehash=False):
        raise NotImplementedError


class DistributedPluginBase(PluginBase):
    """Execute workflow with a distribution engine
    """

    def __init__(self, plugin_args=None):
        """Initialize runtime attributes to none

        procs: list (N) of underlying interface elements to be processed
        proc_done: a boolean numpy array (N) signifying whether a process has been
            executed
        proc_pending: a boolean numpy array (N) signifying whether a
            process is currently running. Note: A process is finished only when
            both proc_done==True and
        proc_pending==False
        depidx: a boolean matrix (NxN) storing the dependency structure accross
            processes. Process dependencies are derived from each column.
        """
        super(DistributedPluginBase, self).__init__(plugin_args=plugin_args)
        self.procs = None
        self.depidx = None
        self.refidx = None
        self.mapnodes = None
        self.mapnodesubids = None
        self.proc_done = None
        self.proc_pending = None
        self.pending_tasks = []
        self.max_jobs = self.plugin_args.get('max_jobs', np.inf)

    def _prerun_check(self, graph):
        """Stub."""

    def run(self, graph, config, updatehash=False):
        """
        Executes a pre-defined pipeline using distributed approaches
        """
        logger.info("Running in parallel.")
        self._config = config

        self._prerun_check(graph)
        # Generate appropriate structures for worker-manager model
        self._generate_dependency_list(graph)
        self.mapnodes = []
        self.mapnodesubids = {}
        # setup polling - TODO: change to threaded model
        notrun = []

        while not np.all(self.proc_done) or np.any(self.proc_pending):
            toappend = []
            # trigger callbacks for any pending results
            while self.pending_tasks:
                logger.debug('Processing %d pending tasks.', len(self.pending_tasks))
                taskid, jobid = self.pending_tasks.pop()
                try:
                    result = self._get_result(taskid)
                except Exception:
                    notrun.append(self._clean_queue(
                        jobid, graph, result={'result': None,
                                              'traceback': format_exc()}))
                else:
                    if result:
                        if result['traceback']:
                            notrun.append(self._clean_queue(jobid, graph,
                                                            result=result))
                        else:
                            self._task_finished_cb(jobid)
                            self._remove_node_dirs()
                        self._clear_task(taskid)
                    else:
                        toappend.insert(0, (taskid, jobid))

            if toappend:
                self.pending_tasks.extend(toappend)
            num_jobs = len(self.pending_tasks)
            logger.debug('Tasks currently running (%d).', num_jobs)
            if num_jobs < self.max_jobs:
                self._send_procs_to_workers(updatehash=updatehash,
                                            graph=graph)
            else:
                logger.debug('Not submitting (max jobs reached)')
            self._wait()

        self._remove_node_dirs()
        report_nodes_not_run(notrun)

        # close any open resources
        self._close()

    def _wait(self):
        sleep(float(self._config['execution']['poll_sleep_duration']))

    def _close(self):
        # close any open resources, this could raise NotImplementedError
        # but I didn't want to break other plugins
        return True

    def _get_result(self, taskid):
        raise NotImplementedError

    def _submit_job(self, node, updatehash=False):
        raise NotImplementedError

    def _report_crash(self, node, result=None):
        tb = None
        if result is not None:
            node._result = getattr(result, 'result')
            tb = getattr(result, 'traceback')
            node._traceback = tb
        return report_crash(node, traceback=tb)

    def _clear_task(self, taskid):
        raise NotImplementedError

    def _clean_queue(self, jobid, graph, result=None):
        logger.debug('Clearing %d from queue', jobid)

        if self._status_callback:
            self._status_callback(self.procs[jobid], 'exception')

        if str2bool(self._config['execution']['stop_on_first_crash']):
            raise RuntimeError("".join(result['traceback']))
        crashfile = self._report_crash(self.procs[jobid],
                                       result=result)
        if jobid in self.mapnodesubids:
            # remove current jobid
            self.proc_pending[jobid] = False
            self.proc_done[jobid] = True
            # remove parent mapnode
            jobid = self.mapnodesubids[jobid]
            self.proc_pending[jobid] = False
            self.proc_done[jobid] = True
        # remove dependencies from queue
        return self._remove_node_deps(jobid, crashfile, graph)

    def _submit_mapnode(self, jobid):
        if jobid in self.mapnodes:
            return True
        self.mapnodes.append(jobid)
        mapnodesubids = self.procs[jobid].get_subnodes()
        numnodes = len(mapnodesubids)
        logger.debug('Adding %d jobs for mapnode %s',
                     numnodes, self.procs[jobid]._id)
        for i in range(numnodes):
            self.mapnodesubids[self.depidx.shape[0] + i] = jobid
        self.procs.extend(mapnodesubids)
        self.depidx = ssp.vstack((self.depidx,
                                  ssp.lil_matrix(np.zeros(
                                      (numnodes, self.depidx.shape[1])))),
                                 'lil')
        self.depidx = ssp.hstack((self.depidx,
                                  ssp.lil_matrix(
                                      np.zeros((self.depidx.shape[0],
                                                numnodes)))),
                                 'lil')
        self.depidx[-numnodes:, jobid] = 1
        self.proc_done = np.concatenate((self.proc_done,
                                         np.zeros(numnodes, dtype=bool)))
        self.proc_pending = np.concatenate((self.proc_pending,
                                            np.zeros(numnodes, dtype=bool)))
        return False

    def _send_procs_to_workers(self, updatehash=False, graph=None):
        """ Sends jobs to workers
        """
        while not np.all(self.proc_done):
            num_jobs = len(self.pending_tasks)
            if np.isinf(self.max_jobs):
                slots = None
            else:
                slots = max(0, self.max_jobs - num_jobs)
            logger.debug('Slots available: %s' % slots)
            if (num_jobs >= self.max_jobs) or (slots == 0):
                break
            # Check to see if a job is available
            jobids = np.flatnonzero(
                ~self.proc_done & (self.depidx.sum(axis=0) == 0).__array__())

            if len(jobids) > 0:
                # send all available jobs
                logger.info('Pending[%d] Submitting[%d] jobs Slots[%d]',
                            num_jobs, len(jobids[:slots]), slots or 'inf')

                for jobid in jobids[:slots]:
                    if isinstance(self.procs[jobid], MapNode):
                        try:
                            num_subnodes = self.procs[jobid].num_subnodes()
                        except Exception:
                            self._clean_queue(jobid, graph)
                            self.proc_pending[jobid] = False
                            continue
                        if num_subnodes > 1:
                            submit = self._submit_mapnode(jobid)
                            if not submit:
                                continue
                    # change job status in appropriate queues
                    self.proc_done[jobid] = True
                    self.proc_pending[jobid] = True
                    # Send job to task manager and add to pending tasks
                    logger.info('Submitting: %s ID: %d' %
                                (self.procs[jobid]._id, jobid))
                    if self._status_callback:
                        self._status_callback(self.procs[jobid], 'start')
                    continue_with_submission = True
                    if str2bool(self.procs[jobid].config['execution']
                                ['local_hash_check']):
                        logger.debug('checking hash locally')
                        try:
                            hash_exists, _, _, _ = self.procs[
                                jobid].hash_exists()
                            logger.debug('Hash exists %s' % str(hash_exists))
                            if (hash_exists and (self.procs[jobid].overwrite is False or
                                (self.procs[jobid].overwrite is None and not
                                    self.procs[jobid]._interface.always_run))):
                                continue_with_submission = False
                                self._task_finished_cb(jobid)
                                self._remove_node_dirs()
                        except Exception:
                            self._clean_queue(jobid, graph)
                            self.proc_pending[jobid] = False
                            continue_with_submission = False
                    logger.debug('Finished checking hash %s' %
                                 str(continue_with_submission))
                    if continue_with_submission:
                        if self.procs[jobid].run_without_submitting:
                            logger.debug('Running node %s on master thread' %
                                         self.procs[jobid])
                            try:
                                self.procs[jobid].run()
                            except Exception:
                                self._clean_queue(jobid, graph)
                            self._task_finished_cb(jobid)
                            self._remove_node_dirs()
                        else:
                            tid = self._submit_job(deepcopy(self.procs[jobid]),
                                                   updatehash=updatehash)
                            if tid is None:
                                self.proc_done[jobid] = False
                                self.proc_pending[jobid] = False
                            else:
                                self.pending_tasks.insert(0, (tid, jobid))
                    logger.info('Finished submitting: %s ID: %d' %
                                (self.procs[jobid]._id, jobid))
            else:
                break

    def _task_finished_cb(self, jobid):
        """ Extract outputs and assign to inputs of dependent tasks

        This is called when a job is completed.
        """
        logger.info('[Job finished] jobname: %s jobid: %d' %
                    (self.procs[jobid]._id, jobid))
        if self._status_callback:
            self._status_callback(self.procs[jobid], 'end')
        # Update job and worker queues
        self.proc_pending[jobid] = False
        # update the job dependency structure
        rowview = self.depidx.getrowview(jobid)
        rowview[rowview.nonzero()] = 0
        if jobid not in self.mapnodesubids:
            self.refidx[self.refidx[:, jobid].nonzero()[0], jobid] = 0

    def _generate_dependency_list(self, graph):
        """ Generates a dependency list for a list of graphs.
        """
        self.procs, _ = topological_sort(graph)
        try:
            self.depidx = nx.to_scipy_sparse_matrix(graph,
                                                    nodelist=self.procs,
                                                    format='lil')
        except:
            self.depidx = nx.to_scipy_sparse_matrix(graph,
                                                    nodelist=self.procs)
        self.refidx = deepcopy(self.depidx)
        self.refidx.astype = np.int
        self.proc_done = np.zeros(len(self.procs), dtype=bool)
        self.proc_pending = np.zeros(len(self.procs), dtype=bool)

    def _remove_node_deps(self, jobid, crashfile, graph):
        subnodes = [s for s in dfs_preorder(graph, self.procs[jobid])]
        for node in subnodes:
            idx = self.procs.index(node)
            self.proc_done[idx] = True
            self.proc_pending[idx] = False
        return dict(node=self.procs[jobid],
                    dependents=subnodes,
                    crashfile=crashfile)

    def _remove_node_dirs(self):
        """Removes directories whose outputs have already been used up
        """
        if str2bool(self._config['execution']['remove_node_directories']):
            for idx in np.nonzero(
                                 (self.refidx.sum(axis=1) == 0).__array__())[0]:
                if idx in self.mapnodesubids:
                    continue
                if self.proc_done[idx] and (not self.proc_pending[idx]):
                    self.refidx[idx, idx] = -1
                    outdir = self.procs[idx]._output_directory()
                    logger.info(('[node dependencies finished] '
                                 'removing node: %s from directory %s') %
                                (self.procs[idx]._id, outdir))
                    shutil.rmtree(outdir)


class SGELikeBatchManagerBase(DistributedPluginBase):
    """Execute workflow with SGE/OGE/PBS like batch system
    """

    def __init__(self, template, plugin_args=None):
        super(SGELikeBatchManagerBase, self).__init__(plugin_args=plugin_args)
        self._template = template
        self._qsub_args = None
        if plugin_args:
            if 'template' in plugin_args:
                self._template = plugin_args['template']
                if os.path.isfile(self._template):
                    with open(self._template) as tpl_file:
                        self._template = tpl_file.read()
            if 'qsub_args' in plugin_args:
                self._qsub_args = plugin_args['qsub_args']
        self._pending = {}

    def _is_pending(self, taskid):
        """Check if a task is pending in the batch system
        """
        raise NotImplementedError

    def _submit_batchtask(self, scriptfile, node):
        """Submit a task to the batch system
        """
        raise NotImplementedError

    def _get_result(self, taskid):
        if taskid not in self._pending:
            raise Exception('Task %d not found' % taskid)
        if self._is_pending(taskid):
            return None
        node_dir = self._pending[taskid]
        # MIT HACK
        # on the pbs system at mit the parent node directory needs to be
        # accessed before internal directories become available. there
        # is a disconnect when the queueing engine knows a job is
        # finished to when the directories become statable.
        t = time()
        timeout = float(self._config['execution']['job_finished_timeout'])
        timed_out = True
        while (time() - t) < timeout:
            try:
                glob(os.path.join(node_dir, 'result_*.pklz')).pop()
                timed_out = False
                break
            except Exception as e:
                logger.debug(e)
            sleep(2)
        if timed_out:
            result_data = {'hostname': 'unknown',
                           'result': None,
                           'traceback': None}
            results_file = None
            try:
                error_message = ('Job id ({0}) finished or terminated, but '
                                 'results file does not exist after ({1}) '
                                 'seconds. Batch dir contains crashdump file '
                                 'if node raised an exception.\n'
                                 'Node working directory: ({2}) '.format(
                                     taskid, timeout, node_dir))
                raise IOError(error_message)
            except IOError as e:
                result_data['traceback'] = format_exc()
        else:
            results_file = glob(os.path.join(node_dir, 'result_*.pklz'))[0]
            result_data = loadpkl(results_file)
        result_out = dict(result=None, traceback=None)
        if isinstance(result_data, dict):
            result_out['result'] = result_data['result']
            result_out['traceback'] = result_data['traceback']
            result_out['hostname'] = result_data['hostname']
            if results_file:
                crash_file = os.path.join(node_dir, 'crashstore.pklz')
                os.rename(results_file, crash_file)
        else:
            result_out['result'] = result_data
        return result_out

    def _submit_job(self, node, updatehash=False):
        """submit job and return taskid
        """
        pyscript = create_pyscript(node, updatehash=updatehash)
        batch_dir, name = os.path.split(pyscript)
        name = '.'.join(name.split('.')[:-1])
        batchscript = '\n'.join((self._template,
                                 '%s %s' % (sys.executable, pyscript)))
        batchscriptfile = os.path.join(batch_dir, 'batchscript_%s.sh' % name)
        with open(batchscriptfile, 'wt') as fp:
            fp.writelines(batchscript)
        return self._submit_batchtask(batchscriptfile, node)

    def _clear_task(self, taskid):
        del self._pending[taskid]


class GraphPluginBase(PluginBase):
    """Base class for plugins that distribute graphs to workflows
    """

    def __init__(self, plugin_args=None):
        if plugin_args and plugin_args.get('status_callback'):
            logger.warning('status_callback not supported for Graph submission plugins')
        super(GraphPluginBase, self).__init__(plugin_args=plugin_args)

    def run(self, graph, config, updatehash=False):
        pyfiles = []
        dependencies = {}
        self._config = config
        nodes = nx.topological_sort(graph)
        logger.debug('Creating executable python files for each node')
        for idx, node in enumerate(nodes):
            pyfiles.append(create_pyscript(node,
                                           updatehash=updatehash,
                                           store_exception=False))
            dependencies[idx] = [nodes.index(prevnode) for prevnode in
                                 graph.predecessors(node)]
        self._submit_graph(pyfiles, dependencies, nodes)

    def _get_args(self, node, keywords):
        values = ()
        for keyword in keywords:
            value = getattr(self, "_" + keyword)
            if keyword == "template" and os.path.isfile(value):
                with open(value) as f:
                    value = f.read()
            if (hasattr(node, "plugin_args") and
                    isinstance(node.plugin_args, dict) and
                    keyword in node.plugin_args):
                if (keyword == "template" and
                        os.path.isfile(node.plugin_args[keyword])):
                    with open(node.plugin_args[keyword]) as f:
                        tmp_value = f.read()
                else:
                    tmp_value = node.plugin_args[keyword]

                if ('overwrite' in node.plugin_args and
                        node.plugin_args['overwrite']):
                    value = tmp_value
                else:
                    value += tmp_value
            values += (value, )
        return values

    def _submit_graph(self, pyfiles, dependencies, nodes):
        """
        pyfiles: list of files corresponding to a topological sort
        dependencies: dictionary of dependencies based on the toplogical sort
        """
        raise NotImplementedError

    def _get_result(self, taskid):
        if taskid not in self._pending:
            raise Exception('Task %d not found' % taskid)
        if self._is_pending(taskid):
            return None
        node_dir = self._pending[taskid]

        glob(os.path.join(node_dir, 'result_*.pklz')).pop()

        results_file = glob(os.path.join(node_dir, 'result_*.pklz'))[0]
        result_data = loadpkl(results_file)
        result_out = dict(result=None, traceback=None)

        if isinstance(result_data, dict):
            result_out['result'] = result_data['result']
            result_out['traceback'] = result_data['traceback']
            result_out['hostname'] = result_data['hostname']
            if results_file:
                crash_file = os.path.join(node_dir, 'crashstore.pklz')
                os.rename(results_file, crash_file)
        else:
            result_out['result'] = result_data

        return result_out
