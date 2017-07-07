# -*- coding: utf-8 -*-
# Copyright (c) 2017  Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Written by Chenxiong Qi <cqi@redhat.com>

import contextlib


class KojiService(object):
    """Wrapper of Koji API and profile configuration

    As an interface of Koji profile configuration, KojiService exposes part
    of options that would be used frequently. However, other options are still
    accessible from ``config`` property.

    As a wrapper of Koji API, new APIs could be added as well.
    """

    def __init__(self, profile=None, logger=None):
        import koji

        self._config = koji.read_config(profile or 'koji')
        self._logger = logger

    @property
    def config(self):
        return self._config

    @property
    def weburl(self):
        return self.config['weburl']

    @property
    def server(self):
        return self.config['server']

    @property
    def session(self):
        import koji
        if not hasattr(self, '_session'):
            self._session = koji.ClientSession(self.config['server'],
                                               {'krb_rdns': self.config['krb_rdns']})
        return self._session

    def krb_login(self, proxyuser=None):
        self.session.krb_login(proxyuser=proxyuser)

    @property
    def logged_in(self):
        return self.session.logged_in

    def logout(self):
        self.session.logout()

    def build_container(self, source_url, branch,
                        namespace=None, scratch=None, repo_urls=None):
        """Build container by buildContainer"""

        build_target = '{}-{}-candidate'.format(
            'rawhide' if branch == 'master' else branch,
            'container' if namespace is None else namespace)

        build_opts = {
            'scratch': False if scratch is None else True,
            'git_branch': branch,
        }
        if repo_urls:
            build_opts['yum_repourls'] = repo_urls

        if self._logger:
            self._logger.debug('Build from target: %s', build_target)
            self._logger.debug('Build options: %s', build_opts)

        task_id = self.session.buildContainer(source_url, build_target, build_opts)

        if self._logger:
            self._logger.info('Task %s is created to build docker image for %s',
                              task_id, source_url)
            self._logger.info('Task info: %s/taskinfo?taskID=%s', self.weburl, task_id)

        return task_id

    def get_build_rpms(self, build_nvr, arches=None):
        build_info = self.session.getBuild(build_nvr)
        return self.session.listRPMs(buildID=build_info['id'],
                                     arches=arches)


@contextlib.contextmanager
def koji_service(profile=None, logger=None):
    """A Koji service context manager that could be used with with

    Example::

        with KojiService() as service:
            ...

        # if you want it to log something
        with KojiService(logger=logger) as service:
            ...

        # if you want it to use alternative Koji profile rather than the default one koji
        with KojiService(koji='stg', logger=logger) as service:
            ...
    """
    service = KojiService(profile=profile, logger=logger)
    try:
        yield service
    finally:
        if service.logged_in:
            if logger:
                logger.debug('Logout Koji session')
            service.logout()
