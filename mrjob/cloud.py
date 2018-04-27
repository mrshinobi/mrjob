# -*- coding: utf-8 -*-
# Copyright 2017 Yelp
# Copyright 2018 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import os
import pipes
import socket
import random
import time
from os.path import basename
from signal import SIGKILL
from subprocess import Popen
from subprocess import PIPE

from mrjob.bin import MRJobBinRunner
from mrjob.conf import combine_dicts
from mrjob.py2 import xrange
from mrjob.setup import WorkingDirManager
from mrjob.setup import parse_setup_cmd
from mrjob.util import cmd_line
from mrjob.util import file_ext

log = logging.getLogger(__name__)

# don't try to bind SSH tunnel to more than this many local ports
_MAX_SSH_RETRIES = 20


# map archive file extensions to the command used to unarchive them
_EXT_TO_UNARCHIVE_CMD = {
    '.zip': 'unzip -o %(file)s -d %(dir)s',
    '.tar': 'mkdir %(dir)s; tar xf %(file)s -C %(dir)s',
    '.tar.gz': 'mkdir %(dir)s; tar xfz %(file)s -C %(dir)s',
    '.tgz': 'mkdir %(dir)s; tar xfz %(file)s -C %(dir)s',
}

# issue a warning if max_mins_idle is set to less than this
_DEFAULT_MAX_MINS_IDLE = 10.0


class HadoopInTheCloudJobRunner(MRJobBinRunner):
    """Abstract base class for all Hadoop-in-the-cloud services."""

    alias = '_cloud'

    OPT_NAMES = MRJobBinRunner.OPT_NAMES | {
        'bootstrap',
        'bootstrap_python',
        'check_cluster_every',
        'cloud_fs_sync_secs',
        'cloud_tmp_dir',
        'cluster_id',
        'core_instance_type',
        'extra_cluster_params',
        'image_version',
        'instance_type',
        'master_instance_type',
        'max_mins_idle',
        'max_hours_idle',
        'num_core_instances',
        'num_task_instances',
        'region',
        'ssh_bind_ports',
        'ssh_tunnel',
        'ssh_tunnel_is_open',
        'task_instance_type',
        'zone',
    }

    # so far, every service provides the ability to run bootstrap scripts
    _BOOTSTRAP_MRJOB_IN_SETUP = False

    def __init__(self, **kwargs):
        super(HadoopInTheCloudJobRunner, self).__init__(**kwargs)

        # if *cluster_id* is not set, ``self._cluster_id`` will be
        # set when we create or join a cluster
        self._cluster_id = self._opts['cluster_id']

        # bootstrapping
        self._bootstrap = self._bootstrap_python() + self._parse_bootstrap()

        # add files to manager
        self._bootstrap_dir_mgr = WorkingDirManager()

        for cmd in self._bootstrap:
            for token in cmd:
                if isinstance(token, dict):
                    # convert dir archive tokens to archives
                    if token['type'] == 'dir':
                        token['path'] = self._dir_archive_path(token['path'])
                        token['type'] = 'archive'

                    self._bootstrap_dir_mgr.add(**token)

        # we'll create this script later, as needed
        self._master_bootstrap_script_path = None

        # ssh state

        # the process for the SSH tunnel
        self._ssh_proc = None

        # if this is true, stop trying to launch the SSH tunnel
        self._give_up_on_ssh_tunnel = False

        # store the (tunneled) URL of the job tracker/resource manager
        self._ssh_tunnel_url = None



    ### Options ###

    def _default_opts(self):
        return combine_dicts(
            super(HadoopInTheCloudJobRunner, self)._default_opts(),
            dict(
                max_mins_idle=_DEFAULT_MAX_MINS_IDLE,
                # don't use a list because it makes it hard to read option
                # values when running in verbose mode. See #1284
                ssh_bind_ports=xrange(40001, 40841),
                ssh_tunnel=False,
                ssh_tunnel_is_open=False,
                # ssh_bin isn't included here. For example, the Dataproc
                # runner launches ssh through the gcloud util
            ),
        )

    def _fix_opts(self, opts, source=None):
        opts = super(HadoopInTheCloudJobRunner, self)._fix_opts(
            opts, source=source)

        # patch max_hours_idle into max_mins_idle (see #1663)
        if opts.get('max_hours_idle') is not None:
            log.warning(
                'max_hours_idle is deprecated and will be removed in v0.7.0.' +
                (' Please use max_mins_idle instead'
                 if opts.get('max_mins_idle') is None else ''))

            if opts.get('max_mins_idle') is None:
                opts['max_mins_idle'] = opts['max_hours_idle'] * 60

        return opts

    def _combine_opts(self, opt_list):
        """Propagate *instance_type* to other instance type opts, if not
        already set.

        Also propagate core instance type to task instance type, if it's
        not already set.
        """
        opts = super(HadoopInTheCloudJobRunner, self)._combine_opts(opt_list)

        if opts['instance_type']:
            # figure out how late in the configs opt was set (setting
            # --instance_type on the command line overrides core_instance_type
            # set in configs)
            opt_priority = {k: -1 for k in opts}

            for i, sub_opts in enumerate(opt_list):
                for k, v in sub_opts.items():
                    if v == opts[k]:
                        opt_priority[k] = i

            # instance_type only affects master_instance_type if there are
            # no other instances
            if opts['num_core_instances'] or opts['num_task_instances']:
                propagate_to = ['core_instance_type', 'task_instance_type']
            else:
                propagate_to = ['master_instance_type']

            for k in propagate_to:
                if opts[k] is None or (
                        opt_priority[k] < opt_priority['instance_type']):
                    opts[k] = opts['instance_type']

        if not opts['task_instance_type']:
            opts['task_instance_type'] = opts['core_instance_type']

        return opts

    ### Bootstrapping ###

    def _bootstrap_python(self):
        """Redefine this to return a (possibly empty) list of parsed commands
        (in the same format as returned by parse_setup_cmd())' to make sure a
        compatible version of Python is installed

        If the *bootstrap_python* option is false, should always return ``[]``.
        """
        return []

    def _cp_to_local_cmd(self):
        """Command to copy files from the cloud to the local directory
        (usually via Hadoop). Redefine this as needed; for example, on EMR,
        we sometimes have to use ``aws s3 cp`` because ``hadoop`` isn't
        installed at bootstrap time."""
        return 'hadoop fs -copyToLocal'

    def _parse_bootstrap(self):
        """Parse the *bootstrap* option with
        :py:func:`mrjob.setup.parse_setup_cmd()`.
        """
        return [parse_setup_cmd(cmd) for cmd in self._opts['bootstrap']]

    def _create_master_bootstrap_script_if_needed(self):
        """Helper for :py:meth:`_add_bootstrap_files_for_upload`.

        Create the master bootstrap script and write it into our local
        temp directory. Set self._master_bootstrap_script_path.

        This will do nothing if there are no bootstrap scripts or commands,
        or if it has already been called."""
        if self._master_bootstrap_script_path:
            return

        # don't bother if we're not starting a cluster
        if self._cluster_id:
            return

        # Also don't bother if we're not bootstrapping
        if not (self._bootstrap or self._bootstrap_mrjob()):
            return

        # create mrjob.zip if we need it, and add commands to install it
        mrjob_bootstrap = []
        if self._bootstrap_mrjob():
            assert self._mrjob_zip_path
            path_dict = {
                'type': 'file', 'name': None, 'path': self._mrjob_zip_path}
            self._bootstrap_dir_mgr.add(**path_dict)

            # find out where python keeps its libraries
            mrjob_bootstrap.append([
                "__mrjob_PYTHON_LIB=$(%s -c "
                "'from distutils.sysconfig import get_python_lib;"
                " print(get_python_lib())')" %
                cmd_line(self._python_bin())])

            # remove anything that might be in the way (see #1567)
            mrjob_bootstrap.append(['sudo rm -rf $__mrjob_PYTHON_LIB/mrjob'])

            # unzip mrjob.zip
            mrjob_bootstrap.append(
                ['sudo unzip ', path_dict, ' -d $__mrjob_PYTHON_LIB'])

            # re-compile pyc files now, since mappers/reducers can't
            # write to this directory. Don't fail if there is extra
            # un-compileable crud in the tarball (this would matter if
            # sh_bin were 'sh -e')
            mrjob_bootstrap.append(
                ['sudo %s -m compileall -q'
                 ' -f $__mrjob_PYTHON_LIB/mrjob && true' %
                 cmd_line(self._python_bin())])

        path = os.path.join(self._get_local_tmp_dir(), 'b.sh')
        log.info('writing master bootstrap script to %s' % path)

        contents = self._master_bootstrap_script_content(
            self._bootstrap + mrjob_bootstrap)

        self._write_script(contents, path, 'master bootstrap script')

        self._master_bootstrap_script_path = path

    def _master_bootstrap_script_content(self, bootstrap):
        """Return a list containing the lines of the master bootstrap script.
        (without trailing newlines)
        """
        out = []

        # shebang, precommands
        out.extend(self._start_of_sh_script())
        out.append('')

        # store $PWD
        out.append('# store $PWD')
        out.append('__mrjob_PWD=$PWD')
        out.append('')

        # special case for PWD being in /, which happens on Dataproc
        # (really we should cd to tmp or something)
        out.append('if [ $__mrjob_PWD = "/" ]; then')
        out.append('  __mrjob_PWD=""')
        out.append('fi')
        out.append('')

        # run commands in a block so we can redirect stdout to stderr
        # (e.g. to catch errors from compileall). See #370
        out.append('{')

        # download files
        out.append('  # download files and mark them executable')

        cp_to_local = self._cp_to_local_cmd()

        # TODO: why bother with $__mrjob_PWD here, since we're already in it?
        for name, path in sorted(
                self._bootstrap_dir_mgr.name_to_path('file').items()):
            uri = self._upload_mgr.uri(path)
            out.append('  %s %s $__mrjob_PWD/%s' %
                       (cp_to_local, pipes.quote(uri), pipes.quote(name)))
            # imitate Hadoop Distributed Cache (see #1602)
            out.append('  chmod u+rx $__mrjob_PWD/%s' % pipes.quote(name))
            out.append('')

        # download and unarchive archives
        archive_names_and_paths = sorted(
            self._bootstrap_dir_mgr.name_to_path('archive').items())
        if archive_names_and_paths:
            # make tmp dir if needed
            out.append('  # download and unpack archives')
            out.append('  __mrjob_TMP=$(mktemp -d)')
            out.append('')

            for name, path in archive_names_and_paths:
                uri = self._upload_mgr.uri(path)
                ext = file_ext(basename(path))

                # copy file to tmp dir
                quoted_archive_path = '$__mrjob_TMP/%s' % pipes.quote(name)

                out.append('  %s %s %s' % (
                    cp_to_local, pipes.quote(uri), quoted_archive_path))

                # unarchive file
                if ext not in _EXT_TO_UNARCHIVE_CMD:
                    raise KeyError('unknown archive file extension: %s' % path)
                unarchive_cmd = _EXT_TO_UNARCHIVE_CMD[ext]

                out.append('  ' + unarchive_cmd % dict(
                    file=quoted_archive_path,
                    dir='$__mrjob_PWD/' + pipes.quote(name)))

                # imitate Hadoop Distributed Cache (see #1602)
                out.append(
                    '  chmod u+rx -R $__mrjob_PWD/%s' % pipes.quote(name))

                out.append('')

        # run bootstrap commands
        out.append('  # bootstrap commands')
        for cmd in bootstrap:
            # reconstruct the command line, substituting $__mrjob_PWD/<name>
            # for path dicts
            line = '  '
            for token in cmd:
                if isinstance(token, dict):
                    # it's a path dictionary
                    line += '$__mrjob_PWD/'
                    line += pipes.quote(self._bootstrap_dir_mgr.name(**token))
                else:
                    # it's raw script
                    line += token
            out.append(line)

        out.append('} 1>&2')  # stdout -> stderr for ease of error log parsing

        return out

    def _start_of_sh_script(self):
        """Return a list of lines (without trailing newlines) containing the
        shell script shebang and pre-commands."""
        out = []

        # shebang
        sh_bin = self._sh_bin()
        if not sh_bin[0].startswith('/'):
            sh_bin = ['/usr/bin/env'] + sh_bin
        out.append('#!' + cmd_line(sh_bin))

        # hook for 'set -e', etc. (see #1549)
        out.extend(self._sh_pre_commands())

        return out

    ### Launching Clusters ###

    def _add_extra_cluster_params(self, params):
        """Return a dict with the *extra_cluster_params* opt patched into
        *params*, and ``None`` values removed."""
        params = params.copy()
        params.update(self._opts['extra_cluster_params'])
        params = {k: v for k, v in params.items() if v is not None}

        return params

    ### SSH Tunnel ###

    def _ssh_tunnel_args(self, bind_port):
        """Redefine this in your subclass. You will probably want to call
        :py:meth:`_ssh_tunnel_opts` somewhere in here.

        Should return the list of args used to run the command
        to open the SSH tunnel, bound to *bind_port* on your computer,
        or ``None`` if it isn't possible to set up an SSH tunnel.
        """
        return None

    def _ssh_tunnel_config(self):
        """Redefine this in your subclass. Should return a dict with the
        following keys:

        *localhost*: once we SSH in, is the web interface?
                     reachable at ``localhost``
        *name*: either ``'job tracker'`` or ``'resource manager'``
        *path*: path of main page on web interface (e.g. "/cluster")
        *port*: port number of the web interface
        """
        raise NotImplementedError

    def _launch_ssh_proc(self, args):
        """The command used to create a :py:class:`subprocess.Popen` to
        run the SSH tunnel. You usually don't need to redefine this."""
        log.debug('> %s' % cmd_line(args))
        return Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE)

    def _ssh_launch_wait_secs(self):
        """Wait this long after launching the SSH process before checking
        for failure (default 1 second). You may redefine this."""
        return 1.0

    def _set_up_ssh_tunnel(self):
        """Call this whenever you think it is possible to SSH to your cluster.
        This sets :py:attr:`_ssh_proc`. Does nothing if :mrjob-opt:`ssh_tunnel`
        is not set, or there is already a tunnel process running.
        """
        # did the user request an SSH tunnel?
        if not self._opts['ssh_tunnel']:
            return

        # no point in trying to launch a nonexistent command twice
        if self._give_up_on_ssh_tunnel:
            return

        # did we already launch the SSH tunnel process? is it still running?
        if self._ssh_proc:
            self._ssh_proc.poll()
            if self._ssh_proc.returncode is None:
                return
            else:
                log.warning('  Oops, ssh subprocess exited with return code'
                            ' %d, restarting...' % self._ssh_proc.returncode)
                self._ssh_proc = None

        tunnel_config = self._ssh_tunnel_config()

        bind_port = None
        popen_exception = None
        ssh_tunnel_args = []

        for bind_port in self._pick_ssh_bind_ports():
            ssh_proc = None
            ssh_tunnel_args = self._ssh_tunnel_args(bind_port)

            # can't launch SSH tunnel right now
            if not ssh_tunnel_args:
                return

            try:
                ssh_proc = self._launch_ssh_proc(ssh_tunnel_args)
            except OSError as ex:
                # e.g. OSError(2, 'File not found')
                popen_exception = ex   # warning handled below
                break

            if ssh_proc:
                time.sleep(self._ssh_launch_wait_secs())

                ssh_proc.poll()
                # still running. We are golden
                if ssh_proc.returncode is None:
                    self._ssh_proc = ssh_proc
                    break
                else:
                    ssh_proc.stdin.close()
                    ssh_proc.stdout.close()
                    ssh_proc.stderr.close()

        if self._ssh_proc:
            if self._opts['ssh_tunnel_is_open']:
                bind_host = socket.getfqdn()
            else:
                bind_host = 'localhost'
            self._ssh_tunnel_url = 'http://%s:%d%s' % (
                bind_host, bind_port, tunnel_config['path'])
            log.info('  Connect to %s at: %s' % (
                tunnel_config['name'], self._ssh_tunnel_url))

        else:
            if popen_exception:
                # this only happens if the ssh binary is not present
                # or not executable (so tunnel_config and the args to the
                # ssh binary don't matter)
                log.warning("    Couldn't run %s: %s" % (
                    cmd_line(ssh_tunnel_args[:1]), popen_exception))
                self._give_up_on_ssh_tunnel = True
                return
            else:
                log.warning(
                    '    Failed to open ssh tunnel to %s' %
                    tunnel_config['name'])

    def _kill_ssh_tunnel(self):
        """Send SIGKILL to SSH tunnel, if it's running."""
        if not self._ssh_proc:
            return

        self._ssh_proc.poll()
        if self._ssh_proc.returncode is None:
            log.info('Killing our SSH tunnel (pid %d)' %
                     self._ssh_proc.pid)

            self._ssh_proc.stdin.close()
            self._ssh_proc.stdout.close()
            self._ssh_proc.stderr.close()

            try:
                os.kill(self._ssh_proc.pid, SIGKILL)
            except Exception as e:
                log.exception(e)

        self._ssh_proc = None
        self._ssh_tunnel_url = None

    def _ssh_tunnel_opts(self, bind_port):
        """Options to SSH related to setting up a tunnel (rather than
        SSHing in). Helper for :py:meth:`_ssh_tunnel_args`.
        """
        args = self._ssh_local_tunnel_opt(bind_port) + [
            '-N', '-n', '-q',
        ]
        if self._opts['ssh_tunnel_is_open']:
            args.extend(['-g', '-4'])  # -4: listen on IPv4 only

        return args

    def _ssh_local_tunnel_opt(self, bind_port):
        """Helper for :py:meth:`_ssh_tunnel_opts`."""
        tunnel_config = self._ssh_tunnel_config()

        return [
            '-L', '%d:%s:%d' % (
                bind_port,
                self._job_tracker_host(),
                tunnel_config['port'],
            ),
        ]

    def _pick_ssh_bind_ports(self):
        """Pick a list of ports to try binding our SSH tunnel to.

        We will try to bind the same port for any given cluster (Issue #67)
        """
        # don't perturb the random number generator
        random_state = random.getstate()
        try:
            # seed random port selection on cluster ID
            random.seed(self._cluster_id)
            num_picks = min(_MAX_SSH_RETRIES,
                            len(self._opts['ssh_bind_ports']))
            return random.sample(self._opts['ssh_bind_ports'], num_picks)
        finally:
            random.setstate(random_state)
